"""v2.0 回归测试：本轮修复的 5 个缺陷（全离线，不触碰真实游戏）。

覆盖：
1. click_button 的 path / type 参数不再被校验层丢弃（同名按钮消歧）；
2. 夜晚白名单包含 equip_backpack / stow_loot / take_ground_items
   （拾荒「装背包→收纳→离场」链路在夜晚必须可用）；
3. CLI 不注入 bridge_client 时，桥接通道仍会构造客户端
   （否则晨报面板守卫、昼夜状态同步等依赖实时 /state 的守卫全部失效）；
4. 谈判面板打开时不再提示「关闭谈判面板」（该动作 v0.9.4 已取消）；
5. 库存观察按 identifier 聚合，同一物品不再按格子逐条重复。
"""
from __future__ import annotations

import copy

import pytest

from game_agent import agent_loop
from game_agent.brain.brain import NIGHT_PHASE_ACTIONS
from game_agent.brain.schema import BRIDGE_ACTION_PARAMS, validate_decision
from game_agent.config import DEFAULT_CONFIG
from game_agent.structured.observer import (
    _compact_inventory,
    _derive_available_actions,
    build_observation,
)


def _decision(action: str, params: dict | None = None) -> dict:
    return {"action": action, "params": params or {}, "reason": "t", "confidence": 1.0}


# ---------------------------------------------------------------- 1. click_button


class TestClickButtonPath:
    def test_path_is_kept(self):
        """v2.0：path 必须原样保留——晨报面板/同名 CloseButton 靠它消歧。"""
        result = validate_decision(
            _decision("click_button", {"name": "CloseButton", "path": "_AdvCalendar"}),
            extra_actions={"click_button": BRIDGE_ACTION_PARAMS["click_button"]},
        )
        assert result["params"] == {"name": "CloseButton", "path": "_AdvCalendar"}

    def test_type_is_kept(self):
        result = validate_decision(
            _decision("click_button", {"type": "StoreShutterButton"}),
            extra_actions={"click_button": BRIDGE_ACTION_PARAMS["click_button"]},
        )
        assert result["params"] == {"type": "StoreShutterButton"}

    def test_path_only_accepted(self):
        """只给 path 也应合法（桥接侧自己决定能否命中）。"""
        result = validate_decision(
            _decision("click_button", {"path": "_AdvCalendar"}),
            extra_actions={"click_button": BRIDGE_ACTION_PARAMS["click_button"]},
        )
        assert result["params"] == {"path": "_AdvCalendar"}

    def test_all_empty_still_rejected(self):
        with pytest.raises(ValueError):
            validate_decision(
                _decision("click_button", {"name": "  ", "path": "  "}),
                extra_actions={"click_button": BRIDGE_ACTION_PARAMS["click_button"]},
            )


# ---------------------------------------------------------------- 2. 夜晚白名单


class TestNightWhitelist:
    def test_brain_whitelist_covers_scavenge_chain(self):
        """装背包 → 拾荒 → 收纳 → 离场，四个动作夜晚都必须可选。"""
        for action in ("equip_backpack", "scavenge", "take_ground_items",
                       "stow_loot", "leave_outside"):
            assert action in NIGHT_PHASE_ACTIONS, action

    def test_agent_loop_whitelist_matches_brain(self):
        """两处白名单同源：brain 收紧后 agent_loop 不能再放宽/漏项。"""
        assert NIGHT_PHASE_ACTIONS <= agent_loop._NIGHT_PHASE_ACTIONS

    def test_equip_backpack_not_stripped_at_night(self):
        """夜晚阶段 decide() 会按白名单过滤 extra_actions——equip_backpack 必须留下。"""
        extra = {k: v for k, v in BRIDGE_ACTION_PARAMS.items()
                 if k in NIGHT_PHASE_ACTIONS}
        assert "equip_backpack" in extra
        assert "sell_item" not in extra


# ---------------------------------------------------------------- 3. 客户端按需构造


class TestBridgeClientInjection:
    def test_step_once_creates_client_for_bridge(self, tmp_path, monkeypatch):
        """CLI 不注入 client 时，bridge 通道必须自己构造一个（否则守卫读不到实时状态）。"""
        created = []

        class _FakeClient:
            def get_state(self):
                return {"in_game": True, "store_state": "OPEN"}

        def _fake_make(config):
            created.append(config)
            return _FakeClient()

        def _fake_decide(cfg, observation, **kwargs):
            return ({"action": "stop", "params": {}, "reason": "t", "confidence": 1.0}, {})

        monkeypatch.setattr(agent_loop, "make_bridge_client", _fake_make)
        monkeypatch.setattr(agent_loop, "decide", _fake_decide)
        monkeypatch.setattr(agent_loop, "push_decision_overlay", lambda *a, **k: False)
        # 观察阶段不碰网络：直接注入观察（此时通道会被标记为 vision，故显式走 bridge 分支）
        monkeypatch.setattr(
            agent_loop.StructuredObserver, "observe",
            lambda self, source, save=True: _struct_obs(),
        )

        config = copy.deepcopy(DEFAULT_CONFIG)
        config["paths"]["artifacts_dir"] = str(tmp_path / "art")
        agent_loop.step_once(
            config, source="bridge", save=False, push_overlay=False,
            allow_mouse=False,
        )
        assert created, "bridge 通道应自动构造桥接客户端"

    def test_run_loop_creates_client_for_bridge(self, tmp_path, monkeypatch):
        created = []

        class _FakeClient:
            def get_state(self):
                return {"in_game": True, "store_state": "OPEN"}

        monkeypatch.setattr(agent_loop, "make_bridge_client",
                            lambda cfg: created.append(cfg) or _FakeClient())
        monkeypatch.setattr(agent_loop, "step_once",
                            lambda *a, **k: {"decision": {"action": "stop"},
                                             "execution": {"ok": True}, "step": 1})
        config = copy.deepcopy(DEFAULT_CONFIG)
        config["paths"]["artifacts_dir"] = str(tmp_path / "art")
        agent_loop.run_loop(config, max_steps=1, source="bridge", push_overlay=False,
                            delay_between=0)
        assert created


def _struct_obs():
    from game_agent.structured.observer import StructuredObservation
    return StructuredObservation(
        source="bridge",
        observation={"source": "bridge", "screen_state": "shop_counter",
                     "available_actions": [], "important_objects": [],
                     "confidence": 1.0, "visible_info": {}},
        state={"in_game": True, "store_state": "OPEN"},
        signature=(True, "StoreScene", "OPEN", False, 700, None, None, None,
                   True, False, None, None, False, "STORE", False, True,
                   False, 4, 100, False),
    )


# ---------------------------------------------------------------- 4. 谈判面板提示


class TestNegotiationHint:
    def test_open_panel_hints_trade_not_close(self):
        actions = _derive_available_actions({
            "in_game": True,
            "store_state": "OPEN",
            "table": {"nego_open": True, "negociated_item": {"identifier": "x"}},
        })
        assert "关闭谈判面板" not in actions
        assert any("谈判面板内成交" in a for a in actions)

    def test_morning_begin_hint_not_duplicated(self):
        actions = _derive_available_actions({
            "in_game": True, "store_state": "MORNING", "queue": {"can_next": True},
        })
        assert actions.count("开始营业") == 1


# ---------------------------------------------------------------- 5. 库存聚合


class TestInventoryCompaction:
    def test_duplicates_merged_with_count(self):
        state = {
            "in_game": True,
            "inventory": {
                "owned": [
                    {"identifier": "water_filter", "name": "滤水器", "value": 25,
                     "count": 1, "item_types": ["FILTER"]},
                    {"identifier": "water_filter", "name": "滤水器", "value": 25,
                     "count": 1, "item_types": ["FILTER"]},
                    {"identifier": "energy_credit", "name": "能源信用点", "value": 5,
                     "count": 1, "item_types": ["CURRENCY"]},
                ],
                "not_owned": [],
            },
            "client": None,
        }
        obs = build_observation(state, source="bridge")
        owned = obs["visible_info"]["inventory"]["owned"]
        by_id = {it["identifier"]: it for it in owned}
        assert len(owned) == 2
        assert by_id["water_filter"]["count"] == 2
        # 顺序沿用游戏库存（首次出现顺序）——不重排，避免下游清单顺序漂移
        assert owned[0]["identifier"] == "water_filter"

    def test_zero_value_items_filtered(self):
        result = _compact_inventory({
            "owned": [
                {"identifier": "shelf", "name": "货架", "value": 0, "count": 1},
                {"identifier": "pill", "name": "药", "value": 9, "count": 1},
            ]
        })
        assert [it["identifier"] for it in result["owned"]] == ["pill"]

    def test_item_types_merged(self):
        result = _compact_inventory({
            "owned": [
                {"identifier": "x", "name": "x", "value": 1, "count": 1,
                 "item_types": ["FOOD"]},
                {"identifier": "x", "name": "x", "value": 1, "count": 1,
                 "item_types": ["JUNK"]},
            ]
        })
        assert result["owned"][0]["item_types"] == ["FOOD", "JUNK"]


# ---------------------------------------------------------------- 6. 商业目标跟踪


class TestBusinessGoal:
    def _cfg(self, tmp_path, **goal):
        config = copy.deepcopy(DEFAULT_CONFIG)
        config["paths"]["artifacts_dir"] = str(tmp_path / "art")
        config["goal"] = {"enabled": True, "label": "十日内净赚 1000 元",
                          "target_profit": 1000, "days": 10, **goal}
        return config

    @staticmethod
    def _obs(cash, stock, day=None, store_state="OPEN"):
        return {"visible_info": {"money": cash, "day": day,
                                 "economy": {"stock_value": stock},
                                 "store_state": store_state}}

    def test_anchors_on_first_observation(self, tmp_path):
        from game_agent.goal import BusinessGoal
        goal = BusinessGoal(self._cfg(tmp_path))
        p = goal.update(self._obs(500, 500, day=10))
        assert p.start_equity == 1000
        assert p.start_cash == 500
        assert p.profit == 0
        assert p.elapsed_days == 0
        assert p.days_left == 10

    def test_profit_is_cash_delta(self, tmp_path):
        from game_agent.goal import BusinessGoal
        goal = BusinessGoal(self._cfg(tmp_path))
        goal.update(self._obs(500, 500, day=10))
        # 卖货：现金 +400（库存同步 -300，但不计入「赚」）
        p = goal.update(self._obs(900, 200, day=11))
        assert p.profit == 400
        assert p.cash == 900 and p.stock_value == 200
        assert p.elapsed_days == 1
        assert p.days_left == 9

    def test_purchase_is_expense(self, tmp_path):
        """进货花掉的现金确实减少进度（不能靠囤货刷分）。"""
        from game_agent.goal import BusinessGoal
        goal = BusinessGoal(self._cfg(tmp_path))
        goal.update(self._obs(500, 500, day=10))
        p = goal.update(self._obs(200, 800, day=10))
        assert p.profit == -300
        assert p.equity == 1000  # 资产合计不变，但现金目标落后

    def test_morning_counting_without_day(self, tmp_path):
        """旧插件没有 day 字段时，用清晨次数兜底计数。"""
        from game_agent.goal import BusinessGoal
        goal = BusinessGoal(self._cfg(tmp_path))
        goal.update(self._obs(500, 500, day=None, store_state="OPEN"))
        goal.update(self._obs(500, 500, day=None, store_state="CLOSED"))
        p = goal.update(self._obs(500, 500, day=None, store_state="MORNING"))
        assert p.elapsed_days == 1

    def test_progress_persists_across_instances(self, tmp_path):
        from game_agent.goal import BusinessGoal
        cfg = self._cfg(tmp_path)
        BusinessGoal(cfg).update(self._obs(500, 500, day=10))
        again = BusinessGoal(cfg)
        p = again.update(self._obs(700, 500, day=11))
        assert p.start_equity == 1000
        assert p.profit == 200

    def test_target_change_restarts_cycle(self, tmp_path):
        from game_agent.goal import BusinessGoal
        BusinessGoal(self._cfg(tmp_path)).update(self._obs(500, 500, day=10))
        p = BusinessGoal(self._cfg(tmp_path, target_profit=2000)).update(
            self._obs(800, 500, day=20))
        assert p.start_equity == 1300
        assert p.profit == 0

    def test_render_contains_progress_and_guardrail(self, tmp_path):
        from game_agent.goal import BusinessGoal
        goal = BusinessGoal(self._cfg(tmp_path))
        block = goal.render(goal.update(self._obs(500, 500, day=10)))
        assert "十日内净赚 1000 元" in block
        assert "现金" in block
        assert "禁止" in block and "MACHINE" in block

    def test_disabled_renders_empty(self, tmp_path):
        from game_agent.goal import BusinessGoal
        cfg = self._cfg(tmp_path)
        cfg["goal"]["enabled"] = False
        goal = BusinessGoal(cfg)
        goal.update(self._obs(500, 500, day=10))
        assert goal.render() == ""

    def test_reset_clears_anchor(self, tmp_path):
        from game_agent.goal import BusinessGoal
        cfg = self._cfg(tmp_path)
        goal = BusinessGoal(cfg)
        goal.update(self._obs(500, 500, day=10))
        goal.reset()
        p = goal.update(self._obs(50, 50, day=99))
        assert p.start_equity == 100
        assert p.elapsed_days == 0


# ---------------------------------------------------------------- 6. 商业目标跟踪


# ---------------------------------------------------------------- 7. 目标注入提示词


class TestGoalInjection:
    def test_step_once_passes_goal_block_to_decide(self, tmp_path, monkeypatch):
        """单步命令没传 tracker 时也要按配置生成 KPI 段并送进决策提示词。"""
        captured = {}

        def _fake_observe(self, source, save=True):
            from game_agent.structured.observer import StructuredObservation
            obs = {"source": "bridge", "screen_state": "shop_counter",
                   "available_actions": [], "important_objects": [],
                   "confidence": 1.0,
                   "visible_info": {"money": 500, "day": 12, "store_state": "OPEN",
                                    "economy": {"stock_value": 300},
                                    "inventory": {"owned": [], "not_owned": []},
                                    "client": None}}
            return StructuredObservation(
                source="bridge", observation=obs,
                state={"in_game": True, "cash": 500, "store_state": "OPEN"},
                signature=(True, None, "OPEN", False, 500, None, None, None,
                           True, False, None, None, False, "STORE", False, True,
                           False, 3, 300, False),
            )

        def _fake_decide(cfg, obs, **kwargs):
            captured["goal_block"] = kwargs.get("goal_block")
            return ({"action": "stop", "params": {}, "reason": "t",
                     "confidence": 1.0}, {})

        monkeypatch.setattr(agent_loop.StructuredObserver, "observe", _fake_observe)
        monkeypatch.setattr(agent_loop, "decide", _fake_decide)
        monkeypatch.setattr(agent_loop, "push_decision_overlay", lambda *a, **k: False)

        config = copy.deepcopy(DEFAULT_CONFIG)
        config["paths"]["artifacts_dir"] = str(tmp_path / "art")
        config["goal"] = {"enabled": True, "label": "十日内净赚 1000 元",
                          "target_profit": 1000, "days": 10}

        class _StubClient:
            """隔离真实桥接：晨报守卫/前置状态校验只看到空按钮与营业中。"""
            def get_ui_buttons(self):
                return {"buttons": []}

            def get_state(self):
                return {"in_game": True, "store_state": "OPEN", "cash": 500}

        record = agent_loop.step_once(config, source="bridge", save=False,
                                      push_overlay=False, allow_mouse=False,
                                      bridge_client=_StubClient())
        assert "商业目标" in (captured.get("goal_block") or "")
        assert record["goal"]["start_cash"] == 500


# ---------------------------------------------------------------- 8. 批发方向（v2.2）


class TestWholesaleDirection:
    """AcceptWholesale = 我方付钱买入。2026-10-24 实测 AI 当卖出按钮用，净亏 62。"""

    def test_prompt_declares_purchase_direction(self):
        from game_agent.brain.prompt import BRIDGE_ACTION_TOOLS
        assert "AcceptWholesale = 你付钱收购对方的整批货" in BRIDGE_ACTION_TOOLS
        assert "有批发就批发" not in BRIDGE_ACTION_TOOLS
        assert "批发场景必须点" not in BRIDGE_ACTION_TOOLS

    def test_prompt_sell_path_uses_accept_offer(self):
        from game_agent.brain.prompt import BRIDGE_ACTION_TOOLS
        assert "卖自己的货：accept_offer" in BRIDGE_ACTION_TOOLS

    def test_observation_notes_direction(self):
        state = {
            "in_game": True, "store_state": "OPEN",
            "table": {
                "nego_open": True,
                "wholesale_cost": "162<sprite name=\"c\" tint=1 > (-19%)",
                "negociated_item": {"identifier": "x", "name": "x", "value": 100},
            },
        }
        table = build_observation(state, source="bridge")["visible_info"]["table"]
        assert "现金流出" in table["wholesale_note"]
        assert "accept_offer" in table["wholesale_note"]


# ---------------------------------------------------------------- 8. 批发方向（v2.2）


# ---------------------------------------------------------------- 8. 读档/换档重锚


class TestGoalRebase:
    """切换存档后，目标账本不能沿用上一局的期初值。"""

    def _cfg(self, tmp_path):
        config = copy.deepcopy(DEFAULT_CONFIG)
        config["paths"]["artifacts_dir"] = str(tmp_path / "art")
        config["goal"] = {"enabled": True, "label": "x", "target_profit": 1000, "days": 10}
        return config

    @staticmethod
    def _obs(cash, stock, day):
        return {"visible_info": {"money": cash, "day": day, "store_state": "OPEN",
                                 "economy": {"stock_value": stock}}}

    def test_day_jump_rebases_anchor(self, tmp_path):
        from game_agent.goal import BusinessGoal
        goal = BusinessGoal(self._cfg(tmp_path))
        goal.update(self._obs(500, 500, 74))
        p = goal.update(self._obs(65, 3958, 25))
        assert p.rebased is True
        assert p.start_day == 25 and p.start_cash == 65
        assert p.profit == 0 and p.elapsed_days == 0

    def test_normal_day_does_not_rebase(self, tmp_path):
        from game_agent.goal import BusinessGoal
        goal = BusinessGoal(self._cfg(tmp_path))
        goal.update(self._obs(65, 3958, 25))
        p = goal.update(self._obs(100, 3958, 26))
        assert p.rebased is False
        assert p.profit == 35 and p.elapsed_days == 1


# ---------------------------------------------------------------- 8. 读档/换档重锚


