"""自检工具：创建一个内容已知、可交互的测试窗口，用于验证观察 + 控制全链路（无需启动游戏）。

功能：
- 六个彩色色块 + 标题文字：保证截图图像的亮度 / 颜色多样性正常；
- 点击反馈：被点击的位置会留下红色圆环标记（可多个），并显示最近一次点击的坐标；
- 按键反馈：显示最近一次按键的名称与字符。

这样即可验证完整闭环：「click 动作 → 窗口画面变化 → 截图确认」，
等价于真实游戏中「AI 点击按钮 → 界面切换 → 截图确认」的流程。

注意：需要自带 tkinter 的 Python 环境（如系统 Anaconda；托管 Python 精简版不含 tkinter）。
本脚本不依赖 game_agent 包。

用法：
    # 终端 1：打开测试窗口并保持运行
    C:\\anaconda\\python.exe scripts\\make_test_window.py

    # 终端 2：截图 / 点击 / 按键（点击后重新截图查看标记）
    python -m game_agent.cli capture --window GameAgent-TestWindow
    python -m game_agent.cli click --x 300 --y 250 --window GameAgent-TestWindow
    python -m game_agent.cli key --name a --window GameAgent-TestWindow
"""
from __future__ import annotations

import tkinter as tk

TITLE = "GameAgent-TestWindow"


def main() -> None:
    root = tk.Tk()
    root.title(TITLE)
    root.geometry("560x420+80+80")

    canvas = tk.Canvas(root, width=560, height=420, bg="#1e2430", highlightthickness=0)
    canvas.pack()

    # 顶部彩色色块：保证图像统计（亮度 / 颜色多样性）处于正常范围
    colors = ["#e74c3c", "#f39c12", "#2ecc71", "#3498db", "#9b59b6", "#1abc9c"]
    for index, color in enumerate(colors):
        canvas.create_rectangle(
            20 + index * 88, 20, 88 + index * 88, 110, fill=color, outline=""
        )

    canvas.create_text(
        280, 170, text="GameAgent 自检窗口",
        fill="#ffffff", font=("Microsoft YaHei", 18, "bold"),
    )
    canvas.create_text(
        280, 205, text="点击任意位置会留下标记；按键会显示在下方反馈区",
        fill="#bdc3c7", font=("Microsoft YaHei", 11),
    )
    # 操作反馈区（显示最近一次点击 / 按键）
    feedback = canvas.create_text(
        280, 320, text="最近操作：（等待输入）",
        fill="#f0c040", font=("Microsoft YaHei", 13, "bold"),
    )
    canvas.create_text(
        280, 380, text="TITLE: GameAgent-TestWindow",
        fill="#7f8c8d", font=("Consolas", 10),
    )

    def on_click(event) -> None:
        """在点击位置画标记（圆环 + 十字），并更新反馈文本。"""
        radius = 12
        canvas.create_oval(
            event.x - radius, event.y - radius, event.x + radius, event.y + radius,
            outline="#ff3c3c", width=3,
        )
        canvas.create_line(event.x - 18, event.y, event.x + 18, event.y, fill="#ff3c3c", width=2)
        canvas.create_line(event.x, event.y - 18, event.x, event.y + 18, fill="#ff3c3c", width=2)
        canvas.itemconfig(feedback, text=f"最近点击: ({event.x}, {event.y})")

    def on_key(event) -> None:
        """显示最近一次按键（名称 + 字符）。"""
        canvas.itemconfig(feedback, text=f"最近按键: {event.keysym}  字符={event.char!r}")

    root.bind("<Button-1>", on_click)
    root.bind("<Key>", on_key)
    root.focus_force()  # 让窗口一开始就有键盘焦点

    root.mainloop()


if __name__ == "__main__":
    main()
