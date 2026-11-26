"""决策模型的客户端：OpenAI 兼容文本接口 + 离线 Mock。"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any, Optional

import requests

from game_agent.config import get_api_key
from game_agent.errors import AnalyzeError

def _log(message: str) -> None:
    """决策链路的进度日志（stderr，带时间戳）。

    没有它的时候，一次卡 6 分钟的运行日志里只有三行启动提示，看起来就是"死机"。
    """
    stamp = time.strftime("%H:%M:%S")
    print(f"[brain {stamp}] {message}", file=sys.stderr, flush=True)


# Mock 决策样例（与 MOCK_SAMPLE 观察配套：点击“收购按钮”）
MOCK_DECISION: dict[str, Any] = {
    "action": "click",
    "params": {"x": 971, "y": 880},
    "reason": "（Mock）顾客报价合适，点击收购按钮完成这笔交易",
    "confidence": 0.6,
}


class MockBrainClient:
    """离线假客户端：返回固定决策（或从文件加载），用于开发与测试。"""

    name = "mock"

    def __init__(self, sample: Optional[dict[str, Any]] = None):
        self._sample = sample or MOCK_DECISION

    def chat(self, prompt: str) -> str:
        return json.dumps(self._sample, ensure_ascii=False)


class OpenAITextClient:
    """OpenAI 兼容的文本对话客户端（Chat Completions，无图像）。"""

    name = "openai_compat"

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key: str,
        timeout: float = 60,
        max_tokens: int = 800,
        temperature: float = 0.3,
        extra_payload: Optional[dict[str, Any]] = None,
        max_retries: int = 2,
        retry_backoff: float = 2.0,
        max_total_seconds: float = 0.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.timeout = timeout
        self.max_tokens = max_tokens
        self.temperature = temperature
        # 自定义附加请求参数（如智谱 GLM 的 {"thinking": {"type": "disabled"}}）
        self.extra_payload = dict(extra_payload or {})
        # v1.3 网络重试（r68 实测：25 步里 9 步因网关抖动直接失败，浪费决策步）
        self.max_retries = max(0, int(max_retries))
        self.retry_backoff = max(0.0, float(retry_backoff))
        # 单次决策的**墙钟上限**（v2.9）。不加这个，最坏情况是
        # 3 次尝试 × 60s 超时 + 退避 6s，撞上"空内容→加倍预算重试"还要再来一轮，
        # 一步能卡 6 分钟且期间毫无输出——实测就是这样假死（Day 8 open_map 之后）。
        self.max_total_seconds = float(max_total_seconds or 0.0)

    def _post_once(self, url: str, payload: dict[str, Any], timeout: float | None = None):
        return requests.post(
            url,
            json=payload,
            timeout=self.timeout if timeout is None else timeout,
            headers={"Authorization": f"Bearer {self.api_key}"},
        )

    def chat(self, prompt: str) -> str:
        # v1.3 截断自适应（r68/r69 实测）：tokeness/free 是推理模型，reasoning
        # 也计入 max_tokens——复杂局面（库存几十件+历史）会耗尽 4000 → content
        # 为空、finish_reason=length。此时把 max_tokens 加倍重试一次（封顶 16000）。
        token_budget = self.max_tokens
        escalations = 1 if self.max_tokens < 16000 else 0
        url = f"{self.base_url}/chat/completions"
        total_budget = self.max_total_seconds if self.max_total_seconds > 0 else self.timeout * 2.5
        deadline = time.time() + total_budget
        while True:
            if time.time() >= deadline:
                raise AnalyzeError(
                    f"决策模型总耗时超过 {int(total_budget)}s 上限"
                    "——多半是上游排队/限流，建议换更快的模型或稍后重试"
                )
            payload: dict[str, Any] = {
                "model": self.model,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": token_budget,
                "temperature": self.temperature,
            }
            # 自定义附加参数（如关闭思考型模型的推理输出，避免正文为空）
            payload.update(self.extra_payload)
            # v1.3 重试退避：连接错误/超时/5xx/429 指数退避重试（网关抖动不该耗掉一步）
            last_exc: Optional[Exception] = None
            response = None
            timeout_retries = 0
            for attempt in range(self.max_retries + 1):
                remaining = max(5.0, deadline - time.time())
                per_try = min(self.timeout, remaining)
                t_start = time.time()
                try:
                    response = self._post_once(url, payload, timeout=per_try)
                except requests.RequestException as exc:
                    last_exc = exc
                    is_timeout = isinstance(exc, requests.Timeout)
                    if is_timeout:
                        timeout_retries += 1
                    # 超时只重试一次：再等一次 60s 只会把整步拖成数分钟（实测假死）
                    can_retry = attempt < self.max_retries and (
                        not is_timeout or timeout_retries < 2
                    ) and time.time() < deadline
                    if can_retry:
                        wait = self.retry_backoff * (2 ** attempt)
                        _log(
                            f"模型调用失败（{type(exc).__name__}，{time.time() - t_start:.1f}s）："
                            f"{str(exc)[:80]} —— {wait:.0f}s 后重试 {attempt + 1}/{self.max_retries}"
                        )
                        time.sleep(wait)
                        continue
                    raise AnalyzeError(f"请求决策模型失败：{exc}") from exc
                if response.status_code >= 500 or response.status_code == 429:
                    if attempt < self.max_retries and time.time() < deadline:
                        wait = self.retry_backoff * (2 ** attempt)
                        _log(
                            f"模型返回 HTTP {response.status_code} —— {wait:.0f}s 后重试 "
                            f"{attempt + 1}/{self.max_retries}"
                        )
                        time.sleep(wait)
                        continue
                break
            if response is None:  # pragma: no cover - 防御
                raise AnalyzeError(f"请求决策模型失败：{last_exc}")

            if response.status_code != 200:
                raise AnalyzeError(f"决策模型返回 HTTP {response.status_code}：{response.text[:300]}")

            try:
                data = response.json()
                choice = data["choices"][0]
                content = choice["message"]["content"]
                finish_reason = choice.get("finish_reason")
            except (ValueError, KeyError, IndexError, TypeError) as exc:
                raise AnalyzeError(f"无法解析决策模型响应：{response.text[:300]}") from exc

            if content is None:
                content = ""
            if isinstance(content, list):  # 兼容分块返回
                content = "".join(
                    part.get("text", "") for part in content if isinstance(part, dict)
                )
            content = str(content).strip()
            if content:
                return content
            # 空内容 + 被截断 → 加倍预算重试一次（推理 token 把额度吃光）
            if finish_reason == "length" and escalations > 0:
                token_budget = min(token_budget * 2, 16000)
                escalations -= 1
                continue
            raise AnalyzeError(
                f"决策模型返回空内容，finish_reason={finish_reason}"
                "（若为思考型模型，可配置 extra_payload 关闭 thinking）"
            )


def make_brain_client(
    config: dict[str, Any],
    force_mock: bool = False,
    mock_file: Optional[str | Path] = None,
) -> MockBrainClient | OpenAITextClient:
    """按配置创建决策客户端（force_mock / provider=mock / mock_file 时使用离线假客户端）。"""
    brain_cfg = config.get("brain", {})
    provider = "mock" if force_mock else brain_cfg.get("provider", "openai_compat")

    if provider == "mock" or mock_file:
        sample = None
        if mock_file:
            sample = json.loads(Path(mock_file).read_text(encoding="utf-8"))
        return MockBrainClient(sample=sample)
    if provider != "openai_compat":
        raise AnalyzeError(f"不支持的 brain provider：{provider}（可选：openai_compat / mock）")

    base_url = brain_cfg.get("base_url") or ""
    model = brain_cfg.get("model") or ""
    if not base_url or not model:
        raise AnalyzeError(
            "尚未配置决策模型。请在**游戏内按 Ctrl+F9** 打开模型设置面板填写 base_url 与 model"
            "（保存后写入 model_config.ini，优先级高于 config/agent.json）；"
            "或直接编辑 config/agent.json 的 brain 段；离线自检：python -m game_agent.cli step --mock"
        )
    api_key = get_api_key(config, "brain")
    if not api_key:
        env_name = brain_cfg.get("api_key_env", "BRAIN_API_KEY")
        raise AnalyzeError(
            f"未找到决策模型 API Key。可在游戏内 Ctrl+F9 面板填写 api_key，"
            f"或设置环境变量 {env_name}（或 OPENAI_API_KEY）；"
            "离线自检：python -m game_agent.cli step --mock"
        )
    return OpenAITextClient(
        base_url=base_url,
        model=model,
        api_key=api_key,
        timeout=float(brain_cfg.get("timeout_seconds", 60)),
        max_tokens=int(brain_cfg.get("max_tokens", 800)),
        temperature=float(brain_cfg.get("temperature", 0.3)),
        extra_payload=dict(brain_cfg.get("extra_payload") or {}),
        max_retries=int(brain_cfg.get("max_retries", 2)),
        retry_backoff=float(brain_cfg.get("retry_backoff_seconds", 2.0)),
        # 默认 = timeout × 2.5（60s → 150s），一步最多两分半，不会无限拖
        max_total_seconds=float(brain_cfg.get("max_total_seconds", 0) or 0),
    )
