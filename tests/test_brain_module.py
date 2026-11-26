"""决策模块与 Agent 主循环的单元测试（全离线，不调用外部 API）。"""
import copy
import json

import pytest

from game_agent import agent_loop
from game_agent.brain.brain import decide
from game_agent.brain.client import MOCK_DECISION, MockBrainClient, make_brain_client
from game_agent.brain.prompt import build_decision_prompt
from game_agent.brain.schema import validate_decision
from game_agent.config import DEFAULT_CONFIG
from game_agent.errors import AnalyzeError
from game_agent.vision.models import WindowInfo


def _valid_decision(**overrides):
    payload = {
        "action": "click",
        "params": {"x": 100, "y": 200},
        "reason": "点击继续按钮",
        "confidence": 0.8,
    }
    payload.update(overrides)
    return payload


def _fake_observation():
    return {
        "screen_state": "main_menu",
        "available_actions": ["新游戏"],
        "important_objects": [
            {"name": "新游戏 按钮", "type": "ui", "value": None, "position": "center", "point": [100, 200]}
        ],
        "confidence": 0.9,
        "visible_info": None,
    }


# ---------------------------------------------------------------- 决策契约校验


class TestValidateDecision:
    def test_valid(self):
        out = validate_decision(_valid_decision())
        assert out["action"] == "click"
        assert out["params"] == {"x": 100, "y": 200}
        assert out["confidence"] == 0.8

    def test_missing_required_key_raises(self):
        """v0.9.13：action 仍硬性必填（reason/confidence 已降级为可选容错，
        见 TestDecisionFieldTolerance——r49 实测 GLM 常缺这两个字段）。"""
        with pytest.raises(ValueError, match="缺少必需字段"):
            validate_decision({"confidence": 0.5, "reason": "r"})

    def test_unknown_action_rejected(self):
        with pytest.raises(ValueError, match="白名单"):
            validate_decision(_valid_decision(action="screenshot"))

    def test_coordinates_normalized(self):
        out = validate_decision(_valid_decision(params={"x": "100.6", "y": 200.4}))
        assert out["params"] == {"x": 101, "y": 200}

    def test_negative_coordinates_rejected(self):
        with pytest.raises(ValueError, match="不能为负数"):
            validate_decision(_valid_decision(params={"x": -5, "y": 10}))

    def test_missing_click_coordinate_rejected(self):
        with pytest.raises(ValueError, match="缺少坐标参数"):
            validate_decision(_valid_decision(params={"x": 10}))

    def test_press_key_normalized(self):
        out = validate_decision(_valid_decision(action="press_key", params={"key": "ESC", "times": 99}))
        assert out["params"] == {"key": "esc", "times": 10}

    def test_wait_clamped(self):
        assert validate_decision(_valid_decision(action="wait", params={"seconds": 100}))["params"]["seconds"] == 8.0
        assert validate_decision(_valid_decision(action="wait", params={"seconds": -5}))["params"]["seconds"] == 0.1

    def test_confidence_clamped(self):
        assert validate_decision(_valid_decision(confidence=3))["confidence"] == 1.0
        # v0.9.13：非法 confidence 按中性值 0.5 容错（原 0.0 会被下游当作明确看空）
        assert validate_decision(_valid_decision(confidence="abc"))["confidence"] == 0.5

    def test_unknown_params_dropped(self):
        out = validate_decision(_valid_decision(params={"x": 1, "y": 2, "foo": "bar"}))
        assert out["params"] == {"x": 1, "y": 2}

    def test_type_text_empty_rejected(self):
        with pytest.raises(ValueError, match="text"):
            validate_decision(_valid_decision(action="type_text", params={"text": ""}))

    def test_stop_has_no_params(self):
        out = validate_decision(_valid_decision(action="stop", params={}))
        assert out["action"] == "stop"
        assert out["params"] == {}


# ---------------------------------------------------------------- 决策流水线


class _CapturingBrainClient:
    """捕获提示词的假决策客户端（验证提示词注入内容用）。"""

    name = "capture"

    def __init__(self):
        self.prompts: list[str] = []

    def chat(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return json.dumps(_valid_decision(), ensure_ascii=False)


class TestBrainPipeline:
    def test_mock_client_sample_is_valid(self):
        raw = MockBrainClient().chat("prompt")
        decision = validate_decision(json.loads(raw))
        assert decision["action"] == MOCK_DECISION["action"]

    def test_decide_with_mock(self):
        config = copy.deepcopy(DEFAULT_CONFIG)
        decision, meta = decide(config, _fake_observation(), image_size=(1942, 1136), mock=True)
        assert decision["action"] == "click"
        assert meta["provider"] == "mock"
        assert meta["attempts"] == 1

    def test_decide_with_history(self):
        config = copy.deepcopy(DEFAULT_CONFIG)
        history = [
            {"step": 1, "action": "click", "params": {"x": 1, "y": 2}, "reason": "r",
             "result": "ok", "screen_state": "main_menu"},
        ]
        decision, _ = decide(config, _fake_observation(), history=history, mock=True)
        assert decision["action"] in {"click", "move", "press_key", "type_text", "wait", "stop"}

    def test_make_brain_client_unconfigured_raises(self):
        config = copy.deepcopy(DEFAULT_CONFIG)
        config["brain"]["provider"] = "openai_compat"
        config["brain"]["base_url"] = ""
        config["brain"]["model"] = ""
        with pytest.raises(AnalyzeError, match="尚未配置决策模型"):
            make_brain_client(config)

    def test_prompt_contains_action_whitelist_and_coordinates(self):
        prompt = build_decision_prompt(_fake_observation(), (1942, 1136))
        for token in ("click", "press_key", "type_text", "stop", "坐标", "1942 x 1136"):
            assert token in prompt


class TestDecideBridgeActionsGate:
    """回归（T1）：桥动作注入完全由调用方显式控制，提示词不得偷跑。"""

    def test_decide_statewatch_without_bridge_actions_excludes_tools(self):
        config = copy.deepcopy(DEFAULT_CONFIG)
        client = _CapturingBrainClient()
        decision, meta = decide(
            config,
            {**_fake_observation(), "source": "statewatch"},
            client=client,
            bridge_actions=False,
        )
        assert decision["action"] == "click"
        assert meta["bridge_actions"] is False
        assert "游戏 API 直调动作" not in client.prompts[0]

    def test_decide_bridge_source_with_bridge_actions_includes_tools(self):
        config = copy.deepcopy(DEFAULT_CONFIG)
        client = _CapturingBrainClient()
        decide(
            config,
            {**_fake_observation(), "source": "bridge"},
            client=client,
            bridge_actions=True,
        )
        assert "游戏 API 直调动作" in client.prompts[0]


class _RateLimitedClient:
    """先 N 次抛限流异常，之后返回合法决策的假客户端。"""

    name = "rate_limited"

    def __init__(self, fail_times: int):
        self.fail_times = fail_times
        self.calls = 0

    def chat(self, prompt: str) -> str:
        self.calls += 1
        if self.calls <= self.fail_times:
            raise AnalyzeError("决策模型返回 HTTP 429：{\"error\":{\"code\":\"1305\"}}")
        return json.dumps(_valid_decision(), ensure_ascii=False)


class TestDecideRateLimitBackoff:
    """限流退避：429 不该立刻冒泡打断主循环，且退避不消耗解析纠错预算。"""

    def test_retries_after_rate_limit(self, monkeypatch):
        monkeypatch.setattr("game_agent.brain.brain.time.sleep", lambda s: None)
        config = copy.deepcopy(DEFAULT_CONFIG)
        client = _RateLimitedClient(fail_times=2)
        decision, meta = decide(config, _fake_observation(), client=client, mock=True)
        assert decision["action"] == "click"
        assert client.calls == 3  # 2 次限流 + 1 次成功
        assert meta["rate_limit_waits"] == 2

    def test_gives_up_after_max_retries(self, monkeypatch):
        monkeypatch.setattr("game_agent.brain.brain.time.sleep", lambda s: None)
        monkeypatch.setattr("game_agent.brain.brain.RATE_LIMIT_MAX_RETRIES", 2)
        config = copy.deepcopy(DEFAULT_CONFIG)
        client = _RateLimitedClient(fail_times=99)
        with pytest.raises(AnalyzeError, match="429"):
            decide(config, _fake_observation(), client=client, mock=True)
        assert client.calls == 3  # 2 次退避重试后放弃

    def test_non_rate_limit_error_raises_immediately(self, monkeypatch):
        """非限流错误（如 401）不做退避，直接抛出。"""
        monkeypatch.setattr("game_agent.brain.brain.time.sleep", lambda s: None)
        config = copy.deepcopy(DEFAULT_CONFIG)

        class _AuthFailClient:
            name = "auth_fail"

            def __init__(self):
                self.calls = 0

            def chat(self, prompt: str) -> str:
                self.calls += 1
                raise AnalyzeError("决策模型返回 HTTP 401：unauthorized")

        client = _AuthFailClient()
        with pytest.raises(AnalyzeError, match="401"):
            decide(config, _fake_observation(), client=client, mock=True)
        assert client.calls == 1


# ---------------------------------------------------------------- 主循环


class TestAgentLoop:
    def test_step_once_override_dry_run(self, tmp_path):
        config = copy.deepcopy(DEFAULT_CONFIG)
        config["paths"]["artifacts_dir"] = str(tmp_path / "art")
        fake_window = WindowInfo(hwnd=1, title="fake", rect=(0, 0, 2000, 1200))
        obs_record = {
            "observation": _fake_observation(),
            "capture": {"image_size": [1942, 1136]},
            "screenshot": None,
        }
        record = agent_loop.step_once(
            config=config, mock=True, live=False,
            window=fake_window, obs_override=obs_record,
        )
        assert record["decision"]["action"] == "click"
        assert record["execution"]["executed"] is False  # dry-run
        assert "step_file" in record

    def test_mock_never_pushes_overlay(self, tmp_path, monkeypatch):
        """mock 决策绝不能推到真机浮层（2026-10-16 事故：浮层被刷成假数据冻了 9 天）。

        用户 10-26 截图反馈「经常看到 AI 在做这个决策，但无效」——
        面板上的「（Mock）顾客报价合适，点击收购按钮」其实是测试用例的样例，
        游戏里从来没有这条命令。根因：mock 运行时 push_overlay 默认 True。
        """
        config = copy.deepcopy(DEFAULT_CONFIG)
        config["paths"]["artifacts_dir"] = str(tmp_path / "art_mock_push")
        pushed = []
        monkeypatch.setattr(
            agent_loop, "push_decision_overlay",
            lambda *a, **k: pushed.append(a) or True,
        )
        agent_loop.step_once(
            config=config, mock=True, live=False,
            window=WindowInfo(hwnd=1, title="fake", rect=(0, 0, 2000, 1200)),
            obs_override={
                "observation": _fake_observation(),
                "capture": {"image_size": [1942, 1136]},
                "screenshot": None,
            },
        )
        assert pushed == [], "mock 运行不得向浮层推送任何决策"


    def test_run_loop_stop_file(self, tmp_path):
        config = copy.deepcopy(DEFAULT_CONFIG)
        config["paths"]["artifacts_dir"] = str(tmp_path / "art2")
        (tmp_path / "art2").mkdir(parents=True, exist_ok=True)
        (tmp_path / "art2" / "STOP").write_text("stop", encoding="utf-8")
        steps = agent_loop.run_loop(
            config=config, max_steps=3, mock=True, live=False,
            # 绝不推浮层：mock 决策推到真机浮层会把玩家的展示面板刷成假数据
            #（2026-10-16 起一直如此，用户 10-26 截图反馈「经常看到这个无效决策」）
            push_overlay=False,
        )
        assert len(steps) == 1
        assert steps[0].get("stopped") is True

    def test_run_loop_dry_run_with_override_window(self, tmp_path):
        """两步 dry-run 循环：mock 决策为 click，执行不产生真实输入。"""
        config = copy.deepcopy(DEFAULT_CONFIG)
        config["paths"]["artifacts_dir"] = str(tmp_path / "art3")
        fake_window = WindowInfo(hwnd=1, title="fake", rect=(0, 0, 2000, 1200))

        # 直接测 execute_decision 的 dry-run 行为（不经过截图）
        decision = validate_decision(_valid_decision())
        result = agent_loop.execute_decision(config, decision, live=False, window=fake_window)
        assert result.executed is False
        assert result.ok is True


class TestNoMouseGuard:
    """禁鼠标模式（--no-mouse）：live 模式也绝不产生真实鼠标键盘输入。"""

    def test_click_blocked_when_mouse_disallowed(self):
        config = copy.deepcopy(DEFAULT_CONFIG)
        fake_window = WindowInfo(hwnd=1, title="fake", rect=(0, 0, 2000, 1200))
        result = agent_loop.execute_decision(
            config, _valid_decision(), live=True, window=fake_window, allow_mouse=False
        )
        assert result.executed is False
        assert result.ok is False
        assert "禁鼠标" in result.detail

    @pytest.mark.parametrize(
        "decision",
        [
            _valid_decision(action="press_key", params={"key": "escape"}),
            _valid_decision(action="type_text", params={"text": "abc"}),
            _valid_decision(action="ctrl_click", params={"x": 10, "y": 20}),
            _valid_decision(action="move", params={"x": 10, "y": 20}),
        ],
    )
    def test_all_mouse_keyboard_actions_blocked(self, decision):
        config = copy.deepcopy(DEFAULT_CONFIG)
        fake_window = WindowInfo(hwnd=1, title="fake", rect=(0, 0, 2000, 1200))
        result = agent_loop.execute_decision(
            config, decision, live=True, window=fake_window, allow_mouse=False
        )
        assert result.executed is False
        assert result.ok is False

    def test_wait_and_stop_still_allowed(self):
        """非输入类动作不受禁鼠标影响（等待/收工仍应正常放行）。"""
        config = copy.deepcopy(DEFAULT_CONFIG)
        wait_result = agent_loop.execute_decision(
            config, validate_decision(
                {"action": "wait", "params": {"seconds": 0.1}, "reason": "x", "confidence": 1.0}
            ),
            live=True, allow_mouse=False,
        )
        assert wait_result.executed is True
        assert wait_result.ok is True

        stop_result = agent_loop.execute_decision(
            config, validate_decision(
                {"action": "stop", "reason": "x", "confidence": 1.0}
            ),
            live=True, allow_mouse=False,
        )
        assert stop_result.ok is True

    def test_bridge_action_still_allowed(self):
        """直调动作照常执行——禁鼠标只拦输入，不拦桥接。"""
        config = copy.deepcopy(DEFAULT_CONFIG)
        client = _RecordingBridgeClient()
        result = agent_loop.execute_decision(
            config,
            {"action": "call_next", "params": {}, "reason": "叫号", "confidence": 0.9},
            live=True,
            bridge_client=client,
            allow_mouse=False,
        )
        assert result.executed is True
        assert result.ok is True
        assert client.calls == [("call_next", {})]

    def test_mouse_allowed_by_default(self):
        """默认 allow_mouse=True：向后兼容，dry-run 行为不变。"""
        config = copy.deepcopy(DEFAULT_CONFIG)
        fake_window = WindowInfo(hwnd=1, title="fake", rect=(0, 0, 2000, 1200))
        result = agent_loop.execute_decision(
            config, _valid_decision(), live=False, window=fake_window
        )
        assert result.ok is True


class _RecordingBridgeClient:
    """记录 post_action 调用的假桥客户端。"""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    def post_action(self, action: str, **params):
        self.calls.append((action, params))
        return {"ok": True, "detail": "mock ok"}


class _ScriptedBrainClient:
    """按脚本返回固定决策 JSON 的假客户端（验证白名单过滤用）。"""

    name = "scripted"

    def __init__(self, decision: dict):
        self._raw = json.dumps(decision, ensure_ascii=False)
        self.prompts: list[str] = []

    def chat(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self._raw


class TestDeadEndCustomerFilter:
    """v1.1 死局硬过滤：无可卖+买不起 → 只留送客/打烊（r61 模型从不送客催生）。"""

    def _obs(self):
        return {
            "source": "bridge", "screen_state": "negotiation",
            "available_actions": [], "important_objects": [], "confidence": 1.0,
            "visible_info": {
                "client": {"identifier": "peatMinorClient", "is_selling": True,
                           "is_buying": False, "buying_tags": []},
                "sellable_now": [],
                "money": 91,
                "rent": {"rent_value": 210, "day_until_rent": 2},
                "inventory": {"owned": [], "not_owned": [{"identifier": "x", "value": 16}]},
            },
        }

    def test_call_next_blocked_in_dead_end(self):
        config = copy.deepcopy(DEFAULT_CONFIG)
        client = _ScriptedBrainClient(
            {"action": "call_next", "params": {}, "reason": "叫号", "confidence": 0.9}
        )
        with pytest.raises(AnalyzeError, match="无法解析"):
            decide(config, self._obs(), client=client, bridge_actions=True, no_mouse=True)

    def test_dismiss_allowed_in_dead_end(self):
        config = copy.deepcopy(DEFAULT_CONFIG)
        client = _ScriptedBrainClient(
            {"action": "dismiss_client", "params": {}, "reason": "送客", "confidence": 0.9}
        )
        decision, _meta = decide(config, self._obs(), client=client,
                                 bridge_actions=True, no_mouse=True)
        assert decision["action"] == "dismiss_client"

    def test_end_day_allowed_in_dead_end(self):
        config = copy.deepcopy(DEFAULT_CONFIG)
        client = _ScriptedBrainClient(
            {"action": "end_day", "params": {}, "reason": "打烊", "confidence": 0.9}
        )
        decision, _meta = decide(config, self._obs(), client=client,
                                 bridge_actions=True, no_mouse=True)
        assert decision["action"] == "end_day"


class TestSellableNowFactFilter:
    """v1.1 事实过滤：sellable_now 为空 → sell_item 从白名单移除（r60 空转 ×7 催生）。"""

    OBS_BASE = {
        "source": "bridge", "screen_state": "x",
        "available_actions": [], "important_objects": [], "confidence": 1.0,
    }

    def _decide(self, sellable_now, night_phase=False):
        config = copy.deepcopy(DEFAULT_CONFIG)
        client = _ScriptedBrainClient(
            {"action": "dismiss_client", "params": {}, "reason": "换人", "confidence": 0.9}
        )
        obs = {**self.OBS_BASE, "visible_info": {"sellable_now": sellable_now}}
        decision, _meta = decide(
            config, obs, client=client, bridge_actions=True, no_mouse=True,
            night_phase=night_phase,
        )
        return decision, client

    def test_empty_sellable_blocks_sell_item(self):
        """清单为空 → 模型选 sell_item 会被校验层拒绝（白名单已剔除）。"""
        config = copy.deepcopy(DEFAULT_CONFIG)
        client = _ScriptedBrainClient(
            {"action": "sell_item", "params": {"identifier": "wire"},
             "reason": "卖", "confidence": 0.9}
        )
        obs = {**self.OBS_BASE, "visible_info": {"sellable_now": []}}
        with pytest.raises(AnalyzeError, match="无法解析"):
            decide(config, obs, client=client, bridge_actions=True,
                   no_mouse=True)

    def test_nonempty_sellable_allows_sell_item(self):
        decision, _ = self._decide([{"identifier": "bandage"}])
        assert decision["action"] == "dismiss_client"  # 脚本决策正常通过

    def test_missing_field_no_filter(self):
        """观察缺 sellable_now（vision 通道）→ 不过滤，向后兼容。"""
        config = copy.deepcopy(DEFAULT_CONFIG)
        client = _ScriptedBrainClient(
            {"action": "sell_item", "params": {"identifier": "wire"},
             "reason": "卖", "confidence": 0.9}
        )
        decision, _meta = decide(config, self.OBS_BASE, client=client,
                                 bridge_actions=True, no_mouse=True)
        assert decision["action"] == "sell_item"


class TestNightPhaseWhitelist:
    """v0.9.15：end_day 后（night_phase=True）营业动作被白名单拒绝。"""

    OBS = {"source": "bridge", "screen_state": "x",
           "available_actions": [], "important_objects": [], "confidence": 1.0}

    def _decide(self, decision: dict, *, night_phase: bool, max_attempts: int = 1):
        config = copy.deepcopy(DEFAULT_CONFIG)
        client = _ScriptedBrainClient(decision)
        decision, _meta = decide(
            config, self.OBS, client=client, bridge_actions=True, no_mouse=True,
            night_phase=night_phase, max_attempts=max_attempts,
        )
        return decision, client

    def test_day_action_rejected_in_night_phase(self):
        """r53 病理复发防护：夜晚阶段选 open_shutter → 校验层拒绝。"""
        with pytest.raises(AnalyzeError, match="无法解析"):
            self._decide(
                {"action": "open_shutter", "params": {}, "reason": "r", "confidence": 0.9},
                night_phase=True,
            )

    def test_night_action_accepted_in_night_phase(self):
        decision, _ = self._decide(
            {"action": "stay_home", "params": {}, "reason": "睡觉", "confidence": 0.9},
            night_phase=True,
        )
        assert decision["action"] == "stay_home"

    def test_day_action_accepted_in_day_phase(self):
        """白天阶段不受影响——营业动作正常放行。"""
        decision, _ = self._decide(
            {"action": "open_shutter", "params": {}, "reason": "开门", "confidence": 0.9},
            night_phase=False,
        )
        assert decision["action"] == "open_shutter"

    def test_wait_stop_always_allowed_in_night_phase(self):
        """wait/stop 是基础白名单动作，夜晚阶段仍可用（防模型无步可走卡死）。"""
        for action in ("wait", "stop"):
            decision, _ = self._decide(
                {"action": action, "params": {}, "reason": "r", "confidence": 0.9},
                night_phase=True,
            )
            assert decision["action"] == action

    def test_night_phase_prompt_banner_sent(self):
        """夜晚阶段的提示词必须带横幅（模型可见的引导）。"""
        _, client = self._decide(
            {"action": "stay_home", "params": {}, "reason": "睡觉", "confidence": 0.9},
            night_phase=True,
        )
        assert "夜晚阶段" in client.prompts[0]


class TestBrainClientRetry:
    """v1.3 模型客户端重试退避（r68 实测 25 步里 9 步被网关抖动耗掉）。"""

    def _client(self):
        from game_agent.brain.client import OpenAITextClient
        return OpenAITextClient(
            base_url="http://example.test/v1", model="m", api_key="k",
            timeout=5, max_tokens=100, max_retries=2, retry_backoff=0.0,
        )

    class _Resp:
        def __init__(self, status=200, content='{"action":"wait"}', finish="stop"):
            self.status_code = status
            self._content = content
            self._finish = finish

        def json(self):
            return {"choices": [{"message": {"content": self._content},
                                 "finish_reason": self._finish}]}

        @property
        def text(self):
            return "body"

    def test_retries_on_connection_error(self, monkeypatch):
        import game_agent.brain.client as mod
        calls = {"n": 0}

        def fake_post(*a, **k):
            calls["n"] += 1
            if calls["n"] < 3:
                raise mod.requests.ConnectionError("boom")
            return self._Resp()

        monkeypatch.setattr(mod.requests, "post", fake_post)
        assert self._client().chat("hi") == '{"action":"wait"}'
        assert calls["n"] == 3

    def test_raises_after_retries_exhausted(self, monkeypatch):
        import game_agent.brain.client as mod

        def fake_post(*a, **k):
            raise mod.requests.Timeout("timeout")

        monkeypatch.setattr(mod.requests, "post", fake_post)
        with pytest.raises(AnalyzeError, match="请求决策模型失败"):
            self._client().chat("hi")

    def test_retries_on_500(self, monkeypatch):
        import game_agent.brain.client as mod
        calls = {"n": 0}

        def fake_post(*a, **k):
            calls["n"] += 1
            if calls["n"] == 1:
                return self._Resp(status=500)
            return self._Resp()

        monkeypatch.setattr(mod.requests, "post", fake_post)
        assert self._client().chat("hi") == '{"action":"wait"}'
        assert calls["n"] == 2

    def test_401_not_retried(self, monkeypatch):
        import game_agent.brain.client as mod
        calls = {"n": 0}

        def fake_post(*a, **k):
            calls["n"] += 1
            return self._Resp(status=401)

        monkeypatch.setattr(mod.requests, "post", fake_post)
        with pytest.raises(AnalyzeError, match="HTTP 401"):
            self._client().chat("hi")
        assert calls["n"] == 1

    def test_escalates_max_tokens_on_length_truncation(self, monkeypatch):
        """v1.3：推理模型耗尽 max_tokens → content 空 + finish_reason=length
        → 自动加倍 max_tokens 重试一次（r68/r69 实测 tokeness/free 截断）。"""
        import game_agent.brain.client as mod
        budgets = []

        def fake_post(url, json=None, **k):
            budgets.append(json["max_tokens"])
            if len(budgets) == 1:
                return self._Resp(content="", finish="length")
            return self._Resp(content='{"action":"call_next"}')

        monkeypatch.setattr(mod.requests, "post", fake_post)
        client = self._client()
        client.max_tokens = 4000
        assert client.chat("hi") == '{"action":"call_next"}'
        assert budgets == [4000, 8000]

    def test_empty_content_without_length_raises(self, monkeypatch):
        import game_agent.brain.client as mod

        def fake_post(*a, **k):
            return self._Resp(content="", finish="stop")

        monkeypatch.setattr(mod.requests, "post", fake_post)
        with pytest.raises(AnalyzeError, match="空内容"):
            self._client().chat("hi")


class _ServiceDownClient:
    """先 N 次抛 5xx（上游渠道挂了），之后返回合法决策的假客户端。"""

    name = "service_down"

    def __init__(self, fail_times: int):
        self.fail_times = fail_times
        self.calls = 0

    def chat(self, prompt: str) -> str:
        self.calls += 1
        if self.calls <= self.fail_times:
            raise AnalyzeError(
                '决策模型返回 HTTP 503：{"error":{"message":'
                '"temporarily_unavailable (request id: x)","code":"server_error"}}'
            )
        return json.dumps(_valid_decision(), ensure_ascii=False)


class TestDecideServiceUnavailableBackoff:
    """5xx 是「对方挂了」，不是决策失败——应退避重试，不该直接白掉一步。

    实测 2026-11-10 模型商 503，旧逻辑直接抛错导致整步失败。
    """

    def test_retries_after_503(self, monkeypatch):
        monkeypatch.setattr("game_agent.brain.brain.time.sleep", lambda s: None)
        config = copy.deepcopy(DEFAULT_CONFIG)
        client = _ServiceDownClient(fail_times=2)
        decision, meta = decide(config, _fake_observation(), client=client, mock=True)
        assert decision["action"] == "click"
        assert client.calls == 3
        assert meta["service_waits"] == 2
        assert meta["rate_limit_waits"] == 0

    def test_gives_up_after_max_retries(self, monkeypatch):
        monkeypatch.setattr("game_agent.brain.brain.time.sleep", lambda s: None)
        monkeypatch.setattr("game_agent.brain.brain.SERVICE_MAX_RETRIES", 2)
        config = copy.deepcopy(DEFAULT_CONFIG)
        client = _ServiceDownClient(fail_times=99)
        with pytest.raises(AnalyzeError, match="503"):
            decide(config, _fake_observation(), client=client, mock=True)
        assert client.calls == 3

    def test_401_still_raises_immediately(self, monkeypatch):
        """认证失败不是瞬时故障——不能拿退避当万能药，否则错配的 key 会卡住 50 秒。"""
        monkeypatch.setattr("game_agent.brain.brain.time.sleep", lambda s: None)
        config = copy.deepcopy(DEFAULT_CONFIG)

        class _AuthClient:
            name = "auth"

            def __init__(self):
                self.calls = 0

            def chat(self, prompt: str) -> str:
                self.calls += 1
                raise AnalyzeError("决策模型返回 HTTP 401：invalid api key")

        client = _AuthClient()
        with pytest.raises(AnalyzeError, match="401"):
            decide(config, _fake_observation(), client=client, mock=True)
        assert client.calls == 1


