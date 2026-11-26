#!/usr/bin/env python3
"""汇总一次自主运行的观察数据：接口使用、机制命中、行为统计。

用法：
    python scripts/analyze_run.py --glob "artifacts/steps/step_20261025_2*.json" [--out artifacts/run_analysis.json]

从 step 文件提取：
- 接口调用统计（action × 次数 × 成功率）
- 机制卡命中（每步注入的知识卡）
- 顾客交互与交易明细（卖出/买入金额）
- 阶段流转（MORNING/OPEN/CLOSED/NIGHT）
- 决策深度（reason 是否含三段式）
"""
from __future__ import annotations

import argparse
import glob
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def load_steps(pattern: str) -> list[dict]:
    files = sorted(glob.glob(pattern))
    steps = []
    for f in files:
        try:
            d = json.loads(Path(f).read_text(encoding="utf-8"))
            d["_file"] = Path(f).name
            steps.append(d)
        except (json.JSONDecodeError, OSError) as exc:
            print(f"[跳过] {f}: {exc}", file=sys.stderr)
    return steps


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--glob", required=True)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    steps = load_steps(args.glob)
    if not steps:
        print("没有匹配的 step 文件")
        return 1

    actions: Counter = Counter()
    action_fail: Counter = Counter()
    cards: Counter = Counter()
    card_categories: Counter = Counter()
    sells: list[dict] = []
    buys: list[dict] = []
    phases: Counter = Counter()
    deep_reason = 0
    errors: list[dict] = []
    clients: Counter = Counter()
    cash_series: list[dict] = []
    night_actions: Counter = Counter()
    machine_actions: list[dict] = []

    for st in steps:
        dec = st.get("decision") or {}
        ex = st.get("execution") or {}
        vi = (st.get("observation") or {}).get("visible_info") or {}
        action = dec.get("action") or "?"
        ok = ex.get("ok")
        actions[action] += 1
        if ok is False:
            action_fail[action] += 1
            errors.append({
                "step": st.get("_file"),
                "action": action,
                "params": dec.get("params"),
                "error": str(ex.get("error") or ex.get("detail"))[:160],
            })
        reason = str(dec.get("reason") or "")
        if "【依据】" in reason and "【策略】" in reason:
            deep_reason += 1
        for cid in st.get("mechanics") or []:
            cards[cid] += 1
        phases[vi.get("store_state") or "?"] += 1
        cl = vi.get("client") or {}
        if cl.get("identifier"):
            clients[cl["identifier"]] += 1
        cash = vi.get("money")
        cash_series.append({"step": st.get("_file"), "phase": vi.get("store_state"), "cash": cash})
        detail = str(ex.get("detail") or "")
        if action == "sell_item" and ok:
            sells.append({"step": st.get("_file"), "item": (dec.get("params") or {}).get("identifier"), "detail": detail[:120]})
        if action in ("buy_item", "buy_table") and ok:
            buys.append({"step": st.get("_file"), "item": (dec.get("params") or {}).get("identifier"), "detail": detail[:120]})
        if vi.get("store_state") in ("CLOSED", "NIGHT") or (vi.get("night") or {}).get("is_outside"):
            night_actions[action] += 1
        if action in ("machine_put", "machine_take"):
            machine_actions.append({"step": st.get("_file"), "params": dec.get("params"), "detail": detail[:120]})

    # 机制卡分类
    try:
        from game_agent.knowledge.cards import load_cards
        all_cards = load_cards("knowledge/mechanics")
        for cid in cards:
            if cid in all_cards:
                card_categories[all_cards[cid].category] += cards[cid]
        total_cards = len(all_cards)
    except Exception as exc:  # 知识库不可用时降级
        print(f"[警告] 知识库加载失败: {exc}", file=sys.stderr)
        total_cards = 0

    cash0 = next((x["cash"] for x in cash_series if x["cash"] is not None), None)
    cash1 = next((x["cash"] for x in reversed(cash_series) if x["cash"] is not None), None)

    report = {
        "steps": len(steps),
        "deep_reason_ratio": round(deep_reason / len(steps), 3),
        "cash_start": cash0,
        "cash_end": cash1,
        "phases": dict(phases),
        "interfaces": {
            "used": dict(actions.most_common()),
            "failures": dict(action_fail),
            "unique": len(actions),
        },
        "clients": dict(clients.most_common()),
        "trades": {"sells": sells, "buys": buys, "sell_count": len(sells), "buy_count": len(buys)},
        "knowledge": {
            "cards_hit": dict(cards.most_common()),
            "by_category": dict(card_categories),
            "unique_cards": len(cards),
            "library_total": total_cards,
        },
        "night_actions": dict(night_actions),
        "machine_actions": machine_actions,
        "errors": errors,
        "cash_series_tail": cash_series[-12:],
    }

    text = json.dumps(report, ensure_ascii=False, indent=1)
    if args.out:
        Path(args.out).write_text(text + "\n", encoding="utf-8")
        print(f"[写入] {args.out}")
    print(f"步数 {len(steps)}  接口 {len(actions)} 种  深度思考 {report['deep_reason_ratio']*100:.0f}%  "
          f"现金 {cash0}→{cash1}  卖出 {len(sells)} 笔  机制卡 {len(cards)}/{total_cards}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
