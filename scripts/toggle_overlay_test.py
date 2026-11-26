"""浮层显隐切换测试：向游戏发送 Ctrl+F10，验证浮层 visible 状态翻转。

用法（游戏运行中）：
    python scripts/toggle_overlay_test.py

流程：
    1. 找到并激活游戏窗口
    2. 发送 Ctrl+F10 → 轮询桥 /decision 确认 visible 翻转
    3. 再发一次 Ctrl+F10 → 确认翻回
    4. 全程打印结果，任一步失败返回非零退出码
"""
from __future__ import annotations

import sys
import time

from game_agent.bridge.client import BridgeClient
from game_agent.config import load_config
from game_agent.control import controller, keyboard
from game_agent.vision.window import find_game_window


def send_ctrl_f10() -> None:
    """发送 Ctrl+F10 组合键（携带真实扫描码，Ctrl 保持覆盖 F10 按下/释放）。"""
    import win32api
    import win32con

    ctrl_vk = keyboard.resolve_vk("ctrl")
    f10_vk = keyboard.resolve_vk("f10")
    ctrl_scan = win32api.MapVirtualKey(ctrl_vk, 0)
    f10_scan = win32api.MapVirtualKey(f10_vk, 0)

    win32api.keybd_event(ctrl_vk, ctrl_scan, 0, 0)          # Ctrl 按下
    time.sleep(0.05)
    win32api.keybd_event(f10_vk, f10_scan, 0, 0)            # F10 按下
    time.sleep(0.05)
    win32api.keybd_event(f10_vk, f10_scan, win32con.KEYEVENTF_KEYUP, 0)  # F10 释放
    time.sleep(0.05)
    win32api.keybd_event(ctrl_vk, ctrl_scan, win32con.KEYEVENTF_KEYUP, 0)  # Ctrl 释放


def wait_visible(client: BridgeClient, expected: bool, timeout: float = 6.0) -> bool:
    deadline = time.time() + timeout
    visible = None
    while time.time() < deadline:
        visible = client.get_decision().get("visible")
        if visible == expected:
            return True
        time.sleep(0.4)
    return False


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except Exception:
            pass

    config = load_config()
    game_cfg = config.get("game", {})
    win = find_game_window(
        game_cfg.get("window_title_keywords", ["Probably Stolen"]),
        exclude_keywords=game_cfg.get("exclude_title_keywords"),
        min_size=tuple(game_cfg.get("min_window_size", (400, 300))),
    )
    if win is None:
        print("[失败] 未找到游戏窗口")
        return 1
    print(f"游戏窗口：{win.title!r} ({win.width}x{win.height})")

    client = BridgeClient()
    if not client.is_available():
        print("[失败] 桥接不可达（游戏需运行且插件已加载）")
        return 1

    before = client.get_decision().get("visible")
    print(f"切换前 visible={before}")

    active = controller._activate(win)
    print("窗口已激活，发送 Ctrl+F10（隐藏）...")
    send_ctrl_f10()
    if not wait_visible(client, not bool(before)):
        print(f"[失败] 切换后 visible 未变为 {not before}")
        return 1
    print(f"[通过] 已切换为 visible={not before}")

    active = controller._activate(active)
    print("再次发送 Ctrl+F10（恢复）...")
    send_ctrl_f10()
    if not wait_visible(client, bool(before)):
        print(f"[失败] 恢复后 visible 未变为 {before}")
        return 1
    print(f"[通过] 已恢复为 visible={before}")

    print("\n浮层显隐切换测试全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
