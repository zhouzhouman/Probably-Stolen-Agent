# Learnings

Corrections, insights, and knowledge gaps captured during development.

**Categories**: correction | insight | knowledge_gap

---

## [LRN-20261020-001] knowledge_gap

**Logged**: 2026-10-20T13:15:00+08:00
**Priority**: high
**Status**: resolved
**Area**: backend

### Summary
用户提供关键游戏机制：拾荒可多次进行；拾荒物品放在地上会消失，必须捡进口袋（背包）才能带回店里。

### Details
此前 AI 只把拾荒当作一次性动作，且不了解地面物品的消失机制——拾到的东西若不主动收进口袋，离开拾荒点即丢失，等于白跑一趟。这解释了此前拾荒收益低的实证现象。

### Suggested Action
① prompt.py 拾荒动作说明补充「可多次 scavenge」与「物品必须捡入口袋，放地上离开即消失」；② 夜晚流程里拾荒后应多拾几次再 leave_outside，离开前确认物品已入袋。

### Metadata
- Source: user_feedback
- Related Files: game_agent/brain/prompt.py
- Tags: game-mechanics, scavenging

---

## [LRN-20261020-002] knowledge_gap

**Logged**: 2026-10-20T13:15:00+08:00
**Priority**: high
**Status**: resolved
**Area**: backend

### Summary
交租日顾客队列固定规律：最后一位 = 房东，倒数第二位 = 商人；商人什么都会收（除违禁品）。

### Details
这是稳定规律（用户实测）。此前 AI 不利用这一点：交租日现金不够时只能干瞪眼。正确策略是——交租日在商人来之前（倒数第二位）把库存尽量卖给他回血攒租金，等最后一位房东上门时现金应已 ≥ rent_due，可正常 pay_rent 避免「拒付→延期→欠款滚雪球」死局。

### Suggested Action
prompt.py 房东与收租段落补充该队列规律，并写明经营策略：交租日把商人当「清库存回血」窗口，非违禁品全可卖。

### Metadata
- Source: user_feedback
- Related Files: game_agent/brain/prompt.py
- Tags: game-mechanics, rent, economy

## [LRN-20261020-003] correction

**Logged**: 2026-10-20T13:27:00+08:00
**Priority**: high
**Status**: resolved
**Area**: backend

### Summary
用户纠正：出门（拾荒）时必须选择背包——不选背包等于空手出门，捡到的东西没地方装。

### Details
LRN-20261020-001 中记录的「物品放地上会消失、要拿到口袋里」的口袋即背包：出门时要先选择背包（外出界面/对话的背包选项），拾到的东西收进背包才能带回。此前只记录了入袋规则，漏掉了「出门选背包」这一前置步骤。

### Suggested Action
prompt.py 拾荒铁律第①条改为「出门时必须选择背包」；夜晚外出流程中遇到背包选项（dialog choices / click_button）一律选背包。

### Metadata
- Source: user_feedback
- Related Files: game_agent/brain/prompt.py
- Tags: game-mechanics, scavenging
- See Also: LRN-20261020-001

## [LRN-20261020-004] correction

**Logged**: 2026-10-20T13:35:00+08:00
**Priority**: high
**Status**: resolved
**Area**: backend

### Summary
实盘问题（附截图）：次日早晨的「日历/夜间报告/状态」结算面板（右上角 ×）出现时必须立刻叉掉。

### Details
该面板为 CanvasPaperUI/_AdvCalendar，关闭按钮 CloseButton 与谈判面板的同名（_NegociationPanel/CloseButton）——按 name 盲点有歧义。面板不关会挡住开店接待（截图里游戏提示「开店迟到，错过一名顾客」）。

### Suggested Action
① click_button 增加 path 子串匹配参数（BridgeApi.cs FindButton v1.6）；② prompt.py 写入晨报面板铁律：click_button {"name":"CloseButton","path":"_AdvCalendar"}；③ 重新编译部署。

### Metadata
- Source: user_feedback
- Related Files: bridge/BridgeApi.cs, game_agent/brain/prompt.py
- Tags: game-mechanics, ui, day-start

### Resolution
- **Resolved**: 2026-10-20T18:25:00+08:00
- **Notes**: ①BridgeApi.FindButton 增加 path 子串匹配，click_button 支持 {"name":"CloseButton","path":"_AdvCalendar"}；②agent_loop 新增 _morning_report_guard：用 GET /ui/buttons 检测 _AdvCalendar 按钮，一旦存在就强制下发关闭动作、跳过模型决策（4 个单测，492 tests 全过）；③prompt.py 写入晨报面板铁律。

## [LRN-20261020-005] knowledge_gap

**Logged**: 2026-10-20T21:55:00+08:00
**Priority**: critical
**Status**: resolved
**Area**: backend

### Summary
拾荒「地面」物品在 EmporiumEntry.afterhourInventory；右侧是「两个口袋 + 一个背包位」
（afterhourPocketSlotInvLeft/Right/Backpack）。放在地面的物品回店即消失，必须逐件
搬进口袋/背包才带得走。

### Details
此前误以为地面 = backInvinvElementCounter（那是店内的台面格），导致
MapUIManager.TransferCounterToBackInv() 调用 ok 但零效果（实测：拾荒 2 次 +2 件，
回店后 owned 53→51，物品全丢）。Resources.FindObjectsOfTypeAll 抓不到 GameInventory
（普通 C# 类，非 UnityEngine.Object），只能通过 EmporiumEntry 的公开字段访问。

### Suggested Action
① bridge 新增 stow_loot：afterhourInventory → afterhourPocketSlotInvBackpack →
左口袋 → 右口袋逐件搬（Expel + UncheckedAccept）；② /debug/slots 暴露各格数量；
③ agent_loop 在拾荒点选 leave_outside 时强制先 stow_loot；④ prompt 写清「两个口袋
+ 一个背包位，背包出门时选好」。

### Metadata
- Source: user_feedback
- Related Files: bridge/BridgeApi.cs, game_agent/agent_loop.py
- Tags: game-mechanics, scavenging, inventory
- See Also: LRN-20261020-001, LRN-20261020-003

### Resolution
- **Resolved**: 2026-10-20T22:20:00+08:00
- **Notes**: stow_loot 实测成功——拾荒 2 轮后调用 stow_loot，回店后「效率模组」
  仍在背包容器（存档 back=2，且进入 owned 可交易）。收纳顺序改为 左口袋→右口袋→背包位
  （背包位通常放腰包本体，最后用）。模型会自发调用 stow_loot，Python 守卫在
  leave_outside 时兜底。
