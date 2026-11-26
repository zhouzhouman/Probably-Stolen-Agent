"""X03 契约缺口补测（R7）：只放**已有 ``tests/test_contracts.py`` 没覆盖**的缺口。

为什么单独开文件：``tests/test_contracts.py`` 的 66 条属于 X01/R1 的交付物，
多人并行期不应改动别人的断言基线与行号；本文件只做增量，条目互不重复。

覆盖范围（对应 Lead 排定的 6 项，每条 docstring 首行写 ``GAP:``）：

* R 组 —— ``state_signature`` 形状：``SIG_INDEX`` 是否为 0..n-1 的双射、
  同一 state 两次调用是否幂等、``night.map_open`` 翻转是否只动第 19 位、
  健康载荷下第 21 位（降级位）是否为空 tuple，以及**整块静默消失**（无
  ``_error``、纯缺键，对应 ``mod/src`` 里 46 处空 ``catch { }``）时降级位是否
  点出块名 —— 最后这条要求夹具是**完整 bridge 载荷**（``_BRIDGE_UNCONDITIONAL_KEYS``
  那 9 个键齐全），否则 ``channels._is_full_bridge_payload()`` 为假、
  「缺块」与「健康」签名相同，测试会假绿；
* D 组 —— 禁止作弊面：``BridgeClient`` 公开方法的**前缀 + 参数**联合判定；
* M 组 —— ``is_model_callable`` 的边界值、``derive_gated_actions()`` 的返回类型、
  可调集合是动作表的真子集（59 − 3 = 56）；
* E 组 —— 租金守卫的现金来源：必须取 ``state_signature[SIG_INDEX["cash"]]``，
  而不是 ``visible_info["cash"]``（双向断言，防「无脑全拦」的假保护）；
* N 组 —— ``brain.NIGHT_PHASE_ACTIONS`` 与 ``contracts.derive_night_phase()`` 同源；
* B 组 —— 成交判定铁律：``ok=true`` 单独出现不得产生任何成交结论/成交后副作用。

纪律（与 ``tests/test_gap_regressions.py`` 一致）：只做**字面量白名单**或
**行为**断言，不做「守卫源码里读了什么」的扫描式断言 —— 后者会随实现演进而假红。
"""

from __future__ import annotations

import copy
import inspect
from typing import Any

import pytest

import game_agent.brain.brain as brain
import game_agent.contracts as C
import game_agent.agent_loop as loop
from game_agent.bridge.client import BridgeClient
from game_agent.goal import BusinessGoal
from game_agent.structured.channels import state_signature
from game_agent.structured.observer import build_observation


# ==========================================================================
# R. state_signature 形状（X01/T13 红绿灯的增量部分）
# ==========================================================================


def _healthy_state() -> dict[str, Any]:
    """构造一份签名 20 位全填满、且 ``STATE_GUARDED_BLOCKS`` 全在场的**完整 bridge 载荷**。

    第 21 位是降级位，它的判据有两条前提，缺一条这个夹具就测不出东西：

    1. ``STATE_GUARDED_BLOCKS`` 的 22 段业务块全部在场（缺一块 → 模式 B 降级）；
    2. 载荷必须能通过 ``channels._is_full_bridge_payload()``，即
       ``_BRIDGE_UNCONDITIONAL_KEYS`` 那 9 个「C# 无条件发射」的键都在场。

    第 2 条是**必须的**：没有它，「块静默缺失」不会被当成降级证据
    （``channels.py:285-287`` 的 ``infer_missing`` 为 False），于是「缺一块」
    与「完全健康」签名相同 —— 那正是本夹具要防的假绿。所以这里把 ``day`` /
    ``rent_value`` / ``is_power_on`` / ``garbage_level`` 都给上真实值。
    """
    state: dict[str, Any] = {
        "in_game": True,
        "scene": "Store",
        "store_state": "OPEN",
        "cash": 550,
        "day": 3,
        "day_until_rent": 3,
        "rent_value": 242,
        "is_power_on": True,
        "garbage_level": 0,
        "shutter_closed": False,
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
        "economy": {"stock_value": 100},
    }
    for block in C.STATE_GUARDED_BLOCKS.values():
        state.setdefault(block, {"placeholder": block})
    return state


def test_sig_index_is_bijection_over_zero_to_n_minus_one():
    """GAP: ``SIG_INDEX`` 可能漏项/错位/重号 —— 必须是 0..n-1 的严格双射。

    ``test_contracts.py`` 只断言了三处**长度**同步；长度相等但取值有重号或空洞
    （例如两位都写 7、漏掉 19）时长度照样相等，而守卫按错的下标读会拿到别的字段。
    """
    assert list(C.SIG_INDEX) == list(C.SIG_FIELDS), "键顺序必须与 SIG_FIELDS 一致"
    values = list(C.SIG_INDEX.values())
    assert sorted(values) == list(range(len(C.SIG_FIELDS))), (
        f"SIG_INDEX 取值必须是 0..{len(C.SIG_FIELDS) - 1} 的一个排列，实际 {sorted(values)}"
    )
    assert len(set(values)) == len(values), "SIG_INDEX 存在重号"
    assert len(C.SIG_INDEX) == len(C.SIG_FIELDS) == len(state_signature({}))


def test_sig_is_idempotent_and_sensitive_to_map_open():
    """GAP: 签名需幂等且对状态敏感 —— 否则事件驱动循环要么漏唤醒要么空转。

    两个方向一起钉：同一 state 连续两次调用必须相等（幂等，否则每步都"变了"
    → 无限唤醒）；只翻转 ``night.map_open`` 必须让签名不等、且**只变化第 19 位**
    （不敏感则地图开合唤不醒循环；变化面过大则误唤醒）。
    """
    state = _healthy_state()
    first = state_signature(state)
    second = state_signature(state)
    assert first == second, "同一 state 两次调用 state_signature 必须相等（幂等）"

    flipped = copy.deepcopy(state)
    flipped["night"]["map_open"] = not state["night"]["map_open"]
    other = state_signature(flipped)
    assert first != other, "night.map_open 翻转后签名必须变化（否则地图开合唤不醒循环）"
    changed = [i for i in range(len(first)) if first[i] != other[i]]
    assert changed == [C.SIG_INDEX["night.map_open"]], (
        f"翻转 map_open 只应影响下标 {C.SIG_INDEX['night.map_open']}，实际 {changed}"
    )


def test_sig_degradation_bit_is_empty_on_healthy_payload():
    """GAP: 降级位在健康载荷下必须为空 tuple —— 否则事件驱动循环每步都认为"有降级"。

    降级位（第 21 位 / ``SIG_DEGRADATION``）折叠 ``STATE_GUARDED_BLOCKS`` 的缺失与
    错误；只要业务块齐全且无 error，它就应当是 ``()``。非空会让恢复逻辑每步都
    触发一次「补读」，把正常一局变成持续降级。
    """
    sig = state_signature(_healthy_state())
    assert len(sig) == len(C.SIG_FIELDS)
    assert C.SIG_FIELDS[C.SIG_DEGRADATION] == "degradation"
    assert sig[C.SIG_DEGRADATION] == (), (
        f"健康载荷的降级位必须是空 tuple，实际 {sig[C.SIG_DEGRADATION]!r}"
    )
    # 反向对照：真降级时该位必须非空，否则「空 tuple」这个判据本身没意义
    broken = _healthy_state()
    broken["economy_error"] = "boom"
    broken_sig = state_signature(broken)
    assert broken_sig[C.SIG_DEGRADATION] != (), "economy_error 在场时降级位必须非空"
    assert broken_sig[: C.SIG_DEGRADATION] == sig[: C.SIG_DEGRADATION], (
        "降级不得污染前 20 位业务签名（否则降级会冒充状态变化反复唤醒循环）"
    )


@pytest.mark.parametrize("missing", ["night", "table", "client", "economy", "queue", "inventory"])
def test_sig_degradation_detects_silently_missing_block(missing):
    """GAP: 整块「静默消失」是 T13 最坏的形态 —— 无 ``_error``、无异常，纯缺键。

    ``mod/src`` 里有 46 处空 ``catch { }`` 会把整块吞掉，连错误都没有。若签名对这种
    情况不变，事件驱动循环就拿着「上一帧的夜晚/桌面/库存」继续规划 —— 正是
    「把不知道当成没有」。这里逐块挖掉，断言降级位必须点出**该块名**。

    前提是载荷仍是完整 bridge 形状（``_healthy_state`` 已给全
    ``_BRIDGE_UNCONDITIONAL_KEYS``）；否则守卫按设计不推断缺失，本测试无意义。
    """
    healthy = _healthy_state()
    assert state_signature(healthy)[C.SIG_DEGRADATION] == (), "夹具本身必须是健康态"

    holed = copy.deepcopy(healthy)
    holed.pop(missing, None)
    bit = state_signature(holed)[C.SIG_DEGRADATION]
    assert bit != (), f"整块 {missing} 消失后降级位必须非空（静默缺失是最坏形态）"
    assert (missing, "missing") in bit, (
        f"降级位必须点出块名 {missing!r} 与 flag 'missing'，实际 {bit!r}"
    )


# ==========================================================================
# D. 禁止作弊接口（前缀 + 参数联合判定）
# ==========================================================================


def test_bridge_client_public_methods_cannot_mutate_economy_directly():
    """GAP: 桥接客户端的作弊面要用「前缀 + 参数」联合判定，不能只看名字。

    只看名字会漏掉 ``set_xxx(state=...)`` 这种「名字人畜无害、参数却直改状态」的
    方法；只看参数又会把 ``set_model_config(**fields)`` 这种合法配置方法误判。
    因此：名字以 ``cheat_`` / ``debug_`` 开头一律不许有；名字以 ``set_`` 开头的，
    参数里不得出现现金/天数/声望/库存这类经济字段。

    注：``get_debug_client`` 是**只读**调试面板接口（无 set 前缀、参数为空），
    是本测试刻意允许的合法例外 —— 它取回数据，不改游戏状态。
    """
    economy_terms = (
        "cash",
        "money",
        "day",
        "reputation",
        "stock",
        "inventory",
        "profit",
        "rent",
    )
    public = [
        (name, getattr(BridgeClient, name))
        for name in dir(BridgeClient)
        if not name.startswith("_") and callable(getattr(BridgeClient, name))
    ]
    assert public, "BridgeClient 没有任何公开方法，本测试前提不成立"

    forbidden_prefix = [n for n, _ in public if n.startswith(("cheat_", "debug_"))]
    assert not forbidden_prefix, f"BridgeClient 出现作弊前缀方法：{forbidden_prefix}"

    offenders = []
    for name, fn in public:
        # 参数名（含 **kwargs 的形参名）：用 signature 而不是 __code__，避免漏掉 var-keyword
        param_names = {p.lower() for p in inspect.signature(fn).parameters}
        hit = [term for term in economy_terms if any(term in p for p in param_names)]
        if name.startswith("set_") and hit:
            offenders.append((name, sorted(param_names), hit))
    assert not offenders, f"set_ 方法的参数里出现经济字段：{offenders}"

    # 反向对照：真正改经济状态的路径只有动作表一条（post_action 必守白名单）
    assert hasattr(BridgeClient, "post_action"), "改状态的唯一入口 post_action 不见了"


# ==========================================================================
# M. is_model_callable 的边界与集合关系（增量，不与 test_contracts.py P 组重复）
# ==========================================================================


@pytest.mark.parametrize("value", [None, 0, 1, 1.5, True, object(), b"sell_item"])
def test_is_model_callable_rejects_non_string_and_none(value):
    """GAP: ``is_model_callable`` 的入参边界 —— ``None`` 与非 str 标量必须返回 False 且不抛异常。

    决策里的 ``action`` 来自模型 JSON，``null`` / 数字 / 布尔都可能出现。若这里抛
    异常，一步坏输出会把整局循环打断（而不是被守卫拒掉继续下一步）。

    边界（本次实测，未写成断言，见交付报告的变更请求）：传入**不可哈希**的容器
    （``[]`` / ``{}``）会抛 ``TypeError: unhashable type``，因为实现是
    ``BY_NAME.get(action)``。当前所有调用点都传字符串，生产不可达，故不把该行为
    钉成契约；若将来有「直接把模型原始 JSON 塞进来」的调用点，需要先加类型守卫。
    """
    assert C.is_model_callable(value) is False


def test_derive_gated_actions_returns_dict_mapping_to_post_launch():
    """GAP: ``derive_gated_actions()`` 返回 dict[str, str]，不是 set。

    直接和 set 比会永远为假（``{'a': 'x'} == {'a'}`` → False），过去已经踩过。
    这里同时钉类型与值，避免有人为了"让比较通过"把它改成 set —— 那会丢掉门禁
    到期时间（``post-launch`` 是唯一能表达"等 1.0 之后再说"的载体）。
    """
    gated = C.derive_gated_actions()
    assert isinstance(gated, dict), f"derive_gated_actions() 必须返回 dict，实际 {type(gated)}"
    assert set(gated) == {"expedition_send", "hire_worker"}
    assert set(gated.values()) == {"post-launch"}


def test_model_callable_is_strict_subset_of_action_table():
    """GAP: 可调集合必须严格小于动作表 —— 相等意味着门禁/恢复专用/弃用全被放出来了。

    数量关系：动作表 59 条，其中 ``expedition_send`` / ``hire_worker``（post-launch
    门禁）与 ``force_dismiss``（恢复专用）不可调，故可调恰好 56 条。这条同时是
    「删动作」的红灯：「删掉 expedition_send 让不可调集合变小」会让动作表掉到 58，
    这里立刻红。
    """
    actions = set(C.derive_actions())
    callable_actions = set(C.derive_model_callable())
    assert callable_actions < actions, "可调集合必须是动作表的**真**子集"
    assert len(actions) == 68
    assert len(callable_actions) == 65
    assert actions - callable_actions == {"expedition_send", "hire_worker", "force_dismiss"}
    assert callable_actions == set(C.derive_model_visible())


# ==========================================================================
# E. 租金守卫的现金来源（行为断言，双向）
# ==========================================================================


def _rent_guard_obs(visible_cash: Any, *, rent_due: int = 693, table: dict | None = None):
    """构造一个「visible_info 里写着 visible_cash」的 observation（现金真值靠签名）。"""
    return {
        "visible_info": {
            "rent": {"day_until_rent": 1, "rent_value": 242, "rent_due": rent_due},
            "table": table or {},
            "client": {},
            "cash": visible_cash,
            "money": visible_cash,
        }
    }


def test_rent_guard_reads_cash_from_signature_not_visible_info():
    """GAP: 租金守卫的现金来源不清 —— 必须取 ``state_signature[SIG_INDEX["cash"]]``。

    ``visible_info`` 里可能出现过期或伪造的 ``cash``/``money``（不同通道拼装时序
    不一致）。守卫若读它，就等于把「现金红线」交给不确定来源。真实签名是
    ``_rent_reserve_guard(decision, observation, state_signature)``，现金取第 4 位。

    双向钉死：
      * 签名现金 442 < rent_due 693，即使 visible_info 写着 999999 → **必须拦**；
      * 签名现金 5000 ≥ rent_due，即使 visible_info 写着 1 → **必须放行**
        （防「无脑全拦」的假保护 —— 那会让 AI 永远不敢进货）。
    """
    decision = {"action": "click_button", "params": {"name": "wholesaleBuyButton"}}
    state = _healthy_state()
    state["cash"] = 442
    state["day_until_rent"] = 1
    state["rent_value"] = 242
    state["rent_due"] = 693

    poor = _rent_guard_obs(999999)
    sig_poor = state_signature(state)
    assert sig_poor[C.SIG_INDEX["cash"]] == 442, "本测试前提：签名里的现金是 442"
    blocked = loop._rent_reserve_guard(decision, poor, sig_poor)
    assert blocked is not None, "签名现金 442 < 693 时必须拦，不许因为 visible_info 有钱就放行"
    assert "693" in blocked

    rich_state = _healthy_state()
    rich_state["cash"] = 5000
    rich_state["day_until_rent"] = 1
    rich_state["rent_value"] = 242
    rich_state["rent_due"] = 693
    sig_rich = state_signature(rich_state)
    assert sig_rich[C.SIG_INDEX["cash"]] == 5000, "本测试前提：签名里的现金是 5000"
    assert loop._rent_reserve_guard(decision, _rent_guard_obs(1), sig_rich) is None, (
        "签名现金充足时必须放行，否则守卫变成无脑全拦"
    )


# ==========================================================================
# N. brain 侧夜晚动作集合同源（R5 派生栅栏的增量）
# ==========================================================================


def test_brain_night_phase_actions_match_contracts_derivation():
    """GAP: ``brain.NIGHT_PHASE_ACTIONS`` 是第三个副本，未被 R5 栅栏覆盖。

    ``agent_loop._NIGHT_PHASE_ACTIONS`` 已有栅栏（test_contracts.py Q2），但
    ``game_agent/brain/brain.py`` 还持有一份同名集合 —— 提示词过滤与执行侧若各用
    各的，会出现「提示词说能做、执行被拦」或反之。这里把它钉到同一派生源。
    类型也要断言：``frozenset`` 被改成 ``set`` 就等于允许运行期改写全局限动白名单。
    """
    derived = C.derive_night_phase()
    assert isinstance(brain.NIGHT_PHASE_ACTIONS, frozenset), (
        f"NIGHT_PHASE_ACTIONS 必须保持 frozenset，实际 {type(brain.NIGHT_PHASE_ACTIONS)}"
    )
    assert brain.NIGHT_PHASE_ACTIONS == derived
    assert brain.NIGHT_PHASE_ACTIONS == loop._NIGHT_PHASE_ACTIONS
    assert len(brain.NIGHT_PHASE_ACTIONS) == 15


# ==========================================================================
# B. 成交判定铁律：ok=true 单独出现不算成交
# ==========================================================================


def _goal_obs(*, cash: float, stock: float, day: int, store_state: str = "OPEN") -> dict:
    """BusinessGoal 能消费的最小 observation（数值口径见 goal._numbers）。"""
    return {
        "visible_info": {
            "money": cash,
            "day": day,
            "store_state": store_state,
            "economy": {"stock_value": stock},
        }
    }


def test_ok_flag_alone_is_not_a_deal_verdict():
    """GAP: 成交判定铁律无独立测试 —— ``ok=true`` 单独出现不得产生任何成交结论。

    docs/26 §1 硬约束③：成交只认**现金 / 库存 / 天数**的 diff，不认动作返回的
    ``ok``。本仓**没有** ``verify_deal`` 之类的判定函数（已 grep 确认：判定逻辑唯一
    落点是提示词 ``game_agent/brain/prompt.py`` 的「只认现金 diff」文案），所以这条
    铁律只能在两个「会因成交而产生后果」的地方行为化：

      1. 目标进度（``BusinessGoal.update``）：现金/库存/天数都没变 → 利润 0、未完成；
      2. 成交后副作用（``agent_loop._needs_stow_after``）：不因 ``ok`` 触发收纳，
         只认「批发成交按钮」这一具体路径 —— 否则一笔假成交就会去点 transferButton。

    反向对照一并钉住：真批发路径必须触发收纳，否则「不触发」这个断言本身没意义。
    """
    goal = BusinessGoal({"goal": {"target_profit": 1000, "window_days": 7}})
    baseline = _goal_obs(cash=1000, stock=500, day=1)
    p0 = goal.update(baseline)
    # 「同一步动作返回 ok=true」在这套接口里根本不进入判定：口径只有数字
    p1 = goal.update(_goal_obs(cash=1000, stock=500, day=1))
    assert p0.profit == 0.0 and p1.profit == 0.0, "状态没变时利润必须是 0"
    assert p1.done is False, "状态没变却判定目标完成 = 把 ok 当成了成交"
    assert p1.cash == 1000 and p1.stock_value == 500

    # 反向：真有现金 diff 时才认（赚 = 现金净增）
    p2 = goal.update(_goal_obs(cash=1100, stock=500, day=2))
    assert p2.profit == pytest.approx(100.0)

    # ok 单独出现不得触发「成交后自动收纳」这类真实副作用
    assert loop._needs_stow_after({"action": "sell_item", "params": {}}) is False
    assert loop._needs_stow_after({"action": "accept_offer", "params": {}}) is False
    assert loop._needs_stow_after({"action": "click_button", "params": {"name": "x"}}) is False
    # 反向对照：批发成交路径必须触发（否则上面三条「不触发」毫无意义）
    assert (
        loop._needs_stow_after(
            {"action": "click_button", "params": {"name": "wholesaleAcceptButton"}}
        )
        is True
    )


# ==========================================================================
# F. 守卫输入同形性（observer 真实产出 → 守卫行为，增量场景）
# ==========================================================================


def test_guard_inputs_survive_observer_roundtrip_for_night_ui():
    """GAP: 夜晚 UI 守卫的输入来自 observer 压缩块 —— 压缩后下标语义必须仍然成立。

    ``_night_ui_guard`` 读签名的第 12 / 19 位（is_outside / map_open），而那两个值
    由 observer 的 ``_compact_night`` 产出。压缩函数若改名或丢键，签名这两位会恒
    None，守卫就静默失效（不会报错，只是不再拦）。这里走一次真实
    ``build_observation``，确认压缩后签名仍能区分「已在外面/地图已开」。
    """
    state = _healthy_state()
    state["night"] = {
        "is_outside": True,
        "current_location": "dumping_ground",
        "map_open": True,
        "visit_left": 1,
    }
    obs = build_observation(state, source=C.CHANNEL_BRIDGE)
    sig = state_signature(state)
    assert sig[C.SIG_INDEX["night.is_outside"]] is True
    assert sig[C.SIG_INDEX["night.map_open"]] is True
    # observer 侧压缩块必须保留这两个键（守卫语义的输入）
    night = obs["visible_info"]["night"]
    assert night["is_outside"] is True
    assert night["map_open"] is True
    assert night["current_location"] == "dumping_ground", (
        "current_location 必须原值保留 —— 拾荒守卫靠它认地点 id"
    )
    # 行为侧：地图已开时再 open_map 必须被拦
    detail = loop._night_ui_guard({"action": "open_map", "params": {}}, sig)
    assert detail is not None, "地图已开还 open_map 必须拦，否则夜间每步空转"
    assert "地图" in detail


# ==========================================================================
# G. 降级可见性：visible_info["degraded"] 与签名末位必须同源
# ==========================================================================


def test_degraded_field_shape_matches_contract():
    """GAP: ``visible_info["degraded"]`` 的结构没人钉过 —— 提示词与守卫都按它写文案。

    R3 的 T13 把降级证据同时投放到两个出口：observer 的
    ``visible_info["degraded"]``（给模型看、给提示词用）和 ``state_signature``
    的第 21 位（给事件驱动循环用）。两者若各写一份判断，就会出现「提示词说有
    降级、循环认为没变」或反之。这里钉结构本身：

      * ``degraded`` 是 list、``is_degraded`` 是 bool，且 ``is_degraded == bool(degraded)``；
      * 每个元素必带 ``block`` / ``mode`` / ``error`` / ``flag`` 四键；
      * ``mode ∈ {A,B,C}``、``flag ∈ {error,missing,null}``（降级项不会是 ``ok``）；
      * ``error`` 在模式 B/C 下是 ``None`` 而非空字符串（R3 明确过这点，
        写成 ``== ""`` 的断言会假红）。
    """
    cases = [
        (_healthy_state(), False, None),
        ({**_healthy_state(), "economy_error": "boom"}, True, ("economy", "error", "A")),
        ({**_healthy_state(), "day": None}, True, ("day", "null", "C")),
        ({}, True, ("in_game", "missing", "B")),
    ]
    for state, want_flag, first in cases:
        vi = build_observation(state, source=C.CHANNEL_BRIDGE)["visible_info"]
        degraded = vi["degraded"]
        assert isinstance(degraded, list), f"degraded 必须是 list，实际 {type(degraded)}"
        assert isinstance(vi["is_degraded"], bool)
        assert vi["is_degraded"] == bool(degraded), (
            "is_degraded 必须等于 bool(degraded) 且是 bool 类型（不是 0/1）"
        )
        assert vi["is_degraded"] is want_flag
        if first is None:
            assert degraded == []
            continue
        block, flag, mode = first
        item = degraded[0]
        assert {"block", "mode", "error", "flag"} <= set(item), (
            f"降级项缺键，实际 {sorted(item)}"
        )
        assert (item["block"], item["flag"], item["mode"]) == (block, flag, mode)
        assert item["error"] is None or isinstance(item["error"], str)
        for entry in degraded:
            assert entry["mode"] in ("A", "B", "C")
            assert entry["flag"] in ("ok", "error", "missing", "null")
            assert entry["flag"] != "ok", "降级列表里不该出现 flag='ok' 的项"


def test_degraded_field_agrees_with_signature_bit():
    """GAP: 两个出口必须同源 —— ``{block,flag}`` 去重有序后要等于签名末位。

    这是跨模块一致性断言里最值钱的一条：它把「observer 怎么说」和「签名怎么变」
    锁在一起。若哪天有人在其中一处加了新判定（例如 observer 抑制了某块而签名没有），
    模型看到的降级与循环感知到的变化就会分叉 —— 表现为「模型一直说某块坏了，
    循环却认为一切正常、从不重新观察」。
    """
    for state in (
        _healthy_state(),
        {**_healthy_state(), "economy_error": "boom"},
        {**_healthy_state(), "day": None},
        {},
        {"in_game": False},
    ):
        vi = build_observation(state, source=C.CHANNEL_BRIDGE)["visible_info"]
        sig_bit = state_signature(state)[C.SIG_DEGRADATION]
        from_visible = tuple(sorted({(e["block"], e["flag"]) for e in vi["degraded"]}))
        assert from_visible == sig_bit, (
            "visible_info.degraded 与 state_signature 末位不同源：\n"
            f"  visible_info: {from_visible}\n  signature   : {sig_bit}"
        )
