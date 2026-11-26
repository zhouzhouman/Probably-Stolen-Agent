"""知识卡跨版本复核工单（正式版迁移后）。

为什么需要它：194 张卡是从**试玩版 wiki** 抓的，其中不少数值（预算公式、掉落表、
服务定价、掉落概率）在正式版可能已改。这些只能逐卡人工核对，无法自动判定对错。

所以本脚本不"修"任何东西，只按**风险从高到低**排出需要人看的顺序：
  · 触发字段引用了正式版新增段（difficulty/mortgage/appraisal/aquaculture/afterhour_events）
    → 说明卡本身是老的，但战场变了，优先看；
  · 证据只有 wiki（没有 decompile 佐证）且 facts 里有具体数值 → 最可能过期；
  · verified_at 早于迁移日 2026-11-07 → 未在正式版复核过。

用法:
    python scripts/knowledge_staleness.py
    python scripts/knowledge_staleness.py --out artifacts/knowledge_review.md
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CARDS = ROOT / "knowledge" / "mechanics"
MIGRATION_DATE = "2026-11-07"

NEW_SEGMENTS = (
    "difficulty", "perks", "mortgage", "afterhour_events",
    "locations", "appraisal", "aquaculture", "gun_order",
)

NUM_RE = re.compile(r"\d")


def trigger_paths(card: dict) -> list[str]:
    out: list[str] = []

    def walk(node) -> None:
        if isinstance(node, dict):
            f = node.get("field")
            if isinstance(f, str):
                out.append(f)
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(card.get("triggers") or {})
    return out


def score(card: dict) -> tuple[int, list[str]]:
    """返回 (风险分, 理由列表)。分数越大越该先看。"""
    reasons: list[str] = []
    pts = 0

    facts_text = " ".join(str(x) for x in (card.get("facts") or []))
    has_numbers = bool(NUM_RE.search(facts_text))
    evidence = " ".join(str(x) for x in (card.get("evidence") or []))
    wiki_only = "wiki:" in evidence and "decompile" not in evidence

    if has_numbers and wiki_only:
        pts += 3
        reasons.append("wiki-only 且带具体数值（最可能过期）")
    elif has_numbers:
        pts += 1
        reasons.append("带具体数值")

    paths = trigger_paths(card)
    if any(any(seg in p for seg in NEW_SEGMENTS) for p in paths):
        pts += 2
        reasons.append("触发器已指向正式版新段（战场变了）")

    verified = str(card.get("verified_at") or "")
    if verified and verified < MIGRATION_DATE:
        pts += 2
        reasons.append(f"未在正式版复核（verified_at={verified}）")
    elif not verified:
        pts += 1
        reasons.append("没有 verified_at")

    if card.get("deprecated"):
        pts -= 5
        reasons.append("已标 deprecated（无需复核）")

    if card.get("effects"):
        pts -= 1  # 有事后可核对，风险略低

    return pts, reasons


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=None, help="输出 Markdown 工单路径")
    ap.add_argument("--top", type=int, default=40)
    args = ap.parse_args()

    cards = []
    for p in sorted(CARDS.rglob("*.json")):
        try:
            card = json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            print(f"× 解析失败 {p}: {e}")
            continue
        pts, reasons = score(card)
        cards.append((pts, card, reasons, p))

    cards.sort(key=lambda x: (-x[0], str(x[3])))
    total = len(cards)
    high = [c for c in cards if c[0] >= 4]
    mid = [c for c in cards if 2 <= c[0] < 4]

    print(f"知识卡总数 {total}｜高风险 {len(high)}｜中风险 {len(mid)}")
    print(f"\n=== 最高风险前 {min(args.top, len(cards))} 张 ===")
    for pts, card, reasons, _ in cards[: args.top]:
        print(f"[{pts:>2}] {card.get('id'):<34} {card.get('title')}")
        print(f"     {'；'.join(reasons) or '—'}")

    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        lines = [
            "# 知识卡跨版本复核工单",
            "",
            f"总卡数 {total}｜高风险 {len(high)}｜中风险 {len(mid)}｜"
            f"生成脚本 `scripts/knowledge_staleness.py`",
            "",
            "> 判定规则：wiki-only 且有具体数值 +3；触发器指向正式版新段 +2；"
            "verified_at 早于 2026-11-07 +2；无 verified_at +1；deprecated -5。",
            "",
            "| 分 | id | 标题 | 理由 |",
            "|---:|---|---|---|",
        ]
        for pts, card, reasons, _ in cards:
            lines.append(
                f"| {pts} | `{card.get('id')}` | {card.get('title')} | {'；'.join(reasons) or '—'} |"
            )
        out.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"\n工单已写入 {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
