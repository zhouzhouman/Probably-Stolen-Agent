"""高层控制模块：窗口激活 + 坐标变换 + 动作执行 + 安全校验。

坐标系约定（见 control/__init__.py 说明）：
- 本模块对外接收的都是「截图坐标」（与游戏窗口截图 PNG 像素一致）；
- 执行前自动换算为屏幕坐标：屏幕坐标 = 窗口左上角 + 截图坐标 × DPI 缩放系数。

DPI 缩放：多数游戏是 DPI 感知窗口（系数 1.0，1:1）；对 DPI 不感知、
被系统虚拟化缩放的窗口（如 150% 缩放的工具窗口），系数为 1.5，
换算与越界校验都会自动处理（见 get_window_dpi_scale）。

安全机制（重要）：
- 点击 / 输入前必须把目标窗口置于前台并校验成功，否则拒绝执行
  （避免把鼠标键盘输入发给错误的窗口）；
- 坐标越界直接拒绝（越界按“截图可见内容范围”判断，而非物理尺寸）；
- dry_run 模式只校验与记录、不产生任何真实输入；
- 每个动作返回 ControlResult（含执行详情与耗时），可审计、可回放。
"""
from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from PIL import Image, ImageDraw

from game_agent.control import keyboard, mouse
from game_agent.errors import ControlError
from game_agent.vision.models import WindowInfo
from game_agent.vision.window import bring_to_front, get_window_dpi_scale, refresh_window


@dataclass
class ControlResult:
    """一次控制动作的执行结果。"""

    action: str
    params: dict[str, Any]
    executed: bool        # False 表示 dry-run（未真实操作）
    ok: bool
    detail: str
    duration_ms: int
    executed_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def screenshot_to_screen(win: WindowInfo, x: int, y: int) -> tuple[int, int]:
    """把窗口截图坐标换算为屏幕物理像素绝对坐标（自动处理 DPI 缩放）。"""
    scale = get_window_dpi_scale(win)
    return win.rect[0] + int(round(x * scale)), win.rect[1] + int(round(y * scale))


def validate_point(win: WindowInfo, x: int, y: int) -> None:
    """校验截图坐标是否落在窗口「可见内容」范围内，越界抛 ControlError。

    注意：DPI 不感知的窗口，截图内容只占图像左上角一部分（其余是黑边），
    可见内容范围 = 物理尺寸 / DPI 缩放系数。
    """
    scale = get_window_dpi_scale(win)
    max_x = int(win.width / scale) if scale else win.width
    max_y = int(win.height / scale) if scale else win.height
    if not (0 <= x < max_x and 0 <= y < max_y):
        raise ControlError(
            f"坐标越界：({x}, {y}) 不在窗口截图范围 (0..{max_x - 1}, 0..{max_y - 1}) 内。"
            "坐标应为「窗口截图像素坐标」（原点=窗口左上角）。"
        )


def _activate(win: WindowInfo, retries: int = 3, wait: float = 0.35) -> WindowInfo:
    """把游戏窗口置于前台并校验成功，返回刷新后的窗口信息。

    点击 / 按键前必须调用：确保输入发送给游戏而不是其他窗口。
    Raises:
        ControlError: 连续尝试后仍未能激活窗口。
    """
    for _ in range(max(1, retries)):
        if bring_to_front(win):
            time.sleep(wait)
            refreshed = refresh_window(win)
            if refreshed.is_foreground:
                return refreshed
        time.sleep(wait)
    raise ControlError(
        "无法激活游戏窗口（可能被其他窗口或系统策略拦截）。"
        "为避免把操作发送到错误窗口，已中止本次控制动作。"
    )


def click_image(
    win: WindowInfo,
    x: int,
    y: int,
    *,
    dry_run: bool = False,
    move_duration: float = 0.35,
    clicks: int = 1,
) -> ControlResult:
    """在窗口截图的 (x, y) 处点击（默认左键单击）。

    dry_run=True 时只做坐标校验与记录，不激活窗口、不移动鼠标。
    """
    started = time.time()
    validate_point(win, x, y)
    if dry_run:
        return ControlResult(
            action="click", params={"x": x, "y": y}, executed=False, ok=True,
            detail=f"dry-run：计划点击截图坐标 ({x}, {y})（未真实操作）",
            duration_ms=int((time.time() - started) * 1000),
        )

    active = _activate(win)
    validate_point(active, x, y)  # 激活后窗口位置可能变化，重新校验
    screen_x, screen_y = screenshot_to_screen(active, x, y)
    mouse.click(screen_x, screen_y, clicks=clicks, duration=move_duration)
    return ControlResult(
        action="click", params={"x": x, "y": y}, executed=True, ok=True,
        detail=f"已点击截图坐标 ({x}, {y}) → 屏幕 ({screen_x}, {screen_y})",
        duration_ms=int((time.time() - started) * 1000),
    )


def ctrl_click_image(
    win: WindowInfo,
    x: int,
    y: int,
    *,
    dry_run: bool = False,
    move_duration: float = 0.35,
) -> ControlResult:
    """按住 Ctrl 点击窗口截图的 (x, y) 处。

    游戏内「Ctrl+点击 = 快速转移物品」——把库存物品移到谈判柜台的标准操作。
    2026-10-14 真机验证：谈判界面开启时，Ctrl+点击库存物品可使其正确进入议价
    （底层 API 直调均无效，UI 层事件是唯一入口，见 .workbuddy/memory）。
    """
    started = time.time()
    validate_point(win, x, y)
    if dry_run:
        return ControlResult(
            action="ctrl_click", params={"x": x, "y": y}, executed=False, ok=True,
            detail=f"dry-run：计划 Ctrl+点击截图坐标 ({x}, {y})（未真实操作）",
            duration_ms=int((time.time() - started) * 1000),
        )

    try:
        import win32api
        import win32con
    except ImportError as exc:  # pragma: no cover - 仅在缺 pywin32 的环境触发
        raise ControlError(f"Ctrl+点击需要 pywin32：{exc}") from exc

    active = _activate(win)
    validate_point(active, x, y)  # 激活后窗口位置可能变化，重新校验
    screen_x, screen_y = screenshot_to_screen(active, x, y)
    win32api.keybd_event(win32con.VK_CONTROL, 0, 0, 0)
    try:
        time.sleep(0.06)  # 修饰键先于鼠标事件落下，确保游戏轮询到 Ctrl 状态
        mouse.click(screen_x, screen_y, duration=move_duration)
    finally:
        time.sleep(0.06)
        win32api.keybd_event(win32con.VK_CONTROL, 0, win32con.KEYEVENTF_KEYUP, 0)
    return ControlResult(
        action="ctrl_click", params={"x": x, "y": y}, executed=True, ok=True,
        detail=f"已 Ctrl+点击截图坐标 ({x}, {y}) → 屏幕 ({screen_x}, {screen_y})",
        duration_ms=int((time.time() - started) * 1000),
    )


def move_image(
    win: WindowInfo,
    x: int,
    y: int,
    *,
    dry_run: bool = False,
    move_duration: float = 0.35,
) -> ControlResult:
    """把鼠标移动到窗口截图的 (x, y) 处（不点击，用于悬停查看）。"""
    started = time.time()
    validate_point(win, x, y)
    if dry_run:
        return ControlResult(
            action="move", params={"x": x, "y": y}, executed=False, ok=True,
            detail=f"dry-run：计划移动鼠标到截图坐标 ({x}, {y})（未真实操作）",
            duration_ms=int((time.time() - started) * 1000),
        )

    active = _activate(win)
    validate_point(active, x, y)
    screen_x, screen_y = screenshot_to_screen(active, x, y)
    mouse.move_to(screen_x, screen_y, duration=move_duration)
    return ControlResult(
        action="move", params={"x": x, "y": y}, executed=True, ok=True,
        detail=f"已移动鼠标到截图坐标 ({x}, {y}) → 屏幕 ({screen_x}, {screen_y})",
        duration_ms=int((time.time() - started) * 1000),
    )


def press_key(
    win: WindowInfo,
    key: str,
    *,
    times: int = 1,
    dry_run: bool = False,
) -> ControlResult:
    """向游戏窗口发送按键（如 escape / enter / space / up 等）。"""
    started = time.time()
    if dry_run:
        return ControlResult(
            action="press_key", params={"key": key, "times": times}, executed=False, ok=True,
            detail=f"dry-run：计划按下 {key!r} × {times}（未真实操作）",
            duration_ms=int((time.time() - started) * 1000),
        )

    active = _activate(win)
    keyboard.press(key, times=times)
    return ControlResult(
        action="press_key", params={"key": key, "times": times}, executed=True, ok=True,
        detail=f"已在游戏窗口按下 {key!r} × {times}",
        duration_ms=int((time.time() - started) * 1000),
    )


def type_text(
    win: WindowInfo,
    text: str,
    *,
    dry_run: bool = False,
) -> ControlResult:
    """向游戏窗口输入 ASCII 文本。"""
    started = time.time()
    if dry_run:
        return ControlResult(
            action="type_text", params={"text": text}, executed=False, ok=True,
            detail=f"dry-run：计划输入 {text!r}（未真实操作）",
            duration_ms=int((time.time() - started) * 1000),
        )

    active = _activate(win)
    keyboard.type_text(text)
    return ControlResult(
        action="type_text", params={"text": text}, executed=True, ok=True,
        detail=f"已在游戏窗口输入 {text!r}",
        duration_ms=int((time.time() - started) * 1000),
    )


def save_click_preview(
    image_path: str | Path,
    x: int,
    y: int,
    out_dir: Path,
    crop_radius: int = 150,
) -> tuple[Path, Path]:
    """在截图上标出目标点（十字准星）并保存全图与放大裁剪图，用于点击前目视确认。

    这是控制环节的安全习惯：先预览确认坐标无误，再执行真实点击。

    Returns:
        (全图路径, 放大图路径)
    """
    image = Image.open(image_path).convert("RGB")
    marked = image.copy()
    draw = ImageDraw.Draw(marked)

    # 十字准星 + 中心圆环
    arm = 28
    draw.line([(x - arm, y), (x + arm, y)], fill=(255, 60, 60), width=3)
    draw.line([(x, y - arm), (x, y + arm)], fill=(255, 60, 60), width=3)
    draw.ellipse([x - 7, y - 7, x + 7, y + 7], outline=(255, 60, 60), width=2)

    out_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
    full_path = out_dir / f"click_preview_{timestamp}.png"
    marked.save(full_path)

    # 围绕目标点的放大裁剪图（2 倍），方便确认落点细节
    left, top = max(0, x - crop_radius), max(0, y - crop_radius)
    right, bottom = min(marked.width, x + crop_radius), min(marked.height, y + crop_radius)
    if right <= left or bottom <= top:
        raise ControlError(f"预览裁剪区域无效：({x}, {y}) 不在图像范围 {image.size} 内")
    crop = marked.crop((left, top, right, bottom))
    crop = crop.resize((crop.width * 2, crop.height * 2), Image.Resampling.LANCZOS)
    zoom_path = out_dir / f"click_preview_{timestamp}_zoom.png"
    crop.save(zoom_path)

    return full_path, zoom_path
