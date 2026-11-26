"""决策提示词：把结构化观察 + 最近操作历史交给语言模型，产出下一步动作。

支持两种观察来源（观察契约一致，提示词按来源微调措辞）：
- 视觉观察（截图分析）：带 important_objects 的截图坐标 point；
- 结构化观察（bridge/statewatch 直读）：无坐标；是否提供「游戏 API 直调动作」组
  由调用方按通道能力显式控制（bridge_actions 参数，桥接可用时才置 True）——
  statewatch 通道等价于桥接不可达，绝不能注入桥动作提示（T1 修复）。
"""
from __future__ import annotations

import json
import re
from typing import Any, Optional

from game_agent.contracts import STATE_GUARDED_BLOCKS
from game_agent.vision.analyzer import GAME_CONTEXT

ACTION_TOOLS = """可用动作（必须从以下白名单中选择；params 为参数对象）：
- click：鼠标点击。params: {"x": 整数, "y": 整数} —— 目标中心点的「截图坐标」
- ctrl_click：按住 Ctrl 并点击（游戏内=「快速转移物品」：把物品移入/移出交易桌）。
  params: {"x": 整数, "y": 整数}（截图坐标）
- move：仅移动鼠标（悬停查看）。params: {"x": 整数, "y": 整数}
- press_key：按键。params: {"key": "escape|enter|space|up|down|left|right|字母|数字|f1..f12", "times": 整数(可选)}
- type_text：输入 ASCII 文本。params: {"text": "..."}
- wait：等待画面加载/动画。params: {"seconds": 0.5~8}
- stop：停止操作（当前无法安全继续、或已完成任务时使用）。

输出格式（严格 JSON，只输出 JSON 本身，不要输出其他内容）：
{
  "action": "click",
  "params": {"x": 0, "y": 0},
  "reason": "你这一步的真实判断，用自己的话说：现在最要紧的是什么、为什么选这个动作而不是别的、你预期会发生什么。不要套模板、不要复述规则条文——写你自己看到和想到的",
  "confidence": 0.0
}"""

# 禁鼠标模式的动作白名单（no_mouse=True 时替换 ACTION_TOOLS）：
# 只保留非输入类动作，模型从提示词层面就无从选择 click/move/按键/输入。
NO_MOUSE_ACTION_TOOLS = """可用动作（禁止使用鼠标与键盘，只能从以下非输入类动作中选择；params 为参数对象）：
- wait：等待画面加载/动画/游戏进程。params: {"seconds": 0.5~8}
  ⚠️ wait 只用于「等一次系统动画/加载」，**绝不能用来等顾客做决定**——
     顾客不会「自己决定」，只有你执行 sell_item / buy_item / dismiss_client /
     call_next 才会改变局面。等顾客＝原地卡死。
     若上一轮已经 wait 过，本轮**必须**改用接口动作推进。
- stop：停止操作（当前无法安全继续、或已完成任务时使用）。

【重要】本模式**不提供任何鼠标/键盘动作**：不要输出 click、ctrl_click、move、
press_key、type_text——它们会被系统拦截并导致本轮失败。所有游戏操作必须通过
下方的「游戏 API 直调动作」完成。

输出格式（严格 JSON，只输出 JSON 本身，不要输出其他内容）：
{
  "action": "wait",
  "params": {"seconds": 2},
  "reason": "你这一步的真实判断，用自己的话说：现在最要紧的是什么、为什么选这个动作而不是别的、你预期会发生什么。不要套模板、不要复述规则条文——写你自己看到和想到的",
  "confidence": 0.0
}"""

# 反空转模式的动作白名单（forbid_wait=True 时替换 NO_MOUSE_ACTION_TOOLS）：
# 连 wait 都不给——模型连续空等时启用，只剩 stop 与接口直调动作。
NO_WAIT_ACTION_TOOLS = """可用动作（当前**禁止 wait**——你已连续多次选择等待，那是空转；
系统在你每个动作之后已自动等待过，状态不会因为等待而改变。现在必须执行一个推进动作）：
- stop：仅在确实无法安全继续时使用。

⛔ 本轮不要输出 wait / 稍等 / 等待——请从下方的游戏 API 直调动作里选一个推进局面：
顾客在买 → sell_item；顾客在卖 → buy_item 或 dismiss_client；没人 → call_next；
队列空 → end_day；面板开着 → 成交/按铃换人（不要关闭面板，也不要当卖出按钮点 AcceptWholesale）；夜晚阶段 → night_services 等。"""

# 桥接直调动作说明（结构化观察 + 桥可用时追加到动作清单）。
# 语义与 bridge/ 插件 BridgeApi.HandleAction 一致。
BRIDGE_ACTION_TOOLS = """【游戏 API 直调动作】（当前观察来自游戏内部状态直读，桥接可用——
这些动作直接调用游戏逻辑，比模拟鼠标点击更可靠，请优先使用）：
- open_shutter：开门营业（开始接待顾客）
- close_shutter：关门歇业
- toggle_shutter：切换卷帘门开/关
- call_next：呼叫下一位顾客进店。⚠️ v0.8.1 起带守卫：queue.can_next=false 时会被拒绝（今日队列已空/
  门未开/新一天未开始）——被拒时按 detail 指引做，严禁重试；只有 queue.can_next=true 时才调用。
- dismiss_client：打发当前顾客离店
- dialog_advance：推进当前对话（继续/下一句）
- dialog_choose：选择对话选项。params: {"index": 选项序号，从 0 开始}
  ⚠️ **必须先看 visible_info.dialog.choices**（v1.4：桥接已暴露每个选项的文本与 index）——按文本
  选符合意图的那一项，**禁止盲选 0**。r75 实测：房东收租对话盲选 index 0 走进「延期」分支（现金未
  扣、租约重置但 business_complete 永远 false），房东卡店、不可遣散、卷帘门被拒 → 整局死锁。收租对
  话里只选含「交/付/租金」的选项。
- click_button：点击游戏界面上的按钮/组件（等效玩家点击，走完整 UI 流程——比直调内部方法更不容易造
  成状态残留；v0.9.0 起界面类操作优先用它）。params: {"name": "按钮对象名"} 或 {"text": "按钮显示
  文本"} 或 {"name": "...", "path": "层级路径子串"}（至少其一；同名按钮歧义时用 path 区分，path 照
  抄 GET /ui/buttons 返回的 path 字段子串，如 _AdvCalendar）；谈判界面常用按钮（实测对象名）：批发
  收购=AcceptWholesale（你付钱买入！）、议价=BargainButton、关闭=CloseButton；接受报价用
  accept_offer 动作（正式版谈判面板只有 primary/secondary 两个大按钮，没有「接受多个」按钮——
  `nego_buttons` 只有 primary/secondary/wholesale/accept_offer 四个键）。
- ⚠️【晨报面板铁律（用户 10-20 指令）】次日早晨弹出的「日历/夜间报告/状态」结算面板
  （右上角有 ×）**出现时必须第一时间叉掉**：click_button {"name": "CloseButton",
  "path": "_AdvCalendar"}——不关它会挡住开店接待，拖久了还会错过顾客。
- place_item：物品上桌（params: {"identifier": 物品标识}）——= 玩家「Ctrl+左键快速上桌」的程序等价
  （设置议价指针 + 打开谈判面板；v0.9.7 起为标准成交链第一步）。之后卖自己的货用 accept_offer（面板
  不可用时 sell_item）；⛔ AcceptWholesale 不是卖出按钮。
- clear_table：清空议价显示状态（table.negociated_item → null）。换顾客前、或发现桌上有残留的幽灵物
  品（画面上没有但 /state 报告有）时必须调用，否则所有「桌上物品」判断都是幻觉。
- sell_item：直接把指定物品卖给当前顾客（一步成交，不依赖桌上状态）。params:
  {"identifier": 物品标识}——**必须带 identifier，且直接从 visible_info.sellable_now 里照抄**。
  v2.4 起这份清单由**游戏本体**（CanSellThisItem）判定，不是标签猜的：每项含
  identifier/name/value/count，budget_ok=false 表示顾客预算不够整件（可能只买得起一件或需议价）。
  活鱼、鲟鱼这类没有 item_types 的物品也只有在清单里才卖得掉。⛔ sellable_now 为空 = 当前顾客
  不收你的任何东西（纯卖家/类别不匹配）——直接 dismiss_client 换下一位，禁止输出无 identifier
  的 sell_item（r59 实测：连续 7 次缺 identifier 整批空转）。
- buy_item：直接收购顾客的指定物品（一步成交，不依赖桌上状态）。params: {"identifier": 物品标识}
  （从 inventory.not_owned 选）。顾客在卖时首选此动作。⚠️ 禁收垃圾：item_types 含 JUNK 的物品
  （典型 scrap_metal「远古外星遗物」）加价后几乎无利可图，买它只会锁死现金——见到就 dismiss 换人。
- buy_wholesale：**一次买下顾客整批货**（调游戏原生批发按钮，按 table.wholesale_cost 计价）。
  顾客提出整批出手（client.is_offer_wholesale=true）时优先用它——不要一件件 buy_item，
  也不要用 click_button 去点 AcceptWholesale（那是同一个功能的低层入口，正式版里点不到）。
- appraise：自动完成估价委托（鉴定服务，一步到位）。params: {"identifier": 物品标识}。直接按游戏内
  真实价值提交——总是完美精度，报酬 = 20 基础费 + 最高比例佣金。有顾客请你估价时优先用它，这是稳定
  的无本收入。
- display_case：把物品放进展示柜。params: {"identifier": 物品标识}（从 inventory.owned 选）。
  展示柜里的商品会吸引感兴趣的顾客上门——把想卖掉的高价值合法物品放进去引流。
- （电源无日间开关：电力由夜间服务 POWER_SERVICE 控制，见 night_service）
- clear_trash：倒垃圾（垃圾堆积会降低店铺吸引力，garbage_level 高时及时清）
- alway_call：开启自动叫号（排队顾客自动接待，营业时段常开）
- pay_rent：交房租（带 CanPayRent 判定，现金不足会被拒）
- pay_loan：还贷款周供（economy.loan_days 归零时必须还，拖欠会被收回店铺=败局）
- phone_order：打电话给供应商订货/服务。params: {"vendor": 名称关键词}
  （commissary 杂货 / red_imp 酿酒 / mining 采矿 / pharma 药房 / chem 化学 /
   wild 王尔德 / rev 革命商）。补货来源——库存见底时用它进货，后续对话走 dialog_choose。
- fill_request：交付可完成的派系委托（一次性交付所有 CanFillRequest 的委托；
  奖励=信用点+声望，委托详情与奖励看 /state 的 requests 列表）

【夜晚阶段动作（v1.3：玩家按钮路径 + 实证顺序）】
完整顺序（r68 实证）：end_day 打烊（卷帘门按钮）→【可选出门】open_map → go_out 外出办事（有
visit_left 时办完 leave_outside 返回）→【睡觉】night_services（点睡袋开服务面板）→ night_service
逐项勾选 → end_day_confirm（=「结束本日」）→ 次日 MORNING → begin_day / open_shutter 开张。
⚠️ 顺序要点（r68 实锤）：end_day_confirm 就是睡觉——**点完立刻进次日**，所以 open_map / go_out
必须放在它之前；确认后不要再做任何夜晚动作（night_services / 再次 end_day_confirm 都会被实时状态
校验拦下，并可能重复换日）。服务面板别久留：打开后尽快勾选并确认（停留过久游戏会自动确认入睡，
此后再点就是重复换日）。
⚠️⚠️ 防重复打烊铁律（v0.9.10/v1.0）：store_state=CLOSED 时绝不要再选 end_day！直调已禁用（会
报错），下一步是夜晚流程（地图/服务/睡觉）——绝不再打烊。
- night_services：点睡袋打开夜晚服务面板（玩家睡觉入口；StoreBedroll.OnClick）
- night_service：勾选/取消一个夜晚服务。params: {"service": "POWER_SERVICE", "enable": true}
  （service 用 id 或中文名，id 清单见 /state 的 services 段：
   POWER_SERVICE 电力服务 / INSURANCE_SERVICE 保险服务 / JANITORIAL_SERVICE 清洁服务 /
   SEC_INFORMANT 盘查预警 / BUY_REFERAL 买家推荐 / SELL_REFERAL 卖家推荐 /
   CRIMINEL_NETWORK 违禁品买家推荐 / WILD_FIXER 王尔德的善后人 / CARTEL_PROTECTION 卡特尔保护费）
  【选购策略】⚠️ 价格一律以 services[].cost 实时值为准，别凭记忆估价（试玩版保险 96/晚、
  正式版个位数，照旧数字判断会做出完全相反的决策）——「贵不贵」= cost 对「今晚可能收益」。
  is_using=true 的项目**每晚都会扣费**，默认全关，只开真正需要的：INSURANCE_SERVICE →
  前台/展示柜有高值货过夜才开；BUY_REFERAL / SELL_REFERAL → **队列 ≥3 人（不缺客）就关**，
  冷清时再开；SEC_INFORMANT → 不卖违禁品就关；WILD_FIXER → 没犯罪点就关（看 reputations）；
  JANITORIAL → garbage_level >0 才开；POWER → 有机器要供电才开；CRIMINEL_NETWORK 利润高但有冷
  却；CARTEL_PROTECTION 保护费是强制项。services[].cooldown = **剩余冷却天数**（>0 = 这几天不能
  勾），不是可用天数。取消用 night_service {"service": "...", "enable": false}。
  💰【合计与可付性（v3.11）】勾完先看 economy.service_total——它就是服务面板上的「合计」（只
  累加 unlocked && is_using 的 cost，已含强制项）。service_affordable=false → **不要再勾任何服
  务**，先卖货回笼现金（sell_item / sell_table）或取消可取消的服务；只有 =true 今晚才付得起。
  两者为 **null**（游戏显式发 null）表示这一轮算不出来，**不是 0 元也不是买不起**——此时自己把 services[] 里
  is_using=true 的 cost 加一遍（mandatory 的保护费取消不了，必须计入），再按加出来的数判断。
  勾完不要靠界面开关的样子判断是否选中（桥接勾选是直写 isUsing，UI 可能不在该面板、看不到点亮）
  ——以 service_total 有没有变、以及下一步 end_day_confirm 是否被拒来复核。
- end_day_confirm：**「结束本日」按钮 = 睡觉**——确认服务扣费并直接进入次日 MORNING；必须在所有
  夜间事务（含出门）办完后才点，点完不要再做夜晚动作。⚠️ 现金不够付今晚服务合计时它返回
  code="INSUFFICIENT_FUNDS"（error 里写明合计、现金与出路）：**不要重试 end_day_confirm**——重试
  只会再被拒一次。按 error 指引进出路：取消可取消的服务（night_service + enable=false）或先卖货
  （sell_item / sell_table），现金够了再点。
- open_map：打开夜间地图（出门入口；显示今晚剩余外出次数 night.visit_left）
- go_out：今夜外出办事。params: {"destination": "commissary|upgrade|scavenging"}（commissary 配给站
  ——用信用点兑换 ration/买东西；upgrade 升级商人——买升级；scavenging 拾荒——冒险淘货，有受伤风
  险，重伤后当夜无法再外出）
  【外出策略】① 受伤（night.seriously_wounded）或 visit_left=0 时只能 stay_home；①b **出门前先
  equip_backpack**（把腰包/背包放进背包位；只有两口袋时战利品装不下就会丢）；② 拾荒是高回报高风
  险：有机会捡到背包/材料/商品，也可能受伤；③ 办完事必须 leave_outside 返回，否则会卡在外面。
  ④ ⚠️【地点口径】`night.current_location` 回读的是**地点 id 字符串**（store 店里 / dumping_ground
  拾荒场 / commissary 配给站 / dr_jackson 升级商人——就是上文 go_out 的 upgrade），不是旧试玩版的
  枚举名；判断「我在哪」只认这个 id。拿不准就当「不知道位置」，先 open_map 或再观察一轮，别猜。
  go_out 的 destination 是入参别名（scavenging 等），与回读 id 不是一回事，外出后以回读值为准。
- stay_home：点床铺/关闭地图并**打开睡前服务面板**（MapUIManager.Sleep 实证 = CloseUI + 打开
  EndOfDay 面板，不是直接入睡）。之后仍需 night_service 勾选 → end_day_confirm 入睡。最稳的收尾仍
  是「night_services → night_service → end_day_confirm」；stay_home 只在想从地图界面转入睡前流程时用。
- scavenge：在拾荒地点开始一次拾荒（需先 go_out scavenging）
  ⚠️【拾荒机制（用户实测铁律）】① **出门时必须选背包**（见上「外出策略」①b）；② scavenge **可反
  复执行多次**——一晚（visit_left 次数内）尽量多拾几轮，只拾一次就走是浪费；③ 拾到的物品**放在地
  上会消失**，只有装进背包的才能在 leave_outside 时带回店，离场前必须确认战利品已全部入包（界面若
  有「捡起/收进背包」类按钮或对话选项，优先用它收纳）。
- stow_loot：**拾荒收纳（必做）**——把「地面」格子的战利品逐件装进 背包 → 左口袋 → 右口袋（顺序
  自动）。每次 scavenge 后、以及 leave_outside 之前都必须先 stow_loot；系统也会在你选 leave_outside
  时自动先替你收纳一步（但别指望它兜底）。⚠️ 返回「失败 N 件」= 口袋/背包已满：立刻 leave_outside
  回店，不要再 scavenge（新战利品会继续堆在地面，回店必丢）。
- leave_outside：从外出地点返回商店

【格子读数（visible_info.slots）——`null` 与 `[]` 含义完全不同】
slots 只列**非空**格子，每项 {label, identifier, count, items}（items 只给前 3 个物品名）。
label：front 前台 / back 后台 / counter 柜台 / ground 地面 / pocketLeft 左口袋 /
pocketRight 右口袋 / backpackSlot 背包位；slots_total = 所有格子件数总和。
- ⚠️ `slots` 为 **null** = **不知道**（端点不可用/旧插件/不在店铺场景）；`[]` = **确实一件都没
  有**。把 null 当「空」会害人：拾荒时以为地上没货就 leave_outside，地上东西全部消失（r55）。slots
  为 null 时**禁止**推断地面是空的——用 stow_loot 的动作结果反推，或先 go_out 到地面再看一轮。
- 拾荒时 pocketLeft / pocketRight / backpackSlot 是携带位；ground 里 count > 0 就先 stow_loot 再离场。
- ⛔ **没有容量字段**（游戏侧不可读）：只能说「这一格里有 N 件」，**绝不许**推断「还能再塞 M 件」。
- inventory_counts（{owned_count, not_owned_count, by_tag}）可做**盘点与风险感知**（如「9 件
  RESTRICTED_1 = 盘查风险高」）——by_tag 只统计**已拥有**侧，not_owned 不计入。但它是**守卫禁用
  输入**（contracts.FORBIDDEN_GUARD_INPUTS）：⛔ 不许当任何守卫判据；⛔ 不许用 by_tag 计数替代
  inventory 清单去选 sell_item 的 identifier——只有清单有 identifier 与单价。
- showcase：**陈列状态游戏侧不可读**（showcase.observable=false），陈列改动后没有任何读数能确认
  效果；不要基于「摆好了应该会有客人」做多步计划，要看客流就用 queue 与 call_next 的实际结果判断。

【电话动作（v0.8.0：PhoneUIManager——打给通讯录联系人）】
open_phone 打开电话面板；phone_book 开/关通讯录 {"show": true}；hang_up 挂断当前通话。
phone_call 拨号：{"number": 8376} 或 {"contact": "王尔德"}（通讯录见 phone.contacts：8376 王尔德助手
/ 8815 革命军代表 / 56371 采矿 Caulson / 4615 药房 Dale / 3319 化学 Ashish / 51189 革命商奥丁）。
接通后对话用 dialog_advance/dialog_choose；没人接/冷却中就 hang_up。
注意：phone_order（按供应商名直接 UseService）与 phone_call（走电话 UI 拨号）二选一即可，优先
phone_order（更直接）；phone_call 用于打非供应商联系人（如王尔德助手、奥丁）。

【机器操作（v2.3：水分收集器 / 循环水养殖 / 熔炉等）】
visible_info.machines = [{i,id,name,slots:[{s,items:[{id,n,v}]}]}]：i = 机器下标（machine_take/put
的 machine 参数直接用这个数）；s = 槽位下标（不同机器含义不同，常见 0 电池、1 模组、其余容器/产
出）；v = 物品价值（水罐的 v 随水量/纯度变化）。
- machine_take 取产出 {"machine": i, "slot": s}（不写 slot 会取所有「非能源/非模组/非便签」槽，适合
  一次清空）；machine_put 放货 {"machine": i, "slot": s, "item": "water_pitcher"}
- 生产节奏：投料/放置容器 → 打烊（end_day）→ 睡觉（end_day_confirm）→ 次日看 machines。水分收集器
  放 water_pitcher 过夜产水；循环水养殖：鱼苗+鱼子+饲料+滤芯+储水箱过夜成长/产鱼片；熔炉：废料/
  矿石 2 份 → 1 金属锭（需通电）。
- ⚠️ 产出留在机器里**不能卖**：必须先 machine_take 到库存，再对匹配品类买家 sell_item。⚠️ 电池/模组
  别取（energy_credit / system_module_*），否则机器停产。

【治安与战斗动作（v0.8.0：通缉犯与伪人）】
- report_client：举报当前顾客（店内侧栏长按「治安部」按钮）。仅当 sec_button_lit=true /
  can_report=true（通缉犯、伪人等可逮捕顾客）时可举报——成功拿悬赏 report_reward（现金）+ 治安部
  声望，但会掉被举报者所属派系声望（reputation_penalty）。错报有声望惩罚，普通顾客千万别举报。
  伪人（is_aug）的正确打法：先正常交易赚一笔 → enter_shooting_mode → shoot 击毙 → 再 report_client
  举报拿悬赏。
- enter_shooting_mode / shoot / exit_shooting_mode：射击模式（击毙伪人/通缉犯用，shoot 带 CanShoot
  守卫）。这是游戏核心道德抉择机制，见到伪人不要犹豫。

【房东与收租（最高优先级规则，违反会导致恶性循环）】
- 交租日（day_until_rent == 0）队列固定：**最后一位 = 房东，倒数第二位 = 商人**。商人什么都收
  （违禁品除外）——「清库存回血」窗口，先尽量 sell_item 卖给他；房东排最后，见到他按下面处理。
  房东也是「顾客」但**绝不能 dismiss_client**（守卫会拒绝并提醒 pay_rent）。
- ⚠️ **判「付不付得起」用 `rent_value + rent_due` 之和**（CanPayRent 原式，不是取大、也不是
  只看 rent_due；r78：名义 242、实交 693）。现金不足时房东对话只会给「拒付租金」选项。
- 🚨【拒付租金 = 立刻游戏结束】没有房东宽限时（宽限在 /state 里读不到，一律按「没有」），
  「拒付 / 不交租金」直接结束整局，不是「再拖欠一期」。现金不够时「付租」选项**根本不出现**，
  剩下的往往只有这条死路。所以 `day_until_rent <= 0` 且现金不够时，唯一正解是先 sell_item /
  sell_table 换现金，或去点 pay_rent、选含「交、付、租金」的对话项；**永不点「拒付」**，
  也别进货、打烊或做与凑钱无关的事。凑够后 `rent.can_pay_rent=true` 才 pay_rent。
- 💵【只有现金算钱】只认 visible_info.money：① 库存标价不能交租，得先 sell_item 变现；
  ② 夜间服务费只能付现金，不能预支当晚 night_services 产出；③ 恩惠（favor）不能兑换现金，
  也不能抵房租或服务费。
- day_until_rent < 0（已拖欠）：吸引力下降、客流减少——尽快 pay_rent 补交，越拖越糟；
  end_day 在「拖欠但现金够交」时也会被拦截。

【经营策略（店主思维，不只是买卖）】
__RHYTHM_RULE__
1. 现金红线（最高优先级）：visible_info.money 要 ≥（rent_value + rent_due）×3。低于红线立即进入
   【只卖不买模式】：禁止 buy_item/buy_table/phone_order，只 sell_item 回款，到 ×5 才恢复进货。交租
   日尤其紧急；生意冷清时宁可 end_day 快进，也别干等。
2. 引流：空闲时把 1~2 件高价值合法物品放进展示柜（display_case）；垃圾多了 clear_trash；营业中开
   alway_call 自动接客。economy.attractiveness 直接反映客流吸引力。
3. 副业：估价委托用 appraise（无本买卖）；economy.lottery_number 是中奖号码，买彩票对照它稳赚；派系
   委托（requests）奖励丰厚，凑齐 required_item 就 fill_request。
4. 进货：库存卖空时用 phone_order 找供应商补货（economy.retail_markup 是零售加价率，
   economy.stock_value 是库存总值）。同一顾客买过一件后常不再收同类——保持多种类别货物。
5. 声望经营：reputations 列表显示各派系声望——对可疑顾客上报换治安部声望；黑市声望
   影响违禁品折扣。经济紧张时才能接 contraband_markup 高利润单（风险自担）。
6. 贷款：economy.loan_days 归零必须 pay_loan，否则同样进入恶性循环（房租见上一节）。
7. 贵重先验：高价值物品先看 item_types 与状态标签再决定收不收（假货常见，看错自担）。
8. 【拒收垃圾货，最高优先级，无例外】item_types 里含 **JUNK** 的物品**一律禁止** buy_item——这类货
   （如 scrap_metal「远古外星遗物」，types=[MATERIAL,BASIC_MATERIAL,JUNK]）买入价已接近加价后的售
   价，净利极少却锁死现金与库存格。⚠️ **不存在「唯一的交易机会」这种例外**：顾客兜售 JUNK 货时正确
   做法是 **dismiss_client 直接换人**，不是买下来「清空顾客库存」。宁可空手也不收 JUNK。同理，连续
   buy_item 同一 identifier 超过 2 件即应停手换人。
9. 【有顾客在场就必须成交，绝不干等】这是最容易空转的场合：
   - client.is_buying=true（顾客要买东西）→ **sellable_now 非空就立刻 sell_item**（identifier 照抄
     清单里的），budget_left 够就直接成交，别等。⛔ sellable_now 为空 = 这笔生意做不成 →
     dismiss_client 换人，不要反复试。
   - client.is_selling=true（顾客要卖东西）→ 货是 JUNK 就 dismiss_client；不是 JUNK 且划算才
     buy_item；**现金 < rent.rent_due（真实应缴；字段缺失时才退回 rent_value）时一律 dismiss_client**
     （交租储备红线会拦买入，别浪费步数试）。
   - 【特殊 NPC（治安盘查员等 dismissable=false）】送客按钮无效、打烊被拒（"队列中还有重要人物"）
     ——**只能等其自行离店**：对话结束后（dialog.is_end=true）不要再 dialog_advance（会打断离店协程
     永久卡店），用 wait 或 click_button 处理界面。
   - 【死局（无可卖+买不起）】sellable_now 为空 且 现金不够买任何一件时，系统白名单只保留
     dismiss_client / end_day / wait——**此时 dismiss_client 是唯一有效出路**，不要尝试
     sell_item/buy_item/call_next（都会被拒，r61 实测 13/14 步空转）。送客后队列空则直接 end_day。
   - dialog/table 有变化不代表在推进：**只有你的动作能推进局面**。看到「可以出售物品给顾客」而自己
     却选了 wait，就是卡死，必须改 sell_item。
- open_negociation：打开谈判界面（物品上桌后交易必须在此界面中进行）
- accept_offer：接受当前报价（成交，需谈判界面打开且桌上有顾客想收的物品）

【谈判界面操作规则（v2.2：批发按钮是「买入」，不是「卖出」！）】
谈判面板开着（table.nego_open=true）时，先分清方向，再点按钮：
1. **AcceptWholesale = 你付钱收购对方的整批货（现金流出）**。成本预判可读
   `table.wholesale_cost`（正式版 3.10.3 起 `/state` 会发射它，单位是总价；非批发报价时为
   `null` = 本次没有批发价可预判，不要自己假设一个价格）。点下去之后**只认现金 diff**：成交后
   visible_info.money 少了多少，就是这批次货实际花了多少；按钮文案里的折扣百分比只是对方标的
   参考，不能当付款额。点之前先确认付得起：现金 ≥ rent.rent_due（真实应缴，见「房东与收租」）
   ——付不起时点它会被交租储备红线直接拦下，白费一步。
   ✅ 只在「这批货值得进货 / 能加价卖出 / 补品类缺口」时才点；⛔ 卖自己的货时**绝不能点它**
   ——实测想卖 100 元化学品却点了它，现金 -162、净亏 62。
2. 普通单件卖出 → accept_offer 接受报价（面板链不可用时用 sell_item 一步直调）；3. 以上都不能用
   （无任何可成交项）→ 直接 call_next 按铃换下一位。
⚠️ 成交方向一律以**现金变化**判定：现金增加=卖出成功；现金减少=买入（不是成交收入）。
⚠️ close_negociation 已取消：严禁以任何方式主动关闭谈判面板——换客/打烊时游戏会自然处理。

【完整交易流程（v0.9.7 玩家路径版：Ctrl+左键上桌 + 点接受，优先按此执行）】
1. 选品：看 client.buying_tags / buying_ids，对照库存物品的 item_types 标签，只挑顾客想收的
   （买食物的顾客不收日用品！）；同时确认 client.budget_left 够买。
2. 标准成交链（首选，=玩家操作的程序等价）：① 顾客买 → place_item {"identifier": 自己的物品}
   ——上桌（设置议价指针并打开谈判面板）；② 卖自己的货：accept_offer 接受报价（面板链不可用时
   sell_item 一步直调）；⛔ 不要把 AcceptWholesale 当卖出按钮——它是「你付钱收购对方整批货」，
   点了只会掉现金；③ 顾客卖（收购）→ buy_table。备选快速结算（面板链不可用/无可接受项时）：
   sell_item / buy_item 一步直调。
   【选品铁律】第一次就必须选对类别——sell_item 有类别守卫：物品 item_types 与 client.buying_tags
   无交集会直接拒绝。绝不拿日用品凑数应付想收医疗品/工具的顾客；⚠️ 类别看 item_types 字段，不要凭
   名字想当然——「「泥炭」合成肉」item_types=[FOOD, PROCESSED_FOOD] 是食物不是材料（MATERIAL）；
   库存里确实没有顾客想收的类别时直接 dismiss 换人。
   【批发与批量·这是进货流程，不是卖货】顾客带多件同类、提出整批出手时
   （client.is_offer_wholesale=true 且 client.is_selling=true）：
① 先确认付得起（现金 ≥ rent.rent_due）并看 item_types；金额按上文「谈判界面操作规则」第 1 条预判
   （`wholesale_cost` 为 `null` ≠ 免费，按未知处理）；JUNK/卖不掉的类别不接（见「拒收垃圾货」铁律）；
② 台面留有上次物品/按钮不亮时先 click_button {"name": "transferButton"} 刷新面板；
③ 批发按钮亮起（table.nego_buttons.wholesale=true）→ **直接用 buy_wholesale 一次买下整批**
   （它调游戏原生批发按钮，一次收购全部货，比连续 buy_item 少 N 次往返）；成交后现金**减少**
   （付货款），系统自动点 transferButton 收纳（不收则柜台占满，下位顾客物品被销毁，r55 一夜毁 12 件）；
④ 收纳完成后接待下一位，重复①②。
   ⛔ 绝不要「一件一件 buy_item」去买顾客的整批货——那是 N 次成交、N 次对话、N 倍耗时，
   而且每件都按单件价，通常比批发价贵。只要 is_offer_wholesale=true 就优先 buy_wholesale。
   只有在没有批发报价（is_offer_wholesale=false）且确实要买多件时，才对同一 identifier 连续 buy_item。
   【真伪判定】动作返回 ok=false 表示**真的没成交**（现金未变）——detail 说明原因，换一件不同
   item_types 的物品重试或 dismiss_client 换顾客，不要对同一物品反复重试。
3. 议价状态：table.negociated_item = 当前议价物品（place_item 设置，等效玩家上桌）；换顾客时对下一位
   重新 place_item 即可（旧物品自然被替换）；clear_table 用于清空议价。若连续 2 次成交动作后现金没
   变，先 clear_table 再走第 2 步重新选品。
3b. **交易台与柜台要保持干净**（老板常识）：送走顾客（dismiss_client）、成交（accept_offer /
   buy_wholesale / accept_barter）或打烊（end_day）之前，若 table.negociated_item 还挂着东西，
   用 clear_table 清掉；若前台柜台有货（收购来的商品堆在前台），用
   click_button {"name": "transferButton"} 搬回储物区。**柜台堆满时下一位顾客的货物会被游戏直接销毁**
   （r55 教训）。系统也会在你送客/成交/打烊后自动做这两件事，但你主动做能更快腾出柜台。
4. 顾客不匹配时：库存确实没有顾客想收的类别才 dismiss_client 换人；sell_item 若被「类别不匹配（售
   前校验）」拦截（detail 写明顾客只收哪些类别、物品是哪些类别）——**直接 dismiss_client 送客**，
   绝不要对其它不匹配物品重复 sell_item（r51 空转 10 步的死法）；若连续 3+ 位顾客都跳过，说明库存品
   类太单一——用 phone_order 补货多样化。
5. 今日顾客已空（v0.8.1 权威信号，不要再试探性叫号）：看 visible_info.queue.can_next——它是游戏
   自己的队列判定：① false 且无顾客在场 → **立即**进入夜晚流程（end_day → night_services → night_service 勾选 → end_day_confirm），严禁再 call_next
   空转（空叫 + wait 都是纯浪费）；② false 但顾客还在店里 → 先处理当前顾客（对话/交易/送客），送客
   后直接 end_day；③ true → 才 call_next 叫下一位。
   另外 store_state=CLOSED 时先 open_shutter 开门；**MORNING 时也用 open_shutter 开张**（反汇编实
   证：开卷帘门就是玩家的开张动作——OpenShutter 内部链含 OnShutterDoorOpenMorning；begin_day 已按
   钮化但推荐直接 open_shutter）。call_next 被守卫拒绝时按 detail 的指引做，不要重试。
- buy_table：收购桌上物品入库存（顾客出售时）
- sell_table：把桌上物品出售给顾客（顾客购买时）
- end_day：打烊结束这一天（OPEN 时点卷帘门按钮；CLOSED/MORNING 会被拒绝并引导夜晚流程）
- begin_day：按钮化的开张（等效点卷帘门；MORNING 时直接 open_shutter 亦可）
- save_game：保存游戏

【玩家输入链路（像真人一样操作，v3.12）】
- player_click{x,y,scene?}：在 Unity 屏幕坐标点击一次（走完整 Input→EventSystem→游戏逻辑链路）。
- player_drag{x,y,to_x,to_y,scene?}：从起点拖到终点（完整 IDrag/IDrop）。
- player_key{key,frames?}：按住一个 Unity KeyCode（如 Return/Escape/W）若干帧。
- player_scroll{x,y,amount?}：在指定位置滚动。
- player_wait{frames?}：逐帧等待（UI 动画/转场）。
- player_stop：取消当前虚拟输入计划。
坐标来源：GET /ui/nodes 返回可拖拽 UI 节点的屏幕矩形（Unity 屏幕坐标，左下原点），
或从 /ui/objects 的控件取；**不要凭截图猜**。scene 参数填上次 game_state/bridge state 的
scene，不匹配时服务端会拒绝执行（防陈旧状态误操作）。

使用规则：能用直调动作完成的操作，不要用 click/move 去点界面；
只有直调动作覆盖不了的操作（如物品上桌、菜单导航）才用鼠标，此时坐标需自行估算并降低 confidence。
若需要"玩家亲自点"的语义（触发游戏内 OnClick 的副作用），优先用 player_click 而非 ui_click。"""

COORDINATE_NOTE = """【坐标规则】important_objects 中的 point 是对象中心点的截图坐标 [x, y]，
可直接作为 click/move 的 x、y 参数。若要点击的对象没有 point，请根据画面布局谨慎估算，
并适当降低 confidence；无法确定目标位置时，优先选择 wait 或 stop，不要盲目点击。"""

STRUCTURED_NOTE = """【数据来源】当前观察来自游戏内部状态直读（非截图）：数值全部准确，
但界面元素的位置未知——因此需要操作具体界面时，优先使用「游戏 API 直调动作」；
若某操作只能通过界面完成，再谨慎估算坐标并降低 confidence。"""

# 正式版（Probably Stolen 100-Rev12）相对试玩版新增的机制。
# 不写价格数字：正式版的服务/物品定价与试玩版不同，一律以 /state 实时值为准。
FULL_RELEASE_NOTE = """【正式版机制（这些是试玩版没有的，决策必须考虑）】
· difficulty：本局难度（cozy/normal/hard/challenge），难度会改规则与成本，以 difficulty.title 为准。
· perks：开局天赋，决定哪些特权可用（例如以物易物、违禁品相关）。
· mortgage：**按揭周供**，与 7 天租约是两套并行的负债。weeks_left=0 前必须按周还
  weekly_payment；现金不够优先保租约与按揭，别把钱压在库存上。
· afterhour_events：夜间事件。active=true 的正在生效；closes_location=true 表示该地点
  今晚**关门**，此时 go_out 过去会白跑——先看事件再决定去哪。
· locations：外出目的地是**注册表**（id 驱动），可用 id 见 locations[].id（如 commissary
  配给站 / dumping_ground 拾荒场 / dr_jackson 升级商人）。试玩版的「升级商人」在正式版
  没被删掉，只是改名成了 id `dr_jackson`——go_out 仍可用旧别名 upgrade，但**判断自己在哪
  只认回读的地点 id**，别拿旧枚举名去比。available=false 的地点今晚去不了。
· 长线进度（攒首付买店）：**所有金额都从实时字段读，别凭记忆**——visible_info.mortgage 里的
  store_price / down_payment / total_weeks / weekly_payment / weeks_left / can_be_offered /
  mortgage_weeks_offered 是权威值；营业许可等一次性费用看 difficulty.rules.permit_fee，房产基价看
  difficulty.rules.property_base_price。为 null 或缺失时一律回答「未知，需先读 mortgage /
  difficulty.rules」，**不许自己填数字**（旧试玩定价与正式版不同，写死会算错账）。
· difficulty.rules 是本局真实倍率（retail_markup / power_mult / insurance_mult 等），
  算成本与定价必须用它，不要凭难度名猜。
· 机器投料前先看 /machines 的 recipes[].materials（ids/tags + 数量）；产出由游戏运行时算，
  接口不提供，别臆断。
· 枪械订单成交用 accept_gun_order（会先校验必选需求），不要用普通 accept_offer。
· 物品带 features[]：known=false 表示**还没验出**，text/condition 是摊主表面看到的（可能是假的）；
  known=true 才是真实情况。真假货判断只能基于 known=true 的条目，别把表面描述当真相。
· 容器物品带 children[]：那是箱子里实际装的东西，"开箱"前先看它值不值得开。
· risk.crime_value 是累计犯罪值、risk.luck 是幸运值；卖违禁品前先看犯罪值会不会把盘查招来。
· 以物易物看 table.barter.acceptable，满足才用 accept_barter（不满足时按下去也是白按）。
· **整批收购用 buy_wholesale**（一次买下顾客全部货，按 table.wholesale_cost 计价）——不要再一件件 buy_item，那是 N 次成交 N 次对话。买入是现金流出，别把它当成卖货。
· 机器模组用 machine_module_install（machine + item），装进模组位后机器性能才生效。
· 一切费用（夜间服务、按揭、门票等）都用 /state 的实时字段判断，不要引用任何历史价格。"""
STRUCTURED_NOTE_NO_BRIDGE = """【数据来源】当前观察来自游戏内部状态直读（非截图）：数值全部准确，
但界面元素的位置未知——当前没有可直接调用游戏逻辑的通道，所有操作都需要通过
鼠标键盘模拟完成；请根据界面布局谨慎估算坐标并降低 confidence，无法确定目标位置时
优先 wait 或 stop，不要盲目点击。"""

# ---------------------------------------------------------------------------
# T13：读数降级（visible_info.is_degraded）——「不知道」不是「没有」
#
# 背景：`/state` 的 BuildState() 有 22 段业务块带 `<block>_error` 兄弟键（模式 A：
# 读失败则该块整个不发射、只出 `_error`），46 处空 `catch { }` 会静默吞掉键
# （模式 B：连错误都没有），另有显式 `day: null`（模式 C）。观察层已把这三类
# 统一收集成 `visible_info.degraded` / `is_degraded`——但**提示词原先一个字都没提**
# （全文件 grep `degraded` = 0 命中），模型收到 `is_degraded=true` 却无从知道
# 那意味着「这块是不知道」。
#
# 为什么这是硬约束级的问题而不是文案优化：降级块的值是 `null` / `[]` / 写死的
# `false`，与「真的是空」在 JSON 形状上**逐字节相同**。守卫的输入静默为空时，
# 守卫本身就等于不存在——r55 就是这条路径：`night` 读不到 → `_at_scavenging_site`
# 返回 False → 拾荒离场强制收纳 `stow_loot` 的守卫生效条件不成立 → 一夜丢光战利品。
#
# 注入位置：**独立成段，挂在 observation 之后、next_step_hints 之前**，
# 不放 coord_block。原因：`coord_block` 在 no_mouse（生产模式）下会被整段清空
# （见下方 `coord_block = "" if no_mouse else ...`），连 FULL_RELEASE_NOTE 都会
# 一起丢掉——把安全规则挂在那儿等于上线即失效，正是本项目反复踩的坑。
# ---------------------------------------------------------------------------

#: 降级块 → 「这块坏了会让哪些判断变成不可靠」。键是
#: :data:`game_agent.contracts.STATE_GUARDED_BLOCKS` 的 **block 名**（22 段业务块，
#: 顺序由契约声明序决定，这里只提供后果文案、不另抄一份块清单），另加 3 个来自
#: :func:`game_agent.structured.channels.degradation_report` 的非业务块项
#: （整体不可读 / 显式 null 标量 / 大面积静默缺失）。
_DEGRADED_BLOCK_IMPACT: dict[str, str] = {
    "screen": "界面/场景判定失真——不要假定自己「正在柜台待客」或「在某面板里」",
    "shutter_closed": "卷帘门开/关未知——开关门各做一次即可，重复 toggle 会让门状态抖动",
    "client": "当前顾客是谁、要买还是要卖都未知——不能假定「没人」就去干别的，也不能假定有人",
    "dialog": "对话是否结束/有无选项未知——重复 dialog_advance 会打断离店协程、永久卡店",
    "table": "交易桌内容与议价状态未知——不能假定桌空而重复 place_item，也不能假定已有报价可接",
    "inventory": "库存列表可能只是空数组而非真的空——不要据此补货，也不要断言「卖完了」",
    "economy": "贷款天数、零售加价、库存总值与吸引力全部不可信——算成本和定价都会错（现金本身取自顶层 cash，不受本块影响）",
    "reputations": "派系声望未知——不要假定声望够/不够，也别据此决定接不接单",
    "requests": "委托可交付性未知——不要断言「没有可交付委托」，也别把 fill_request 当无收益",
    "services": "夜间服务清单与价格未知——不要假定某服务可买或已买",
    "difficulty": "难度倍率与规则未知——一切按难度缩放的数值（加价/电费/保险倍率）都不能拿来算账",
    "perks": "天赋特权未知——不要假定以物易物等特权可用",
    "mortgage": "房价/首付/期数/周供/剩余周数未知——不要假定「还完了」或「这周不用交」",
    "afterhour_events": "夜间事件未知——不要假定目的地今晚开着（closes_location 的坑）",
    "locations": "外出目的地注册表与 available 未知——不要假定某地点存在或今晚能去",
    "appraisal": "鉴定等级与升职待处理未知——不要假定还能接更高品级的货",
    "risk": "犯罪值/幸运值未知——卖违禁品前的盘查风险不可估，此时宁可不碰违禁品",
    "aquaculture": "养殖/水体状态未知——不要据此投料或判断产出",
    "night": (
        "夜间状态未知：**是否在拾荒点、能否外出、地图开闭、今晚外出次数全部读不到**"
        "——「在拾荒点」判定会返回否，离场强制收纳守卫随之静默失效（r55：一夜丢光战利品）。"
        "若你判断自己可能在外面/在拾荒点：**离场前先自己 stow_loot 把地面物收进口袋**"
        "（不要指望守卫替你兜底；若本轮工具列表里没有这个动作，说明当前通道不支持直接动作，"
        "改用界面点击、或干脆先别离场），再用 night 之外的证据（locations、上一轮 go_out 的去向）"
        "确认自己在哪。地点是 **id 字符串**（store / dumping_ground / commissary / dr_jackson），"
        "不要用试玩期的 SCAVENGING 之类枚举名去比"
    ),
    "phone": "电话通讯录与通话状态未知——不要假定能拨号或正在通话",
    "queue": "can_next / has_client 未知——**最贵的误判是把读不到的队列当成「今日已空」直接打烊**",
    "shooting": "射击模式与通缉犯判定未知——不要贸然进入射击流程",
    # 非业务块项（degradation_report 的三种特殊条目）
    "in_game": "连「是否在游戏里」都读不到（整体不可读/黑屏态）——此时只做最保守的动作，不要贸然推进局面",
    "day": "经营天数是显式 null（游戏侧算天数失败）——不要用它算租金倒计时或目标进度",
    "<payload>": "大面积业务块静默缺失（系统接近全盲）——按「大部分读数都不可信」处理，只做不依赖状态的动作",
}

#: 契约新增了块但这里还没写后果文案时的兜底——**保守而非放行**：
#: 不假设「没影响」，一律当未知。
_DEGRADED_IMPACT_FALLBACK = (
    "这块读不到时它的值不可信——只能当「未知」，不能当「为空/为零/为假」"
)

DEGRADED_STATE_NOTE = """【读数降级（visible_info.is_degraded=true）——「不知道」不是「没有」】

本轮 observation 里 is_degraded=true，意思是**有一段或多段状态没读到**，
而不是「读到了空值」。降级块的取值是占位符（null / [] / false / 0），
它和「真的是空」在 JSON 上**长得一模一样**——这正是本项目反复踩的坑：
把「不知道」当成「没有」，然后基于假空值做决定。**判断降级只看 is_degraded 这个标志，
不要自己拿「某个键在不在」去猜好坏**（健康的 /state 本来就会少发一批键）。
所以按下面五条办：

1. 降级块一律当「不知道」。**绝不当成「没有顾客」「今日已空」「库存为零」
   「已经还完」「门是关着的」**——这些结论都需要证据，而降级块不提供证据。
2. 不依赖降级块的动作照常做，先把局面推进；能换个手段确认的
   （再观察一轮、改看别的块、用历史记录交叉验证）先确认再动。
   判断「这个动作是否依赖降级块」时看它的参数从哪来，不要凭印象——
   例如叫号依赖 queue.can_next，queue 降级时它就不是安全动作。
3. 必须读降级块才能决策时，**在 reason 里明确写出「X 块不可信」**，
   并说明你用的是哪个替代证据；不许假装知道，更不许把猜测写成事实。
4. 踩到红线的块降级 → 该方向的动作用**先不动**：能不能打烊看 queue、
   拾荒与出外看 night、成本与加价看 economy、买房看 mortgage。
   宁可不做，也不做错——一次做错的代价远大于空一轮。
   （现金与房租本身取自顶层 cash / rent 字段，不属于这些块；
   但只要成本或库存价值算不准，进货与定价同样不能拍脑袋。）
5. 降级 ≠「什么都不能做」。真正贵的错误只有一种：**拿假空值当结论**。
   典型死法：把读不到的队列当成「今日已空」直接打烊、把读不到的 night
   当成「不在拾荒点」直接回家（r55 就是这样一夜丢光战利品）。
   正确做法是把降级当成「眼睛被蒙住了一半」，改做不依赖眼睛的判断。
"""

#: 降级影响表的表头（表体由 :func:`_degraded_impact_table` 按契约块序生成）。
_DEGRADED_IMPACT_TABLE_HEADER = """
【降级影响表：哪块坏了会让哪些判断不可靠（本表按契约块顺序生成，随时可查）】
"""


def _degraded_entries(observation: dict[str, Any]) -> list[dict[str, Any]]:
    """取出 observation 里的降级条目（非结构化观察/旧载荷返回空列表）。

    形状来自 :func:`game_agent.structured.channels.degradation_report`：
    ``[{"block": str, "mode": "A"|"B"|"C", "error": str|None, "flag": ...}, ...]``。
    这里只做防御性筛选，**不重新判定降级**——判定只有一个入口，就是观察层。
    """
    visible = observation.get("visible_info") if isinstance(observation, dict) else None
    if not isinstance(visible, dict):
        return []
    if not visible.get("is_degraded"):
        return []
    raw = visible.get("degraded")
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, dict) and str(item.get("block") or "")]


def _says_degraded(observation: dict[str, Any]) -> bool:
    """观察层是否声称本轮有降级（只看 ``is_degraded`` 标志本身）。"""
    visible = observation.get("visible_info") if isinstance(observation, dict) else None
    return bool(isinstance(visible, dict) and visible.get("is_degraded"))


def _degraded_block_names(observation: dict[str, Any]) -> list[str]:
    """本轮实际降级的块名（去重、保持观察层给出的顺序）。

    注意 :data:`game_agent.structured.channels._PAYLOAD_BLOCK` 折叠项：
    大面积静默缺失时观察层只报一条 ``block="<payload>"``，真实块名在它的
    ``blocks`` 字段里。这里**必须展开**——否则提示词只能告诉模型「有一大块坏了」，
    却说不出是哪几块、对应哪些判断失效，等于白报（T13 验收要求逐块可查）。
    """
    names: list[str] = []
    for item in _degraded_entries(observation):
        block = str(item.get("block") or "")
        inner = item.get("blocks")
        expanded = (
            [str(b) for b in inner if str(b)]
            if isinstance(inner, (list, tuple)) and block.startswith("<")
            else [block]
        )
        for name in expanded:
            if name and name not in names:
                names.append(name)
    return names


def _degraded_impact_table(degraded: list[str]) -> str:
    """按 ``contracts.STATE_GUARDED_BLOCKS`` 的块序生成逐块影响表。

    块清单**从契约派生**（不手抄一份，避免契约新增块后本表静默过期）；
    未登记后果文案的块走 :data:`_DEGRADED_IMPACT_FALLBACK`（当未知，不放行）。

    本轮实际降级的行前面标 ``⚠️``：全表保留（模型需要知道任意块缺失意味着什么），
    同时一眼能看出**这一轮**哪几行是必须当「未知」处理的。
    """
    flagged = set(degraded)
    return "\n".join(
        f"· {'⚠️ ' if block in flagged else ''}{block}："
        f"{_DEGRADED_BLOCK_IMPACT.get(block, _DEGRADED_IMPACT_FALLBACK)}"
        for block in STATE_GUARDED_BLOCKS.values()
    )


def build_degraded_note(observation: dict[str, Any]) -> str:
    """构造「读数降级」提示段；未降级时返回空串（提示词体积不变）。

    Args:
        observation: 观察 dict。只有结构化观察（``build_observation`` 产物）
            才带 ``visible_info.is_degraded``；视觉观察/旧载荷返回 ``""``。

    Returns:
        降级时返回：铁律五条 + 本轮实际降级块清单 + 逐块影响表；
        否则 ``""``。**空串不再追加任何文案**，因此未降级的轮次里
        「降级段」这一整块为零字符——体积变化只来自常驻的
        :data:`UNRELIABLE_SIGNAL_NOTE`（P1 的连贯纪律，与是否降级无关）。
    """
    names = _degraded_block_names(observation)
    if not names:
        # 防御：观察层说降级了，但条目形状我们解析不出来（例如未来改了结构）。
        # **不能沉默**——沉默等于把「不知道」当「一切正常」，正是本节要禁止的错。
        if not _says_degraded(observation):
            return ""
        return (
            DEGRADED_STATE_NOTE
            + "\n本轮降级细节读不出来（visible_info.degraded 形状异常），"
            "按「多处状态不可信」处理：任何「没有/为空/已空/没顾客」的结论都不许下。\n"
            + _DEGRADED_IMPACT_TABLE_HEADER
            + _degraded_impact_table([])
            + "\n"
        )
    listed = "、".join(f"`{name}`" for name in names)
    current = (
        f"\n本轮实际降级的块（表里带 ⚠️ 的行）：{listed}。"
        "这些行就是本轮必须当「未知」处理的判断；"
        "若它们与 observation 里的占位值、或与下方【本轮优先动作】的建议冲突，"
        "一律以「未知」为准——那些建议可能是基于降级块算出来的，"
        "读出「今天没顾客了」「不在拾荒点」这类结论时先怀疑是不是降级造成的假空。\n"
    )
    return DEGRADED_STATE_NOTE + current + _DEGRADED_IMPACT_TABLE_HEADER + _degraded_impact_table(names) + "\n"


# T13 ③：把「不认 ok 标志」与降级语义连成同一条纪律。
# 注意：**不能出现字面量 `ok=true`**——tests/test_gap_regressions.py:428 明确断言
# 提示词里不得把该串当判据教给模型（`assert "ok=true" not in prompt`）。
# 这里用「ok 字段为 true」的中文表述，语义等价且不触发该断言。
UNRELIABLE_SIGNAL_NOTE = """【同一条纪律：可疑信号都不能当结论】
读数降级与成交判定是同一件事的两面——**都不许把「看起来有值」当事实**：
· 动作返回的 `ok` 字段为 true，只说明游戏执行了这个动作，**不代表成交**
  （点错目标、被系统拒绝都会是 true）。成交的唯一判据是现金/库存/天数 diff
  （见上文「成交方向一律以现金变化判定」）。
· 读数降级是同一个毛病的另一种形态：块的值确实在，但内容是占位符——
  「有值」和「可信」是两件事。
· 两者做法一致：**先找可靠证据（现金 diff、另一个块、历史记录），
  找不到就在 reason 里说不知道**。宁可承认不知道，也不要编一个结论。
"""


def _render_history(history: list[dict[str, Any]], limit: int) -> str:
    """把操作历史渲染为紧凑文本（最近 limit 条）。"""
    if not history:
        return "（这是第一步操作，没有历史记录）"
    lines = []
    for item in history[-limit:]:
        step = item.get("step", "?")
        if "error" in item:
            lines.append(f"- 第{step}步：发生错误（{item['error']}）")
            continue
        action = item.get("action")
        params = item.get("params") or {}
        reason = item.get("reason") or ""
        result = item.get("result") or ""
        screen_state = item.get("screen_state") or "?"
        lines.append(
            f"- 第{step}步：{action}({json.dumps(params, ensure_ascii=False)}) → {result}"
            f"｜目的：{reason}｜当时界面：{screen_state}"
        )
    return "\n".join(lines)


#: 动作说明里「只在特定阶段才需要」的小节标题。
#: 白天不注入夜晚流程，夜间不注入机器/治安/战斗——每步少发几千字符，模型推理更快。
_SECTION_MARKERS: dict[str, tuple[str, ...]] = {
    "night": ("【夜晚阶段动作",),
    "low_freq": ("【机器操作", "【治安与战斗"),
    # v3.19 场景专节：只在真的用得上时注入，避免每步背着近 5K 无关规则
    "trade": ("【完整交易流程", "【谈判界面操作规则"),
    "slots": ("【格子读数",),
    "phone": ("【电话动作",),
}


def _scenario_flags(observation: Any) -> dict[str, bool]:
    """判断「此刻哪些专题小节用得上」。拿不准时返回 True —— 宁可多给，不要漏给。"""
    vi: dict[str, Any] = {}
    if isinstance(observation, dict):
        raw = observation.get("visible_info")
        if isinstance(raw, dict):
            vi = raw
    if not vi:
        return {"trade": True, "slots": True, "phone": True}

    client = vi.get("client") if isinstance(vi.get("client"), dict) else {}
    table = vi.get("table") if isinstance(vi.get("table"), dict) else {}
    night = vi.get("night") if isinstance(vi.get("night"), dict) else {}
    scene = str(vi.get("scene") or "").lower()

    return {
        # 交易/谈判专节：有顾客，或谈判面板开着/桌上已有议价物品
        "trade": bool(client) or bool(table.get("nego_open")) or bool(table.get("negociated_item")),
        # 格子读数：夜里要整理/拾荒，或真读到了格子内容
        "slots": bool(night) or bool(vi.get("slots")) or "night" in scene,
        # 电话专节：只在电话界面
        "phone": "phone" in scene,
    }


def _pick_bridge_sections(
    night_phase: bool = False,
    *,
    include_low_freq: bool = False,
    observation: Any = None,
    text: str | None = None,
) -> str:
    """按阶段 + 场景裁剪 ``BRIDGE_ACTION_TOOLS``，只保留当前用得上的小节。

    为什么在拼装时切而不是改常量：常量是维护者的单一真相（含实盘教训注释），
    切分只影响一次请求的体积，零信息损失、随时可回退。

    实测收益（v3.19）：
      - 白天不带夜晚流程：约 -4.7K
      - 无顾客时不带「完整交易流程」(2890) + 「谈判界面规则」(693)：约 -3.6K
      - 不在夜里/没读格子时不带「格子读数」(1003)：-1K
      - 不打电话时不带「电话动作」(440)：-0.4K

    Args:
        night_phase: 夜晚阶段保留「夜晚阶段动作」节，否则剔除。
        include_low_freq: 机器/治安战斗属低频，默认不注入；需要时置 True。
        observation: 用于判断场景；None 时专题节全保留（保守）。
        text: 便于测试注入；默认用模块级 BRIDGE_ACTION_TOOLS。
    """
    source = BRIDGE_ACTION_TOOLS if text is None else text
    parts = re.split(r"(?m)^(?=【)", source)
    drop: list[str] = []
    if not night_phase:
        drop.extend(_SECTION_MARKERS["night"])
    if not include_low_freq:
        drop.extend(_SECTION_MARKERS["low_freq"])

    if observation is not None:
        flags = _scenario_flags(observation)
        if not flags["trade"]:
            drop.extend(_SECTION_MARKERS["trade"])
        # 注意：**不裁**「格子读数」小节。它讲的是「件数 ≠ 容量，不许编造还能塞几件」，
        # 属于防错规则——实盘踩过编造容量的坑。省这 1K 字符换一次错误决策不划算。
        if not flags["phone"]:
            drop.extend(_SECTION_MARKERS["phone"])

    kept = [p for p in parts if not any(p.startswith(m) for m in drop)]
    return "".join(kept)


#: 库存明细里决策真正用得到的字段。
#: 坐标(cw/ch/ox/oy/mx/my)随时可用 ``ui_item_point`` 实时取，features/unique_id 属详情，
#: 每一步都带上纯属浪费——16 件物品的坐标就能吃掉 900+ 字符。
_INVENTORY_ITEM_KEYS = ("identifier", "name", "count", "value", "item_types", "inv")

#: 只在夜晚有意义、白天纯占位的字段。
_NIGHT_ONLY_FIELDS = ("locations", "afterhour_events", "services")


#: 动作说明行的前缀模式：``- 动作名：说明``（续行缩进两格）。
_ACTION_LINE_RE = re.compile(r"^- ([a-z_0-9]+)[：:]")


def _filter_action_docs(
    text: str, available: Optional[set[str]], all_actions: Optional[set[str]] = None
) -> str:
    """删掉「当前不可用动作」的说明行（含其缩进续行）。

    为什么这样做：动作说明是提示词最大的一块（约 11.5K 字符）。夜晚只有 15 个动作可用，
    却仍带着白天 52 个动作的说明；这些内容模型既不能选也用不上，纯属稀释注意力。

    只删**已知动作名**的行——策略段里普通的项目符号（``- 别囤垃圾货：…``）不受影响。
    """
    if not available:
        return text
    names = all_actions or set()
    lines = text.split("\n")
    out: list[str] = []
    dropping = False
    for line in lines:
        match = _ACTION_LINE_RE.match(line)
        if match:
            name = match.group(1)
            # 只处理确认为动作名的行；其余（策略小标题）原样保留
            dropping = name in names and name not in available
            if not dropping:
                out.append(line)
        elif dropping and (line.startswith("  ") or line.startswith("\t")):
            continue  # 动作说明的续行，一并丢弃
        else:
            dropping = False
            out.append(line)
    return "\n".join(out)


def _project_observation(observation: dict[str, Any]) -> dict[str, Any]:
    """按当前场景裁剪 observation。**只影响提示词，不碰守卫与决策逻辑。**

    为什么单独一层：agent_loop 的卷帘门/租金红线/夜间幂等守卫依赖**完整**字段，
    若在 observer 层就删，守卫会静默失效。裁剪只发生在「发给模型」这一步。

    裁剪原则：**裁此刻用不上的，不裁容易犯错的**。实盘踩坑换来的信息
    （顾客 buying_tags、桌上物品、garbage_level、attractiveness）一个都不动。

    Args:
        observation: 原始观察（不会就地修改）。
    Returns:
        投影后的观察副本；非字典或结构异常时原样返回。
    """
    if not isinstance(observation, dict):
        return observation
    vi = observation.get("visible_info")
    if not isinstance(vi, dict):
        return observation  # 视觉通道：字段本来就少，不动

    out = dict(observation)
    vi = dict(vi)

    # 1) 库存明细瘦身（保留标识/名称/数量/价值/类别）
    inv = vi.get("inventory")
    if isinstance(inv, dict):
        slim = dict(inv)
        for key in ("owned", "not_owned"):
            items = inv.get(key)
            if isinstance(items, list):
                slim[key] = [
                    {k: it.get(k) for k in _INVENTORY_ITEM_KEYS if k in it}
                    for it in items
                    if isinstance(it, dict)
                ]
        vi["inventory"] = slim

    # 2) 白天用不上的夜间字段（地点/夜间事件/服务面板）
    #    判定不能只看 night 是否存在——营业中也可能带着 night.visit_left（今晚还能出门几次），
    #    那样会误判成夜晚、白白保留 locations/services。以门店状态为准，再看是否真的在店外。
    night = vi.get("night") if isinstance(vi.get("night"), dict) else {}
    store_state = str(vi.get("store_state") or "")
    is_night = store_state in ("CLOSED", "CLOSING") or bool(night.get("is_outside"))
    if not is_night:
        for field in _NIGHT_ONLY_FIELDS:
            vi.pop(field, None)

    # 3) 通讯录只留名字（真要打电话时名字足够；号码/状态无用）
    phone = vi.get("phone")
    if isinstance(phone, dict) and isinstance(phone.get("contacts"), list):
        slim_phone = dict(phone)
        slim_phone["contacts"] = [
            {k: c.get(k) for k in ("name", "identifier") if k in c}
            for c in phone["contacts"]
            if isinstance(c, dict)
        ]
        vi["phone"] = slim_phone

    out["visible_info"] = vi

    # 4) 结构化通道：important_objects 是给视觉模型定位用的，bridge 不需要
    if str(observation.get("source") or "") in ("bridge", "statewatch"):
        out.pop("important_objects", None)
    return out


def build_decision_prompt(
    observation: dict[str, Any],
    image_size: Optional[tuple[int, int] | list[int]] = None,
    history: Optional[list[dict[str, Any]]] = None,
    history_limit: int = 6,
    *,
    bridge_actions: bool = False,
    no_mouse: bool = False,
    forbid_wait: bool = False,
    forbid_actions: Optional[set[str]] = None,
    cycle_forbid: Optional[set[str]] = None,
    night_phase: bool = False,
    goal_block: Optional[str] = None,
    mechanics_block: Optional[str] = None,
    experience_block: Optional[str] = None,
    available_action_names: Optional[set[str]] = None,
) -> str:
    """构建发给决策模型的完整提示词。

    Args:
        observation: 观察 dict（视觉契约或结构化契约，均可）。
        image_size: 当前截图尺寸 (宽, 高)，用于提示模型的坐标空间。
            结构化观察无截图，传 None。
        history: 最近操作历史（紧凑字典列表）。
        history_limit: 提示词中携带的历史条数上限。
        bridge_actions: 是否追加「游戏 API 直调动作」组。完全由调用方按观察通道
            显式控制（桥接可用时置 True）；本函数不按 observation["source"]
            做任何自动推断——statewatch 通道等价于桥接不可达，调用方传 False
            时绝不注入（T1 修复）。
        no_mouse: 是否禁用鼠标/键盘动作（禁鼠标模式）。True 时动作白名单只保留
            非输入类动作（wait / stop），并移除坐标说明——从提示词层面杜绝
            模型想点鼠标，AI 只能用游戏 API 直调动作。
        forbid_wait: 是否禁用 wait（反空转，r36）。模型连续选 wait 时置 True——
            从动作白名单中移除 wait，只留 stop + 直调动作，强制它推进局面。
        forbid_actions: 无效动作禁用集（v0.9.8「无效动作 streak」）——同名动作连续
            执行且状态零变化时由 agent_loop 传入，提示词明确禁止再选这些动作。
        cycle_forbid: 周期动作禁用集（v0.9.13「周期一次」）——end_day /
            begin_day / end_day_confirm / pay_rent / buy_insurance 在最近窗口内
            成功执行过即禁用（r49：end_day 连选 4 次全部真实生效，零变化 streak
            拦不住）。文案与 forbid_actions 分开：这类动作不是「没效果」而是
            「不该重复」，要去推进夜晚流程/开张营业。
        night_phase: 夜晚阶段（v0.9.15）——end_day 成功后置 True，此时营业类
            动作（open_shutter/call_next/sell_item…）已从白名单硬剔除（重新开门
            会导致当天结算回滚、天数卡死——r53 实测），只能执行夜晚流程。
        goal_block: 商业目标 KPI 段（v2.1，见 game_agent.goal）——含进度、
            记分口径与当日节奏要求；None/空串时不插入。
        mechanics_block: 机制知识段（v2.2，见 game_agent.knowledge）——按当前局面
            检索出的 1~3 张机制卡；None/空串时不插入。
        available_action_names: 当前**真的可选**的动作集合（v3.19）。给出时会删掉
            不可用动作的说明，避免夜晚还背着白天 52 个动作的长文。None = 不过滤。
        experience_block: 经验段（v3.15，见 game_agent.experience）——同一局面下
            历史上「某动作是否推进了局面」的统计；None/空串时不插入。
    """
    is_structured = str(observation.get("source") or "") in ("bridge", "statewatch")

    size_text = (
        f"{int(image_size[0])} x {int(image_size[1])}" if image_size else "未知（以 important_objects 的 point 为准）"
    )
    # 不缩进、紧凑分隔符：模型读 JSON 不需要美化，indent=1 会白白多送 ~30% 字符。
    # 实测这 2.2K 字符约等于 3~4 秒生成延迟（输入 token 与耗时近似线性）。
    observation_json = json.dumps(
        _project_observation(observation), ensure_ascii=False, separators=(",", ":")
    )

    # v2.8：把观察层的确定性结论（visible_info.next_step_hints）单独置顶。
    # 埋在 JSON 里模型容易忽略；实盘教训是「看得见状态却不行动」——
    # 顾客要的货没有仍连猜 5 步按钮、visit_left>0 连续多晚不出门拾荒。
    _hints = [
        str(h).strip()
        for h in ((observation.get("visible_info") or {}).get("next_step_hints") or [])
        if str(h).strip()
    ]
    hints_block = ""
    if _hints:
        hints_block = (
            "\n【本轮优先动作·系统按局面算出】\n"
            + "\n".join(f"- {h}" for h in _hints)
            + "\n（这是确定性结论，不是建议：与你的计划冲突时以它为准；"
            "若你判断它不适用，必须在 reason 里说清楚为什么，否则按它执行。）\n"
        )

    # 节奏规则按通道切换（r34 回归）：
    # 鼠标模式需人为留出反应时间；接口直调模式下系统已在每个动作后自动等待
    #（after_action_delay + NPC 动画延迟），再让模型 wait 只会原地卡死。
    rhythm_rule = (
        "0. 节奏要求（重要）：系统在你每个动作之后**已自动等待**（约 1 秒，"
        "叫号/送客等动作还会额外等动画完成），你**不需要也不应该**用 wait 来等。"
        "每一步都要真正推进局面：顾客进店先用 dialog_advance 看来意，随后立刻"
        "sell_item / buy_item / dismiss_client 成交或送客，绝不空等。"
        if no_mouse else
        "0. 节奏要求（重要）：你是一个真实店主，不是机器——每个动作之间系统已有约 1 秒停顿，"
        "不要追求速度：顾客进店后先 dialog_advance 对话一轮看看来意，再决定交易或送客；"
        "遣散顾客前先用 wait {\"seconds\": 2} 给对方一点时间；成交后稍等再叫下一位。"
        "NPC 刚进门就被送走、连续秒买秒卖都是不对的。"
    )

    # 按阶段裁剪动作说明：白天不发夜晚流程，机器/战斗默认不发（低频）。
    bridge_tools = (
        _pick_bridge_sections(night_phase, observation=observation)
        if bridge_actions else ""
    )
    if bridge_tools and available_action_names:
        from game_agent.contracts import derive_actions  # 局部导入避免循环

        bridge_tools = _filter_action_docs(
            bridge_tools, available_action_names, set(derive_actions())
        )

    tools = ACTION_TOOLS
    if bridge_actions:
        tools = tools + "\n\n" + bridge_tools
    if no_mouse:
        # 禁鼠标模式：动作白名单只留非输入类动作（禁鼠标动作组 + wait/stop）
        tools = NO_MOUSE_ACTION_TOOLS + (
            "\n\n" + bridge_tools if bridge_actions else ""
        )
    if forbid_wait:
        # 反空转（r36）：连续 wait 时彻底移除 wait，只留 stop + 直调动作。
        # 提示词层的「禁止 wait」劝导对推理模型无效（它自洽认为「还在加载」），
        # 必须从白名单里拿掉才能真正阻断。
        tools = NO_WAIT_ACTION_TOOLS + (
            "\n\n" + bridge_tools if bridge_actions else ""
        )
    if forbid_actions:
        # v0.9.8「无效动作 streak」：同名动作连续多次且零状态变化 → 明确禁用
        banned = "、".join(sorted(forbid_actions))
        tools = tools + (
            f"\n\n⛔【无效动作禁用（自动检测）】以下动作已连续多次执行且没有产生任何变化，"
            f"禁止再选：{banned}——必须换策略（换物品 / 换顾客 / 换其它动作类型），"
            f"重复无效动作只会浪费整轮机会。"
        )
    if cycle_forbid:
        # v0.9.13「周期一次」：这些动作刚成功执行过（打烊/开张/交租等），
        # 重复执行永远非法——不是没效果，是不该做。要走夜晚流程或开张营业。
        banned = "、".join(sorted(cycle_forbid))
        tools = tools + (
            f"\n\n⛔【周期动作禁用（每周期仅一次）】以下动作刚刚成功执行过，"
            f"禁止再选：{banned}——每一天/每个周期只能做一次。"
            f"打烊后应进入夜晚流程：open_map 打开地图 → go_out 出门办事 → "
            f"leave_outside 返回 → stay_home 睡觉（可先 night_services 选购服务）"
            f"→ end_day_confirm 确认 → 次日 open_shutter 开张。"
        )
    if night_phase:
        # v0.9.15 夜晚阶段横幅：打烊后营业动作已从白名单硬剔除（重新开门 =
        # 当天结算回滚、天数卡死，r53 实测「没睡觉一直第 11 天」的根因）。
        tools = tools + (
            "\n\n🌙【夜晚阶段（已打烊）】现在只能执行夜晚流程动作，"
            "营业类动作（open_shutter / close_shutter / call_next / sell_item / "
            "buy_item / dismiss_client 等）已被系统禁止——打烊后重新开门叫客会让"
            "当天结算回滚、天数卡住不前进（实测教训）。\n"
            "标准流程：① night_services 打开服务面板 → night_service 勾选服务"
            "（现金紧张只买保险+清洁）→ end_day_confirm 确认扣费；"
            "② open_map 打开夜间地图 → go_out 出门办事（今晚 visit_left 次内）"
            "→ leave_outside 返回；③ stay_home 睡觉度过夜晚；"
            "④ 次日 begin_day 开张营业。服务选购也可在睡前 stay_home 前完成。\n"
            "⛔ 界面已打开时禁止重复打开：地图已开着（看得到目的地按钮）就直接"
            " go_out 或 stay_home，不要再调 open_map（r54 实测空转 5 步卡死）；"
            "人在外出地点就先 leave_outside 回店。"
        )
    # 节奏规则按通道替换（r34 回归：原「节奏要求」教模型 wait，接口直调模式下
    # 系统每个动作后已自动等待，模型再 wait 只会原地卡死——15/15 全是 wait）
    tools = tools.replace("__RHYTHM_RULE__", rhythm_rule)

    if is_structured:
        # 结构化说明按通道能力降级：有直调通道才引导优先使用直调动作（T1）
        coord_note = (STRUCTURED_NOTE if bridge_actions else STRUCTURED_NOTE_NO_BRIDGE)
        # v3.1：结构化通道才带得出这些字段，所以正式版机制说明只在结构化下注入
        coord_note = coord_note + "\n\n" + FULL_RELEASE_NOTE
        state_intro = "【当前游戏状态（来自游戏内部状态直读）】"
        task_intro = "你现在是这家当铺的 AI 老板，需要根据当前的游戏内部状态，决定【下一步要执行的一个操作】。"
    else:
        coord_note = COORDINATE_NOTE
        state_intro = "【当前游戏状态（来自画面观察）】"
        task_intro = "你现在是这家当铺的 AI 老板，需要根据当前的游戏画面信息，决定【下一步要执行的一个操作】。"

    # 禁鼠标模式下没有截图坐标空间可言，去掉尺寸说明与坐标提示
    size_line = "" if no_mouse else (
        f"\n【当前截图尺寸】{size_text}（click/move 的坐标空间与此一致，原点=窗口左上角）\n"
    )
    coord_block = "" if no_mouse else f"\n{coord_note}\n"
    decision_rules = (
        "4. **绝不用 wait 等顾客**：顾客不会自己改变主意，只有你的动作会改变局面。"
        "每轮必须推进一步——顾客在买就 sell_item、顾客在卖就 buy_item（或 dismiss_client 换人）、"
        "没人就 call_next、队列空就 end_day、面板开着就批发/成交/换人。"
        "wait 只用于等系统动画（对话推进、场景加载），一次之后必须换成推进动作。"
        if no_mouse else
        "3. 对不确定的信息保持保守：宁可 wait / move 观察，也不要盲目点击未知区域。"
    )

    # v2.7：顾客操作专用动作规则——动作名只在桥接模式出现（statewatch 模式
    # 的提示词禁止提及桥接动作，TestBrainBridgeActions 有契约测试）
    client_action_rule = (
        "7. **顾客操作一律用专用动作**：遣散=`dismiss_client`、叫客=`call_next`、"
        "强制遣散=`force_dismiss`、推进对话=`dialog_advance`、对话选项=`dialog_choose {text}`。"
        "**禁止用 click_button + 猜测按钮文字**来代替它们——猜错 tooltip 会连续空转"
        "（实盘教训：连猜「打发走当前顾客」5 次全失败，烧掉 5 步）。click_button 只用于"
        "没有专用动作的界面元素，且 text 参数必须照抄 /ui/buttons 里的原文。"
        if bridge_actions else
        "7. **不要用 click_button + 猜测按钮文字**来代替已有专用操作——猜错 tooltip "
        "会连续空转。click_button 只用于没有专用动作的界面元素，且 text 参数必须"
        "照抄 /ui/buttons 里的原文。"
    )

    # v3.18（用户 2026-11-22「他怎么不用知识点」）：hints 原来排在提示词 **88%** 的位置，
    # 前面压着上万字符的动作说明与 observation JSON——模型基本读不到重点。
    # 改用 primacy 位置：紧跟 GAME_CONTEXT 置顶。降级时加警示前缀（hints 可能基于假空数据算出）。
    top_hints = ""
    if hints_block:
        top_hints = hints_block
        if str(observation.get("visible_info", {}).get("is_degraded") or "") not in ("", "False", "false"):
            top_hints = top_hints.replace(
                "【本轮优先动作·系统按局面算出】",
                "【本轮优先动作·系统按局面算出（⚠️观察有降级，结论可能不准，先核对 observation）】",
                1,
            )

    # T13：读数降级段。未降级时返回空串 → 未降级轮次的提示词逐字节不变。
    # 刻意放在 observation_json **之后、hints_block 之前**：
    # ① 模型刚读完状态 JSON 就立刻看到「这些块是不知道」，不会被占位值先入为主；
    # ② hints（next_step_hints）可能是基于降级块算出来的，必须先声明它可疑，
    #    否则模型会把「今天没顾客了」这类假空结论当确定指令执行。
    degraded_note = build_degraded_note(observation)

    # T13 ③：只在有直调动作时才把「不认 ok 标志」与降级连起来——该段引用的
    # 「成交方向一律以现金变化判定」只存在于 BRIDGE_ACTION_TOOLS 里，
    # 视觉/无桥通道注入它会出现悬空引用。
    unreliable_note = UNRELIABLE_SIGNAL_NOTE if bridge_actions else ""

    return f"""{GAME_CONTEXT}

{top_hints}{task_intro}

{tools}
{coord_block}{size_line}
{state_intro}
{observation_json}
{degraded_note}
【最近操作历史】
{_render_history(history or [], history_limit)}

{experience_block or ""}{mechanics_block or ""}
{goal_block or ""}

{unreliable_note}【决策要求】
1. 目标是像一位稳健、精明的当铺老板一样推进游戏；优先推进主线流程（如进入游戏、完成当前界面的事务）。
2. 一次只决定一个动作；如果需要多步，后续步骤会再次决策。
3. **reason 就是你自己的想法，不是作文**：说清你此刻看到的关键局面、为什么挑这个动作、接下来预期什么变化。**不要套【依据】【策略】【预期】这类固定框架**，也不要把规则条文抄一遍——那些东西系统早就知道。写你真实的推理过程，哪怕粗糙、口语化、带犹豫，都比模板强；但如果这一步确实很简单（比如只剩一个动作可做），一句话说清就行，不必硬凑。
{decision_rules}
5. **打烊（end_day）前必须过检查单**——逐项自查，全部为"否"才允许打烊：
   ① `night.visit_left > 0` 且今晚没出过门 → 走拾荒流程：
      `open_map` → `go_out`（垃圾倾倒场）→ `scavenge`（次数内尽量多拾）→
      `take_ground_items`/`stow_loot` 收进口袋 → `leave_outside` 带回。
      拾荒是**零成本货源**，放弃一次=白丢一天的自由资源（实盘教训：连续 4 晚全放弃）；
   ② `requests` 里有 `can_fill=true` → `fill_request`（信用点+声望白拿，过期作废）；
   ③ 展示柜已解锁且有好货未摆 → `display_case`（客源效果次日生效）；
   ④ 店里还有顾客且 `sellable_now` 非空 → 先做完生意再打烊；
   ⑤ 以上全"否" → 才 `end_day`。
6. **卖出纪律**：`sell_item` 的 identifier 只能照抄**本步观察**的 `sellable_now`；
   刚成交一笔后顾客的预算与多样性状态已变，下一件前必须以新的 `sellable_now` 为准，
   禁止凭记忆重复卖同一件；`client.refused` 列出的物品是这位顾客明确拒收的，不要再试。
{client_action_rule}
8. confidence 反映你对这个动作正确性的把握（0~1）。
9. 只输出一个 JSON 对象，不要输出任何解释、前后缀或 Markdown 代码块。"""
