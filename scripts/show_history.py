"""决策历史查看工具：打印最近 N 步的「观察界面 → 决策 → 执行结果」。

用法：
    python scripts/show_history.py [N]     # 默认显示最近 5 步
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    count = int(sys.argv[1]) if len(sys.argv) > 1 else 5
    steps_dir = PROJECT_ROOT / "artifacts" / "steps"
    files = sorted(steps_dir.glob("step_*.json"), key=os.path.getmtime)[-count:]
    if not files:
        print("暂无步骤记录")
        return

    for index, path in enumerate(files, start=1):
        data = json.loads(path.read_text(encoding="utf-8"))
        if "error" in data:
            print(f"[{index}] {path.name}  错误：{data['error']}")
            continue
        decision = data.get("decision") or {}
        execution = data.get("execution") or {}
        observation = data.get("observation") or {}
        live = "LIVE" if data.get("live") else "dry"
        print(f"[{index}] {path.name}  ({live})")
        print(f"    界面: {observation.get('screen_state')}   置信度: {observation.get('confidence')}")
        print(f"    决策: {decision.get('action')} {json.dumps(decision.get('params') or {}, ensure_ascii=False)}"
              f"  (confidence={decision.get('confidence')})")
        print(f"    理由: {decision.get('reason')}")
        print(f"    执行: {execution.get('detail')}")
        print()


if __name__ == "__main__":
    main()
