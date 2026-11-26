"""v0.9.0「UI 按钮化」模块测试（全离线）。

背景（用户 2026-10-16 指令）：操作方式从「直调底层方法/变量」转型为
「操作玩家一般操作的地方（UI 按钮）」——桥接新增 click_button 动作 +
GET /ui/buttons 按钮枚举接口；本文件覆盖 Python 侧契约。

覆盖点：
1. click_button 进入桥接动作白名单（client.ACTIONS）
2. schema 校验：name / text 至少其一；回归错误路径
3. BridgeClient.get_ui_buttons 转发 GET /ui/buttons
"""
from __future__ import annotations

import pytest

from game_agent.brain.schema import BRIDGE_ACTION_PARAMS, validate_decision
from game_agent.bridge.client import ACTIONS, BridgeClient


def _decision(action: str, params: dict | None = None) -> dict:
    return {"action": action, "params": params or {}, "reason": "t", "confidence": 1.0}


class TestClickButtonContract:
    def test_click_button_in_actions(self):
        assert "click_button" in ACTIONS
        assert "click_button" in BRIDGE_ACTION_PARAMS

    def test_schema_accepts_name(self):
        result = validate_decision(
            _decision("click_button", {"name": "BtnShop"}),
            extra_actions={"click_button": BRIDGE_ACTION_PARAMS["click_button"]},
        )
        assert result["action"] == "click_button"
        assert result["params"]["name"] == "BtnShop"

    def test_schema_accepts_text(self):
        result = validate_decision(
            _decision("click_button", {"text": "下一位"}),
            extra_actions={"click_button": BRIDGE_ACTION_PARAMS["click_button"]},
        )
        assert result["params"]["text"] == "下一位"

    def test_schema_rejects_empty(self):
        with pytest.raises(ValueError):
            validate_decision(
                _decision("click_button", {}),
                extra_actions={"click_button": BRIDGE_ACTION_PARAMS["click_button"]},
            )

    def test_schema_strips_whitespace(self):
        result = validate_decision(
            _decision("click_button", {"name": "  BtnX  "}),
            extra_actions={"click_button": BRIDGE_ACTION_PARAMS["click_button"]},
        )
        assert result["params"]["name"] == "BtnX"


class TestGetUiButtons:
    def test_forwards_get_ui_buttons(self, monkeypatch):
        """get_ui_buttons 应请求 GET /ui/buttons 并原样返回 payload。"""
        payload = {"ok": True, "buttons": [{"name": "B", "text": "确定"}], "count": 1}
        calls = []

        def fake_request(self, method, path, body=None, **kwargs):
            calls.append((method, path))
            return payload

        monkeypatch.setattr(BridgeClient, "_request", fake_request)
        client = BridgeClient("http://127.0.0.1:1", timeout=0.1)
        result = client.get_ui_buttons()
        assert calls == [("GET", "/ui/buttons")]
        assert result["buttons"][0]["name"] == "B"


class TestWholesaleFlowPrompt:
    def test_wholesale_flow_mentions_transfer_button(self):
        """v0.9.9：批发标准流程必须写清「先点转移按钮刷新台面」。"""
        from game_agent.brain.prompt import BRIDGE_ACTION_TOOLS

        assert "transferButton" in BRIDGE_ACTION_TOOLS
        assert "AcceptWholesale" in BRIDGE_ACTION_TOOLS
