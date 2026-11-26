"""`/debug/slots` 的三态契约：`None`（不知道）≠ `[]`（确实空）≠ 有数据。

事故（2026-11-15，台账 §3.7b）
------------------------------
`GET /debug/slots` 在**非店铺场景**返回 `{"ok":false,"error":"..."}`——**没有 `slots`
键，但 HTTP 状态码是 200**。于是 `BridgeClient._request` 认为这是一次成功响应，
两层「宽容」把「不知道」压成了「确认一件都没有」：

1. `client.get_debug_slots()`：`slots if isinstance(slots, list) else []`
2. `observer._observe_bridge()`：`client.get_debug_slots().get("slots") or []`

观察层本来刻意用 `None` 表示「不可观测」、用 `[]` 表示「确实全空」,这层三态设计
因此在真实链路上成了死代码。后果与 r55 同形：拾荒时模型以为地上没货 → `leave_outside`
→ 地上的战利品全部消失。

**为什么 837 个测试全绿**：既有测试都直接给假客户端喂 `None`（或直接调
`_slots_summary(None)`），绕过了「真客户端永远不会产出 `None`」这个事实。
本文件的三组断言分别覆盖三个层次：

- T1 客户端层：`get_debug_slots()` 的形状归一（失败一律 `None`，空列表仍是 `[]`）；
- T2 跨层：走**真实** `_observe_bridge()` 装配路径，验证 client→observer 的契约；
- T3 结构：AST 断言客户端里不存在「isinstance 为假则回落空列表」的写法（防回归）。

纪律（台账 §6.1）：**凡「源码不得包含 X」的断言一律按语法结构判**，不做字符串匹配 ——
`get_debug_slots` 的 docstring 故意引用了旧写法来解释为何废弃它，字符串匹配会立刻假红。
"""

from __future__ import annotations

import ast
import copy
import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:  # 直接按文件跑时也能 import game_agent
    sys.path.insert(0, str(REPO_ROOT))

from game_agent.brain.prompt import build_decision_prompt  # noqa: E402
from game_agent.bridge.client import BridgeClient  # noqa: E402
from game_agent.structured.observer import StructuredObserver  # noqa: E402

CLIENT_SRC = REPO_ROOT / "game_agent" / "bridge" / "client.py"

#: 一份「健康」的 `/state` 载荷——`_observe_bridge()` 需要它来装配观察。
_BASE_STATE: dict[str, Any] = {
    "in_game": True,
    "scene": "Store",
    "cash": 500,
    "store_state": "OPEN",
    "shutter_closed": False,
    "day": 1,
    "day_until_rent": 3,
    "rent_value": 100,
    "is_power_on": True,
    "garbage_level": 0,
    "client": None,
    "dialog": {"is_end": True, "is_choice": False},
    "table": {},
    "inventory": {"owned": [], "not_owned": []},
    "economy": {"stock_value": 100},
    "night": {"is_outside": False, "current_location": None, "map_open": False},
    "queue": {"can_next": True},
}

#: 真实形态的一格（`BuildDebugSlots` 的 ground）。
_GROUND_SLOT = {
    "label": "ground",
    "identifier": "towel",
    "count": 2,
    "gameObject": "ScavengeGround",
    "items": ["towel", "can"],
}


def _client_with_raw_response(monkeypatch, response: dict[str, Any]) -> BridgeClient:
    """造一个真 `BridgeClient`，只把 `_request` 换成返回固定原始响应。

    注意换的是 `_request`（**HTTP 层**），不是 `get_debug_slots`（被测层）——
    被测的正是「原始响应 → 归一结果」这段映射。
    """
    client = BridgeClient(base_url="http://127.0.0.1:1", timeout=1.0)
    monkeypatch.setattr(client, "_request", lambda *a, **k: dict(response))
    return client


class _FakeBridgeClient:
    """只实现 `_observe_bridge()` 需要的四个方法的假客户端。

    `slots_value` 直接决定 `get_debug_slots()` 里 `slots` 键的值——用它模拟
    「客户端层归一之后」的输出，从而验证 observer 不会再把 `None` 压成 `[]`。
    """

    def __init__(self, slots_value: Any) -> None:
        self.slots_value = slots_value
        self.calls: list[str] = []

    def get_state(self) -> dict[str, Any]:
        self.calls.append("state")
        return copy.deepcopy(_BASE_STATE)

    def get_machines(self) -> dict[str, Any]:
        self.calls.append("machines")
        return {"machines": []}

    def get_sellable(self) -> dict[str, Any]:
        self.calls.append("sellable")
        return {"sellable": [], "estimated_total": 0}

    def get_debug_slots(self) -> dict[str, Any]:
        self.calls.append("slots")
        return {"slots": self.slots_value}


def _observe_with(slots_value: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    """走**真实** `_observe_bridge()` 装配路径，返回 `(归一后的 state, 观察)`。"""
    observer = StructuredObserver({"agent": {}}, client=_FakeBridgeClient(slots_value))
    return observer._observe_bridge()


# ---------------------------------------------------------------- T1 客户端层


def test_get_debug_slots_none_when_plugin_reports_failure(monkeypatch):
    """GAP: 插件报 `ok:false` 时 `get_debug_slots()` 必须返回 `{"slots": None}`，**不是 `[]`**。

    实机形态：非店铺场景下 `BuildDebugSlots` 走 `Err("EmporiumEntry 不可用（不在店铺场景）")`，
    响应体 `{"ok":false,"error":"..."}` 且 **HTTP 200** —— `_request` 不会抛 `BridgeError`。
    所以「端点报错」与「七格全空」在这一层长得几乎一样，只能靠 `ok` 区分。
    """
    client = _client_with_raw_response(
        monkeypatch, {"ok": False, "error": "EmporiumEntry 不可用（不在店铺场景）"}
    )

    got = client.get_debug_slots()

    assert got == {"slots": None}, (
        f"插件报错时必须归一成 {{'slots': None}}，实际 {got!r}。"
        "回落成 [] 会把『不知道地上有没有货』渲染成『地上确认没货』，"
        "拾荒时模型据此 leave_outside，地上战利品全部消失（r55 形态）"
    )


def test_get_debug_slots_none_when_slots_key_missing(monkeypatch):
    """GAP: 响应里没有 `slots` 键时必须是 `None`（旧插件没这个端点也走这条）。"""
    client = _client_with_raw_response(monkeypatch, {"ok": True})

    got = client.get_debug_slots()

    assert got == {"slots": None}, f"缺 slots 键时必须归一成 None，实际 {got!r}"


def test_get_debug_slots_none_when_slots_is_not_a_list(monkeypatch):
    """GAP: `slots` 是非法形状（字符串/字典/数字）时必须是 `None`，不得猜。"""
    client = _client_with_raw_response(monkeypatch, {"ok": True, "slots": "nope"})

    got = client.get_debug_slots()

    assert got == {"slots": None}, f"非法形状必须归一成 None，实际 {got!r}"


def test_get_debug_slots_keeps_empty_list(monkeypatch):
    """GAP: 空列表是**合法数据**（确实七格全空），绝不能被也变成 `None`。

    这是「归一」最容易写坏的方向：为了让失败返回 None，顺手加了
    `if not slots: return None` 就会把「确认全空」也吞掉，模型于是永远不知道东西到底在不在。
    """
    client = _client_with_raw_response(monkeypatch, {"ok": True, "slots": []})

    got = client.get_debug_slots()

    assert got == {"slots": []}, (
        f"空列表是合法数据，必须原样保留为 []，实际 {got!r}。"
        "把 [] 变成 None 会让『确认地上没货』退化成『不知道』，模型失去确定性"
    )


def test_get_debug_slots_passes_through_real_data(monkeypatch):
    """GAP: 真数据必须原样透传（不得裁剪、不得重排）。"""
    client = _client_with_raw_response(monkeypatch, {"ok": True, "slots": [_GROUND_SLOT]})

    got = client.get_debug_slots()

    assert got == {"slots": [_GROUND_SLOT]}, f"真数据必须原样透传，实际 {got!r}"


# ---------------------------------------------------------------- T2 跨层装配
#
# 特别说明（Lead 指定写进 docstring）：
# 这一组**必须**走 `_observe_bridge()`，因为它验证的是 client 与 observer 之间的
# **跨层契约**；只测 `_slots_summary` 无法发现上游把 None 吞成 [] ——
# 那正是让 T05 这个 bug 活下来的测法（837 个测试全绿，链路却是断的）。


def test_observe_bridge_omits_slots_key_when_unknown():
    """GAP: 客户端说「不知道」（`None`）时，observer **不写 `slots` 键**。

    这个测试必须走 `_observe_bridge()`，因为它验证的是 client 与 observer 之间的跨层契约；
    只测 `_slots_summary` 无法发现上游把 None 吞成 []。

    「不写键」是刻意设计：观察层把「无键」渲染成 `None`（不可观测）。若这里写 `[]`，
    下游 `_slots_summary([])` 会得到 `([], 0)`——「确认一件都没有」。
    """
    state, observation = _observe_with(None)

    assert "slots" not in state, (
        f"客户端返回 None 时 observer 不得写 slots 键（实际写入了 {state.get('slots')!r}）——"
        "写 [] 会把『不知道』压成『确认全空』"
    )
    assert observation["visible_info"]["slots"] is None, (
        f"降级后 visible_info.slots 必须是 None，实际 {observation['visible_info'].get('slots')!r}"
    )
    assert observation["visible_info"]["slots_total"] is None, (
        f"降级后 slots_total 必须是 None，实际 {observation['visible_info'].get('slots_total')!r}"
    )


def test_observe_bridge_keeps_empty_list_when_confirmed_empty():
    """GAP: 客户端说「确实全空」（`[]`）时，observer 必须如实写 `[]` 与 `slots_total=0`。"""
    state, observation = _observe_with([])

    assert state.get("slots") == [], f"确认全空时必须写 []，实际 {state.get('slots')!r}"
    assert observation["visible_info"]["slots"] == [], (
        f"visible_info.slots 应为 []，实际 {observation['visible_info'].get('slots')!r}"
    )
    assert observation["visible_info"]["slots_total"] == 0, (
        f"确认全空时 slots_total 应为 0，实际 {observation['visible_info'].get('slots_total')!r}"
    )


def test_observe_bridge_passes_real_slots_through():
    """GAP: 有货时 observer 必须把格子数据装配进 `visible_info.slots` 并算出总件数。"""
    state, observation = _observe_with([_GROUND_SLOT])

    assert state.get("slots") == [_GROUND_SLOT], "原始格子数据必须进 state"
    rendered = observation["visible_info"]["slots"]
    assert rendered == [
        {"label": "ground", "identifier": "towel", "count": 2, "items": ["towel", "can"]}
    ], f"渲染结果形状不对：{rendered!r}"
    assert observation["visible_info"]["slots_total"] == 2, (
        f"slots_total 应为 2，实际 {observation['visible_info'].get('slots_total')!r}"
    )


# ---------------------------------------------------------------- T3 结构断言


def _empty_list_fallback_violations(src: str) -> list[str]:
    """扫 `get_debug_slots` 函数体，找出「isinstance 为假则回落空列表」的写法。

    两种形态都要拦：
      * `a if isinstance(x, list) else []` —— `IfExp` 的 `orelse` 是空 `ast.List`；
      * `x.get("slots") or []` —— `BoolOp(Or)` 里出现空 `ast.List`。

    **按语法结构判，不做字符串匹配**：该函数的 docstring 里故意引用了旧写法
    （`slots if isinstance(slots, list) else []`）来解释为何废弃它，
    字符串匹配会立刻假红；AST 里 docstring 只是 `ast.Constant`，不会被当成代码。
    """
    tree = ast.parse(src)
    fn = next(
        (
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == "get_debug_slots"
        ),
        None,
    )
    if fn is None:
        return ["没找到 get_debug_slots 函数定义（改名了？本断言会失效）"]

    problems: list[str] = []
    for node in ast.walk(fn):
        if isinstance(node, ast.IfExp):
            if isinstance(node.orelse, ast.List) and not node.orelse.elts:
                problems.append(
                    f"第 {node.lineno} 行：`{ast.unparse(node)}` —— "
                    "isinstance 为假时回落到空列表，等于把「不知道」写成「确认没有」"
                )
        elif isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or):
            for value in node.values[1:]:
                if isinstance(value, ast.List) and not value.elts:
                    problems.append(
                        f"第 {node.lineno} 行：`{ast.unparse(node)}` —— "
                        "`or []` 会把 None 兜底成空列表（2026-11-15 回归的第二种形态）"
                    )
    return problems


def test_get_debug_slots_has_no_empty_list_fallback():
    """GAP: 客户端里不得再有「拿不到就回落 `[]`」的写法（AST 判定，防回归）。

    这是本组唯一的结构断言：行为断言能证明**现在**是对的，
    结构断言能证明**写法**不会再被改回去（例如有人为了让返回值「总是 list」而加回兜底）。
    """
    problems = _empty_list_fallback_violations(
        CLIENT_SRC.read_text(encoding="utf-8-sig")
    )

    assert not problems, (
        "get_debug_slots 里出现了「失败回落空列表」的写法——会把『不知道』压成"
        "『确认一件都没有』：\n  " + "\n  ".join(problems)
    )


# ---------------------------------------------------------------- 提示词侧（R5 CR）


def _prompt_obs() -> dict[str, Any]:
    """带 `slots` 字段的最小观察（`null` = 不知道）。"""
    return {
        "source": "bridge",
        "screen_state": "shop_counter",
        "available_actions": [],
        "important_objects": [],
        "confidence": 1.0,
        "visible_info": {"slots": None, "slots_total": None},
    }


def test_prompt_documents_slots_section_in_no_mouse_mode():
    """GAP: 【格子读数】小节必须出现在**禁鼠标生产模式**的提示词里（R5 的 CR）。

    `no_mouse=True` 这个参数是关键：该小节挂在常驻段 `BRIDGE_ACTION_TOOLS` 上，
    而 `coord_block` 在 `no_mouse` 下会被整段清空（`prompt.py` 的
    `coord_block = "" if no_mouse else ...`）。若日后有人把本节挪进 `coord_block`
    或 `FULL_RELEASE_NOTE`，生产模式会整段丢弃 —— 只有带 `no_mouse=True` 的断言能抓到。
    """
    prompt = build_decision_prompt(_prompt_obs(), bridge_actions=True, no_mouse=True)

    assert "【格子读数（visible_info.slots）" in prompt, (
        "禁鼠标生产模式的提示词里缺【格子读数】小节——"
        "该节必须挂在常驻段 BRIDGE_ACTION_TOOLS 上，不能放进 no_mouse 下会被清空的 coord_block"
    )


def test_prompt_slots_section_forbids_capacity_inference():
    """GAP: 讲 `slots` 时必须同时给出**禁止性表述**（不许推断容量）。

    用「必须存在禁止句」的**正向**断言，而不是 `assert "还能再塞" not in prompt`：
    后者太窄——文案换个说法（「再放 M 件」「还有几个空位」）就能绕过。

    背景：`GameInventory` 基类无 capacity，`GameGridInventory` 的宽高与
    `GameSlotInventory` 的网格尺寸全是 private/readonly，反射读不到。
    所以「这一格里还有 N 件」是事实，「还能再塞 M 件」是编造。
    """
    prompt = build_decision_prompt(_prompt_obs(), bridge_actions=True, no_mouse=True)
    slots_idx = prompt.find("【格子读数（visible_info.slots）")
    assert slots_idx >= 0, "提示词里没有【格子读数】小节，后面的禁止性断言无从谈起"

    # 只在该小节范围内找（避免命中提示词其它段落里无关的「禁止」）
    section = prompt[slots_idx : slots_idx + 1200]

    assert "没有容量字段" in section, (
        "【格子读数】小节必须明说「没有容量字段」——"
        "否则模型会把件数当容量，编造「还能再塞 M 件」"
    )
    assert "绝不许" in section or "禁止" in section, (
        "【格子读数】小节必须带禁止性表述（如『绝不许推断还能再塞 M 件』），"
        "只写「没有容量字段」不足以阻止模型推断"
    )
