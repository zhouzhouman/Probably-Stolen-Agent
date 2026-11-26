"""游戏持久化目录定位。

Unity 游戏把运行日志与存档写到固定路径：

    %USERPROFILE%/AppData/LocalLow/<公司名>/<产品名>/

本游戏为 Questing Goose Studio / Probably Stolen。解析优先级：

1. 显式传入的 path（CLI --data-dir）；
2. 配置 statewatch.data_dir（config/agent.json）；
3. 环境变量 GAME_AGENT_DATA_DIR；
4. 按「公司名 / 产品名」自动推导（可用 game.unity_company /
   game.unity_product 覆盖）。

另提供 find_local_appdata() 以便将来支持其他 %LOCALAPPDATA% 位置。
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Optional

__all__ = [
    "find_local_appdata",
    "default_data_dir",
    "resolve_data_dir",
]

DEFAULT_COMPANY = "Questing Goose Studio"
DEFAULT_PRODUCT = "Probably Stolen"


def find_local_appdata() -> Optional[Path]:
    """定位 Windows 的 LocalLow 目录（%USERPROFILE%/AppData/LocalLow）。"""
    userprofile = os.environ.get("USERPROFILE")
    if userprofile:
        candidate = Path(userprofile) / "AppData" / "LocalLow"
        if candidate.is_dir():
            return candidate
    home = Path.home()
    candidate = home / "AppData" / "LocalLow"
    return candidate if candidate.is_dir() else None


def default_data_dir(
    company: str = DEFAULT_COMPANY,
    product: str = DEFAULT_PRODUCT,
) -> Optional[Path]:
    """按「公司名/产品名」自动推导游戏持久化目录；无法推导时返回 None。"""
    locallow = find_local_appdata()
    if locallow is None:
        return None
    return locallow / company / product


def resolve_data_dir(
    config: Optional[dict[str, Any]] = None,
    override: Optional[str | Path] = None,
) -> Optional[Path]:
    """按优先级解析游戏持久化目录（不检查目录是否存在）。

    Args:
        config: 配置字典（读取 statewatch.data_dir 与 game.unity_* 覆盖项）。
        override: 最高优先级路径（如 CLI --data-dir）。
    """
    if override:
        return Path(override)

    config = config or {}
    statewatch_cfg = config.get("statewatch", {})
    configured = statewatch_cfg.get("data_dir")
    if configured:
        return Path(configured)

    env_dir = os.environ.get("GAME_AGENT_DATA_DIR")
    if env_dir:
        return Path(env_dir)

    game_cfg = config.get("game", {})
    return default_data_dir(
        company=game_cfg.get("unity_company", DEFAULT_COMPANY),
        product=game_cfg.get("unity_product", DEFAULT_PRODUCT),
    )
