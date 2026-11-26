"""诊断工具：检查目标窗口的前台状态与激活动作是否成功。

用法：
    python scripts/probe_activation.py [窗口关键词]     # 默认 Probably Stolen
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import win32gui  # noqa: E402

from game_agent.vision.window import bring_to_front, find_game_window  # noqa: E402


def main() -> None:
    keywords = [sys.argv[1]] if len(sys.argv) > 1 else ["Probably Stolen"]
    win = find_game_window(keywords, [], (400, 300))
    if win is None:
        print(f"未找到窗口：{keywords}")
        return

    foreground = win32gui.GetForegroundWindow()
    print(f"目标窗口: {win.title!r} hwnd={win.hwnd}")
    print(f"  最小化: {win.is_minimized}   已在后台前台: {win.is_foreground}")
    print(f"当前前台窗口: hwnd={foreground} title={win32gui.GetWindowText(foreground)!r}")
    print(f"目标是否当前前台: {foreground == win.hwnd}")

    started = time.time()
    ok = bring_to_front(win)
    elapsed = time.time() - started
    time.sleep(0.3)
    now_foreground = win32gui.GetForegroundWindow()
    print(f"bring_to_front 结果: {ok}（耗时 {elapsed:.2f}s）")
    print(f"  现在前台窗口: hwnd={now_foreground} title={win32gui.GetWindowText(now_foreground)!r}")


if __name__ == "__main__":
    main()
