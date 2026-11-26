"""游戏状态组装：把「存档快照」与「日志事件流」合并为统一状态对象。

- GameState：某一时刻的统一视图（供 CLI 展示 / 未来送给 brain 决策）；
- GameStateBuilder：增量状态机——逐步吸收新事件与快照更新，维护最近的
  事件环形缓冲，并随时可导出 GameState；
- derive_current_client()：从事件流推导「当前在店顾客」的轻量状态机。

设计取舍：快照管「静」的部分（现金 / 库存 / 租金），事件流管「动」的部分
（顾客来去 / 交易结果），二者通过时间自然对齐（快照年龄会随更新消失）。
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

from game_agent.statewatch.events import LogEvent
from game_agent.statewatch.savefile import GameSnapshot, SaveFileInfo

__all__ = ["GameState", "GameStateBuilder", "derive_current_client", "rebuild_client_sessions"]


def derive_current_client(events: Iterable[LogEvent]) -> Optional[dict[str, Any]]:
    """从事件序列推导「当前在店顾客」；店内无人时返回 None。

    状态来源：client_approaching / client_arrived / client_intent /
    conversation_ended / client_leaving / client_left（见 events 模块）。
    """
    current: Optional[dict[str, Any]] = None
    for event in events:
        kind = event.kind
        if kind == "client_approaching":
            current = {
                "client_id": None,
                "intent": None,
                "stage": "approaching",
                "since": event.ts,
            }
        elif kind == "client_arrived":
            if current is None:
                current = {"client_id": None, "intent": None, "stage": "in_store", "since": event.ts}
            else:
                current["stage"] = "in_store"
        elif kind == "client_intent":
            if current is None:
                current = {"client_id": None, "intent": None, "stage": "in_store", "since": event.ts}
            current["client_id"] = event.data.get("client_id")
            current["intent"] = event.data.get("intent")
            current["stage"] = "intent"
        elif kind == "client_dialogue":
            if current is None:
                current = {"client_id": None, "intent": None, "stage": "in_store", "since": event.ts}
            current["client_id"] = current["client_id"] or event.data.get("client_id")
            current["stage"] = "dialogue"
        elif kind == "conversation_ended":
            if current is not None:
                current["stage"] = "conversation"
        elif kind == "client_leaving":
            if current is not None:
                current["stage"] = "leaving"
        elif kind == "client_left":
            current = None
    return current


def rebuild_client_sessions(events: Iterable[LogEvent]) -> list[dict[str, Any]]:
    """从事件流重建「顾客会话」流水（每位顾客一条记录）。

    一条会话：approaching → arrived → intent → [对话/动作] → left。
    仅收集「最终离开且识别出类型」的顾客；结果（outcome）为启发式推断：

    - "not_done"：出现 ``Business Not Done``（交易未成交，可靠信号）；
    - "completed"：无 not_done 且出现 ``End Action Executed``（大概率成交）；
    - "unknown"：其余情况。

    返回字段：arrived / client_id / intent / outcome / left。
    """
    sessions: list[dict[str, Any]] = []
    current: Optional[dict[str, Any]] = None
    for event in events:
        kind = event.kind
        if kind == "client_approaching":
            current = {
                "arrived": event.ts,
                "client_id": None,
                "intent": None,
                "outcome": "unknown",
                "left": None,
            }
        elif current is None:
            continue
        elif kind == "client_intent":
            current["client_id"] = event.data.get("client_id")
            current["intent"] = event.data.get("intent")
        elif kind == "business_not_done":
            current["outcome"] = "not_done"
        elif kind == "action_ended" and current["outcome"] == "unknown":
            current["outcome"] = "completed"
        elif kind == "client_left":
            current["left"] = event.ts
            if current["client_id"]:
                sessions.append(current)
            current = None
    return sessions


@dataclass
class GameState:
    """统一游戏状态视图（快照 + 最近事件）。"""

    snapshot: Optional[GameSnapshot] = None
    events: list[LogEvent] = field(default_factory=list)  # 时间升序，尾部是最新

    def recent_events(
        self,
        limit: int = 10,
        kinds: Optional[Iterable[str]] = None,
    ) -> list[LogEvent]:
        """取最近的若干事件；kinds 非空时只保留指定类型。"""
        events = self.events
        if kinds is not None:
            allowed = set(kinds)
            events = [e for e in events if e.kind in allowed]
        return events[-limit:] if limit > 0 else list(events)

    def current_client(self) -> Optional[dict[str, Any]]:
        """当前在店顾客（推导结果见 derive_current_client）。"""
        return derive_current_client(self.events)

    def summary(self) -> dict[str, Any]:
        """精简摘要（CLI 展示 / 未来 brain 输入）。"""
        snap = self.snapshot
        inventory_counts = (
            {name: len(items) for name, items in snap.inventories.items() if items}
            if snap is not None
            else {}
        )
        return {
            "cash": snap.cash if snap else None,
            "day": snap.day if snap else None,
            "day_until_rent": snap.day_until_rent if snap else None,
            "rent_value": snap.rent_value if snap else None,
            "is_power_on": snap.is_power_on if snap else None,
            "inventory_counts": inventory_counts,
            "client": self.current_client(),
            "snapshot": (
                {
                    "slot": snap.slot,
                    "file": snap.path,
                    "age_seconds": round(snap.age_seconds, 1),
                }
                if snap is not None
                else None
            ),
            "recent_events": [e.to_dict() for e in self.recent_events(limit=10)],
        }

    def to_dict(self, include_raw: bool = False) -> dict[str, Any]:
        """完整导出（include_raw=True 时附带存档完整解析树）。"""
        return {
            "snapshot": self.snapshot.to_dict(include_raw=include_raw) if self.snapshot else None,
            "events": [e.to_dict() for e in self.events],
            "client": self.current_client(),
        }


class GameStateBuilder:
    """增量状态机：吸收事件与快照更新，维护最近事件缓冲。"""

    def __init__(self, max_events: int = 500) -> None:
        self.max_events = max_events
        self._events: deque[LogEvent] = deque(maxlen=max_events)
        self._snapshot: Optional[GameSnapshot] = None
        self._snapshot_sig: Optional[tuple[str, float]] = None

    # ---------------------------------------------------------------- 输入

    def apply_events(self, events: Iterable[LogEvent]) -> None:
        """吸收一批新事件（超上限自动丢弃最旧的）。"""
        for event in events:
            self._events.append(event)

    def update_snapshot(self, snapshot: GameSnapshot) -> bool:
        """更新快照；与现有快照相同（路径+mtime 未变）时返回 False。"""
        sig = (snapshot.path, round(snapshot.mtime, 3))
        if sig == self._snapshot_sig:
            return False
        self._snapshot = snapshot
        self._snapshot_sig = sig
        return True

    def snapshot_needs_update(self, info: SaveFileInfo) -> bool:
        """判断给定存档文件是否比当前快照更新（用于避免重复解析）。"""
        sig = (str(info.path), round(info.mtime, 3))
        return sig != self._snapshot_sig

    # ---------------------------------------------------------------- 输出

    @property
    def snapshot(self) -> Optional[GameSnapshot]:
        return self._snapshot

    def state(self) -> GameState:
        return GameState(snapshot=self._snapshot, events=list(self._events))
