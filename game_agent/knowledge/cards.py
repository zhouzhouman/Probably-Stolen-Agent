"""机制卡片：数据结构、校验与加载。

设计目标
--------
把「游戏机制知识」从一段 13KB 的静态提示词里拆出来，变成**可检索、可验证、
可分级（置信度/来源）的数据**。每张卡片只描述一个机制，字段固定：

    id / title / category / summary
    triggers   什么时候该把这张卡交给 AI（对 /state 观察的谓词）
    facts      机制事实（AI 必须知道的规则）
    actions    正确操作序列（可执行的动作名）
    pitfalls   常见错误（踩过的坑，带实证）
    effects    动作的可观测后果（用于事后校验）
    guards     硬性前置条件（可编译成代码守卫）
    evidence   证据来源（反汇编/日志/实盘）
    confidence 置信度 0~1
    verified_at / related / source

文件格式用 JSON（零第三方依赖，与项目其余部分一致）：
    knowledge/mechanics/<category>/<id>.json
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

__all__ = [
    "CATEGORIES",
    "TRIGGER_OPS",
    "MechanicError",
    "MechanicCard",
    "load_cards",
    "load_card_file",
]

# 机制分类（覆盖一款店铺经营 Roguelike 的全部系统面）
CATEGORIES = (
    "loop",        # 日循环 / 营业阶段 / 换日
    "trade",       # 交易 / 议价 / 批发 / 真伪 / 估价
    "customer",    # 顾客类型 / 意图 / 派系 / 特殊 NPC
    "economy",     # 现金 / 租金 / 贷款 / 加价率 / 库存估值
    "security",    # 通缉 / 举报 / 伪人 / 射击 / 突袭
    "night",       # 夜晚服务 / 睡觉 / 外出 / 拾荒 / 伤势
    "phone",       # 电话 / 供应商 / 订货
    "quest",       # 派系委托 / 解锁 / 导师
    "production",  # 鱼场 / 养殖 / 净水 / 设备 / 电力
    "store",       # 吸引力 / 垃圾 / 展示柜 / 扩建 / 雇佣
    "risk",        # 违禁品 / 卡特尔 / 犯罪点 / 罚款
    "meta",        # 存档 / 读档 / 成就 / 结局
)

# 触发器支持的比较算子
TRIGGER_OPS = frozenset({
    "eq", "ne", "in", "contains", "truthy", "falsy", "gte", "lte", "exists",
})

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_\-]{1,63}$")

# 纯数字/百分比/单位类关键词：子串匹配会命中任何金额或天数（如 "7" 命中 "第 74 天"）
_NUMERIC_KW = re.compile(r"^[\d\s.,:%+\-×xX/]+$")


def usable_keyword(keyword: Any) -> bool:
    """关键词是否可用于检索（数字类一律排除）。"""
    text = str(keyword).strip()
    if not text:
        return False
    if _NUMERIC_KW.match(text):
        return False
    if len(text) <= 2 and text.isascii():
        return False
    return True


def _as_bool(value: Any) -> bool:
    """宽容地把 JSON 里的布尔读成 ``bool``。

    历史卡里这些标记键根本不存在（``None`` → False）；R4 新写的卡是标准 JSON
    布尔。但手工编辑容易写成 ``"true"`` / ``1`` / ``"yes"``，这里一并接受，
    **不抛异常**——加载期抛错会让整包卡加载失败，代价远大于一个标记不准。
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value or "").strip().lower()
    if not text:
        return False
    return text in ("1", "true", "yes", "y", "on")


class MechanicError(ValueError):
    """机制卡校验失败（文件损坏 / 字段缺失 / 算子非法）。"""


@dataclass
class MechanicCard:
    """一张机制卡（immutable 使用，加载后只读）。"""

    id: str
    title: str
    category: str
    summary: str
    facts: list[str] = field(default_factory=list)
    actions: list[str] = field(default_factory=list)
    pitfalls: list[str] = field(default_factory=list)
    effects: list[str] = field(default_factory=list)
    guards: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    triggers: dict[str, Any] = field(default_factory=dict)
    evidence: list[str] = field(default_factory=list)
    confidence: float = 0.5
    verified_at: str = ""
    related: list[str] = field(default_factory=list)
    source: str = ""
    # —— v2.2 版本/适用性标记（R4 的 203 张卡需要，纯增量，不改变既有字段行为）——
    #: 该卡描述的机制所属游戏版本（如 "1.0"、"0.9.4"）；空串 = 未标注。
    game_version: str = ""
    #: 结论依据（如 "BridgeApi.cs:2813" / "实测 r79"）；空串 = 未标注。
    verify_basis: str = ""
    #: 是否还需实测复核。注意「需要复核」不等于「不可用」——卡片仍可进提示词，
    #: 只是提示模型这是待验证结论。真正的过滤在 ``router.select()``。
    needs_verify: bool = False
    #: 上线限制：``"post-launch"`` = 该机制在 1.0 未实装，不得喂给模型；
    #: 空串 = 无限制。只认这两个值，其它值按「未标注」容忍处理（见 from_dict）。
    gated_until: str = ""
    #: 是否已废弃（整条机制在正式版不存在或被改名/替换）。
    deprecated: bool = False
    #: 废弃理由（给人看的说明，不会进提示词）。
    deprecated_reason: str = ""

    # ------------------------------------------------------------ 校验

    @classmethod
    def from_dict(cls, data: Any, *, origin: str = "") -> "MechanicCard":
        if not isinstance(data, dict):
            raise MechanicError(f"{origin}: 卡片必须是 JSON 对象")
        card_id = str(data.get("id") or "").strip()
        if not _SLUG_RE.match(card_id):
            raise MechanicError(f"{origin}: id 非法（小写字母/数字/下划线/连字符）：{card_id!r}")
        category = str(data.get("category") or "").strip()
        if category not in CATEGORIES:
            raise MechanicError(f"{origin}: category 非法：{category!r}（可选 {CATEGORIES}）")
        title = str(data.get("title") or "").strip()
        if not title:
            raise MechanicError(f"{origin}: 缺少 title")

        facts = _str_list(data.get("facts"), origin, "facts")
        if not facts:
            raise MechanicError(f"{origin}: facts 不能为空（至少一条机制事实）")

        triggers = data.get("triggers") or {}
        if not isinstance(triggers, dict):
            raise MechanicError(f"{origin}: triggers 必须是对象")
        for key in ("all", "any"):
            for rule in triggers.get(key) or []:
                _validate_trigger(rule, origin)

        try:
            confidence = float(data.get("confidence", 0.5))
        except (TypeError, ValueError) as exc:
            raise MechanicError(f"{origin}: confidence 不是数字") from exc
        if not 0.0 <= confidence <= 1.0:
            raise MechanicError(f"{origin}: confidence 必须在 0~1")

        return cls(
            id=card_id,
            title=title,
            category=category,
            summary=str(data.get("summary") or "").strip(),
            facts=facts,
            actions=_str_list(data.get("actions"), origin, "actions"),
            pitfalls=_str_list(data.get("pitfalls"), origin, "pitfalls"),
            effects=_str_list(data.get("effects"), origin, "effects"),
            guards=_str_list(data.get("guards"), origin, "guards"),
            keywords=_str_list(data.get("keywords"), origin, "keywords"),
            triggers=triggers,
            evidence=_str_list(data.get("evidence"), origin, "evidence"),
            confidence=round(confidence, 3),
            verified_at=str(data.get("verified_at") or "").strip(),
            related=_str_list(data.get("related"), origin, "related"),
            source=str(data.get("source") or origin).strip(),
            # —— v2.2 版本/适用性标记，全部容错读取，不做硬校验 ——
            # 空值合法：历史卡（203 张里绝大多数）没有这些键，硬校验会让它们全部加载失败。
            game_version=str(data.get("game_version") or "").strip(),
            verify_basis=str(data.get("verify_basis") or "").strip(),
            needs_verify=_as_bool(data.get("needs_verify")),
            # gated_until 只认 "post-launch" 和 ""；其它值不抛异常（容忍），
            # 原样保留以便上层发现写错的标记（router 只认 "post-launch"）。
            gated_until=str(data.get("gated_until") or "").strip(),
            deprecated=_as_bool(data.get("deprecated")),
            deprecated_reason=str(data.get("deprecated_reason") or "").strip(),
        )

    # ------------------------------------------------------------ 匹配

    def trigger_score(self, observation: Any) -> int:
        """按 triggers 对观察求值：all 必须全真，any 命中即加分。返回 0 表示不匹配。"""
        if not isinstance(observation, dict):
            return 0
        rules = self.triggers or {}
        all_rules = rules.get("all") or []
        any_rules = rules.get("any") or []
        if not all_rules and not any_rules:
            return 0
        for rule in all_rules:
            if not evaluate_trigger(rule, observation):
                return 0
        hits = 0
        for rule in any_rules:
            if evaluate_trigger(rule, observation):
                hits += 1
        if any_rules and hits == 0:
            return 0
        return 10 + hits * 2

    def keyword_score(self, text: str) -> int:
        """关键词命中数（观察 + 最近动作 + 目标文本）。"""
        if not text:
            return 0
        haystack = text.lower()
        hits = 0
        for kw in self.keywords:
            kw_text = str(kw).strip().lower()
            if usable_keyword(kw_text) and kw_text in haystack:
                hits += 1
        return hits * 2

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "category": self.category,
            "summary": self.summary,
            "facts": list(self.facts),
            "actions": list(self.actions),
            "pitfalls": list(self.pitfalls),
            "effects": list(self.effects),
            "guards": list(self.guards),
            "keywords": list(self.keywords),
            "triggers": self.triggers,
            "evidence": list(self.evidence),
            "confidence": self.confidence,
            "verified_at": self.verified_at,
            "related": list(self.related),
            "source": self.source,
            # —— v2.2 版本/适用性标记（原样输出，便于 R4 的生成器与文档同步）——
            "game_version": self.game_version,
            "verify_basis": self.verify_basis,
            "needs_verify": self.needs_verify,
            "gated_until": self.gated_until,
            "deprecated": self.deprecated,
            "deprecated_reason": self.deprecated_reason,
        }


# ---------------------------------------------------------------- 触发器求值


def _validate_trigger(rule: Any, origin: str) -> None:
    if not isinstance(rule, dict):
        raise MechanicError(f"{origin}: trigger 规则必须是对象")
    if not str(rule.get("field") or "").strip():
        raise MechanicError(f"{origin}: trigger 缺少 field")
    op = str(rule.get("op") or "eq")
    if op not in TRIGGER_OPS:
        raise MechanicError(f"{origin}: trigger op 非法：{op!r}（可选 {sorted(TRIGGER_OPS)}）")


_MISSING = object()


def _collect(data: Any, parts: list[str]) -> list[Any]:
    """按 a.b[].c 收集全部取值。

    - 普通段：取字典键；
    - ``xxx[]`` 段：对列表逐元素继续走剩余路径，把结果摊平。

    返回空列表表示「路径不存在」——调用方必须把它当作**不可判定**，
    而不是 falsy（v3.0 修复：旧实现把缺失字段判成 falsy，导致
    「电源关闭」类卡片在任何局面都命中，检索退化成噪声）。
    """
    if not parts:
        return [data]
    head, rest = parts[0], parts[1:]
    if head.endswith("[]"):
        key = head[:-2]
        if not isinstance(data, dict):
            return []
        items = data.get(key)
        if not isinstance(items, list):
            return []
        out: list[Any] = []
        for item in items:
            out.extend(_collect(item, rest))
        return out
    if not isinstance(data, dict):
        return []
    if head not in data:
        return []
    return _collect(data[head], rest)


def _get_path(data: Any, path: str) -> Any:
    """按 a.b.c 取值（保留旧接口）；缺失返回 ``_MISSING`` 哨兵。"""
    values = _collect(data, str(path).split("."))
    return values[0] if values else _MISSING


def _match(value: Any, op: str, target: Any) -> bool:
    if op == "exists":
        return value is not None and value is not _MISSING
    if op == "truthy":
        return bool(value)
    if op == "falsy":
        return not bool(value)
    if op == "eq":
        return value == target
    if op == "ne":
        return value != target
    if op == "gte":
        try:
            return float(value) >= float(target)
        except (TypeError, ValueError):
            return False
    if op == "lte":
        try:
            return float(value) <= float(target)
        except (TypeError, ValueError):
            return False
    if op == "in":
        try:
            return value in (target or [])
        except TypeError:
            return False
    if op == "contains":
        if isinstance(value, list):
            return target in value
        if isinstance(value, str):
            return str(target) in value
        return False
    return False


def evaluate_trigger(rule: dict[str, Any], observation: dict[str, Any]) -> bool:
    """对单条触发器求值（纯函数，便于单测）。

    字段缺失或值为 ``None`` 一律返回 False：**无数据不等于「否」**。
    这一条很关键——观察层会保留 ``is_power_on: null`` 这类占位键，
    若把 None 当 falsy，「电源关闭」类卡片会在任何局面命中（实测噪声 24 分）。
    """
    values = [
        value
        for value in _collect(observation, str(rule.get("field") or "").split("."))
        if value is not None
    ]
    if not values:
        return False
    op = str(rule.get("op") or "eq")
    target = rule.get("value")
    return any(_match(value, op, target) for value in values)


def _str_list(value: Any, origin: str, field_name: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise MechanicError(f"{origin}: {field_name} 必须是字符串数组")
    result: list[str] = []
    for item in value:
        text = str(item).strip()
        if text:
            result.append(text)
    return result


# ---------------------------------------------------------------- 加载


def load_card_file(path: str | Path) -> MechanicCard:
    """加载单张卡片；文件损坏 / 校验失败抛 MechanicError。"""
    p = Path(path)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise MechanicError(f"{p}: 读取失败（{exc}）") from exc
    return MechanicCard.from_dict(data, origin=str(p))


def load_cards(directory: str | Path) -> dict[str, MechanicCard]:
    """递归加载目录下所有 *.json 机制卡。

    - 同 id 重复：保留 confidence 更高者，并在结果里覆盖（后加载者胜）；
    - 单张卡片损坏：抛 MechanicError（宁可失败，不要静默丢知识）。
    """
    root = Path(directory)
    cards: dict[str, MechanicCard] = {}
    if not root.is_dir():
        return cards
    for path in sorted(root.rglob("*.json")):
        card = load_card_file(path)
        existing = cards.get(card.id)
        if existing is not None and existing.confidence > card.confidence:
            continue
        cards[card.id] = card
    return cards
