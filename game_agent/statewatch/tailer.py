"""GameLog 文件跟随器：定位最新日志文件并增量产出新事件。

游戏每次启动都会新建 ``GameLog_YYYY-MM-DD_HH-MM-SS.txt``（文件名自带
可排序的时间戳）。跟随逻辑：

- 每次 poll() 取「文件名最大」的 GameLog_*.txt 作为当前文件；
- 文件未变 → 从上次字节偏移继续读（增量）；
- 文件变了（游戏重启）→ 切换到新文件、从头读；
- 读取按「行」切分，不以换行结尾的尾部字节暂存为缓冲，
  待写完整后再产出——因此不会把「半行」误解析成事件；
- 文件被截断/重写（size 变小）时自动重置偏移。

start_at_end=True（默认，实时监听场景）：首次附着时直接跳到文件末尾，
只关心「开始监听之后」的新事件；False 时从头读（离线回放场景）。
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from game_agent.statewatch.events import LogEvent, parse_log_line

__all__ = ["find_latest_gamelog", "read_recent_lines", "LogTailer"]


def find_latest_gamelog(logs_dir: str | Path) -> Optional[Path]:
    """返回目录中「最新」的 GameLog_*.txt（文件名时间戳可排序），没有则 None。"""
    directory = Path(logs_dir)
    if not directory.is_dir():
        return None
    candidates = sorted(directory.glob("GameLog_*.txt"))
    return candidates[-1] if candidates else None


def read_recent_lines(
    path: str | Path,
    n: int = 30,
    *,
    encoding: str = "utf-8",
    max_bytes: int = 2_000_000,
) -> list[str]:
    """读取文件末尾最多 n 行（用于「回眸最近事件」；大文件只读尾部一片）。"""
    p = Path(path)
    size = p.stat().st_size
    with open(p, "rb") as f:
        if size > max_bytes:
            f.seek(size - max_bytes)
        data = f.read()
    text = data.decode(encoding, errors="replace")
    lines = [line.rstrip("\r") for line in text.split("\n") if line.strip()]
    return lines[-n:]


class LogTailer:
    """增量跟随最新的 GameLog 文件，产出结构化事件。"""

    def __init__(
        self,
        logs_dir: str | Path,
        *,
        encoding: str = "utf-8",
        start_at_end: bool = True,
    ) -> None:
        self.logs_dir = Path(logs_dir)
        self.encoding = encoding
        self.start_at_end = start_at_end
        self._file: Optional[Path] = None
        self._offset = 0
        self._partial = ""   # 半行缓冲（不含换行结尾的尾部）
        self._seq = 0

    @property
    def current_file(self) -> Optional[Path]:
        """当前跟随的日志文件（尚未附着时为 None）。"""
        return self._file

    def poll(self) -> list[LogEvent]:
        """扫描一次：发现新文件 / 新行时返回新事件列表（无变化返回空表）。"""
        latest = find_latest_gamelog(self.logs_dir)
        if latest is None:
            return []

        if latest != self._file:
            # 切换文件：首次附着按 start_at_end 决定起点；游戏重启后从新文件头读
            first_attach = self._file is None
            self._file = latest
            self._partial = ""
            if first_attach and self.start_at_end:
                self._offset = latest.stat().st_size
            else:
                self._offset = 0
        else:
            # 文件未变：防御文件截断/重写
            try:
                if latest.stat().st_size < self._offset:
                    self._offset = 0
                    self._partial = ""
            except OSError:
                return []

        chunk = self._read_from_offset()
        if not chunk:
            return []

        text = self._partial + chunk.decode(self.encoding, errors="replace")
        lines = text.split("\n")
        self._partial = lines.pop()  # 最后一段可能是不完整行，留到下次

        events: list[LogEvent] = []
        for line in lines:
            line = line.rstrip("\r")
            if not line.strip():
                continue
            self._seq += 1
            event = parse_log_line(line, seq=self._seq)
            if event is not None:
                events.append(event)
        return events

    def _read_from_offset(self) -> bytes:
        """从当前偏移读取新增字节并推进偏移；读取失败返回空。"""
        assert self._file is not None
        try:
            with open(self._file, "rb") as f:
                f.seek(self._offset)
                chunk = f.read()
                self._offset = f.tell()
            return chunk
        except OSError:
            return b""
