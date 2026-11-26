"""点击校准工具：在一次运行内完成「点击 → 截图 → 标记检测 → 对比」，验证坐标换算链路。

用法：
    python scripts/calibrate_click.py --x 400 --y 300 [--window GameAgent-TestWindow]

流程：
1. 定位窗口，打印窗口矩形 / DPI 缩放系数等诊断信息；
2. 截图（before）并检测已有标记；
3. 通过控制模块在截图坐标 (x, y) 处真实点击；
4. 重新截图（after）并检测标记；
5. 打印新增标记位置，并与预期坐标 (x, y) 对比。

判定标准：新增标记中心应 ≈ (x, y)（允许 ±（边框宽度+几像素）的偏差）。
"""
from __future__ import annotations

import argparse
import ctypes
import importlib.util
import sys
import time
from pathlib import Path

# 保证可以直接运行：把项目根目录加入 sys.path
PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from game_agent.config import artifacts_dir, load_config  # noqa: E402
from game_agent.control import controller  # noqa: E402
from game_agent.errors import ControlError  # noqa: E402
from game_agent.vision.capture import capture_window  # noqa: E402
from game_agent.vision.window import (  # noqa: E402
    find_game_window,
    get_window_dpi_scale,
    refresh_window,
)

# 导入 find_markers（同目录脚本）
_spec = importlib.util.spec_from_file_location(
    "find_markers", Path(__file__).parent / "find_markers.py"
)
find_markers_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(find_markers_module)
find_markers = find_markers_module.find_markers


def _print_dpi_debug(hwnd: int) -> None:
    """打印底层 DPI 相关 API 的原始返回值（诊断用）。"""
    try:
        user32 = ctypes.windll.user32
        user32.GetWindowDpiAwarenessContext.argtypes = [ctypes.c_void_p]
        user32.GetWindowDpiAwarenessContext.restype = ctypes.c_void_p
        user32.GetAwarenessFromDpiAwarenessContext.argtypes = [ctypes.c_void_p]
        user32.GetAwarenessFromDpiAwarenessContext.restype = ctypes.c_int
        user32.GetDpiForWindow.argtypes = [ctypes.c_void_p]
        user32.GetDpiForWindow.restype = ctypes.c_uint

        context = user32.GetWindowDpiAwarenessContext(ctypes.c_void_p(hwnd))
        awareness = user32.GetAwarenessFromDpiAwarenessContext(context) if context else None
        dpi = user32.GetDpiForWindow(ctypes.c_void_p(hwnd))
        print(f"  [dpi-debug] context={context} awareness={awareness} dpi={dpi}")
    except Exception as exc:  # pragma: no cover
        print(f"  [dpi-debug] 调用失败：{exc}")


def main() -> int:
    parser = argparse.ArgumentParser(description="点击坐标链路校准")
    parser.add_argument("--x", type=int, required=True, help="截图坐标 x")
    parser.add_argument("--y", type=int, required=True, help="截图坐标 y")
    parser.add_argument("--window", help="窗口标题关键词（默认用配置中的游戏关键词）")
    args = parser.parse_args()

    config = load_config()
    game_cfg = config.get("game", {})
    keywords = [args.window] if args.window else game_cfg.get("window_title_keywords", [])
    excludes = [] if args.window else game_cfg.get("exclude_title_keywords", [])
    win = find_game_window(keywords, excludes, tuple(game_cfg.get("min_window_size", (400, 300))))
    if win is None:
        print(f"未找到窗口：{keywords}")
        return 2

    shots_dir = artifacts_dir(config) / "screenshots"
    print(f"=== 点击校准：目标截图坐标 ({args.x}, {args.y}) ===")
    print(f"窗口：{win.title!r} hwnd={win.hwnd} rect={win.rect} size={win.width}x{win.height}")
    print(f"DPI 缩放系数：{get_window_dpi_scale(win)}")
    _print_dpi_debug(win.hwnd)

    before = capture_window(win, shots_dir, tag="calib_before")
    before_markers = find_markers(before.image_path)
    print(f"[before] {before.image_path}")
    print(f"[before] 已有标记 {len(before_markers)} 个：{[(m[0], m[1]) for m in before_markers]}")

    print("执行点击……")
    try:
        result = controller.click_image(win, args.x, args.y)
    except ControlError as exc:
        print(f"点击失败：{exc}")
        return 5
    print(f"[click ] {result.detail}")

    time.sleep(1.0)
    win_now = refresh_window(win)
    after = capture_window(win_now, shots_dir, tag="calib_after")
    after_markers = find_markers(after.image_path)
    print(f"[after ] {after.image_path}")
    print(f"[after ] 标记 {len(after_markers)} 个：{[(m[0], m[1]) for m in after_markers]}")

    before_set = {(m[0], m[1]) for m in before_markers}
    new_markers = [m for m in after_markers if (m[0], m[1]) not in before_set]
    print("---- 结果 ----")
    if new_markers:
        for (mx, my, count) in new_markers:
            dx, dy = mx - args.x, my - args.y
            verdict = "✅ 命中" if abs(dx) <= 40 and abs(dy) <= 40 else "❌ 偏差过大"
            print(f"新增标记 ({mx}, {my})  与目标偏差 ({dx:+d}, {dy:+d})  {verdict}")
    else:
        print("未检测到新增标记（点击可能未生效，或标记被聚类合并）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
