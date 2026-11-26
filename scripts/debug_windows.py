"""诊断脚本：枚举系统窗口（含不可见窗口），排查目标窗口名称与可见性。

用法：
    python scripts/debug_windows.py          # 列出全部（含不可见）
    python scripts/debug_windows.py steam    # 只看标题包含某关键词的窗口

与 `python -m game_agent.cli list-windows` 的区别：
后者只列“可见且有标题”的窗口（即观察模块实际能用的窗口），
本脚本会额外列出不可见/最小化窗口，用于排查“游戏在运行但找不到窗口”类问题。
"""
from __future__ import annotations

import sys

import win32gui

from game_agent.vision.window import list_windows


def main() -> None:
    keyword = sys.argv[1].lower() if len(sys.argv) > 1 else None

    print("== 模块视角：可见且有标题的窗口（list_windows）==")
    for win in list_windows(min_size=(0, 0)):
        if keyword and keyword not in win.title.lower():
            continue
        flag = "MIN" if win.is_minimized else "   "
        print(f"  hwnd={win.hwnd:<10} {win.width:>5}x{win.height:<5} {flag} {win.title!r}")

    print()
    print("== 原始视角：所有顶层窗口（含不可见，EnumWindows 全量）==")
    rows: list[tuple[int, str, bool, bool, tuple[int, int, int, int]]] = []

    def _callback(hwnd: int, _extra) -> bool:
        title = (win32gui.GetWindowText(hwnd) or "").strip()
        if not title:
            return True
        if keyword and keyword not in title.lower():
            return True
        rows.append((
            hwnd,
            title,
            bool(win32gui.IsWindowVisible(hwnd)),
            bool(win32gui.IsIconic(hwnd)),
            win32gui.GetWindowRect(hwnd),
        ))
        return True

    win32gui.EnumWindows(_callback, None)
    if not rows:
        print("  （没有匹配的窗口）")
    for hwnd, title, visible, minimized, rect in rows:
        flags = []
        if not visible:
            flags.append("HIDDEN")
        if minimized:
            flags.append("MIN")
        width, height = rect[2] - rect[0], rect[3] - rect[1]
        print(f"  hwnd={hwnd:<10} {width:>5}x{height:<5} {'+'.join(flags) or 'visible'} {title!r}")


if __name__ == "__main__":
    main()
