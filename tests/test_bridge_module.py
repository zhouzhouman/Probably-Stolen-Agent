"""桥接模块单元测试（全离线：本地 mock HTTP 服务模拟 DeepSpaceBridge 插件）。

mock 服务实现与 bridge/ 目录下 C# 插件相同的路由契约：
    GET  /ping /state /decision · POST /action /decision

覆盖点：
- 五个端点的正常往返
- 白名单校验（客户端侧拒绝未知动作）
- 插件返回 ok:false 时的行为（不抛异常，由调用方判断）
- 异常路径：连接拒绝 / 超时 / 非法 JSON / HTTP 404
- 代理回归：http_proxy 存在时仍必须直连 127.0.0.1（沙箱环境曾踩坑）
- agent_loop 的浮层推送 best-effort 行为
"""
from __future__ import annotations

import json
import os
import threading
import time
import warnings
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import pytest

from game_agent.agent_loop import push_decision_overlay
from game_agent.bridge import ACTIONS, BridgeClient, BridgeError
from game_agent.bridge.client import format_params_text, make_bridge_client
from game_agent.config import BRIDGE_MIN_TIMEOUT_SECONDS, load_config


# ================================================================ mock 插件服务


class _BridgeState:
    """mock 服务端的可变状态（最近请求 + 当前浮层决策）。"""

    def __init__(self) -> None:
        self.last_action_body: dict[str, Any] = {}
        self.last_decision_body: dict[str, Any] = {}
        self.decision: dict[str, Any] = {"visible": True, "has_decision": False}
        self.action_should_fail = False


class _MockHandler(BaseHTTPRequestHandler):
    """按 DeepSpaceBridge 的路由契约响应；行为由 server.state 控制。"""

    def log_message(self, format, *args):  # 静音，避免污染测试输出
        pass

    # ---- 工具 ----

    def _send_json(self, payload: dict[str, Any], status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}

    # ---- 路由 ----

    def do_GET(self):
        state: _BridgeState = self.server.state  # type: ignore[attr-defined]
        if self.path == "/ping":
            self._send_json({"ok": True, "version": "0.2.0"})
        elif self.path == "/state":
            self._send_json({
                "ok": True,
                "state": {
                    "in_game": True,
                    "scene": "Store",
                    "cash": 550,
                    "store_state": "OPEN",
                    "shutter_closed": False,
                    "client": None,
                    "dialog": {"is_end": True, "is_choice": False},
                },
            })
        elif self.path == "/decision":
            self._send_json({"ok": True, "decision": state.decision})
        elif self.path == "/slow":
            time.sleep(2.0)  # 供超时测试
            self._send_json({"ok": True})
        elif self.path == "/badjson":
            body = b"NOT_JSON_AT_ALL"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self._send_json({"ok": False, "error": f"未知路由：{self.path}"}, status=404)

    def do_POST(self):
        state: _BridgeState = self.server.state  # type: ignore[attr-defined]
        body = self._read_body()
        if self.path == "/action":
            state.last_action_body = body
            # place_item 的 identifier 校验（与 C# BridgeApi 契约一致）
            if body.get("action") == "place_item" and not body.get("identifier"):
                self._send_json({"ok": False, "error": "place_item 需要 identifier 参数（物品标识，如 toothpaste）"})
                return
            if state.action_should_fail:
                self._send_json({"ok": False, "error": "PlayerStore 不可用"})
                return
            self._send_json({
                "ok": True,
                "detail": f"{body.get('action')} 已调用",
                "state": {"in_game": True, "cash": 550},
            })
        elif self.path == "/decision":
            state.last_decision_body = body
            if not body.get("action"):
                self._send_json({"ok": False, "error": "缺少 action 字段"})
                return
            state.decision = {
                "visible": True,
                "has_decision": True,
                "action": body.get("action"),
                "params_text": body.get("params_text", ""),
                "reason": body.get("reason", ""),
                "confidence": body.get("confidence", 0),
                "step": body.get("step", 0),
                "ts": body.get("ts", ""),
            }
            self._send_json({"ok": True, "detail": "决策已推送浮层"})
        else:
            self._send_json({"ok": False, "error": f"未知路由：{self.path}"}, status=404)


@pytest.fixture()
def mock_bridge():
    """启动本地 mock 插件服务，yield (client, state)。"""
    server = HTTPServer(("127.0.0.1", 0), _MockHandler)
    server.state = _BridgeState()  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    client = BridgeClient(base_url=f"http://127.0.0.1:{port}", timeout=5.0)
    yield client, server.state  # type: ignore[attr-defined]
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


# ================================================================ 正常往返


class TestBridgeRoundTrip:
    def test_ping(self, mock_bridge):
        client, _ = mock_bridge
        result = client.ping()
        assert result["ok"] is True
        assert result["version"] == "0.2.0"

    def test_is_available(self, mock_bridge):
        client, _ = mock_bridge
        assert client.is_available() is True

    def test_get_state(self, mock_bridge):
        client, _ = mock_bridge
        state = client.get_state()
        assert state["in_game"] is True
        assert state["cash"] == 550
        assert state["store_state"] == "OPEN"

    def test_post_action_ok(self, mock_bridge):
        client, state = mock_bridge
        result = client.post_action("open_shutter")
        assert result["ok"] is True
        assert state.last_action_body["action"] == "open_shutter"

    def test_post_action_with_params(self, mock_bridge):
        client, state = mock_bridge
        result = client.post_action("dialog_choose", index=2)
        assert result["ok"] is True
        assert state.last_action_body["index"] == 2

    def test_post_action_whitelist_rejected_client_side(self, mock_bridge):
        """白名单外的动作在客户端就被拒绝，不发请求。"""
        client, state = mock_bridge
        with pytest.raises(BridgeError, match="未知桥接动作"):
            client.post_action("hack_the_planet")
        assert state.last_action_body == {}

    def test_place_item_with_identifier(self, mock_bridge):
        """P4：place_item 带 identifier 参数直调（交易闭环第一步）。"""
        client, state = mock_bridge
        result = client.post_action("place_item", identifier="toothpaste")
        assert result["ok"] is True
        assert state.last_action_body == {"action": "place_item", "identifier": "toothpaste"}

    def test_place_item_without_identifier_refused_by_plugin(self, mock_bridge):
        """P4：缺 identifier 时插件侧拒绝（ok:false，不抛异常，由调用方判断）。"""
        client, state = mock_bridge
        result = client.post_action("place_item")
        assert result["ok"] is False
        assert "identifier" in result["error"]

    def test_post_action_server_refusal(self, mock_bridge):
        """插件返回 ok:false 时不抛异常（由调用方判断），符合 CLI 约定。"""
        client, state = mock_bridge
        state.action_should_fail = True
        result = client.post_action("close_shutter")
        assert result["ok"] is False
        assert "PlayerStore" in result["error"]

    def test_post_decision_payload_shape(self, mock_bridge):
        """推送 payload 必须带齐 C# 插件解析所需的全部字段。"""
        client, state = mock_bridge
        decision = {
            "action": "click",
            "params": {"x": 1223, "y": 716},
            "reason": "点击载入游戏按钮",
            "confidence": 0.92,
        }
        result = client.post_decision(decision, step=3)
        assert result["ok"] is True

        body = state.last_decision_body
        assert body["action"] == "click"
        assert body["reason"] == "点击载入游戏按钮"
        assert body["confidence"] == 0.92
        assert body["params_text"] == "x=1223 y=716"
        assert body["step"] == 3
        assert "T" in body["ts"]  # ISO 时间戳

    def test_decision_round_trip(self, mock_bridge):
        """推送后能通过 GET /decision 读回（模拟浮层状态）。"""
        client, _ = mock_bridge
        client.post_decision(
            {"action": "wait", "params": {"seconds": 2}, "reason": "等待动画", "confidence": 0.8},
            step=7,
        )
        current = client.get_decision()
        assert current["has_decision"] is True
        assert current["action"] == "wait"
        assert current["step"] == 7
        assert current["params_text"] == "seconds=2"

    def test_post_decision_requires_action(self, mock_bridge):
        client, state = mock_bridge
        with pytest.raises(BridgeError, match="action"):
            client.post_decision({"reason": "没有动作"})
        assert state.last_decision_body == {}

    def test_post_decision_tolerates_bad_confidence(self, mock_bridge):
        """confidence 非数字时归一为 0，不崩溃。"""
        client, state = mock_bridge
        result = client.post_decision({"action": "wait", "confidence": "high"})
        assert result["ok"] is True
        assert state.last_decision_body["confidence"] == 0.0


# ================================================================ 异常路径


class TestBridgeErrors:
    def test_connection_refused(self):
        """游戏未运行 → 连接拒绝 → BridgeError（错误信息含指引）。"""
        client = BridgeClient(base_url="http://127.0.0.1:59999", timeout=2.0)
        with pytest.raises(BridgeError, match="桥接不可达"):
            client.ping()

    def test_timeout(self, mock_bridge):
        client, _ = mock_bridge
        slow_client = BridgeClient(
            base_url=client.base_url.replace(":59999", ""), timeout=0.5
        )
        # 直接打 /slow（不在客户端白名单路由上，用底层 _request）
        with pytest.raises(BridgeError):
            slow_client._request("GET", "/slow")

    def test_malformed_json(self, mock_bridge):
        client, _ = mock_bridge
        with pytest.raises(BridgeError, match="不是合法 JSON"):
            client._request("GET", "/badjson")

    def test_http_404(self, mock_bridge):
        client, _ = mock_bridge
        with pytest.raises(BridgeError, match="HTTP 404"):
            client._request("GET", "/nonexistent")

    def test_is_available_swallows_error(self):
        client = BridgeClient(base_url="http://127.0.0.1:59999", timeout=2.0)
        assert client.is_available() is False


# ================================================================ 代理回归


class TestProxyBypass:
    def test_works_with_hostile_proxy_env(self, mock_bridge, monkeypatch):
        """http_proxy 指向黑洞地址时，桥接仍必须直连成功。

        回归背景：沙箱 bash 设置了 http_proxy，urllib 默认走代理，
        导致连 127.0.0.1 的桥都返回 502。客户端必须显式禁用代理。
        """
        client, _ = mock_bridge
        monkeypatch.setenv("http_proxy", "http://127.0.0.1:59998")
        monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:59998")
        monkeypatch.setenv("https_proxy", "http://127.0.0.1:59998")
        monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:59998")
        result = client.ping()
        assert result["ok"] is True


# ================================================================ 纯函数与配置


class TestHelpers:
    def test_format_params_text_basic(self):
        assert format_params_text({"x": 1223, "y": 716}) == "x=1223 y=716"

    def test_format_params_text_empty(self):
        assert format_params_text(None) == ""
        assert format_params_text({}) == ""

    def test_format_params_text_truncates(self):
        text = format_params_text({"key": "a" * 50})
        assert text.endswith("…")
        assert len(text) <= 120

    def test_make_bridge_client_from_config(self):
        config = load_config()
        client = make_bridge_client(config)
        assert client.base_url == "http://127.0.0.1:8965"
        # T2：配置值须 ≥ 服务端 RunOnMain 派发上限 15s（防重复执行）
        assert client.timeout >= BRIDGE_MIN_TIMEOUT_SECONDS
        assert client.timeout == float(config["bridge"]["timeout_seconds"])

    def test_make_bridge_client_default(self):
        client = make_bridge_client({})
        assert client.base_url == "http://127.0.0.1:8965"


# ================================================================ 桥接超时配置（T2）


class TestBridgeTimeoutConfig:
    """T2：客户端超时必须大于服务端 RunOnMain 派发上限 15s，否则倒挂。

    倒挂后果：客户端超时放弃后服务端仍执行动作，AI 重试导致重复执行
    （重复扣费/成交）。
    """

    def test_agent_json_timeout_not_below_server_limit(self):
        """仓库配置（config/agent.json）的 timeout_seconds 必须 ≥ 15。"""
        config = load_config()
        timeout = (config.get("bridge") or {}).get("timeout_seconds")
        assert timeout is not None, "config/agent.json 缺少 bridge.timeout_seconds"
        assert float(timeout) >= BRIDGE_MIN_TIMEOUT_SECONDS

    def test_load_config_warns_when_timeout_below_limit(self, tmp_path):
        """配置值 < 15 时 load_config 输出警告（不阻断、不修改配置值）。"""
        cfg_path = tmp_path / "agent.json"
        cfg_path.write_text(
            json.dumps({"bridge": {"timeout_seconds": 10}}), encoding="utf-8"
        )
        with pytest.warns(UserWarning, match="15"):
            config = load_config(cfg_path)
        # 警告只提示，不覆盖用户显式配置
        assert config["bridge"]["timeout_seconds"] == 10

    def test_load_config_no_warning_at_recommended_value(self, tmp_path):
        """达到推荐值 20 时不产生任何警告。"""
        cfg_path = tmp_path / "agent.json"
        cfg_path.write_text(
            json.dumps({"bridge": {"timeout_seconds": 20}}), encoding="utf-8"
        )
        with warnings.catch_warnings():
            warnings.simplefilter("error")  # 有任何警告即失败
            config = load_config(cfg_path)
        assert config["bridge"]["timeout_seconds"] == 20

    def test_load_config_no_warning_when_section_absent(self, tmp_path):
        """未配置 bridge 段时不警告（走客户端默认 20s）。"""
        cfg_path = tmp_path / "agent.json"
        cfg_path.write_text(json.dumps({"game": {}}), encoding="utf-8")
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            load_config(cfg_path)

    def test_make_bridge_client_uses_config_timeout(self):
        client = make_bridge_client({"bridge": {"timeout_seconds": 20}})
        assert client.timeout == 20.0


# ================================================================ 主循环集成


class TestOverlayPushIntegration:
    def test_push_skips_when_bridge_down(self):
        """桥不可达时静默跳过（返回 False），绝不抛异常阻断主循环。"""
        config = {"bridge": {"base_url": "http://127.0.0.1:59999", "timeout_seconds": 2}}
        ok = push_decision_overlay(
            config, {"action": "click", "params": {"x": 1, "y": 2}, "reason": "测试"}
        )
        assert ok is False

    def test_push_succeeds_against_mock(self, mock_bridge):
        client, state = mock_bridge
        config = {"bridge": {"base_url": client.base_url, "timeout_seconds": 5}}
        ok = push_decision_overlay(
            config,
            {"action": "press_key", "params": {"key": "escape"}, "reason": "关闭面板", "confidence": 0.9},
            step=1,
        )
        assert ok is True
        assert state.decision["has_decision"] is True
        assert state.decision["action"] == "按键"  # v0.7.3 浮层全中文
