"""从机制卡生成《机制知识总纲》。

用法::

    python scripts/gen_knowledge_doc.py                 # 写 docs/13-维基机制知识总纲.md
    python scripts/gen_knowledge_doc.py --check         # 只校验能否生成（不写文件）

为什么用生成而不是手写
----------------------
知识库的机制卡每张都会随实盘校准变化。手写总纲必然与卡片漂移，
所以正文（分类目录、覆盖矩阵、关键卡速查）全部由卡片渲染，只有
前言里的「组织方式 / 调用链」是固定文案。

`--check` 不比较「生成日期」那一行：日期每天都会变，若把它纳入比较，
产物会在跨天后永久报「漂移」，与卡片是否同步无关（2026-11-13 实证：
唯一差异就是日期戳导致 V1 长期红）。
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from game_agent.knowledge.cards import MechanicCard, load_cards  # noqa: E402

CATEGORY_NAMES = {
    "loop": "日循环与阶段",
    "trade": "交易与议价",
    "customer": "顾客与派系来客",
    "economy": "现金、租金与贷款",
    "security": "治安、盘查与许可",
    "night": "夜晚、外出与拾荒",
    "phone": "电话与供应商",
    "quest": "委托、远征与雇工",
    "production": "生产、养殖与模组",
    "store": "店铺、展柜与扩建",
    "risk": "违禁、赃物与犯罪",
    "meta": "模式、职业与结局",
}

# 十条「看完就能改变下一步动作」的卡
KEY_CARDS = [
    "rent-increase-rate",
    "budget-formula",
    "wholesale-batch-deal",
    "nego-four-directions",
    "insurance-fee-formula",
    "night-theft-threshold",
    "scavenge-attempt-cap",
    "fish-products-value",
    "inspection-severity-ladder",
    "gun-permit-tiers",
    "crime-ledger-entry",
    "market-event-weights",
]

PREFACE = """# 深空当铺 · 机制知识总纲

> 本文由 `scripts/gen_knowledge_doc.py` 从 `knowledge/mechanics/` 的机制卡渲染而成，
> 与知识库同源同版本；改卡片后重跑即可同步，不要手改正文。

## 一、这套知识是怎么组织的

游戏维基（`probably-stolen.nicokobo.com/zh/wiki`）共抓取 **{WIKI_PAGES} 个 wiki 页面 + {GUIDE_PAGES} 篇攻略**。抓取后分三层落地：

| 层 | 位置 | 作用 | 规模 |
|---|---|---|---|
| L0 原始摘要 | `knowledge/wiki_raw/*.md` | 逐页保留原文要点、表格、公式，可审计 | {RAW_FILES} 份 / 约 {RAW_KB}KB |
| L1 机制卡 | `knowledge/mechanics/<category>/<id>.json` | 一张卡 = 一个可决策单元：触发条件 / 事实 / 该做 / 禁忌 / 后果 / 证据 | 见下方统计 |
| L2 检索器 | `game_agent/knowledge/` | 按当前局面挑 1~3 张卡，注入提示词 | 打分 + 类别去重 |

**为什么不把所有机制写进提示词**：静态机制说明曾经是 13.5KB / 284 行线性文本，
关键规则埋在中段（「批发是买入」写在里面，AI 照样点错、实盘净亏 62）。
拆成卡片后，每步只带当前相关的 1~3 张（约 1KB），既省 token 又能保证「此刻该看的那条」在注意力最近处。

## 二、一张卡包含什么

| 字段 | 回答的问题 |
|---|---|
| `facts` | 这条机制的规则与数值是什么（带证据） |
| `actions` | 此刻该做什么（可执行动作或中文指令） |
| `pitfalls` | 做错会怎样（反例 + 后果） |
| `effects` | 做完后状态怎么变（用于事后核对现金/库存差分） |
| `guards` | 前置条件（现金/声望/许可/容量） |
| `triggers` | 什么局面下该被检索出来（对 `/state` 求值的谓词） |
| `keywords` | 触发器覆盖不到时的兜底（只放专有名词） |
| `related` | 关联机制（同一条链上的其它卡） |

## 三、AI 在决策时怎么用这些卡

```
/state ──► 观察层(build_observation) ──► 检索器(MechanicRouter)
                                            │  触发器×2 + 关键词 + 置信×0.5
                                            │  同类别只取一张（避免日循环卡挤掉交易卡）
                                            ▼
                                 提示词【机制知识】段（≤3 张 / ≤1800 字）
                                            │
                                     LLM 产出动作 JSON
                                            │
                              校验层（白名单 + 8 个硬守卫 + 卡片 guards）
                                            │
                                        执行 ──► 用 effects 核对现金/库存差分
```

四个关键设计：

1. **触发为主、关键词为辅**：关键词只匹配观察里的**值**，不匹配字段名——
   否则 `rent_due` 这类字段名会让租金卡常驻。
2. **置信度只做同分微调**（×0.5）：否则「泛词命中 + 高置信」会压过真正的局面信号。
3. **同类别去重**：{CARD_COUNT} 张卡里 `loop` 在任何营业/打烊局面都会触发，不去重会挤掉真正该看的卡。
4. **执行后核对**：卡片的 `effects` 用来判断动作是否真生效——本项目只认现金/库存差分，
   不认 `ok=true`（存在「返回成功但零成交」的动作）。

## 四、怎么用这套知识（命令）

```bash
python -m game_agent.cli knowledge --list                 # 全部卡片与分类覆盖
python -m game_agent.cli knowledge --id rent-increase-rate  # 看单卡全文
python -m game_agent.cli knowledge --select               # 用当前局面检索（需游戏在跑）
python -m game_agent.cli knowledge --validate             # 校验卡片
python scripts/audit_cards.py                             # 质量审计（死卡/坏路径/重复）
python scripts/gen_knowledge_doc.py                       # 重新生成本文
```
"""


def _fmt_related(card: MechanicCard, by_id: dict[str, MechanicCard]) -> str:
    rows = []
    for rid in card.related[:6]:
        other = by_id.get(rid)
        rows.append(f"{other.title}（{rid}）" if other else rid)
    return "、".join(rows)


def _badge(card: MechanicCard) -> str:
    """版本可信度徽标（空串表示无标记，不输出）。

    字段由 game_agent/knowledge/cards.py 的 MechanicCard 提供；这里用 getattr 读取，
    使得 cards.py 尚未加字段时生成器仍可运行（只是不输出徽标），不会因字段缺失崩溃。
    """
    if getattr(card, "deprecated", False):
        reason = getattr(card, "deprecated_reason", "") or "未说明理由"
        return f"🚫 已废弃：{reason}"
    if getattr(card, "gated_until", ""):
        return f"⏳ 正式版未实装（{card.gated_until}）"
    if getattr(card, "needs_verify", False):
        basis = getattr(card, "verify_basis", "") or "未知来源"
        return f"⚠️ 未核实（依据 {basis}）"
    version = getattr(card, "game_version", "")
    if version == "1.0.0":
        basis = getattr(card, "verify_basis", "") or "未知来源"
        return f"✅ 已按 1.0 核实（{basis}）"
    return ""


def _badge_line(card: MechanicCard) -> str:
    """返回可直接 append 的一整行；无标记时返回空串。"""
    mark = _badge(card)
    return f"- 版本：{mark}" if mark else ""


def _card_block(card: MechanicCard, *, facts: int = 3) -> list[str]:
    lines = [f"#### {card.title}", f"`{card.id}` · {CATEGORY_NAMES.get(card.category, card.category)} · 置信 {card.confidence:.2f}"]
    mark_line = _badge_line(card)
    if mark_line:
        lines.append(mark_line)
    if card.summary:
        lines.append(f"- **一句话**：{card.summary}")
    for fact in card.facts[:facts]:
        lines.append(f"- {fact}")
    if card.pitfalls:
        lines.append(f"- **禁忌**：{card.pitfalls[0]}")
    if card.related:
        lines.append(f"- **关联**：{'、'.join(card.related[:5])}")
    lines.append("")
    return lines


_DATE_STAMP_RE = re.compile(r"^生成日期：.*$", re.MULTILINE)


def _strip_date_stamp(text: str) -> str:
    """抹掉「生成日期：…」整行，供 --check 比较用（日期每天变，不代表漂移）。"""
    return _DATE_STAMP_RE.sub("生成日期：<不比较>", text)


def main() -> int:
    parser = argparse.ArgumentParser(description="生成机制知识总纲")
    parser.add_argument("--dir", default="knowledge/mechanics")
    parser.add_argument("--out", default="docs/13-维基机制知识总纲.md")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()

    url_file = _ROOT / "knowledge" / "wiki_raw" / "_fetched_urls.txt"
    urls = url_file.read_text(encoding="utf-8").splitlines() if url_file.exists() else []
    wiki_pages = sum(1 for u in urls if u.startswith("/zh/wiki/"))
    guide_pages = sum(1 for u in urls if u.startswith("/zh/guides/"))

    raw_dir = _ROOT / "knowledge" / "wiki_raw"
    raw_files = sorted(raw_dir.glob("[0-9][0-9]-*.md")) if raw_dir.exists() else []
    raw_kb = sum(p.stat().st_size for p in raw_files) // 1024 if raw_files else 0

    cards = load_cards(_ROOT / args.dir)
    if not cards:
        print("[错误] 知识库为空")
        return 1
    by_cat: dict[str, list[MechanicCard]] = defaultdict(list)
    for card in cards.values():
        by_cat[card.category].append(card)
    for rows in by_cat.values():
        rows.sort(key=lambda c: (-c.confidence, c.id))

    counter = Counter(card.category for card in cards.values())
    no_trigger = [c for c in cards.values() if not c.triggers]
    no_keyword = [c for c in cards.values() if not c.keywords]
    thin = [cat for cat in CATEGORY_NAMES if counter.get(cat, 0) < 5]

    out: list[str] = [PREFACE.replace("{WIKI_PAGES}", str(wiki_pages))
                              .replace("{GUIDE_PAGES}", str(guide_pages))
                              .replace("{RAW_FILES}", str(len(raw_files)))
                              .replace("{RAW_KB}", str(raw_kb))
                              .replace("{CARD_COUNT}", str(len(cards)))]
    out.append("\n## 五、规模与覆盖\n")
    out.append(f"- 机制卡：**{len(cards)} 张**，覆盖 **{len([c for c in counter if counter[c]])}/12** 个分类")
    out.append(f"- 带触发器（能按局面自动命中）：{len(cards) - len(no_trigger)} 张；"
               f"仅靠关键词兜底：{len(no_trigger)} 张")
    out.append(f"- 无关键词（永远不会被检索）：{len(no_keyword)} 张")
    out.append(f"- 分类薄弱（<5 张）：{'、'.join(f'{c}({counter.get(c,0)})' for c in thin) or '无'}")

    # 版本可信度分布：让人翻到这一节就能看出「哪部分数据能信、哪部分只是快照」
    all_cards = list(cards.values())
    n_dep = sum(1 for c in all_cards if getattr(c, "deprecated", False))
    n_gated = sum(1 for c in all_cards if getattr(c, "gated_until", ""))
    n_need = sum(1 for c in all_cards if getattr(c, "needs_verify", False))
    n_ver = Counter(getattr(c, "game_version", "") for c in all_cards)
    n_basis = Counter(getattr(c, "verify_basis", "") for c in all_cards)
    out.append("\n**版本可信度分布**（v1.0 校正后新增，用于快速判断哪部分数据可依据）：\n")
    out.append("| 标记 | 张数 | 含义 |")
    out.append("|---|---:|---|")
    out.append(f"| `game_version=1.0.0` | {n_ver.get('1.0.0', 0)} | 已按正式版实证 |")
    out.append(f"| `game_version=demo` | {n_ver.get('demo', 0)} | 事实来自发售前 Demo 快照，正式版可能已变 |")
    out.append(f"| `game_version` 空 | {n_ver.get('', 0)} | **无版本证据**（不等于错，等于不知道） |")
    out.append(f"| `needs_verify=true` | {n_need} | 明确标注待实机核实 |")
    out.append(f"| `gated_until=post-launch` | {n_gated} | 官方确认未来内容，运行时不得使用 |")
    out.append(f"| `deprecated=true` | {n_dep} | 结论已废止，理由见卡片 |")
    if n_basis:
        basis_desc = "、".join(f"{k or '(空)'} {v} 张" for k, v in sorted(n_basis.items(), key=lambda kv: -kv[1]))
        out.append(f"\n证据层级分布：{basis_desc}")
        out.append("（`live_state` = 1.0 实机 `/state` 快照；`decompile` = 正式版程序集解包；"
                   "`wiki_snapshot` = 2026-10-24 维基快照）")

    out.append("\n| 分类 | 张数 | 代表机制 |")
    out.append("|---|---:|---|")
    for cat, name in CATEGORY_NAMES.items():
        rows = by_cat.get(cat) or []
        examples = "、".join(c.title for c in rows[:3]) or "—"
        out.append(f"| {name}（`{cat}`） | {counter.get(cat, 0)} | {examples} |")

    out.append("\n## 六、十二条最影响决策的机制\n")
    out.append("> 每条都直接改变下一步动作；数值以卡片为准，点击 id 可在 `knowledge/mechanics/` 查到全文。\n")
    for index, cid in enumerate(KEY_CARDS, start=1):
        card = cards.get(cid)
        if not card:
            continue
        out.append(f"### {index}. {card.title}")
        out.append(f"`{cid}` · 置信 {card.confidence:.2f}")
        mark_line = _badge_line(card)
        if mark_line:
            out.append(mark_line)
        if card.summary:
            out.append(f"- **结论**：{card.summary}")
        for fact in card.facts[:3]:
            out.append(f"- {fact}")
        if card.actions:
            out.append(f"- **该做**：{card.actions[0]}")
        if card.pitfalls:
            out.append(f"- **禁忌**：{card.pitfalls[0]}")
        out.append("")

    out.append("\n## 七、机制 → 接口对照（v2.5）\n")
    out.append("每条机制都能对应一个**玩家操作**。三级分类：UI 级（点按钮/对话/拖拽）> "
               "库存 API 级（容器间搬运，走接受校验）> 字段级（仅标记，不移动物品）。"
               "完整对照见 `docs/16-玩家操作接口对照表.md`。\n")
    out.append("| 机制面 | 接口 | 玩家操作 | 状态 |")
    out.append("|---|---|---|---|")
    out.append("| 远征 / 雇工 | `GET /expedition`、`expedition_send`、`hire_worker` | 夜间面板选点选人 | 已接门禁：未解锁如实拒绝（本存档 `unlocked=false`） |")
    out.append("| 机器与配方 | `GET /machines`、`machine_put`、`machine_take` | 打开机器投料/取产 | 库存 API 级，可用；拖拽迁移待做 |")
    out.append("| 彩票 | `dialog_choose {text}` | 与代表对话选号 | 选号接口就绪，等代表到场 |")
    out.append("| 治疗与戒瘾 | `night_service`、`dialog_choose {text}` | 夜间服务界面 / 医生对话 | 已解锁项可用 |")
    out.append("| 议价方向 | `open_negociation` + `object_action`/`ui_click` | 点谈判面板按钮 | 面板自绘，需坐标（`ui_hit` 先探） |")
    out.append("| 贿赂 | `dialog_choose {text}` | 盘查对话选「打点」分支 | 接口就绪，需盘查在场 |")
    out.append("| 许可购买 | 网络面板 | 平板电脑点条目 | 面板自绘，需坐标；未接 |")
    out.append("")
    out.append("**通用输入层（v2.5 新增）**：`ui_hit`（射线查这一点归谁管）、`ui_click`（派发 "
               "pointerDown/Up/Click，等价玩家点击）、`object_action`（对具名对象 click / double_click / "
               "right_click）、`/ui/objects`（全量可点击清单，从 26 类白名单改为按接口全扫）。\n")
    out.append("**明确不做**：远征派遣、雇工招募、许可购买涉及内部方法直调，一律改为「未解锁就拒绝」，"
               "不用 `CreateExpedition` / `Hireling.Hire` 绕面板校验。\n")

    # 抓取范围与排除项（避免「以为覆盖全了」）
    groups: dict[str, int] = {}
    for url in urls:
        parts = url.strip("/").split("/")
        if parts[:2] == ["zh", "wiki"] and len(parts) > 2:
            key = parts[2]
        elif parts[:2] == ["zh", "guides"]:
            key = "guides"
        elif parts[:2] == ["zh", "items"]:
            key = "items"
        else:
            key = parts[1] if len(parts) > 1 else url
        groups[key] = groups.get(key, 0) + 1
    out.append("\n## 八、抓取范围与排除项\n")
    out.append("| 来源 | 已抓页面 |")
    out.append("|---|---:|")
    for key, count in sorted(groups.items(), key=lambda kv: -kv[1]):
        out.append(f"| `{key}` | {count} |")
    out.append("\n**明确排除**（不是遗漏，是不属于「机制」或不属于本 Demo）：\n")
    out.append("- `terms/community-mod-*` 7 页：Mod 内容，非原版机制；")
    out.append("- 物品图鉴（`/zh/items/**`，数百条）与 collections：数据表，不是机制；")
    out.append("- 10 个非 Demo 职业页：Demo 只开 4 个（学徒 / 养鼠人 / Rock Bottom / 水商），其余无游戏内效果；")
    out.append("- Mod 安装 / Steamcommunity / 投稿类攻略：与玩法无关。")
    out.append("\n已覆盖但**代码还做不到**的机制见第七节；只有关键词能命中的卡片是「观测字段缺失」，不是知识缺失。\n")

    out.append("\n## 九、全量卡片目录\n")
    for cat, name in CATEGORY_NAMES.items():
        rows = by_cat.get(cat) or []
        if not rows:
            continue
        out.append(f"### {name}（`{cat}`，{len(rows)} 张）\n")
        for card in rows:
            flag = "" if card.triggers else "（仅关键词）"
            mark = _badge(card)
            badged = f" — {mark}" if mark else ""
            out.append(f"- **{card.title}**{flag}{badged} `{card.id}`")
            if card.summary:
                out.append(f"  - {card.summary}")
            if card.related:
                out.append(f"  - 关联：{'、'.join(card.related[:4])}")
        out.append("")

    out.append("\n---\n")
    out.append(f"生成日期：{datetime.now().strftime('%Y-%m-%d')} ｜ "
               f"卡片 {len(cards)} 张 ｜ 生成器 `scripts/gen_knowledge_doc.py`\n")

    text = "\n".join(out)
    target = _ROOT / args.out
    if args.check:
        existing = target.read_text(encoding="utf-8") if target.exists() else ""
        # 只忽略「生成日期」这一行的日期戳：它每天不同，不反映卡片是否同步。
        if _strip_date_stamp(existing) != _strip_date_stamp(text):
            print(f"[漂移] {target} 与卡片不一致——请重跑 `python scripts/gen_knowledge_doc.py`")
            return 1
        print(f"[check] 已同步：{len(text)} 字符，{len(cards)} 张卡")
        return 0
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    print(f"[写入] {target}（{len(text)} 字符，{len(cards)} 张卡）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
