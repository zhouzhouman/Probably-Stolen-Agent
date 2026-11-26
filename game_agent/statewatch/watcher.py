"""状态监听器：轮询组合「日志跟随 + 存档监听」，产出增量更新。

两种使用方式：

- 实时监听（默认）：``StateWatcher.run(on_update=...)`` 轮询循环，
  游戏运行期间持续产出「新事件 / 快照更新」；
- 单次采集：``StateWatcher.collect_once()`` 读当前存档 + 最近（或全部）
  日志事件，适合 CLI 一次性输出与离线回放。

存档解析采用「path + mtime」签名去重：只有文件真正被重写时才重新解析
（游戏自动保存约每 2~4 分钟一次 + 关键节点触发）；解析失败（恰逢文件
写入中）不抛异常，下一轮轮询重试。
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from game_agent.statewatch.events import LogEvent, parse_log_lines
from game_agent.statewatch.savefile import (
    GameSnapshot,
    build_snapshot,
    find_latest_save,
    parse_save,
)
from game_agent.statewatch.state import GameState, GameStateBuilder
from game_agent.statewatch.tailer import LogTailer, find_latest_gamelog, read_recent_lines

__all__ = ["WatchUpdate", "StateWatcher"]


@dataclass
class WatchUpdate:
    """一次轮询产出的增量更新。"""

    new_events: list[LogEvent]
    snapshot_updated: bool
    state: GameState

    @property
    def changed(self) -> bool:
        return bool(self.new_events) or self.snapshot_updated


class StateWatcher:
    """组合日志跟随与存档监听的轮询式状态监听器。"""

    def __init__(
        self,
        data_dir: str | Path,
        *,
        logs_dir: Optional[str | Path] = None,
        saves_dir: Optional[str | Path] = None,
        interval: float = 0.5,
        start_at_end: bool = True,
        max_events: int = 500,
    ) -> None:
        """
        Args:
            data_dir: 游戏持久化目录（日志与存档默认都在这里）。
            logs_dir / saves_dir: 单独覆盖日志 / 存档目录（默认同 data_dir）。
            interval: 轮询间隔（秒）。
            start_at_end: 日志跟随从「文件末尾」开始（实时监听默认）；
                设 False 或使用 collect_once(from_start=True) 可回放历史。
            max_events: 事件环形缓冲上限。
        """
        self.data_dir = Path(data_dir)
        self.logs_dir = Path(logs_dir) if logs_dir else self.data_dir
        self.saves_dir = Path(saves_dir) if saves_dir else self.data_dir
        self.interval = interval
        self.tailer = LogTailer(self.logs_dir, start_at_end=start_at_end)
        self.builder = GameStateBuilder(max_events=max_events)
        self._load_initial_snapshot()

    # ---------------------------------------------------------------- 增量模式

    def poll_once(self) -> WatchUpdate:
        """轮询一次：产出自上次以来的新事件与快照更新。"""
        events = self.tailer.poll()
        self.builder.apply_events(events)
        snapshot_updated = self._poll_snapshot()
        return WatchUpdate(
            new_events=events,
            snapshot_updated=snapshot_updated,
            state=self.builder.state(),
        )

    def run(
        self,
        on_update: Optional[Callable[[WatchUpdate], None]] = None,
        *,
        duration: Optional[float] = None,
        on_tick: Optional[Callable[[], None]] = None,
    ) -> None:
        """轮询循环：有变化时调用 on_update；duration（秒）到期或 Ctrl+C 停止。

        Args:
            on_update: 每次「有内容变化」时回调（WatchUpdate）。
            duration: 最长运行秒数；None 表示一直运行。
            on_tick: 每轮轮询都会调用（无论是否有变化，用于心跳显示）。
        """
        start = time.time()
        try:
            while True:
                update = self.poll_once()
                if on_tick is not None:
                    on_tick()
                if update.changed and on_update is not None:
                    on_update(update)
                if duration is not None and time.time() - start >= duration:
                    break
                time.sleep(self.interval)
        except KeyboardInterrupt:
            return

    # ---------------------------------------------------------------- 单次采集

    def collect_once(
        self,
        *,
        history_lines: int = 30,
        from_start: bool = False,
    ) -> WatchUpdate:
        """单次采集（无状态、可重复调用）：当前存档 + 最近/全部日志事件。

        Args:
            history_lines: 回眸的日志行数（from_start=False 时生效）。
            from_start: True 时读取当前日志文件的全部内容（离线回放）。
        """
        # 1) 日志事件
        events: list[LogEvent] = []
        latest_log = find_latest_gamelog(self.logs_dir)
        if latest_log is not None:
            if from_start:
                text = latest_log.read_text(encoding="utf-8", errors="replace")
                lines = [line.rstrip("\r") for line in text.split("\n") if line.strip()]
            else:
                lines = read_recent_lines(latest_log, n=history_lines)
            events = parse_log_lines(lines)

        # 2) 存档快照（独立解析，不回写增量状态）
        snapshot: Optional[GameSnapshot] = None
        snapshot_updated = False
        info = find_latest_save(self.saves_dir)
        if info is not None:
            try:
                snapshot = parse_save(info.path)
                cached = self.builder.snapshot
                snapshot_updated = cached is None or self.builder.snapshot_needs_update(info)
            except (OSError, ValueError):
                # 恰逢写入中：退回已有快照（可能为 None）
                snapshot = self.builder.snapshot

        state = GameState(snapshot=snapshot, events=events)
        return WatchUpdate(
            new_events=events,
            snapshot_updated=snapshot_updated,
            state=state,
        )

    # ---------------------------------------------------------------- 内部

    def _load_initial_snapshot(self) -> None:
        """启动时加载一次最新存档（列表展示用；失败则留 None）。"""
        info = find_latest_save(self.saves_dir)
        if info is not None:
            self._try_load_snapshot(info)

    def _poll_snapshot(self) -> bool:
        """检查最新存档是否有更新，有则解析并更新 builder。"""
        info = find_latest_save(self.saves_dir)
        if info is None or not self.builder.snapshot_needs_update(info):
            return False
        return self._try_load_snapshot(info)

    def _try_load_snapshot(self, info) -> bool:
        """解析指定存档并写入 builder；失败（写入中/损坏）返回 False。"""
        try:
            snapshot = parse_save(info.path)
        except (OSError, ValueError):
            return False
        return self.builder.update_snapshot(snapshot)

    # ---------------------------------------------------------------- 辅助信息

    def describe_paths(self) -> dict[str, Any]:
        """当前监听路径的可读描述（CLI 启动提示用）。"""
        return {
            "data_dir": str(self.data_dir),
            "logs_dir": str(self.logs_dir),
            "saves_dir": str(self.saves_dir),
            "logs_dir_exists": self.logs_dir.is_dir(),
            "saves_dir_exists": self.saves_dir.is_dir(),
        }
