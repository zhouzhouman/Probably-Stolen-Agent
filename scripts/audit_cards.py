"""机制卡质量审计：找出「看着有卡、实际永远不会被检索到」的坏卡。

用法::

    python scripts/audit_cards.py                 # 全量审计
    python scripts/audit_cards.py --strict        # 有严重问题时退出码 1

审计项
------
1. 触发路径：``triggers`` 里的 ``field`` 是否真的出现在观察结构里（对照
   ``knowledge/wiki_raw/_observation_fields.txt``）；拼错字段名的卡片永远不会命中。
2. 可检索性：既没有 triggers 又没有 keywords 的卡 = 死卡。
3. 重复：title 归一化后相同，或 facts 高度重合。
4. 低置信：confidence < 0.7 的卡需要人工复核。
5. 关键词泛化：单字/双字泛词（价格、顾客、服务）会污染检索。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from game_agent.knowledge.cards import MechanicCard, load_cards  # noqa: E402

SEMANTIC_THRESHOLD = 0.6

GENERIC = {
    "价格", "顾客", "服务", "库存", "现金", "交易", "商店", "物品", "钱", "时间",
    "白天", "晚上", "客人", "赚钱", "买入", "卖出", "上架", "营业", "经营", "货物",
}


def _known_paths() -> set[str]:
    path = _ROOT / "knowledge" / "wiki_raw" / "_observation_fields.txt"
    if not path.exists():
        return set()
    known: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("visible_info"):
            token = line.split(" = ")[0].split("  <")[0].strip()
            if token:
                known.add(token)
                # 列表元素路径归一化：visible_info.services[0].id / …[].id
                # 都归一到 visible_info.services.id，供卡片直接写 […].id
                known.add(token.replace("[]", "").replace("[0]", ""))
    return known


def _title_key(title: str) -> str:
    return re.sub(r"[\s（）()·、，,。：:—\-]+", "", title).lower()


def main() -> int:
    parser = argparse.ArgumentParser(description="机制卡质量审计")
    parser.add_argument("--dir", default="knowledge/mechanics")
    parser.add_argument("--strict", action="store_true")
    args = parser.parse_args()

    cards = load_cards(_ROOT / args.dir)
    known = _known_paths()
    print(f"知识库：{len(cards)} 张卡，{len(known)} 条已知观察路径\n")

    counter = Counter(c.category for c in cards.values())
    print("分类覆盖：", "  ".join(f"{k}={v}" for k, v in sorted(counter.items())))

    dead: list[str] = []
    bad_path: dict[str, list[str]] = defaultdict(list)
    generic_kw: list[tuple[str, str]] = []
    low_conf: list[str] = []
    no_facts_number: list[str] = []

    for card in cards.values():
        if not card.triggers and not card.keywords:
            dead.append(card.id)
        for rule in (card.triggers or {}).get("all") or []:
            field = str(rule.get("field") or "")
            norm = field.replace("[]", "")
            if known and norm not in known:
                # 父路径是已登记的列表/对象时放行（列表元素字段无法穷举）
                parent = norm.rsplit(".", 1)[0]
                if parent not in known and parent + "[]" not in known:
                    bad_path[card.id].append(field)
        for rule in (card.triggers or {}).get("any") or []:
            field = str(rule.get("field") or "")
            norm = field.replace("[]", "")
            if known and norm not in known:
                # 父路径是已登记的列表/对象时放行（列表元素字段无法穷举）
                parent = norm.rsplit(".", 1)[0]
                if parent not in known and parent + "[]" not in known:
                    bad_path[card.id].append(field)
        for kw in card.keywords:
            if kw in GENERIC or len(kw) <= 1:
                generic_kw.append((card.id, kw))
        if card.confidence < 0.7:
            low_conf.append(f"{card.id}({card.confidence})")
        if not any(re.search(r"\d", fact) for fact in card.facts):
            no_facts_number.append(card.id)

    # 语义重复：同类别 facts 的二字符组 Jaccard 相似度
    def _grams(text: str) -> set[str]:
        t = re.sub(r"\s+", "", text)
        return {t[i:i + 2] for i in range(len(t) - 1)}

    sig = {cid: _grams(" ".join(c.facts)) for cid, c in cards.items()}
    near: list[str] = []
    ids = sorted(cards)
    for i, left in enumerate(ids):
        for right in ids[i + 1:]:
            if cards[left].category != cards[right].category:
                continue
            a, b = sig[left], sig[right]
            if not a or not b:
                continue
            score = len(a & b) / len(a | b)
            if score >= SEMANTIC_THRESHOLD:
                near.append(f"{left} ≈ {right}（{score:.2f}）")

    # 重复：标题归一化 / facts 首条重合
    by_title: dict[str, list[str]] = defaultdict(list)
    by_fact: dict[str, list[str]] = defaultdict(list)
    for card in cards.values():
        by_title[_title_key(card.title)].append(card.id)
        if card.facts:
            by_fact[card.facts[0][:24]].append(card.id)
    dup_title = {k: v for k, v in by_title.items() if len(v) > 1}
    dup_fact = {k: v for k, v in by_fact.items() if len(v) > 1}

    def section(name: str, rows: list[str], limit: int = 25) -> None:
        print(f"\n【{name}】{len(rows)}")
        for row in rows[:limit]:
            print("  -", row)
        if len(rows) > limit:
            print(f"  … 另有 {len(rows) - limit} 条")

    section("死卡（无触发器且无关键词）", dead)
    section("触发路径不存在", [f"{cid}: {', '.join(set(f))}" for cid, f in bad_path.items()])
    section("泛化关键词", [f"{cid}: {kw}" for cid, kw in generic_kw])
    section("低置信（<0.7）", low_conf)
    section("facts 无任何数值", no_facts_number)
    section("标题重复", [f"{', '.join(v)}" for v in dup_title.values()])
    section("语义重复（同类别 facts 相似度 ≥0.6）", near)
    money_only = []
    for cid, card in cards.items():
        rules = (card.triggers or {}).get("all", []) + (card.triggers or {}).get("any", [])
        if rules and all(r.get("field") == "visible_info.money" for r in rules):
            money_only.append(cid)
    section("仅用金钱阈值触发（会长期命中）", money_only)

    numeric_kw = []
    for cid, card in cards.items():
        for kw in card.keywords:
            if re.fullmatch(r"[\d\s.,:%+\-×xX/]+", str(kw)):
                numeric_kw.append(f"{cid}: {kw}")
    section("纯数字关键词（已不参与匹配，建议删除）", numeric_kw)

    no_effect = [cid for cid, c in cards.items() if not c.effects]
    print(f"\n【无 effects（无法做事后核对）】{len(no_effect)}（多为阈值/知识型卡片，可接受）")
    section("首条事实重复", [f"{', '.join(v)}" for v in dup_fact.values()])

    serious = len(dead) + len(bad_path) + len(dup_title)
    print(f"\n合计：严重问题 {serious} 项（死卡 {len(dead)} / 坏路径 {len(bad_path)} / 重名 {len(dup_title)}）")
    if args.strict and serious:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
