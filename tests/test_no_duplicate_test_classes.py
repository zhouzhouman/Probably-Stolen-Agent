"""AST 守卫：禁止同名重复定义（X03 测试治理，防「静默丢测试」事故复发）。

事故背景
--------
`tests/` 下曾存在 12 组同名类定义（`TestBusinessGoal` / `TestRuntimeDirLocation` /
`TestMorningReportGuard` / `TestNightPhaseWhitelist` / `TestDialogChoiceEnrichment` /
`TestDeadEndCustomerFilter` / `TestDecideServiceUnavailableBackoff` / `TestWholesaleDirection` /
`TestGoalRebase` / `_ScriptedBrainClient` / `_ServiceDownClient` / `_FakeButtonsClient`）
以及 4 处类内同名方法（`TestAgentLoop.test_mock_never_pushes_overlay`、
`TestRentReserveGuard` 的三个 `test_buy_*` / `test_cash_*`）。

Python 语义是**后定义覆盖前定义**：同一个模块里第二次 `class X:` 会让第一次定义
彻底消失，于是前一份里的 `test_*` 从未被 pytest 收集。表现是「测试全绿」，
但那一整批用例其实不存在——这正是本项目反复踩到的失效模式（测试与代码共谋
同一个过时假设，绿灯是假绿）。

注意：`docs/26 §X03` 曾据字面统计判断「静默丢失约 37 个用例」，实测副本与原件
逐字节相同（只差一处 docstring 的反斜杠转义），被覆盖的那份本来就零覆盖，
所以删除副本后 `--collect-only` 仍是 646——不是丢用例，而是从来就没收集过。
真正的教训是：**重复定义本身必须被机器抓住**，而不是靠人肉数数。

本文件把「不允许同名重复定义」变成可执行断言，覆盖三层：
1. 模块级重复 `class`（含非 `Test*` 的辅助类）；
2. 模块级重复 `def`（含非 `test_*` 的辅助函数，以及 `def test_*` 同名）；
3. 类体内的重复方法名（同名方法同样会被后者覆盖）。

命中即 fail，错误信息给出 `文件:行号: 名称`，便于直接定位。
"""
from __future__ import annotations

import ast
from pathlib import Path
from typing import Any, Iterable

TESTS_DIR = Path(__file__).resolve().parent

# 类体内不算「重复方法」的特殊名：同名 `__init__` 等重复同样是 bug，
# 但 `@property` / `@x.setter` 这类装饰器会让同名多定义变得**合法**
# （getter + setter 本来就成对出现），故按装饰器名放行。
_ALLOWED_DUP_METHOD_DECORATORS = frozenset({"setter", "getter", "deleter"})


def _rel(path: Path, root: Path) -> str:
    """把绝对路径转成相对 tests/ 的 posix 路径（错误信息稳定、可 grep）。"""
    try:
        return path.relative_to(root).as_posix()
    except ValueError:  # pragma: no cover - 防御：root 之外的路径
        return path.as_posix()


def _iter_py_files(root: Path) -> list[Path]:
    """列出 root 下所有参与扫描的 .py 文件（跳过 __pycache__）。"""
    return sorted(
        p for p in root.rglob("*.py")
        if "__pycache__" not in p.parts
    )


def _decorator_names(node: ast.AST) -> set[str]:
    """收集函数/方法的装饰器末端名（`@property` → property，`@x.setter` → setter）。"""
    names: set[str] = set()
    for dec in getattr(node, "decorator_list", []) or []:
        target = dec.func if isinstance(dec, ast.Call) else dec
        if isinstance(target, ast.Attribute):
            names.add(target.attr)
        elif isinstance(target, ast.Name):
            names.add(target.id)
    return names


def _dup_defs(
    nodes: Iterable[Any],
    *,
    kind: str,
    where: str,
    rel: str,
) -> list[str]:
    """在一批**同层**定义节点里找同名重复，返回人类可读的问题描述列表。

    Args:
        nodes: 同一作用域内的定义节点（模块 body / 类 body）。
        kind: 用于报错文案的定义种类（"class" / "def" / "method"）。
        where: 作用域描述（"模块级" 或 "class Foo"）。
        rel: 文件名（相对 tests/）。
    """
    seen: dict[str, int] = {}
    problems: list[str] = []
    for node in nodes:
        name = getattr(node, "name", None)
        if not isinstance(name, str):
            continue  # pragma: no cover - ast 保证有 name，防御性跳过
        if kind == "method" and (
            _decorator_names(node) & _ALLOWED_DUP_METHOD_DECORATORS
        ):
            continue  # @property + @x.setter 成对定义，合法
        first = seen.get(name)
        if first is None:
            seen[name] = node.lineno
            continue
        problems.append(
            f"{rel}:{node.lineno}: 重复{_kind_zh(kind)} `{name}`"
            f"（{where}；首次定义在第 {first} 行，后者会覆盖前者）"
        )
    return problems


def _kind_zh(kind: str) -> str:
    return {"class": "类", "def": "函数", "method": "方法"}.get(kind, "定义")


def _scan_file(path: Path, root: Path) -> dict[str, list[str]]:
    """扫描单个文件，按三个层级返回重复定义问题。"""
    rel = _rel(path, root)
    # utf-8-sig：容忍 BOM（某些编辑器/PowerShell 重定向会写入 BOM，
    # 否则 ast.parse 直接报 invalid non-printable character U+FEFF）。
    tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
    result: dict[str, list[str]] = {"classes": [], "functions": [], "methods": []}
    module_classes: list[Any] = []
    module_funcs: list[Any] = []
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            module_classes.append(node)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            module_funcs.append(node)
    result["classes"] = _dup_defs(
        module_classes, kind="class", where="模块级", rel=rel,
    )
    result["functions"] = _dup_defs(
        module_funcs, kind="def", where="模块级", rel=rel,
    )
    for cls in module_classes:
        methods = [
            n for n in cls.body
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        ]
        result["methods"].extend(
            _dup_defs(methods, kind="method", where=f"class {cls.name}", rel=rel)
        )
    return result


def scan_test_tree(root: Path = TESTS_DIR) -> dict[str, list[str]]:
    """扫描整个 tests/ 目录，聚合三层重复定义问题（供测试与自测共用）。"""
    agg: dict[str, list[str]] = {"classes": [], "functions": [], "methods": []}
    for path in _iter_py_files(root):
        try:
            found = _scan_file(path, root)
        except SyntaxError as exc:  # pragma: no cover - 语法错由 pytest 自己报
            agg["functions"].append(f"{_rel(path, root)}: 语法错误 {exc}")
            continue
        for key, items in found.items():
            agg[key].extend(items)
    return agg


def _format(problems: list[str]) -> str:
    return "\n".join(f"  - {p}" for p in problems)


# ================================================================ 1. 模块级重复类


def test_no_duplicate_class_definitions():
    """GUARD: 同一模块内不允许出现同名 class（后定义会覆盖前定义）。

    X03 实测曾有 12 组：`TestBusinessGoal`×2、`TestRuntimeDirLocation`×2 等，
    被覆盖那份里的 `test_*` 从未被 pytest 收集。
    """
    dups = scan_test_tree()["classes"]
    assert not dups, (
        "tests/ 下存在同名重复类定义（后者覆盖前者，前者的 test_* 会静默丢失）：\n"
        + _format(dups)
    )


# ================================================================ 2. 模块级重复函数


def test_no_duplicate_module_level_functions():
    """GUARD: 同一模块内不允许出现同名模块级 def（含 `def test_*` 同名）。

    模块级 `def test_x` 重命名时若复制粘贴，后一个会覆盖前一个，
    pytest 只按最终属性收集——同样静默丢用例。
    """
    dups = scan_test_tree()["functions"]
    assert not dups, (
        "tests/ 下存在同名重复模块级函数（后者覆盖前者，前者的 test_* 会静默丢失）：\n"
        + _format(dups)
    )


# ================================================================ 3. 类内重复方法


def test_no_duplicate_methods_in_classes():
    """GUARD: 同一个类的 body 内不允许出现同名方法。

    X03 实测曾有 4 处：`TestAgentLoop.test_mock_never_pushes_overlay`、
    `TestRentReserveGuard.test_buy_item_blocked_when_it_would_break_rent` 等。
    （`@property` + `@x.setter` 这种成对定义不算，已放行。）
    """
    dups = scan_test_tree()["methods"]
    assert not dups, (
        "tests/ 下存在类内同名重复方法（后者覆盖前者）：\n" + _format(dups)
    )


# ================================================================ 4. 扫描器自测


def test_scanner_detects_synthetic_duplicates(tmp_path):
    """GUARD: 自测——扫描器必须真的能红，否则上面三个断言是空转。

    造一个含三层重复（类 / 模块级函数 / 类内方法）的临时文件，
    断言扫描器全部报出来，且信息里带 `文件:行号`。
    """
    sample = tmp_path / "test_synthetic_dup.py"
    sample.write_text(
        "class DupClass:\n"
        "    def test_a(self):\n"
        "        assert True\n"
        "    def test_a(self):\n"
        "        assert True\n"
        "class DupClass:\n"
        "    def test_b(self):\n"
        "        assert True\n"
        "def test_dup_func():\n"
        "    assert True\n"
        "def test_dup_func():\n"
        "    assert True\n",
        encoding="utf-8",
    )
    found = scan_test_tree(tmp_path)
    assert len(found["classes"]) == 1, found
    assert len(found["methods"]) == 1, found
    assert len(found["functions"]) == 1, found
    joined = " ".join(
        found["classes"] + found["methods"] + found["functions"]
    )
    # 报错必须带行号，否则无法定位
    assert "test_synthetic_dup.py:" in joined, joined
    assert "DupClass" in joined
    assert "test_dup_func" in joined


def test_scanner_allows_property_setter_pairs(tmp_path):
    """GUARD: `@property` + `@x.setter` 成对定义是合法写法，不得误报。"""
    sample = tmp_path / "test_synthetic_ok.py"
    sample.write_text(
        "class WithProperty:\n"
        "    @property\n"
        "    def value(self):\n"
        "        return 1\n"
        "    @value.setter\n"
        "    def value(self, new):\n"
        "        pass\n",
        encoding="utf-8",
    )
    assert scan_test_tree(tmp_path)["methods"] == []
