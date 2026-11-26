"""键盘控制：基于 Win32 keybd_event 的按键与文本输入。

实现要点：
- 发送按键时携带真实扫描码（MapVirtualKey 转换）——部分游戏/框架（含 Tk）
  只认扫描码，只给虚拟键码会出现按键“无反应”或 keysym 无法识别的问题；
- 限制：type_text 仅支持 ASCII 可见字符（keybd_event 无法直接输入中文，
  中文输入需要 IME 配合，暂不支持）。
"""
from __future__ import annotations

import time

from game_agent.errors import ControlError

try:
    import win32api  # type: ignore
    import win32con  # type: ignore

    WIN32_AVAILABLE = True
except ImportError:  # pragma: no cover
    win32api = None  # type: ignore
    win32con = None  # type: ignore
    WIN32_AVAILABLE = False

# 常用按键名 -> 虚拟键码（不区分大小写）
VK_MAP: dict[str, int] = {
    "escape": 0x1B, "esc": 0x1B,
    "enter": 0x0D, "return": 0x0D,
    "space": 0x20, "tab": 0x09, "backspace": 0x08, "delete": 0x2E,
    "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
    "home": 0x24, "end": 0x23, "pageup": 0x21, "pagedown": 0x22,
    "shift": 0x10, "ctrl": 0x11, "alt": 0x12,
}
for _char in "abcdefghijklmnopqrstuvwxyz":
    VK_MAP[_char] = ord(_char.upper())
for _digit in "0123456789":
    VK_MAP[_digit] = ord(_digit)
for _index in range(1, 13):
    VK_MAP[f"f{_index}"] = 0x6F + _index

SHIFT_VK = 0x10


def _require_win32() -> None:
    if not WIN32_AVAILABLE:
        raise ControlError("键盘控制依赖 pywin32（仅支持 Windows）。请先执行：pip install pywin32")


def resolve_vk(key: str) -> int:
    """把按键名解析为虚拟键码；未知按键抛 ControlError。"""
    name = str(key).strip().lower()
    if name in VK_MAP:
        return VK_MAP[name]
    # 单字符回退：交给 VkKeyScan 处理符号（如 "," "-" "+" 等）
    if len(name) == 1 and name.isascii():
        vk_scan = win32api.VkKeyScan(name) if win32api else -1
        if vk_scan != -1:
            return int(vk_scan) & 0xFF
    known = ", ".join(sorted(VK_MAP.keys())[:16])
    raise ControlError(f"未知按键名：{key!r}（常用可用键：{known} ...）")


def _send_key(vk: int, scan: int, *, with_shift: bool = False) -> None:
    """按下并释放一个按键（可选 Shift 组合），携带真实扫描码。"""
    shift_scan = win32api.MapVirtualKey(SHIFT_VK, 0)  # MAPVK_VK_TO_VSC
    if with_shift:
        win32api.keybd_event(SHIFT_VK, shift_scan, 0, 0)
    win32api.keybd_event(vk, scan, 0, 0)
    time.sleep(0.02)
    win32api.keybd_event(vk, scan, win32con.KEYEVENTF_KEYUP, 0)
    if with_shift:
        win32api.keybd_event(SHIFT_VK, shift_scan, win32con.KEYEVENTF_KEYUP, 0)


def press(key: str, times: int = 1, interval: float = 0.06) -> None:
    """按下并释放一个按键，可重复多次。"""
    _require_win32()
    vk = resolve_vk(key)
    scan = win32api.MapVirtualKey(vk, 0)  # 虚拟键码 -> 扫描码
    for index in range(max(1, int(times))):
        _send_key(vk, scan)
        if index < times - 1:
            time.sleep(interval)


def type_text(text: str, interval: float = 0.04) -> None:
    """逐字符输入 ASCII 文本（不支持中文等非 ASCII 字符）。

    遇到非 ASCII 字符立即抛错（不会先输入一半再报错）。
    """
    _require_win32()
    for char in text:
        if not (char.isascii() and (char.isprintable() or char == " ")):
            raise ControlError(f"type_text 仅支持 ASCII 字符，收到：{char!r}（中文需输入法支持，暂不支持）")
    for char in text:
        vk_scan = win32api.VkKeyScan(char)
        vk = int(vk_scan) & 0xFF
        need_shift = bool((int(vk_scan) >> 8) & 1)
        scan = win32api.MapVirtualKey(vk, 0)
        _send_key(vk, scan, with_shift=need_shift)
        time.sleep(interval)
