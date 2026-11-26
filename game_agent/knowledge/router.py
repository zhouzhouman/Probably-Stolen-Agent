"""机制检索：按当前局面挑选最相关的机制卡，并渲染成提示词片段。

为什么不是「把所有机制都塞进提示词」
------------------------------------
现有 `BRIDGE_ACTION_TOOLS` 已经 13.5KB / 284 行，单步提示词约 37KB；再线性
堆叠几十个机制，既超预算又会让模型注意力稀释（长上下文里中段信息最容易被忽略）。
所以采用「常驻索引 + 按需检索」：

- 常驻：每个机制一行索引（可选，默认不开，节省 token）；
- 按需：每步最多 3 张完整卡片，按触发器（/state 谓词）+ 关键词（观察/动作/目标）打分；
- 渲染：事实 / 该做 / 禁忌 / 后果 四段，硬性截断到 max_chars。

检索是纯函数式的（不依赖网络/模型），可单测、可复现；卡片本身是数据，
运营时改 JSON 即可，不用改代码。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Iterable, Optional

from game_agent.knowledge.cards import MechanicCard

__all__ = ["MechanicRouter", "render_cards", "summarize_coverage"]

_SECTION_LIMITS = {"facts": 3, "actions": 2, "pitfalls": 2, "effects": 2}
_LINE_CHARS = 96


@dataclass
class MechanicRouter:
    """机制卡检索器（无状态，可复用）。"""

    cards: dict[str, MechanicCard]
    max_cards: int = 3
    max_chars: int = 1800
    min_score: float = 3.0

    # ------------------------------------------------------------ 检索

    @staticmethod
    def _text_of(observation: Any, history: Optional[Iterable[dict[str, Any]]], extra: str) -> str:
        """构造关键词检索文本：只取**值**，不取字段名。

        这一点很关键：observation 的 JSON 里字段名本身就是一堆专有名词
        （rent_due / sellable_now / wholesale_cost…），如果把键也算进去，
        任何以字段名做关键词的卡片都会「永远命中」。
        """
        values: list[str] = []

        def walk(node: Any) -> None:
            if isinstance(node, dict):
                for value in node.values():
                    walk(value)
            elif isinstance(node, (list, tuple)):
                for value in node:
                    walk(value)
            elif isinstance(node, (str, int, float, bool)):
                text = str(node).strip()
                if text:
                    values.append(text)

        walk(observation)
        for item in list(history or [])[-8:]:
            if not isinstance(item, dict):
                continue
            walk({k: item.get(k) for k in ("action", "reason", "params")})
        if extra:
            values.append(str(extra))
        return "\n".join(values)

    def score(self, card: MechanicCard, observation: Any, text: str) -> float:
        trigger = card.trigger_score(observation)
        keyword = card.keyword_score(text)
        if trigger <= 0 and keyword <= 0:
            return 0.0
        # 触发器比关键词更可信：状态谓词命中说明「此刻确实在这个机制里」。
        # confidence 只做同分时的微调（×0.5）——不能让它把「一个泛词命中」抬进候选，
        # 否则「租金/服务」这类在观察 JSON 里到处出现的词会让卡片常驻。
        #
        # v3.17（用户 2026-11-22 质疑「他到底收到知识点了没有」）：
        # 权重从 trigger*2 提到 trigger*5。实测原因是——_text_of 只产出中文摘要
        # （「卷帘门 关闭」「顾客队列 已空」），**不含 garbage_level/attractiveness
        # 这类英文字段名**，于是「垃圾降吸引力」卡的关键词 0 命中，只靠触发器拿
        # 12*2=24 分，被关键词命中多的常驻卡（26~28 分）挤出 max_cards=3。
        # 提权后：精确条件命中 12*5=60，远高于纯词命中（≤10），顺序被纠正。
        return trigger * 5.0 + keyword + card.confidence * 0.5

    def select(
        self,
        observation: Any,
        *,
        history: Optional[Iterable[dict[str, Any]]] = None,
        extra_text: str = "",
        limit: Optional[int] = None,
        diversify: bool = True,
    ) -> list[MechanicCard]:
        """返回按相关度排序的卡片（最多 limit 张）。

        ``diversify``（v2.2）：先保证**每个 category 只取一张**，再按分数回填。
        203 张卡里 ``loop`` 类的日循环卡在任何营业/打烊局面都会触发；如果不做
        去重，「批发面板开着」也会被日循环挤掉「批发=整批全收」这种真正该看的卡。
        同一机制面只留最相关的一张，比单纯取 Top-N 更接近「此刻该看什么」。

        **过滤已废弃 / 未上线的卡（v2.2，T13）**：``deprecated`` 或 ``gated_until``
        非空的卡直接跳过。这与 docs/26 §1 硬约束⑥「远征/雇工禁止实现」是同一件事
        的两半——卡片标了 ``gated_until="post-launch"``，运行时就不该再喂给模型；
        否则「标记」只是写在 JSON 里给人看的，模型照样按它行动。
        """
        text = self._text_of(observation, history, extra_text)
        scored: list[tuple[float, MechanicCard]] = []
        for card in self.cards.values():
            # 已废弃 / 未上线：不进提示词（判据在卡片自身，不在这里重写业务规则）
            if card.deprecated or card.gated_until:
                continue
            value = self.score(card, observation, text)
            if value >= self.min_score:
                scored.append((value, card))
        scored.sort(key=lambda pair: (-pair[0], -pair[1].confidence, pair[1].id))
        ordered = [card for _, card in scored]
        cap = limit or self.max_cards
        if not diversify:
            return ordered[:cap]

        picked: list[MechanicCard] = []
        overflow: list[MechanicCard] = []
        seen: set[str] = set()
        for card in ordered:
            if len(picked) >= cap:
                break
            if card.category in seen:
                overflow.append(card)
                continue
            picked.append(card)
            seen.add(card.category)
        for card in overflow:
            if len(picked) >= cap:
                break
            picked.append(card)
        return picked

    # ------------------------------------------------------------ 渲染

    def render(self, cards: Optional[list[MechanicCard]] = None) -> str:
        return render_cards(cards if cards is not None else [], max_chars=self.max_chars)

    def block(
        self,
        observation: Any,
        *,
        history: Optional[Iterable[dict[str, Any]]] = None,
        extra_text: str = "",
    ) -> tuple[str, list[str]]:
        """一步到位的便捷接口：检索 + 渲染，返回 (提示词块, 命中 id 列表)。"""
        selected = self.select(observation, history=history, extra_text=extra_text)
        if not selected:
            return "", []
        return self.render(selected), [c.id for c in selected]

    # ------------------------------------------------------------ 索引/盘点

    def index_text(self) -> str:
        """一行一张卡的常驻索引（默认不注入，供调试或极简模式）。"""
        lines = ["【机制索引】"]
        for card in sorted(self.cards.values(), key=lambda c: (c.category, c.id)):
            lines.append(f"- [{card.category}] {card.title}（{card.id}）")
        return "\n".join(lines)

    def coverage(self) -> dict[str, int]:
        return summarize_coverage(self.cards.values())


def _clip(text: str, limit: int = _LINE_CHARS) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def render_cards(cards: Iterable[MechanicCard], *, max_chars: int = 1800) -> str:
    """把卡片渲染成紧凑提示词块；超出预算时先丢卡片、再截断条目。"""
    blocks: list[str] = []
    used = 0
    header = "【机制知识（按当前局面检索）】"
    used += len(header) + 1
    for card in cards:
        lines = [f"◆ {card.title}（{card.category} · 置信 {card.confidence:.2f}）"]
        if card.summary:
            lines.append(f"  摘要：{_clip(card.summary)}")
        for key, label in (("facts", "事实"), ("actions", "该做"),
                           ("pitfalls", "禁忌"), ("effects", "后果")):
            items = getattr(card, key) or []
            for item in items[: _SECTION_LIMITS[key]]:
                lines.append(f"  {label}：{_clip(item)}")
        block = "\n".join(lines)
        if used + len(block) + 2 > max_chars and blocks:
            break
        blocks.append(block)
        used += len(block) + 1
    if not blocks:
        return ""
    return header + "\n" + "\n".join(blocks)


def summarize_coverage(cards: Iterable[MechanicCard]) -> dict[str, int]:
    result: dict[str, int] = {}
    for card in cards:
        result[card.category] = result.get(card.category, 0) + 1
    return dict(sorted(result.items()))
