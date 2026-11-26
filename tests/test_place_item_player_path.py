"""place_item 玩家路径优先（Ctrl+点击真上桌）与回退逻辑的单元测试。

不连游戏：monkeypatch BridgeClient._request，模拟桥的响应序列。
"""

from __future__ import annotations

from typing import Any

import pytest

from game_agent.bridge.client import BridgeClient


def _client() -> BridgeClient:
    return BridgeClient(base_url="http://127.0.0.1:1")


def test_place_item_player_path_success(monkeypatch):
    """ui_item_point → player_click(LeftControl) → 回读确认 → 返回 path=player。"""
    c = _client()
    calls: list[tuple[str, dict[str, Any]]] = []
    states = iter([
        {"state": {"scene": "ShopScene", "client": {"identifier": "顾客"},
                   "table": {"negociated_item": None}}},
        {"state": {"scene": "ShopScene", "client": {"identifier": "顾客"},
                   "table": {"negociated_item": {"identifier": "toothpaste"}}}},
    ])

    def fake_request(method, path, payload=None):
        calls.append((path, payload or {}))
        if path == "/state":
            return next(states)
        action = (payload or {}).get("action")
        if action == "ui_item_point":
            return {"ok": True, "x": 700, "y": 320}
        if action == "player_click":
            return {"ok": True, "dispatched": True}
        raise AssertionError(f"unexpected {path} {payload}")

    monkeypatch.setattr(c, "_request", fake_request)
    out = c.post_action("place_item", identifier="toothpaste")

    assert out["ok"] is True and out["path"] == "player"
    click = next(p for _, p in calls if p.get("action") == "player_click")
    assert click["modifier"] == "LeftControl"
    assert click["x"] == 700 and click["y"] == 320
    # 绝不能在玩家路径成功后还发原始 place_item
    assert all(p.get("action") != "place_item" for _, p in calls)


def test_place_item_falls_back_when_not_placed(monkeypatch):
    """点了但桌上没出现该物品 → 返回 None → 回退到原 place_item。"""
    c = _client()
    calls: list[str] = []
    states = iter([
        {"state": {"scene": "ShopScene", "client": {"identifier": "顾客"},
                   "table": {"negociated_item": None}}},
        {"state": {"scene": "ShopScene", "client": {"identifier": "顾客"},
                   "table": {"negociated_item": {"identifier": "other"}}}},
    ])

    def fake_request(method, path, payload=None):
        action = (payload or {}).get("action")
        calls.append(action or path)
        if path == "/state":
            return next(states)
        if action == "ui_item_point":
            return {"ok": True, "x": 700, "y": 320}
        if action == "player_click":
            return {"ok": True, "dispatched": True}
        if action == "place_item":
            return {"ok": True, "detail": "字段标记（回退）"}
        raise AssertionError(action)

    monkeypatch.setattr(c, "_request", fake_request)
    out = c.post_action("place_item", identifier="toothpaste")

    assert out["detail"] == "字段标记（回退）"
    assert "place_item" in calls  # 确实回退了


def test_place_item_skips_player_path_without_client(monkeypatch):
    """没顾客时游戏不收上桌，直接走原实现（省掉两次无效调用）。"""
    c = _client()
    calls: list[str] = []

    def fake_request(method, path, payload=None):
        action = (payload or {}).get("action")
        calls.append(action or path)
        if path == "/state":
            return {"state": {"scene": "ShopScene", "client": None,
                              "table": {"negociated_item": None}}}
        if action == "place_item":
            return {"ok": False, "error": "没有顾客"}
        raise AssertionError(action)

    monkeypatch.setattr(c, "_request", fake_request)
    out = c.post_action("place_item", identifier="toothpaste")

    assert out["ok"] is False
    assert "ui_item_point" not in calls


def test_place_item_falls_back_when_item_obscured(monkeypatch):
    """ui_item_point 返回失败（物品被遮挡）→ 不发点击，直接回退。"""
    c = _client()
    calls: list[str] = []

    def fake_request(method, path, payload=None):
        action = (payload or {}).get("action")
        calls.append(action or path)
        if path == "/state":
            return {"state": {"scene": "ShopScene", "client": {"identifier": "顾客"},
                              "table": {"negociated_item": None}}}
        if action == "ui_item_point":
            return {"ok": False, "error": "被遮挡"}
        if action == "place_item":
            return {"ok": True, "detail": "回退"}
        raise AssertionError(action)

    monkeypatch.setattr(c, "_request", fake_request)
    out = c.post_action("place_item", identifier="toothpaste")

    assert out["detail"] == "回退"
    assert "player_click" not in calls
