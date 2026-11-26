"""深空当铺 AI Agent（deepspace-agent）。

目标：让 AI 自主游玩 Steam 游戏《深空当铺：可能是偷得》（Probably Stolen）。

架构（双通道）：
    数据直读（首选）：游戏日志 + 存档 → 结构化状态 → 决策 → 控制
    视觉观察（兜底）：游戏窗口 → 截图 → 视觉模型 → 结构化状态 → 决策 → 控制

当前进度：
    - vision      视觉观察模块（截图 + 视觉分析 → 结构化状态）        [v0.2.0 已实现]
    - control     控制模块（鼠标 / 键盘 / 窗口激活 / 点击预览）        [v0.2.0 已实现]
    - brain       决策模块（观察 + 历史 → {action, params, reason, confidence}）[v0.2.0 已实现]
    - agent_loop  主循环（观察→决策→执行，dry-run / 步数上限 / 熔断）  [v0.2.0 已实现]
    - statewatch  状态监听模块（日志事件流 + 存档快照，替代视觉识别）  [v0.3.0 已实现]
    - bridge      BepInEx 桥接模块（HTTP 直调游戏 API + 决策浮层推送）[v2.1.0 已实现]
    - goal        商业目标跟踪（现金净增 KPI + 库存底线 + 跨批次进度）[v2.1.0 已实现]
"""

__version__ = "2.1.0"
