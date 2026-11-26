"""v2.8 决策层修复的回归测试。

这一批修的都是「看得见状态却不行动」类故障——不是接口报错，而是模型选错动作：

1. 死局顾客：他要买 X，你一件都没有，模型却选 `wait` 或反复猜按钮文字；
2. 夜间拾荒：`visit_left > 0` 连续多晚不出门（知识卡命中 21 次也没动）；
3. 委托可交付：`requests.can_fill` 摆在那儿没人领。

修法两层：
- 观察层 `visible_info.next_step_hints` —— 用代码算出「本轮该干什么」，随状态下发；
- 执行层 `_stalled_guard` —— 拦下必然空转的 wait / 猜文字 click_button。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from game_agent.agent_loop import _enrich_dialog_choice, _stalled_guard  # noqa: E402
from game_agent.brain.prompt import build_decision_prompt  # noqa: E402
from game_agent.structured.observer import (  # noqa: E402
    _attempts_tonight,
    _next_step_hints,
    build_observation,
)


def _obs(**vi) -> dict:
    return {"visible_info": vi}


class TestNextStepHints:
    """观察层提示：每条都必须点出具体动作名。"""

    def test_dead_end_client_says_dismiss(self):
        hints = _next_step_hints({
            "client": {"identifier": "foreman", "is_buying": True, "buying_tags": ["MATERIAL"]},
            "sellable_now": [],
        })
        assert hints, "死局顾客必须给出提示"
        assert "dismiss_client" in hints[0]
        assert "wait" in hints[0], "要明确禁止 wait"

    def test_sellable_client_says_sell(self):
        hints = _next_step_hints({
            "client": {"identifier": "miner", "is_buying": True, "buying_tags": ["FOOD"]},
            "sellable_now": [{"identifier": "beer"}, {"identifier": "nudka"}],
        })
        assert any("sell_item" in h for h in hints)

    def test_night_visit_left_says_scavenge(self):
        hints = _next_step_hints({
            "night": {"visit_left": 2, "is_outside": False, "current_location": "STORE"},
        })
        assert any("scavenge" in h for h in hints), "有外出次数就必须提示拾荒"
        joined = " ".join(hints)
        assert "open_map" in joined and "leave_outside" in joined, "要给出完整流程"

    def test_outside_scavenging_says_stow_and_leave(self):
        hints = _next_step_hints({
            "night": {"visit_left": 1, "is_outside": True, "current_location": "SCAVENGING"},
        })
        joined = " ".join(hints)
        assert "stow_loot" in joined
        assert "leave_outside" in joined

    def test_no_visit_left_no_scavenge_hint(self):
        hints = _next_step_hints({
            "night": {"visit_left": 0, "is_outside": False, "current_location": "STORE"},
        })
        assert not any("scavenge" in h for h in hints), "次数用完不该再催出门"

    def test_scavenge_attempts_stop_rule(self):
        """拾 3 次后必须催收工——scavenge 没有服务端硬上限，否则会一直烧步数。"""
        early = _next_step_hints({
            "night": {"is_outside": True, "current_location": "SCAVENGING",
                      "scavenging_attempts": 0},
        })
        assert any("scavenge" in h and "leave_outside" in h for h in early)

        late = _next_step_hints({
            "night": {"is_outside": True, "current_location": "SCAVENGING",
                      "scavenging_attempts": 3},
        })
        joined = " ".join(late)
        assert "收工" in joined and "leave_outside" in joined
        assert "再拾一次" not in joined, "已经拾够，不该再催 scavenge"


    def test_fillable_request_says_fill(self):
        hints = _next_step_hints({
            "requests": [{"id": "r1", "can_fill": True}, {"id": "r2", "can_fill": False}],
        })
        assert any("fill_request" in h for h in hints)

    def test_plain_state_has_no_hints(self):
        assert _next_step_hints({"night": {"visit_left": 0, "is_outside": False}}) == []

    def test_hints_capped(self):
        hints = _next_step_hints({
            "client": {"identifier": "x", "is_buying": True, "is_selling": True},
            "sellable_now": [{"identifier": "a"}],
            "queue": {"can_next": True},
            "dialog": {"is_end": False, "choices": ["接受"]},
            "night": {"visit_left": 1, "is_outside": True, "current_location": "SCAVENGING"},
            "requests": [{"id": "r", "can_fill": True}],
        })
        # 上限由 6 提到 8：正式版新增的枪械订单/按揭/事件/鉴定提示会占名额，
        # 6 条时夜间「该出门拾荒」的提示会被挤掉（最贵的一类丢失）。
        assert len(hints) <= 8


class TestObservationCarriesHints:
    def test_visible_info_has_key(self):
        obs = build_observation({
            "in_game": True, "cash": 500, "day": 3, "store_state": "CLOSED",
            "night": {"visit_left": 1, "is_outside": False, "current_location": "STORE"},
            "inventory": {"owned": [], "not_owned": []},
            "queue": {"can_next": False, "has_client": False, "count": 0},
        }, source="bridge")
        assert "next_step_hints" in obs["visible_info"]
        assert any("scavenge" in h for h in obs["visible_info"]["next_step_hints"])

    def test_no_scavenge_hint_while_open(self):
        """营业中不催出门——跑出去会丢客人，等打烊再说。"""
        obs = build_observation({
            "in_game": True, "cash": 500, "day": 3, "store_state": "OPEN",
            "night": {"visit_left": 1, "is_outside": False, "current_location": "STORE"},
            "inventory": {"owned": [], "not_owned": []},
            "queue": {"can_next": False, "has_client": False, "count": 0},
        }, source="bridge")
        assert obs["visible_info"]["next_step_hints"] == []


class TestRefusedSubtraction:
    """顾客明确拒收的物品不能留在 sellable_now 里（否则提示会让人去卖废货）。"""

    def test_refused_removed_from_sellable(self):
        obs = build_observation({
            "in_game": True, "cash": 563, "day": 5, "store_state": "OPEN",
            "queue": {"can_next": False, "has_client": True, "count": 0},
            "client": {
                "identifier": "upperLevelVisitorMinor", "is_buying": True,
                "buying_tags": ["NARCOTIC"], "refused": ["packet_red_cigarette"],
            },
            "inventory": {"owned": [{
                "identifier": "packet_red_cigarette", "value": 18, "count": 1,
                "item_types": ["NARCOTIC"],
            }], "not_owned": []},
            "night": {"visit_left": 0, "is_outside": False},
            "table": None, "dialog": {"is_end": True},
            "services": [], "phone": {}, "economy": {},
            "reputations": [], "requests": [], "unlocks": {},
        }, source="bridge")
        vi = obs["visible_info"]
        assert vi["sellable_now"] == [], "拒收物品必须从可卖清单剔除"
        assert any("dismiss_client" in h for h in vi["next_step_hints"]), \
            "剔空后应提示换客，而不是硬卖"


class TestStalledGuard:
    def test_blocks_wait_in_dead_end(self):
        reason = _stalled_guard(
            {"action": "wait", "params": {"seconds": 1.5}},
            _obs(client={"identifier": "foreman", "is_buying": True},
                 dialog={"is_end": True}, sellable_now=[]),
        )
        assert reason and "dismiss_client" in reason

    def test_allows_wait_when_something_to_sell(self):
        assert _stalled_guard(
            {"action": "wait", "params": {}},
            _obs(client={"identifier": "m", "is_buying": True},
                 dialog={"is_end": True}, sellable_now=[{"identifier": "beer"}]),
        ) is None

    def test_allows_wait_during_dialog(self):
        assert _stalled_guard(
            {"action": "wait", "params": {}},
            _obs(client={"identifier": "m", "is_buying": True},
                 dialog={"is_end": False}, sellable_now=[]),
        ) is None

    def test_blocks_guessed_dismiss_button(self):
        reason = _stalled_guard(
            {"action": "click_button", "params": {"text": "打发走当前顾客"}},
            _obs(),
        )
        assert reason and "dismiss_client" in reason

    def test_blocks_guessed_call_button(self):
        reason = _stalled_guard(
            {"action": "click_button", "params": {"text": "呼叫下一位顾客"}},
            _obs(),
        )
        assert reason and "call_next" in reason

    def test_allows_legit_click_button(self):
        assert _stalled_guard(
            {"action": "click_button", "params": {"name": "CloseButton", "path": "_AdvCalendar"}},
            _obs(),
        ) is None

    def test_ignores_unrelated_actions(self):
        assert _stalled_guard({"action": "sell_item", "params": {}}, _obs()) is None


class TestPromptRendering:
    def _prompt(self, **state_over):
        st = {
            "in_game": True, "cash": 559, "day": 5, "store_state": "OPEN",
            "queue": {"can_next": False, "has_client": True, "count": 0},
            "client": {"identifier": "foreman", "is_buying": True, "buying_tags": ["MATERIAL"]},
            "table": None, "dialog": {"is_end": True},
            "inventory": {"owned": [], "not_owned": []}, "services": [],
            "night": {"visit_left": 1, "is_outside": False, "current_location": "STORE"},
            "phone": {}, "economy": {}, "reputations": [], "requests": [], "unlocks": {},
        }
        st.update(state_over)
        return build_decision_prompt(
            build_observation(st, source="bridge"), bridge_actions=True, no_mouse=True
        )

    def test_hints_block_present(self):
        p = self._prompt()
        assert "【本轮优先动作" in p
        assert "dismiss_client" in p

    def test_rules_numbered_without_duplicates(self):
        import re

        p = self._prompt()
        seg = p[p.find("【决策要求】"):]
        nums = [int(m.group(1)) for m in re.finditer(r"^(\d)\.", seg, re.M)]
        assert nums == sorted(nums), f"规则编号必须递增：{nums}"
        assert len(nums) == len(set(nums)), f"规则编号不能重复：{nums}"

class TestPerTripScavengeCount:
    """scavenging_attempts 是跨天累计的，必须按「本次外出」计数。

    否则第二天一开局累计值就已经 ≥3，提示会让 AI 一出门就收工，
    把拾荒收益整晚丢掉（这正是 v2.8 收工规则引入的回归）。
    """

    def test_new_day_starts_at_zero(self):
        t = {}
        # Day 6 拾到 3 次，累计值停在 3
        # 当天首次观察时基线未知，按 0 计（宁可多拾，也不要在门外空手而归）
        assert _attempts_tonight(
            {"day": 6, "night": {"scavenging_attempts": 3, "is_outside": False}}, t
        ) == 0
        # Day 7 开局：累计仍是 3，本次同样是 0
        assert _attempts_tonight(
            {"day": 7, "night": {"scavenging_attempts": 3, "is_outside": False}}, t
        ) == 0

    def test_counts_within_a_trip(self):
        t = {}
        _attempts_tonight({"day": 7, "night": {"scavenging_attempts": 3, "is_outside": False}}, t)
        assert _attempts_tonight(
            {"day": 7, "night": {"scavenging_attempts": 3, "is_outside": True}}, t
        ) == 0
        assert _attempts_tonight(
            {"day": 7, "night": {"scavenging_attempts": 4, "is_outside": True}}, t
        ) == 1
        assert _attempts_tonight(
            {"day": 7, "night": {"scavenging_attempts": 6, "is_outside": True}}, t
        ) == 3

    def test_stop_rule_uses_trip_count(self):
        """同一个累计值 6：本次 1 次 → 继续拾；本次 3 次 → 催收工。"""
        early = _next_step_hints(
            {"night": {"is_outside": True, "current_location": "SCAVENGING"}},
            attempts_tonight=1,
        )
        assert any("再拾一次" in h for h in early)

        late = _next_step_hints(
            {"night": {"is_outside": True, "current_location": "SCAVENGING"}},
            attempts_tonight=3,
        )
        assert any("收工" in h for h in late)

    def test_second_night_can_scavenge_again(self):
        """跨天后即使累计值很高，也必须重新催出门拾荒。"""
        obs = build_observation({
            "in_game": True, "cash": 600, "day": 9, "store_state": "CLOSED",
            "night": {"visit_left": 1, "is_outside": False, "current_location": "STORE",
                      "scavenging_attempts": 12},
            "inventory": {"owned": [], "not_owned": []},
            "queue": {"can_next": False, "has_client": False, "count": 0},
        }, source="bridge")
        assert any("scavenge" in h for h in obs["visible_info"]["next_step_hints"])


class TestDialogChoiceEnrichment:
    """模型给序号时，按观察里的 choices 换成文字再下发（防序号错位点错选项）。"""

    OBS = {"visible_info": {"dialog": {"choices": [
        {"index": 0, "text": "[支付租金（200）]"},
        {"index": 1, "text": "[拒付租金]"},
    ]}}}

    def test_index_replaced_by_text(self):
        d = {"action": "dialog_choose", "params": {"index": 1}}
        _enrich_dialog_choice(d, self.OBS)
        assert d["params"]["text"] == "[拒付租金]"
        assert "index" not in d["params"]

    def test_existing_text_untouched(self):
        d = {"action": "dialog_choose", "params": {"text": "拒付租金"}}
        _enrich_dialog_choice(d, self.OBS)
        assert d["params"] == {"text": "拒付租金"}

    def test_out_of_range_index_kept(self):
        d = {"action": "dialog_choose", "params": {"index": 9}}
        _enrich_dialog_choice(d, self.OBS)
        assert d["params"] == {"index": 9}, "查不到就别改，交给桥接报错"

    def test_no_choices_no_change(self):
        d = {"action": "dialog_choose", "params": {"index": 0}}
        _enrich_dialog_choice(d, {"visible_info": {"dialog": {"is_end": True}}})
        assert d["params"] == {"index": 0}

    def test_other_actions_untouched(self):
        d = {"action": "sell_item", "params": {"index": 0}}
        _enrich_dialog_choice(d, self.OBS)
        assert d["params"] == {"index": 0}


class TestFullReleaseHints:
    """正式版新增机制的确定性提示（v3.4）。

    这些提示的价值在于「不用模型自己推断」：按揭够不够、地点关没关、订单能不能交，
    都由代码算结论。所以用测试把结论钉住。
    """

    def test_scavenge_hint_works_with_full_release_location_id(self):
        """回归：正式版地点是 id 字符串，旧的 "SCAVENGING" 枚举名不会出现。

        只比老值会让「已在外地、该继续拾荒」的提示彻底失效——这条测试防的就是它。
        """
        hints = _next_step_hints({
            "night": {"visit_left": 1, "is_outside": True, "current_location": "dumping_ground"},
        })
        assert any("scavenge" in h for h in hints), hints

    def test_mortgage_hint_when_cash_below_weekly_payment(self):
        hints = _next_step_hints({
            "money": 100,
            "mortgage": {"weeks_left": 3, "weekly_payment": 250, "total_weeks": 14},
        })
        assert any("按揭" in h for h in hints), hints

    def test_no_mortgage_hint_when_cash_enough(self):
        hints = _next_step_hints({
            "money": 900,
            "mortgage": {"weeks_left": 3, "weekly_payment": 250, "total_weeks": 14},
        })
        assert not any("按揭" in h for h in hints), hints

    def test_closed_location_hint_at_night(self):
        hints = _next_step_hints({
            "store_state": "CLOSED",
            "night": {"visit_left": 1, "is_outside": False, "current_location": "store"},
            "afterhour_events": [
                {"id": "blackout", "location": "dumping_ground", "closes_location": True, "active": True},
            ],
        })
        assert any("dumping_ground" in h and "别选" in h for h in hints), hints

    def test_appraisal_promotion_hint(self):
        hints = _next_step_hints({
            "appraisal": {"rank": 0, "rank_name": "初级鉴定员", "promotion_pending": True},
        })
        assert any("升职" in h for h in hints), hints


class TestInventoryGuardMessages:
    """库存守卫的拒绝理由必须可执行（v3.6）。

    实机发现：正式版顾客的货要先开议价面板才出现，旧文案说"顾客物品里没有 X"，
    会让模型以为记错货，实际只是顺序不对。
    """

    def _obs(self, *, selling=True, nego_open=False, not_owned=None):
        return {"visible_info": {
            "client": {"is_selling": selling, "name": "化学家"},
            "table": {"nego_open": nego_open},
            "inventory": {"owned": [], "not_owned": not_owned or []},
        }}

    def test_buy_item_before_opening_negotiation_tells_to_open(self):
        from game_agent.agent_loop import _inventory_guard

        msg = _inventory_guard({"action": "buy_item", "params": {"identifier": "x"}}, self._obs())
        assert msg and "open_negociation" in msg

    def test_buy_item_when_panel_open_lists_available(self):
        from game_agent.agent_loop import _inventory_guard

        obs = self._obs(nego_open=True, not_owned=[{"identifier": "salve", "name": "软膏"}])
        msg = _inventory_guard({"action": "buy_item", "params": {"identifier": "x"}}, obs)
        assert msg and "salve" not in msg and "软膏" in msg

    def test_sell_item_message_unchanged(self):
        from game_agent.agent_loop import _inventory_guard

        msg = _inventory_guard({"action": "sell_item", "params": {"identifier": "x"}}, self._obs())
        assert msg and "inventory.owned" in msg


class TestSellingBeforeNegotiation:
    """正式版：顾客在卖货但面板没开时，提示必须先让他开面板（v3.6 实机）。"""

    def test_hint_asks_to_open_negotiation_first(self):
        hints = _next_step_hints({
            "client": {"identifier": "chemist", "name": "化学家", "is_selling": True, "is_buying": False},
            "table": {"nego_open": False},
        })
        assert any("open_negociation" in h for h in hints), hints

    def test_hint_goes_straight_to_buy_when_open(self):
        hints = _next_step_hints({
            "client": {"identifier": "chemist", "name": "化学家", "is_selling": True, "is_buying": False},
            "table": {"nego_open": True},
        })
        assert any("buy_item" in h for h in hints), hints


class TestDebtAndInsuranceHints:
    """v3.18：贷款到期 / 保险缺口这两类「持续恶化但没有明确下一步」的指标。

    设计取舍：只在「能行动时」才提示（还得起才催贷款，夜间才催保险），
    否则每步重复同一句话会变成噪声，反而稀释真正紧急的提示。
    """

    def test_loan_due_and_affordable(self):
        from game_agent.structured.observer import _next_step_hints

        hs = _next_step_hints({
            "money": 800, "store_state": "OPEN",
            "economy": {"loan_days": 1, "loan_value": 500},
        })
        assert any("pay_loan" in h for h in hs)

    def test_loan_due_but_unaffordable_is_silent(self):
        from game_agent.structured.observer import _next_step_hints

        hs = _next_step_hints({
            "money": 100, "store_state": "OPEN",
            "economy": {"loan_days": 1, "loan_value": 500},
        })
        assert not any("pay_loan" in h for h in hs)

    def test_insurance_gap_at_night(self):
        from game_agent.structured.observer import _next_step_hints

        hs = _next_step_hints({
            "money": 500, "store_state": "CLOSED",
            "economy": {"insurance_days_without": 4},
        })
        assert any("buy_insurance" in h for h in hs)

    def test_insurance_gap_not_nagged_during_day(self):
        from game_agent.structured.observer import _next_step_hints

        hs = _next_step_hints({
            "money": 500, "store_state": "OPEN",
            "economy": {"insurance_days_without": 4},
        })
        assert not any("buy_insurance" in h for h in hs)


class TestGarbageHint:
    """v3.17：脏乱降吸引力必须由代码算成结论（用户实机质疑过）。"""

    def test_garbage_high_emits_action_chain(self):
        from game_agent.structured.observer import _next_step_hints

        hs = _next_step_hints({"money": 500, "rent": {"garbage_level": 10}})
        hit = [h for h in hs if "脏乱" in h]
        assert hit, "垃圾等级 10 必须产出提示"
        assert "JANITORIAL_SERVICE" in hit[0] and "night_services" in hit[0]

    def test_garbage_low_is_silent(self):
        from game_agent.structured.observer import _next_step_hints

        hs = _next_step_hints({"money": 500, "rent": {"garbage_level": 2}})
        assert not [h for h in hs if "脏乱" in h]

    def test_garbage_penalty_formula(self):
        """G=4→20%、G=10→50%（按卡内实测数据，非 (G-2)×5）。"""
        from game_agent.structured.observer import _next_step_hints

        def pct(g):
            hs = _next_step_hints({"money": 500, "rent": {"garbage_level": g}})
            hit = [h for h in hs if "脏乱" in h][0]
            import re
            m = re.search(r"已扣约 (\d+)%", hit)
            return int(m.group(1)) if m else None

        assert pct(4) == 20
        assert pct(10) == 50
