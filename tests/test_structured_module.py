"""P2 模块单元测试：结构化观察通道 + brain 桥动作 + 事件驱动主循环（全离线）。

mock 策略：
- bridge 通道：本地 mock HTTP 服务模拟 DeepSpaceBridge 插件（/state 可动态改变，
  用于验证事件驱动的「签名变化检测」）；
- statewatch 通道：tmp_path 构造最小游戏数据目录（GameLog + save_*.es3）；
- brain：MockBrainClient + 临时决策文件（wait 动作，dry-run 下不真实睡眠）；
- artifacts：全部指向 tmp_path，不污染项目 artifacts/。

覆盖点（对应 P2 设计目标）：
1. 通道探测与优先级选择（probe_channels / pick_source）；
2. 状态签名：变化检测与缺字段同构可比（state_signature）；
3. 结构化观察组装：screen_state 推导 / available_actions / important_objects；
4. StructuredObserver：bridge 与 statewatch 两通道、落盘、异常路径；
5. brain 扩展：桥动作提示词注入、schema 白名单动态扩展与注入防御；
6. agent_loop：execute_decision 桥动作分发（dry-run / live）、事件驱动循环
   （签名变化即醒 / 无变化心跳兜底 / vision 通道不等待）。
"""
from __future__ import annotations

import copy
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Optional

import pytest

from game_agent import agent_loop
from game_agent.agent_loop import execute_decision, run_loop
from game_agent.brain.prompt import ACTION_TOOLS, BRIDGE_ACTION_TOOLS, build_decision_prompt
from game_agent.brain.schema import (
    ALLOWED_ACTIONS,
    BRIDGE_ACTION_PARAMS,
    validate_decision,
)
from game_agent.bridge.client import ACTIONS, BridgeClient
from game_agent.config import DEFAULT_CONFIG
from game_agent.vision.models import WindowInfo
from game_agent.structured.channels import (
    CHANNEL_BRIDGE,
    CHANNEL_STATEWATCH,
    CHANNEL_VISION,
    pick_source,
    probe_channels,
    state_signature,
)
from game_agent.structured.observer import (
    StructuredObserver,
    build_observation,
    derive_screen_state,
)

# ================================================================ mock 插件服务


class _MockState:
    """mock 服务端可变状态：/state 返回内容与调用计数。

    state_provider：可注入的 callable(state_calls) -> state dict，用于
    按调用序号动态改变状态（事件驱动「签名变化唤醒」测试依赖）。
    """

    def __init__(self, state: dict[str, Any]) -> None:
        self.state = state
        self.state_calls = 0
        self.state_provider: Optional[Any] = None
        self.action_log: list[dict[str, Any]] = []


class _MockHandler(BaseHTTPRequestHandler):
    """DeepSpaceBridge 路由契约的最小实现（/state 内容由 server.state 控制）。"""

    def log_message(self, format, *args):  # 静音
        pass

    def _send_json(self, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        try:
            parsed = json.loads(self.rfile.read(length).decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}

    def do_GET(self):
        state: _MockState = self.server.state  # type: ignore[attr-defined]
        if self.path == "/ping":
            self._send_json({"ok": True, "version": "0.3.0"})
        elif self.path == "/state":
            state.state_calls += 1
            if state.state_provider is not None:
                self._send_json({"ok": True, "state": state.state_provider(state.state_calls)})
            else:
                self._send_json({"ok": True, "state": state.state})
        else:
            self._send_json({"ok": False, "error": f"未知路由：{self.path}"})

    def do_POST(self):
        state: _MockState = self.server.state  # type: ignore[attr-defined]
        body = self._read_body()
        if self.path == "/action":
            state.action_log.append(body)
            action = body.get("action")
            # place_item 的 identifier 校验（与 C# BridgeApi 契约一致）
            if action == "place_item" and not body.get("identifier"):
                self._send_json({"ok": False, "error": "place_item 需要 identifier 参数（物品标识，如 toothpaste）"})
                return
            self._send_json({"ok": True, "detail": f"{action} 已调用"})
        else:
            self._send_json({"ok": False, "error": f"未知路由：{self.path}"})


# 典型营业中状态（桥 /state 同款字段）
OPEN_STATE: dict[str, Any] = {
    "in_game": True,
    "scene": "Store",
    "cash": 550,
    "store_state": "OPEN",
    "shutter_closed": False,
    "client": None,
    "dialog": {"is_end": True, "is_choice": False},
}

CLIENT_SELLING_STATE: dict[str, Any] = {
    **OPEN_STATE,
    "client": {
        "identifier": "mentor_01",
        "is_selling": True,
        "is_buying": False,
    },
    "dialog": {"is_end": False, "is_choice": False},
}

# P4：带交易桌与库存的完整状态（对应新版 /state 契约）
TABLE_EMPTY_STATE: dict[str, Any] = {
    **OPEN_STATE,
    "client": {"identifier": "spacerFoodStory", "is_buying": True, "is_selling": False},
    "dialog": {"is_end": True, "is_choice": False},
    "table": {"negociated_item": None, "player_grid_items": [], "client_grid_items": []},
    "inventory": {
        "owned": [
            {"name": "一支牙膏", "identifier": "toothpaste", "value": 15, "count": 1},
            {"name": "一卷厕纸", "identifier": "toilet_paper", "value": 25, "count": 1},
            {"name": "储藏区", "identifier": "storage_bay", "value": 300, "count": 1},
            {"name": "垃圾桶", "identifier": "trashcan", "value": 0, "count": 1},
        ],
        "not_owned": [
            {"name": "繁星果糊", "identifier": "galaxy_blend", "value": 25, "count": 1},
        ],
    },
}

TABLE_WITH_ITEM_STATE: dict[str, Any] = {
    **TABLE_EMPTY_STATE,
    "table": {
        "negociated_item": {"name": "一支牙膏", "identifier": "toothpaste", "value": 15, "count": 1},
        "negociated_value": 15,
        "player_grid_items": [
            {"name": "一支牙膏", "identifier": "toothpaste", "value": 15, "count": 1},
        ],
        "client_grid_items": [],
    },
}


@pytest.fixture()
def mock_bridge_server():
    """启动本地 mock 插件服务，yield (state, port)。"""
    server = HTTPServer(("127.0.0.1", 0), _MockHandler)
    server.state = _MockState(dict(OPEN_STATE))  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server.state, server.server_address[1]  # type: ignore[attr-defined]
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


def make_bridge_client(port: int) -> BridgeClient:
    return BridgeClient(base_url=f"http://127.0.0.1:{port}", timeout=5.0)


def make_config(tmp_path: Path, *, port: Optional[int] = None, data_dir: Optional[Path] = None,
                prefer: Optional[list[str]] = None, **structured: Any) -> dict[str, Any]:
    """构造测试用配置（桥指向 mock 服务、artifacts 指向 tmp、可指定数据目录）。"""
    cfg: dict[str, Any] = {
        "paths": {"artifacts_dir": str(tmp_path / "artifacts")},
        "structured": {
            "prefer": prefer or [CHANNEL_BRIDGE, CHANNEL_STATEWATCH, CHANNEL_VISION],
            "poll_interval": 0.01,
            "event_idle_limit": 0.2,
            **structured,
        },
    }
    if port is not None:
        cfg["bridge"] = {"base_url": f"http://127.0.0.1:{port}", "timeout_seconds": 5.0}
    if data_dir is not None:
        cfg["statewatch"] = {"data_dir": str(data_dir)}
    return cfg


def make_data_dir(base: Path) -> Path:
    """构造最小游戏数据目录（日志 + 存档），内容与 statewatch 测试同款。"""
    data_dir = base / "game_data"
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "GameLog_2026-01-01_10-00-00.txt").write_text(
        "[10:00:00] [Log] Arriving\n"
        "[10:00:02] [Log] Client Arrived\n"
        "[10:00:02] [Log] wanted2 SELL\n"
        "[10:00:30] [Log] Client Left\n",
        encoding="utf-8",
    )
    (data_dir / "save_5.es3").write_text(
        '{"playerStore":{"__type":"P","value":{"playerCash":123,"saveSlotId":5}},'
        '"storeStation":{"__type":"S","value":{"dayCounter":2}}}',
        encoding="utf-8",
    )
    return data_dir


def make_wait_mock_file(base: Path) -> Path:
    """wait 动作的 brain mock 决策文件（dry-run 下不真实睡眠）。"""
    path = base / "brain_mock.json"
    path.write_text(json.dumps({
        "action": "wait",
        "params": {"seconds": 0.05},
        "reason": "（Mock）等待画面稳定",
        "confidence": 0.9,
    }), encoding="utf-8")
    return path


# ================================================================ 1. 通道探测与选择


class TestProbeChannels:
    def test_all_available(self, mock_bridge_server, tmp_path):
        _, port = mock_bridge_server
        config = make_config(tmp_path, port=port, data_dir=make_data_dir(tmp_path))
        result = probe_channels(config, client=make_bridge_client(port))
        assert result == {"bridge": True, "statewatch": True, "vision": True}

    def test_bridge_down_statewatch_up(self, tmp_path):
        """桥不可达（占用一个必然拒绝的端口）时回退 statewatch。"""
        config = make_config(tmp_path, data_dir=make_data_dir(tmp_path))
        # 端口 1 上没有任何服务监听，连接必被拒绝
        dead_client = BridgeClient(base_url="http://127.0.0.1:1", timeout=1.0)
        result = probe_channels(config, client=dead_client)
        assert result["bridge"] is False
        assert result["statewatch"] is True
        assert result["vision"] is True

    def test_nothing_available(self, tmp_path, monkeypatch):
        # 隔离本机真实游戏数据目录（开发机上游戏确实运行过）
        monkeypatch.setenv("GAME_AGENT_DATA_DIR", str(tmp_path / "no_such_dir"))
        config = make_config(tmp_path)  # 无 bridge 段、无 data_dir
        dead_client = BridgeClient(base_url="http://127.0.0.1:1", timeout=1.0)
        result = probe_channels(config, client=dead_client)
        assert result == {"bridge": False, "statewatch": False, "vision": True}

    def test_probe_never_raises(self, tmp_path):
        """探测本身异常（客户端构造失败等）必须按不可用处理，绝不外抛。"""

        class _ExplodingClient:
            def is_available(self):
                raise RuntimeError("boom")

        config = make_config(tmp_path, data_dir=make_data_dir(tmp_path))
        result = probe_channels(config, client=_ExplodingClient())  # type: ignore[arg-type]
        assert result["bridge"] is False
        assert result["statewatch"] is True


class TestPickSource:
    def test_priority_bridge_first(self, tmp_path):
        config = make_config(tmp_path)
        probe = {"bridge": True, "statewatch": True, "vision": True}
        assert pick_source(config, probe=probe) == CHANNEL_BRIDGE

    def test_fallback_to_statewatch(self, tmp_path):
        config = make_config(tmp_path)
        probe = {"bridge": False, "statewatch": True, "vision": True}
        assert pick_source(config, probe=probe) == CHANNEL_STATEWATCH

    def test_fallback_to_vision(self, tmp_path):
        config = make_config(tmp_path)
        probe = {"bridge": False, "statewatch": False, "vision": True}
        assert pick_source(config, probe=probe) == CHANNEL_VISION

    def test_mock_forces_vision(self, tmp_path):
        """mock 语义 = 全离线：即使桥活着也强制 vision。"""
        config = make_config(tmp_path)
        probe = {"bridge": True, "statewatch": True, "vision": True}
        assert pick_source(config, probe=probe, mock=True) == CHANNEL_VISION

    def test_custom_prefer_order(self, tmp_path):
        config = make_config(tmp_path, prefer=[CHANNEL_STATEWATCH, CHANNEL_BRIDGE])
        probe = {"bridge": True, "statewatch": True, "vision": True}
        assert pick_source(config, probe=probe) == CHANNEL_STATEWATCH

    def test_config_prefer_read(self, tmp_path):
        """未显式传 prefer 时读取 structured.prefer。"""
        config = make_config(tmp_path, prefer=[CHANNEL_STATEWATCH, CHANNEL_BRIDGE])
        probe = {"bridge": True, "statewatch": True, "vision": True}
        assert pick_source(config, probe=probe) == CHANNEL_STATEWATCH

    def test_unknown_channel_ignored(self, tmp_path):
        """优先序中的未知通道名被忽略，不中断选择。"""
        config = make_config(tmp_path, prefer=["telepathy", CHANNEL_STATEWATCH])
        probe = {"bridge": True, "statewatch": True, "vision": True}
        assert pick_source(config, probe=probe) == CHANNEL_STATEWATCH

    def test_prefer_overrides_probe_gap(self, tmp_path):
        """显式 prefer 只含 vision 时，即使桥活着也走 vision。"""
        config = make_config(tmp_path)
        assert pick_source(config, prefer=[CHANNEL_VISION],
                           probe={"bridge": True, "statewatch": True}) == CHANNEL_VISION


# ================================================================ 2. 状态签名


class TestStateSignature:
    def test_extracts_key_fields(self):
        """前 20 位逐位语义固定；第 21 位（CR-X01-2）是降级证据，这里只断言其存在与类型。

        第 21 位 = 「该 state 缺失/降级/报错的全部业务块」的有序元组，会随契约块集合
        变化而抖动，因此**不写死字面值**（写死必然脆断）。
        """
        sig = state_signature(CLIENT_SELLING_STATE)
        assert sig[:20] == (
            True, "Store", "OPEN", False, 550,
            "mentor_01", True, False,
            False, False,
            None, None,  # 交易桌：桌上物品 / 议价值（该样例无 table 段）
            None, None, None,  # v0.8.0：外出 / 位置 / 可举报（该样例无 night/client 段）
            None,  # v0.8.1：顾客队列 can_next
            None,  # v0.8.2：谈判面板 nego_open
            None, None,  # v0.8.3：租约倒计时 / 库存总值（该样例无 economy 段）
            None,  # v0.9.16：夜间地图开闭态 map_open（该样例无 night 段）
        )
        assert len(sig) == 21  # CR-X01-2：v0.9.16 的 20 + 降级证据位
        assert isinstance(sig[20], tuple)

    def test_rent_countdown_change_detected(self):
        """v0.8.3：交租日切换必须唤醒循环（现金红线策略依赖该字段）。"""
        a = state_signature({**OPEN_STATE, "day_until_rent": 1})
        b = state_signature({**OPEN_STATE, "day_until_rent": 0})
        assert a != b

    def test_stock_value_change_detected(self):
        """v0.8.3：库存显著变化应触发重新决策。"""
        a = state_signature({**OPEN_STATE, "economy": {"stock_value": 100}})
        b = state_signature({**OPEN_STATE, "economy": {"stock_value": 900}})
        assert a != b

    def test_cash_change_detected(self):
        """议价过程中报价变化必须触发重新决策。"""
        a = state_signature(OPEN_STATE)
        b = state_signature({**OPEN_STATE, "cash": 999})
        assert a != b

    def test_client_swap_detected(self):
        a = state_signature(OPEN_STATE)
        b = state_signature(CLIENT_SELLING_STATE)
        assert a != b

    def test_table_item_change_detected(self):
        """P4：桌上物品出现/消失必须触发重新决策（放置成功的唤醒依据）。"""
        a = state_signature(OPEN_STATE)
        with_table = {
            **OPEN_STATE,
            "table": {
                "negociated_item": {"identifier": "toothpaste", "name": "一支牙膏", "value": 15},
                "negociated_value": 15,
            },
        }
        b = state_signature(with_table)
        assert a != b
        assert b[10] == "toothpaste"
        assert b[11] == 15

    def test_negociated_value_change_detected(self):
        """P4：议价过程中报价变化应触发重新决策。"""
        base = {
            "in_game": True,
            "table": {"negociated_item": {"identifier": "toothpaste"}, "negociated_value": 15},
        }
        a = state_signature(base)
        b = state_signature({**base, "table": {**base["table"], "negociated_value": 20}})
        assert a != b
        assert a[11] == 15 and b[11] == 20

    def test_irrelevant_field_change_ignored(self):
        """签名只收录影响决策的字段；scene 之外的无关注销不触发。"""
        a = state_signature(OPEN_STATE)
        b = state_signature({**OPEN_STATE, "rent_value": 1})
        assert a == b  # rent_value 不在签名内

    def test_missing_fields_are_none_placeholders(self):
        """字段缺失以 None 占位，保证同构可比（不会 KeyError）。"""
        sig = state_signature({"in_game": True})
        assert sig[:20] == (True, None, None, None, None, None, None, None, None, None, None, None,
                            None, None, None, None, None, None, None, None)  # v0.9.16 的 20 位
        assert len(sig) == 21  # CR-X01-2：+ 降级证据位
        assert isinstance(sig[20], tuple)

    def test_non_dict_returns_empty(self):
        assert state_signature(None) == ()
        assert state_signature("nope") == ()

    def test_signature_is_hashable(self):
        """签名需可放入 set / 可比较（事件驱动主循环依赖）。"""
        sigs = {state_signature(OPEN_STATE), state_signature(CLIENT_SELLING_STATE)}
        assert len(sigs) == 2

    def test_going_outside_detected(self):
        """v0.8.0：外出/位置变化必须触发重新决策（夜晚阶段推进的唤醒依据）。"""
        a = state_signature(OPEN_STATE)
        outside = {
            **OPEN_STATE,
            "night": {"is_outside": True, "current_location": "SCAVENGING",
                      "visit_left": 1, "map_open": True},
        }
        b = state_signature(outside)
        assert a != b
        assert b[12] is True and b[13] == "SCAVENGING"

    def test_report_window_detected(self):
        """v0.8.0：顾客变为可举报（治安部按钮点亮）必须唤醒循环——举报窗口期机不可失。"""
        a = state_signature(CLIENT_SELLING_STATE)
        wanted = {
            **CLIENT_SELLING_STATE,
            "client": {**CLIENT_SELLING_STATE["client"], "sec_button_lit": True,
                       "is_wanted": True, "report_reward": 450},
        }
        b = state_signature(wanted)
        assert a != b
        assert b[14] is True

    def test_queue_empty_detected(self):
        """v0.8.1：队列空（can_next false）必须被签名感知——触发打烊决策。"""
        a = state_signature(OPEN_STATE)
        empty = {**OPEN_STATE, "queue": {"can_next": False, "has_client": False, "count": 0}}
        b = state_signature(empty)
        assert a != b
        assert b[15] is False


# ================================================================ 3. screen_state 推导


class TestScreenStateDerivation:
    def test_not_in_game_empty_is_main_menu(self):
        assert derive_screen_state({}) == "main_menu"

    def test_not_in_game_with_data_is_loading(self):
        assert derive_screen_state({"cash": 100}) == "loading"

    def test_inventory_scene(self):
        assert derive_screen_state({"in_game": True, "scene": "InventoryScene"}) == "inventory"

    def test_market_scene(self):
        assert derive_screen_state({"in_game": True, "scene": "MarketUI"}) == "market"

    def test_upgrade_scene(self):
        assert derive_screen_state({"in_game": True, "scene": "UpgradePanel"}) == "upgrade_menu"

    def test_dialog_choice(self):
        state = {"in_game": True, "scene": "Store", "dialog": {"is_choice": True}}
        assert derive_screen_state(state) == "dialogue"

    def test_customer_offer_selling(self):
        assert derive_screen_state({"in_game": True, **CLIENT_SELLING_STATE,
                                    "scene": "Store"}) == "customer_offer"

    def test_customer_offer_buying(self):
        state = {"in_game": True, "scene": "Store",
                 "client": {"identifier": "c1", "is_buying": True}}
        assert derive_screen_state(state) == "customer_offer"

    def test_client_without_intent_is_dialogue(self):
        state = {"in_game": True, "scene": "Store", "client": {"identifier": "c1"}}
        assert derive_screen_state(state) == "dialogue"

    def test_morning_is_day_start_news(self):
        state = {"in_game": True, "scene": "Store", "store_state": "MORNING"}
        assert derive_screen_state(state) == "day_start_news"

    def test_default_shop_counter(self):
        assert derive_screen_state(OPEN_STATE) == "shop_counter"

    def test_night_map_state(self):
        """v0.8.0：夜间地图打开 → night_map。"""
        state = {**OPEN_STATE, "night": {"map_open": True, "visit_left": 1,
                                         "is_outside": False}}
        assert derive_screen_state(state) == "night_map"

    def test_outside_state(self):
        """v0.8.0：人在外面 → outside（优先于 night_map）。"""
        state = {**OPEN_STATE, "night": {"map_open": True, "visit_left": 0,
                                         "is_outside": True,
                                         "current_location": "SCAVENGING"}}
        assert derive_screen_state(state) == "outside"


# ================================================================ 4. 观察组装


class TestBuildObservation:
    def test_contract_keys(self):
        obs = build_observation(OPEN_STATE, source=CHANNEL_BRIDGE)
        assert set(obs) >= {"source", "screen_state", "available_actions",
                            "important_objects", "confidence", "visible_info"}
        assert obs["source"] == "bridge"
        assert obs["confidence"] == 1.0  # 内存直读，不存在看错

    def test_points_are_null(self):
        """结构化观察没有截图坐标，point 一律 null。"""
        obs = build_observation(CLIENT_SELLING_STATE, source=CHANNEL_BRIDGE)
        for obj in obs["important_objects"]:
            assert obj["point"] is None

    def test_actions_closed_shutter(self):
        """日状态 CLOSED（begin_day 后）→ 引导开门营业（v0.5.1 起以 store_state 为准）。"""
        state = {**OPEN_STATE, "store_state": "CLOSED", "shutter_closed": True}
        actions = build_observation(state, source=CHANNEL_BRIDGE)["available_actions"]
        assert "开门营业" in actions

    def test_actions_open_store(self):
        actions = build_observation(OPEN_STATE, source=CHANNEL_BRIDGE)["available_actions"]
        assert "呼叫下一位顾客" in actions
        assert "关门歇业" in actions
        assert "查看库存" in actions

    def test_actions_queue_empty_goes_night(self):
        """v0.8.1：队列空且无顾客 → 提示打烊进夜晚流程，不再提示叫号（实盘空转的修复）。"""
        state = {**OPEN_STATE, "queue": {"can_next": False, "has_client": False, "count": 0}}
        actions = build_observation(state, source=CHANNEL_BRIDGE)["available_actions"]
        assert "打烊进入夜晚流程" in actions
        assert "呼叫下一位顾客" not in actions
        objects = build_observation(state, source=CHANNEL_BRIDGE)["important_objects"]
        names = {o["name"] for o in objects}
        assert "顾客队列" in names
        info = build_observation(state, source=CHANNEL_BRIDGE)["visible_info"]
        assert info["queue"]["can_next"] is False

    def test_actions_client_selling(self):
        actions = build_observation(CLIENT_SELLING_STATE, source=CHANNEL_BRIDGE)["available_actions"]
        assert "收购顾客物品" in actions
        assert "打发走当前顾客" in actions

    def test_actions_client_buying(self):
        state = {"in_game": True, "scene": "Store",
                 "client": {"identifier": "c1", "is_buying": True}}
        actions = build_observation(state, source=CHANNEL_BRIDGE)["available_actions"]
        assert "出售物品给顾客" in actions

    def test_actions_dialog_choice(self):
        state = {"in_game": True, "scene": "Store", "dialog": {"is_choice": True}}
        actions = build_observation(state, source=CHANNEL_BRIDGE)["available_actions"]
        assert "选择对话选项" in actions

    def test_actions_not_in_game(self):
        actions = build_observation({}, source=CHANNEL_BRIDGE)["available_actions"]
        assert actions == ["进入游戏", "载入存档"]

    def test_actions_morning_has_begin_day(self):
        state = {**OPEN_STATE, "store_state": "MORNING"}
        actions = build_observation(state, source=CHANNEL_BRIDGE)["available_actions"]
        assert "开始营业" in actions

    def test_objects_contain_money_and_shutter(self):
        objects = build_observation(OPEN_STATE, source=CHANNEL_BRIDGE)["important_objects"]
        names = {obj["name"] for obj in objects}
        assert "现金" in names
        assert "卷帘门" in names

    def test_objects_client_intent(self):
        objects = build_observation(CLIENT_SELLING_STATE, source=CHANNEL_BRIDGE)["important_objects"]
        client_obj = next(o for o in objects if o["name"] == "当前顾客")
        assert "mentor_01" in client_obj["value"]
        assert "出售" in client_obj["value"]

    def test_visible_info_mapping(self):
        obs = build_observation(CLIENT_SELLING_STATE, source=CHANNEL_BRIDGE)
        info = obs["visible_info"]
        assert info["money"] == 550
        assert info["client"]["identifier"] == "mentor_01"
        assert info["store_state"] == "OPEN"
        assert info["scene"] == "Store"

    def test_actions_night_map(self):
        """v0.8.0：夜间地图打开 → 外出/睡觉选项（有剩余次数时）。"""
        state = {**OPEN_STATE, "night": {"map_open": True, "visit_left": 1,
                                         "is_outside": False}}
        actions = build_observation(state, source=CHANNEL_BRIDGE)["available_actions"]
        assert "外出-拾荒" in actions
        assert "睡觉不出门" in actions

    def test_actions_outside_scavenging(self):
        """v0.8.0：人在拾荒地点 → 返回/拾荒选项。"""
        state = {**OPEN_STATE, "night": {"map_open": True, "visit_left": 0,
                                         "is_outside": True,
                                         "current_location": "SCAVENGING"}}
        actions = build_observation(state, source=CHANNEL_BRIDGE)["available_actions"]
        assert "返回商店" in actions
        assert "开始拾荒" in actions

    def test_actions_report_window(self):
        """v0.8.0：可举报顾客在场 → 举报选项高优先出现。"""
        state = {**OPEN_STATE, "client": {"identifier": "wanted3", "is_wanted": True,
                                          "report_reward": 450, "can_report": True,
                                          "sec_button_lit": True}}
        actions = build_observation(state, source=CHANNEL_BRIDGE)["available_actions"]
        assert "举报当前顾客（拿悬赏）" in actions

    def test_actions_aug_client(self):
        """v0.8.0：伪人在场 → 击毙选项。"""
        state = {**OPEN_STATE, "client": {"identifier": "aug_01", "is_aug": True}}
        actions = build_observation(state, source=CHANNEL_BRIDGE)["available_actions"]
        assert "击毙伪人（射击模式）" in actions

    def test_visible_info_services_night_phone(self):
        """v0.8.0：visible_info 带 services/night/phone 三段（夜晚流程决策依据）。"""
        state = {
            **OPEN_STATE,
            "services": [
                {"id": "POWER_SERVICE", "name": "电力服务", "cost": 5, "unlocked": True,
                 "introduced": True, "is_using": True, "mandatory": False, "cooldown": 0},
                {"id": "INSURANCE_SERVICE", "name": "保险服务", "cost": 4, "unlocked": True,
                 "introduced": False, "is_using": False, "mandatory": False, "cooldown": 0},
            ],
            "night": {"visit_left": 1, "is_outside": False, "current_location": "STORE",
                      "seriously_wounded": False, "scavenging_attempts": 0},
            "phone": {"open": True, "book_shown": False, "current_client": None,
                      "contacts": [{"number": 8376, "name": "Joe Wild's Assistant",
                                    "state": "Responding", "cooldown": 0}]},
        }
        info = build_observation(state, source=CHANNEL_BRIDGE)["visible_info"]
        # 未介绍的服务被过滤，只留电力服务
        assert info["services"] == [
            {"id": "POWER_SERVICE", "name": "电力服务", "cost": 5, "is_using": True,
             "mandatory": False, "cooldown": 0},
        ]
        assert info["night"]["visit_left"] == 1
        assert info["night"]["seriously_wounded"] is False
        assert info["phone"]["open"] is True
        assert info["phone"]["contacts"][0]["number"] == 8376

    def test_objects_wanted_client(self):
        """v0.8.0：通缉犯/伪人顾客生成「高危顾客」对象（悬赏与代价可见）。"""
        state = {**OPEN_STATE, "client": {"identifier": "wanted3", "is_wanted": True,
                                          "report_reward": 450, "reputation_penalty": 20,
                                          "can_report": True, "sec_button_lit": True}}
        objects = build_observation(state, source=CHANNEL_BRIDGE)["important_objects"]
        danger = next((o for o in objects if o["name"] == "高危顾客"), None)
        assert danger is not None
        assert "450" in danger["value"]
        assert "声望-20" in danger["value"]

    def test_visible_info_economy_and_rent(self):
        """v0.8.3：economy / rent 段进入观察契约。

        此前 economy 完全缺失，模型拿不到 retail_markup / stock_value，
        只能靠猜判断进货是否划算（曾批量买入 JUNK 垃圾货）。
        """
        state = {
            **OPEN_STATE,
            "cash": 703,
            "day_until_rent": 1,
            "rent_value": 200,
            "garbage_level": 2,
            "is_power_on": True,
            "shutter_closed": False,
            "economy": {
                "retail_markup": 15,
                "stock_value": 1443,
                "attractiveness": 250,
                "loan_days": 12,
                "loan_value": 800,
                "lottery_number": "7421",
                "wild_favor": 22,
                "insurance_days_without": 0,
                "deal_maker_bonus": 5,
            },
        }
        info = build_observation(state, source=CHANNEL_BRIDGE)["visible_info"]
        econ = info["economy"]
        assert econ["retail_markup"] == 15
        assert econ["stock_value"] == 1443
        assert econ["attractiveness"] == 250
        assert econ["loan_days"] == 12
        assert econ["lottery_number"] == "7421"
        # 未纳入的字段不应泄漏
        assert "deal_maker_bonus" not in econ

        rent = info["rent"]
        assert rent["day_until_rent"] == 1
        assert rent["rent_value"] == 200
        assert rent["garbage_level"] == 2
        assert rent["is_power_on"] is True

    def test_economy_absent_yields_empty_dict(self):
        """防御：state 无 economy 段时返回空 dict 而非 None/报错。"""
        info = build_observation(OPEN_STATE, source=CHANNEL_BRIDGE)["visible_info"]
        assert info["economy"] == {}
        # OPEN_STATE 无 day_until_rent 字段 → None 占位，rent 段本身仍存在
        assert "rent" in info
        assert info["rent"]["day_until_rent"] is None

    def test_non_dict_state_is_safe(self):
        obs = build_observation(None, source=CHANNEL_STATEWATCH)  # type: ignore[arg-type]
        assert obs["screen_state"] == "main_menu"
        assert obs["source"] == "statewatch"


# ================================================================ 4b. 交易桌观察（P4）


class TestTableObservation:
    """P4：交易桌状态进入观察契约（screen_state / actions / objects / visible_info）。"""

    def test_screen_state_negotiation_when_item_on_table(self):
        assert derive_screen_state(TABLE_WITH_ITEM_STATE) == "negotiation"

    def test_screen_state_customer_offer_when_table_empty(self):
        assert derive_screen_state(TABLE_EMPTY_STATE) == "customer_offer"

    def test_actions_place_item_when_table_empty(self):
        actions = build_observation(TABLE_EMPTY_STATE, source=CHANNEL_BRIDGE)["available_actions"]
        assert "把物品放上交易桌" in actions

    def test_actions_accept_when_item_on_table(self):
        actions = build_observation(TABLE_WITH_ITEM_STATE, source=CHANNEL_BRIDGE)["available_actions"]
        assert "接受当前报价" in actions
        assert "出售桌上物品" in actions  # 顾客在买
        assert "把物品放上交易桌" not in actions

    def test_objects_table_item(self):
        objects = build_observation(TABLE_WITH_ITEM_STATE, source=CHANNEL_BRIDGE)["important_objects"]
        table_obj = next((o for o in objects if o["name"] == "桌上物品"), None)
        assert table_obj is not None
        assert "牙膏" in table_obj["value"]
        assert "议价 15" in table_obj["value"]

    def test_no_table_object_when_empty(self):
        objects = build_observation(TABLE_EMPTY_STATE, source=CHANNEL_BRIDGE)["important_objects"]
        assert all(o["name"] != "桌上物品" for o in objects)

    def test_visible_info_inventory_filters_zero_value(self):
        info = build_observation(TABLE_EMPTY_STATE, source=CHANNEL_BRIDGE)["visible_info"]
        owned_ids = {i["identifier"] for i in info["inventory"]["owned"]}
        assert "toothpaste" in owned_ids
        assert "trashcan" not in owned_ids    # value=0 过滤（垃圾桶/档案箱等）
        # 注意：只按 value>0 过滤，家具类（如 storage_bay 300）保留，
        # 由模型结合顾客预算判断该卖什么（硬编码排除列表更脆）
        assert {i["identifier"] for i in info["inventory"]["not_owned"]} == {"galaxy_blend"}

    def test_visible_info_table_mapping(self):
        info = build_observation(TABLE_WITH_ITEM_STATE, source=CHANNEL_BRIDGE)["visible_info"]
        assert info["table"]["negociated_item"]["identifier"] == "toothpaste"
        assert info["table"]["negociated_value"] == 15

    def test_legacy_state_without_table_is_safe(self):
        """旧形状状态（无 table/inventory 段）不得报错——向后兼容。"""
        obs = build_observation(OPEN_STATE, source=CHANNEL_BRIDGE)
        assert obs["visible_info"]["table"] is None
        assert obs["visible_info"]["inventory"] == {}


# ================================================================ 4c. 顾客购买偏好观察（v0.4.1）

# v0.4.1：/state 的 client 段新增 buying_tags/buying_ids/black_tags/black_ids/budget_left，
# inventory 物品新增 item_types——AI 据此只放「顾客想收」的物品（买食物的顾客不收牙膏）
BUYING_CLIENT_STATE: dict[str, Any] = {
    **TABLE_EMPTY_STATE,
    "client": {
        "identifier": "spacerFoodStory2",
        "is_buying": True,
        "is_selling": False,
        "buying_tags": ["food", "drink"],
        "buying_ids": [],
        "black_tags": ["toiletries"],
        "black_ids": [],
        "budget_left": 88,
    },
}


class TestBuyingPreferenceObservation:
    """v0.4.1：购买偏好进入观察契约（顾客对象/透传/选品标签）。"""

    def test_client_object_shows_buying_tags(self):
        objects = build_observation(BUYING_CLIENT_STATE, source=CHANNEL_BRIDGE)["important_objects"]
        client_obj = next(o for o in objects if o["name"] == "当前顾客")
        assert "想收:food,drink" in client_obj["value"]

    def test_client_object_shows_budget(self):
        objects = build_observation(BUYING_CLIENT_STATE, source=CHANNEL_BRIDGE)["important_objects"]
        client_obj = next(o for o in objects if o["name"] == "当前顾客")
        assert "预算:88" in client_obj["value"]

    def test_selling_client_has_no_buying_suffix(self):
        """顾客在卖（非买）时不应出现「想收/预算」后缀。"""
        objects = build_observation(CLIENT_SELLING_STATE, source=CHANNEL_BRIDGE)["important_objects"]
        client_obj = next(o for o in objects if o["name"] == "当前顾客")
        assert "想收" not in client_obj["value"]
        assert "预算" not in client_obj["value"]

    def test_visible_info_client_passes_buying_fields(self):
        info = build_observation(BUYING_CLIENT_STATE, source=CHANNEL_BRIDGE)["visible_info"]
        assert info["client"]["buying_tags"] == ["food", "drink"]
        assert info["client"]["black_tags"] == ["toiletries"]
        assert info["client"]["budget_left"] == 88

    def test_compact_inventory_keeps_item_types(self):
        """库存物品保留 item_types（新契约字段；旧状态缺字段时兜底空列表）。"""
        info = build_observation(TABLE_EMPTY_STATE, source=CHANNEL_BRIDGE)["visible_info"]
        toothpaste = next(i for i in info["inventory"]["owned"] if i["identifier"] == "toothpaste")
        assert toothpaste["item_types"] == []

    def test_prompt_mentions_open_negociation_and_matching(self):
        """提示词：open_negociation 动作 + 按 buying_tags/item_types 选品的指引。"""
        assert "open_negociation" in BRIDGE_ACTION_TOOLS
        assert "buying_tags" in BRIDGE_ACTION_TOOLS
        assert "item_types" in BRIDGE_ACTION_TOOLS


# ================================================================ 5. StructuredObserver




class TestSellableNowObservation:
    """v1.1：观察层直接计算「顾客想收 ∩ 库存」清单（r59「缺 identifier」催生）。"""

    def _state(self, buying_tags):
        return {
            **TABLE_EMPTY_STATE,
            "client": {
                "identifier": "tester", "is_buying": bool(buying_tags),
                "is_selling": False, "buying_tags": buying_tags,
                "buying_ids": [], "black_tags": [], "black_ids": [],
                "budget_left": 200,
            },
            "inventory": {
                "owned": [
                    {"identifier": "bandage", "name": "绷带", "value": 10,
                     "count": 1, "item_types": ["MEDICAL"]},
                    {"identifier": "bread", "name": "面包", "value": 5,
                     "count": 1, "item_types": ["FOOD"]},
                    {"identifier": "wrench", "name": "扳手", "value": 20,
                     "count": 1, "item_types": ["TOOL", "BASIC_MATERIAL"]},
                ],
                "not_owned": [],
            },
        }

    def test_intersection_by_tag(self):
        info = build_observation(self._state(["MEDICAL", "TOOL"]), source=CHANNEL_BRIDGE)["visible_info"]
        idents = [x["identifier"] for x in info["sellable_now"]]
        assert idents == ["bandage", "wrench"]

    def test_empty_when_no_buying_tags(self):
        info = build_observation(self._state([]), source=CHANNEL_BRIDGE)["visible_info"]
        assert info["sellable_now"] == []

    def test_empty_when_no_match(self):
        info = build_observation(self._state(["JUNK"]), source=CHANNEL_BRIDGE)["visible_info"]
        assert info["sellable_now"] == []

    def test_capped_at_eight(self):
        state = self._state(["MEDICAL"])
        state["inventory"]["owned"] = [
            {"identifier": f"med_{i}", "name": f"药{i}", "value": 1,
             "count": 1, "item_types": ["MEDICAL"]}
            for i in range(12)
        ]
        info = build_observation(state, source=CHANNEL_BRIDGE)["visible_info"]
        assert len(info["sellable_now"]) == 8

    def test_prompt_mentions_sellable_now(self):
        from game_agent.brain.prompt import BRIDGE_ACTION_TOOLS as TOOLS
        assert "sellable_now" in TOOLS


class TestStructuredObserverBridge:
    def test_observe_bridge(self, mock_bridge_server, tmp_path):
        _, port = mock_bridge_server
        config = make_config(tmp_path, port=port)
        observer = StructuredObserver(config, client=make_bridge_client(port))
        record = observer.observe(CHANNEL_BRIDGE)

        assert record.source == CHANNEL_BRIDGE
        assert record.observation["source"] == CHANNEL_BRIDGE
        assert record.observation["screen_state"] == "shop_counter"
        assert record.signature[0] is True  # in_game
        assert record.signature[4] == 550  # cash

    def test_observe_bridge_saves_file(self, mock_bridge_server, tmp_path):
        _, port = mock_bridge_server
        config = make_config(tmp_path, port=port)
        record = StructuredObserver(config, client=make_bridge_client(port)).observe(CHANNEL_BRIDGE)
        assert record.file is not None
        saved = json.loads(Path(record.file).read_text(encoding="utf-8"))
        assert saved["source"] == CHANNEL_BRIDGE
        assert saved["observation"]["confidence"] == 1.0
        assert isinstance(saved["state_signature"], list)
        assert Path(record.file).parent == tmp_path / "artifacts" / "observations"

    def test_observe_bridge_no_save(self, mock_bridge_server, tmp_path):
        _, port = mock_bridge_server
        config = make_config(tmp_path, port=port)
        record = StructuredObserver(config, client=make_bridge_client(port)).observe(
            CHANNEL_BRIDGE, save=False)
        assert record.file is None

    def test_unknown_channel_rejected(self, mock_bridge_server, tmp_path):
        _, port = mock_bridge_server
        config = make_config(tmp_path, port=port)
        observer = StructuredObserver(config, client=make_bridge_client(port))
        with pytest.raises(ValueError, match="不支持通道"):
            observer.observe(CHANNEL_VISION)

    def test_client_shared_across_observes(self, mock_bridge_server, tmp_path):
        """注入的客户端被复用（不按配置重复构造）。"""
        _, port = mock_bridge_server
        config = make_config(tmp_path, port=port)
        observer = StructuredObserver(config, client=make_bridge_client(port))
        observer.observe(CHANNEL_BRIDGE, save=False)
        observer.observe(CHANNEL_BRIDGE, save=False)
        assert observer._bridge_client() is observer._client  # type: ignore[attr-defined]


class TestStructuredObserverStatewatch:
    def test_observe_statewatch(self, tmp_path):
        data_dir = make_data_dir(tmp_path)
        config = make_config(tmp_path, data_dir=data_dir)
        record = StructuredObserver(config).observe(CHANNEL_STATEWATCH)

        assert record.source == CHANNEL_STATEWATCH
        assert record.observation["source"] == CHANNEL_STATEWATCH
        # statewatch 摘要归一后现金来自存档
        assert record.state["cash"] == 123
        assert record.observation["visible_info"]["money"] == 123
        assert record.observation["visible_info"]["time_or_day"] == "第 2 天"

    def test_observe_statewatch_normalized_fields(self, tmp_path):
        """归一层把 statewatch 摘要补成桥同款字段（供 screen_state/签名复用）。"""
        data_dir = make_data_dir(tmp_path)
        config = make_config(tmp_path, data_dir=data_dir)
        record = StructuredObserver(config).observe(CHANNEL_STATEWATCH)
        assert "in_game" in record.state
        assert "client" in record.state
        assert "dialog" in record.state
        assert record.signature[0] is True  # in_game 归一为 True（有现金）

    def test_missing_data_dir_raises(self, tmp_path):
        from game_agent.errors import BridgeError

        config = make_config(tmp_path, data_dir=tmp_path / "not_exist")
        with pytest.raises(BridgeError, match="statewatch 通道不可用"):
            StructuredObserver(config).observe(CHANNEL_STATEWATCH)


# ================================================================ 6. brain 扩展


class TestBrainBridgeActions:
    STRUCT_OBS = {
        "source": "bridge",
        "screen_state": "shop_counter",
        "available_actions": ["呼叫下一位顾客"],
        "important_objects": [],
        "confidence": 1.0,
        "visible_info": {},
    }

    def test_prompt_includes_bridge_tools_when_enabled(self):
        prompt = build_decision_prompt(self.STRUCT_OBS, bridge_actions=True)
        assert "游戏 API 直调动作" in prompt
        assert "dialog_choose" in prompt
        assert "优先使用" in prompt

    def test_no_auto_enable_bridge_tools_for_structured(self):
        """回归（T1）：结构化观察不再自动追加桥动作组——由调用方按通道显式控制。

        旧行为：任何结构化观察（含 statewatch）都强制注入桥动作组；
        auto 模式选中 statewatch 等价于桥接不可达，模型按提示优先发桥动作
        必然 BridgeError，约 8 步后熔断、循环退出。
        """
        prompt = build_decision_prompt(self.STRUCT_OBS)
        assert "游戏 API 直调动作" not in prompt
        assert "call_next" not in prompt

    def test_statewatch_without_bridge_actions_excludes_bridge_tools(self):
        """回归（T1 验收）：statewatch + bridge_actions=False 不含桥动作组。"""
        prompt = build_decision_prompt(
            {**self.STRUCT_OBS, "source": "statewatch"}, bridge_actions=False
        )
        assert "游戏 API 直调动作" not in prompt
        assert "call_next" not in prompt
        # 结构化说明同步降级：不再出现任何「直调」引导
        assert "直调" not in prompt

    def test_statewatch_with_bridge_actions_includes_tools(self):
        """调用方显式开启（bridge_actions=True）时，statewatch 也注入桥动作组。"""
        prompt = build_decision_prompt(
            {**self.STRUCT_OBS, "source": "statewatch"}, bridge_actions=True
        )
        assert "游戏 API 直调动作" in prompt
        assert "call_next" in prompt
        assert "优先使用" in prompt


class TestNoMousePrompt:
    """禁鼠标模式：提示词层就不提供任何鼠标/键盘动作。"""

    STRUCT_OBS = {
        "source": "bridge",
        "screen_state": "shop_counter",
        "available_actions": [],
        "important_objects": [],
        "confidence": 1.0,
        "visible_info": {},
    }

    def test_mouse_actions_absent_from_whitelist(self):
        prompt = build_decision_prompt(self.STRUCT_OBS, bridge_actions=True, no_mouse=True)
        # 白名单段落不得出现鼠标动作的可用声明
        assert "可用动作（禁止使用鼠标与键盘" in prompt
        assert "鼠标点击。params" not in prompt
        assert "仅移动鼠标" not in prompt
        assert "按住 Ctrl 并点击" not in prompt
        assert "按键。params" not in prompt
        assert "输入 ASCII 文本" not in prompt
        # 但保留非输入类动作
        assert "- wait：等待画面加载" in prompt
        assert "- stop：停止操作" in prompt

    def test_bridge_tools_still_present(self):
        prompt = build_decision_prompt(self.STRUCT_OBS, bridge_actions=True, no_mouse=True)
        assert "游戏 API 直调动作" in prompt
        assert "call_next" in prompt

    def test_coordinate_note_removed(self):
        """禁鼠标模式下没有坐标空间，去掉坐标说明与截图尺寸。"""
        prompt = build_decision_prompt(self.STRUCT_OBS, bridge_actions=True, no_mouse=True)
        assert "【坐标规则】" not in prompt
        assert "【当前截图尺寸】" not in prompt

    def test_visual_observation_also_excludes_mouse(self):
        """视觉观察 + 禁鼠标：同样不给鼠标动作。"""
        obs = {**self.STRUCT_OBS, "source": "vision",
               "important_objects": [{"name": "按钮", "point": [100, 200]}]}
        prompt = build_decision_prompt(obs, image_size=(1920, 1080), no_mouse=True)
        assert "可用动作（禁止使用鼠标与键盘" in prompt
        assert "【坐标规则】" not in prompt

    def test_normal_mode_keeps_mouse_actions(self):
        """默认模式不受影响（向后兼容）：鼠标动作与坐标说明照旧提供。"""
        vision_obs = {**self.STRUCT_OBS, "source": "vision",
                      "important_objects": [{"name": "按钮", "point": [100, 200]}]}
        prompt = build_decision_prompt(vision_obs, image_size=(1920, 1080))
        assert "鼠标点击。params" in prompt
        assert "【坐标规则】" in prompt


class TestJunkRejectRule:
    """拒收垃圾货规则：AI 曾连续 3 次 buy_item scrap_metal（JUNK）导致现金流失。"""

    STRUCT_OBS = {
        "source": "bridge",
        "screen_state": "shop_counter",
        "available_actions": [],
        "important_objects": [],
        "confidence": 1.0,
        "visible_info": {},
    }

    def test_business_rules_include_junk_rejection(self):
        prompt = build_decision_prompt(self.STRUCT_OBS, bridge_actions=True)
        assert "拒收垃圾货" in prompt
        assert "JUNK" in prompt
        assert "scrap_metal" in prompt

    def test_buy_item_entry_warns_against_junk(self):
        """动作定义处也带禁忌——模型更容易看到 buy_item 条目。"""
        prompt = build_decision_prompt(self.STRUCT_OBS, bridge_actions=True)
        lines = prompt.splitlines()
        idx = next(i for i, ln in enumerate(lines) if ln.startswith("- buy_item"))
        # 条目正文含下一行的续行，一并检查
        entry = "\n".join(lines[idx:idx + 3])
        assert "JUNK" in entry
        assert "scrap_metal" in entry

    def test_rule_present_in_no_mouse_mode(self):
        """禁鼠标模式同样携带该规则（AI 只用接口时更需要）。"""
        prompt = build_decision_prompt(self.STRUCT_OBS, bridge_actions=True, no_mouse=True)
        assert "拒收垃圾货" in prompt
        assert "JUNK" in prompt

    def test_no_exception_clause_present(self):
        """规则必须写死「无例外」——AI 曾用「唯一交易机会」绕过禁令买 JUNK。"""
        prompt = build_decision_prompt(self.STRUCT_OBS, bridge_actions=True)
        assert "最高优先级，无例外" in prompt
        assert "不存在「唯一的交易机会」这种例外" in prompt
        # 不再留下「唯一的交易机会即可购买」的暗示
        assert "它是当前唯一的交易机会" not in prompt


class TestForbidWaitAntiStall:
    """反空转（r36）：提示词劝导对推理模型无效，必须从白名单移除 wait。

    背景：AI 连续 13/15 步选 wait，理由恒为「等待系统加载」——即使提示词
    已写「禁止连续 2 次以上 wait」，模型仍自洽地认为「还在加载」而继续等。
    """

    STRUCT_OBS = {
        "source": "bridge",
        "screen_state": "shop_counter",
        "available_actions": [],
        "important_objects": [],
        "confidence": 1.0,
        "visible_info": {},
    }

    def test_wait_absent_from_whitelist(self):
        prompt = build_decision_prompt(
            self.STRUCT_OBS, bridge_actions=True, no_mouse=True, forbid_wait=True
        )
        assert "- wait：等待画面加载" not in prompt
        assert "本轮不要输出 wait" in prompt

    def test_stop_and_bridge_actions_present(self):
        prompt = build_decision_prompt(
            self.STRUCT_OBS, bridge_actions=True, no_mouse=True, forbid_wait=True
        )
        assert "- stop：仅在确实无法安全继续时使用" in prompt
        assert "游戏 API 直调动作" in prompt
        assert "call_next" in prompt
        assert "end_day" in prompt

    def test_forbid_wait_off_keeps_wait(self):
        """未触发反空转时，wait 仍正常提供。"""
        prompt = build_decision_prompt(
            self.STRUCT_OBS, bridge_actions=True, no_mouse=True, forbid_wait=False
        )
        assert "- wait：等待画面加载" in prompt

    def test_run_loop_forces_action_after_wait_streak(self, tmp_path, monkeypatch):
        """连续 wait 达到阈值后，decide 应以 forbid_wait=True 调用。"""
        config = copy.deepcopy(DEFAULT_CONFIG)
        config["paths"]["artifacts_dir"] = str(tmp_path / "art_stall")
        captured: list[bool] = []
        # 前两步返回 wait（制造连续空转），之后返回 call_next（推进）
        seq = ["wait", "wait", "call_next", "call_next", "call_next"]

        def _fake_decide(cfg, observation, **kwargs):
            captured.append(bool(kwargs.get("forbid_wait")))
            action = seq[min(len(captured) - 1, len(seq) - 1)]
            return (
                {"action": action, "params": {}, "reason": "r", "confidence": 0.9},
                {"provider": "fake", "attempts": 1},
            )

        monkeypatch.setattr(agent_loop, "decide", _fake_decide)
        monkeypatch.setattr(agent_loop, "push_decision_overlay", lambda *a, **k: False)
        monkeypatch.setattr(agent_loop, "stop_requested", lambda cfg: len(captured) >= 5)
        fake_window = WindowInfo(hwnd=1, title="fake", rect=(0, 0, 2000, 1200))
        obs_record = {
            "observation": {
                "screen_state": "shop_counter", "available_actions": [],
                "important_objects": [], "confidence": 1.0, "visible_info": {},
            },
            "capture": {"image_size": None},
            "screenshot": None,
        }
        agent_loop.run_loop(
            config=config, max_steps=5, live=False, allow_mouse=False,
            window=fake_window, obs_override=obs_record, delay_between=0,
            push_overlay=False,
        )
        # 前 2 步 streak<2 → False；第 3 步（streak 达阈值）强制 True；
        # 该步返回 call_next 后 streak 清零 → 第 4 步回到 False（单次 wait 不强制）
        assert captured == [False, False, True, False, False]

    def test_wait_streak_resets_after_action(self, tmp_path, monkeypatch):
        """出现推进类动作后 wait 计数清零。"""
        config = copy.deepcopy(DEFAULT_CONFIG)
        config["paths"]["artifacts_dir"] = str(tmp_path / "art_reset")
        seq = ["wait", "wait", "call_next", "wait"]
        captured: list[bool] = []

        def _fake_decide(cfg, observation, **kwargs):
            captured.append(bool(kwargs.get("forbid_wait")))
            action = seq[min(len(captured) - 1, len(seq) - 1)]
            return (
                {"action": action, "params": {}, "reason": "r", "confidence": 0.9},
                {"provider": "fake", "attempts": 1},
            )

        monkeypatch.setattr(agent_loop, "decide", _fake_decide)
        monkeypatch.setattr(agent_loop, "push_decision_overlay", lambda *a, **k: False)
        monkeypatch.setattr(agent_loop, "stop_requested", lambda cfg: len(captured) >= 4)
        fake_window = WindowInfo(hwnd=1, title="fake", rect=(0, 0, 2000, 1200))
        obs_record = {
            "observation": {
                "screen_state": "shop_counter", "available_actions": [],
                "important_objects": [], "confidence": 1.0, "visible_info": {},
            },
            "capture": {"image_size": None},
            "screenshot": None,
        }
        agent_loop.run_loop(
            config=config, max_steps=4, live=False, allow_mouse=False,
            window=fake_window, obs_override=obs_record, delay_between=0,
            push_overlay=False,
        )
        # 第 3 步 call_next → streak 清零 → 第 4 步重新 False
        assert captured == [False, False, True, False]


class TestIdentifierParamsValidation:
    """T4：五个物品动作的 identifier 统一校验（防幻觉参数静默放行）。

    历史：仅 place_item 校验，sell_item / buy_item / appraise / display_case
    漏传 identifier 时被静默放行到桥接层。
    """

    ACTIONS = ["place_item", "sell_item", "buy_item", "appraise", "display_case"]

    @pytest.mark.parametrize("action", ACTIONS)
    def test_missing_identifier_rejected(self, action):
        with pytest.raises(ValueError, match="identifier"):
            validate_decision(
                {"action": action, "reason": "x", "confidence": 0.8},
                extra_actions=BRIDGE_ACTION_PARAMS,
            )

    @pytest.mark.parametrize("action", ACTIONS)
    def test_empty_identifier_rejected(self, action):
        with pytest.raises(ValueError, match="identifier"):
            validate_decision(
                {"action": action, "params": {"identifier": "   "},
                 "reason": "x", "confidence": 0.8},
                extra_actions=BRIDGE_ACTION_PARAMS,
            )

    @pytest.mark.parametrize("action", ACTIONS)
    def test_valid_identifier_trimmed(self, action):
        decision = validate_decision(
            {"action": action, "params": {"identifier": " toothpaste "},
             "reason": "x", "confidence": 0.8},
            extra_actions=BRIDGE_ACTION_PARAMS,
        )
        assert decision["params"]["identifier"] == "toothpaste"

    def test_phone_order_vendor_required(self):
        """T4：phone_order 的 vendor 必须非空。"""
        with pytest.raises(ValueError, match="vendor"):
            validate_decision(
                {"action": "phone_order", "reason": "x", "confidence": 0.8},
                extra_actions=BRIDGE_ACTION_PARAMS,
            )

    def test_phone_order_vendor_trimmed(self):
        decision = validate_decision(
            {"action": "phone_order", "params": {"vendor": " commissary "},
             "reason": "x", "confidence": 0.8},
            extra_actions=BRIDGE_ACTION_PARAMS,
        )
        assert decision["params"]["vendor"] == "commissary"


def _fake_decide(*args, **kwargs):
    """固定的「非 mock」决策源：让浮层推送分支照常执行（测试步号透传用）。"""
    return (
        {"action": "wait", "params": {"seconds": 0.0}, "reason": "测试用固定决策", "confidence": 1.0},
        {"provider": "fake", "model": "fake", "duration_ms": 0, "attempts": 1},
    )


class TestOverlayStepNumber:
    """浮层步号透传（曾恒显示 #0，无法区分不同步骤的决策）。"""

    def test_step_once_passes_step_to_overlay(self, tmp_path, monkeypatch):
        """step_once 应把 step 号透传给浮层推送。"""
        config = copy.deepcopy(DEFAULT_CONFIG)
        config["paths"]["artifacts_dir"] = str(tmp_path / "art_step")
        captured: list[Optional[int]] = []

        def _fake_push(cfg, decision, *, step=None, observation=None):
            captured.append(step)
            return True

        monkeypatch.setattr(agent_loop, "push_decision_overlay", _fake_push)
        # v2.9：mock 运行已禁止推浮层（防假决策污染真机面板），
        # 这里改用「非 mock + 假决策源」来验证步号透传。
        monkeypatch.setattr(agent_loop, "decide", _fake_decide)
        fake_window = WindowInfo(hwnd=1, title="fake", rect=(0, 0, 2000, 1200))
        obs_record = {
            "observation": {
                "screen_state": "main_menu", "available_actions": [],
                "important_objects": [], "confidence": 0.9, "visible_info": None,
            },
            "capture": {"image_size": [1942, 1136]},
            "screenshot": None,
        }
        agent_loop.step_once(
            config=config, mock=False, live=False, window=fake_window,
            obs_override=obs_record, step=7,
        )
        assert captured == [7]

    def test_run_loop_passes_incrementing_steps(self, tmp_path, monkeypatch):
        """run_loop 每步应递进 step 号（#1、#2、#3…）。"""
        config = copy.deepcopy(DEFAULT_CONFIG)
        config["paths"]["artifacts_dir"] = str(tmp_path / "art_steps")
        captured: list[Optional[int]] = []

        def _fake_push(cfg, decision, *, step=None, observation=None):
            captured.append(step)
            return True

        monkeypatch.setattr(agent_loop, "push_decision_overlay", _fake_push)
        monkeypatch.setattr(agent_loop, "decide", _fake_decide)   # v2.9：非 mock 才推浮层
        # 第 3 步后触发 STOP，验证 step 递进而非恒为 None
        monkeypatch.setattr(agent_loop, "stop_requested", lambda cfg: len(captured) >= 3)
        fake_window = WindowInfo(hwnd=1, title="fake", rect=(0, 0, 2000, 1200))
        obs_record = {
            "observation": {
                "screen_state": "main_menu", "available_actions": [],
                "important_objects": [], "confidence": 0.9, "visible_info": None,
            },
            "capture": {"image_size": [1942, 1136]},
            "screenshot": None,
        }
        agent_loop.run_loop(
            config=config, max_steps=5, mock=False, live=False,
            window=fake_window, delay_between=0,
            obs_override=obs_record,
        )
        assert captured == [1, 2, 3]
    """禁鼠标模式的 wait 空转纪律：AI 曾连续 5 步 wait 原地打转。"""

    STRUCT_OBS = {
        "source": "bridge",
        "screen_state": "shop_counter",
        "available_actions": [],
        "important_objects": [],
        "confidence": 1.0,
        "visible_info": {},
    }

    def test_wait_rule_forbids_consecutive_waits(self):
        prompt = build_decision_prompt(self.STRUCT_OBS, bridge_actions=True, no_mouse=True)
        # 规则已从「连续次数」改为「语义禁止等顾客」——断言新措辞
        assert "绝不用 wait 等顾客" in prompt
        assert "每轮必须推进一步" in prompt

    def test_wait_action_entry_warns(self):
        """wait 动作定义处也带空转警告。"""
        prompt = build_decision_prompt(self.STRUCT_OBS, bridge_actions=True, no_mouse=True)
        lines = prompt.splitlines()
        idx = next(i for i, ln in enumerate(lines) if ln.startswith("- wait"))
        entry = "\n".join(lines[idx:idx + 4])
        assert "绝不能用来等顾客做决定" in entry
        assert "等顾客＝原地卡死" in entry

    def test_explicitly_forbids_waiting_for_client(self):
        """r33 回归：AI 连续 13 步 wait，理由永远是「等顾客做出决定」。

        根因：wait 纪律按「措辞/连续次数」判定，AI 用「状态在变」绕过。
        新规则必须按语义禁止——绝不用 wait 等顾客。
        """
        prompt = build_decision_prompt(self.STRUCT_OBS, bridge_actions=True, no_mouse=True)
        assert "绝不用 wait 等顾客" in prompt
        assert "顾客不会自己改变主意" in prompt
        assert "顾客不会「自己决定」" in prompt

    def test_client_present_must_close_deal_rule(self):
        """r33 回归：顾客 is_buying 且有预算时，AI 却在等——需明确成交准则。"""
        prompt = build_decision_prompt(self.STRUCT_OBS, bridge_actions=True, no_mouse=True)
        assert "有顾客在场就必须成交" in prompt
        assert "budget_left 够就直接成交" in prompt
        assert "只有你的动作能推进局面" in prompt

    def test_placeholder_always_replaced(self):
        """r34 回归：节奏规则占位符不得残留在任何模式下。"""
        for no_mouse in (False, True):
            prompt = build_decision_prompt(
                self.STRUCT_OBS, bridge_actions=True, no_mouse=no_mouse
            )
            assert "__RHYTHM_RULE__" not in prompt

    def test_no_mouse_rhythm_rule_forbids_wait(self):
        """r34 核心修复：接口直调模式下「节奏要求」原教模型 wait，导致 15/15 全 wait。

        禁鼠标模式必须改写该条——系统每个动作后已自动等待，模型不该再 wait。
        """
        prompt = build_decision_prompt(self.STRUCT_OBS, bridge_actions=True, no_mouse=True)
        assert "不需要也不应该**用 wait 来等" in prompt
        assert "已自动等待" in prompt
        # 原「遣散顾客前先用 wait」诱导句不得出现在禁鼠标模式
        assert "遣散顾客前先用 wait" not in prompt

    def test_mouse_rhythm_rule_unchanged(self):
        """鼠标模式保留原节奏规则（鼠标操作确实需要人为留反应时间）。"""
        prompt = build_decision_prompt(self.STRUCT_OBS, bridge_actions=True, no_mouse=False)
        assert "遣散顾客前先用 wait" in prompt
        assert "不需要也不应该**用 wait 来等" not in prompt

    def test_vision_prompt_excludes_bridge_tools(self):
        prompt = build_decision_prompt({**self.STRUCT_OBS, "source": "vision"})
        assert "游戏 API 直调动作" not in prompt
        assert "坐标规则" in prompt  # 视觉通道用坐标说明

    def test_structured_prompt_uses_structured_note(self):
        prompt = build_decision_prompt(self.STRUCT_OBS)
        assert "数据来源" in prompt
        assert "来自游戏内部状态直读" in prompt

    def test_validate_accepts_bridge_action_with_extra(self):
        decision = validate_decision(
            {"action": "open_shutter", "reason": "开始营业", "confidence": 0.9},
            extra_actions=BRIDGE_ACTION_PARAMS,
        )
        assert decision["action"] == "open_shutter"
        assert decision["params"] == {}

    def test_validate_rejects_bridge_action_without_extra(self):
        """未注入白名单时，桥动作与幻觉动作一样被拒绝。"""
        with pytest.raises(ValueError, match="不在白名单"):
            validate_decision({"action": "open_shutter", "reason": "x", "confidence": 1.0})

    def test_validate_ignores_unknown_extra_action_names(self):
        """防御：extra_actions 里混入的未知动作名不会进白名单。"""
        with pytest.raises(ValueError, match="不在白名单"):
            validate_decision(
                {"action": "hack_the_planet", "reason": "x", "confidence": 1.0},
                extra_actions={"hack_the_planet": set()},
            )

    def test_validate_dialog_choose_index_normalized(self):
        decision = validate_decision(
            {"action": "dialog_choose", "params": {"index": "2.0"},
             "reason": "选第三项", "confidence": 0.8},
            extra_actions=BRIDGE_ACTION_PARAMS,
        )
        assert decision["params"]["index"] == 2

    def test_validate_dialog_choose_missing_index_defaults_zero(self):
        decision = validate_decision(
            {"action": "dialog_choose", "reason": "选第一项", "confidence": 0.8},
            extra_actions=BRIDGE_ACTION_PARAMS,
        )
        assert decision["params"]["index"] == 0

    def test_validate_dialog_choose_negative_index_clamped(self):
        decision = validate_decision(
            {"action": "dialog_choose", "params": {"index": -5},
             "reason": "x", "confidence": 0.8},
            extra_actions=BRIDGE_ACTION_PARAMS,
        )
        assert decision["params"]["index"] == 0

    def test_bridge_action_params_matches_client_actions(self):
        """schema 的桥动作表必须与 bridge.client.ACTIONS 一致（防漂移）。

        v1.5 起 force_dismiss 是恢复专用动作：在 ACTIONS 里（执行链路要走桥动作
        分支），但**不**在模型参数表里（模型不应该主动选它）。"""
        from game_agent.bridge.client import ACTIONS

        assert set(BRIDGE_ACTION_PARAMS) == set(ACTIONS) - {"force_dismiss"}

    def test_base_whitelist_unchanged(self):
        """基础白名单 7 个动作（v0.5.1 加 ctrl_click；桥动作只动态注入）。"""
        assert set(ALLOWED_ACTIONS) == {
            "click", "ctrl_click", "move", "press_key", "type_text", "wait", "stop",
        }


class TestPlaceItemAction:
    """P4：place_item（交易闭环第一步）的提示词 / 校验 / 执行分发。"""

    def test_prompt_includes_place_item(self):
        # bridge 通道由调用方显式开启桥动作组（T1 后不再按 source 自动注入）
        prompt = build_decision_prompt({
            "source": "bridge", "screen_state": "customer_offer",
            "available_actions": [], "important_objects": [], "confidence": 1.0,
        }, bridge_actions=True)
        assert "place_item" in prompt
        assert "Ctrl+左键" in prompt  # v0.9.7 玩家路径版：上桌 = Ctrl+左键的程序等价
        assert "clear_table" in prompt

    def test_validate_accepts_place_item(self):
        decision = validate_decision(
            {"action": "place_item", "params": {"identifier": " toothpaste "},
             "reason": "顾客想买，放牙膏上桌", "confidence": 0.8},
            extra_actions=BRIDGE_ACTION_PARAMS,
        )
        assert decision["params"]["identifier"] == "toothpaste"  # 去空白

    def test_validate_rejects_place_item_without_identifier(self):
        with pytest.raises(ValueError, match="identifier"):
            validate_decision(
                {"action": "place_item", "reason": "x", "confidence": 0.8},
                extra_actions=BRIDGE_ACTION_PARAMS,
            )

    def test_validate_rejects_empty_identifier(self):
        with pytest.raises(ValueError, match="identifier"):
            validate_decision(
                {"action": "place_item", "params": {"identifier": "   "},
                 "reason": "x", "confidence": 0.8},
                extra_actions=BRIDGE_ACTION_PARAMS,
            )

    def test_bridge_action_params_has_place_item(self):
        """v0.8.0 后桥动作 44 个（28 + 夜晚 8 + 电话 4 + 治安战斗 4）；
        v0.8.2 增 close_negociation → 45 个；v0.9.0 增 click_button → 46 个；
        v0.9.4 取消 close_negociation（用户令）→ 45 个；
        v1.0 移除 toggle_power（电源由夜间服务 POWER_SERVICE 控制，
        用户 10-18 指令：禁止字段直翻）→ 44 个；
        v1.5 增 force_dismiss（卡店恢复专用，不进模型参数表）→ ACTIONS 45；
        v1.7 增 take_ground_items / equip_backpack / stow_loot（拾荒背包收纳，
        用户 10-20 铁律）→ ACTIONS 48 / 参数表 47；
        v2.3 增 machine_put / machine_take（水商/养殖/熔炉机器操作）
        → ACTIONS 50 / 参数表 49；
        v2.5 增 ui_hit / ui_click / object_action / expedition_send / hire_worker；
        v2.6 增 ui_drag（完整拖拽派发）
        → ACTIONS 56 / 参数表 55。
        v3.3（正式版适配）增 accept_gun_order（枪械订单先校验必选需求再成交）
        → ACTIONS 57 / 参数表 56；
        v3.6 增 accept_barter（以物易物成交）/ machine_module_install（装模组）
        → ACTIONS 59 / 参数表 58。"""
        assert len(BRIDGE_ACTION_PARAMS) == 67
        assert len(ACTIONS) == 68
        assert set(BRIDGE_ACTION_PARAMS) == set(ACTIONS) - {"force_dismiss"}
        assert "force_dismiss" in ACTIONS
        assert "force_dismiss" not in BRIDGE_ACTION_PARAMS
        assert "close_negociation" not in BRIDGE_ACTION_PARAMS
        assert "toggle_power" not in BRIDGE_ACTION_PARAMS
        assert BRIDGE_ACTION_PARAMS["place_item"] == {"identifier"}
        assert BRIDGE_ACTION_PARAMS["open_negociation"] == set()
        assert BRIDGE_ACTION_PARAMS["sell_item"] == {"identifier"}
        assert BRIDGE_ACTION_PARAMS["buy_item"] == {"identifier"}
        assert BRIDGE_ACTION_PARAMS["buy_insurance"] == set()
        assert BRIDGE_ACTION_PARAMS["appraise"] == {"identifier"}
        assert BRIDGE_ACTION_PARAMS["display_case"] == {"identifier"}
        # v0.8.0 新动作参数
        assert BRIDGE_ACTION_PARAMS["night_service"] == {"service", "enable"}
        assert BRIDGE_ACTION_PARAMS["go_out"] == {"destination"}
        assert BRIDGE_ACTION_PARAMS["phone_call"] == {"number", "contact"}
        assert BRIDGE_ACTION_PARAMS["phone_book"] == {"show"}
        assert BRIDGE_ACTION_PARAMS["report_client"] == set()
        assert BRIDGE_ACTION_PARAMS["shoot"] == set()

    def test_place_item_dry_run(self, mock_bridge_server, tmp_path):
        _, port = mock_bridge_server
        config = make_config(tmp_path, port=port)
        result = execute_decision(
            config,
            {"action": "place_item", "params": {"identifier": "toothpaste"},
             "reason": "顾客想买，放牙膏上桌", "confidence": 0.8},
            live=False,
            bridge_client=make_bridge_client(port),
        )
        assert result.executed is False
        assert "dry-run" in result.detail

    def test_place_item_live_passes_identifier(self, mock_bridge_server, tmp_path):
        server_state, port = mock_bridge_server
        config = make_config(tmp_path, port=port)
        result = execute_decision(
            config,
            {"action": "place_item", "params": {"identifier": "toothpaste"},
             "reason": "顾客想买，放牙膏上桌", "confidence": 0.8},
            live=True,
            bridge_client=make_bridge_client(port),
        )
        assert result.ok is True
        assert server_state.action_log[-1] == {"action": "place_item", "identifier": "toothpaste"}


# ================================================================ 7. 主循环桥动作分发


class TestExecuteBridgeActions:
    def test_dry_run_records_without_calling(self, mock_bridge_server, tmp_path):
        _, port = mock_bridge_server
        config = make_config(tmp_path, port=port)
        result = execute_decision(
            config,
            {"action": "open_shutter", "params": {}, "reason": "开始营业", "confidence": 0.9},
            live=False,
            bridge_client=make_bridge_client(port),
        )
        assert result.executed is False
        assert result.ok is True
        assert "dry-run" in result.detail

    def test_live_calls_bridge(self, mock_bridge_server, tmp_path):
        server_state, port = mock_bridge_server
        config = make_config(tmp_path, port=port)
        result = execute_decision(
            config,
            {"action": "call_next", "params": {}, "reason": "叫下一位", "confidence": 0.9},
            live=True,
            bridge_client=make_bridge_client(port),
        )
        assert result.executed is True
        assert result.ok is True
        assert "call_next" in result.detail
        assert server_state.action_log[-1]["action"] == "call_next"

    def test_live_passes_params(self, mock_bridge_server, tmp_path):
        server_state, port = mock_bridge_server
        config = make_config(tmp_path, port=port)
        result = execute_decision(
            config,
            {"action": "dialog_choose", "params": {"index": 1},
             "reason": "选第二项", "confidence": 0.9},
            live=True,
            bridge_client=make_bridge_client(port),
        )
        assert result.ok is True
        assert server_state.action_log[-1] == {"action": "dialog_choose", "index": 1}

    def test_stop_action(self, tmp_path):
        config = make_config(tmp_path)
        result = execute_decision(
            config,
            {"action": "stop", "params": {}, "reason": "完成", "confidence": 1.0},
        )
        assert result.ok is True
        assert result.executed is False
        assert "停止" in result.detail

    def test_wait_dry_run_does_not_sleep(self, tmp_path):
        config = make_config(tmp_path)
        started = time.time()
        result = execute_decision(
            config,
            {"action": "wait", "params": {"seconds": 5.0}, "reason": "等待", "confidence": 0.9},
            live=False,
        )
        assert time.time() - started < 1.0
        assert result.detail.startswith("dry-run")


# ================================================================ 7b. Ctrl+点击动作（交易上桌）


class TestCtrlClickAction:
    """v0.5.1：ctrl_click 组合键动作（游戏内=快速转移物品到谈判柜台）。"""

    @staticmethod
    def _fake_window():
        return WindowInfo(hwnd=1, title="fake", rect=(0, 0, 2000, 1200))

    def test_schema_accepts_and_normalizes(self):
        decision = validate_decision(
            {"action": "ctrl_click", "params": {"x": 774.0, "y": "814"},
             "reason": "把合成肉放上交易桌", "confidence": 0.6}
        )
        assert decision["action"] == "ctrl_click"
        assert decision["params"] == {"x": 774, "y": 814}

    def test_schema_rejects_missing_y(self):
        with pytest.raises(ValueError):
            validate_decision(
                {"action": "ctrl_click", "params": {"x": 100},
                 "reason": "缺 y 坐标", "confidence": 0.6}
            )

    def test_execute_dry_run_needs_no_game(self, tmp_path):
        config = make_config(tmp_path)
        result = execute_decision(
            config,
            {"action": "ctrl_click", "params": {"x": 774, "y": 814},
             "reason": "上桌", "confidence": 0.6},
            live=False,
            window=self._fake_window(),
        )
        assert result.executed is False
        assert result.ok is True
        assert "Ctrl+点击" in result.detail

    def test_execute_live_dispatches_to_controller(self, tmp_path, monkeypatch):
        """live 分发到 controller.ctrl_click_image 并透传坐标。"""
        import game_agent.agent_loop as loop_mod
        from game_agent.control.controller import ControlResult

        calls: list[tuple[int, int]] = []

        def fake_ctrl_click(win, x, y, *, dry_run=False, move_duration=0.35):
            calls.append((x, y))
            return ControlResult(
                action="ctrl_click", params={"x": x, "y": y},
                executed=True, ok=True, detail="ok", duration_ms=1,
            )

        monkeypatch.setattr(loop_mod, "ctrl_click_image", fake_ctrl_click)
        config = make_config(tmp_path)
        result = execute_decision(
            config,
            {"action": "ctrl_click", "params": {"x": 774, "y": 814},
             "reason": "上桌", "confidence": 0.6},
            live=True,
            window=self._fake_window(),
        )
        assert calls == [(774, 814)]
        assert result.ok is True

    def test_prompt_includes_ctrl_click_and_flow(self):
        """提示词包含 ctrl_click 动作与玩家路径版交易流程（v0.9.7）。"""
        assert "ctrl_click" in ACTION_TOOLS
        assert "玩家路径版" in BRIDGE_ACTION_TOOLS
        assert "buying_tags" in BRIDGE_ACTION_TOOLS


# ================================================================ 8. 事件驱动主循环


class TestEventDrivenLoop:
    def test_signature_change_wakes_up(self, mock_bridge_server, tmp_path):
        """状态变化（现金变动）应立即可醒并推进下一步，不用等心跳。"""
        server_state, port = mock_bridge_server
        config = make_config(tmp_path, port=port)
        brain_file = make_wait_mock_file(tmp_path)

        # 第 3 次 /state 起现金变化：
        # 第 1 次 = 事件驱动预观测（无历史签名，直接放行），
        # 第 2 次 = 步骤 1 观察（记录签名 cash=550），
        # 第 3 次 = 步骤 2 预观测（cash=999，签名变化 → 立即唤醒）。
        def provider(calls: int) -> dict[str, Any]:
            return {**OPEN_STATE, "cash": 999} if calls >= 3 else dict(OPEN_STATE)

        server_state.state_provider = provider
        try:
            steps = run_loop(
                config, max_steps=2, live=False, mock=True,
                delay_between=0, push_overlay=False,
                brain_mock_file=brain_file,
                source=CHANNEL_BRIDGE,
                event_driven=True,
                poll_interval=0.01,
                idle_limit=5.0,  # 心跳设很长：若唤醒机制失效，本测试会挂到超时
                bridge_client=make_bridge_client(port),
            )
        finally:
            server_state.state_provider = None

        assert len(steps) == 2
        assert all(s.get("source") == CHANNEL_BRIDGE for s in steps)
        assert all("error" not in s for s in steps)

    def test_no_change_heartbeat_fallback(self, mock_bridge_server, tmp_path):
        """状态一直不变时，心跳兜底保证循环仍能推进（不永久挂起）。"""
        _, port = mock_bridge_server
        config = make_config(tmp_path, port=port)
        brain_file = make_wait_mock_file(tmp_path)

        started = time.time()
        steps = run_loop(
            config, max_steps=2, live=False, mock=True,
            delay_between=0, push_overlay=False,
            brain_mock_file=brain_file,
            source=CHANNEL_BRIDGE,
            event_driven=True,
            poll_interval=0.02,
            idle_limit=0.15,  # 第二轮等待 0.15s 后心跳放行
            bridge_client=make_bridge_client(port),
        )
        elapsed = time.time() - started

        assert len(steps) == 2
        assert elapsed >= 0.15  # 确实等待过（而非跳过等待机制）

    def test_vision_channel_never_waits(self, tmp_path, monkeypatch):
        """vision 通道下事件驱动不生效：不构造预观测，直接逐步走。"""
        config = make_config(tmp_path)
        brain_file = make_wait_mock_file(tmp_path)

        # vision 观察需要真实游戏窗口（截图），测试环境没有；
        # 这里只关心主循环的通道门控，故替换 observe 为离线假记录。
        class _FakeRecord:
            observation = {"source": "vision", "screen_state": "shop_counter"}
            screenshot = None
            capture = {"image_size": [1280, 720]}

        monkeypatch.setattr("game_agent.agent_loop.observe", lambda *a, **k: _FakeRecord())

        started = time.time()
        steps = run_loop(
            config, max_steps=2, live=False, mock=True,
            delay_between=0, push_overlay=False,
            brain_mock_file=brain_file,
            source=CHANNEL_VISION,
            event_driven=True,
            poll_interval=0.02,
            idle_limit=5.0,  # 若误走等待逻辑，本测试会挂到超时
        )
        elapsed = time.time() - started

        assert len(steps) == 2
        assert all(s.get("source") == CHANNEL_VISION for s in steps)
        assert elapsed < 2.0

    def test_step_records_signature(self, mock_bridge_server, tmp_path):
        """结构化步骤记录必须带 state_signature（事件驱动的比较依据）。"""
        _, port = mock_bridge_server
        config = make_config(tmp_path, port=port)
        brain_file = make_wait_mock_file(tmp_path)
        steps = run_loop(
            config, max_steps=1, live=False, mock=True,
            delay_between=0, push_overlay=False,
            brain_mock_file=brain_file,
            source=CHANNEL_BRIDGE,
            bridge_client=make_bridge_client(port),
        )
        assert len(steps) == 1
        sig = steps[0].get("state_signature")
        assert isinstance(sig, list) and len(sig) == 21  # v0.9.16 的 20 + CR-X01-2 降级证据位
        assert sig[4] == 550  # cash

    def test_stop_file_aborts_loop(self, mock_bridge_server, tmp_path):
        """artifacts/STOP 文件中止机制在结构化通道下同样有效。"""
        _, port = mock_bridge_server
        config = make_config(tmp_path, port=port)
        brain_file = make_wait_mock_file(tmp_path)
        stop_file = tmp_path / "artifacts" / "STOP"
        stop_file.parent.mkdir(parents=True, exist_ok=True)
        stop_file.write_text("stop", encoding="utf-8")

        steps = run_loop(
            config, max_steps=3, live=False, mock=True,
            delay_between=0, push_overlay=False,
            brain_mock_file=brain_file,
            source=CHANNEL_BRIDGE,
            bridge_client=make_bridge_client(port),
        )
        assert len(steps) == 1
        assert steps[0]["stopped"] is True


# ================================================================ 9. 夜晚/电话/治安动作（v0.8.0）


class TestV080NightActions:
    """v0.8.0：夜晚阶段动作（night_services/night_service/end_day_confirm/
    open_map/go_out/stay_home/scavenge/leave_outside）的 schema 校验与执行透传。"""

    def test_actions_whitelist_contains_v080(self):
        for name in ("night_services", "night_service", "end_day_confirm", "open_map",
                     "go_out", "stay_home", "scavenge", "leave_outside",
                     "open_phone", "phone_call", "phone_book", "hang_up",
                     "report_client", "enter_shooting_mode", "shoot", "exit_shooting_mode"):
            assert name in ACTIONS
            assert name in BRIDGE_ACTION_PARAMS

    def test_night_service_accepts_and_normalizes(self):
        decision = validate_decision(
            {"action": "night_service",
             "params": {"service": "POWER_SERVICE", "enable": True},
             "reason": "买电力服务给机器供电", "confidence": 0.85},
            extra_actions=BRIDGE_ACTION_PARAMS,
        )
        assert decision["params"] == {"service": "POWER_SERVICE", "enable": True}

    def test_night_service_enable_default_true(self):
        decision = validate_decision(
            {"action": "night_service", "params": {"service": "保险服务"},
             "reason": "防失窃", "confidence": 0.8},
            extra_actions=BRIDGE_ACTION_PARAMS,
        )
        assert decision["params"]["enable"] is True

    def test_night_service_enable_string_false(self):
        decision = validate_decision(
            {"action": "night_service",
             "params": {"service": "POWER_SERVICE", "enable": "false"},
             "reason": "不用电取消电力", "confidence": 0.8},
            extra_actions=BRIDGE_ACTION_PARAMS,
        )
        assert decision["params"]["enable"] is False

    def test_night_service_requires_service(self):
        with pytest.raises(ValueError):
            validate_decision(
                {"action": "night_service", "params": {},
                 "reason": "缺服务", "confidence": 0.8},
                extra_actions=BRIDGE_ACTION_PARAMS,
            )

    def test_go_out_accepts_destination(self):
        decision = validate_decision(
            {"action": "go_out", "params": {"destination": "scavenging"},
             "reason": "今夜外出拾荒淘货", "confidence": 0.7},
            extra_actions=BRIDGE_ACTION_PARAMS,
        )
        assert decision["params"] == {"destination": "scavenging"}

    def test_go_out_requires_destination(self):
        with pytest.raises(ValueError):
            validate_decision(
                {"action": "go_out", "params": {},
                 "reason": "缺目的地", "confidence": 0.7},
                extra_actions=BRIDGE_ACTION_PARAMS,
            )

    def test_phone_call_accepts_number(self):
        decision = validate_decision(
            {"action": "phone_call", "params": {"number": 8376},
             "reason": "打给王尔德助手", "confidence": 0.8},
            extra_actions=BRIDGE_ACTION_PARAMS,
        )
        assert decision["params"] == {"number": 8376}

    def test_phone_call_accepts_contact_keyword(self):
        decision = validate_decision(
            {"action": "phone_call", "params": {"contact": "王尔德"},
             "reason": "按名字打给王尔德", "confidence": 0.8},
            extra_actions=BRIDGE_ACTION_PARAMS,
        )
        assert decision["params"] == {"contact": "王尔德"}

    def test_phone_call_requires_number_or_contact(self):
        with pytest.raises(ValueError):
            validate_decision(
                {"action": "phone_call", "params": {},
                 "reason": "缺参数", "confidence": 0.8},
                extra_actions=BRIDGE_ACTION_PARAMS,
            )

    def test_phone_call_rejects_non_numeric_number(self):
        with pytest.raises(ValueError):
            validate_decision(
                {"action": "phone_call", "params": {"number": "not-a-number"},
                 "reason": "坏号码", "confidence": 0.8},
                extra_actions=BRIDGE_ACTION_PARAMS,
            )

    def test_phone_book_show_default_true(self):
        decision = validate_decision(
            {"action": "phone_book", "params": {},
             "reason": "看通讯录", "confidence": 0.9},
            extra_actions=BRIDGE_ACTION_PARAMS,
        )
        assert decision["params"]["show"] is True

    def test_night_service_live_passes_params(self, mock_bridge_server, tmp_path):
        """live：night_service 的 service/enable 原样透传到桥接请求体。"""
        server_state, port = mock_bridge_server
        # v1.3 前置校验：夜晚服务只能在 CLOSED（已打烊）时执行
        server_state.state["store_state"] = "CLOSED"
        config = make_config(tmp_path, port=port)
        result = execute_decision(
            config,
            {"action": "night_service",
             "params": {"service": "POWER_SERVICE", "enable": True},
             "reason": "买电力服务", "confidence": 0.85},
            live=True,
            bridge_client=make_bridge_client(port),
        )
        assert result.ok is True
        assert server_state.action_log[-1] == {
            "action": "night_service", "service": "POWER_SERVICE", "enable": True,
        }

    def test_go_out_live_passes_destination(self, mock_bridge_server, tmp_path):
        server_state, port = mock_bridge_server
        # v1.3 前置校验：外出只能在夜晚（CLOSED）执行
        server_state.state["store_state"] = "CLOSED"
        config = make_config(tmp_path, port=port)
        result = execute_decision(
            config,
            {"action": "go_out", "params": {"destination": "commissary"},
             "reason": "去配给站换东西", "confidence": 0.75},
            live=True,
            bridge_client=make_bridge_client(port),
        )
        assert result.ok is True
        assert server_state.action_log[-1] == {
            "action": "go_out", "destination": "commissary",
        }

    def test_report_client_live(self, mock_bridge_server, tmp_path):
        server_state, port = mock_bridge_server
        config = make_config(tmp_path, port=port)
        result = execute_decision(
            config,
            {"action": "report_client", "params": {},
             "reason": "通缉犯在店，举报拿悬赏", "confidence": 0.9},
            live=True,
            bridge_client=make_bridge_client(port),
        )
        assert result.ok is True
        assert server_state.action_log[-1]["action"] == "report_client"

    def test_prompt_documents_night_flow(self):
        """提示词必须包含夜晚流程关键动作（AI 按提示词执行完整夜晚）。"""
        for name in ("night_services", "night_service", "end_day_confirm", "open_map",
                     "go_out", "stay_home", "scavenge", "leave_outside",
                     "phone_call", "phone_book", "hang_up", "report_client",
                     "enter_shooting_mode", "shoot"):
            assert name in BRIDGE_ACTION_TOOLS
        assert "POWER_SERVICE" in BRIDGE_ACTION_TOOLS
        assert "8376" in BRIDGE_ACTION_TOOLS

    def test_prompt_documents_queue_guard(self):
        """v0.8.1：提示词必须写清队列空的权威信号（禁止试探性叫号空转）。"""
        assert "queue.can_next" in BRIDGE_ACTION_TOOLS
        assert "不要再 call_next 空转" in BRIDGE_ACTION_TOOLS or "严禁再 call_next" in BRIDGE_ACTION_TOOLS


# ================================================================ 4d. 谈判面板可控化（v0.8.2）

# v0.8.2：/state 的 table 段新增 nego_open（谈判面板开闭态）；
# 多 Agent 硬规则「未关闭界面必须优先关闭」从此有观测依据与执行手段
# （close_negociation）。此前 open_negociation 后面板是否开着只能猜。
NEGO_PANEL_OPEN_STATE: dict[str, Any] = {
    **TABLE_EMPTY_STATE,
    "table": {
        "negociated_item": None,
        "negociated_value": None,
        "player_grid_items": [],
        "client_grid_items": [],
        "nego_open": True,
    },
}

NEGO_PANEL_CLOSED_STATE: dict[str, Any] = {
    **TABLE_EMPTY_STATE,
    "table": {
        "negociated_item": None,
        "negociated_value": None,
        "player_grid_items": [],
        "client_grid_items": [],
        "nego_open": False,
    },
}


class TestNegotiationPanelObservation:
    """v0.8.2：谈判面板开闭态进入观察契约（actions/objects/visible_info/signature）。

    v2.0 变更：v0.9.4 已取消 close_negociation（「面板是交易操作台，
    禁止主动关闭」），因此「关闭谈判面板」不再是候选动作——改为提示面板内成交。
    原断言（关闭优先）编码的是已被废止的旧行为。
    """

    def test_actions_hint_trade_when_panel_open(self):
        """面板开着时给出成交指引，且绝不提示关闭面板。

        2026-11-15 更正：原文案含「接受多个」，但正式版谈判面板只有
        primaryButton / secondaryButton 两个大按钮
        （tools/decompile_full/NegociationUIManager.cs:59/61），
        ``nego_buttons`` 也只有 primary/secondary/wholesale/accept_offer 四键
        （BridgeApi.cs:1185-1191）——「接受多个」是幽灵能力，必须从提示里删掉，
        否则模型会等一个永不为真的条件。
        """
        actions = build_observation(NEGO_PANEL_OPEN_STATE, source=CHANNEL_BRIDGE)["available_actions"]
        assert actions[0] == "在谈判面板内成交（批发 / 接受报价）"
        assert "接受多个" not in actions[0]
        assert "关闭谈判面板" not in actions

    def test_actions_no_close_when_panel_closed(self):
        actions = build_observation(NEGO_PANEL_CLOSED_STATE, source=CHANNEL_BRIDGE)["available_actions"]
        assert "关闭谈判面板" not in actions

    def test_actions_no_close_when_field_absent(self):
        """旧形状状态（无 nego_open 字段）不得冒出关闭动作——向后兼容。"""
        actions = build_observation(TABLE_EMPTY_STATE, source=CHANNEL_BRIDGE)["available_actions"]
        assert "关闭谈判面板" not in actions

    def test_screen_state_negotiation_when_panel_open(self):
        assert derive_screen_state(NEGO_PANEL_OPEN_STATE) == "negotiation"

    def test_screen_state_customer_offer_when_panel_closed(self):
        assert derive_screen_state(NEGO_PANEL_CLOSED_STATE) == "customer_offer"

    def test_objects_negotiation_panel(self):
        objects = build_observation(NEGO_PANEL_OPEN_STATE, source=CHANNEL_BRIDGE)["important_objects"]
        panel = next((o for o in objects if o["name"] == "谈判面板"), None)
        assert panel is not None
        # v0.9.11：文案改为"已打开——…"（面板是交易操作台，开着≠需关闭）
        assert "已打开" in panel["value"]

    def test_visible_info_table_nego_open(self):
        info = build_observation(NEGO_PANEL_OPEN_STATE, source=CHANNEL_BRIDGE)["visible_info"]
        assert info["table"]["nego_open"] is True

    def test_signature_nego_open_change_detected(self):
        """面板开/关必须被签名捕获（事件驱动循环据此唤醒）。"""
        a = state_signature(NEGO_PANEL_CLOSED_STATE)
        b = state_signature(NEGO_PANEL_OPEN_STATE)
        assert a != b

    def test_signature_length_21(self):
        """v0.9.16 的 20 字段 + CR-X01-2 的降级证据位 = 21 字段。

        第 21 位是「降级/缺失/报错块」的有序元组，长度恒为 21（不是可变长），
        降级恢复时该位从非空回到 ()，事件驱动主循环据此唤醒重规划。
        """
        sig = state_signature(OPEN_STATE)
        assert len(sig) == 21
        assert isinstance(sig[20], tuple)
        # 未降级的完整 state 该位为空元组（不含 error 文本，避免文本抖动导致无限重规划）
        assert sig[20] == ()

    def test_signature_degradation_bit_wakes_loop(self):
        """CR-X01-2：某块读取失败/恢复必须改变签名，否则事件驱动循环不会重规划。"""
        healthy = state_signature({**OPEN_STATE, "economy": {"stock_value": 100}})
        broken = state_signature({**OPEN_STATE, "economy_error": "boom"})
        assert healthy != broken
        assert healthy[20] == ()
        assert broken[20] != ()
        # 前 20 位不受降级影响（避免降级把业务值也抹掉，掩盖真实变化）
        assert state_signature({**OPEN_STATE, "economy": {"stock_value": 100}})[:20] == healthy[:20]


class TestCloseNegociationAction:
    """v0.9.4：close_negociation 已取消（白名单 / 校验层 / 提示词三类出口）。"""

    def test_not_in_whitelist(self):
        """白名单与 Python 客户端动作表都不再包含 close_negociation。"""
        assert "close_negociation" not in ACTIONS
        assert "close_negociation" not in BRIDGE_ACTION_PARAMS

    def test_validate_rejects_close_negociation(self):
        """模型若仍输出 close_negociation（幻觉残留），校验层直接拒绝。"""
        with pytest.raises(ValueError):
            validate_decision(
                {"action": "close_negociation", "params": {},
                 "reason": "谈判面板未关闭，先关面板", "confidence": 0.95},
                extra_actions=BRIDGE_ACTION_PARAMS,
            )

    def test_prompt_no_longer_offers_close_negociation(self):
        """提示词不再把 close_negociation 列为可用动作；含「已取消」禁止声明。"""
        assert "- close_negociation：" not in BRIDGE_ACTION_TOOLS
        assert "close_negociation 已取消" in BRIDGE_ACTION_TOOLS
        assert "nego_open" in BRIDGE_ACTION_TOOLS
