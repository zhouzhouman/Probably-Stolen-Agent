"""MCP 工具服务（docs/28 双协议工具层）的契约与端到端测试。

不依赖真实游戏：用线程内假桥模拟 8965，走真实 HTTP 打一遍 MCP 协议。
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, ProxyHandler, build_opener

import pytest

from game_agent import contracts
from game_agent.bridge.client import BridgeClient
from game_agent.mcp_server import (
    MCP_CONFIG,
    MCP_TOOLS,
    MCPHandler,
    PROTOCOL_VERSION,
    handle_jsonrpc,
    _verify_change,
)

OPENER = build_opener(ProxyHandler({}))


class _FakeBridge(BaseHTTPRequestHandler):
    state: dict[str, Any] = {
        "scene": "ShopScene",
        "day": 3,
        "cash": 500,
        "store_state": "OPEN",
        "client": {"identifier": "顾客"},
        "inventory": {"owned": [{"identifier": "滤水器"}], "not_owned": []},
        "night": {"visit_left": 1},
    }
    actions: list[dict[str, Any]] = []

    def log_message(self, *args: Any) -> None:
        return

    def _send(self, obj: dict[str, Any]) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        self._send({"ok": True, "version": "fake", "game_version": "0.46D"}
                   if self.path == "/ping" else {"ok": True, "state": dict(self.state)})

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        payload = json.loads(self.rfile.read(length) or b"{}")
        type(self).actions.append(payload)
        self._send({"ok": True, "detail": "done"})


@pytest.fixture()
def servers():
    _FakeBridge.actions.clear()
    bridge = ThreadingHTTPServer(("127.0.0.1", 0), _FakeBridge)
    bt = threading.Thread(target=bridge.serve_forever, daemon=True)
    bt.start()
    port = bridge.server_address[1]

    token = "test-token-abcdef"
    handler = type("BoundMCPHandler", (MCPHandler,), {
        "token": token,
        "bridge": BridgeClient(base_url=f"http://127.0.0.1:{port}"),
        "allowed_host": f"127.0.0.1:{MCP_CONFIG['default_port']}",
    })
    mcp = ThreadingHTTPServer(("127.0.0.1", MCP_CONFIG["default_port"]), handler)
    mcp.daemon_threads = True
    mt = threading.Thread(target=mcp.serve_forever, daemon=True)
    mt.start()

    yield {
        "url": f"http://127.0.0.1:{MCP_CONFIG['default_port']}/mcp",
        "token": token,
        "bridge_port": port,
    }

    mcp.shutdown()
    mcp.server_close()
    bridge.shutdown()
    bridge.server_close()


def _rpc(url: str, token: str, payload: dict[str, Any], *, auth: bool = True,
         host: str | None = None) -> tuple[int, dict[str, Any] | None]:
    headers = {"Content-Type": "application/json"}
    if auth:
        headers["Authorization"] = f"Bearer {token}"
    if host:
        headers["Host"] = host
    req = Request(url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
    try:
        with OPENER.open(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except HTTPError as exc:
        return exc.code, None


# ------------------------------ 纯函数层 ------------------------------


def test_tools_schema_derived_from_contracts():
    names = [t["name"] for t in MCP_TOOLS["tools"]]
    assert names == ["game_state", "game_action"]
    action_tool = MCP_TOOLS["tools"][1]
    enum = set(action_tool["inputSchema"]["properties"]["action"]["enum"])
    assert enum == contracts.derive_model_callable()
    assert "force_dismiss" not in enum
    assert "expedition_send" not in enum


def test_initialize_and_ping():
    client = BridgeClient(base_url="http://127.0.0.1:1")
    reply = handle_jsonrpc({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                            "params": {"protocolVersion": PROTOCOL_VERSION}}, client)
    assert reply["result"]["protocolVersion"] == PROTOCOL_VERSION
    assert reply["result"]["serverInfo"]["name"] == "deepspace-bridge"
    assert handle_jsonrpc({"jsonrpc": "2.0", "id": 2, "method": "ping"}, client)["result"] == {}


def test_notification_returns_none():
    assert handle_jsonrpc({"jsonrpc": "2.0", "method": "ping"}, BridgeClient()) is None


def test_tools_list_contract():
    reply = handle_jsonrpc({"jsonrpc": "2.0", "id": 3, "method": "tools/list"}, BridgeClient())
    assert [t["name"] for t in reply["result"]["tools"]] == ["game_state", "game_action"]


def test_verify_change_reports_actual_deltas():
    before = {"cash": 500, "day": 3, "inventory": {"owned": [{"identifier": "a"}]}}
    after = {"cash": 450, "day": 3, "inventory": {"owned": []}}
    verdict = _verify_change({"state": before}, {"state": after})
    assert verdict["cash"]["changed"] is True
    assert verdict["day"]["changed"] is False
    assert verdict["inventory_count"]["changed"] is True
    assert verdict["any_change"] is True


def test_game_action_scene_guard():
    from game_agent.mcp_server import _call_game_action
    client = BridgeClient(base_url="http://127.0.0.1:1")
    # 通过猴子补丁让 get_state 返回固定场景
    client.get_state = lambda: {"ok": True, "state": {"scene": "OtherScene", "cash": 1}}  # type: ignore[method-assign]
    out = _call_game_action(client, {"action": "call_next", "scene": "ShopScene"})
    assert out["ok"] is False
    assert "场景已变化" in out["error"]


def test_game_action_rejects_unknown():
    from game_agent.mcp_server import _call_game_action
    out = _call_game_action(BridgeClient(), {"action": "no_such_action"})
    assert out["ok"] is False


# ------------------------------ 端到端 HTTP ------------------------------


def test_http_auth_and_host_guard(servers):
    payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    assert _rpc(servers["url"], servers["token"], payload, auth=False)[0] == 401
    assert _rpc(servers["url"], servers["token"], payload,
                host="evil.example")[0] == 403


def test_http_tools_and_state(servers):
    status, reply = _rpc(servers["url"], servers["token"],
                         {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert status == 200 and len(reply["result"]["tools"]) == 2

    status, reply = _rpc(servers["url"], servers["token"], {
        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {"name": "game_state", "arguments": {"section": "night"}},
    })
    assert status == 200
    result = reply["result"]
    assert result["structuredContent"]["ok"] is True
    assert result["structuredContent"]["night"]["visit_left"] == 1
    assert "content" in result


def test_http_action_with_verify(servers):
    status, reply = _rpc(servers["url"], servers["token"], {
        "jsonrpc": "2.0", "id": 3, "method": "tools/call",
        "params": {"name": "game_action",
                   "arguments": {"action": "call_next", "scene": "ShopScene"}},
    })
    assert status == 200
    result = reply["result"]["structuredContent"]
    assert result["ok"] is True and result["dispatched"] is True
    assert result["verified"]["day"]["changed"] is False
    assert _FakeBridge.actions[-1] == {"action": "call_next"}


def test_http_unknown_tool(servers):
    status, reply = _rpc(servers["url"], servers["token"], {
        "jsonrpc": "2.0", "id": 4, "method": "tools/call",
        "params": {"name": "game_nope", "arguments": {}},
    })
    assert status == 200 and reply["error"]["code"] == -32602


def test_http_scene_guard_rejects_stale_action(servers):
    """执行安全三件套②：陈旧 scene 必须在 HTTP 层被拒，且不触发桥调用。"""
    before_count = len(_FakeBridge.actions)
    status, reply = _rpc(servers["url"], servers["token"], {
        "jsonrpc": "2.0", "id": 5, "method": "tools/call",
        "params": {"name": "game_action",
                   "arguments": {"action": "call_next", "scene": "StaleScene"}},
    })
    assert status == 200
    data = reply["result"]["structuredContent"]
    assert data["ok"] is False and "场景已变化" in data["error"]
    assert len(_FakeBridge.actions) == before_count  # 关键：拒绝时绝不下发动作


def test_http_action_without_scene_still_dispatches(servers):
    """scene 是可选守卫：不传时仍可执行（兼容简单客户端）。"""
    status, reply = _rpc(servers["url"], servers["token"], {
        "jsonrpc": "2.0", "id": 6, "method": "tools/call",
        "params": {"name": "game_action", "arguments": {"action": "open_shutter"}},
    })
    assert status == 200 and reply["result"]["structuredContent"]["dispatched"] is True


def test_no_sensitive_endpoints_exposed():
    """MCP 工具不得暴露 /modelconfig（含 Key）、/agent/start 等敏感端点。"""
    from game_agent.mcp_server import _TOOL_DISPATCH

    assert set(_TOOL_DISPATCH) == {"game_state", "game_action"}
    serialized = json.dumps(MCP_TOOLS, ensure_ascii=False)
    assert "modelconfig" not in serialized
    assert "api_key" not in serialized
    assert "agent/start" not in serialized
