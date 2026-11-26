"""GameLog 日志行解析：把游戏运行日志解析为结构化事件流。

日志位置与格式
--------------
游戏每次启动都会在持久化目录新建一个 ``GameLog_YYYY-MM-DD_HH-MM-SS.txt``，
每行格式：

    [HH:MM:SS] [Log|Warning|Error] 消息文本

消息文本中可能包含 Unity 富文本标记（如 ``<color=orange>...</color>``），
解析时会先剥离。事件类型（kind）通过「精确匹配表 + 正则规则表」判定，
未命中任何规则的行走 ``kind="other"``（原文保留在 message 中，不丢信息）。

事件字典（供上层推导状态 / 决策消费）
--------------------------------------
client_approaching        顾客接近店铺
client_arrived            顾客到达柜台
client_leaving            顾客准备离开
client_left               顾客已离开
client_intent             顾客买卖意图（data: client_id / intent=SELL|BUY）
client_dialogue           顾客开始对话（非买卖顾客，如 mentor 导师；data: client_id）
conversation_ended        对话结束
action_started            动作开始执行（收购 / 鉴定等）
action_ended              动作执行结束
business_not_done         交易未成交
arrest_started            开始逮捕对话（治安官抓通缉犯）
wanted_reported_correct   正确举报通缉犯
game_saved                游戏自动保存（data: slot）
save_loaded               载入存档（data: slot）
game_started              新局开始（data: profession / endless）
price_tracked             价格跟踪（data: price）
other                     其他未分类消息
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional

__all__ = ["LogEvent", "parse_log_line", "parse_log_lines"]

# 主行格式：[09:19:12] [Log] 消息文本
_LINE_RE = re.compile(r"^\[(\d{2}:\d{2}:\d{2})\]\s*\[(\w+)\]\s*(.*)$")

# Unity 富文本标记：<color=...> </color> <b> <i> <size=...> 等
_RICH_TEXT_RE = re.compile(r"</?(?:color|b|i|size|material)(?:=[^>]*)?>", re.IGNORECASE)

# 精确匹配的消息 → 事件类型（查表前会去掉结尾感叹号）
_EXACT_KINDS: dict[str, str] = {
    "Arriving": "client_approaching",
    "Client Arrived": "client_arrived",
    "Leaving": "client_leaving",
    "Client Left": "client_left",
    "Conversation Ended": "conversation_ended",
    "Start Action Executed": "action_started",
    "End Action Executed": "action_ended",
    "Business Not Done": "business_not_done",
    "Starting Arrest Dialogue": "arrest_started",
    "Correct Report of Wanted Customers": "wanted_reported_correct",
}

# 正则规则表：模式 → 事件类型（附加数据在 _classify 内提取）
_REGEX_KINDS: list[tuple[re.Pattern[str], str]] = [
    # 顾客意图：如 "wanted2 SELL" / "pettyThief BUY"
    (re.compile(r"^([A-Za-z0-9_]+) (SELL|BUY)$"), "client_intent"),
    # 顾客开始对话（非买卖顾客，如导师）：如 "mentor DIALOGUE"
    (re.compile(r"^([A-Za-z0-9_]+) DIALOGUE$"), "client_dialogue"),
    # 自动保存："Game Saved in Slot: 15"
    (re.compile(r"^Game Saved in Slot: (\d+)"), "game_saved"),
    # 载入存档："Loaded save slot: 16"
    (re.compile(r"^Loaded save slot: (\d+)$"), "save_loaded"),
    # 价格跟踪："tracked price: 101"
    (re.compile(r"^tracked price: (\d+)"), "price_tracked"),
    # 开局："Started From The Main Menu as APPRENTICE with skipIntro=False endlessMode=False"
    (
        re.compile(
            r"^Started From The Main Menu as (\S+) with skipIntro=(\w+) endlessMode=(\w+)$"
        ),
        "game_started",
    ),
]


@dataclass
class LogEvent:
    """一条结构化的游戏日志事件。"""

    seq: int                 # 监听器内的自增序号（排序 / 去重）
    ts: str                  # 游戏内时间戳 "HH:MM:SS"（日志行自带）
    level: str               # Log / Warning / Error
    kind: str                # 规范化事件类型（见模块文档）
    message: str             # 剥离富文本后的消息文本
    data: dict[str, Any] = field(default_factory=dict)  # 附加结构化数据
    raw: str = ""            # 原始行（审计用）

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "ts": self.ts,
            "level": self.level,
            "kind": self.kind,
            "message": self.message,
            "data": self.data,
        }


def _classify(message: str) -> tuple[str, dict[str, Any]]:
    """按消息文本判定事件类型与附加数据。"""
    key = message.rstrip("!")
    kind = _EXACT_KINDS.get(key)
    if kind:
        return kind, {}

    for pattern, kind in _REGEX_KINDS:
        m = pattern.match(message)
        if not m:
            continue
        if kind == "client_intent":
            return kind, {"client_id": m.group(1), "intent": m.group(2).lower()}
        if kind == "client_dialogue":
            return kind, {"client_id": m.group(1)}
        if kind == "game_saved":
            return kind, {"slot": int(m.group(1))}
        if kind == "save_loaded":
            return kind, {"slot": int(m.group(1))}
        if kind == "price_tracked":
            return kind, {"price": int(m.group(1))}
        if kind == "game_started":
            return kind, {
                "profession": m.group(1),
                "skip_intro": m.group(2).lower() == "true",
                "endless_mode": m.group(3).lower() == "true",
            }
    return "other", {}


def parse_log_line(line: str, seq: int = 0) -> Optional[LogEvent]:
    """解析单行日志；空行或格式不符返回 None（不抛异常）。"""
    stripped = line.strip()
    if not stripped:
        return None
    m = _LINE_RE.match(stripped)
    if not m:
        return None

    ts, level, raw_message = m.group(1), m.group(2), m.group(3)
    message = _RICH_TEXT_RE.sub("", raw_message).strip()
    if not message:
        return None

    kind, data = _classify(message)
    return LogEvent(seq=seq, ts=ts, level=level, kind=kind, message=message, data=data, raw=line)


def parse_log_lines(lines: list[str], start_seq: int = 0) -> list[LogEvent]:
    """批量解析多行日志（自动编号，跳过无法解析的行）。"""
    events: list[LogEvent] = []
    seq = start_seq
    for line in lines:
        seq += 1
        event = parse_log_line(line, seq=seq)
        if event is not None:
            events.append(event)
    return events
