"""v0.9.8「无效动作 streak」测试：同名动作连续零效果 → 提示词禁用（全离线）。

背景（r45 实测）：AI 在「无议价物品」状态下连点 accept_offer ×11 零效果——
wait 之外的无效动作空转。机制：同名动作连续执行且状态签名零变化达阈值，
由 agent_loop 在下一步提示词中禁用该动作（forbid_actions，v0.9.10 起同时从
白名单硬剔除——提示词声明对推理模型约束不足）。

v0.9.13 扩展（r49 实测修复）：两类新测试——
- 周期动作窗口禁用（_update_cycle_forbid）：end_day 等「每周期一次」动作
  每次执行都真实改变状态（打烊推进天数），零变化 streak 拦不住，改用
  「最近窗口内成功执行过即禁」的动作计数维度；
- 卷帘门幂等守卫（_shutter_guard）：门已处于目标态仍开/关 → 业务失败。
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from game_agent import agent_loop
from game_agent.agent_loop import (
    _CYCLE_FORBID_WINDOW,
    _DEAD_ACTION_STREAK_LIMIT,
    _NIGHT_PHASE_ACTIONS,
    _ONCE_PER_CYCLE_ACTIONS,
    _category_guard,
    _dialog_guard,
    _needs_stow_after,
    _night_ui_guard,
    _parse_leading_int,
    _pre_action_state_guard,
    _rent_reserve_guard,
    _seed_night_phase,
    _shutter_guard,
    _update_cycle_forbid,
    _update_dead_action_tracker,
    _clear_night_marker,
    _night_marker_valid,
    _write_night_marker,
)
from game_agent.brain.prompt import build_decision_prompt
from game_agent.brain.schema import BRIDGE_ACTION_PARAMS, validate_decision
from game_agent.errors import GameAgentError


def _obs():
    return {
        "source": "bridge", "screen_state": "customer_offer",
        "available_actions": [], "important_objects": [], "confidence": 1.0,
    }


class TestDeadActionTracker:
    def test_same_action_no_change_marks_stuck(self):
        """同名动作连续零变化达到阈值 → 产出禁用集。"""
        prev_action, prev_sig = None, None
        same, nc, stuck = 0, 0, set()
        # 第 N 次执行后只能确认前 N-1 次无效——循环 LIMIT+1 次判定成立
        for _ in range(_DEAD_ACTION_STREAK_LIMIT + 1):
            same, nc, stuck = _update_dead_action_tracker(
                prev_action, prev_sig, "accept_offer", (1, 2), same, nc
            )
            prev_action, prev_sig = "accept_offer", (1, 2)
        assert stuck == {"accept_offer"}

    def test_action_change_clears(self):
        """换动作 → 计数重置、不再禁用。"""
        same, nc, stuck = _update_dead_action_tracker(
            "accept_offer", (1,), "call_next", (1,), 3, 3
        )
        assert stuck == set()
        assert same == 1

    def test_state_change_resets_nochange(self):
        """状态发生改变 → no_change 清零（动作有效果，不算 stuck）。"""
        same, nc, stuck = _update_dead_action_tracker(
            "accept_offer", (1,), "accept_offer", (2,), 2, 2
        )
        assert nc == 0
        assert stuck == set()

    def test_no_signature_no_false_positive(self):
        """无签名数据（vision 通道等）→ 不误报。"""
        same, nc, stuck = _update_dead_action_tracker("x", None, "x", None, 5, 5)
        assert nc == 0
        assert stuck == set()

    def test_below_limit_not_stuck(self):
        """未达阈值 → 不禁用。"""
        same, nc, stuck = _update_dead_action_tracker(
            "accept_offer", (1,), "accept_offer", (1,), _DEAD_ACTION_STREAK_LIMIT - 2, 0
        )
        assert stuck == set()


class TestForbidActionsPrompt:
    def test_prompt_contains_ban_notice(self):
        prompt = build_decision_prompt(
            _obs(), bridge_actions=True, no_mouse=True,
            forbid_actions={"accept_offer"},
        )
        assert "无效动作禁用" in prompt
        assert "accept_offer" in prompt

    def test_prompt_without_forbid_actions(self):
        prompt = build_decision_prompt(_obs(), bridge_actions=True, no_mouse=True)
        assert "无效动作禁用" not in prompt


class TestDeadActionHardBlock:
    def test_forbidden_action_rejected_by_validate(self):
        """v0.9.10：被禁动作从白名单剔除后，模型输出会被校验层直接拒绝。"""
        extra = {k: v for k, v in BRIDGE_ACTION_PARAMS.items() if k != "end_day"}
        with pytest.raises(ValueError):
            validate_decision(
                {"action": "end_day", "params": {}, "reason": "r", "confidence": 0.9},
                extra_actions=extra,
            )

    def test_normal_action_still_accepted(self):
        """剔除其它动作不影响正常动作。"""
        extra = {k: v for k, v in BRIDGE_ACTION_PARAMS.items() if k != "end_day"}
        decision = validate_decision(
            {"action": "call_next", "params": {}, "reason": "r", "confidence": 0.9},
            extra_actions=extra,
        )
        assert decision["action"] == "call_next"


def _step(action: str, *, ok: bool = True) -> dict:
    """构造一步已完成记录（决策 + 执行结果）。"""
    return {"decision": {"action": action, "params": {}}, "execution": {"ok": ok}}


class TestCycleActionForbid:
    """v0.9.13 周期动作窗口禁用：end_day 等每周期一次动作，执行过即禁。"""

    def test_end_day_success_forbids_next(self):
        """r49 病理：end_day 成功后下一步必须禁用（而不是拖到零变化 streak）。"""
        steps = [_step("end_day")]
        assert _update_cycle_forbid(steps) == {"end_day"}

    def test_all_once_per_cycle_actions_covered(self):
        """周期一次集合里的动作都走窗口禁用。"""
        for action in sorted(_ONCE_PER_CYCLE_ACTIONS):
            assert _update_cycle_forbid([_step(action)]) == {action}

    def test_still_forbidden_within_window(self):
        """窗口内（隔 3 步）仍禁用。"""
        steps = [_step("end_day")] + [_step("call_next") for _ in range(3)]
        assert _update_cycle_forbid(steps) == {"end_day"}

    def test_released_after_window(self):
        """滑出窗口（隔 7 步）后解禁——不阻碍正常的天数推进。"""
        steps = [_step("end_day")] + [_step("wait") for _ in range(7)]
        assert _update_cycle_forbid(steps) == set()

    def test_failed_execution_not_counted(self):
        """执行失败（ok=false）不计入——没打成烊，不禁下次。"""
        steps = [_step("end_day", ok=False)]
        assert _update_cycle_forbid(steps) == set()

    def test_error_steps_skipped(self):
        """解析失败的错误步（无 decision）天然跳过，不影响窗口计算。"""
        steps = [{"step": 1, "error": "解析失败"}, _step("end_day")]
        assert _update_cycle_forbid(steps) == {"end_day"}

    def test_non_cycle_action_not_affected(self):
        """普通动作不受周期规则影响（call_next 连叫是合法队列遍历）。"""
        steps = [_step("call_next") for _ in range(5)]
        assert _update_cycle_forbid(steps) == set()

    def test_empty_steps(self):
        assert _update_cycle_forbid([]) == set()

    def test_window_constant_sane(self):
        """窗口 ≥ 最小合法夜晚流程步数的一半，且 < 正常经营日步数。"""
        assert _CYCLE_FORBID_WINDOW >= 4


class TestCategoryGuard:
    """v0.9.14 售前类别校验：顾客想收类别与物品类别无交集 → 本地拦截（r51 催生）。

    r52 实测修正：bridge 观察契约把 client/inventory 放在 visible_info 内。
    """

    CLIENT_FOOD = {
        "identifier": "secRequisitionOfficer", "name": "治安部征用官",
        "is_buying": True, "buying_tags": ["FOOD", "MEDICAL"], "buying_ids": [],
    }
    OWNED = [
        {"identifier": "water_kit", "name": "水质检测试剂盒",
         "value": 1, "item_types": ["CHEMICAL"]},
        {"identifier": "nutrifruit", "name": "营养果",
         "value": 12, "item_types": ["FOOD"]},
        {"identifier": "medkit", "name": "医疗包",
         "value": 30, "item_types": ["MEDICAL"]},
    ]

    def _obs(self, *, owned=None, **client_over):
        client = dict(self.CLIENT_FOOD)
        client.update(client_over)
        items = self.OWNED if owned is None else owned
        return {
            "source": "bridge",
            "visible_info": {
                "client": client,
                "inventory": {"owned": items, "not_owned": []},
            },
        }

    def test_mismatch_blocked(self):
        reason = _category_guard(
            {"action": "sell_item", "params": {"identifier": "water_kit"}}, self._obs()
        )
        assert reason is not None
        assert "FOOD" in reason and "CHEMICAL" in reason and "dismiss_client" in reason

    def test_match_passes(self):
        for ident in ("nutrifruit", "medkit"):
            assert _category_guard(
                {"action": "sell_item", "params": {"identifier": ident}}, self._obs()
            ) is None

    def test_no_matching_category_at_all_blocked(self):
        """B 级兜底：清单里没有任何匹配类别（含不在清单里的物品也卖不动）→ 拦。"""
        no_food = [
            {"identifier": "toilet", "name": "一卷厕纸", "value": 25, "item_types": ["TOILETRIES"]},
            {"identifier": "wire", "name": "电子元件", "value": 33, "item_types": ["ELECTRONIC"]},
        ]
        reason = _category_guard(
            {"action": "sell_item", "params": {"identifier": "water_test_container"}},
            self._obs(owned=no_food),
        )
        assert reason is not None and "无货可卖" in reason

    def test_buying_ids_rescues_no_category_match(self):
        """顾客点名（buying_ids）的物品即使类别不匹配也放行。"""
        no_food = [
            {"identifier": "toilet", "name": "一卷厕纸", "value": 25, "item_types": ["TOILETRIES"]},
        ]
        reason = _category_guard(
            {"action": "sell_item", "params": {"identifier": "toilet"}},
            self._obs(owned=no_food, buying_ids=["toilet"]),
        )
        assert reason is None

    def test_customer_not_buying_passes(self):
        """顾客在卖（is_selling）不做买家类别校验。"""
        assert _category_guard(
            {"action": "sell_item", "params": {"identifier": "water_kit"}},
            self._obs(is_buying=False),
        ) is None

    def test_no_client_passes(self):
        obs = {"source": "bridge",
               "visible_info": {"inventory": {"owned": self.OWNED, "not_owned": []}}}
        assert _category_guard(
            {"action": "sell_item", "params": {"identifier": "water_kit"}}, obs
        ) is None

    def test_item_not_in_inventory_passes(self):
        """物品不在清单→归库存守卫/游戏判定；但 B 级仍会因「无匹配类别」兜底。"""
        reason = _category_guard(
            {"action": "sell_item", "params": {"identifier": "ghost"}}, self._obs()
        )
        # 清单里有 FOOD/MEDICAL 物品 → B 级不拦；A 级物品不在清单 → 放行
        assert reason is None

    def test_empty_owned_passes(self):
        assert _category_guard(
            {"action": "sell_item", "params": {"identifier": "water_kit"}},
            self._obs(owned=[]),
        ) is None

    def test_empty_item_types_passes(self):
        owned = [
            {"identifier": "mystery", "name": "神秘物品", "value": 10, "item_types": []},
            {"identifier": "nutrifruit", "name": "营养果", "value": 12, "item_types": ["FOOD"]},
        ]
        assert _category_guard(
            {"action": "sell_item", "params": {"identifier": "mystery"}},
            self._obs(owned=owned),
        ) is None

    def test_empty_buying_tags_passes(self):
        assert _category_guard(
            {"action": "sell_item", "params": {"identifier": "water_kit"}},
            self._obs(buying_tags=[]),
        ) is None

    def test_other_actions_pass(self):
        for action in ("buy_item", "call_next", "wait", "end_day"):
            assert _category_guard(
                {"action": action, "params": {}}, self._obs()
            ) is None

    def test_case_insensitive_tags(self):
        assert _category_guard(
            {"action": "sell_item", "params": {"identifier": "nutrifruit"}},
            self._obs(buying_tags=["food"]),
        ) is None

    def test_toplevel_fallback(self):
        """无 visible_info 时回退顶层 client/inventory（vision 契约兼容）。"""
        obs = {
            "source": "vision",
            "client": dict(self.CLIENT_FOOD),
            "inventory": {"owned": [
                {"identifier": "water_kit", "name": "水质检测试剂盒", "value": 1,
                 "item_types": ["CHEMICAL"]},
            ]},
        }
        reason = _category_guard(
            {"action": "sell_item", "params": {"identifier": "water_kit"}}, obs
        )
        assert reason is not None and "类别不匹配" in reason


class TestNightMarker:
    """v0.9.16 夜晚阶段跨批次标记：文件存在且未陈旧 → 新批次继续锁夜。"""

    def test_write_then_valid(self, tmp_path):
        config = {"paths": {"artifacts_dir": str(tmp_path)}}
        assert _night_marker_valid(config) is False
        _write_night_marker(config)
        assert _night_marker_valid(config) is True

    def test_stale_marker_ignored(self, tmp_path):
        config = {"paths": {"artifacts_dir": str(tmp_path)}}
        marker = tmp_path / "night_phase.flag"
        marker.write_text(
            (datetime.now() - timedelta(hours=3)).isoformat(timespec="seconds"),
            encoding="utf-8",
        )
        assert _night_marker_valid(config) is False

    def test_future_marker_ignored(self, tmp_path):
        config = {"paths": {"artifacts_dir": str(tmp_path)}}
        marker = tmp_path / "night_phase.flag"
        marker.write_text(
            (datetime.now() + timedelta(hours=1)).isoformat(timespec="seconds"),
            encoding="utf-8",
        )
        assert _night_marker_valid(config) is False

    def test_garbage_marker_ignored(self, tmp_path):
        config = {"paths": {"artifacts_dir": str(tmp_path)}}
        (tmp_path / "night_phase.flag").write_text("not-a-timestamp", encoding="utf-8")
        assert _night_marker_valid(config) is False

    def test_clear(self, tmp_path):
        config = {"paths": {"artifacts_dir": str(tmp_path)}}
        _write_night_marker(config)
        _clear_night_marker(config)
        assert _night_marker_valid(config) is False


class _FakeSeedObserver:
    """假结构化观察器：只提供 signature（seed 测试用）。"""

    def __init__(self, signature):
        self.signature = signature

    def observe(self, source, save=False):
        return self


class TestDialogGuard:
    """v1.2 对话/送客守卫：检查官卡店催生（dialog.is_end 推进 / dismissable=false 送客）。"""

    def test_dialog_advance_blocked_when_ended(self):
        obs = {"visible_info": {"dialog": {"is_end": True},
                                "client": {"identifier": "securityInspector"}}}
        reason = _dialog_guard({"action": "dialog_advance"}, obs)
        assert reason is not None and "已结束" in reason

    def test_dialog_advance_allowed_in_progress(self):
        obs = {"visible_info": {"dialog": {"is_end": False}}}
        assert _dialog_guard({"action": "dialog_advance"}, obs) is None

    def test_dismiss_blocked_when_not_dismissable(self):
        obs = {"visible_info": {"client": {"identifier": "securityInspector",
                                           "dismissable": False}}}
        reason = _dialog_guard({"action": "dismiss_client"}, obs)
        assert reason is not None and "不可遣散" in reason

    def test_dismiss_allowed_when_dismissable(self):
        obs = {"visible_info": {"client": {"identifier": "foreman",
                                           "dismissable": True}}}
        assert _dialog_guard({"action": "dismiss_client"}, obs) is None

    def test_other_actions_unaffected(self):
        obs = {"visible_info": {"dialog": {"is_end": True},
                                "client": {"dismissable": False}}}
        for action in ("sell_item", "call_next", "end_day", "wait"):
            assert _dialog_guard({"action": action}, obs) is None

    def test_no_visible_info_passes(self):
        assert _dialog_guard({"action": "dialog_advance"}, {"source": "bridge"}) is None


class _FakeStateClient:
    """最小桥接客户端：只提供 get_state（前置校验用）。"""

    def __init__(self, store_state: str) -> None:
        self.store_state = store_state

    def get_state(self) -> dict:
        return {"in_game": True, "store_state": self.store_state}


class TestPreActionStateGuard:
    """v1.3 实时状态前置校验（r68 跳天实锤：观察过期后夜间动作重复换日）。"""

    def test_night_action_blocked_in_morning(self):
        reason = _pre_action_state_guard("end_day_confirm", _FakeStateClient("MORNING"))
        assert reason is not None and "夜晚动作" in reason

    def test_night_services_blocked_before_closing(self):
        reason = _pre_action_state_guard("night_services", _FakeStateClient("OPEN"))
        assert reason is not None and "夜晚动作" in reason

    def test_night_action_allowed_when_closed(self):
        assert _pre_action_state_guard("end_day_confirm", _FakeStateClient("CLOSED")) is None
        assert _pre_action_state_guard("night_services", _FakeStateClient("CLOSED")) is None

    def test_sell_blocked_when_not_open(self):
        reason = _pre_action_state_guard("sell_item", _FakeStateClient("CLOSED"))
        assert reason is not None and "营业中" in reason
        assert _pre_action_state_guard("sell_item", _FakeStateClient("OPEN")) is None

    def test_open_shutter_blocked_when_closed(self):
        """夜晚重新开门会让当天结算回滚（r53 实测）——必须拦。"""
        reason = _pre_action_state_guard("open_shutter", _FakeStateClient("CLOSED"))
        assert reason is not None and "回滚" in reason
        assert _pre_action_state_guard("open_shutter", _FakeStateClient("MORNING")) is None

    def test_begin_day_blocked_when_closed(self):
        reason = _pre_action_state_guard("begin_day", _FakeStateClient("CLOSED"))
        assert reason is not None
        assert _pre_action_state_guard("begin_day", _FakeStateClient("MORNING")) is None

    def test_guard_is_noop_without_client(self):
        assert _pre_action_state_guard("sell_item", None) is None

    def test_guard_is_noop_when_state_unavailable(self):
        class _Broken:
            def get_state(self):
                raise RuntimeError("bridge down")

        assert _pre_action_state_guard("sell_item", _Broken()) is None

    def test_guard_ignores_not_in_game(self):
        class _NotInGame:
            def get_state(self):
                return {"in_game": False, "store_state": "CLOSED"}

        assert _pre_action_state_guard("sell_item", _NotInGame()) is None


class TestStageFallback:
    """v1.1 阶段兜底：连续模型失败后执行阶段唯一合理动作（r62 催生）。"""

    def test_closing_stage_fallback(self):
        from game_agent.agent_loop import _fallback_action
        assert _fallback_action("closing", {}, "bridge", None) == "end_day_confirm"

    def test_outing_stage_fallback(self):
        from game_agent.agent_loop import _fallback_action
        assert _fallback_action("outing", {}, "bridge", None) == "stay_home"

    def test_night_fallback_follows_live_state(self):
        """v1.3：夜晚兜底按实时状态——MORNING 开张、CLOSED 入睡（r71 白跑一步催生）。"""
        from game_agent.agent_loop import _fallback_action
        assert _fallback_action("outing", {}, "bridge", _FakeStateClient("MORNING")) == "begin_day"
        assert _fallback_action("outing", {}, "bridge", _FakeStateClient("CLOSED")) == "end_day_confirm"
        assert _fallback_action("closing", {}, "bridge", _FakeStateClient("MORNING")) == "begin_day"

    def test_day_deadlock_dismiss(self, tmp_path, monkeypatch):
        """「队列空+顾客在店」→ 兜底送客。"""
        import game_agent.agent_loop as al
        BASE = TestNightUiGuard._BASE
        # [5]=client.identifier 有值；[15]=can_next False（BASE 默认已是 False）
        sig = tuple(BASE[:5]) + ("tester",) + tuple(BASE[6:])
        monkeypatch.setattr(al, "StructuredObserver",
                            lambda *a, **k: _FakeSeedObserver(sig))
        assert al._fallback_action(None, {"paths": {"artifacts_dir": str(tmp_path)}},
                                   "bridge", None) == "dismiss_client"

    def test_day_empty_queue_end_day(self, tmp_path, monkeypatch):
        """无顾客+队列空+OPEN → 兜底打烊。"""
        import game_agent.agent_loop as al
        BASE = TestNightUiGuard._BASE
        sig = tuple(BASE[:2]) + ("OPEN",) + tuple(BASE[3:])  # [2]=OPEN
        monkeypatch.setattr(al, "StructuredObserver",
                            lambda *a, **k: _FakeSeedObserver(sig))
        assert al._fallback_action(None, {"paths": {"artifacts_dir": str(tmp_path)}},
                                   "bridge", None) == "end_day"

    def test_vision_source_no_fallback(self):
        from game_agent.agent_loop import _fallback_action
        assert _fallback_action(None, {}, "vision", None) is None


class TestRentReserveGuard:
    """v0.9.17 交租储备红线：快交租还大笔花 → 拦截（r55：213 现金点 150 批发）。"""

    def _obs(self, *, day_until=2, rent=210, rent_due=None,
             wholesale="150<sprite name=\"c\" tint=1 > (-25%)",
             nego_value=30, is_selling=True):
        rent_info = {"day_until_rent": day_until, "rent_value": rent}
        if rent_due is not None:
            rent_info["rent_due"] = rent_due
        return {
            "source": "bridge",
            "visible_info": {
                "rent": rent_info,
                "table": {"wholesale_cost": wholesale, "negociated_value": nego_value},
                "client": {"identifier": "georgio2", "is_buying": not is_selling,
                           "is_selling": is_selling},
            },
        }

    # 签名 [4]=cash
    SIG = tuple([True, "InventoryScene", "OPEN", False, 213] + [None] * 15)

    def test_buy_item_blocked_when_it_would_break_rent(self):
        """v1.8：buy_item 花 100 后现金 207 < 租金 210 → 拦（r97 实测漏洞）。"""
        obs = {
            "source": "bridge",
            "visible_info": {
                "rent": {"day_until_rent": 2, "rent_value": 210},
                "table": {},
                "client": {"identifier": "seller", "is_buying": False, "is_selling": True},
                "inventory": {"not_owned": [
                    {"identifier": "big_gear", "name": "大零件", "value": 100},
                ]},
            },
        }
        sig = tuple([True, "InventoryScene", "OPEN", False, 307] + [None] * 15)
        reason = _rent_reserve_guard(
            {"action": "buy_item", "params": {"identifier": "big_gear"}}, obs, sig,
        )
        assert reason is not None and "储备红线" in reason

    def test_buy_item_allowed_when_cash_stays_above_rent(self):
        obs = {
            "source": "bridge",
            "visible_info": {
                "rent": {"day_until_rent": 2, "rent_value": 210},
                "table": {},
                "client": {"identifier": "seller", "is_buying": False, "is_selling": True},
                "inventory": {"not_owned": [
                    {"identifier": "small_gear", "name": "小零件", "value": 30},
                ]},
            },
        }
        sig = tuple([True, "InventoryScene", "OPEN", False, 400] + [None] * 15)
        assert _rent_reserve_guard(
            {"action": "buy_item", "params": {"identifier": "small_gear"}}, obs, sig,
        ) is None


    def test_wholesale_buy_blocked_when_rent_unaffordable(self):
        """r55 病理：现金 213 - 批发 150 = 63 < 租金 210 → 拦。"""
        reason = _rent_reserve_guard(
            {"action": "click_button", "params": {"name": "AcceptWholesale"}},
            self._obs(), self.SIG,
        )
        assert reason is not None and "储备红线" in reason and "150" in reason

    def test_wholesale_buy_allowed_when_rent_distant(self):
        """交租日还远（>2 天）→ 放行。"""
        assert _rent_reserve_guard(
            {"action": "click_button", "params": {"name": "AcceptWholesale"}},
            self._obs(day_until=5), self.SIG,
        ) is None

    def test_wholesale_buy_allowed_when_affordable(self):
        """花完仍够交租 → 放行。"""
        sig = tuple(list(self.SIG[:4]) + [500] + list(self.SIG[5:]))
        assert _rent_reserve_guard(
            {"action": "click_button", "params": {"name": "AcceptWholesale"}},
            self._obs(), sig,
        ) is None

    def test_cash_below_rent_due_blocks_any_purchase(self):
        """v1.5b：现金低于真实应缴时，离交租日再远也禁止采购（r79 螺旋催生）。"""
        sig = tuple(list(self.SIG[:4]) + [442] + list(self.SIG[5:]))
        reason = _rent_reserve_guard(
            {"action": "click_button", "params": {"name": "AcceptWholesale"}},
            self._obs(day_until=6, rent=254, rent_due=693, wholesale="335"), sig,
        )
        assert reason is not None and "693" in reason


    def test_rent_due_overrides_nominal_rent(self):
        """v1.5：延期累计后真实应缴(rent_due) > 名义租金 → 按真实额拦。

        r78 实证：名义 242、实际应交 693，现金 442，按名义值会误放行。
        """
        sig = tuple(list(self.SIG[:4]) + [442] + list(self.SIG[5:]))
        reason = _rent_reserve_guard(
            {"action": "click_button", "params": {"name": "AcceptWholesale"}},
            self._obs(day_until=1, rent=242, rent_due=693, wholesale="150"), sig,
        )
        assert reason is not None and "693" in reason

    def test_accept_offer_buy_blocked(self):
        """顾客在卖时 accept_offer = 支出，红线内拦截。"""
        reason = _rent_reserve_guard(
            {"action": "accept_offer", "params": {}}, self._obs(nego_value=200), self.SIG,
        )
        assert reason is not None and "储备红线" in reason

    def test_accept_offer_selling_to_customer_never_blocked(self):
        """顾客在买时 accept_offer = 收款，永远不拦。"""
        assert _rent_reserve_guard(
            {"action": "accept_offer", "params": {}},
            self._obs(is_selling=False), self.SIG,
        ) is None

    def test_other_actions_pass(self):
        for action in ("buy_item", "call_next", "end_day", "wait", "sell_item"):
            assert _rent_reserve_guard(
                {"action": action, "params": {}}, self._obs(), self.SIG,
            ) is None

    def test_unparseable_cost_passes(self):
        """花费不明 → 不误拦（end_day 守卫兜底）。"""
        assert _rent_reserve_guard(
            {"action": "click_button", "params": {"name": "AcceptWholesale"}},
            self._obs(wholesale=""), self.SIG,
        ) is None

    def test_no_signature_passes(self):
        assert _rent_reserve_guard(
            {"action": "click_button", "params": {"name": "AcceptWholesale"}},
            self._obs(), None,
        ) is None

    def test_parse_leading_int(self):
        assert _parse_leading_int("150<sprite name=\"c\" tint=1 > (-25%)") == 150
        assert _parse_leading_int(" 75 (-10%)") == 75
        assert _parse_leading_int("abc") is None
        assert _parse_leading_int(None) is None


class TestNightUiGuard:
    """v0.9.16 夜晚界面幂等守卫：界面已处于目标态 → 业务失败（r54 卡死催生）。"""

    # 签名元组（见 channels.state_signature）共 20 位，关键位：
    # [4]=cash [12]=night.is_outside [19]=night.map_open
    _BASE = (
        True,             # 0 in_game
        "InventoryScene",  # 1 scene
        "CLOSED",         # 2 store_state
        True,             # 3 shutter_closed
        523,              # 4 cash
        None,             # 5 client.identifier
        None,             # 6 is_selling
        None,             # 7 is_buying
        True,             # 8 dialog.is_end
        False,            # 9 dialog.is_choice
        None,             # 10 table_item.identifier
        None,             # 11 negociated_value
        False,            # 12 is_outside
        "STORE",          # 13 current_location
        True,             # 14 sec_button_lit
        False,            # 15 can_next
        False,            # 16 nego_open
        2,                # 17 day_until_rent
        1735,             # 18 stock_value
        False,            # 19 map_open
    )
    SIG_MAP_CLOSED = _BASE
    SIG_MAP_OPEN = _BASE[:19] + (True,)
    SIG_OUTSIDE = _BASE[:12] + (True, "COMMISSARY") + _BASE[14:]

    def test_open_map_when_already_open_blocked(self):
        """r54 病理：地图开着还 open_map ×5 → 拦截。"""
        reason = _night_ui_guard({"action": "open_map"}, self.SIG_MAP_OPEN)
        assert reason is not None and "已打开" in reason and "go_out" in reason

    def test_open_map_when_closed_passes(self):
        assert _night_ui_guard({"action": "open_map"}, self.SIG_MAP_CLOSED) is None

    def test_go_out_when_outside_blocked(self):
        reason = _night_ui_guard({"action": "go_out", "params": {"destination": "commissary"}},
                                 self.SIG_OUTSIDE)
        assert reason is not None and "leave_outside" in reason

    def test_leave_outside_when_inside_blocked(self):
        reason = _night_ui_guard({"action": "leave_outside"}, self.SIG_MAP_CLOSED)
        assert reason is not None and "stay_home" in reason

    def test_night_actions_pass_normally(self):
        """合法夜晚动作不受影响。"""
        assert _night_ui_guard({"action": "stay_home"}, self.SIG_MAP_OPEN) is None
        assert _night_ui_guard({"action": "go_out", "params": {"destination": "commissary"}},
                               self.SIG_MAP_CLOSED) is None
        assert _night_ui_guard({"action": "leave_outside"}, self.SIG_OUTSIDE) is None

    def test_other_actions_pass(self):
        for action in ("call_next", "sell_item", "wait", "end_day"):
            assert _night_ui_guard({"action": action}, self.SIG_MAP_OPEN) is None

    def test_no_signature_passes(self):
        assert _night_ui_guard({"action": "open_map"}, None) is None

    def test_short_signature_passes(self):
        """旧版 19 位签名（无 map_open 位）→ 放行不误拦。"""
        assert _night_ui_guard({"action": "open_map"}, tuple(range(19))) is None


class TestShutterGuard:
    """v0.9.13 卷帘门幂等守卫：门已处于目标态 → 业务失败（不调桥接）。"""

    # 签名元组（见 structured.channels.state_signature）：
    # (in_game, scene, store_state, shutter_closed, cash, ...)
    SIG_OPEN = (True, "InventoryScene", "OPEN", False, 169)
    SIG_CLOSED = (True, "InventoryScene", "CLOSED", True, 169)

    def test_open_when_already_open_blocked(self):
        """r49 病理：已开门还连点 open_shutter → 拦截。"""
        reason = _shutter_guard({"action": "open_shutter"}, self.SIG_OPEN)
        assert reason is not None and "已是开启" in reason

    def test_close_when_already_closed_blocked(self):
        reason = _shutter_guard({"action": "close_shutter"}, self.SIG_CLOSED)
        assert reason is not None and "已是关闭" in reason

    def test_open_when_closed_allowed(self):
        """关门状态开门——合法放行。"""
        assert _shutter_guard({"action": "open_shutter"}, self.SIG_CLOSED) is None

    def test_close_when_open_allowed(self):
        """开门状态关门——合法放行（打烊前第一动作）。"""
        assert _shutter_guard({"action": "close_shutter"}, self.SIG_OPEN) is None

    def test_other_actions_ignored(self):
        assert _shutter_guard({"action": "call_next"}, self.SIG_OPEN) is None
        assert _shutter_guard({"action": "toggle_shutter"}, self.SIG_OPEN) is None

    def test_no_signature_passes(self):
        """vision 通道 / 离线场景无签名 → 放行（与库存守卫同思路）。"""
        assert _shutter_guard({"action": "open_shutter"}, None) is None

    def test_unknown_shutter_state_passes(self):
        sig = (True, "InventoryScene", "OPEN", None, 169)
        assert _shutter_guard({"action": "open_shutter"}, sig) is None


class TestDecisionFieldTolerance:
    """v0.9.13 修复 B：reason/confidence 缺失不再判整步失败（r49 连挂 12 步）。"""

    def test_missing_reason_confidence_accepted(self):
        decision = validate_decision({"action": "wait", "params": {"seconds": 2}})
        assert decision["action"] == "wait"
        assert decision["confidence"] == 0.5  # 缺失按中性置信
        assert "未提供" in decision["reason"]

    def test_missing_action_still_rejected(self):
        """action 仍硬性必填。"""
        with pytest.raises(ValueError, match="action"):
            validate_decision({"params": {}, "reason": "x", "confidence": 0.9})

    def test_invalid_confidence_defaults_neutral(self):
        decision = validate_decision(
            {"action": "wait", "params": {}, "confidence": "abc"}
        )
        assert decision["confidence"] == 0.5

    def test_confidence_clamped(self):
        decision = validate_decision(
            {"action": "wait", "params": {}, "confidence": 5}
        )
        assert decision["confidence"] == 1.0

    def test_bridge_action_without_extra_fields(self):
        """桥接动作只给 action/params 也能过（GLM 常这么输出）。"""
        decision = validate_decision(
            {"action": "end_day", "params": {}},
            extra_actions=BRIDGE_ACTION_PARAMS,
        )
        assert decision["action"] == "end_day"


class TestNightPhaseWhitelist:
    """v0.9.15 夜晚阶段：end_day 后营业动作从白名单剔除（r53「卡第 11 天」根因）。"""

    def test_night_actions_defined(self):
        assert "stay_home" in _NIGHT_PHASE_ACTIONS
        assert "open_map" in _NIGHT_PHASE_ACTIONS
        assert "night_services" in _NIGHT_PHASE_ACTIONS
        assert "begin_day" in _NIGHT_PHASE_ACTIONS
        # 营业动作绝不在名单内
        for bad in ("open_shutter", "call_next", "sell_item", "end_day",
                    "buy_item", "dismiss_client", "accept_offer"):
            assert bad not in _NIGHT_PHASE_ACTIONS

    def test_prompt_banner_in_night_phase(self):
        obs = {
            "source": "bridge", "screen_state": "x",
            "available_actions": [], "important_objects": [],
        }
        prompt = build_decision_prompt(obs, bridge_actions=True, no_mouse=True,
                                       night_phase=True)
        assert "夜晚阶段" in prompt
        assert "stay_home" in prompt and "open_map" in prompt
        assert "重新开门" in prompt  # 回滚警示

    def test_prompt_no_banner_in_day_phase(self):
        obs = {
            "source": "bridge", "screen_state": "x",
            "available_actions": [], "important_objects": [],
        }
        prompt = build_decision_prompt(obs, bridge_actions=True, no_mouse=True)
        # 白天不得出现夜晚阶段横幅（静态夜晚规则段本就有"夜晚阶段"字样，assert 横幅专属文案）
        assert "只能执行夜晚流程动作" not in prompt


class TestSeedNightPhase:
    """v0.9.16：从实时状态 seed 夜晚标志（地图开着/人在外面 ⇒ 夜晚）。"""

    BASE = TestNightUiGuard._BASE

    def test_map_open_seeds_true(self, tmp_path, monkeypatch):
        config = {"paths": {"artifacts_dir": str(tmp_path)}}
        monkeypatch.setattr(
            agent_loop, "StructuredObserver",
            lambda *a, **k: _FakeSeedObserver(tuple(self.BASE[:19]) + (True,)),
        )
        assert _seed_night_phase(config, "bridge", None) is True

    def test_outside_seeds_true(self, tmp_path, monkeypatch):
        config = {"paths": {"artifacts_dir": str(tmp_path)}}
        monkeypatch.setattr(
            agent_loop, "StructuredObserver",
            lambda *a, **k: _FakeSeedObserver(
                tuple(self.BASE[:12]) + (True, "COMMISSARY") + tuple(self.BASE[14:])),
        )
        assert _seed_night_phase(config, "bridge", None) is True

    def test_day_state_seeds_false(self, tmp_path, monkeypatch):
        config = {"paths": {"artifacts_dir": str(tmp_path)}}
        monkeypatch.setattr(
            agent_loop, "StructuredObserver",
            lambda *a, **k: _FakeSeedObserver(tuple(self.BASE)),
        )
        assert _seed_night_phase(config, "bridge", None) is False

    def test_morning_state_seeds_false(self, tmp_path, monkeypatch):
        """MORNING = 新一天早晨（夜晚已过）；map_open 陈旧也不能锁夜。"""
        config = {"paths": {"artifacts_dir": str(tmp_path)}}
        sig = tuple(self.BASE[:2]) + ("MORNING",) + tuple(self.BASE[3:19]) + (True,)
        monkeypatch.setattr(
            agent_loop, "StructuredObserver",
            lambda *a, **k: _FakeSeedObserver(sig),
        )
        assert _seed_night_phase(config, "bridge", None) is False

    def test_vision_channel_seeds_false(self, tmp_path):
        config = {"paths": {"artifacts_dir": str(tmp_path)}}
        assert _seed_night_phase(config, "vision", None) is False

    def test_observe_failure_seeds_false(self, tmp_path, monkeypatch):
        config = {"paths": {"artifacts_dir": str(tmp_path)}}

        class _Boom:
            def __init__(self, *a, **k):
                pass

            def observe(self, source, save=False):
                raise GameAgentError("bridge down")

        monkeypatch.setattr(agent_loop, "StructuredObserver", _Boom)
        assert _seed_night_phase(config, "bridge", None) is False


class TestStowAfterDeal:
    """v0.9.18 成交后自动收纳判定：批发买入成交 → 触发 transferButton 收纳。"""

    def test_accept_wholesale_triggers(self):
        assert _needs_stow_after(
            {"action": "click_button", "params": {"name": "AcceptWholesale"}}
        ) is True

    def test_chinese_text_button_triggers(self):
        assert _needs_stow_after(
            {"action": "click_button", "params": {"text": "接受批发"}}
        ) is True

    def test_name_case_insensitive(self):
        assert _needs_stow_after(
            {"action": "click_button", "params": {"name": "acceptwholesale"}}
        ) is True

    def test_other_click_buttons_do_not_trigger(self):
        """其它按钮（转移/关闭/议价）不触发自动收纳。"""
        for name in ("transferButton", "CloseButton", "BargainButton"):
            assert _needs_stow_after(
                {"action": "click_button", "params": {"name": name}}
            ) is False

    def test_other_actions_do_not_trigger(self):
        for action in ("buy_item", "accept_offer", "call_next", "end_day", "wait"):
            assert _needs_stow_after({"action": action, "params": {}}) is False

    def test_empty_params_do_not_trigger(self):
        assert _needs_stow_after({"action": "click_button", "params": {}}) is False


class TestCycleForbidPrompt:
    """周期禁用文案与无效禁用文案并存、互不遮掩。"""

    def test_cycle_notice_present(self):
        obs = {
            "source": "bridge", "screen_state": "x",
            "available_actions": [], "important_objects": [],
        }
        prompt = build_decision_prompt(
            obs, bridge_actions=True, no_mouse=True, cycle_forbid={"end_day"},
        )
        assert "周期动作禁用" in prompt
        assert "end_day" in prompt
        assert "无效动作禁用" not in prompt

    def test_both_notices_coexist(self):
        obs = {
            "source": "bridge", "screen_state": "x",
            "available_actions": [], "important_objects": [],
        }
        prompt = build_decision_prompt(
            obs, bridge_actions=True, no_mouse=True,
            forbid_actions={"accept_offer"}, cycle_forbid={"end_day"},
        )
        assert "无效动作禁用" in prompt
        assert "周期动作禁用" in prompt


class _FakeButtonsClient:
    """最小桥接客户端：只提供 get_ui_buttons（晨报面板检测用）。"""

    def __init__(self, buttons=None, exc: Exception | None = None) -> None:
        self.buttons = buttons or []
        self.exc = exc

    def get_ui_buttons(self) -> dict:
        if self.exc is not None:
            raise self.exc
        return {"ok": True, "buttons": self.buttons}


class TestMorningReportGuard:
    """v1.6 晨报面板硬守卫（用户 2026-10-20 指令：面板出来必须立刻叉掉）。"""

    def test_forces_close_when_calendar_panel_open(self):
        client = _FakeButtonsClient([
            {"name": "StatusButton", "path": "CanvasPaperUI/_AdvCalendar/TopToggleGroup/StatusButton"},
            {"name": "CloseButton", "path": "CanvasPaperUI/_AdvCalendar/CloseButton"},
        ])
        decision = agent_loop._morning_report_guard(client)
        assert decision is not None
        assert decision["action"] == "click_button"
        assert decision["params"] == {"name": "CloseButton", "path": "_AdvCalendar"}
        assert decision["confidence"] >= 0.9

    def test_no_force_when_panel_closed(self):
        client = _FakeButtonsClient([
            {"name": "CloseButton", "path": "CanvasPaperUI/_NegociationPanel/CloseButton"},
        ])
        assert agent_loop._morning_report_guard(client) is None

    def test_ignores_inactive_calendar_buttons(self):
        client = _FakeButtonsClient([
            {"name": "CloseButton", "path": "CanvasPaperUI/_AdvCalendar/CloseButton", "active": False},
        ])
        assert agent_loop._morning_report_guard(client) is None

    def test_silent_when_bridge_unavailable(self):
        assert agent_loop._morning_report_guard(None) is None
        assert agent_loop._morning_report_guard(_FakeButtonsClient(exc=RuntimeError("boom"))) is None




class TestCleanupAfterScenes:
    """v3.16：顾客离开/成交/打烊后要清柜并搬回储物区。

    背景：用户实机反馈「桌子上面的东西怎么一直在桌上」——
    原来只有批发成交后才收纳，其余场景的柜台残留没人管。
    """

    def _f(self):
        from game_agent.agent_loop import _needs_cleanup_after

        return _needs_cleanup_after

    def test_scene_actions_trigger_cleanup(self):
        fn = self._f()
        for action in ("dismiss_client", "end_day", "accept_offer", "buy_wholesale",
                       "accept_barter", "call_next"):
            assert fn({"action": action, "params": {}}) is True, action

    def test_ordinary_actions_do_not_trigger(self):
        fn = self._f()
        for action in ("sell_item", "buy_item", "wait", "dialog_advance", "move_item"):
            assert fn({"action": action, "params": {}}) is False, action

    def test_wholesale_click_still_triggers(self):
        fn = self._f()
        assert fn({"action": "click_button", "params": {"name": "AcceptWholesale"}}) is True
        assert fn({"action": "click_button", "params": {"name": "transferButton"}}) is False

    def test_is_superset_of_stow(self):
        """cleanup 必须包含 stow 的全部触发场景（否则批发买入的货会漏收）。"""
        from game_agent.agent_loop import _needs_cleanup_after, _needs_stow_after

        for d in (
            {"action": "click_button", "params": {"name": "AcceptWholesale"}},
            {"action": "click_button", "params": {"text": "接受批发"}},
        ):
            assert _needs_stow_after(d) is True
            assert _needs_cleanup_after(d) is True
