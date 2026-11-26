"""v0.9.3 库存守卫测试：交易前校验 identifier 真实在库（防「凭空卖」，全离线）。

背景（用户 2026-10-16 15:30 指令）："卖东西要看自己有什么，不要凭空卖"——
r43 实测 AI 把 FOOD 类"泥炭合成肉"幻觉成 MATERIAL 卖给要材料的顾客
（游戏守卫最终拒绝，但浪费步数且观感差）；本守卫为代码级最终防线。
"""
from __future__ import annotations

from game_agent import agent_loop
from game_agent.agent_loop import _inventory_guard, step_once
from game_agent.control.controller import ControlResult


def _obs(owned=None, not_owned=None):
    return {"visible_info": {"inventory": {"owned": owned or [], "not_owned": not_owned or []}}}


def _dec(action, ident):
    return {"action": action, "params": {"identifier": ident}, "reason": "t", "confidence": 1.0}


def _patch_loop(monkeypatch, decision, fail=False):
    calls = []

    def fake_decide(config, observation, **kwargs):
        return decision, {}

    def fake_execute(config, dec, *, live=False, window=None, bridge_client=None, allow_mouse=True):
        calls.append(dec["action"])
        return ControlResult(action=dec["action"], params={}, executed=True,
                             ok=not fail, detail="exec", duration_ms=1)

    monkeypatch.setattr(agent_loop, "decide", fake_decide)
    monkeypatch.setattr(agent_loop, "execute_decision", fake_execute)
    return calls


_CFG = {"control": {"after_action_delay": 0}, "action_gate": {"enabled": False}}


class TestInventoryGuard:
    def test_sell_in_owned_passes(self):
        obs = _obs(owned=[{"identifier": "toothpaste"}])
        assert _inventory_guard(_dec("sell_item", "toothpaste"), obs) is None

    def test_sell_not_in_owned_blocks(self):
        obs = _obs(owned=[{"identifier": "toothpaste"}])
        msg = _inventory_guard(_dec("sell_item", "processed_meat"), obs)
        assert msg is not None
        assert "库存守卫" in msg

    def test_buy_in_not_owned_passes(self):
        obs = _obs(not_owned=[{"identifier": "hatchet"}])
        assert _inventory_guard(_dec("buy_item", "hatchet"), obs) is None

    def test_buy_not_in_not_owned_blocks(self):
        obs = _obs(not_owned=[{"identifier": "hatchet"}])
        assert _inventory_guard(_dec("buy_item", "ghost"), obs) is not None

    def test_non_trade_action_passes(self):
        obs = _obs(owned=[{"identifier": "x"}])
        assert _inventory_guard(_dec("call_next", "x"), obs) is None

    def test_no_inventory_data_passes(self):
        """无库存数据（vision/异常观察）：不校验、不误拦。"""
        assert _inventory_guard(_dec("sell_item", "anything"), {"screen_state": "store"}) is None
        assert _inventory_guard(_dec("sell_item", "anything"), None) is None


class TestGuardIntegration:
    def test_blocked_no_execute_no_recovery(self, monkeypatch):
        calls = _patch_loop(monkeypatch, _dec("sell_item", "ghost_item"), fail=True)
        rec = step_once(
            _CFG, live=True,
            obs_override={"observation": _obs(owned=[{"identifier": "toothpaste"}])},
            push_overlay=False, save=False,
        )
        assert calls == []  # 守卫拦截：未执行
        assert rec["execution"]["ok"] is False
        assert "库存守卫" in rec["execution"]["detail"]
        assert "recovery" not in rec  # 不触发清桌

    def test_in_stock_executes(self, monkeypatch):
        calls = _patch_loop(monkeypatch, _dec("sell_item", "toothpaste"), fail=False)
        rec = step_once(
            _CFG, live=True,
            obs_override={"observation": _obs(owned=[{"identifier": "toothpaste"}])},
            push_overlay=False, save=False,
        )
        assert calls == ["sell_item"]
        assert rec["execution"]["ok"] is True

    def test_sell_fail_with_stock_triggers_recovery(self, monkeypatch):
        """真实执行失败（物品在库）→ 仍触发清桌恢复（回归保障）。"""
        calls = _patch_loop(monkeypatch, _dec("sell_item", "toothpaste"), fail=True)
        rec = step_once(
            _CFG, live=True,
            obs_override={"observation": _obs(owned=[{"identifier": "toothpaste"}])},
            push_overlay=False, save=False,
        )
        assert calls == ["sell_item", "clear_table"]
        assert rec["recovery"]["action"] == "clear_table"
