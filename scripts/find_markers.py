"""红色标记检测工具：找出截图中所有红色十字/圆环标记的中心坐标。

用途：验证鼠标点击的落点是否与预期坐标一致（点击后自动复核的原型）。

用法（命令行）：
    python scripts/find_markers.py <图片路径>

也可作为模块导入使用：`from find_markers import find_markers`（返回标记列表）。
"""
from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image

# 标记颜色：测试窗口用 #ff3c3c（255, 60, 60）
RED_MIN, GREEN_MAX, BLUE_MAX = 200, 120, 120
# 像素采样步长（2 表示隔一个像素采一个，兼顾速度与精度）
SAMPLE_STEP = 2
# 聚类半径：距离小于该值的命中像素归为同一个标记
CLUSTER_RADIUS = 35


def find_markers(image_path: str | Path) -> list[tuple[int, int, int]]:
    """检测图像中的红色标记，返回 [(x, y, 命中像素数), ...]（按像素数降序）。"""
    image = Image.open(image_path).convert("RGB")
    width, height = image.size
    pixels = image.load()

    hits: list[tuple[int, int]] = []
    for y in range(0, height, SAMPLE_STEP):
        for x in range(0, width, SAMPLE_STEP):
            red, green, blue = pixels[x, y]
            if red > RED_MIN and green < GREEN_MAX and blue < BLUE_MAX:
                hits.append((x, y))

    clusters: list[dict[str, float]] = []
    for (x, y) in hits:
        placed = False
        for cluster in clusters:
            center_x = cluster["sum_x"] / cluster["count"]
            center_y = cluster["sum_y"] / cluster["count"]
            if abs(x - center_x) < CLUSTER_RADIUS and abs(y - center_y) < CLUSTER_RADIUS:
                cluster["sum_x"] += x
                cluster["sum_y"] += y
                cluster["count"] += 1
                placed = True
                break
        if not placed:
            clusters.append({"sum_x": x, "sum_y": y, "count": 1})

    markers = [
        (int(c["sum_x"] / c["count"]), int(c["sum_y"] / c["count"]), int(c["count"]))
        for c in clusters
        if c["count"] >= 3
    ]
    markers.sort(key=lambda m: -m[2])
    return markers


def main() -> None:
    if len(sys.argv) < 2:
        print("用法：python scripts/find_markers.py <图片路径>")
        raise SystemExit(2)
    markers = find_markers(sys.argv[1])
    print(f"markers={len(markers)}")
    for (x, y, count) in markers:
        print(f"  marker: ({x}, {y})  pixels={count}")


if __name__ == "__main__":
    main()
