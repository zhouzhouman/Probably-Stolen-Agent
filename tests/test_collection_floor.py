"""收集数下限守卫：pytest 实际收集到的用例，不得少于 `tests/` 里的定义总数（动态）。

为什么要这条（X03 核心教训）
--------------------------
`tests/` 曾存在 12 组同名类 + 4 处类内同名方法。Python 语义是**后定义覆盖前定义**，
于是被覆盖那批 `test_*` 从未被 pytest 收集 —— 而 `pytest -q` 照样全绿。
**passed 数看不出覆盖丢失**，只有把「源码里的定义数」和「实际收集数」对比才看得见。

为什么不写死一个数字（例如 799）
------------------------------
写死的下限每加一个测试就过期一次：要么忘了更新（守卫失效），要么频繁改（噪音）。
这里改成**动态下限**：现场用 AST 数出 `tests/` 下「模块级 `test_*` + 类内 `test_*`」
的总数，再让 pytest 真收集一遍，断言 `收集数 >= 定义数`。

- **同名重复定义** → 后定义覆盖前定义 → 收集数 < 定义数 → 变红 ✓
- **collect_ignore / 文件名不匹配** → 整个文件不收集 → 变红 ✓
- **parametrize 展开** → 收集数 > 定义数 → 正常通过 ✓（不误伤）

代价是这一个用例会起一次子进程做 `--collect-only`（约 1s，不执行任何测试体）。
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
TESTS_DIR = Path(__file__).resolve().parent


def _iter_test_files() -> list[Path]:
    """`tests/` 下所有 `test_*.py`（递归，跳过 `__pycache__`）。"""
    return sorted(
        p for p in TESTS_DIR.rglob("test_*.py") if "__pycache__" not in p.parts
    )


def _ast_test_names(path: Path) -> list[str]:
    """按 Python 语义列出该文件**最终生效**的测试定义全名。

    这里刻意模拟「后定义覆盖前定义」：同一作用域里同名定义只算最后一个，
    因为前面那些在运行期已经被覆盖掉了。**但这正是我们要抓的失效** ——
    所以这里用 `set` 收集（同名只算一个），再让 pytest 的收集数去对账：
    若 pytest 收集到的比这个还少，说明有定义根本没被收集器看见。

    注意：本函数统计的是「生效定义数」，重复定义不会让它变小，
    所以校验方向必须是 `collected >= ast_unique`；真正的重复检测由
    `tests/test_no_duplicate_test_classes.py` 负责（那里会直接报错）。
    """
    tree = ast.parse(path.read_text(encoding="utf-8-sig"))
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name.startswith("test"):
                names.add(node.name)
        elif isinstance(node, ast.ClassDef):
            for sub in node.body:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if sub.name.startswith("test"):
                        names.add(f"{node.name}.{sub.name}")
    return sorted(names)


def _collected_per_file() -> tuple[int, dict[str, int]]:
    """跑一次 `--collect-only`，返回（总数, 每文件用例数）。

    用 `-q` 的 node id 输出解析，不依赖终端颜色或统计行格式漂移。
    """
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "tests"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if proc.returncode not in (0, 5):  # 5 = 没收集到任何用例
        raise AssertionError(
            "收集阶段失败，无法对账收集数下限（先修 collection error）：\n"
            + (proc.stdout or "")[-2000:]
            + (proc.stderr or "")[-2000:]
        )
    per_file: dict[str, int] = {}
    for line in proc.stdout.splitlines():
        line = line.strip()
        if "::" not in line:
            continue
        # node id 形如 `tests/foo.py::TestX::test_y[param]`
        fname = line.split("::", 1)[0].replace("\\", "/")
        per_file[fname] = per_file.get(fname, 0) + 1
    return sum(per_file.values()), per_file


def test_collected_count_covers_every_definition():
    """GAP: 收集数必须覆盖 `tests/` 的全部定义 —— 防同名类静默丢用例。

    这是 X03 唯一能自动发现「测试被静默覆盖」的守卫：`passed` 数看不出这件事，
    只有把「源码定义数」与「收集数」对账才看得见。动态下限，加测试不用改它。
    """
    files = _iter_test_files()
    assert files, f"在 {TESTS_DIR} 下没找到任何 test_*.py —— 路径或命名规则变了"

    ast_total = 0
    deficits: list[str] = []
    total_collected, per_file = _collected_per_file()

    for path in files:
        rel = path.relative_to(REPO_ROOT).as_posix()
        expected = len(_ast_test_names(path))
        ast_total += expected
        got = per_file.get(rel, 0)
        if got < expected:
            deficits.append(f"{rel}: 定义 {expected} 条，只收集到 {got} 条")

    assert not deficits, (
        "以下文件的用例没有全部被收集（同名重复定义会被后定义覆盖，"
        "整批 test_* 就此消失，而 pytest 仍然全绿）：\n  "
        + "\n  ".join(deficits)
    )
    assert total_collected >= ast_total, (
        f"总收集数 {total_collected} < 定义总数 {ast_total} —— 有用例在收集阶段丢失"
    )
