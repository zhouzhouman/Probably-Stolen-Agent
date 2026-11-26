"""`/state` economy 段的夜间服务费契约（`service_total` / `service_affordable`）。

背景（v3.11 / T02-P0，2026-11-15）
--------------------------------
AI 要在打烊前决定「还要不要勾服务、要不要先卖货回笼现金」，但原先拿不到今晚已选服务的
合计费用——它只能把 `services[]` 里十几项 `cost` 自己加一遍，而 mandatory 项（保护费）
用户根本取消不了，漏加一项就会得出「钱够」的错误结论。

C# 侧新增两个键，口径**照抄游戏自身**（不发明求和规则）：
* `EndOfDayUIManager.cs:108-117 RefreshUI()` —— `num=0; foreach(s) if (s.unlocked && s.isUsing) num += s.cost;`
  然后 `flag = num <= 0 || num <= playerCash`（UI「合计」行与确定按钮可用性）；
* `PlayerStore.cs:1710-1722 EndNight()` —— 同样的 `unlocked && isUsing` 才扣费。

对应实现在 `mod/src/BridgeApi.cs` 的 economy 段（`sb.Append(",\\"economy\\":{")` 到
`economy_error` 之间）：`svcTotal` 求和，`svcAffordable = (sum <= 0 || sum <= ps.playerCash)`。

**核心设计（本文件一半的断言在守它）**：求和异常时两个键都发 **null**，
`service_affordable` **不发 `false`**。因为 `false` 是「钱不够，别买了」这个**结论**，
而实际只是「算不出来」；用一个结论冒充另一个结论，AI 会停止卖货，而它本该做的是去确认。

本文件四组断言
--------------
* T1 观察层透传：`_compact_economy` 必须原样透传，`None` 不得变成 `0` / `False`；
* T2 C# 契约一致性：**按 C# 真源码**核 economy 段是否真发射这两个键（不只查 Python 登记）；
* T3 `end_day_confirm` 的 `INSUFFICIENT_FUNDS` 契约：在**该 case 自己的范围**内断言；
* T4 反向守卫：AST 断言 `_compact_economy` 内不得出现 `or 0` / `or False` 之类归一化。

纪律（台账 §6.1）：凡「源码不得包含 X」的断言一律按**语法结构**判（AST / 大括号配对），
不做裸字符串匹配 —— `observer.py` 与 `BridgeApi.cs` 的注释都**引用了**这些被禁写法来解释
为何禁止，字符串匹配会立刻假红（同一坑已踩过两次）。
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:  # 直接按文件跑时也能 import game_agent
    sys.path.insert(0, str(REPO_ROOT))

from game_agent import contracts as C  # noqa: E402
from game_agent.structured.observer import _compact_economy  # noqa: E402

OBSERVER_SRC = REPO_ROOT / "game_agent" / "structured" / "observer.py"
BRIDGE_API_CS = REPO_ROOT / "mod" / "src" / "BridgeApi.cs"
CONTRACTS_SRC = REPO_ROOT / "game_agent" / "contracts.py"

#: 本次新增的两个服务费键。
SERVICE_KEYS = ("service_total", "service_affordable")


# ---------------------------------------------------------------- C# 切片工具


def _slice_economy_block(src: str) -> str:
    """切出 C# 里 `"economy":{...}` 这一段（从开块到 `economy_error` 兜底之前）。

    用「开块位置 → economy_error 位置」定界，而不是全文搜：`retail_markup` 之类的键
    在 `/risk` 等其它构件里也出现，全文扫会误命中（`scripts/check_contract_tables.py`
    的 `extract()` 也是按 case 范围切片，同理）。
    """
    start_marker = 'sb.Append(",\\"economy\\":{")'
    start = src.find(start_marker)
    assert start >= 0, f"C# 里找不到 economy 开块标记 {start_marker!r}（economy 段被重构了？）"
    # `economy_error` 在源码里是 `,\"economy_error\":` —— 只搜键名本身，不带引号，
    # 免得被 C# 的 \\" 转义写法绊住（第一版就栽在这里）。
    end = src.find("economy_error", start)
    assert end >= 0, "C# 里找不到 economy_error 兜底（economy 段的异常分支被改了？）"
    return src[start:end]


def _match_brace(clean: str, open_at: int) -> int:
    """从 `clean[open_at] == '{'` 起做括号配对，返回匹配 `}` 的下标（失败 -1）。

    与 `scripts/check_contract_tables.py:_match_brace` 同一套做法：
    先抹掉字面量与注释再配对，否则 `"{...}"` 里的花括号会扰乱栈。
    """
    depth = 0
    for i in range(open_at, len(clean)):
        if clean[i] == "{":
            depth += 1
        elif clean[i] == "}":
            depth -= 1
            if depth == 0:
                return i
    return -1


def _slice_case_body(src: str, case_name: str) -> str:
    """切出 `switch (action)` 里某个 case **自己的**语句块（大括号配对，不靠下一个 case 名）。

    不按「到下一个 case 标签为止」切：那样一旦有人调整 case 顺序，切片会静默变宽/变窄。
    这里从 case 标签后第一个 `{` 起做配对，得到的正是该 case 的语句块。
    """
    label = f'case "{case_name}":'
    at = src.find(label)
    assert at >= 0, f"C# 里找不到 {label}（动作被改名或删除了？）"
    open_at = src.find("{", at + len(label))
    assert open_at >= 0, f"{label} 之后找不到语句块起始 '{{'"
    close_at = _match_brace(src, open_at)
    assert close_at >= 0, f"{label} 的大括号不平衡"
    return src[open_at : close_at + 1]


# ---------------------------------------------------------------- T1 观察层透传


def test_service_fields_pass_through_real_values():
    """GAP: 夜间服务费两键有值时必须原样透传（不换算、不取整）。"""
    rendered = _compact_economy(
        {"service_total": 735, "service_affordable": False, "stock_value": 4200}
    )

    assert rendered["service_total"] == 735, (
        f"service_total 必须原样透传，实际 {rendered.get('service_total')!r}"
    )
    assert rendered["service_affordable"] is False, (
        "service_affordable 为 False 时必须原样透传（这是『钱确实不够』的真结论），"
        f"实际 {rendered.get('service_affordable')!r}"
    )


def test_service_fields_none_stays_none():
    """GAP: `None` 必须保持 `None`，**绝不许**变成 `0` / `False`（本次改动的核心语义）。

    C# 求和异常时两键都发 `null`。`None` 是「算不出来」，`0` 是「今晚不用付钱」，
    `False` 是「钱不够，别买了」——三者对模型是三个完全不同的指令：
    看到 `0` 它会放心去打烊，看到 `False` 它会去取消服务，而真相只是「读数坏了，去确认一下」。
    """
    rendered = _compact_economy(
        {"service_total": None, "service_affordable": None}
    )

    assert rendered["service_total"] is None, (
        "service_total 为 None 时必须保持 None，"
        f"实际 {rendered.get('service_total')!r} —— 归一成 0 会让模型误以为『今晚免费』"
    )
    assert rendered["service_affordable"] is None, (
        "service_affordable 为 None 时必须保持 None，"
        f"实际 {rendered.get('service_affordable')!r} —— 归一成 False 会让模型误以为『钱不够』"
    )


def test_service_fields_default_to_none_when_keys_missing():
    """GAP: 老插件（economy 段没有这两个键）→ 透传为 `None`，不得 `KeyError`。

    向后兼容是硬要求：旧版 DLL 仍在用户机器上跑，观察层不能因为少了两个新键就整段炸掉。
    """
    rendered = _compact_economy({"retail_markup": 45, "stock_value": 100})

    assert rendered["service_total"] is None, (
        f"缺键时应为 None，实际 {rendered.get('service_total')!r}"
    )
    assert rendered["service_affordable"] is None, (
        f"缺键时应为 None，实际 {rendered.get('service_affordable')!r}"
    )


def test_service_fields_absent_from_non_dict_economy():
    """GAP: economy 段本身不可用（非 dict）时返回空映射，不得凭空编出服务费键。

    `_compact_economy` 对非 dict 返回 `{}`（不是带 None 的桩）——这是既有契约，
    调用方（`build_observation`）会因此把整个 economy 段按缺失处理，走统一的降级通道，
    而不是让半真半假的 `{"service_total": None, ...}` 混进正常读数。
    """
    for bad in (None, [], "boom", 3):
        rendered = _compact_economy(bad)
        assert rendered == {}, (
            f"非 dict 的 economy（{bad!r}）应返回 {{}}，实际 {rendered!r}"
        )


# ---------------------------------------------------------------- T2 C# 契约一致性


def test_bridge_api_economy_block_emits_service_fields():
    """GAP: `BridgeApi.cs` 的 economy 段必须**真的发射** `service_total` / `service_affordable`。

    **必须按 C# 真源码抽，不能只断言 `contracts.py` 里登记了**：这正是踩过多次的坑
    （`wholesale_cost` 只改生产方不回改消费方、`accept_multiple` 幽灵字段）。
    Python 侧登记只证明「我们以为插件会发」，源码才证明「插件真的发了」。

    断言分两层，缺一不可：
      * **值分支**：`sb.Append(",\\"<key>\\":").Append(...)` —— 真正携带数字/布尔的那句。
        只查键名会把「只剩 null 分支」误判为正常（实测变异：删掉值分支后仅查键名仍绿）。
      * **null 分支**：`sb.Append(",\\"<key>\\":null")` —— 求和失败时的显式降级。
    """
    block = _slice_economy_block(BRIDGE_API_CS.read_text(encoding="utf-8-sig"))

    for key in SERVICE_KEYS:
        value_form = f'\\"{key}\\":").Append('
        assert value_form in block, (
            f"BridgeApi.cs 的 economy 段没有**发值**的 {key!r} 分支"
            f"（找不到 {value_form!r}）。只剩 null 分支的话，"
            "Python 侧会一直读到 None，模型永远看不到今晚服务合计——T02-P0 的链路又断了"
        )
        null_form = f'\\"{key}\\":null'
        assert null_form in block, (
            f"economy 段缺少 {key!r} 的 null 分支（找不到 {null_form!r}）——"
            "求和失败时降级必须显式发 null，不能借道 false/0"
        )


def test_bridge_api_emits_null_not_false_when_sum_fails():
    """GAP: 求和异常时必须发 `null`，`false` 只允许出现在「已知金额」的那条分支上。

    C# 用 `svcTotal = -1` 当「未知」哨兵，随后 `if (svcTotal < 0)` 发 `null`，
    `else` 分支才 `B(svcAffordable)`（初值 `false`）。若有人「简化」成直接发
    `B(svcAffordable)`，「算不出来」就变成了「钱不够」——AI 会去取消服务或停止卖货，
    而它该做的是确认读数。

    这条断言的是**分支形态**，不是「出现过 null」：光有 null 分支不够，
    还必须能证明 `B(svcAffordable)` 与它是 if/else 关系（否则两句都在、但都无条件执行，
    等于永远发 false）。
    """
    block = _slice_economy_block(BRIDGE_API_CS.read_text(encoding="utf-8-sig"))

    # 归一空白后比对整句，避免被缩进/换行差异绊住
    flat = " ".join(block.split())

    assert "svcTotal < 0" in block, (
        "economy 段里找不到 `svcTotal < 0` 的未知判定——"
        "降级分支会变成死代码，求和失败时照旧发 false"
    )
    for key in SERVICE_KEYS:
        null_branch = f'if (svcTotal < 0) sb.Append(",\\"{key}\\":null");'
        assert null_branch in flat, (
            f"找不到「未知时发 null」的分支：`{null_branch}`。"
            "未知必须显式降级成 null，不能借道 false/0"
        )

    known_branch = (
        'else sb.Append(",\\"service_affordable\\":").Append(B(svcAffordable));'
    )
    assert known_branch in flat, (
        f"找不到「已知时发真实可付性」的 else 分支：`{known_branch}`。"
        "若 false 不在 else 里，它会在求和失败时也被发出去，把『算不出来』谎报成『钱不够』"
    )


def test_silent_catch_tables_stay_in_sync():
    """GAP: `SILENT_CATCH_KEYS` / `SILENT_CATCH_PATHS` 必须键集相等、且与自查里的写死数字一致。

    两张表是「静默 catch 会把这些键糊成默认值」的登记册，两者的键集必须严格相等
    （任一表多登记就是漏网）。同时 `contracts.py` 的自查项里写死了
    `len(SILENT_CATCH_KEYS) == N` 与块数 `== M`：这里的断言把它和真实长度钉在一起，
    这样「加了键却忘改自查数字」与「改了自查数字却忘加键」任一方向都会红。
    """
    assert set(C.SILENT_CATCH_KEYS) == set(C.SILENT_CATCH_PATHS), (
        "SILENT_CATCH_KEYS 与 SILENT_CATCH_PATHS 键集不一致：\n"
        f"  仅在 KEYS：{sorted(set(C.SILENT_CATCH_KEYS) - set(C.SILENT_CATCH_PATHS))}\n"
        f"  仅在 PATHS：{sorted(set(C.SILENT_CATCH_PATHS) - set(C.SILENT_CATCH_KEYS))}"
    )
    assert len(C.SILENT_CATCH_KEYS) == len(C.SILENT_CATCH_PATHS)

    src = CONTRACTS_SRC.read_text(encoding="utf-8-sig")
    tree = ast.parse(src)

    def _declared_len(target: str) -> int | None:
        """在 contracts.py 的自查项里找 `target == <int>`，返回那个整数。

        按 `ast.Compare` 判（左操作数是形如 `len(SILENT_CATCH_KEYS)` 的调用），
        **不用正则扫源码**：正则会被注释里的同一句话命中。
        """
        for node in ast.walk(tree):
            if not isinstance(node, ast.Compare):
                continue
            if len(node.ops) != 1 or not isinstance(node.ops[0], ast.Eq):
                continue
            if ast.unparse(node.left) != target:
                continue
            right = node.comparators[0]
            if isinstance(right, ast.Constant) and isinstance(right.value, int):
                return right.value
        return None

    declared_keys = _declared_len("len(SILENT_CATCH_KEYS)")
    declared_blocks = _declared_len("len(set(SILENT_CATCH_KEYS.values()))")

    assert declared_keys is not None, (
        "contracts.py 里找不到 `len(SILENT_CATCH_KEYS) == <N>` 自查项——"
        "登记册失去长度校验，随手多登一个键不会有任何提示"
    )
    assert declared_keys == len(C.SILENT_CATCH_KEYS), (
        f"contracts.py 自查写着 {declared_keys} 键，实际 {len(C.SILENT_CATCH_KEYS)} 键——"
        "加了键没改自查数字（或反之）"
    )
    assert declared_blocks == len(set(C.SILENT_CATCH_KEYS.values())), (
        f"contracts.py 自查写着 {declared_blocks} 个块，实际 "
        f"{len(set(C.SILENT_CATCH_KEYS.values()))} 个——块名被改写或新增块没登记"
    )


def test_service_fields_are_not_registered_as_silent_catch():
    """GAP: 两个服务费键**不得**进 `SILENT_CATCH_KEYS`（它们不是静默 catch 产物）。

    这张表的语义是「用空 `catch { }` 兜底、失败时糊成默认值 `false`/`0`/`-1`、
    下游**完全看不出失败**」的键。而 `service_total`/`service_affordable` 恰恰相反：
    C# 用 `svcTotal = -1` 当哨兵，失败时**显式**发 `null`（见上一组断言），
    下游能明确看出「算不出来」。把显式降级登记进静默 catch 册子，
    会让模式 A 的降级检测把正常读数误报成降级（反之亦然）——两类降级不能混登。
    """
    for key in SERVICE_KEYS:
        assert key not in C.SILENT_CATCH_KEYS, (
            f"{key!r} 被登记进 SILENT_CATCH_KEYS（块 = "
            f"{C.SILENT_CATCH_KEYS.get(key)!r}）。它失败时是**显式 null**，不是静默默认值——"
            "登记进去会让降级成因判断错误"
        )
        assert key not in C.SILENT_CATCH_PATHS, (
            f"{key!r} 被登记进 SILENT_CATCH_PATHS，同 SILENT_CATCH_KEYS 的理由"
        )


# ---------------------------------------------------------------- T3 INSUFFICIENT_FUNDS


def test_end_day_confirm_case_declares_insufficient_funds_contract():
    """GAP: `case "end_day_confirm"` 自己的范围内必须同时出现 `INSUFFICIENT_FUNDS` /
    `service_total` / `cash` 三个字面量。

    **只在该 case 自己的范围里找**：`cash` 这种词全文到处都是，全文扫等于没断言。
    切片用大括号配对（`_slice_case_body`），不按「到下一个 case 为止」。

    为什么必须拦：`EndOfDayUIManager.RefreshUI()` 只做 UI 层拦截
    （`SetInteractable(false)`），桥接直调 `OnEndDayButtonClick()` 绕过了按钮可用性，
    `PlayerStore.EndNight()` 的 `playerCash -= item.cost` 照扣 —— **现金变负数**。
    这是只有走桥接才会发生的状态，玩家手点不可能出现。
    """
    body = _slice_case_body(BRIDGE_API_CS.read_text(encoding="utf-8-sig"), "end_day_confirm")

    for literal in ("INSUFFICIENT_FUNDS", "service_total", "cash"):
        assert literal in body, (
            f"end_day_confirm 分支里缺少 {literal!r}。"
            "现金不足时必须返回可机器判读的结构化错误，而不是照旧成功"
        )


def test_end_day_confirm_error_payload_has_code_and_ok_false():
    """GAP: 该分支返回的 JSON 必须**同时**带 `code` 与 `ok:false`。

    只给中文错误串会让 Python 侧不得不解析中文（`"现金不足" in detail`）——文案一改判定就失效。
    给 `code` 常量才能按码分支；给 `ok:false` 才能让既有的 `execution.ok` 短路生效。
    """
    body = _slice_case_body(BRIDGE_API_CS.read_text(encoding="utf-8-sig"), "end_day_confirm")

    # C# 里这串 JSON 用 \" 转义；两种写法都接受（拼接/整串各一种风格）
    assert '"ok":false' in body or '\\"ok\\":false' in body, (
        "end_day_confirm 的拒绝响应里没有 ok:false —— "
        "调用方会把它当成功，继续走夜晚流程"
    )
    assert '"code":' in body or '\\"code\\":' in body, (
        "end_day_confirm 的拒绝响应里没有 code —— "
        "Python 侧只能靠解析中文错误串判断原因，文案一改就失效"
    )


# ---------------------------------------------------------------- T4 反向守卫


#: 视为「归一化」的兜底字面量：把未知压成确定的 0 / False / 空容器。
_FALSEY_FALLBACKS = (0, False, None)


def _is_falsey_fallback(node: ast.expr) -> bool:
    """判断表达式是否是「把未知压成确定值」的兜底（`0` / `False` / `[]` / `{}`）。

    注意 `None` 也在这里：`x or None` 与直接 `x` 等价，本不该出现——
    出现即说明有人想「兜底」却写了个空操作，容易被后人当成「已处理」。
    """
    if isinstance(node, ast.List) and not node.elts:
        return True
    if isinstance(node, ast.Dict) and not node.keys:
        return True
    if isinstance(node, ast.Constant):
        return node.value in _FALSEY_FALLBACKS
    return False


def _service_key_fallbacks(src: str) -> list[str]:
    """在 `_compact_economy` 体内找「对服务费键做归一化」的写法。

    三种形态都拦（全部按 AST 结构判，不看字符串）：
      * `economy.get("service_total") or 0` —— `BoolOp(Or)` 里出现错误兜底值；
      * `x if x is not None else 0`        —— `IfExp` 的某个分支是错误兜底值；
      * `if not x: return 0`               —— `If(test=Not(...))` 体内 `return 0/False`。

    **不能写成 `assert "or 0" not in src`**：`observer.py` 的注释里**引用了**
    `or 0` / `or False` 来解释为何禁止，裸字符串匹配会立刻假红（同一坑已踩两次）。
    """
    tree = ast.parse(src)
    fn = next(
        (
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == "_compact_economy"
        ),
        None,
    )
    if fn is None:
        return ["找不到 _compact_economy 函数定义（改名了？本断言会失效）"]

    problems: list[str] = []
    for node in ast.walk(fn):
        text = ast.unparse(node)
        mentions = any(key in text for key in SERVICE_KEYS)
        if not mentions:
            continue
        if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or):
            problems.append(
                f"第 {node.lineno} 行：`{text}` —— 用 `or` 给服务费兜底，"
                "会把『算不出来』压成确定的 0/False"
            )
        elif isinstance(node, ast.IfExp) and (
            _is_falsey_fallback(node.body) or _is_falsey_fallback(node.orelse)
        ):
            problems.append(
                f"第 {node.lineno} 行：`{text}` —— 条件表达式里带兜底值，"
                "等于把 None 归一成确定值"
            )
        elif isinstance(node, ast.If) and isinstance(node.test, ast.UnaryOp):
            if isinstance(node.test.op, ast.Not):
                for stmt in node.body:
                    if isinstance(stmt, ast.Return) and stmt.value is not None:
                        if _is_falsey_fallback(stmt.value):
                            problems.append(
                                f"第 {node.lineno} 行：`if not ...: return {ast.unparse(stmt.value)}` "
                                "—— 缺省分支把未知归成确定值"
                            )
    return problems


def test_compact_economy_has_no_falsey_fallback_for_service_fields():
    """GAP: `_compact_economy` 内**不得**对服务费键做 `or 0` / `or False` 式归一化（AST 判定）。

    行为断言（T1）证明**现在**透传正确；这条结构断言证明**写法**不会再被改回去：
    只要有人为了「让下游少判空」而加兜底，`None`（算不出来）就会静默变成
    `0`（今晚免费）或 `False`（钱不够），而所有行为断言都会继续绿——
    因为 mock 里喂的正是 `None`，兜底只在真实链路才现形。
    """
    problems = _service_key_fallbacks(OBSERVER_SRC.read_text(encoding="utf-8-sig"))

    assert not problems, (
        "_compact_economy 里出现了「把服务费归一成确定值」的写法——"
        "None 的语义是『算不出来』，不是 0 也不是 False：\n  " + "\n  ".join(problems)
    )


def test_compact_economy_still_lists_both_service_keys():
    """GAP: 两个服务费键必须仍在 `_compact_economy` 的返回映射里（防被误删）。

    与上一条互补：上一条防「写坏」，这条防「删掉」。键名写错（如 `service_costs`）
    在 Python 侧不会报错——只会一直读到 `None`，模型静默失去这个读数。
    """
    rendered = _compact_economy({"service_total": 1, "service_affordable": True})

    for key in SERVICE_KEYS:
        assert key in rendered, (
            f"_compact_economy 的返回映射里没有 {key!r}（实际键：{sorted(rendered)}）——"
            "模型会静默失去这个读数，且不会有任何报错"
        )
