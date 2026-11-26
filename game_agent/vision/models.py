"""视觉观察模块的数据结构与输出校验。"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Optional

# 观察结果的 4 个核心字段（对下游的稳定契约，不得缺失）
REQUIRED_OBSERVATION_KEYS = ("screen_state", "available_actions", "important_objects", "confidence")

# screen_state 允许的枚举值（与提示词中给模型的约定保持一致）
SCREEN_STATES = (
    "main_menu",       # 主菜单 / 启动界面
    "creation",        # 角色创建 / 开局配置（专精、特性选择等）
    "day_start_news",  # 每日开始：报纸 / 新闻界面
    "shop_counter",    # 柜台营业：店内主界面
    "customer_offer",  # 顾客上门 / 展示物品 / 报价
    "negotiation",     # 讨价还价界面
    "inventory",       # 库存 / 仓库界面
    "market",          # 市场 / 交易 / 黑市界面
    "upgrade_menu",    # 升级 / 装修 / 技能界面
    "dialogue",        # 对话 / 剧情文本界面
    "night_map",       # 夜间地图：打烊后外出去向选择（v0.8.0，结构化通道）
    "outside",         # 夜晚外出中：配给站/升级商人/拾荒地点（v0.8.0，结构化通道）
    "loading",         # 加载中
    "unknown",         # 无法识别
)


@dataclass
class WindowInfo:
    """一个 Windows 顶层窗口的描述。"""

    hwnd: int
    title: str
    rect: tuple[int, int, int, int]  # (left, top, right, bottom)，物理像素
    is_foreground: bool = False
    is_minimized: bool = False
    process_id: Optional[int] = None

    @property
    def width(self) -> int:
        return max(0, self.rect[2] - self.rect[0])

    @property
    def height(self) -> int:
        return max(0, self.rect[3] - self.rect[1])

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class CaptureResult:
    """一次截图捕获的结果（含图像有效性统计，便于排查黑屏等问题）。"""

    image_path: str
    mode: str                    # "window" 或 "fullscreen"
    method: str                  # "printwindow"（后台抓取）或 "screen_grab"（抓屏回退）
    window: Optional[WindowInfo]
    image_size: tuple[int, int]
    stats: dict[str, float]      # 亮度 / 方差 / 颜色多样性
    captured_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["window"] = self.window.to_dict() if self.window else None
        return data


@dataclass
class ObservationRecord:
    """一次完整“观察”的记录：结构化状态 + 截图 + 溯源信息。"""

    observation: dict[str, Any]   # 视觉模型产出的结构化状态（已校验）
    screenshot: str               # 截图文件路径
    capture: dict[str, Any]       # 截图捕获元信息
    meta: dict[str, Any]          # 分析元信息（provider/model/耗时/原始输出等）

    def to_dict(self) -> dict[str, Any]:
        return {
            "observation": self.observation,
            "screenshot": self.screenshot,
            "capture": self.capture,
            "meta": self.meta,
        }


def _normalize_point(value: Any) -> Optional[list[int]]:
    """把对象的 point 字段规范化为 [int, int] 或 None（容忍浮点与非法输入）。"""
    if isinstance(value, (list, tuple)) and len(value) == 2:
        try:
            return [int(round(float(value[0]))), int(round(float(value[1])))]
        except (TypeError, ValueError):
            return None
    return None


def validate_observation(data: dict[str, Any]) -> dict[str, Any]:
    """校验并规范化视觉模型的输出。

    契约（4 个核心字段，缺失即抛 ValueError）：
      - screen_state: str
      - available_actions: list[str]
      - important_objects: list[{name, type, value, position, point?}]
        （point 为可选：对象中心点的截图像素坐标 [x, y]，供控制模块点击）
      - confidence: float，位于 [0, 1]
    可选扩展：visible_info: dict | None

    Raises:
        ValueError: 整体不是 dict，或缺少必需字段、字段类型完全无法规范化。
    """
    if not isinstance(data, dict):
        raise ValueError(f"观察结果必须是 JSON 对象，实际为 {type(data).__name__}")

    missing = [key for key in REQUIRED_OBSERVATION_KEYS if key not in data]
    if missing:
        raise ValueError(f"观察结果缺少必需字段：{missing}")

    # screen_state：强制为非空字符串
    screen_state = str(data["screen_state"]).strip() or "unknown"

    # available_actions：字符串自动包成单元素列表
    actions = data["available_actions"]
    if actions is None:
        actions = []
    elif isinstance(actions, str):
        actions = [actions]
    if not isinstance(actions, (list, tuple)):
        raise ValueError("available_actions 必须是数组")
    actions_out = [str(item).strip() for item in actions if str(item).strip()]

    # important_objects：单个对象自动包成列表；统一每个元素结构
    objects = data["important_objects"]
    if objects is None:
        objects = []
    elif isinstance(objects, dict):
        objects = [objects]
    if not isinstance(objects, (list, tuple)):
        raise ValueError("important_objects 必须是数组")
    objects_out: list[dict[str, Any]] = []
    for obj in objects:
        if isinstance(obj, dict):
            objects_out.append({
                "name": str(obj.get("name", "?")).strip() or "?",
                "type": str(obj.get("type", "unknown")).strip() or "unknown",
                "value": obj.get("value"),
                "position": obj.get("position"),
                "point": _normalize_point(obj.get("point")),
            })
        else:
            objects_out.append({
                "name": str(obj), "type": "unknown", "value": None,
                "position": None, "point": None,
            })

    # confidence：容错转换并夹到 [0, 1]
    try:
        confidence = float(data["confidence"])
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))

    # visible_info：可选扩展字段
    visible_info = data.get("visible_info")
    if not isinstance(visible_info, dict):
        visible_info = None

    return {
        "screen_state": screen_state,
        "available_actions": actions_out,
        "important_objects": objects_out,
        "confidence": round(confidence, 3),
        "visible_info": visible_info,
    }
