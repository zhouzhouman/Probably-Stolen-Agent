"""窗口定位模块：基于 Win32 API 枚举、查找与激活目标游戏窗口。

依赖：pywin32（win32gui / win32process / win32api），仅 Windows。
为便于导入与测试，模块在缺少依赖时不会立即崩溃，
而是在真正调用功能函数时抛出明确的错误。
"""
from __future__ import annotations

import ctypes
from typing import Optional

from game_agent.vision.models import WindowInfo

try:  # Windows 专有依赖
    import win32api  # type: ignore
    import win32gui  # type: ignore
    import win32process  # type: ignore

    WIN32_AVAILABLE = True
except ImportError:  # pragma: no cover - 非 Windows 或未安装 pywin32
    win32api = None  # type: ignore
    win32gui = None  # type: ignore
    win32process = None  # type: ignore
    WIN32_AVAILABLE = False


def setup_dpi_awareness() -> None:
    """声明进程 DPI 感知（Per-Monitor V2），应在读取窗口坐标 / 截图前调用。

    高 DPI 缩放下，若进程不声明 DPI 感知：
    - Win32 返回的是“逻辑坐标”，而截图得到的是“物理像素”，两者会错位；
    - 鼠标控制模块同样依赖此设置，必须保持一致。
    重复调用是安全的（幂等）。
    """
    if not WIN32_AVAILABLE:
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PROCESS_PER_MONITOR_DPI_AWARE
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass  # 尽力而为，失败不阻断


def _require_win32() -> None:
    if not WIN32_AVAILABLE:
        raise RuntimeError(
            "窗口功能依赖 pywin32（仅支持 Windows）。请先执行：pip install pywin32"
        )


def list_windows(min_size: tuple[int, int] = (0, 0)) -> list[WindowInfo]:
    """枚举当前所有“可见 + 有标题”的顶层窗口（可过滤最小尺寸）。"""
    _require_win32()
    setup_dpi_awareness()
    foreground = win32gui.GetForegroundWindow()
    results: list[WindowInfo] = []

    def _callback(hwnd: int, _extra) -> bool:
        if not win32gui.IsWindowVisible(hwnd):
            return True
        title = (win32gui.GetWindowText(hwnd) or "").strip()
        if not title:
            return True
        try:
            rect = win32gui.GetWindowRect(hwnd)  # (left, top, right, bottom)
        except Exception:
            return True
        width, height = rect[2] - rect[0], rect[3] - rect[1]
        if width < min_size[0] or height < min_size[1]:
            return True
        try:
            pid = win32process.GetWindowThreadProcessId(hwnd)[1]
        except Exception:
            pid = None
        results.append(WindowInfo(
            hwnd=hwnd,
            title=title,
            rect=rect,
            is_foreground=(hwnd == foreground),
            is_minimized=bool(win32gui.IsIconic(hwnd)),
            process_id=pid,
        ))
        return True

    win32gui.EnumWindows(_callback, None)
    return results


def find_game_window(
    keywords: list[str],
    exclude_keywords: Optional[list[str]] = None,
    min_size: tuple[int, int] = (400, 300),
) -> Optional[WindowInfo]:
    """按标题关键词查找游戏窗口。

    匹配规则：标题包含任意 keywords（不区分大小写）且不含任何 exclude_keywords。
    尺寸过滤：**最小化窗口不做尺寸过滤**（最小化时坐标是占位值，如 237x39 / 1x1，
    直接过滤会导致"游戏明明开着却找不到窗口"）；恢复动作由 capture_window 负责。
    排序偏好：非最小化 > 窗口面积大 > 当前前台。返回最佳匹配，找不到返回 None。
    """
    _require_win32()
    excludes = [k.lower() for k in (exclude_keywords or [])]
    min_width, min_height = min_size
    matches: list[WindowInfo] = []
    # 先全量取回（不做尺寸过滤），再在此处手动过滤，以便对最小化窗口豁免尺寸检查
    for win in list_windows(min_size=(0, 0)):
        title_lower = win.title.lower()
        hit = any(k.lower() in title_lower for k in keywords)
        blocked = any(e in title_lower for e in excludes)
        if not hit or blocked:
            continue
        if not win.is_minimized and (win.width < min_width or win.height < min_height):
            continue
        matches.append(win)
    if not matches:
        return None

    matches.sort(
        key=lambda w: (not w.is_minimized, w.width * w.height, w.is_foreground),
        reverse=True,
    )
    return matches[0]


def refresh_window(win: WindowInfo) -> WindowInfo:
    """重新读取窗口最新状态（恢复 / 移动 / 激活后坐标会变化）。"""
    _require_win32()
    try:
        rect = win32gui.GetWindowRect(win.hwnd)
        foreground = bool(win32gui.GetForegroundWindow() == win.hwnd)
        minimized = bool(win32gui.IsIconic(win.hwnd))
    except Exception:
        return win
    return WindowInfo(
        hwnd=win.hwnd,
        title=win.title,
        rect=rect,
        is_foreground=foreground,
        is_minimized=minimized,
        process_id=win.process_id,
    )


def get_window_dpi_scale(win: WindowInfo) -> float:
    """返回「窗口截图内容像素 → 屏幕物理像素」的缩放系数。

    - DPI 感知的窗口（多数游戏）：1.0，截图与物理像素 1:1；
    - DPI 不感知的窗口（被系统虚拟化缩放，如部分工具窗口）：返回系统缩放系数
      （例如 150% 缩放时为 1.5，即截图内容中的 1 像素对应屏幕 1.5 个物理像素）。

    鼠标控制依赖该系数换算坐标；检测失败时保守返回 1.0。
    """
    if not WIN32_AVAILABLE:
        return 1.0
    try:
        user32 = ctypes.windll.user32
        user32.GetWindowDpiAwarenessContext.argtypes = [ctypes.c_void_p]
        user32.GetWindowDpiAwarenessContext.restype = ctypes.c_void_p
        user32.GetAwarenessFromDpiAwarenessContext.argtypes = [ctypes.c_void_p]
        user32.GetAwarenessFromDpiAwarenessContext.restype = ctypes.c_int

        context = user32.GetWindowDpiAwarenessContext(ctypes.c_void_p(win.hwnd))
        if not context:
            return 1.0
        # 0=UNAWARE，1=SYSTEM_AWARE，2=PER_MONITOR_AWARE
        awareness = user32.GetAwarenessFromDpiAwarenessContext(context)
        if awareness != 0:
            return 1.0  # DPI 感知窗口：内容与物理像素 1:1

        # DPI 不感知窗口：实际缩放系数 = 所在显示器 DPI / 96。
        # 注意：不能使用 GetDpiForWindow——它对不感知窗口恒返回 96，
        # 必须查询窗口所在显示器的真实 DPI（GetDpiForMonitor，Win 8.1+）。
        dpi = 0
        try:
            user32.MonitorFromWindow.argtypes = [ctypes.c_void_p, ctypes.c_uint]
            user32.MonitorFromWindow.restype = ctypes.c_void_p
            monitor = user32.MonitorFromWindow(ctypes.c_void_p(win.hwnd), 2)  # MONITOR_DEFAULTTONEAREST
            if monitor:
                shcore = ctypes.windll.shcore
                shcore.GetDpiForMonitor.argtypes = [
                    ctypes.c_void_p, ctypes.c_int,
                    ctypes.POINTER(ctypes.c_uint), ctypes.POINTER(ctypes.c_uint),
                ]
                shcore.GetDpiForMonitor.restype = ctypes.c_long
                dpi_x = ctypes.c_uint()
                dpi_y = ctypes.c_uint()
                if shcore.GetDpiForMonitor(monitor, 0, ctypes.byref(dpi_x), ctypes.byref(dpi_y)) == 0:
                    dpi = dpi_x.value
        except Exception:
            dpi = 0
        if not dpi:
            try:
                user32.GetDpiForSystem.restype = ctypes.c_uint
                dpi = user32.GetDpiForSystem()
            except Exception:
                dpi = 96
        return (dpi / 96.0) if dpi else 1.0
    except Exception:
        return 1.0


def bring_to_front(win: WindowInfo) -> bool:
    """把窗口置于前台并激活（截图回退与鼠标/键盘控制都依赖它）。

    先尝试直接 SetForegroundWindow；若被系统“前台窗口锁定”策略拦截，
    使用 AttachThreadInput 技巧重试（绕过焦点抢占限制的常用方案）。

    Returns:
        bool：是否成功成为前台窗口。
    """
    _require_win32()
    hwnd = win.hwnd
    try:
        if win32gui.IsIconic(hwnd):
            win32gui.ShowWindow(hwnd, 9)  # SW_RESTORE
        win32gui.SetForegroundWindow(hwnd)
        if win32gui.GetForegroundWindow() == hwnd:
            return True

        # 兜底：把当前线程的输入队列附加到前台窗口/目标窗口线程，解除前台锁定
        fg = win32gui.GetForegroundWindow()
        current = win32api.GetCurrentThreadId()
        attached: list[int] = []
        for target in (fg, hwnd):
            if not target:
                continue
            try:
                thread_id = win32process.GetWindowThreadProcessId(target)[0]
            except Exception:
                continue
            if thread_id and thread_id != current:
                try:
                    win32process.AttachThreadInput(current, thread_id, True)
                    attached.append(thread_id)
                except Exception:
                    pass
        try:
            win32gui.BringWindowToTop(hwnd)
            win32gui.SetForegroundWindow(hwnd)
        finally:
            for thread_id in attached:
                try:
                    win32process.AttachThreadInput(current, thread_id, False)
                except Exception:
                    pass
        return win32gui.GetForegroundWindow() == hwnd
    except Exception:
        return False
