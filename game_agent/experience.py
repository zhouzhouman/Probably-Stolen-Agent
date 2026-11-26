"""跨会话经验记忆：让 agent 记住「上次这种局面这么做，结果如何」。

为什么需要它
------------
项目原有的两套"知识"都不能替代经验：

1. ``game_agent.knowledge`` 的机制卡讲的是**游戏规则**（"批发按钮是买入不是卖出"），
   是静态的、与本次运行无关；
2. ``agent_loop`` 里的 ``history`` 只保留最近 3 步、且**只在内存**，进程一退出就丢。

结果是：Agent 每次启动都是零经验新手，会在同一类局面反复做无用功
（同一物品连卖 5 次、现金不够还去进货、桌上没货却点成交……）。

本模块把「局面特征 → 动作 → 结果」落成可跨会话复用的经验条目，
并在下一次遇到相似局面时以极短文本注入提示词。**模型不需要换**：
它只是在上下文里看到了"你自己上次这么干亏了"，就足以改变选择。

设计约束
--------
* **纯本地统计，不额外调模型**——记录和检索都不花钱、不增加延迟。
* **只认状态变化**：动作返回 ok 不代表有效（项目已有"假成功"教训），
  一律用 ``progressed``（状态签名是否变化）判定。
* **噪声防护**：同一「局面+动作」至少观测 2 次才允许进提示词，
  否则一次偶然失败会永久钉死一个错误结论。
* **体积极小**：注入文本硬上限 300 字符，避免又把提示词撑大。
* **失败不阻断**：文件损坏/写不进去都静默降级，绝不影响主流程。
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

#: 注入提示词的最大字符数（经验段必须短，否则又变成噪声）。
MAX_INJECT_CHARS = 300

#: 同一「局面+动作」至少观测次数，低于它不写进提示词（防一次偶然）。
MIN_SAMPLES = 2

#: 现金分档阈值（用于局面匹配，不必精确金额）。
_CASH_LOW = 200
_CASH_HIGH = 1200


def _bucket_cash(cash: Any) -> str:
    try:
        value = float(cash)
    except (TypeError, ValueError):
        return "unknown"
    if value < _CASH_LOW:
        return "low"
    if value < _CASH_HIGH:
        return "mid"
    return "high"


def pattern_of(observation: dict[str, Any]) -> dict[str, str]:
    """从观察里提取**稳定、可匹配**的局面特征。

    刻意只取"换成另一天也仍然成立"的维度：门店状态、昼夜、顾客意图、
    桌上有没有货、现金档次、队列有无。不取具体金额/物品名——那会让
    每次局面都独一无二，经验永远命中不了。
    """
    vi = (observation.get("visible_info") or {}) if isinstance(observation, dict) else {}
    client = vi.get("client") or {}
    table = vi.get("table") or {}
    night = vi.get("night") or {}
    queue = vi.get("queue") or {}

    # 夜间判定：只有明确「在店外」才算。字段缺失时**不能**默认成夜晚
    #（早期实现把空字符串 != "IN_STORE" 判成店外，结果营业中被标成 night）。
    outside = bool(night.get("is_outside"))
    if not outside:
        loc = str(night.get("player_location") or night.get("current_location") or "").upper()
        outside = loc not in ("", "STORE", "IN_STORE")
    if str(vi.get("scene") or "").lower().startswith("night") or outside:
        phase = "night"
    else:
        phase = "day"

    intent = str(client.get("intent") or "").upper()
    if not intent:
        intent = "none"

    return {
        "phase": phase,
        "store": str(vi.get("store_state") or "?"),
        "intent": intent,
        "nego": "yes" if table.get("negociated_item") else "no",
        "cash": _bucket_cash(vi.get("money") if vi.get("money") is not None else vi.get("cash")),
        "queue": "has" if int(queue.get("count") or 0) > 0 else "empty",
    }


@dataclass
class Experience:
    """一条经验：某局面下做了某动作，是否推进了局面。"""

    pattern: dict[str, str]
    action: str
    progressed: bool
    note: str = ""
    ts: float = field(default_factory=time.time)

    @property
    def key(self) -> tuple[str, str]:
        """统计聚合键：(局面指纹, 动作)。"""
        return (json.dumps(self.pattern, sort_keys=True, ensure_ascii=False), self.action)


class ExperienceStore:
    """经验库：JSONL 追加写 + 内存统计。

    文件格式每行一条：
    ``{"pattern": {...}, "action": "sell_item", "progressed": true, "note": "...", "ts": 1.0}``
    """

    def __init__(self, path: Optional[Path | str] = None) -> None:
        self.path = Path(path) if path else None
        self._items: list[Experience] = []
        if self.path and self.path.exists():
            self._load()

    # ------------------------------------------------------------------ 读写

    def _load(self) -> None:
        try:
            with self.path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        raw = json.loads(line)
                        self._items.append(
                            Experience(
                                pattern=dict(raw.get("pattern") or {}),
                                action=str(raw.get("action") or ""),
                                progressed=bool(raw.get("progressed")),
                                note=str(raw.get("note") or ""),
                                ts=float(raw.get("ts") or 0),
                            )
                        )
                    except (json.JSONDecodeError, TypeError, ValueError):
                        continue  # 单行坏了不影响其余
        except OSError:
            pass

    def add(self, exp: Experience) -> None:
        """记录一条经验；内存总是更新，落盘失败只忽略磁盘那一步。"""
        self._items.append(exp)
        if not self.path:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(asdict(exp), ensure_ascii=False) + "\n")
        except OSError:
            pass

    # ------------------------------------------------------------------ 统计

    def stats(self, pattern: dict[str, str], action: str) -> tuple[int, int]:
        """返回 (该局面下该动作的样本数, 其中推进的次数)。"""
        total = ok = 0
        for exp in self._items:
            if exp.action != action:
                continue
            if not matches(exp.pattern, pattern):
                continue
            total += 1
            if exp.progressed:
                ok += 1
        return total, ok

    def summarize(self, pattern: dict[str, str], limit: int = 6) -> list[dict[str, Any]]:
        """按当前局面聚合出可注入的经验条目（已过滤噪声）。"""
        buckets: dict[str, dict[str, Any]] = {}
        for exp in self._items:
            if not matches(exp.pattern, pattern):
                continue
            slot = buckets.setdefault(exp.action, {"action": exp.action, "total": 0, "ok": 0, "note": ""})
            slot["total"] += 1
            if exp.progressed:
                slot["ok"] += 1
            if exp.note and not slot["note"]:
                slot["note"] = exp.note

        rows = [
            v for v in buckets.values()
            if v["total"] >= MIN_SAMPLES
        ]
        # 先展示「做完完全没效果」的，那是模型最容易踩的坑
        rows.sort(key=lambda r: (r["ok"] / max(r["total"], 1), -r["total"]))
        return rows[:limit]

    # ------------------------------------------------------------------ 渲染

    def render(self, pattern: dict[str, str], max_chars: int = MAX_INJECT_CHARS) -> str:
        """渲染成注入提示词的短文本；没有可用经验时返回空串。"""
        rows = self.summarize(pattern)
        if not rows:
            return ""
        lines: list[str] = []
        used = 0
        for row in rows:
            total, ok, action = row["total"], row["ok"], row["action"]
            if ok == 0:
                line = f"· {action}：{total} 次全部无效（局面没有任何变化，别再试）"
            elif ok == total:
                line = f"· {action}：{total} 次都推进了局面，可优先考虑"
            else:
                line = f"· {action}：{total} 次里 {ok} 次有效（{ok * 100 // total}%）"
            if row["note"]:
                line += f"；{row['note'][:40]}"
            if used + len(line) + 1 > max_chars:
                break
            lines.append(line)
            used += len(line) + 1
        if not lines:
            return ""
        head = "【你自己的经验（来自过往实跑，不是规则；可参考但以当前局面为准）】\n"
        return head + "\n".join(lines) + "\n"


def _similarity(a: dict[str, str], b: dict[str, str]) -> float:
    """局面相似度：共同键的取值一致比例。1.0 表示完全一致。"""
    keys = set(a) & set(b)
    if not keys:
        return 0.0
    same = sum(1 for k in keys if a.get(k) == b.get(k))
    return same / len(keys)


#: 必须一致的核心维度——顾客意图决定「买/卖/以物易物」，桌前有没有货决定能否成交。
#: 这两项不同就是完全不同的局面，经验不能互相复用（把「对买家」的结论套到「卖家」上会误导）。
CORE_KEYS = ("intent", "nego")

#: 非核心字段允许差异的比例阈值（6 个维度里至少 4 个一致）。
MATCH_THRESHOLD = 0.66


def matches(a: dict[str, str], b: dict[str, str]) -> bool:
    """两个局面是否足够相似、可以复用经验。

    为什么不用严格相等：``store`` 从 OPEN 变 CLOSED、``queue`` 有无变化，
    并不改变「这个动作在这类局面下有没有用」的结论。严格相等会让命中率接近 0，
    经验形同虚设；而完全不限又会让买家经验污染卖家。
    """
    for key in CORE_KEYS:
        if key in a and key in b and a.get(key) != b.get(key):
            return False
    return _similarity(a, b) >= MATCH_THRESHOLD


#: 必须一致的核心维度——顾客意图决定「买/卖/以物易物」，桌前有没有货决定能否成交。
#: 这两项不同就是完全不同的局面，经验不能互相复用（把「对买家」的结论套到「卖家」上会误导）。
CORE_KEYS = ("intent", "nego")

#: 非核心字段允许差异的比例阈值（6 个维度里至少 4 个一致）。
MATCH_THRESHOLD = 0.66


def matches(a: dict[str, str], b: dict[str, str]) -> bool:
    """两个局面是否足够相似、可以复用经验。

    为什么不用严格相等：``store`` 从 OPEN 变 CLOSED、``queue`` 有无变化，
    并不改变「这个动作在这类局面下有没有用」的结论。严格相等会让命中率接近 0，
    经验形同虚设；而完全不限又会让买家经验污染卖家。
    """
    for key in CORE_KEYS:
        if key in a and key in b and a.get(key) != b.get(key):
            return False
    return _similarity(a, b) >= MATCH_THRESHOLD
