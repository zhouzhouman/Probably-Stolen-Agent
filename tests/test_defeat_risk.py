"""T06-P0 终局风险守卫（`_defeat_risk_guard`）测试。

机制依据（反编译核实，非推测）：
- 房东对话的「拒付租金」分支 `StoreClientList.cs:2650-2686`：当
  `!NetworkUpgrade.IsLandlordGraceUnlocked()`（`NetworkUpgrade.cs:873-880` 只在
  `state == 2` 返 true）时走 `dialog_landlord_no_grace_1/2` →
  `PlayerStore.Instance.ExecuteGameOver()`（`:2661`）——**整局结束**。
- 「付租」选项带 `CanPayRent()` 前置（`StoreClientList.cs:2637`），现金不足时该选项
  根本不出现；「拒付」无前置（`:2650`）。
- `CanPayRent()`（`PlayerStore.cs:1470-1477`）= `playerCash >= rentValue + temporaryRent`，
  所以判定金额是 **rent_value + rent_due 之和**，不是二者取大。
- 宽限状态当前不可观测（`BridgeApi.cs:420-435` 的 /state 无任何 grace 字段），
  故守卫按最坏情况（无宽限）判定。

载荷形状取自真实 1.0 /state（`artifacts/_lead_probe/v5_state_live.json`，
cash=150 / rent_value=250 / rent_due=0 / day_until_rent=6 / can_pay_rent=False），
只改被测字段。
"""
from __future__ import annotations

from game_agent.agent_loop import (
    _DEFEAT_RISK_ALLOWED_ACTIONS,
    _SIG_CASH,
    _defeat_risk_guard,
)


def _sig(cash):
    """按 contracts.SIG_FIELDS 的长度造签名：索引 _SIG_CASH 放现金，其余留 None。"""
    sig = [None] * 21
    sig[0] = True          # in_game
    sig[1] = "InventoryScene"  # scene
    sig[2] = "OPEN"        # store_state
    sig[_SIG_CASH] = cash
    return tuple(sig)


def _obs(*, day_until=0, rent_value=250, rent_due=0, can_pay_rent=False, **over):
    rent = {
        "day_until_rent": day_until,
        "rent_value": rent_value,
        "rent_due": rent_due,
        "can_pay_rent": can_pay_rent,
    }
    if over.pop("drop_can_pay_rent", False):
        rent.pop("can_pay_rent", None)
    rent.update(over)
    return {
        "source": "bridge",
        "visible_info": {
            "money": 150,
            "rent": rent,
            "queue": {"can_next": True, "has_client": True, "count": 3},
        },
    }


class TestDefeatRiskGuard:
    """交租日 + 付不起 + 无宽限 ⇒ 只许救命动作。"""

    # —— 必须拦 ——
    def test_blocks_purchase_on_rent_day_when_cannot_pay(self):
        """(a) 交租日 + can_pay_rent=False + 现金 150 < 应缴 250 → 拦 buy_item，且含差额。"""
        reason = _defeat_risk_guard(
            {"action": "buy_item", "params": {"identifier": "x"}}, _obs(), _sig(150),
        )
        assert reason is not None
        assert "终局风险" in reason
        assert "还差 100" in reason      # 250 - 150
        assert "150" in reason and "250" in reason
        # 必须点明这三件事：不可恢复、具体差额、唯一出路
        assert "游戏结束" in reason
        assert "sell_item" in reason and "pay_rent" in reason

    def test_blocks_end_day_on_rent_day_when_cannot_pay(self):
        """交租日付不起时打烊同样是死路（天数照推、欠款照涨）。"""
        assert _defeat_risk_guard(
            {"action": "end_day", "params": {}}, _obs(), _sig(150),
        ) is not None

    def test_blocks_when_overdue(self):
        """已拖欠（day_until_rent=-2）+ 付不起 → 仍拦，且文案说明已拖欠 2 天。"""
        reason = _defeat_risk_guard(
            {"action": "buy_item", "params": {}}, _obs(day_until=-2), _sig(150),
        )
        assert reason is not None and "已拖欠租金 2 天" in reason

    def test_uses_sum_of_rent_value_and_due(self):
        """金额口径必须是 rent_value + rent_due（CanPayRent 原式），不是取大。"""
        reason = _defeat_risk_guard(
            {"action": "buy_item", "params": {}},
            _obs(rent_due=300), _sig(150),
        )
        assert reason is not None
        assert "还差 400" in reason     # (250 + 300) - 150
        assert "550" in reason

    def test_blocks_without_signature_but_says_amount_unknown(self):
        """现金读不到（无签名）仍按 can_pay_rent 拦，且文案明说数字读不到（不编数字）。"""
        reason = _defeat_risk_guard({"action": "buy_item", "params": {}}, _obs(), None)
        assert reason is not None
        assert "读不到具体数字" in reason
        assert "还差" not in reason

    # —— 必须放行 ——
    def test_allows_rescue_actions_on_rent_day(self):
        """(b) 同条件下五条救命动作全部放行（拦掉 pay_rent/dialog_choose 会让 AI 无路可走）。"""
        for action in ("sell_item", "sell_table", "dialog_choose", "pay_rent", "wait"):
            assert action in _DEFEAT_RISK_ALLOWED_ACTIONS
            assert _defeat_risk_guard(
                {"action": action, "params": {}}, _obs(), _sig(150),
            ) is None, f"{action} 必须放行"

    def test_passes_when_rent_day_is_far(self):
        """(c) day_until_rent=3（还早）即使付不起也不拦——那是 _rent_reserve_guard 的事。"""
        assert _defeat_risk_guard(
            {"action": "buy_item", "params": {}}, _obs(day_until=3), _sig(150),
        ) is None

    def test_passes_when_can_pay_rent_true(self):
        """(d) can_pay_rent=True → 不拦。"""
        assert _defeat_risk_guard(
            {"action": "buy_item", "params": {}},
            _obs(can_pay_rent=True, rent_value=100, rent_due=0), _sig(400),
        ) is None

    def test_passes_when_can_pay_rent_missing(self):
        """can_pay_rent 缺失（降级/老 mod）→ 无法判定，放行（沿用「缺失即放行」）。"""
        assert _defeat_risk_guard(
            {"action": "buy_item", "params": {}},
            _obs(drop_can_pay_rent=True), _sig(150),
        ) is None

    def test_passes_when_cash_recomputation_contradicts_flag(self):
        """can_pay_rent=false 但现金自算够（双确认矛盾）→ 以可复算证据为准，放行。"""
        assert _defeat_risk_guard(
            {"action": "buy_item", "params": {}}, _obs(), _sig(999),
        ) is None

    def test_passes_on_non_dict_observation(self):
        """观察形状异常时不误拦。"""
        assert _defeat_risk_guard({"action": "buy_item", "params": {}}, None, _sig(0)) is None
        assert _defeat_risk_guard({"action": "buy_item", "params": {}}, {}, _sig(0)) is None

    def test_real_live_snapshot_shape_not_blocked(self):
        """真实 1.0 快照原样（can_pay_rent=False 但 day_until_rent=6）绝不能拦。

        这条是防「只看 can_pay_rent 就拦」的回归锁：开局现金 150 < 房租 250，
        can_pay_rent 天然为 false，若忽略交租日就会整局卡死。
        """
        assert _defeat_risk_guard(
            {"action": "buy_item", "params": {}},
            _obs(day_until=6, can_pay_rent=False), _sig(150),
        ) is None
