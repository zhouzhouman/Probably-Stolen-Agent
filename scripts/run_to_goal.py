"""按商业目标连续跑批（实机）。

用途：无人值守地跑到商业目标达成——每批 N 步，批间检查 KPI；
达标、期限已满、出现 STOP 文件或连续两批异常即停。

用法（默认步数与批次数都不设上限）：
    python scripts/run_to_goal.py
    python scripts/run_to_goal.py --delay 0.8
    python scripts/run_to_goal.py --max-batches 12   # 想限制批次数时显式传

退出码：0 正常结束（达标/满期/STOP） | 1 连续异常中止
日志：artifacts/goal_run.jsonl（每批一行 JSON）+ 标准输出
"""
from __future__ import annotations

import argparse
import itertools
import json
import sys
import time
from datetime import datetime

from game_agent.agent_loop import run_loop
from game_agent.bridge.client import make_bridge_client
from game_agent.config import artifacts_dir, load_config
from game_agent.goal import BusinessGoal


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="按目标连续跑批（实机）")
    parser.add_argument("--steps", type=int, default=0,
                        help="每批步数（默认 0 = 不设上限；靠达标 / STOP / 模型 stop 结束）")
    parser.add_argument("--max-batches", type=int, default=0,
                        help="最多批次数（默认 0 = 不设上限；靠达标 / STOP / 连续异常结束）")
    parser.add_argument("--delay", type=float, default=0.3, help="步间隔秒数（默认 0.3）")
    parser.add_argument("--keep-going", action="store_true",
                        help="达标后继续跑（默认达标即停）")
    args = parser.parse_args(argv)

    config = load_config()
    goal = BusinessGoal(config)
    bridge = make_bridge_client(config)
    out_dir = artifacts_dir(config)
    stop_file = out_dir / "STOP"
    log_path = out_dir / "goal_run.jsonl"

    def emit(payload: dict) -> None:
        line = json.dumps(payload, ensure_ascii=False)
        print(line, flush=True)
        try:
            with log_path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except OSError:
            pass

    emit({"ts": _now(), "event": "start", "steps": args.steps,
          "max_batches": args.max_batches,
          "last": goal.last.to_dict() if goal.last else None})

    consecutive_errors = 0
    # 批次数 <= 0 视为不设上限：用 itertools.count 保留 batch 序号语义。
    batches = (range(1, args.max_batches + 1) if args.max_batches > 0
               else itertools.count(1))
    for batch in batches:
        if stop_file.exists():
            emit({"ts": _now(), "event": "stop_file", "batch": batch})
            break

        started = time.time()
        try:
            steps = run_loop(
                config,
                max_steps=args.steps,
                live=True,
                delay_between=args.delay,
                source="bridge",
                event_driven=True,
                push_overlay=True,
                allow_mouse=False,
                bridge_client=bridge,
                goal=goal,
            )
        except Exception as exc:  # noqa: BLE001 - 驱动器必须扛住单批异常
            consecutive_errors += 1
            emit({"ts": _now(), "event": "batch_error", "batch": batch,
                  "error": f"{type(exc).__name__}: {exc}"})
            if consecutive_errors >= 2:
                emit({"ts": _now(), "event": "abort", "reason": "连续两批异常"})
                return 1
            time.sleep(5)
            continue
        consecutive_errors = 0

        actions = [str((s.get("decision") or {}).get("action")) for s in steps]
        errors = sum(1 for s in steps if s.get("error"))
        progress = goal.last
        snapshot = progress.to_dict() if progress else None
        emit({
            "ts": _now(),
            "event": "batch",
            "batch": batch,
            "steps": len(steps),
            "errors": errors,
            "actions": actions,
            "duration_s": round(time.time() - started, 1),
            "progress": snapshot,
        })

        # 每两批存一次档（游戏换日也会自动存）
        if batch % 2 == 0:
            try:
                bridge.post_action("save_game")
            except Exception:
                pass

        if progress is not None:
            if progress.done and not args.keep_going:
                emit({"ts": _now(), "event": "goal_reached", "batch": batch,
                      "progress": snapshot})
                break
            if progress.elapsed_days >= progress.window_days:
                emit({"ts": _now(), "event": "window_closed", "batch": batch,
                      "progress": snapshot})
                break

    emit({"ts": _now(), "event": "end", "last": goal.last.to_dict() if goal.last else None})
    return 0


if __name__ == "__main__":
    sys.exit(main())
