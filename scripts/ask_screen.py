"""对游戏画面提问（视觉侦察工具）：截图 → 视觉模型 → 文本回答。

用法：
    python scripts/ask_screen.py "画面里有哪些可点击的按钮？"
    python scripts/ask_screen.py --file artifacts/screenshots/xxx.png "问题"
    python scripts/ask_screen.py --window Probably "问题"

说明：
- 默认截取游戏窗口最新画面（找不到窗口时可用 --file 指定已有截图）；
- 请求前图像会等比缩至最长边 <= 2048（当前游戏窗口 1942x1136 不会被缩放），
  因此模型给出的坐标基本等于「截图坐标系」（与 click 命令一致）；
- 本工具用于开发期侦察游戏 UI（找按钮位置 / 确认界面状态），不参与主流水线。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from game_agent.config import artifacts_dir, load_config  # noqa: E402
from game_agent.errors import AnalyzeError  # noqa: E402
from game_agent.vision.analyzer import make_client  # noqa: E402
from game_agent.vision.capture import capture_window  # noqa: E402
from game_agent.vision.window import find_game_window  # noqa: E402


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except Exception:
            pass

    parser = argparse.ArgumentParser(description="对游戏画面提问（视觉侦察工具）")
    parser.add_argument("question", help="要向视觉模型提出的问题")
    parser.add_argument("--file", help="使用指定截图文件（默认实时截取游戏窗口）")
    parser.add_argument("--window", help="窗口标题关键词（默认用配置中的游戏关键词）")
    args = parser.parse_args(argv)

    config = load_config()

    if args.file:
        image_path = Path(args.file)
        if not image_path.is_file():
            print(f"截图文件不存在：{image_path}", file=sys.stderr)
            return 1
    else:
        game_cfg = config.get("game", {})
        keywords = [args.window] if args.window else game_cfg.get("window_title_keywords", [])
        excludes = [] if args.window else game_cfg.get("exclude_title_keywords", [])
        win = find_game_window(keywords, excludes, tuple(game_cfg.get("min_window_size", (400, 300))))
        if win is None:
            print("未找到游戏窗口，可用 --file 指定已有截图", file=sys.stderr)
            return 1
        capture = capture_window(win, artifacts_dir(config) / "screenshots", tag="ask")
        image_path = Path(capture.image_path)

    client = make_client(config)
    scale_note = ""
    client_side = getattr(client, "max_image_side", None)
    if client_side:
        from PIL import Image
        with Image.open(image_path) as img:
            w, h = img.size
        if max(w, h) > client_side:
            factor = client_side / max(w, h)
            scale_note = (
                f"\n注意：发给你的图片已从原图 {w}x{h} 等比缩小到 "
                f"{int(w * factor)}x{int(h * factor)}，你给出的坐标请注明基于哪张图。"
            )
    prompt = f"这是游戏《深空当铺》(Probably Stolen) 的截图。{args.question}{scale_note}"
    print(f"[ask_screen] 截图：{image_path}")
    print(f"[ask_screen] 提问：{args.question}\n")
    try:
        answer = client.analyze(image_path, prompt)
    except AnalyzeError as exc:
        print(f"视觉分析失败：{exc}", file=sys.stderr)
        return 2
    print(answer)
    return 0


if __name__ == "__main__":
    sys.exit(main())
