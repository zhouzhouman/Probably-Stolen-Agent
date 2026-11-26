"""存档解析：读取 save_*.es3（ES3 明文 JSON）并提取核心游戏状态。

存档文件位于游戏持久化目录，命名 ``save_<槽位>.es3``（``SaveFile.es3``
是设置文件、``saves_index.es3`` 是索引，均不视为存档槽位）。

解析分三层：
1. parse_lenient() 宽容解析（详见 lenient_json 模块）；
2. ``playerStore`` / ``storeStation`` 两个根对象 → 核心字段提取；
3. 库存字段（``*InvJSON``）是**字符串形式的嵌套 JSON**，逐层解析后
   提取为 InventoryItem 列表。

GameSnapshot.raw 保留完整解析树（深字段备用），to_dict() 默认不导出 raw
以控制体积。
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from game_agent.statewatch.lenient_json import parse_lenient

__all__ = [
    "SaveFileInfo",
    "InventoryItem",
    "GameSnapshot",
    "list_saves",
    "find_latest_save",
    "parse_save",
    "build_snapshot",
]

# save_16.es3 / save_3.es3 ……（排除 SaveFile.es3 与 saves_index.es3）
SAVE_NAME_RE = re.compile(r"^save_(\d+)\.es3$", re.IGNORECASE)

_INV_SUFFIX = "InvJSON"


# ---------------------------------------------------------------- 存档文件定位


@dataclass
class SaveFileInfo:
    """一个存档槽位文件的基本信息。"""

    slot: int
    path: Path
    mtime: float
    size: int

    @property
    def age_seconds(self) -> float:
        """距上次修改的秒数（判断快照新鲜度）。"""
        return max(0.0, time.time() - self.mtime)

    def to_dict(self) -> dict[str, Any]:
        return {
            "slot": self.slot,
            "path": str(self.path),
            "mtime": self.mtime,
            "size": self.size,
            "age_seconds": round(self.age_seconds, 1),
        }


def list_saves(saves_dir: str | Path) -> list[SaveFileInfo]:
    """列出目录内全部存档槽位（按槽位号升序）；目录不存在返回空表。"""
    directory = Path(saves_dir)
    if not directory.is_dir():
        return []
    infos: list[SaveFileInfo] = []
    for name in directory.iterdir():
        m = SAVE_NAME_RE.match(name.name)
        if not m:
            continue
        try:
            st = name.stat()
        except OSError:
            continue
        infos.append(SaveFileInfo(slot=int(m.group(1)), path=name, mtime=st.st_mtime, size=st.st_size))
    infos.sort(key=lambda i: i.slot)
    return infos


def find_latest_save(saves_dir: str | Path) -> Optional[SaveFileInfo]:
    """返回最近修改的存档槽位；没有存档时返回 None。"""
    infos = list_saves(saves_dir)
    if not infos:
        return None
    return max(infos, key=lambda i: i.mtime)


# ---------------------------------------------------------------- 状态提取


@dataclass
class InventoryItem:
    """库存中的一件物品（从存档 JSON 提取的精简视图）。"""

    name: str
    identifier: str = ""
    unit_value: Optional[int] = None
    unit_count: Optional[int] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "identifier": self.identifier,
            "unit_value": self.unit_value,
            "unit_count": self.unit_count,
        }


@dataclass
class GameSnapshot:
    """某个时刻的游戏状态快照（来源：一次存档解析）。"""

    # —— 溯源信息 ——
    path: str
    slot: Optional[int]
    mtime: float
    parsed_at: str

    # —— 核心状态 ——
    cash: Optional[int] = None
    run_number: Optional[int] = None
    day: Optional[int] = None
    day_until_rent: Optional[int] = None
    rent_value: Optional[int] = None
    day_until_loan: Optional[int] = None
    loan_value: Optional[int] = None
    is_power_on: Optional[bool] = None
    garbage_level: Optional[int] = None
    happiness: Optional[int] = None
    order: Optional[int] = None
    traffic: Optional[int] = None
    store_state: Optional[int] = None
    reputations: list[dict[str, Any]] = field(default_factory=list)
    inventories: dict[str, list[InventoryItem]] = field(default_factory=dict)

    # —— 完整解析树（深字段备用；默认不导出）——
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def age_seconds(self) -> float:
        """快照相对文件修改时间的年龄（秒）。"""
        return max(0.0, time.time() - self.mtime)

    def inventory_count(self, name: str = "main") -> int:
        """指定库存的件数（默认主库存 main）。"""
        return len(self.inventories.get(name, []))

    def to_dict(self, include_raw: bool = False) -> dict[str, Any]:
        data: dict[str, Any] = {
            "path": self.path,
            "slot": self.slot,
            "mtime": self.mtime,
            "age_seconds": round(self.age_seconds, 1),
            "parsed_at": self.parsed_at,
            "cash": self.cash,
            "run_number": self.run_number,
            "day": self.day,
            "day_until_rent": self.day_until_rent,
            "rent_value": self.rent_value,
            "day_until_loan": self.day_until_loan,
            "loan_value": self.loan_value,
            "is_power_on": self.is_power_on,
            "garbage_level": self.garbage_level,
            "happiness": self.happiness,
            "order": self.order,
            "traffic": self.traffic,
            "store_state": self.store_state,
            "reputations": self.reputations,
            "inventories": {
                key: [item.to_dict() for item in items]
                for key, items in self.inventories.items()
            },
        }
        if include_raw:
            data["raw"] = self.raw
        return data


def _unwrap(value: Any) -> Any:
    """解包 ES3 的 ``{"__type": ..., "value": ...}`` 包装；普通值原样返回。"""
    if isinstance(value, dict) and "__type" in value and "value" in value:
        return value["value"]
    return value


def _extract_items(doc: Any) -> list[InventoryItem]:
    """从某个库存 JSON 文档提取物品列表（兼容 saveItems 包装与裸列表）。"""
    if isinstance(doc, dict):
        raw_items = doc.get("saveItems") or []
    elif isinstance(doc, list):
        raw_items = doc
    else:
        raw_items = []

    items: list[InventoryItem] = []
    for entry in raw_items:
        if not isinstance(entry, dict):
            continue
        items.append(
            InventoryItem(
                name=str(entry.get("name") or entry.get("identifier") or "?"),
                identifier=str(entry.get("identifier") or ""),
                unit_value=entry.get("unitValue"),
                unit_count=entry.get("unitCount"),
            )
        )
    return items


def build_snapshot(
    data: dict[str, Any],
    *,
    path: Optional[Path] = None,
    mtime: Optional[float] = None,
) -> GameSnapshot:
    """从解析后的存档数据树构建 GameSnapshot（与文件 IO 分离，便于测试）。"""
    store = _unwrap(data.get("playerStore")) or {}
    station = _unwrap(data.get("storeStation")) or {}
    if not isinstance(store, dict):
        store = {}
    if not isinstance(station, dict):
        station = {}

    # 槽位：优先存档内字段，其次文件名
    slot: Optional[int] = store.get("saveSlotId")
    if slot is None and path is not None:
        m = SAVE_NAME_RE.match(path.name)
        if m:
            slot = int(m.group(1))

    # 库存：所有以 InvJSON 结尾的字段都是「字符串形式的 JSON」
    inventories: dict[str, list[InventoryItem]] = {}
    for key, value in store.items():
        if not (key.endswith(_INV_SUFFIX) and isinstance(value, str) and value.strip()):
            continue
        try:
            doc = parse_lenient(value)
        except ValueError:
            continue  # 单个库存解析失败不影响整体快照
        inventories[key[: -len(_INV_SUFFIX)]] = _extract_items(doc)

    # 阵营声望（精简字段）
    reputations: list[dict[str, Any]] = []
    for rep in store.get("storeReputations") or []:
        if isinstance(rep, dict):
            reputations.append(
                {
                    "faction_id": rep.get("factionId"),
                    "faction": rep.get("factionDisplay"),
                    "amount": rep.get("amount"),
                }
            )

    if mtime is None:
        mtime = path.stat().st_mtime if path is not None else time.time()

    return GameSnapshot(
        path=str(path) if path is not None else "",
        slot=slot,
        mtime=mtime,
        parsed_at=datetime.now().isoformat(timespec="seconds"),
        cash=store.get("playerCash"),
        run_number=store.get("runNumber"),
        day=station.get("dayCounter"),
        day_until_rent=store.get("dayUntilRent"),
        rent_value=store.get("rentValue"),
        day_until_loan=store.get("dayUntilLoan"),
        loan_value=store.get("loanValue"),
        is_power_on=store.get("isPowerOn"),
        garbage_level=store.get("garbageLevel"),
        happiness=station.get("happiness"),
        order=station.get("order"),
        traffic=station.get("traffic"),
        store_state=store.get("storeState"),
        reputations=reputations,
        inventories=inventories,
        raw=data,
    )


def parse_save(path: str | Path) -> GameSnapshot:
    """读取并解析一个存档文件，返回 GameSnapshot。

    Raises:
        OSError: 文件不可读。
        ValueError: 内容无法解析（宽容修复后仍失败）。
    """
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    data = parse_lenient(text)
    if not isinstance(data, dict):
        raise ValueError(f"存档顶层结构不是对象：{p}")
    return build_snapshot(data, path=p)
