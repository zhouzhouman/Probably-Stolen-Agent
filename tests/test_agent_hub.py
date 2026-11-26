r"""agent_hub 的命令拆解。

为什么单独测这个：Windows 路径常含空格（`C:/Program Files/...`），
拆错就变成"找不到可执行文件 C:\Program"。而 hub 是唯一能真正拉起 agent 的地方，
它拆错等于按钮彻底失效。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from agent_hub import split_command  # noqa: E402


class TestSplitCommand:
    def test_quoted_exe_with_spaces(self):
        argv = split_command('"C:\\Program Files\\Py\\python.exe" -m game_agent.cli run')
        assert argv[0] == "C:\\Program Files\\Py\\python.exe"
        assert argv[1:] == ["-m", "game_agent.cli", "run"]

    def test_bare_exe(self):
        argv = split_command("python -m game_agent.cli step")
        assert argv == ["python", "-m", "game_agent.cli", "step"]

    def test_exe_only(self):
        assert split_command("python") == ["python"]

    def test_empty(self):
        assert split_command("") == []
        assert split_command("   ") == []

    def test_unbalanced_quote_returns_empty(self):
        """引号没闭合属于配置写坏了——宁可返回空让上层报错，也不要瞎猜路径。"""
        assert split_command('"C:\\broken') == []

    def test_flags_with_equals(self):
        argv = split_command('"C:\\py\\python.exe" -m game_agent.cli run --max-steps 5')
        assert argv[-2:] == ["--max-steps", "5"]


class TestStepLimit:
    """max_steps 归一：0 / None / 负数 = 不设上限（长局不必手动重启）。"""

    def test_positive_passthrough(self):
        from game_agent.agent_loop import _step_limit

        assert _step_limit(10) == 10
        assert _step_limit(200) == 200

    def test_zero_means_unlimited(self):
        from game_agent.agent_loop import _step_limit

        assert _step_limit(0) > 10 ** 6
        assert _step_limit(None) > 10 ** 6
        assert _step_limit(-5) > 10 ** 6

    def test_garbage_falls_back_to_unlimited(self):
        from game_agent.agent_loop import _step_limit

        assert _step_limit("abc") > 10 ** 6
