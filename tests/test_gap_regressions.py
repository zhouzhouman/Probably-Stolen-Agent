"""X03 补测：硬守卫事故回归 + 成交判定铁律 + state_signature 形状 + 禁止作弊接口。

本文件是 R7 在 docs/26 §X03「测试治理」下、按 Lead 的 C 类补测裁决（只准四类、
15-20 个 test 函数）写的行为断言集合。每条测试的 docstring 第一行都以
``GAP: <缺口名>`` 开头，便于审计「这条到底补的是哪个缺口」。

四类缺口与来源：
  (a) 硬守卫事故回归 —— r36 / r49 / r55 / r68 / r78 / r97 六起事故，docs/26 §1
      硬约束② 要求守卫语义不可退化。其中 r55（拾荒离场丢光战利品，一夜 12 件）
      是全项目最贵回归，单独占 4 条。
  (b) 成交判定铁律 —— docs/26 §1 硬约束③：只认现金/库存/天数 diff，``ok=true``
      单独出现不算成交；未验出真伪不得吐 realCondition。
  (c) ``state_signature`` 形状 —— 为 X01/T13 的签名同源化提供红绿灯：
      ``len(state_signature({})) == len(SIG_FIELDS)`` 且逐位语义正确。
  (d) 禁止作弊接口 —— ``/action`` 白名单里不得出现能直接改现金/天数的动作名。

写测试时的两条纪律（Lead m00152/m00397 裁决）：
  1. **不做源码扫描**做「守卫读了某字段即失效」的硬断言 —— 那种命题会随字段补齐
     变成永久假红。守卫失效只由**行为**判定（见 ``TestScavengePickupGuard``）。
  2. 唯一的源码扫描是 (d) 与「Demo 枚举残留」这类**字面量白名单**检查：它们断言
     的是「代码里不该出现的字符串」，与实现方式无关，不会随字段/逻辑演进而假红。
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import pytest

import game_agent.agent_loop as loop
import game_agent.contracts as C
from game_agent.agent_loop import (
    _at_scavenging_site,
    _pre_action_state_guard,
    _rent_reserve_guard,
    _update_cycle_forbid,
)
from game_agent.brain.prompt import build_decision_prompt
from game_agent.brain.schema import BRIDGE_ACTION_PARAMS, validate_decision
from game_agent.bridge.client import ACTIONS, BridgeClient
from game_agent.goal import BusinessGoal
from game_agent.statewatch.state import LogEvent, rebuild_client_sessions
from game_agent.structured.channels import state_signature
from game_agent.structured.observer import (
    _SCAVENGING_LOCATION_IDS,
    _derive_available_actions,
    build_observation,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
GAME_AGENT_DIR = REPO_ROOT / "game_agent"

#: observer 展开到 ``visible_info`` 顶层、但**不是** ``/state`` 原始字段的合成块。
OBSERVER_SYNTHESIZED_TOPLEVEL = frozenset({"rent", "sellable_now", "money"})


# ==========================================================================
# A. 硬守卫事故回归（r36 / r49 / r68 / r78 / r97）
# ==========================================================================


class TestGuardIncidentRegressions:
    """六起硬守卫事故的最小回归钉（r55 单独成类，见下）。"""

    def test_r36_forbid_wait_removes_wait_from_prompt(self):
        """GAP: r36 连续 wait 反空转 —— 只断言「wait 从可选动作清单消失」，不靠提示词劝导。

        r36 的教训是：对推理模型，「绝不用 wait」这句劝导无效，必须把 wait 从可选
        动作里**物理移除**（``NO_WAIT_ACTION_TOOLS``）。提示词正文里仍会出现
        ``wait`` 字样（例如「禁止 wait」的告示、以及描述界面时的举例），所以判据
        只能是**工具规格行**在不在，而不是「全文有没有 wait」。
        """
        obs = {"visible_info": {"money": 100}}
        normal = build_decision_prompt(obs, bridge_actions=True, forbid_wait=False)
        blocked = build_decision_prompt(obs, bridge_actions=True, forbid_wait=True)
        wait_spec = "- wait："
        assert wait_spec in normal, "基线：正常情况下 wait 的动作规格行本应出现"
        assert wait_spec not in blocked, "forbid_wait=True 时 wait 必须从动作规格清单中移除"
        # 同时必须显式告知模型「本轮禁止 wait」，否则模型会去猜
        assert "禁止 wait" in blocked
        # 且提示词整体变短（wait 的规格与参数说明被整段摘掉）
        assert len(blocked) < len(normal)

    def test_r36_forbid_wait_keeps_stop_and_bridge_actions(self):
        """GAP: r36 反空转不得连坐 —— 摘掉 wait 后 stop 与桥接直调动作必须还在。"""
        obs = {"visible_info": {"money": 100}}
        blocked = build_decision_prompt(obs, bridge_actions=True, forbid_wait=True)
        assert "stop" in blocked
        assert "sell_item" in blocked, "反空转只该摘 wait，不能把桥接动作一起摘掉"

    def test_r49_cycle_forbid_blocks_once_per_cycle_action(self):
        """GAP: r49 周期动作重复触发 —— end_day 成功执行后必须在下个决策点被禁用。"""
        assert "end_day" in loop._ONCE_PER_CYCLE_ACTIONS
        steps = [{"decision": {"action": "end_day"}, "execution": {"ok": True}}]
        assert _update_cycle_forbid(steps) == {"end_day"}
        # 反向：干过但失败了不算「本周期已用」，否则玩家永远没法重试
        failed = [{"decision": {"action": "end_day"}, "execution": {"ok": False}}]
        assert _update_cycle_forbid(failed) == set()

    def test_r49_cycle_forbid_window_expires(self):
        """GAP: r49 禁用集必须出窗失效 —— 否则一次 end_day 会永久封死该动作。"""
        steps = [{"decision": {"action": "end_day"}, "execution": {"ok": True}}]
        steps += [
            {"decision": {"action": "wait"}, "execution": {"ok": True}}
            for _ in range(loop._CYCLE_FORBID_WINDOW)
        ]
        assert _update_cycle_forbid(steps) == set()

    def test_r68_pre_action_guard_blocks_day_action_when_store_closed(self):
        """GAP: r68 跳天实锤 —— 店铺已 CLOSED 时白天动作必须下发前拦截，不能真发出去。"""

        class _Client:
            def get_state(self):
                return {"in_game": True, "store_state": "CLOSED"}

        detail = _pre_action_state_guard("sell_item", _Client())
        assert detail is not None
        assert "CLOSED" in detail
        # 反向：营业中同一动作必须放行（防「无脑全拦」把正常流程也堵死）
        class _Open:
            def get_state(self):
                return {"in_game": True, "store_state": "OPEN"}

        assert _pre_action_state_guard("sell_item", _Open()) is None

    def test_r68_pre_action_guard_blocks_night_action_when_store_open(self):
        """GAP: r68 反向 —— 店铺 OPEN 时夜晚动作同样必须拦（会空转/重复换日）。"""

        class _Client:
            def get_state(self):
                return {"in_game": True, "store_state": "OPEN"}

        detail = _pre_action_state_guard("open_map", _Client())
        assert detail is not None
        assert "open_map" in detail

    def test_r78_rent_guard_does_not_block_when_cost_unknown(self):
        """GAP: r78 真伪房租 —— 花费未知时不得拿「假想花费」当拦截依据（只按现金红线和放行）。

        r78 的 pathology 是「名义租金 242、实际应交 693，按名义值算会误放行」，
        已由 I 组（``rent_due`` 优先取 max）钉死。这里补的是另一个方向：
        花费**不可知**时不能凭空造一个花费去拦，否则现金充足的正常批发也会被堵。
        """
        obs = {
            "visible_info": {
                "rent": {"day_until_rent": 1, "rent_value": 242, "rent_due": 0},
                "table": {},
                "client": {"is_selling": True},
                "inventory": {"owned": [], "not_owned": []},
            }
        }
        sig = state_signature({"in_game": True, "cash": 5000})
        decision = {"action": "click_button", "params": {"name": "wholesaleBuyButton"}}
        assert _rent_reserve_guard(decision, obs, sig) is None

    def test_r97_rent_guard_covers_buy_table(self):
        """GAP: r97 直购族漏网 —— buy_table 也必须走交租红线（v1.8 才补的这个分支）。"""
        obs = {
            "visible_info": {
                "rent": {"day_until_rent": 1, "rent_value": 242, "rent_due": 242},
                "table": {"negociated_value": 150},
                "client": {"is_selling": True},
                "inventory": {"owned": [], "not_owned": []},
            }
        }
        sig = state_signature({"in_game": True, "cash": 250})
        detail = _rent_reserve_guard({"action": "buy_table", "params": {}}, obs, sig)
        assert detail is not None
        assert "交租储备红线" in detail


# ==========================================================================
# B. r55 拾荒守卫（全项目最贵回归：一夜毁 12 件）
# ==========================================================================


def _scavenge_obs(location: str, *, is_outside: bool = True) -> dict[str, Any]:
    return {"visible_info": {"night": {"current_location": location, "is_outside": is_outside}}}


class TestScavengePickupGuard:
    """r55：模型在拾荒点选 leave_outside 会把地面战利品全丢光（一夜 12 件）。

    守卫有两处独立实现，必须**双值兼容**（正式版 id ``dumping_ground`` + Demo 枚举名
    ``SCAVENGING``）：``agent_loop._at_scavenging_site`` 与
    ``structured.observer._SCAVENGING_LOCATION_IDS``。任何一侧退化成单值，这里就该红。
    """

    @pytest.mark.parametrize(
        "location",
        ["dumping_ground", "SCAVENGING", "DUMPING_GROUND", "Dumping_Ground"],
    )
    def test_r55_at_scavenging_site_accepts_both_value_spellings(self, location):
        """GAP: r55 拾荒点判定双值兼容 —— 正式版 id 与 Demo 枚举名都必须认。

        ``dumping_ground`` 是正式版唯一生产的形态（BridgeApi.cs:732 用
        ``map.Current.Id``），只认 ``SCAVENGING`` 就是这次事故的根因。
        大小写不敏感（实现里做了 ``.upper()``），故 PascalCase 也算。
        """
        assert _at_scavenging_site(_scavenge_obs(location)) is True

    def test_r55_at_scavenging_site_requires_is_outside(self):
        """GAP: r55 反向 —— 在店里（is_outside=False）即便地点字符串匹配也不得判定为拾荒点。"""
        assert _at_scavenging_site(_scavenge_obs("dumping_ground", is_outside=False)) is False
        assert _at_scavenging_site(_scavenge_obs("store")) is False

    @pytest.mark.parametrize(
        "observation",
        [
            {},
            {"visible_info": {}},
            {"visible_info": {"night": None}},
            {"visible_info": {"night": []}},
            None,
        ],
    )
    def test_r55_at_scavenging_site_tolerates_malformed_observation(self, observation):
        """GAP: r55 健壮性 —— 门槛函数在畸形/缺失观察下必须返回 False 而不是抛异常。

        守卫在 step_once 主链路上调用，抛异常会让整步失败（比放行更糟）。
        """
        assert _at_scavenging_site(observation) is False

    def test_r55_observer_available_actions_accepts_both_spellings(self):
        """GAP: r55 同 bug 第二处 —— observer 的「开始拾荒」提示也必须双值兼容。

        原先 ``observer.py:137`` 只比 ``"SCAVENGING"``，导致正式版里提示恒不出现，
        模型永远不知道可以 scavenge。
        """
        assert _SCAVENGING_LOCATION_IDS == frozenset({"SCAVENGING", "DUMPING_GROUND"})
        for location in ("dumping_ground", "SCAVENGING"):
            state = {
                "in_game": True,
                "night": {"is_outside": True, "current_location": location},
            }
            actions = _derive_available_actions(state)
            assert "开始拾荒" in actions, (location, actions)

    def test_r55_no_single_value_scavenging_literal_left_in_source(self):
        """GAP: r55 Demo 枚举残留守卫 —— 任何 ``"SCAVENGING"`` 字面量必须与
        ``"DUMPING_GROUND"`` 同时出现（双值兼容），否则就是又一处单值退化。

        这是源码字面量白名单检查（不是「守卫读了某字段即失效」那类会假红的命题）：
        断言的是「代码里不该出现孤立旧值」，与实现方式无关。
        """
        offenders: list[str] = []
        for path in sorted(GAME_AGENT_DIR.rglob("*.py")):
            source = path.read_text(encoding="utf-8-sig")
            tree = ast.parse(source)
            lines = source.splitlines()
            for node in ast.walk(tree):
                # 只看字符串字面量所在的**整行**：同一行里必须也出现新值
                if isinstance(node, ast.Constant) and node.value == "SCAVENGING":
                    line = lines[node.lineno - 1]
                    if "DUMPING_GROUND" not in line:
                        rel = path.relative_to(REPO_ROOT).as_posix()
                        offenders.append(f"{rel}:{node.lineno}: {line.strip()}")
        assert not offenders, (
            "发现未与新值并存（单值、无新值兼容）的旧 Demo 枚举名 'SCAVENGING'；"
            "请改成双值兼容（参照 agent_loop._at_scavenging_site 与 "
            "observer._SCAVENGING_LOCATION_IDS）：\n  " + "\n  ".join(offenders)
        )

    def test_r55_leave_outside_is_rewritten_to_stow_loot_end_to_end(self, tmp_path, monkeypatch):
        """GAP: r55 端到端 —— 守卫真的接在 step_once 主链路上，不是只存在于单测里。

        这是全项目最贵的回归（一夜毁 12 件战利品）。只单测 ``_at_scavenging_site``
        抓不到「函数对了但没人调用」这种断链（正是它修好前的形态：函数只认
        ``SCAVENGING``，且调用条件还叠了 ``effective_source == CHANNEL_BRIDGE``）。
        故这里走**真实 step_once**：``source=CHANNEL_BRIDGE`` + duck-typed 桥客户端
        + monkeypatch ``loop.decide``，断言最终下发的动作被改写成 ``stow_loot``。

        注意不能用 ``obs_override`` 注入观察 —— 那会把 ``effective_source`` 强制成
        ``CHANNEL_VISION``（``agent_loop.py`` 的 ``obs_override`` 分支），守卫条件
        直接失效，测试就会假绿。
        """
        rec, bridge = _run_step_once(
            tmp_path,
            monkeypatch,
            decision={"action": "leave_outside", "params": {}, "reason": "拾荒完回店", "confidence": 0.9},
            state=_scavenge_site_state(),
            history=None,
        )
        assert rec["decision"]["action"] == "stow_loot", (
            f"拾荒点选 leave_outside 必须被改写成 stow_loot，实际 {rec['decision']}"
        )
        assert rec["decision"]["params"] == {}
        assert rec["brain_meta"] == {"forced_by": "scavenge_pickup_guard"}
        # live=False 时是 dry-run，不应真的把动作发出去
        assert bridge.actions == []

    def test_r55_guard_does_not_rewrite_after_stow_loot(self, tmp_path, monkeypatch):
        """GAP: r55 反向防死循环 —— 上一步已是 stow_loot 时守卫必须撒手。

        ``_at_scavenging_site`` 只看「人在不在拾荒点」，不看「东西装了没」。若守卫
        无条件改写，模型每次想走都被按回去 → ``leave_outside`` 永远发不出去，整局
        卡在拾荒点出不来（比丢货更致命）。故 ``not _last_action_was(history,
        "stow_loot")`` 这条是**防死循环的必需项**，必须有测试钉死。
        """
        rec, _ = _run_step_once(
            tmp_path,
            monkeypatch,
            decision={"action": "leave_outside", "params": {}, "reason": "拾荒完回店", "confidence": 0.9},
            state=_scavenge_site_state(),
            history=[{"action": "stow_loot", "params": {}}],
        )
        assert rec["decision"]["action"] == "leave_outside", (
            f"刚 stow_loot 过就必须允许离场，实际 {rec['decision']}"
        )
        assert rec["brain_meta"] != {"forced_by": "scavenge_pickup_guard"}
        # 对照臂：同一份 state、同一条模型决策，只把 history 换成空 —— 守卫必须改写。
        # 没有这一臂，本测试在「守卫整体失效」时也会通过（守卫永远不改写），
        # 变成一条永远绿的假测试。
        rec_control, _ = _run_step_once(
            tmp_path,
            monkeypatch,
            decision={"action": "leave_outside", "params": {}, "reason": "拾荒完回店", "confidence": 0.9},
            state=_scavenge_site_state(),
            history=None,
        )
        assert rec_control["decision"]["action"] == "stow_loot", (
            "对照组失败：守卫在无 stow_loot 历史时必须改写，否则反向断言无意义"
        )


def _scavenge_site_state() -> dict[str, Any]:
    """拾荒点的 ``/state``：人在外面（is_outside=True）、地点是正式版 id。"""
    return {
        "in_game": True,
        "scene": "Store",
        "cash": 300,
        "store_state": "CLOSED",
        "day": 5,
        "shutter_closed": True,
        "client": None,
        "dialog": {"is_end": True, "is_choice": False},
        "table": {},
        "inventory": {"owned": [], "not_owned": []},
        "economy": {"stock_value": 50},
        "night": {
            "is_outside": True,
            "current_location": "dumping_ground",
            "map_open": False,
            "visit_left": 1,
        },
        "queue": {"can_next": False},
    }


class _FakeBridge:
    """最小 duck-typed 桥客户端（observer 的 GET + execute_decision 的 post_action）。"""

    def __init__(self, state: dict[str, Any]) -> None:
        self._state = state
        self.actions: list[tuple[str, dict[str, Any]]] = []

    def ping(self):
        return {"ok": True}

    def get_state(self):
        import copy

        return copy.deepcopy(self._state)

    def get_machines(self):
        return {"ok": True, "machines": []}

    def get_sellable(self):
        return {"ok": True, "items": []}

    def get_ui_buttons(self):
        return {"ok": True, "buttons": []}

    def post_action(self, action, **params):
        self.actions.append((action, params))
        return {"ok": True, "detail": f"{action} ok"}


def _run_step_once(tmp_path, monkeypatch, *, decision, state, history):
    """走真实 ``step_once``（CHANNEL_BRIDGE）跑一步，返回 ``(step_record, fake_bridge)``。"""
    import copy

    from game_agent.config import DEFAULT_CONFIG

    cfg = copy.deepcopy(DEFAULT_CONFIG)
    cfg["paths"] = dict(cfg.get("paths") or {})
    cfg["paths"]["artifacts_dir"] = str(tmp_path / "artifacts")
    cfg["structured"] = dict(cfg.get("structured") or {})
    cfg["structured"]["prefer"] = [C.CHANNEL_BRIDGE]

    bridge = _FakeBridge(state)
    monkeypatch.setattr(loop, "decide", lambda *a, **k: (dict(decision), {"provider": "fake"}))
    rec = loop.step_once(
        cfg,
        live=False,
        mock=True,
        push_overlay=False,
        save=False,
        source=C.CHANNEL_BRIDGE,
        bridge_client=bridge,
        history=history,
    )
    return rec, bridge


# ==========================================================================
# C. 成交判定铁律（docs/26 §1 硬约束③）
# ==========================================================================


class TestDealVerdictRule:
    """成交只认「现金/库存/天数 diff」，``ok=true`` 单独出现**不算**成交。"""

    def test_deal_rule_prompt_requires_cash_delta_not_ok_flag(self):
        """GAP: 成交铁律未钉死 —— 提示词必须以现金变化为唯一判据，且不得教模型看 ok=true。

        ``ok=true`` 只表示「动作被游戏执行了」，不代表成交（点错目标、被拒绝都会 ok）。
        提示词若出现 ``ok=true`` 就等于把错误判据教给模型。
        """
        prompt = build_decision_prompt({"visible_info": {"money": 100}}, bridge_actions=True)
        assert "现金变化" in prompt
        assert "visible_info.money" in prompt
        # ok=false 是可靠信号（真的没成交），必须写进提示词
        assert "ok=false" in prompt
        # 但绝不能出现 ok=true 作为判据
        assert "ok=true" not in prompt

    def test_deal_rule_goal_profit_counts_cash_only(self):
        """GAP: 成交口径 —— 目标利润只认现金净增，库存增值不算「赚到」。

        若把库存增值也算进利润，模型就能靠囤货刷进度，而店铺一分钱没收。
        ``equity`` 才是含库存的口径，两者必须分开。
        """
        goal = BusinessGoal({"goal": {"target_profit": 1000, "window_days": 7}})
        goal.update(_goal_obs(cash=1000, stock=500, day=1))
        # 现金 +100：算利润
        assert goal.update(_goal_obs(cash=1100, stock=500, day=2)).profit == pytest.approx(100.0)
        # 现金回到起点、库存 +700：利润必须回到 0（库存增值不算利润），但 equity 涨
        p = goal.update(_goal_obs(cash=1000, stock=1200, day=3))
        assert p.profit == pytest.approx(0.0)
        assert p.equity > p.profit

    def test_deal_rule_statewatch_completed_is_heuristic_not_verdict(self):
        """GAP: 成交事实来源 —— statewatch 的 ``completed`` 只是日志启发式，不是成交结论。

        ``rebuild_client_sessions`` 的 docstring 明写「大概率成交」；它靠
        ``End Action Executed`` 推断，拿不到现金 diff。任何把 ``completed`` 当成交
        事实的地方都会误判（顾客点开面板又取消也会走到这里）。
        这里钉死：① 有 action_ended → completed；② 有 not_done → not_done 且
        not_done 优先（可靠信号压过启发式）。
        """
        events = [
            _log_event(1, "client_approaching"),
            _log_event(2, "client_intent", client_id="c1", intent="sell"),
            _log_event(3, "action_ended"),
            _log_event(4, "client_left"),
        ]
        sessions = rebuild_client_sessions(events)
        assert [s["outcome"] for s in sessions] == ["completed"]

        # not_done 是可靠信号，必须压过 completed（同一会话先 action_ended 后 not_done）
        events2 = [
            _log_event(1, "client_approaching"),
            _log_event(2, "client_intent", client_id="c1", intent="sell"),
            _log_event(3, "action_ended"),
            _log_event(4, "business_not_done"),
            _log_event(5, "client_left"),
        ]
        assert [s["outcome"] for s in rebuild_client_sessions(events2)] == ["not_done"]

    def test_deal_rule_no_real_condition_leak(self):
        """GAP: 不吐 realCondition —— 未验出真伪时不得把「真伪结论」当成可下发字段。

        docs/26 硬约束③ 要求「未验出真伪不吐 realCondition」。Python 侧不应有
        任何 ``real_condition`` 之类的动作/参数通道；它只应作为作弊关键字存在于
        ``CHEAT_ACTION_PATTERNS`` 里被拒绝。
        """
        assert "real_condition" in C.CHEAT_ACTION_PATTERNS
        assert not [n for n in ACTIONS if "real_condition" in n]
        assert not [n for n in BRIDGE_ACTION_PARAMS if "real_condition" in n]
        # 动作参数表里也不得出现任何「真伪」类参数
        for name, params in BRIDGE_ACTION_PARAMS.items():
            keys = set(params) if isinstance(params, dict) else set()
            assert not (keys & {"real_condition", "truth", "verdict"}), name


def _goal_obs(*, cash: float, stock: float, day: int, store_state: str = "OPEN") -> dict[str, Any]:
    return {
        "visible_info": {
            "money": cash,
            "day": day,
            "time_or_day": f"第{day}天",
            "store_state": store_state,
            "economy": {"stock_value": stock},
        }
    }


def _log_event(seq: int, kind: str, **data: Any) -> LogEvent:
    return LogEvent(
        seq=seq,
        ts=f"09:{seq:02d}:00",
        level="Log",
        kind=kind,
        message=kind,
        data=data,
        raw="",
    )


# ==========================================================================
# D. state_signature 形状（X01/T13 红绿灯）
# ==========================================================================


class TestStateSignatureShape:
    """签名长度与逐位语义 —— 守卫靠 ``sig[N]`` 做决策，形状漂移等于守卫读出垃圾。"""

    def test_signature_length_matches_named_fields(self):
        """GAP: 签名形状无独立红绿灯 —— ``len(state_signature({}))`` 必须等于
        ``len(SIG_FIELDS)``，且降级位固定在末尾。

        contracts.py 的注释里点名要求这条断言；缺了它，签名扩容时两侧会静默错位
        （守卫继续按旧下标读，读到的是别的字段）。
        """
        sig = state_signature({})
        assert len(sig) == len(C.SIG_FIELDS) == len(C.SIG_INDEX)
        assert len(C.SIG_FIELDS) == 21
        assert C.SIG_FIELDS[-1] == "degradation"
        assert C.SIG_DEGRADATION == 20

    @pytest.mark.parametrize(
        "field_name, index",
        [
            ("in_game", 0),
            ("store_state", 2),
            ("shutter_closed", 3),
            ("cash", 4),
            ("client.identifier", 5),
            ("dialog.is_end", 8),
            ("table.negociated_value", 11),
            ("night.is_outside", 12),
            ("night.current_location", 13),
            ("queue.can_next", 15),
            ("day_until_rent", 17),
            ("economy.stock_value", 18),
            ("night.map_open", 19),
        ],
    )
    def test_signature_index_semantics(self, field_name, index):
        """GAP: 下标语义无逐位断言 —— 每个具名常量必须真的指向它声称的那个字段。

        做法：以一份「所有 20 位都填满、且互不相同」的基线 state 为底，只改动该
        字段的值，断言**只有该下标变化**。单纯断言 ``SIG_INDEX[x] == N`` 是同义
        反复，证明不了语义（下标写对、取值读错块，守卫照样拿到垃圾）。
        """
        assert C.SIG_INDEX[field_name] == index
        base_state = _full_sig_state()
        base = state_signature(base_state)
        assert len(base) == 21
        assert base[20] == (), "基线 state 必须是健康的（无降级），否则本测试无意义"

        mutated_state = _with_sig_field_changed(base_state, field_name)
        mutated = state_signature(mutated_state)
        changed = [i for i in range(20) if mutated[i] != base[i]]
        assert changed == [index], (
            f"改动 {field_name} 后变化的下标是 {changed}，期望只有 {index}"
        )

    def test_signature_guard_constants_match_contracts(self):
        """GAP: 守卫常量与契约表同源 —— ``_SIG_IS_OUTSIDE`` / ``_SIG_MAP_OPEN`` 必须由
        ``SIG_INDEX`` 派生，不能各写一份魔法数字（写两份就会漂）。
        """
        assert loop._SIG_IS_OUTSIDE == C.SIG_INDEX["night.is_outside"]
        assert loop._SIG_MAP_OPEN == C.SIG_INDEX["night.map_open"]
        assert state_signature({})[loop._SIG_IS_OUTSIDE] is None
        assert state_signature({})[loop._SIG_MAP_OPEN] is None


def _full_sig_state() -> dict[str, Any]:
    """构造一份 20 位签名全部填满、且各值互不相同的**健康** state（无降级块）。

    注意「健康」不只是「签名那 20 位有值」：``state_signature`` 的第 21 位是
    :func:`game_agent.structured.channels.degradation_report` 的折叠结果，它检查
    ``contracts.STATE_GUARDED_BLOCKS`` 声明的 **22 段业务块**是否都在场。缺任何
    一段（哪怕签名不读它），第 21 位就会非空、基线断言随之失败。因此这里把 22
    段业务块全部填上，只留 ``day`` 缺席（缺席不触发模式 C，``null`` 才触发）。
    """
    state: dict[str, Any] = {
        "in_game": True,
        "scene": "Store",
        "store_state": "OPEN",
        "shutter_closed": False,
        "cash": 550,
        "client": {
            "identifier": "mentor_01",
            "is_selling": True,
            "is_buying": False,
            "sec_button_lit": False,
        },
        "dialog": {"is_end": False, "is_choice": False},
        "table": {
            "negociated_item": {"identifier": "toothpaste"},
            "negociated_value": 15,
            "nego_open": False,
        },
        "queue": {"can_next": True},
        "night": {"is_outside": False, "current_location": "store", "map_open": False},
        "day_until_rent": 3,
        "economy": {"stock_value": 100},
    }
    # 补齐 STATE_GUARDED_BLOCKS 里剩下那些「签名不读、但降级位会检查」的业务块。
    for block in C.STATE_GUARDED_BLOCKS.values():
        state.setdefault(block, {"placeholder": block})
    return state


#: 各签名位上的「探针值」——必须与 ``_full_sig_state`` 里的默认值都不同。
_SIG_PROBE_VALUES: dict[str, Any] = {
    "in_game": False,
    "scene": "Outside",
    "store_state": "CLOSED",
    "shutter_closed": True,
    "cash": 999,
    "client.identifier": "probe_client",
    "client.is_selling": False,
    "client.is_buying": True,
    "dialog.is_end": True,
    "dialog.is_choice": True,
    "table.negociated_item.identifier": "galaxy_blend",
    "table.negociated_value": 77,
    "night.is_outside": True,
    "night.current_location": "dumping_ground",
    "client.sec_button_lit": True,
    "queue.can_next": False,
    "table.nego_open": True,
    "day_until_rent": 1,
    "economy.stock_value": 999,
    "night.map_open": True,
}


def _with_sig_field_changed(state: dict[str, Any], field_name: str) -> dict[str, Any]:
    """返回把 ``field_name`` 换成探针值后的 state 深拷贝。"""
    import copy

    out = copy.deepcopy(state)
    block, _, key = field_name.partition(".")
    if not key:  # 顶层标量
        out[block] = _SIG_PROBE_VALUES[field_name]
        return out
    if "." in key:  # 两级：table.negociated_item.identifier
        mid, _, leaf = key.partition(".")
        out[block][mid][leaf] = _SIG_PROBE_VALUES[field_name]
        return out
    out[block][key] = _SIG_PROBE_VALUES[field_name]
    return out


# ==========================================================================
# E. 禁止作弊接口（docs/26 §1 硬约束③ 的下半场）
# ==========================================================================


class TestNoCheatActionSurface:
    """``/action`` 白名单里不得出现能直接改现金/天数的动作名。"""

    def test_no_cheat_action_name_in_action_table(self):
        """GAP: 作弊动作面未钉死 —— ``ACTIONS`` / 参数表 / 契约名表都不得命中作弊关键字。

        命中任意一个就意味着模型可以一步改钱/改天，整局经济失去意义。
        """
        for table_name, names in (
            ("ACTIONS", ACTIONS),
            ("BRIDGE_ACTION_PARAMS", tuple(BRIDGE_ACTION_PARAMS)),
            ("ACTION_NAMES", tuple(C.ACTION_NAMES)),
        ):
            hits = [n for n in names for pat in C.CHEAT_ACTION_PATTERNS if pat in n]
            assert not hits, f"{table_name} 命中作弊动作名：{hits}"

    def test_cheat_action_rejected_by_decision_validator(self):
        """GAP: 校验器未拦作弊动作 —— 直接下发 set_cash / jump_day 必须抛错，不得静默接受。"""
        extra = BRIDGE_ACTION_PARAMS
        for action in ("set_cash", "add_cash", "set_day", "jump_day"):
            with pytest.raises(ValueError):
                validate_decision({"action": action, "params": {}}, extra_actions=extra)
        # 反向：正常桥接动作必须通过（防「白名单写坏把正常动作也拒了」）
        ok = validate_decision(
            {"action": "sell_item", "params": {"identifier": "toothpaste"}},
            extra_actions=extra,
        )
        assert ok["action"] == "sell_item"

    def test_economy_mutating_actions_are_registered_and_legal(self):
        """GAP: 经济动作登记表可能残缺 —— 改钱/改天的动作必须全部登记在案。

        ``ECONOMY_MUTATING_ACTIONS`` 是「允许改经济状态」的白名单（如 sell_item、
        pay_rent）。它必须 ⊆ ACTIONS（否则是幽灵动作），且自身不得命中作弊关键字
        （否则白名单本身就成了后门）。
        """
        assert C.ECONOMY_MUTATING_ACTIONS <= set(ACTIONS)
        assert C.ECONOMY_MUTATING_ACTIONS, "登记表不能为空，否则等于没有治理"
        hits = [
            n for n in C.ECONOMY_MUTATING_ACTIONS
            for pat in C.CHEAT_ACTION_PATTERNS
            if pat in n
        ]
        assert not hits, f"经济动作登记表自身命中作弊关键字：{hits}"

    def test_bridge_client_exposes_no_raw_economy_endpoint(self):
        """GAP: 桥接层作弊面 —— ``BridgeClient`` 不得暴露「直接改现金/天数」的方法或端点。

        动作白名单干净不代表客户端干净：如果客户端有 ``set_cash()`` 这样的方法
        （或 ``/set_cash`` 端点），绕过动作表就能改钱。

        注意这里**只用改钱/改天那一小撮** ``ECONOMY_MUTATING`` 作弊模式，不用整张
        ``CHEAT_ACTION_PATTERNS``：``debug_`` / ``reveal`` / ``truth`` / ``cheat``
        是为**动作名**筛的宽泛模式，套到方法名上会把 ``get_debug_client``（调试面板
        只读接口，不改钱）误判成作弊面。这不是放宽断言 —— 真正要防的是「绕过动作表
        一步改经济」，只有 set_/add_/jump_ 这类词能表达该能力。
        """
        money_day_patterns = (
            "set_cash",
            "add_cash",
            "add_money",
            "set_money",
            "give_money",
            "set_day",
            "jump_day",
            "skip_day",
            "add_day",
            "set_reputation",
            "set_stock",
            "set_inventory",
        )
        for pat in money_day_patterns:
            assert pat in C.CHEAT_ACTION_PATTERNS, (
                f"{pat} 必须仍在 CHEAT_ACTION_PATTERNS 里被治理，本测试的白名单已过时"
            )
        public = {n for n in dir(BridgeClient) if not n.startswith("_")}
        cheat_methods = [
            n for n in public for pat in money_day_patterns if pat in n.lower()
        ]
        assert not cheat_methods, f"BridgeClient 暴露了作弊方法：{cheat_methods}"
        # 端点全集同样不得含作弊路径（所有对外方法必须走 post_action 的统一白名单）
        source = (GAME_AGENT_DIR / "bridge" / "client.py").read_text(encoding="utf-8-sig")
        for pat in ("set_cash", "add_cash", "set_day", "jump_day"):
            assert pat not in source, f"bridge/client.py 出现作弊端点/字段名：{pat}"


# ==========================================================================
# F. 守卫输入同形性（GAP(e)：被守卫消费的块必须真实存在）
# ==========================================================================


class TestGuardInputShape:
    """守卫读的 visible_info 块必须能由 ``build_observation`` 真实产出。

    守卫的典型失效方式是「读一个永远不存在的键 → 拿到 None → 提前 return None（放行）」
    → 守卫静默失效。这里**用行为断言**（喂真实 observation 看守卫是否真的会拦），
    不做「守卫源码里读了什么」的扫描 —— 后者会随实现演进而假红。
    """

    def test_guarded_blocks_are_produced_by_observer(self):
        """GAP: 守卫输入同形性 —— 守卫消费的 visible_info 块必须真的由 observer 产出。

        白名单两个例外（必须显式列出，否则是假红）：
          * ``rent`` / ``sellable_now`` / ``money``：observer 展开到顶层的合成块，
            不是 ``/state`` 的原始字段，因此不在 ``STATE_FIELDS`` 里；
          * 其余块必须是 ``STATE_FIELDS`` 的成员（``/state`` 真实字段）。
        """
        state = {
            "in_game": True,
            "cash": 100,
            "store_state": "OPEN",
            "client": {"identifier": "c1", "is_selling": True},
            "dialog": {"is_end": False},
            "table": {"negociated_value": 10},
            "inventory": {"owned": [], "not_owned": []},
            "rent": {"day_until_rent": 1, "rent_value": 200, "rent_due": 200},
            "night": {"is_outside": False, "current_location": "store"},
            "queue": {"can_next": True},
            "economy": {"stock_value": 50},
        }
        info = build_observation(state, source=C.CHANNEL_BRIDGE)["visible_info"]
        # 守卫真正消费的块（见 agent_loop 的 _inventory_guard/_dialog_guard/
        # _category_guard/_stalled_guard/_rent_reserve_guard）
        for block in ("client", "dialog", "table", "inventory", "night", "queue", "economy"):
            assert block in info, f"守卫消费的块 {block} 未被 observer 产出"
            assert block in C.STATE_FIELDS, f"{block} 不在 /state 契约表里"
        for synthesized in ("rent", "sellable_now", "money"):
            assert synthesized in info, f"observer 合成块 {synthesized} 未被产出"
            assert synthesized not in C.STATE_FIELDS, (
                f"{synthesized} 已进入 STATE_FIELDS，请同步更新本测试的白名单"
            )
        assert OBSERVER_SYNTHESIZED_TOPLEVEL == {"rent", "sellable_now", "money"}

    def test_guard_actually_blocks_on_real_observation(self):
        """GAP: 守卫在真实 observation 下真的会拦 —— 不是靠「读到 None 就放行」蒙对。

        用 observer 真实产出的 observation 喂守卫，断言它给出拦截文案。
        如果守卫依赖的字段被 observer 弄丢，这里会从「拦」变成「放行」→ 红。

        关键陷阱：observer 的 ``rent`` 块**只从 ``/state`` 顶层**读
        ``day_until_rent`` / ``rent_value`` / ``rent_due``
        （``game_agent/structured/observer.py:1098-1102``），不读 ``state["rent"]``
        子字典。写成子字典会让 rent 三键全变 None → 守卫提前 return None（放行），
        测试就会以「守卫本来就不该拦」的错误理由通过。
        """
        state = {
            "in_game": True,
            "cash": 442,
            "store_state": "OPEN",
            "client": {"identifier": "c1", "is_selling": True},
            "table": {"negociated_value": 150},
            "inventory": {"owned": [], "not_owned": []},
            # rent 三键必须在 /state 顶层，observer 才认
            "day_until_rent": 1,
            "rent_value": 242,
            "rent_due": 693,
        }
        obs = build_observation(state, source=C.CHANNEL_BRIDGE)
        vi = obs["visible_info"]
        assert vi["rent"]["rent_due"] == 693, "observer 没读到 rent_due，本测试前提不成立"
        assert vi["money"] == 442, "observer 没读到现金，本测试前提不成立"
        sig = state_signature(state)
        assert sig[C.SIG_INDEX["cash"]] == 442
        decision = {"action": "click_button", "params": {"name": "wholesaleBuyButton"}}
        detail = _rent_reserve_guard(decision, obs, sig)
        assert detail is not None, "现金 442 < 应缴 693 时守卫必须拦，不许因字段缺失而放行"
        assert "693" in detail
