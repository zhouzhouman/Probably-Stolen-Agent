#!/usr/bin/env python
"""库存网格坐标校准工具（v0.4.2 配套）。

读取桥 /state 中每件物品的容器（inv）与网格坐标（gx, gy），
配合人工 ctrl_click 校准点，推算「格子坐标 → 截图像素坐标」换算公式。

用法：
    python scripts/calibrate_grid.py            # 打印当前物品的网格坐标表
    python scripts/calibrate_grid.py --verify   # 与已保存的换算参数对照打印像素坐标

校准流程：
1. 运行本脚本记录几件物品的 (gx, gy)；
2. 对其中一件物品用 `click --preview` 找到其像素中心（基准点）；
3. 把 (gx, gy) -> (px, py) 的两组以上对应关系代入线性式解出 base 与 cell；
4. 将公式写进 brain/prompt.py 的交易流程说明。
"""
from __future__ import annotations

import argparse
import json
import sys

sys.path.insert(0, ".")

from game_agent.bridge.client import make_bridge_client  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="库存网格坐标校准")
    parser.add_argument("--verify", action="store_true", help="按换算参数打印像素坐标")
    parser.add_argument("--base-x", type=float, default=714.0, help="gx=0 的截图像素 x")
    parser.add_argument("--base-y", type=float, default=810.0, help="gy=0 的截图像素 y")
    parser.add_argument("--cell-w", type=float, default=64.0, help="格子宽（像素）")
    parser.add_argument("--cell-h", type=float, default=64.0, help="格子高（像素）")
    args = parser.parse_args()

    state = make_bridge_client().get_state()
    inv = state.get("inventory") or {}
    rows = []
    seen = set()
    for key in ("owned", "not_owned"):
        for it in inv.get(key) or []:
            sig = (it.get("identifier"), it.get("inv"), it.get("gx"), it.get("gy"))
            if sig in seen:
                continue
            seen.add(sig)
            rows.append(it)

    print(f"{'identifier':<18} {'inv':<22} {'gx':>4} {'gy':>4}  name")
    for it in sorted(rows, key=lambda r: (str(r.get('inv')), r.get('gy') or 0, r.get('gx') or 0)):
        extra = ""
        if args.verify:
            px = args.base_x + (it.get("gx") or 0) * args.cell_w
            py = args.base_y - (it.get("gy") or 0) * args.cell_h
            extra = f"  -> 像素 ({px:.0f}, {py:.0f})"
        print(f"{str(it.get('identifier')):<18} {str(it.get('inv')):<22} "
              f"{str(it.get('gx')):>4} {str(it.get('gy')):>4}  {it.get('name')}{extra}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
