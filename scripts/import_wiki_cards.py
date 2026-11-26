"""把 wiki 抓取产出的候选卡片导入知识库。

用法::

    python scripts/import_wiki_cards.py --dry-run      # 只校验，不落盘
    python scripts/import_wiki_cards.py                # 导入新卡片
    python scripts/import_wiki_cards.py --update       # 覆盖同 id 的旧卡
    python scripts/import_wiki_cards.py --report       # 打印覆盖矩阵

设计要点
--------
- 候选卡片放在 ``knowledge/wiki_cards/*.json``（每个文件一个数组）；
- 正式卡片按分类落到 ``knowledge/mechanics/<category>/<id>.json``；
- **默认不覆盖**：已存在的 id 跳过并计入 ``skipped``，避免自动抓取冲掉
  实盘校准过的数值（如 rent-lease 的 321/693）；
- 全部卡片先过 ``MechanicCard.from_dict`` 校验，任何一张不合法都不落盘
  （all-or-nothing），保证知识库不会半坏。
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from collections import Counter
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from game_agent.knowledge.cards import MechanicCard, MechanicError, load_cards  # noqa: E402


def _load_candidates(src: Path) -> tuple[list[dict[str, Any]], list[str]]:
    """读取候选卡片，返回 (卡片列表, 错误列表)。"""
    cards: list[dict[str, Any]] = []
    errors: list[str] = []
    for path in sorted(src.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            errors.append(f"{path.name}: JSON 解析失败：{exc}")
            continue
        if not isinstance(data, list):
            errors.append(f"{path.name}: 顶层必须是数组")
            continue
        for index, item in enumerate(data):
            if not isinstance(item, dict):
                errors.append(f"{path.name}[{index}]: 不是对象")
                continue
            item = dict(item)
            item.setdefault("source", f"wiki:{path.stem}")
            cards.append(item)
    return cards, errors


def _dedupe(cards: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
    """同 id 只保留第一条（先到先得），返回去重结果与被丢弃的 id。"""
    seen: dict[str, dict[str, Any]] = {}
    dropped: list[str] = []
    for card in cards:
        cid = str(card.get("id") or "")
        if cid in seen:
            dropped.append(cid)
            continue
        seen[cid] = card
    return list(seen.values()), dropped


def main() -> int:
    parser = argparse.ArgumentParser(description="导入 wiki 候选机制卡")
    parser.add_argument("--src", default="knowledge/wiki_cards", help="候选卡片目录")
    parser.add_argument("--dst", default="knowledge/mechanics", help="正式知识库目录")
    parser.add_argument("--dry-run", action="store_true", help="只校验不写入")
    parser.add_argument("--update", action="store_true", help="覆盖同 id 的已有卡片")
    parser.add_argument("--report", action="store_true", help="导入后打印覆盖矩阵")
    parser.add_argument("--force", action="store_true",
                        help="候选目录已冻结时仍强制导入（会复活已合并/删除的旧卡）")
    args = parser.parse_args()

    src = _ROOT / args.src
    dst = _ROOT / args.dst
    if not src.exists():
        print(f"[错误] 候选目录不存在：{src}")
        return 1
    if (src / ".frozen").exists() and not args.force:
        print("[已冻结] 候选目录只作存档；正式库是 knowledge/mechanics/。")
        print("         确需重新导入请加 --force（注意会复活已合并的旧卡）。")
        return 2

    raw_cards, errors = _load_candidates(src)
    if errors:
        print("[校验失败] 候选文件有问题：")
        for line in errors:
            print("  -", line)
        return 1

    cards, dropped = _dedupe(raw_cards)
    existing = load_cards(dst) if dst.exists() else {}

    valid: list[MechanicCard] = []
    invalid: list[str] = []
    skipped: list[str] = []
    for item in cards:
        cid = str(item.get("id") or "")
        try:
            card = MechanicCard.from_dict(item, origin=str(item.get("source") or ""))
        except MechanicError as exc:
            invalid.append(f"{cid}: {exc}")
            continue
        if cid in existing and not args.update:
            skipped.append(cid)
            continue
        valid.append(card)

    print(f"候选 {len(raw_cards)} 张 → 去重后 {len(cards)} 张"
          f"（重复 id {len(dropped)} 个）")
    if dropped:
        print("  重复 id：", ", ".join(sorted(set(dropped))[:12]))
    print(f"可导入 {len(valid)} 张 ｜ 已存在跳过 {len(skipped)} 张 ｜ 不合法 {len(invalid)} 张")
    if invalid:
        for line in invalid[:20]:
            print("  ✗", line)
        print("[中止] 存在不合法卡片，未写入任何文件。")
        return 1

    if args.dry_run:
        print("[dry-run] 未写入。")
    else:
        for card in valid:
            target = dst / card.category / f"{card.id}.json"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(
                json.dumps(card.to_dict(), ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        print(f"[写入] {len(valid)} 张 → {dst}")

    if args.report or not args.dry_run:
        all_cards = load_cards(dst)
        counter = Counter(card.category for card in all_cards.values())
        print(f"\n知识库共 {len(all_cards)} 张，分类覆盖：")
        for name, count in sorted(counter.items(), key=lambda kv: (-kv[1], kv[0])):
            print(f"  {name:12} {count:3d}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
