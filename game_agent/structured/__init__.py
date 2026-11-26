"""结构化观察通道：绕开截图与视觉模型，直读游戏状态供 AI 决策。

三条通道按优先级：
- bridge：游戏内 BepInEx 插件 HTTP 桥（毫秒级，数据最全）；
- statewatch：游戏日志 + 存档文件直读（百毫秒级，无需插件）；
- vision：截图 + 视觉模型（兜底，在 vision 包，不在本模块）。

对外入口：
- channels.probe_channels / pick_source：通道探测与选择；
- channels.state_signature：状态变化签名（事件驱动主循环用）；
- observer.StructuredObserver：产出 brain 观察契约的结构化观察。
"""
from game_agent.structured.channels import (
    CHANNEL_BRIDGE,
    CHANNEL_STATEWATCH,
    CHANNEL_VISION,
    DEFAULT_PREFER,
    pick_source,
    probe_channels,
    state_signature,
)
from game_agent.structured.observer import (
    StructuredObservation,
    StructuredObserver,
    build_observation,
    derive_screen_state,
)

__all__ = [
    "CHANNEL_BRIDGE",
    "CHANNEL_STATEWATCH",
    "CHANNEL_VISION",
    "DEFAULT_PREFER",
    "pick_source",
    "probe_channels",
    "state_signature",
    "StructuredObservation",
    "StructuredObserver",
    "build_observation",
    "derive_screen_state",
]
