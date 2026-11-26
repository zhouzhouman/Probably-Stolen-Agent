"""截图捕获模块。

捕获策略（默认自动，可强制指定）：
1. PrintWindow(hwnd, PW_RENDERFULLCONTENT=0x2)：后台抓取窗口内容，
   窗口被遮挡时也能工作，且不打扰用户（多数窗口化 / 无边框游戏可用）；
2. 回退：把窗口置前 + PIL.ImageGrab 抓取窗口区域（会短暂抢占前台焦点）。

每次捕获后执行“有效性检查”（过暗 = 黑屏、颜色过单一 = 纯色），
避免把无效画面喂给视觉模型浪费 token。
"""
from __future__ import annotations

import re
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from PIL import Image, ImageGrab

from game_agent.errors import CaptureError
from game_agent.vision.models import CaptureResult, WindowInfo
from game_agent.vision.window import (
    WIN32_AVAILABLE,
    bring_to_front,
    refresh_window,
    setup_dpi_awareness,
)

try:
    import win32gui  # type: ignore
    import win32ui  # type: ignore
    from ctypes import windll  # type: ignore
except ImportError:  # pragma: no cover
    win32gui = None  # type: ignore
    win32ui = None  # type: ignore
    windll = None  # type: ignore

# PrintWindow 标志：要求完整渲染内容（Win 8.1+），对 GPU 加速窗口更友好
PW_RENDERFULLCONTENT = 0x00000002

# 有效性判定阈值：平均亮度低于该值视为黑屏
MIN_BRIGHTNESS = 6.0
# 缩略图采样的颜色多样性低于该值视为纯色（无效画面）
MIN_UNIQUE_LEVELS = 4


def _slugify(text: str, max_length: int = 40) -> str:
    """把窗口标题清洗为合法的文件名片段（保留中文，去掉路径非法字符）。"""
    cleaned = re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", text).strip("_ .")
    return cleaned[:max_length] or "capture"


def image_stats(image: Image.Image) -> dict[str, float]:
    """图像有效性统计：平均亮度 / 亮度方差 / 颜色多样性（缩略图采样，快速）。"""
    gray = image.convert("L").resize((80, 45))
    # Pillow 12+ 推荐 get_flattened_data；旧版本回退 getdata
    if hasattr(gray, "get_flattened_data"):
        pixels = list(gray.get_flattened_data())
    else:  # pragma: no cover
        pixels = list(gray.getdata())
    mean = sum(pixels) / len(pixels)
    variance = sum((p - mean) ** 2 for p in pixels) / len(pixels)
    return {
        "mean_brightness": round(mean, 2),
        "variance": round(variance, 2),
        "unique_levels": float(len(set(pixels))),
    }


def is_image_valid(stats: dict[str, float]) -> bool:
    """判定截图是否“有内容”：过暗（黑屏）或颜色过单一（纯色）均视为无效。"""
    return stats["mean_brightness"] > MIN_BRIGHTNESS and stats["unique_levels"] >= MIN_UNIQUE_LEVELS


def save_image(image: Image.Image, save_dir: Path, prefix: str = "shot") -> Path:
    """把截图保存为 PNG（文件名带毫秒级时间戳），返回文件路径。"""
    save_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
    path = save_dir / f"{prefix}_{timestamp}.png"
    image.save(path, "PNG")
    return path


def _capture_with_printwindow(win: WindowInfo) -> Image.Image:
    """用 PrintWindow 抓取窗口内容（支持被遮挡的后台窗口）。"""
    if not WIN32_AVAILABLE:
        raise CaptureError("PrintWindow 需要 Windows + pywin32 环境")

    left, top, right, bottom = win32gui.GetWindowRect(win.hwnd)
    width, height = right - left, bottom - top
    if width <= 0 or height <= 0:
        raise CaptureError(f"窗口尺寸无效：{width}x{height}（窗口可能已关闭或最小化）")

    hwnd_dc = win32gui.GetWindowDC(win.hwnd)
    try:
        src_dc = win32ui.CreateDCFromHandle(hwnd_dc)
        mem_dc = src_dc.CreateCompatibleDC()
        bitmap = win32ui.CreateBitmap()
        try:
            bitmap.CreateCompatibleBitmap(src_dc, width, height)
            mem_dc.SelectObject(bitmap)
            ok = windll.user32.PrintWindow(win.hwnd, mem_dc.GetSafeHdc(), PW_RENDERFULLCONTENT)
            if not ok:
                raise CaptureError("PrintWindow 调用失败（窗口可能不支持后台抓取）")
            info = bitmap.GetInfo()
            bits = bitmap.GetBitmapBits(True)
            # BGRX：32 位小端格式，丢弃 alpha 通道
            image = Image.frombytes(
                "RGB", (info["bmWidth"], info["bmHeight"]), bits, "raw", "BGRX", 0, 1
            )
            return image
        finally:
            win32gui.DeleteObject(bitmap.GetHandle())
            mem_dc.DeleteDC()
            src_dc.DeleteDC()
    finally:
        win32gui.ReleaseDC(win.hwnd, hwnd_dc)


def _capture_with_screen(win: WindowInfo, wait_seconds: float = 0.4) -> tuple[Image.Image, list[str]]:
    """回退方案：置前窗口后直接抓屏。返回 (图像, 警告列表)。"""
    warnings: list[str] = []
    raised = bring_to_front(win)
    if not raised:
        warnings.append("窗口未能置于前台，截图可能被其他窗口遮挡")
    time.sleep(wait_seconds)  # 等待窗口置前后的渲染稳定
    rect = win32gui.GetWindowRect(win.hwnd)  # 置前后位置可能变化，重新读取
    image = ImageGrab.grab(bbox=rect, all_screens=True)
    return image, warnings


def capture_window(
    win: WindowInfo,
    save_dir: Path,
    tag: Optional[str] = None,
    method: str = "auto",
) -> CaptureResult:
    """捕获指定窗口并保存为 PNG。

    Args:
        method: auto（先 PrintWindow，黑屏则回退抓屏） | printwindow | screen

    若窗口处于最小化状态，会先自动恢复（带到前台）再截图。
    """
    setup_dpi_awareness()
    prefix = tag or f"win_{_slugify(win.title)}"
    warnings: list[str] = []
    image: Optional[Image.Image] = None
    used_method = ""

    # 最小化窗口无法渲染，先自动恢复（会将其带到前台）
    if win.is_minimized:
        if bring_to_front(win):
            warnings.append("窗口处于最小化，已自动恢复")
            time.sleep(0.8)  # 等待窗口恢复后的渲染稳定
            win = refresh_window(win)
        else:
            raise CaptureError("窗口处于最小化且无法自动恢复，请手动打开游戏窗口后重试")

    if method in ("auto", "printwindow"):
        try:
            image = _capture_with_printwindow(win)
            used_method = "printwindow"
            if not is_image_valid(image_stats(image)):
                if method == "printwindow":
                    raise CaptureError(f"PrintWindow 结果为黑屏/纯色：{image_stats(image)}")
                warnings.append("PrintWindow 结果为黑屏/纯色，改用抓屏回退")
                image = None
        except CaptureError as exc:
            if method == "printwindow":
                raise
            warnings.append(f"PrintWindow 失败（{exc}），改用抓屏回退")
            image = None

    if image is None:
        image, screen_warnings = _capture_with_screen(win)
        warnings.extend(screen_warnings)
        used_method = "screen_grab"

    stats = image_stats(image)
    if not is_image_valid(stats):
        raise CaptureError(f"捕获结果无效（可能是黑屏或纯色窗口）。统计信息：{stats}")

    path = save_image(image, save_dir, prefix=prefix)
    return CaptureResult(
        image_path=str(path),
        mode="window",
        method=used_method,
        window=win,
        image_size=(image.width, image.height),
        stats=stats,
        warnings=warnings,
    )


def capture_fullscreen(save_dir: Path, tag: Optional[str] = None) -> CaptureResult:
    """捕获整个虚拟屏幕（多显示器拼接）。仅建议用于无游戏窗口时的管线自检。"""
    setup_dpi_awareness()
    image = ImageGrab.grab(all_screens=True)
    stats = image_stats(image)
    if not is_image_valid(stats):
        raise CaptureError(f"全屏捕获结果无效：{stats}")
    path = save_image(image, save_dir, prefix=tag or "fullscreen")
    return CaptureResult(
        image_path=str(path),
        mode="fullscreen",
        method="screen_grab",
        window=None,
        image_size=(image.width, image.height),
        stats=stats,
        warnings=["全屏模式会包含桌面等无关内容，仅建议用于管线自检"],
    )
