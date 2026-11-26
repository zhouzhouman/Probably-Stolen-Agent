"""控制模块：鼠标、键盘与窗口激活的高层封装。

对上层（agent_loop / brain）主要暴露 controller 中的函数：
    click_image / move_image / press_key / type_text / save_click_preview

坐标系约定（贯穿整个项目）：
- 截图坐标：与 artifacts/screenshots 中 PNG 的像素坐标一致（原点 = 窗口左上角、
  包含标题栏，因为 PrintWindow 抓取的是完整窗口）。视觉模型给出的 point 即此坐标系。
- 屏幕坐标：物理像素绝对坐标。执行时自动换算：屏幕坐标 = 窗口左上角 + 截图坐标。
"""

from game_agent.control.controller import (
    ControlResult,
    click_image,
    ctrl_click_image,
    move_image,
    press_key,
    save_click_preview,
    screenshot_to_screen,
    type_text,
    validate_point,
)

__all__ = [
    "ControlResult",
    "click_image",
    "ctrl_click_image",
    "move_image",
    "press_key",
    "type_text",
    "save_click_preview",
    "screenshot_to_screen",
    "validate_point",
]
