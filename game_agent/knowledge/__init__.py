"""机制知识库：加载、缓存与检索入口。

配置（config/agent.json 的 knowledge 段）：

    "knowledge": {
      "enabled": false,
      "dir": "knowledge/mechanics",
      "max_cards": 3,
      "max_chars": 1800,
      "min_score": 3.0
    }

默认关闭：卡片库还在建设中，开启前请先用 `python -m game_agent.cli knowledge --validate`
校验，并用 `--select` 在真实局面上检查命中的卡片是否合理。开启后每步最多注入
max_chars 个字符的机制提示，检索结果会写进 step 记录的 "mechanics" 字段。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable, Optional

from game_agent.config import project_root
from game_agent.knowledge.cards import MechanicCard, load_cards
from game_agent.knowledge.router import MechanicRouter, render_cards, summarize_coverage

__all__ = [
    "MechanicCard",
    "MechanicRouter",
    "get_router",
    "mechanic_block",
    "reload_router",
    "summarize_coverage",
    "render_cards",
]

_DEFAULT_CFG = {
    "enabled": False,
    "dir": "knowledge/mechanics",
    "max_cards": 3,
    "max_chars": 1800,
    "min_score": 3.0,
}

# 缓存：(目录, 最新 mtime) -> router；改卡片文件即时生效，无需重启进程
_CACHE: dict[str, Any] = {"key": None, "router": None}


def _knowledge_dir(config: dict[str, Any]) -> Path:
    cfg = {**_DEFAULT_CFG, **(config.get("knowledge") or {})}
    path = Path(str(cfg["dir"]))
    return path if path.is_absolute() else project_root() / path


def _latest_mtime(directory: Path) -> float:
    latest = 0.0
    if directory.is_dir():
        for p in directory.rglob("*.json"):
            try:
                latest = max(latest, p.stat().st_mtime)
            except OSError:
                continue
    return latest


def get_router(config: dict[str, Any]) -> Optional[MechanicRouter]:
    """按配置返回路由器；未启用 / 目录不存在时返回 None。"""
    cfg = {**_DEFAULT_CFG, **(config.get("knowledge") or {})}
    if not cfg.get("enabled"):
        return None
    directory = _knowledge_dir(config)
    if not directory.is_dir():
        return None
    key = (str(directory), round(_latest_mtime(directory), 3))
    if _CACHE["key"] != key:
        cards = load_cards(directory)
        _CACHE["router"] = MechanicRouter(
            cards,
            max_cards=max(1, int(cfg.get("max_cards") or 3)),
            max_chars=max(200, int(cfg.get("max_chars") or 1800)),
            min_score=float(cfg.get("min_score") or 0),
        )
        _CACHE["key"] = key
    return _CACHE["router"]


def reload_router() -> None:
    """强制下次重新加载（测试/运营改卡后使用）。"""
    _CACHE["key"] = None
    _CACHE["router"] = None


def mechanic_block(
    config: dict[str, Any],
    observation: Any,
    *,
    history: Optional[Iterable[dict[str, Any]]] = None,
    extra_text: str = "",
) -> tuple[str, list[str]]:
    """检索并渲染当前局面需要的机制提示；未启用返回 ("", [])。"""
    router = get_router(config)
    if router is None:
        return "", []
    return router.block(observation, history=history, extra_text=extra_text)
