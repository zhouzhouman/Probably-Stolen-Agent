# -*- coding: utf-8 -*-
"""X01 契约一致性测试（由 R1 草案 `artifacts/r1/test_contracts_draft.py` 落地到 tests/）。

净新增用例，与 X03 测试治理无关；不 import `artifacts/**`，可独立运行::

    python -m pytest -q tests/test_contracts.py

I 组（禁区谓词）的落地口径（Lead 裁决，R7 执行）：草案里「源码扫描 `_guard*`
函数体是否读取 `wholesale_cost`」的写法**不落地**——R2 正在给 BuildTable 补该字段
（CR-T04-1），补完后读它正确且必要；若按源码扫描落 xfail(strict=True)，补完字段后
该命题仍成立 → xfail 永不解禁，变成永久假红。改为 I4/I5/I6/I7 四条**行为断言**。
`is_forbidden_guard_input()` 谓词保留，仅作文档/告警用途（见 I1/I2/I3）。
"""
from __future__ import annotations

import json

import pytest

import game_agent
import game_agent.agent_loop as loop
import game_agent.brain.brain as brain
import game_agent.contracts as C
from game_agent.brain.schema import (
    _IDENTIFIER_ACTIONS,
    ALLOWED_ACTIONS,
    BRIDGE_ACTION_PARAMS,
)
from game_agent.bridge.client import ACTIONS
from game_agent.structured.channels import state_signature


# ==========================================================================
# A. 派生结果与线上代码逐项相等
# ==========================================================================


def test_a1_actions_derived_equals_live_and_order():
    assert C.derive_actions() == tuple(ACTIONS)
    assert len(C.derive_actions()) == 68


def test_a2_params_derived_equals_live():
    derived = C.derive_params()
    assert set(derived) == set(BRIDGE_ACTION_PARAMS)
    for name in derived:
        assert set(derived[name]) == set(BRIDGE_ACTION_PARAMS[name]), name


def test_a3_params_are_subset_of_actions():
    assert set(C.derive_params()) <= set(C.derive_actions())


def test_a4_allowed_actions():
    assert C.derive_allowed() == {k: set(v) for k, v in ALLOWED_ACTIONS.items()}
    assert len(C.derive_allowed()) == 7


def test_a5_night_only_and_day_only_disjoint():
    assert C.derive_night_only() == set(loop._NIGHT_ONLY_ACTIONS)
    assert C.derive_day_only() == set(loop._DAY_ONLY_ACTIONS)
    assert C.derive_night_only().isdisjoint(C.derive_day_only())


def test_a6_night_phase_actions_single_source():
    assert (
        C.derive_night_phase()
        == loop._NIGHT_PHASE_ACTIONS
        == brain.NIGHT_PHASE_ACTIONS
    )
    assert len(C.derive_night_phase()) == 15


def test_a7_once_per_cycle():
    assert C.derive_once_per_cycle() == loop._ONCE_PER_CYCLE_ACTIONS


def test_a8_recovery_actions():
    assert C.derive_recovery() == loop._RECOVERY_ACTIONS
    assert C.derive_recovery() == {"sell_item": "clear_table"}


def test_a9_action_cn_no_fabrication():
    assert C.derive_action_cn() == loop.ACTION_CN
    assert len(C.derive_action_cn()) == 43
    # 现状确实没有中文名的动作，登记 None 而不是编造
    assert C.BY_NAME["force_dismiss"].cn_name is None
    assert C.BY_NAME["buy_insurance"].cn_name is None
    # 有中文名的必须真的出现在表里
    assert C.derive_action_cn()["open_shutter"] == "开门营业"


# ==========================================================================
# B. C# 侧覆盖关系
# ==========================================================================


def test_b1_csharp_case_count():
    assert len(C.derive_csharp_cases()) == 70


def test_b2_coverage_chain():
    cases = C.derive_csharp_cases()
    assert cases >= set(ACTIONS) >= set(BRIDGE_ACTION_PARAMS) | {"force_dismiss"}


def test_b3_every_python_action_implemented_in_csharp():
    assert set(ACTIONS) <= C.derive_csharp_cases()


def test_b4_deprecated_csharp_only_cases():
    assert C.derive_csharp_cases() - set(ACTIONS) == {
        "close_negociation",
        "toggle_power",
    }
    for name in ("close_negociation", "toggle_power"):
        spec = C.BY_NAME[name]
        assert spec.deprecated is True
        assert spec.implemented_in_csharp is True
        assert spec.in_python_actions is False
        assert spec.in_param_table is False
        assert spec.model_visible is False


def test_b5_force_dismiss_recovery_only():
    fd = C.BY_NAME["force_dismiss"]
    assert fd.in_python_actions is True
    assert fd.in_param_table is False
    assert fd.model_visible is False
    assert fd.phase == "recovery"
    assert "force_dismiss" not in BRIDGE_ACTION_PARAMS


# ==========================================================================
# C. 未实装门禁
# ==========================================================================


def test_c1_gated_actions_post_launch():
    assert C.derive_gated_actions() == {
        "expedition_send": "post-launch",
        "hire_worker": "post-launch",
    }
    for name in ("expedition_send", "hire_worker"):
        spec = C.BY_NAME[name]
        assert spec.model_visible is False
        assert spec.gated_until == "post-launch"
        # 物理白名单保持现状（tests/test_player_interfaces.py 有断言）
        assert spec.in_python_actions is True
        assert spec.in_param_table is True


def test_c2_model_visible_excludes_gated_and_recovery():
    mv = C.derive_model_visible()
    assert mv.isdisjoint({"expedition_send", "hire_worker"})
    assert mv.isdisjoint({"force_dismiss", "close_negociation", "toggle_power"})


# ==========================================================================
# D. 参数补登
# ==========================================================================


def test_d1_click_button_op_registered():
    assert "op" in BRIDGE_ACTION_PARAMS["click_button"]
    assert C.BY_NAME["click_button"].params["op"].enum == C.CLICK_OPS
    assert C.CLICK_OPS == ("click", "double_click", "right_click")


def test_d2_machine_module_install_occurrence_registered():
    assert "occurrence" in BRIDGE_ACTION_PARAMS["machine_module_install"]


def test_d3_click_locator_shared():
    assert set(C.BY_NAME["object_action"].params) == set(
        C.BY_NAME["click_button"].params
    )


# ==========================================================================
# E. destination 枚举
# ==========================================================================


def test_e1_dest_all_union():
    """DEST_ALL = 别名 ∪ 已知 id ∪ 旧名映射键。

    原先 `upgrade` 家族被登记为 `DEST_DEPRECATED`（拒绝语义），R4 的新证据推翻了
    该判断：它们是 `dr_jackson`（升级商人）的旧名 —— 见 `tools/decompile_full/
    EmporiumAnalytics.cs:524` `"dr_jackson" => "UPGRADE_MERCHANT"`、
    `MapUIManager.cs:25/412` 的 `UIUpgradeMerchantPanel` / `LeaveUpgradeMerchant()`。
    因此改为 `DEST_ALIAS_MAP` 映射语义，`go_out` 恢复接受这 4 个名字。
    """
    assert len(C.DEST_ALIASES) == 10
    assert len(C.DEST_IDS) == 3
    assert len(C.DEST_ALIAS_MAP) == 4
    assert set(C.DEST_ALL) == (
        set(C.DEST_ALIASES) | set(C.DEST_IDS) | set(C.DEST_ALIAS_MAP)
    )
    # DEST_ALL 本身不去重（别名 + id + 旧名三段直接拼接，commissary 出现两次）
    assert len(C.DEST_ALL) == len(C.DEST_ALIASES) + len(C.DEST_IDS) + len(C.DEST_ALIAS_MAP)
    # 去重后 16：commissary 同时是别名与已知 id
    assert len(set(C.DEST_ALL)) == 16


def test_e2_upgrade_family_maps_to_dr_jackson():
    """`upgrade`/`upgrade_merchant`/`升级`/`升级商人` 是 dr_jackson 的旧名，不是非法地点。

    R4 证据：`EmporiumAnalytics.cs:524` 把 `dr_jackson` 映射成 `UPGRADE_MERCHANT`；
    `MapUIManager.cs:25` 有 `UIUpgradeMerchantPanel` 字段、`:412` 有 `LeaveUpgradeMerchant()`。
    所以这 4 个名字必须**可传且被归一化到 dr_jackson**，而不是被拒绝。
    """
    family = {"upgrade", "upgrade_merchant", "升级", "升级商人"}
    assert set(C.DEST_ALIAS_MAP) == family
    assert set(C.DEST_ALIAS_MAP.values()) == {"dr_jackson"}
    assert "dr_jackson" in C.DEST_IDS
    # 旧名合法：都在 DEST_ALL 里（go_out 的 destination 参数枚举取自该表）
    assert family <= set(C.DEST_ALL)
    # 拒绝语义的表已彻底移除，防止两套语义并存
    assert not hasattr(C, "DEST_DEPRECATED")


def test_e3_known_location_ids_present():
    assert {"commissary", "dumping_ground", "dr_jackson"} <= set(C.DEST_ALL)


def test_e4_cjk_aliases_present():
    for alias in ("配给站", "杂货", "黑市", "垃圾场", "拾荒"):
        assert alias in C.DEST_ALL


# ==========================================================================
# F. state_signature 形状
# ==========================================================================


def test_f1_sig_fields_shape():
    """三处长度必须同步：SIG_FIELDS / state_signature({}) / SIG_INDEX。

    v0.9.16 基线 20 位（0..19），CR-X01-2 追加第 21 位 `degradation`。
    这里**不写死条数**（写死会在契约扩展瞬间变成纯噪音红），改为断言三者同步 +
    不短于 20（防被悄悄截断）。
    """
    assert len(C.SIG_FIELDS) == len(state_signature({})) == len(C.SIG_INDEX)
    assert len(C.SIG_FIELDS) >= 20
    assert C.SIG_INDEX["night.map_open"] == 19
    assert C.SIG_FIELDS[-1] == "degradation"  # CR-X01-2：降级证据位固定在末尾


def test_f2_sig_named_constants_match_agent_loop():
    assert C.SIG_IS_OUTSIDE == loop._SIG_IS_OUTSIDE == 12
    assert C.SIG_MAP_OPEN == loop._SIG_MAP_OPEN == 19
    assert state_signature({})[C.SIG_MAP_OPEN] == state_signature({})[19]


# ==========================================================================
# G. /state 字段契约与三种降级形态
# ==========================================================================


def test_g1_state_field_counts():
    assert len(C.STATE_FIELDS) == 56
    assert len(C.STATE_ERROR_FIELDS) == 22
    assert set(C.STATE_ERROR_FIELDS) <= set(C.STATE_FIELDS)


def test_g2_guarded_block_mapping():
    assert len(C.STATE_GUARDED_BLOCKS) == 22
    assert C.STATE_GUARDED_BLOCKS["queue_error"] == "queue"


def test_g3_observer_synthesized_not_in_state():
    assert not (set(C.STATE_OBSERVER_SYNTHESIZED) & set(C.STATE_FIELDS))
    assert set(C.STATE_OBSERVER_SYNTHESIZED) == {
        "machines",
        "sellable",
        "sellable_total",
    }


def test_g4_missing_in_csharp_not_in_state():
    assert C.STATE_FIELDS_MISSING_IN_CSHARP == {
        "inventory_counts",
        "dialogue",
    }
    assert not (C.STATE_FIELDS_MISSING_IN_CSHARP & set(C.STATE_FIELDS))
    # ``wholesale_cost`` 已由 CR-T04-1 补进 C#（``BridgeApi.cs:1234``
    # ``BuildTable`` 发射 ``ps.GetWholesalePrice()``），因此它**必须不再**
    # 出现在「C# 没有」集合里，且必须在桥接通道上可用。
    assert "wholesale_cost" not in C.STATE_FIELDS_MISSING_IN_CSHARP
    assert C.field_available_on("wholesale_cost", C.CHANNEL_BRIDGE) is True


def test_g5_silent_catch_registry_complete():
    assert len(C.SILENT_CATCH_KEYS) == 112
    assert len(set(C.SILENT_CATCH_KEYS.values())) == 17
    assert set(C.SILENT_CATCH_KEYS) == set(C.SILENT_CATCH_PATHS)


def test_g6_guard_input_keys_are_in_ledger():
    for key in ("can_next", "has_client", "can_pay_rent", "rent_due"):
        assert key in C.SILENT_CATCH_KEYS


def test_g7_mode_a_and_b_disjoint():
    assert not (set(C.SILENT_CATCH_KEYS) & set(C.STATE_ERROR_FIELDS))


def test_g8_silent_blocks_are_state_fields():
    fields = set(C.STATE_FIELDS)
    for key, block in C.SILENT_CATCH_KEYS.items():
        assert block == "<top-level>" or block in fields, key


def test_g9_duplicate_keys_have_full_paths():
    """重名键必须给完整路径（不能只按键名取值，否则守卫会读错块）。"""
    for key in ("id", "text", "title", "count", "shooting"):
        assert len(C.SILENT_CATCH_PATHS[key]) >= 1
    # 每条路径都必须以所属块开头（顶层键即等于键名）
    for key, paths in C.SILENT_CATCH_PATHS.items():
        block = C.SILENT_CATCH_KEYS[key]
        for path in paths:
            if block == "<top-level>":
                assert path == key
            else:
                assert path == block or path.startswith(block + "."), (key, path, block)
    # 同一键名在别的块里也出现时，路径不得混淆（shooting 在 unlocks 与顶层各一份）
    assert C.SILENT_CATCH_PATHS["shooting"] == ("unlocks.shooting",)
    assert C.SILENT_CATCH_PATHS["id"] == ("reputations.id",)


def test_g10_degradation_modes_documented():
    assert set(C.DEGRADATION_MODES) == {"A", "B", "C"}


# ==========================================================================
# H. 降级四态语义
# ==========================================================================


def test_h1_four_states_distinct():
    assert C.field_state({"k": 1}, "k") == C.FIELD_OK
    assert C.field_state({"k": None}, "k") == C.FIELD_NULL
    assert C.field_state({}, "k") == C.FIELD_MISSING
    assert C.field_state({"k": 1}, "k", "boom") == C.FIELD_ERROR
    assert C.FIELD_DEGRADED == frozenset(
        {C.FIELD_NULL, C.FIELD_MISSING, C.FIELD_ERROR}
    )
    assert not C.is_degraded(C.FIELD_OK)
    assert all(C.is_degraded(f) for f in C.FIELD_DEGRADED)


def test_h2_sig_sentinels_distinguish_null_from_missing():
    assert C.sig_encode({"k": None}, "k") != C.sig_encode({}, "k")
    assert C.sig_encode({}, "k") == C.SIG_SENTINEL_MISSING
    assert C.sig_encode({"k": None}, "k") == C.SIG_SENTINEL_NULL
    assert C.sig_encode({"k": 1}, "k", "x") == C.SIG_SENTINEL_ERROR
    assert C.sig_encode({"k": 1}, "k") == 1


def test_h3_field_state_tolerates_non_mapping():
    assert C.field_state("not-a-dict", "k") == C.FIELD_MISSING
    assert C.field_state(None, "k") == C.FIELD_MISSING


# ==========================================================================
# I. 禁区谓词
# ==========================================================================


def test_i1_forbidden_guard_inputs_detected():
    for path in (
        "inventory_counts",
        "dialogue",
        "dialogue.text",
    ):
        assert C.is_forbidden_guard_input(path) is True
    # ``wholesale_cost`` 已于 2026-11-15 移出禁区：CR-T04-1 兑现后
    # ``BridgeApi.cs:1234`` 真会发射它，此时再判「读它就失效」就是假告警。
    for path in ("wholesale_cost", "table.wholesale_cost"):
        assert C.is_forbidden_guard_input(path) is False


def test_i2_normal_paths_not_flagged():
    for path in ("", "night.map_open", "cash", "queue.can_next", "rent_due"):
        assert C.is_forbidden_guard_input(path) is False


def test_i3_forbidden_set_is_the_documented_pair():
    """恒不可得字段集合只作文档/告警用 —— **不得**用它做「守卫读了即失效」的硬断言。

    守卫读 wholesale_cost 本身没有错（CR-T04-1 已补该字段，读它正确且必要，
    故它已从本集合移出）。失效的判据只能是行为，见 I4/I5。
    """
    assert C.FORBIDDEN_GUARD_INPUTS == C.STATE_FIELDS_MISSING_IN_CSHARP
    assert C.FORBIDDEN_GUARD_INPUTS == {"inventory_counts", "dialogue"}


def _rent_obs(table, cash=442, rent_due=693, rent_value=242, day_until_rent=1):
    """构造 r79 原场景的 observation（现金 442 < 真实应缴 693）。"""
    return {
        "cash": cash,
        "rent_due": rent_due,
        "rent_value": rent_value,
        "day_until_rent": day_until_rent,
        "visible_info": {
            "rent": {
                "rent_due": rent_due,
                "rent_value": rent_value,
                "day_until_rent": day_until_rent,
            },
            "table": table,
            "client": {},
        },
    }


def test_i4_rent_guard_blocks_wholesale_when_cost_unavailable():
    """r79 原场景：table 里没有 wholesale_cost（C# 真实产出形态）时，现金 442 < 693 必须拦截。

    守住的不变量是「不可得 ≠ 花 0 元」，不是「不许读该字段」。
    无论 C# 将来是否补 wholesale_cost，这条都应成立。
    """
    obs = _rent_obs({})
    decision = {"action": "click_button", "params": {"name": "wholesale"}}
    result = loop._rent_reserve_guard(decision, obs, state_signature(obs))
    assert result is not None
    assert "693" in result


def test_i5_rent_guard_still_blocks_when_cost_is_available():
    """反向：字段可得时也必须拦（证明 CR-T04-1 补完字段不会让守卫失效）。"""
    obs = _rent_obs({"wholesale_cost": "335"})
    decision = {"action": "click_button", "params": {"name": "wholesale"}}
    result = loop._rent_reserve_guard(decision, obs, state_signature(obs))
    assert result is not None
    assert "693" in result


def test_i6_rent_guard_does_not_block_when_cash_is_sufficient():
    """守住「不是无脑全拦」：现金充足时批发放行，避免把人为的假拦截当成保护。"""
    obs = _rent_obs({}, cash=5000)
    decision = {"action": "click_button", "params": {"name": "wholesale"}}
    assert loop._rent_reserve_guard(decision, obs, state_signature(obs)) is None


def test_i7_rent_guard_ignores_non_spending_actions():
    """非采购动作不受租金红线影响。"""
    obs = _rent_obs({})
    decision = {"action": "sell_item", "params": {"identifier": "x"}}
    assert loop._rent_reserve_guard(decision, obs, state_signature(obs)) is None


# ==========================================================================
# J. 防作弊契约
# ==========================================================================


def test_j1_no_cheat_action_names():
    for name in ACTIONS:
        assert not any(pat in name for pat in C.CHEAT_ACTION_PATTERNS), name


def test_j2_economy_mutating_registered():
    assert C.ECONOMY_MUTATING_ACTIONS <= set(ACTIONS)
    assert "end_day" in C.ECONOMY_MUTATING_ACTIONS
    assert "pay_rent" in C.ECONOMY_MUTATING_ACTIONS


def test_j3_cheat_patterns_nonempty():
    assert C.CHEAT_ACTION_PATTERNS
    assert "set_cash" in C.CHEAT_ACTION_PATTERNS


# ==========================================================================
# K. 契约比对三场景（V6）
# ==========================================================================


def test_k1_healthy_contract_has_no_problems():
    assert C.check_contract(C.contract_payload()) == []


def test_k2_v6a_csharp_has_extra_action():
    problems = C.check_contract({"actions": [{"name": "ghost_action", "params": []}]})
    assert any("ghost_action" in p for p in problems)


def test_k3_v6b_python_has_action_csharp_lacks():
    remote = {
        "actions": [
            {"name": n, "params": sorted(C.BY_NAME[n].params)}
            for n in C.derive_csharp_cases()
            if n != "save_game"
        ]
    }
    problems = C.check_contract(remote)
    assert any("save_game" in p for p in problems)


def test_k4_v6c_param_missing():
    remote = {
        "actions": [
            {"name": "click_button", "params": ["name", "text", "path", "type"]}
        ]
    }
    assert C.check_contract(remote) != []


def test_k5_v6d_state_field_extra():
    problems = C.check_contract(
        {"state_fields": list(C.STATE_FIELDS) + ["bogus_field"]}
    )
    assert any("bogus_field" in p for p in problems)


def test_k6_assert_contract_raises_with_problems():
    with pytest.raises(C.ContractMismatch) as ei:
        C.assert_contract({"actions": [{"name": "ghost_action", "params": []}]})
    assert ei.value.problems


def test_k7_graceful_when_contract_endpoint_absent():
    assert C.check_contract({}) == []
    assert C.check_contract({"actions": []}) == []


def test_k8_payload_roundtrip():
    assert C.check_contract(json.loads(json.dumps(C.contract_payload()))) == []


def test_k9_silent_catch_remote_mismatch_detected():
    assert C.check_contract({"silent_catch_keys": {"bogus_silent_key": ["x"]}}) != []


# ==========================================================================
# L. 元数据完整性
# ==========================================================================


def test_l1_every_spec_has_line_and_version():
    for spec in C.SPECS:
        assert spec.csharp_line > 0, spec.name
        assert spec.since_version, spec.name


def test_l2_phase_values_legal():
    for spec in C.SPECS:
        assert spec.phase in {"day", "night", "both", "recovery"}, spec.name


def test_l3_risky_actions_have_guard_hint():
    risky = [
        s
        for s in C.SPECS
        if (
            any(p.required for p in s.params.values())
            or s.deprecated
            or s.gated_until
            or s.once_per_cycle
            or s.recovery_fallback
            or s.night_phase_ok
            or s.name in C.derive_night_only()
            or s.name in C.derive_day_only()
        )
    ]
    assert risky
    assert all(s.guard_hint for s in risky)


def test_l4_identifier_required_matches_schema():
    derived = {n for n in C.derive_params() if C.spec_requires_identifier(n)}
    assert derived == set(_IDENTIFIER_ACTIONS)
    assert derived == {
        "appraise",
        "buy_item",
        "display_case",
        "place_item",
        "sell_item",
        "ui_item_point",
    }
    assert C.spec_requires_identifier("equip_backpack") is False
    assert C.spec_requires_identifier("no_such_action") is False


def test_l5_version_single_source():
    assert C.CONTRACT_VERSION == game_agent.__version__


# ==========================================================================
# M. 分通道可用性
# ==========================================================================


def test_m1_unregistered_fields_available_everywhere():
    assert C.field_channels("cash") == C.CHANNELS


def test_m2_statewatch_only_fields():
    assert C.field_available_on("client.intent", C.CHANNEL_STATEWATCH) is True
    assert C.field_available_on("client.intent", C.CHANNEL_BRIDGE) is False


def test_m3_forbidden_fields_unavailable_on_bridge():
    # ``wholesale_cost`` 现在**只有桥接通道**能拿到（``BridgeApi.cs:1234``），
    # 这是 2026-11-15 的修正：此前误登记为 frozenset() = 两通道都不可用。
    assert C.field_available_on("wholesale_cost", C.CHANNEL_BRIDGE) is True
    assert C.field_available_on("wholesale_cost", C.CHANNEL_STATEWATCH) is False
    assert C.field_available_on("dialogue", C.CHANNEL_BRIDGE) is False
    # statewatch 通道的 wholesale_cost 不可用，但字段本身不禁读
    assert C.is_forbidden_guard_input("wholesale_cost") is False


# ==========================================================================
# O. 自检入口
# ==========================================================================


def test_o1_selfcheck_is_green():
    assert C.selfcheck(verbose=False) == 0


# ==========================================================================
# P. 模型可调性判定 is_model_callable（docs/26 §1 硬约束⑥：远征/雇工 1.0 未实装）
# ==========================================================================


def test_p1_gated_actions_are_not_model_callable():
    """GAP: 未实装门禁——expedition_send / hire_worker 冻结到 post-launch，模型不可调。

    硬约束⑥：远征与雇工在 1.0 正式版不实装，禁止实现派遣/招募链路。
    这两个动作必须是「存在但不可调」，不是「不存在」。
    """
    assert C.is_model_callable("expedition_send") is False
    assert C.is_model_callable("hire_worker") is False
    for name in ("expedition_send", "hire_worker"):
        assert C.BY_NAME[name].gated_until == "post-launch"
        assert C.BY_NAME[name].model_visible is False


def test_p2_gated_actions_still_exist_in_action_table():
    """GAP: 冻结动作不得从 ACTIONS 删除——删掉会把 59 掉到 57，破坏 C# 契约一致性。

    「不可调」不等于「不存在」：C# 有 case、Python 有名字，只是不给模型看。
    这条与 P1 成对存在，防止有人用「删掉它」来实现「禁止调它」。
    """
    actions = C.derive_actions()
    assert "expedition_send" in actions
    assert "hire_worker" in actions
    assert len(actions) == 68
    assert set(actions) == set(ACTIONS)


def test_p3_recovery_and_deprecated_are_not_model_callable():
    """GAP: 恢复专用与弃用动作不得出现在模型可调集合里。"""
    # 恢复专用：C# 有 case，但参数表故意不登记（模型不该看见）
    assert C.is_model_callable("force_dismiss") is False
    assert C.BY_NAME["force_dismiss"].model_visible is False
    # 弃用：C# 只有 case，Python 侧已移除
    assert C.is_model_callable("close_negociation") is False
    assert C.is_model_callable("toggle_power") is False
    for name in ("close_negociation", "toggle_power"):
        assert C.BY_NAME[name].deprecated is True


def test_p4_normal_actions_are_model_callable():
    """GAP: 反向防误伤——正常动作必须仍可调，否则守卫把合法动作全禁掉。"""
    for name in ("sell_item", "scavenge", "end_day", "click_button", "buy_item"):
        assert C.is_model_callable(name) is True, name


def test_p5_unknown_action_is_not_model_callable():
    """GAP: 未登记的动作名一律不可调（默认拒绝，不是默认放行）。"""
    for name in ("no_such_action", "", "SET_CASH", "expedition_send "):
        assert C.is_model_callable(name) is False, name


def test_p6_model_callable_matches_derive_model_callable():
    """GAP: 单点判定与集合派生必须同源——不允许两套口径各说各话。"""
    derived = C.derive_model_callable()
    assert set(derived) == {n for n in C.derive_actions() if C.is_model_callable(n)}
    assert set(derived).isdisjoint({"expedition_send", "hire_worker"})
    assert set(derived).isdisjoint({"force_dismiss", "close_negociation", "toggle_power"})
    assert set(derived) <= set(C.derive_actions())


# ==========================================================================
# Q. agent_loop 手写副本 → contracts 派生（R5 改造的栅栏）
# ==========================================================================


def test_q1_night_day_only_sets_not_drifted():
    """GAP: agent_loop 的昼夜动作白名单不得与 contracts 派生值漂移。

    R5 正在把 agent_loop 的 5 处手工副本改成派生。改到一半时值仍应相等，
    这条守的是「值不能漂」——有人手工改错一个字符就红。
    """
    assert loop._NIGHT_ONLY_ACTIONS == C.derive_night_only()
    assert loop._DAY_ONLY_ACTIONS == C.derive_day_only()
    assert loop._NIGHT_ONLY_ACTIONS.isdisjoint(loop._DAY_ONLY_ACTIONS)


def test_q2_night_phase_and_once_per_cycle_not_drifted():
    """GAP: 夜晚动作集合与每轮一次动作集合不得与派生值漂移。"""
    assert loop._NIGHT_PHASE_ACTIONS == C.derive_night_phase()
    assert loop._ONCE_PER_CYCLE_ACTIONS == C.derive_once_per_cycle()


def test_q3_action_cn_not_drifted():
    """GAP: 中文动作名映射不得与派生值漂移（含「不确定就登记 None 而非编造」）。"""
    assert loop.ACTION_CN == C.derive_action_cn()
    assert len(loop.ACTION_CN) == 43


def test_q4_recovery_map_not_drifted():
    """GAP: 恢复动作映射不得与派生值漂移。"""
    assert loop._RECOVERY_ACTIONS == C.derive_recovery()


def test_q5_container_types_preserved():
    """GAP: 派生改造不许悄悄改容器类型——不可变语义是并发安全的依据。

    `frozenset` 被改成 `set` 会让「谁都能改全局白名单」变成合法操作；
    `dict` 被改成 `list` 会让按键取值失效。
    """
    assert isinstance(loop._NIGHT_ONLY_ACTIONS, set)
    assert isinstance(loop._DAY_ONLY_ACTIONS, set)
    assert isinstance(loop._NIGHT_PHASE_ACTIONS, frozenset)
    assert isinstance(loop._ONCE_PER_CYCLE_ACTIONS, frozenset)
    assert isinstance(loop.ACTION_CN, dict)
    assert isinstance(loop._RECOVERY_ACTIONS, dict)
