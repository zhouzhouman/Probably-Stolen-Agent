"""v0.8.5「操作纪律」模块测试：动画等待闸门 + 失败自动恢复（全离线）。

背景（用户 2026-10-16 14:17 反馈「调用接口要让动画过完」）：
- 加快连调接口会跳过游戏内收纳/移动动画 → 物品残留、流程错位；
- 修复：① live 动作前检查 GameLog 事件流的「在途动画」，等待播完（超时放行）；
        ② 卖出失败自动 clear_table 清桌回收。

覆盖点：
1. pending_animation_reason：GameLog 尾部在途动画判定（新鲜/已完成/超窗/无日志）
2. wait_for_animation_settle：轮询等待收敛与超时放行（不卡死）
3. step_once：闸门启用时记录等待；禁用时跳过
4. step_once：失败自动恢复（sell_item 失败 → clear_table；成功/其它动作不触发）
"""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from game_agent import agent_loop
from game_agent.agent_loop import (
    pending_animation_reason,
    step_once,
    wait_for_animation_settle,
    wait_for_state_settle,
)
from game_agent.control.controller import ControlResult
from game_agent.errors import BridgeError


# ---------------------------------------------------------------- 测试工具


def _write_gamelog(tmp_path: Path, lines: list[str]) -> Path:
    """在 tmp_path 造一个 GameLog 文件（模拟真实日志目录的最小环境）。"""
    log = tmp_path / "GameLog_2026-10-16_00-00-00.txt"
    log.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return log


def _line(message: str, age_seconds: float = 0.0) -> str:
    """造一行日志；age_seconds 为「多少秒前」（用当前时间回推，保证新鲜）。"""
    ts = (datetime.now() - timedelta(seconds=age_seconds)).strftime("%H:%M:%S")
    return f"[{ts}] [Log] {message}"


def _config(tmp_path: Path) -> dict:
    return {"statewatch": {"data_dir": str(tmp_path)}}


def _obs_record() -> dict:
    return {
        "observation": {
            "screen_state": "store", "available_actions": [],
            "important_objects": [], "confidence": 0.9, "visible_info": None,
        },
    }


def _decision(action: str, params: dict | None = None) -> dict:
    return {"action": action, "params": params or {}, "reason": "test", "confidence": 1.0}


# ================================================================ 1. 在途动画判定


class TestPendingAnimationReason:
    def test_action_started_pending(self, tmp_path):
        _write_gamelog(tmp_path, [_line("Start Action Executed")])
        reason = pending_animation_reason(_config(tmp_path))
        assert reason is not None
        assert "动作动画" in reason

    def test_action_completed_releases(self, tmp_path):
        _write_gamelog(tmp_path, [
            _line("Start Action Executed", age_seconds=6),
            _line("End Action Executed", age_seconds=3),
        ])
        assert pending_animation_reason(_config(tmp_path)) is None

    def test_client_approaching_pending(self, tmp_path):
        _write_gamelog(tmp_path, [_line("Arriving", age_seconds=1)])
        reason = pending_animation_reason(_config(tmp_path))
        assert reason is not None
        assert "顾客移动" in reason

    def test_client_arrived_releases(self, tmp_path):
        _write_gamelog(tmp_path, [
            _line("Arriving", age_seconds=3),
            _line("Client Arrived", age_seconds=1),
        ])
        assert pending_animation_reason(_config(tmp_path)) is None

    def test_stale_pending_ignored(self, tmp_path):
        """未配对但发生在窗口外的事件视作历史残留，不再等待（防死等）。"""
        _write_gamelog(tmp_path, [_line("Arriving", age_seconds=60)])
        assert pending_animation_reason(_config(tmp_path), window_seconds=15.0) is None

    def test_no_log_file_releases(self, tmp_path):
        assert pending_animation_reason(_config(tmp_path)) is None

    def test_unrelated_events_release(self, tmp_path):
        _write_gamelog(tmp_path, [
            _line("Setting locale to: zh"),
            _line("UI scale for game area 1920x1080: 2"),
        ])
        assert pending_animation_reason(_config(tmp_path)) is None


# ================================================================ 2. 等待收敛与超时


class TestWaitForAnimationSettle:
    def test_polls_until_clear(self, monkeypatch):
        seq = ["顾客移动进行中（x）", "顾客移动进行中（x）", None]
        calls = {"n": 0}

        def fake_reason(config, *, window_seconds=15.0, now=None):
            i = min(calls["n"], len(seq) - 1)
            calls["n"] += 1
            return seq[i]

        monkeypatch.setattr(agent_loop, "pending_animation_reason", fake_reason)
        settled, waited, reason = wait_for_animation_settle(
            {"action_gate": {"poll_interval_seconds": 0.01, "settle_timeout_seconds": 5.0}}
        )
        assert settled is True
        assert waited >= 0.015
        assert reason == ""

    def test_timeout_releases_with_reason(self, monkeypatch):
        def fake_reason(config, *, window_seconds=15.0, now=None):
            return "动作动画进行中（x）"

        monkeypatch.setattr(agent_loop, "pending_animation_reason", fake_reason)
        settled, waited, reason = wait_for_animation_settle(
            {"action_gate": {"poll_interval_seconds": 0.01, "settle_timeout_seconds": 0.05}}
        )
        assert settled is False
        assert reason  # 超时放行且携带挂起原因
        assert waited >= 0.05


# ================================================================ 3/4. step_once 集成


def _patch_loop(monkeypatch, decision_action: str, fail_actions: set[str]):
    """替换 decide / execute_decision，记录执行动作序列并制造指定失败。"""
    calls: list[str] = []

    def fake_decide(config, observation, **kwargs):
        return _decision(decision_action), {}

    def fake_execute(config, decision, *, live=False, window=None,
                     bridge_client=None, allow_mouse=True):
        action = decision["action"]
        calls.append(action)
        ok = action not in fail_actions
        return ControlResult(
            action=action, params=decision.get("params") or {},
            executed=True, ok=ok,
            detail=f"{action}:{'ok' if ok else 'fail'}", duration_ms=1,
        )

    monkeypatch.setattr(agent_loop, "decide", fake_decide)
    monkeypatch.setattr(agent_loop, "execute_decision", fake_execute)
    return calls


_BASE_CFG = {"control": {"after_action_delay": 0}, "action_gate": {"enabled": False}}


class TestFailureRecovery:
    def test_sell_failure_triggers_clear_table(self, monkeypatch):
        calls = _patch_loop(monkeypatch, "sell_item", fail_actions={"sell_item"})
        record = step_once(
            _BASE_CFG, live=True, obs_override=_obs_record(),
            push_overlay=False, save=False,
        )
        assert calls == ["sell_item", "clear_table"]
        assert record["recovery"]["trigger"] == "sell_item"
        assert record["recovery"]["action"] == "clear_table"
        assert record["recovery"]["ok"] is True

    def test_sell_success_no_recovery(self, monkeypatch):
        calls = _patch_loop(monkeypatch, "sell_item", fail_actions=set())
        record = step_once(
            _BASE_CFG, live=True, obs_override=_obs_record(),
            push_overlay=False, save=False,
        )
        assert calls == ["sell_item"]
        assert "recovery" not in record

    def test_other_action_failure_no_recovery(self, monkeypatch):
        calls = _patch_loop(monkeypatch, "buy_item", fail_actions={"buy_item"})
        record = step_once(
            _BASE_CFG, live=True, obs_override=_obs_record(),
            push_overlay=False, save=False,
        )
        assert calls == ["buy_item"]
        assert "recovery" not in record

    def test_dry_run_no_recovery(self, monkeypatch):
        """dry-run 下不触发恢复（恢复也是真实动作，只在 live 生效）。"""
        calls = _patch_loop(monkeypatch, "sell_item", fail_actions={"sell_item"})
        record = step_once(
            _BASE_CFG, live=False, obs_override=_obs_record(),
            push_overlay=False, save=False,
        )
        assert calls == ["sell_item"]
        assert "recovery" not in record


class TestGateIntegration:
    def test_gate_records_wait_when_busy(self, monkeypatch):
        """日志通道：在途动画被等待并记录（状态通道 monkeypatch 为秒过）。"""
        seq = ["顾客移动进行中（x）", None]

        def fake_reason(config, *, window_seconds=15.0, now=None):
            return seq.pop(0) if seq else None

        monkeypatch.setattr(agent_loop, "wait_for_state_settle",
                            lambda cfg, c: (True, 0.0, ""))
        calls = _patch_loop(monkeypatch, "open_shutter", fail_actions=set())
        monkeypatch.setattr(agent_loop, "pending_animation_reason", fake_reason)
        cfg = {
            "control": {"after_action_delay": 0},
            "action_gate": {"enabled": True, "poll_interval_seconds": 0.1,
                            "settle_timeout_seconds": 2.0},
        }
        record = step_once(
            cfg, live=True, obs_override=_obs_record(),
            push_overlay=False, save=False,
        )
        assert calls == ["open_shutter"]
        gate = record.get("action_gate")
        assert gate is not None
        assert gate["settled"] is True
        assert gate["waited_seconds"] > 0

    def test_gate_state_channel_waits(self, monkeypatch):
        """实时通道：状态稳定等待被记录（注入假桥客户端，不触真实 HTTP）。"""

        class _StableClient:
            def get_state(self):
                return {"cash": 100, "scene": "store"}

        def _no_pending(config, *, window_seconds=15.0, now=None):
            return None

        calls = _patch_loop(monkeypatch, "open_shutter", fail_actions=set())
        monkeypatch.setattr(agent_loop, "pending_animation_reason", _no_pending)
        cfg = {
            "control": {"after_action_delay": 0},
            "action_gate": {"enabled": True, "state_probe_interval_seconds": 0.1,
                            "settle_timeout_seconds": 2.0},
        }
        record = step_once(
            cfg, live=True, obs_override=_obs_record(), bridge_client=_StableClient(),
            push_overlay=False, save=False,
        )
        assert calls == ["open_shutter"]
        gate = record.get("action_gate")
        assert gate is not None
        assert gate["waited_seconds"] > 0

    def test_gate_disabled_skips(self, monkeypatch):
        def _boom(*args, **kwargs):
            raise AssertionError("闸门被禁用时不应调用任何闸门通道")

        calls = _patch_loop(monkeypatch, "open_shutter", fail_actions=set())
        monkeypatch.setattr(agent_loop, "wait_for_state_settle", _boom)
        monkeypatch.setattr(agent_loop, "pending_animation_reason", _boom)
        record = step_once(
            _BASE_CFG, live=True, obs_override=_obs_record(),
            push_overlay=False, save=False,
        )
        assert calls == ["open_shutter"]
        assert "action_gate" not in record

    def test_gate_skipped_for_wait_action(self, monkeypatch):
        def _boom(*args, **kwargs):
            raise AssertionError("wait 动作不应触发闸门")

        _patch_loop(monkeypatch, "wait", fail_actions=set())
        monkeypatch.setattr(agent_loop, "wait_for_state_settle", _boom)
        monkeypatch.setattr(agent_loop, "pending_animation_reason", _boom)
        cfg = {
            "control": {"after_action_delay": 0},
            "action_gate": {"enabled": True},
        }
        step_once(
            cfg, live=True, obs_override=_obs_record(),
            push_overlay=False, save=False,
        )


# ================================================================ 2b. 状态稳定闸门（实时通道）


class _SeqClient:
    """测试用假桥客户端：按序列返回状态（最后一次重复）。"""

    def __init__(self, states):
        self.states = list(states)

    def get_state(self):
        if len(self.states) > 1:
            return self.states.pop(0)
        return self.states[0]


class _CountingClient:
    """测试用假桥客户端：每次返回不同现金（模拟状态持续变化）。"""

    def __init__(self):
        self.n = 0

    def get_state(self):
        self.n += 1
        return {"cash": self.n, "scene": "store"}


class TestWaitForStateSettle:
    def test_settles_when_stable(self):
        client = _SeqClient([{"cash": 1}, {"cash": 2}, {"cash": 2}])
        settled, waited, reason = wait_for_state_settle(
            {"action_gate": {"state_probe_interval_seconds": 0.01,
                             "settle_timeout_seconds": 5.0}},
            client,
        )
        assert settled is True
        assert waited >= 0.015
        assert reason == ""

    def test_timeout_when_always_changing(self):
        settled, waited, reason = wait_for_state_settle(
            {"action_gate": {"state_probe_interval_seconds": 0.01,
                             "settle_timeout_seconds": 0.05}},
            _CountingClient(),
        )
        assert settled is False
        assert "持续变化" in reason

    def test_bridge_down_releases(self):
        class _DownClient:
            def get_state(self):
                raise BridgeError("bridge down")

        settled, waited, reason = wait_for_state_settle({}, _DownClient())
        assert settled is True
        assert waited == 0.0
        assert reason == ""
