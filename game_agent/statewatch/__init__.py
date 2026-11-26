"""状态监听模块（statewatch）：直接读取游戏底层数据，替代视觉识别。

数据源（均为游戏自身写出的明文文件，零侵入、离线可测）：

1. **运行日志 GameLog_*.txt**（实时事件流，毫秒级）
   顾客到达 / 买卖意图 / 对话与交易结果 / 自动保存等，见 events 模块。
2. **存档 save_*.es3**（完整状态快照，ES3 明文 JSON）
   现金 / 租金 / 声望 / 各库存物品 / 经营统计，见 savefile 模块。

对外入口：

    StateWatcher       —— 组合监听器（实时轮询 run() / 单次采集 collect_once()）
    LogTailer          —— 日志文件增量跟随
    parse_save()       —— 解析单个存档 → GameSnapshot
    parse_lenient()    —— 宽容 JSON 解析（修复 ES3 非标写法）
    parse_log_line()   —— 解析单行日志 → LogEvent
    resolve_data_dir() —— 定位游戏持久化目录

典型用法：

    watcher = StateWatcher(resolve_data_dir())
    update = watcher.poll_once()        # 增量
    update = watcher.collect_once()     # 单次（含最近历史事件）
"""
from game_agent.statewatch.events import LogEvent, parse_log_line, parse_log_lines
from game_agent.statewatch.lenient_json import parse_lenient, repair_es3_json
from game_agent.statewatch.paths import default_data_dir, resolve_data_dir
from game_agent.statewatch.savefile import (
    GameSnapshot,
    InventoryItem,
    SaveFileInfo,
    build_snapshot,
    find_latest_save,
    list_saves,
    parse_save,
)
from game_agent.statewatch.state import (
    GameState,
    GameStateBuilder,
    derive_current_client,
    rebuild_client_sessions,
)
from game_agent.statewatch.tailer import LogTailer, find_latest_gamelog, read_recent_lines
from game_agent.statewatch.watcher import StateWatcher, WatchUpdate

__all__ = [
    # 监听器
    "StateWatcher",
    "WatchUpdate",
    # 日志
    "LogEvent",
    "LogTailer",
    "parse_log_line",
    "parse_log_lines",
    "find_latest_gamelog",
    "read_recent_lines",
    # 存档
    "GameSnapshot",
    "InventoryItem",
    "SaveFileInfo",
    "parse_save",
    "build_snapshot",
    "list_saves",
    "find_latest_save",
    # 状态组装
    "GameState",
    "GameStateBuilder",
    "derive_current_client",
    "rebuild_client_sessions",
    # 工具
    "parse_lenient",
    "repair_es3_json",
    "resolve_data_dir",
    "default_data_dir",
]
