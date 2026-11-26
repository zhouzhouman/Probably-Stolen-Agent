"""截图裁剪工具：裁剪截图指定区域并放大保存，便于核对 UI 细节（文字、图标等）。

用法：
    python scripts/crop_image.py <图片路径> <x> <y> <宽> <高> [--scale 2] [--out 输出路径]

输出默认保存为 <图片名>_crop.png（与原图同目录），并打印实际裁剪范围。
"""
from __future__ import annotations

import argparse
from pathlib import Path

from PIL import Image


def main() -> None:
    parser = argparse.ArgumentParser(description="裁剪截图区域并放大保存")
    parser.add_argument("image", help="源图片路径")
    parser.add_argument("x", type=int, help="裁剪起点 x")
    parser.add_argument("y", type=int, help="裁剪起点 y")
    parser.add_argument("width", type=int, help="裁剪宽度")
    parser.add_argument("height", type=int, help="裁剪高度")
    parser.add_argument("--scale", type=float, default=2.0, help="放大倍数（默认 2）")
    parser.add_argument("--out", help="输出路径（默认：<图片名>_crop.png）")
    args = parser.parse_args()

    image = Image.open(args.image).convert("RGB")
    # 越界坐标自动收敛到图像范围内
    left = max(0, min(args.x, image.width - 1))
    top = max(0, min(args.y, image.height - 1))
    right = max(left + 1, min(args.x + args.width, image.width))
    bottom = max(top + 1, min(args.y + args.height, image.height))

    crop = image.crop((left, top, right, bottom))
    if args.scale and args.scale != 1:
        crop = crop.resize(
            (int(crop.width * args.scale), int(crop.height * args.scale)),
            Image.Resampling.LANCZOS,
        )

    out_path = Path(args.out) if args.out else Path(args.image).with_name(f"{Path(args.image).stem}_crop.png")
    crop.save(out_path)
    print(str(out_path))
    print(f"source={image.size} crop=({left},{top})-({right},{bottom}) scale={args.scale}")


if __name__ == "__main__":
    main()
