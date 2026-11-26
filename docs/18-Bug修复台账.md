# Bug 修复台账（v2.8）

记录实机运行中发现并已修复的全部问题。每条都写明**现象 → 根因 → 修法 → 验证**，
便于回溯与回归。按发现顺序编号。

---

## 一、接口与桥接层（4 条）

### BUG-1 `machine_take` 把产出放进谈判桌，物品"消失"

- **现象**：取产报成功，但 `/state` 的 `inventory.owned` 里找不到；紧接着 `machine_put`
  报"背包里没有"。机器槽位已空、物品不在任何可见容器——看起来像凭空丢失。
- **根因**：接收容器列表把 `ps.gridInv` 放在第一位。`ps.gridInv` 是**谈判桌的玩家侧格子**
  （对应 `/state` 的 `table.player_grid_items`），不是背包；而 `owned` 不含桌上物品。
- **修法**：目标链改为 `主背包(EmporiumEntry.invElement)` → `储物区(backInvinvElement)`
  → 谈判桌（仅兜底）。返回信息里写明落点名，并检查 `parentInventory == null`
  （此时物品成了"哪个容器都不在"的孤儿，必须如实上报）。
- **验证**：`machine_take` → "water_pitcher→主背包"，`/state` 可见，`machine_put` 放回成功。

### BUG-2 机器里的电池泄漏进 `/sellable`

- **现象**：顾客收电池时，可卖清单给出 8 块，而玩家散装电池只有 6 块——多出来的是
  机器槽位里正在供电的电池。真卖出去机器会当场停机。
- **根因**：`GameInventory.GetParentItem()` 对机器槽位库存（`GameSlotInventory`）
  拿不到所属机器，`IsInsideMachine` 判定失效。
- **修法**：先遍历所有机器，收集其槽位库存对象的**指针集合**（实测 49 个），
  再比对物品所在容器的指针；`/sellable` 与 `sell_item` 共用这份集合。
- **验证**：对账 —— 总量 14 电池 + 3 培育电池 − 机器槽内 6+2 = 可卖 8+1，与实际输出一致。

### BUG-3 `end_day` 确认框时序竞态（首次必然失败）

- **现象**：队列有顾客时打烊，第一次调用永远返回"卷帘门按钮未受理"，第二次才成功。
- **根因**：点完卷帘门后游戏才实例化 `_CloseShutterConfirmation`（否/是），
  要一到两帧；原实现点完立刻搜索，必然扑空。
- **修法**：点按钮后重试搜索确认框（6 次 × 150ms），命中则点「是」并回读 `storeState`。
  另外查清游戏规则：**当前还有顾客在店时必须先遣散**，此时错误信息直接提示
  "先 dismiss_client，再 end_day"，不再给含糊的"未受理"。
- **验证**：有顾客 → 明确指引遣散；遣散后 → 一次调用即 CLOSED。

### BUG-8 `sellable_now` 混进顾客明确拒收的物品

- **现象**（Day 5 第 10 步实盘）：`sellable_now` 给出 `packet_red_cigarette`，
  同一个顾客的 `client.refused` 里也列着它。AI 照清单去卖、被拦一次才改道。
- **根因**：两条来源都会漏 —— 游戏判定列表在预算/多样性变化后可能残留旧结果；
  标签回退路径（`buying_tags ∩ item_types`）根本不知道 `refused`。
- **修法**：观察层统一用 `client.refused` 对 `sellable_now` 做减法，再交给提示与守卫。
- **验证**：同一构造场景下 `sellable_now == []`，提示改为"换客"而非"硬卖"。

---

## 二、决策与执行层（6 条）

### BUG-4 「拒收清单」计算把 `/state` 轮询拖死（运行假死 21 分钟）

- **现象**：事件驱动运行 15 分钟只走了 1 步，之后彻底不动；日志无报错。
- **根因**：新加的 `refused` 需要对全部库存（约 200 件）逐件调
  `IsClientRefusingItem`，而事件驱动每 0.5s 轮询一次 `/state`——观察被拖到秒级，
  循环看起来像假死。
- **修法**：按"顾客标识 + 预算 + 物品数"做缓存键，同一访问期内只算一次；
  再加扫描上限 120 件作双保险。
- **验证**：有顾客时 `/state` 平均 **21ms**（修复前秒级）。

### BUG-5 `click_button` 猜按钮文字，连续空转 5 步

- **现象**：AI 反复发 `click_button {text:"打发走当前顾客"}`，全部失败；
  真实界面里没有这个 tooltip，而专用动作 `dismiss_client` 一直可用。
- **根因**：提示词没写"顾客操作要用专用动作"；失败信息也不给替代方案。
- **修法**：三层 ——
  ① 提示词规则 7 明确「遣散/叫客/打烊一律用专用动作，禁止猜文字」；
  ② 桥接失败信息按关键词追加指引（"遣散顾客请用专用动作 dismiss_client"）；
  ③ 执行层新增 `_stalled_guard`，本地直接拦下这类决策，不浪费桥接往返。
- **验证**：实机 `click_button{text:"打发走当前顾客"}` → "请用专用动作 dismiss_client"；
  后续运行不再出现猜文字。

### BUG-6 `wait` 等顾客（死局里不换客）

- **现象**（Day 5）：顾客要 MATERIAL，库存一件没有，AI 选 `wait{1.5}`，局面完全不变。
- **根因**：提示词写了"绝不用 wait"，但模型在"无货可卖"的死局里没有可执行的出路提示。
- **修法**：① `_stalled_guard` 拦下"有顾客 + 对话已结束 + sellable_now 为空"时的 wait；
  ② 观察层给出确定性出路（`dismiss_client`）。
- **验证**：单测覆盖"死局拦 / 有货放 / 对话中放"三种情形；实机 18 步里 3 次主动遣散、
  零 wait。

### BUG-7 夜间拾荒从未触发（连续多晚全放弃）

- **现象**：`night.visit_left > 0`，两个夜晚＋多轮运行一次都没出门；机制卡
  `scavenge-attempt-cap` 被命中 21 次也没转化为行动。
- **根因**：这不是接口问题——`open_map → go_out → scavenge → stow_loot → leave_outside`
  实测全部可用（3 次拾取→3 件装包→带回）。缺的是"到点该做"的触发：状态齐备，
  但没人把它翻译成动作。
- **修法**：观察层新增 `visible_info.next_step_hints`（用代码算结论），提示词把结论
  **置顶**成【本轮优先动作·系统按局面算出】；仅在**打烊后**提示（营业中跑出去会丢客人）。
- **验证**：实机一轮 18 步完整走出
  `end_day → open_map → go_out scavenging → scavenge×3 → stow_loot → leave_outside → night_services`，
  带回 `junk`；决策日志明确引用"系统给出的确定性优先动作"。

### BUG-9 `scavenge` 无收工规则（潜在空转）

- **现象**：`scavenge` 没有服务端次数上限（走游戏自己的按钮），提示若只说"继续 scavenge"，
  AI 可能整批步数烧在一个地点。
- **修法**：提示带 `scavenging_attempts`，**满 3 次改为催收工**（`stow_loot` → `leave_outside`）。
- **验证**：单测覆盖"第 0 次催再拾 / 第 3 次催收工"。

### BUG-10 提示词规则编号重复

- **现象**：`【决策要求】` 出现两个 `3.`，编号 1,2,3,3,4…8。
- **修法**：重排为 1–9；桥接专属规则按 `bridge_actions` 条件注入（statewatch 模式
  不得出现桥接动作名，有契约测试守着）。
- **验证**：单测断言编号递增且不重复。

---

## 三、工程与文档（2 条）

### BUG-11 机制总纲与卡片不同步（`test_doc_is_in_sync` 失败）

- **根因**：上一轮改了 `scripts/gen_knowledge_doc.py` 的第七节，但没重跑生成器。
- **修法**：`python scripts/gen_knowledge_doc.py` 重生成（194 张卡 / 38,734 字符），
  `--check` 通过。

### BUG-12 提示词里裸 `{text}` 触发 f-string 解析（59 项测试失败）

- **根因**：新增规则文本含 `dialog_choose {text}`，被当成占位符。
- **修法**：转义为 `{{text}}`；回归确认渲染出 `dialog_choose {text}`。

---

## 四、环境注意（非代码缺陷，记录以免再踩）

| 现象 | 说明 |
|---|---|
| 直接运行 `Probably Stolen.exe` 退出码 53 | 需要 Steam 环境；用 `steam://rungameid/4349200` 启动 |
| 新 DLL 覆盖失败 | 游戏进程占用文件；必须先退出（`taskkill`）再 `cp` 到 `BepInEx/plugins/` |
| `/status` 里 `machine_slots` 很小 | 说明机器未实例化（不在店铺场景），不是过滤失效 |

---

## 五、当前状态

- 回归测试：**596 passed**（新增 `tests/test_agent_hints.py` 21 项）
- 桥接动作 56 / 参数表 55（本轮未新增动作，只修行为与观测）
- 实机验证：Day 5 一轮 18 步零失败，拾荒闭环首次打通

---

## 六、第二轮实机追加（v2.9，Day 6–8）

### BUG-13 `buy_item` 同帧校验 → 误报「钱货两失」，AI 放弃交易

- **现象**（Day 7 供货商）：`buy_item cassette2d` 返回 `ok=false`，
  文案是「⚠️ 扣款 52 但物品未入仓」。AI 据此认定收购失败，随即 `dismiss_client` 放弃该顾客。
- **真相**：钱没白花，物品**确实进了背包**（随后 `/state` 的 owned 里就有 `cassette2d`，
  位置 `GameGridInventory`）。只是付款那一帧 `FindAllItem(true)` 还没刷新。
- **修法**：检测到「已扣款但未见入仓」时，按 6 次 × 120ms 重试计数；
  真入仓则如实报成功（注明"库存延迟刷新"），仍无则保留原来那条诚实的失败信息。
- **影响**：避免 AI 把已成交的交易当失败，进而丢掉整条交易链（本例还白送走一位供货商）。
- **验证**：代码已编译进 v2.9 DLL（Day 8 载入后生效）；`owned/not_owned` 去重同步验证通过。

### BUG-14 同一件物品同时出现在 `owned` 与 `not_owned`

- **现象**：收购后 `cassette2d` 在两个列表里都有（`/state` 的 `inventory.owned` 与
  `inventory.not_owned`），AI 无法判断"这件到底是不是我的"。
- **根因**：两个列表分别来自 `PlayerStore.FindAllItem(true/false)`；同一物件在某个时刻
  会被两边同时返回（收购刚完成后尤其明显）。
- **修法**：`/state` 输出前用 owned 的 identifier 集合对 `not_owned` 做差集（`_ExcludeOwned`）。
  owned 优先——那才是能拿去卖的那份。
- **验证**：Day 8 实测 `owned 31 种 / not_owned 0 种 / 重叠 0`。

### BUG-15 「拾满 3 次收工」规则踩了跨天累计值（v2.8 自引入的回归）

- **现象**：`night.scavenging_attempts` 是**跨天累计**的（Day 6 拾到 3，Day 7 一开局仍是 3）。
  v2.8 的收工规则直接读它，会让第二天一出门就立刻"收工"，整晚拾荒收益全丢。
- **修法**：观察层按「本次外出」计数——记住换天基线与出门基线，用
  `attempts - 出门基线` 作为本次次数（`_attempts_tonight`）。实测出门时游戏会把值清零，
  但不清零的中间态同样被这套基线兜住。
- **验证**：Day 7 夜晚实测 `att 0→1→2→3` 后催 `stow_loot → leave_outside`，收工点准确。

### BUG-16 `dialog_choose` 按序号盲选（历史事故源）

- **现象**：Day 7 交租对话，模型只给 `{"index": 0}`，靠序号选中「[支付租金（200）]」。
  这次选对了，但 r75 就是盲选 index 0 走进「延期交租」分支、房东卡店。
- **修法**：下发前用观察里的 `dialog.choices` 把序号反查成**选项原文**再发给桥接
  （`_enrich_dialog_choice`）；查不到则保留原样交给桥接报错。
  这样序号错位也点不歪，日志里也能直接看出选了哪句。
- **验证**：单测覆盖"换文字 / 已有文字不动 / 越界不改 / 非对话动作不改"。

---

## 七、决策耗时实测（回答"为什么慢"）

79 步样本（Day 6–7）：

| 环节 | 中位耗时 |
|---|---|
| 模型调用 | **31.8 s**（均值 33.5 s，最慢 104.5 s） |
| 桥接接口执行 | 36 ms |
| 动作后等待（action_gate） | 1.0 s |
| 单步合计 | 32.9 s |

真实提示词单独测一次模型：`prompt_tokens 13,155`、`completion_tokens 900`，
其中 **617 个是隐藏 reasoning tokens**（占 68%），实际决策文本仅 551 字符，
生成速度 40 tokens/s。

**结论**：慢在模型服务侧，不在游戏/桥接/本地代码。当前入口是第三方免费代理
`https://n.tokeness.dev/v1`（`tokeness/free`），它不遵守"禁用思考"：
试过 `reasoning_effort=minimal`（400 报错）、`enable_thinking=false`（无效）、
`chat_template_kwargs`（超时），都无法真正关掉推理。

可选提速方向（按见效排序）：① 换官方直连模型（单步可望降到 8–12 s）；
② 压缩提示词（现 26.9K 字符，其中静态规则约 15K、`inventory` 3,968）＋ 收紧 reason 长度；
③ 连招批处理（一次决策产出多步，把高频循环从 N 次调用降到 1 次）。

### BUG-17 决策浮层被「Mock 假决策」污染，冻了 9 天

- **现象**（用户 10-26 截图）：浮层一直显示「#0 点击 / x:971,y:880 /
  （Mock）顾客报价合适，点击收购按钮完成这笔交易」。看着像 AI 在乱下指令，
  但实际步骤记录里 **2341 步中 0 次**出现过这个动作。
- **根因**：`tests/test_brain_module.py` 里一处 `run_loop(..., mock=True, live=False)`
  **没有关掉 `push_overlay`**（默认 True）。mock 决策被推到真机 `/decision`，
  浮层写进假数据。最早一条是 2026-10-16，之后每次跑测试都再覆盖一次，
  玩家看到的面板就一直是那句话。
- **修法**（三层）：
  ① 测试显式 `push_overlay=False`；
  ② `agent_loop` 三处推送点统一加 `and not mock`——**mock 运行绝不推浮层**；
  ③ 新增 `test_mock_never_pushes_overlay` 守住这条契约。
  另外两个「步号透传」测试原本也用 mock 跑，改为 `mock=False` + 假决策源
  （monkeypatch `decide`），继续验证 `#1 #2 #3` 递进。
- **验证**：单测通过；浮层已推送一条说明覆盖掉旧假数据，之后由真实决策覆盖。

### BUG-18 模型调用超时重试没有总上限、且全程无日志（看起来像死机）

- **现象**（Day 8）：`open_map` 之后 10 分钟只走了 0 步，运行日志只有三行启动提示，
  进程还活着但毫无输出——现场判断不了是"慢"还是"卡死"。
- **根因**：单次请求 `timeout=60s`，`max_retries=2` → 最坏 3 次 × 60s + 退避 6s；
  若返回空内容且 `finish_reason=length`，还会「加倍预算再走一整轮」→ 单步最坏 ~6 分钟。
  期间没有任何日志。
- **修法**：
  ① 单次决策加**墙钟上限**（默认 `timeout × 2.5` = 150s，可配 `brain.max_total_seconds`），
     到点直接报「总耗时超过上限」，不再无限拖；
  ② **超时只重试一次**（`timeout_retries < 2`），连接错误仍按原策略；
  ③ 每次重试都写带时间戳的日志（`[brain HH:MM:SS] 模型调用失败（Timeout，60.1s）…`），
     并把逐次超时压到剩余预算内。
- **验证**：`tests/test_brain_limits.py`（4 项）锁住"超时最多 2 次请求 / 超预算报错 / 有日志"。
