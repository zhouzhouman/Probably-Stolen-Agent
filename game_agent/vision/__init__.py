"""视觉观察模块：捕获游戏窗口画面并用视觉模型产出结构化游戏状态。

对外入口（供其他模块与测试使用）：
    observe()              —— 一步完成：定位窗口 → 截图 → 视觉分析 → 返回结构化状态
    find_game_window()     —— 按关键词查找游戏窗口
    list_windows()         —— 枚举当前所有可见窗口（排查用）
    capture_window()       —— 仅截图（不做分析）
    make_client()          —— 按配置创建视觉模型客户端（真实 / Mock）
"""

from game_agent.vision.analyzer import (
    build_observation_prompt,
    make_client,
    parse_observation,
)
from game_agent.vision.capture import capture_fullscreen, capture_window
from game_agent.vision.models import (
    CaptureResult,
    ObservationRecord,
    WindowInfo,
    validate_observation,
)
from game_agent.vision.observer import observe, save_observation
from game_agent.vision.window import find_game_window, list_windows, setup_dpi_awareness

__all__ = [
    "observe",
    "save_observation",
    "find_game_window",
    "list_windows",
    "setup_dpi_awareness",
    "capture_window",
    "capture_fullscreen",
    "make_client",
    "parse_observation",
    "build_observation_prompt",
    "validate_observation",
    "WindowInfo",
    "CaptureResult",
    "ObservationRecord",
]
