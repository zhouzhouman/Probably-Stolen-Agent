"""鼠标控制：基于 Win32 API 的底层操作。

所有坐标均为「屏幕物理像素绝对坐标」（进程已声明 DPI 感知）。
高层模块（controller.py）负责把窗口截图坐标转换为屏幕坐标。
"""
from __future__ import annotations

import time

from game_agent.errors import ControlError

try:
    import win32api  # type: ignore
    import win32con  # type: ignore

    WIN32_AVAILABLE = True
except ImportError:  # pragma: no cover - 非 Windows 或未安装 pywin32
    win32api = None  # type: ignore
    win32con = None  # type: ignore
    WIN32_AVAILABLE = False


def _require_win32() -> None:
    if not WIN32_AVAILABLE:
        raise ControlError("鼠标控制依赖 pywin32（仅支持 Windows）。请先执行：pip install pywin32")


def get_cursor_pos() -> tuple[int, int]:
    """读取当前光标屏幕坐标。"""
    _require_win32()
    return tuple(win32api.GetCursorPos())  # type: ignore[return-value]


def move_to(x: int, y: int, duration: float = 0.3, steps: int | None = None) -> None:
    """平滑移动鼠标到屏幕坐标 (x, y)。

    采用线性插值分步移动：许多游戏只响应带位移过程的光标移动（悬停效果、
    部分游戏引擎需要 mousemove 事件流而不是瞬移）。
    """
    _require_win32()
    start_x, start_y = win32api.GetCursorPos()
    dx, dy = int(x) - start_x, int(y) - start_y
    distance = max(abs(dx), abs(dy))
    if distance == 0:
        return
    if steps is None:
        steps = max(8, min(60, distance // 12))  # 依据距离自适应步数
    interval = (duration / steps) if duration and duration > 0 else 0.0
    for index in range(1, steps + 1):
        win32api.SetCursorPos((start_x + int(dx * index / steps), start_y + int(dy * index / steps)))
        if interval:
            time.sleep(interval)


def click(
    x: int | None = None,
    y: int | None = None,
    *,
    button: str = "left",
    clicks: int = 1,
    duration: float = 0.3,
    interval_between: float = 0.12,
) -> None:
    """点击鼠标。给出 (x, y) 时先平滑移动再点击。"""
    _require_win32()
    if x is not None and y is not None:
        move_to(x, y, duration=duration)
        time.sleep(0.05)  # 让游戏完成悬停状态更新
    if button not in ("left", "right"):
        raise ControlError(f"不支持的鼠标按键：{button}（可用：left / right）")
    down_flag = win32con.MOUSEEVENTF_LEFTDOWN if button == "left" else win32con.MOUSEEVENTF_RIGHTDOWN
    up_flag = win32con.MOUSEEVENTF_LEFTUP if button == "left" else win32con.MOUSEEVENTF_RIGHTUP
    for index in range(clicks):
        win32api.mouse_event(down_flag, 0, 0, 0, 0)
        time.sleep(0.03)  # 按下与释放之间的短间隔，模拟真实点击
        win32api.mouse_event(up_flag, 0, 0, 0, 0)
        if index < clicks - 1:
            time.sleep(interval_between)


def scroll(amount: int, *, x: int | None = None, y: int | None = None) -> None:
    """滚轮滚动。amount 正数向上滚动，负数向下滚动。"""
    _require_win32()
    if x is not None and y is not None:
        move_to(x, y, duration=0.2)
    win32api.mouse_event(win32con.MOUSEEVENTF_WHEEL, 0, 0, int(amount) * win32con.WHEEL_DELTA, 0)
