"""离线回放：把一次游戏会话的日志（+ 配对存档）重建为可读报告——无需启动游戏。

用法：
    python scripts/replay_session.py                        # 自动选最新日志
    python scripts/replay_session.py --log GameLog_2026-10-08_09-16-34.txt
    python scripts/replay_session.py --save save_15.es3     # 手动指定配对存档
    python scripts/replay_session.py --json                 # 机器可读输出

流程：
1. 定位游戏持久化目录（--data-dir 覆盖，默认为标准 Unity 路径）；
2. 选定日志文件（--log 指定，默认取最新），全量解析为事件流；
3. 重建顾客会话流水（到达时间 / 顾客类型 / 买卖意图 / 结果 / 离开）；
4. 选定配对存档（--save 指定；默认取与日志文件修改时间最接近的存档），
   解析状态快照（现金 / 经营天数 / 库存……）；
5. 打印报告。

该脚本演示了「数据直读」路线最大的优势之一：历史会话完全可离线复盘。
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from game_agent.statewatch import (  # noqa: E402
    find_latest_gamelog,
    list_saves,
    parse_log_lines,
    parse_save,
    rebuild_client_sessions,
    resolve_data_dir,
)


def pick_log(logs_dir: Path, name: str | None) -> Path | None:
    """选择日志文件：--log 指定（文件名或路径），否则取最新的 GameLog。"""
    if name:
        candidate = Path(name)
        if not candidate.is_absolute():
            candidate = logs_dir / candidate
        return candidate if candidate.is_file() else None
    return find_latest_gamelog(logs_dir)


def pick_paired_save(saves_dir: Path, log_path: Path, name: str | None):
    """选择配对存档：--save 指定，否则取与日志 mtime 最接近的存档。"""
    if name:
        candidate = Path(name)
        if not candidate.is_absolute():
            candidate = saves_dir / candidate
        return candidate if candidate.is_file() else None
    infos = list_saves(saves_dir)
    if not infos:
        return None
    log_mtime = log_path.stat().st_mtime
    return min(infos, key=lambda i: abs(i.mtime - log_mtime)).path


def build_report(log_path: Path, save_path: Path | None) -> dict:
    """解析日志与存档，产出结构化报告数据。"""
    lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    events = parse_log_lines(lines)
    sessions = rebuild_client_sessions(events)

    timestamps = [e.ts for e in events]
    by_kind = Counter(e.kind for e in events)
    by_client = Counter(s["client_id"] for s in sessions)
    intents = Counter(s["intent"] for s in sessions if s["intent"])

    report: dict = {
        "log_file": str(log_path),
        "time_range": [timestamps[0], timestamps[-1]] if timestamps else None,
        "events_total": len(events),
        "events_by_kind": dict(by_kind.most_common()),
        "client_sessions": sessions,
        "client_count": len(sessions),
        "client_types": dict(by_client.most_common()),
        "intent_distribution": dict(intents),
        "saves_in_session": by_kind.get("game_saved", 0),
    }

    if save_path is not None:
        snapshot = parse_save(save_path)
        report["paired_save"] = {
            "file": str(save_path),
            "slot": snapshot.slot,
            "cash": snapshot.cash,
            "day": snapshot.day,
            "day_until_rent": snapshot.day_until_rent,
            "rent_value": snapshot.rent_value,
            "is_power_on": snapshot.is_power_on,
            "happiness": snapshot.happiness,
            "order": snapshot.order,
            "traffic": snapshot.traffic,
            "inventory_counts": {k: len(v) for k, v in snapshot.inventories.items() if v},
            "reputations": snapshot.reputations,
        }
    return report


def print_report(report: dict) -> None:
    """人类可读报告输出。"""
    line = "=" * 64
    print(line)
    print(f"会话回放：{Path(report['log_file']).name}")
    if report["time_range"]:
        print(f"时间范围：{report['time_range'][0]} → {report['time_range'][1]}")
    print(f"事件总数：{report['events_total']}（自动保存 {report['saves_in_session']} 次）")
    print(line)

    sessions = report["client_sessions"]
    print(f"\n【顾客流水】共 {report['client_count']} 位"
          f"（卖 {report['intent_distribution'].get('sell', 0)} / "
          f"买 {report['intent_distribution'].get('buy', 0)}）")
    print(f"  {'到达':<9} {'顾客类型':<28} {'意图':<5} {'结果':<10} {'离开'}")
    for s in sessions:
        print(
            f"  {s['arrived']:<9} {str(s['client_id']):<28} "
            f"{str(s['intent'] or '?'):<5} {s['outcome']:<10} {s['left']}"
        )

    print("\n【顾客类型统计】")
    for name, count in report["client_types"].items():
        print(f"  {name:<32} {count}")

    print("\n【事件类型统计（前 12）】")
    for kind, count in list(report["events_by_kind"].items())[:12]:
        print(f"  {kind:<32} {count}")

    if "paired_save" in report:
        snap = report["paired_save"]
        print(f"\n【配对存档】{Path(snap['file']).name}（槽位 {snap['slot']}）")
        print(f"  现金 {snap['cash']} ｜ 经营天数 {snap['day']} ｜ "
              f"租金 {snap['day_until_rent']} 天后 {snap['rent_value']} ｜ "
              f"电力 {'开' if snap['is_power_on'] else '关'}")
        print(f"  客流 {snap['traffic']} ｜ 秩序 {snap['order']} ｜ 幸福 {snap['happiness']}")
        print(f"  库存件数：{snap['inventory_counts']}")
        rep_text = " ｜ ".join(
            f"{r['faction']}:{r['amount']}" for r in snap["reputations"] if r.get("faction")
        )
        if rep_text:
            print(f"  阵营声望：{rep_text}")
    else:
        print("\n【配对存档】未找到")
    print()


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except Exception:
            pass

    parser = argparse.ArgumentParser(description="离线回放一次游戏会话（日志 + 存档）")
    parser.add_argument("--data-dir", help="游戏持久化目录（默认自动推导）")
    parser.add_argument("--log", help="日志文件名或路径（默认取最新）")
    parser.add_argument("--save", help="配对存档文件名或路径（默认按 mtime 就近匹配）")
    parser.add_argument("--json", action="store_true", help="输出 JSON（不打印可读报告）")
    args = parser.parse_args(argv)

    data_dir = resolve_data_dir(override=args.data_dir)
    if data_dir is None or not data_dir.is_dir():
        print(f"未找到游戏数据目录：{data_dir}", file=sys.stderr)
        return 1

    log_path = pick_log(data_dir, args.log)
    if log_path is None:
        print(f"未找到日志文件（目录：{data_dir}）", file=sys.stderr)
        return 1

    save_path = pick_paired_save(data_dir, log_path, args.save)
    report = build_report(log_path, save_path)

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print_report(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
