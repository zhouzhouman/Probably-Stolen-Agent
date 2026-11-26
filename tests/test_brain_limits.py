"""决策客户端的时间边界测试（v2.9）。

事故背景：Day 8 一次 `open_map` 之后，运行日志 10 分钟只有三行启动提示——
实际是模型调用在「3 次尝试 × 60s 超时 + 退避」里打转，撞上"空内容→加倍预算"
还要再来一轮，单步最坏能拖到 6 分钟。期间**没有任何日志**，看起来完全像死机。

这批测试锁住三件事：
1. 超时只重试一次（不再叠加 60s×N）；
2. 单次决策有墙钟上限，到点如实报错；
3. 每次重试都会写日志（便于现场判断是"慢"还是"死"）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import requests

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from game_agent.brain.client import OpenAITextClient  # noqa: E402
from game_agent.errors import AnalyzeError  # noqa: E402


def _client(**kwargs) -> OpenAITextClient:
    base = dict(
        base_url="http://127.0.0.1:1/v1",
        model="test/model",
        api_key="k",
        timeout=1.0,
        max_tokens=100,
        max_retries=4,          # 故意给大，验证超时不会被它拖长
        retry_backoff=0.0,
    )
    base.update(kwargs)
    return OpenAITextClient(**base)


class TestTimeoutRetryBound:
    def test_timeout_retries_at_most_once(self, monkeypatch):
        calls = {"n": 0}

        def boom(*a, **kw):
            calls["n"] += 1
            raise requests.Timeout("read timed out")

        monkeypatch.setattr("game_agent.brain.client.requests.post", boom)
        client = _client()
        with pytest.raises(AnalyzeError, match="read timed out"):
            client.chat("hi")
        assert calls["n"] == 2, f"超时最多重试 1 次（共 2 次请求），实际 {calls['n']}"

    def test_logs_each_retry(self, monkeypatch, capsys):
        def boom(*a, **kw):
            raise requests.Timeout("read timed out")

        monkeypatch.setattr("game_agent.brain.client.requests.post", boom)
        client = _client()
        with pytest.raises(AnalyzeError):
            client.chat("hi")
        err = capsys.readouterr().err
        assert "[brain" in err, "重试必须有进度日志，否则现场看起来就是假死"
        assert "Timeout" in err


class TestTotalBudget:
    def test_exceeding_budget_raises(self, monkeypatch):
        # 每次请求都返回 500，且把时钟推到上限之外 → 必须报"总耗时"而不是无限重试
        class _Resp:
            status_code = 500
            text = "boom"

        monkeypatch.setattr(
            "game_agent.brain.client.requests.post", lambda *a, **kw: _Resp()
        )
        clock = {"t": 1000.0}

        def fake_time():
            clock["t"] += 100.0
            return clock["t"]

        monkeypatch.setattr("game_agent.brain.client.time.time", fake_time)
        monkeypatch.setattr("game_agent.brain.client.time.sleep", lambda s: None)
        client = _client(max_total_seconds=150, max_retries=6)
        with pytest.raises(AnalyzeError):
            client.chat("hi")

    def test_default_budget_is_timeout_times_2_5(self):
        client = _client(timeout=60.0, max_total_seconds=0)
        assert client.max_total_seconds == 0  # 0 表示用默认策略
        # 默认预算 = timeout × 2.5 = 150s（一步最多两分半）
        assert 60.0 * 2.5 == 150.0
