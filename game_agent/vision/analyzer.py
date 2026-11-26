"""视觉模型分析模块：把游戏截图交给视觉模型，产出结构化游戏状态。

支持两类客户端：
- OpenAIVisionClient：任意 OpenAI 兼容的 Chat Completions 视觉接口
  （通义 Qwen-VL / 智谱 GLM-4V / OpenAI GPT-4o / 豆包 vision / 本地 vLLM 等均可，
   只要服务商支持 image_url 形式的多模态输入）
- MockVisionClient：不联网的假实现，用于离线开发与自动化测试

模型被要求输出 JSON（4 个核心字段 + 可选扩展 visible_info），
本模块负责「提示词构建 → 请求 → 从文本中提取 JSON → 校验规范化」的完整链路，
解析失败时会携带纠错提示自动重试一次。
JSON 提取工具（extract_json）已移至 game_agent.util，供多模块共用。
"""
from __future__ import annotations

import base64
import io
import json
import time
from pathlib import Path
from typing import Any, Optional

import requests
from PIL import Image

from game_agent.config import get_api_key
from game_agent.errors import AnalyzeError
from game_agent.util import extract_json  # noqa: F401  （重新导出，保持向后兼容）
from game_agent.vision.models import validate_observation

# 单次观察允许的最大尝试次数（首次解析失败后用纠错提示重试一次）
MAX_ATTEMPTS = 2

# ---------------------------------------------------------------- 提示词

GAME_CONTEXT = """你正在观察一款 Windows PC 游戏的实时截图，游戏名为《深空当铺：可能是偷得》（英文名 Probably Stolen），是一款赛博朋克题材的当铺经营模拟游戏，界面语言为简体中文。
游戏玩法：玩家在一座空间站的下城区经营一家当铺。每天开门营业，接待不同身份的顾客（拾荒者、反抗军、治安员、落魄精英等），收购他们带来的物品（需要鉴定真伪与估算价值），再给物品定价出售赚取差价；也可以查看市场消息、购买设备、扩建店铺。周期性需要缴纳房租，资金或声誉崩溃会导致经营失败。"""

OBSERVATION_TASK = """你的任务是【观察与理解】，不做任何决策。请分析这张游戏截图，输出严格的 JSON。

必须输出的 JSON 格式（4 个核心字段）：
{
  "screen_state": "当前界面类型，从以下枚举中选择一个：main_menu | creation | day_start_news | shop_counter | customer_offer | negotiation | inventory | market | upgrade_menu | dialogue | loading | unknown",
  "available_actions": ["玩家当前可执行的操作，用简短中文动词短语，如：接受报价 / 还价 / 拒绝 / 鉴定物品 / 查看库存 / 继续 / 跳过"],
  "important_objects": [
    {"name": "物品名/角色名/按钮名/金额标签", "type": "item|npc|ui|money|text", "value": "关键数值或描述（没有填 null）", "position": "left|center|right|top|bottom", "point": [x, y]}
  ],
  "confidence": 0.85,
  "visible_info": {
    "money": "画面中可见的玩家资金数额，没有则 null",
    "time_or_day": "可见的日期/天数/时间信息，没有则 null",
    "inventory_summary": "可见的库存物品摘要，没有则 null",
    "dialogue": "画面中正在进行的对话或文本内容摘要，没有则 null",
    "market": "可见的市场行情/新闻信息摘要，没有则 null"
  }
}

规则：
1. confidence 是 0 到 1 之间的小数，表示你对判断的把握；看不清或不确定时必须降低它。
2. 只报告截图中「看得见」的内容，不要脑补看不见的游戏数据。
3. 如果截图不是游戏画面（黑屏、桌面、加载中），screen_state 用 loading 或 unknown，并在 important_objects 里说明原因。
4. important_objects 中的 point 是对象中心点在截图中的像素坐标 [x, y]（原点 = 窗口左上角，x 向右、y 向下），后续动作环节将按此坐标点击目标。**对较小的图标、网格按钮等小尺寸目标，务必仔细核对后给出其中心坐标**；无法估算时填 null。
5. 开局配置类界面（角色创建、专精选择、特性选择等）的 screen_state 用 creation。
6. 物品价格、金额、数量等关键数字要尽量准确抄录进 important_objects 的 value 字段。
7. 只输出 JSON 本身，不要输出任何解释、开场白或 Markdown 代码块。"""


def build_observation_prompt() -> str:
    """构建发送给视觉模型的完整提示词。"""
    return GAME_CONTEXT + "\n\n" + OBSERVATION_TASK


# ---------------------------------------------------------------- Mock

# 离线假客户端返回的样例（形如“顾客上门展示物品”界面；point 为示例坐标）
MOCK_SAMPLE: dict[str, Any] = {
    "screen_state": "customer_offer",
    "available_actions": ["查看物品", "鉴定物品", "出价收购", "拒绝"],
    "important_objects": [
        {"name": "顾客（拾荒者）", "type": "npc", "value": "上门推销一件旧式机械表",
         "position": "center", "point": [880, 380]},
        {"name": "机械表", "type": "item", "value": "顾客开价 120",
         "position": "center", "point": [880, 470]},
        {"name": "收购按钮", "type": "ui", "value": None,
         "position": "bottom", "point": [971, 880]},
    ],
    "confidence": 0.85,
    "visible_info": {
        "money": "1840",
        "time_or_day": None,
        "inventory_summary": None,
        "dialogue": "顾客：这可是好东西，给个价吧。",
        "market": None,
    },
}


class MockVisionClient:
    """离线假客户端：返回固定样例（或从文件加载），用于开发与测试。"""

    name = "mock"

    def __init__(self, sample: Optional[dict[str, Any]] = None):
        self._sample = sample or MOCK_SAMPLE

    def analyze(self, image_path: Path, prompt: str) -> str:
        return json.dumps(self._sample, ensure_ascii=False)


# ---------------------------------------------------------------- OpenAI 兼容客户端


class OpenAIVisionClient:
    """OpenAI 兼容的视觉模型客户端（Chat Completions + image_url base64）。"""

    name = "openai_compat"

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key: str,
        timeout: float = 60,
        max_tokens: int = 1200,
        temperature: float = 0.2,
        use_json_format: bool = False,
        max_image_side: int = 1600,
        extra_payload: Optional[dict[str, Any]] = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.timeout = timeout
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.use_json_format = use_json_format
        self.max_image_side = max_image_side
        # 自定义附加请求参数（如智谱 GLM 的 {"thinking": {"type": "disabled"}}）
        self.extra_payload = dict(extra_payload or {})

    def _encode_image(self, image_path: Path) -> str:
        """读取截图 →（可选）等比缩小 → base64 编码，控制请求体积与 token 成本。"""
        image = Image.open(image_path).convert("RGB")
        longest = max(image.size)
        if longest > self.max_image_side:
            scale = self.max_image_side / longest
            image = image.resize(
                (int(image.width * scale), int(image.height * scale)),
                Image.Resampling.LANCZOS,
            )
        buffer = io.BytesIO()
        image.save(buffer, format="PNG", optimize=True)
        return base64.b64encode(buffer.getvalue()).decode("ascii")

    def analyze(self, image_path: Path, prompt: str) -> str:
        image_b64 = self._encode_image(image_path)
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{image_b64}"}},
                ],
            }],
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
        }
        if self.use_json_format:
            payload["response_format"] = {"type": "json_object"}
        # 自定义附加参数（如关闭思考型模型的推理输出，避免正文为空）
        payload.update(self.extra_payload)

        url = f"{self.base_url}/chat/completions"
        try:
            response = requests.post(
                url,
                json=payload,
                timeout=self.timeout,
                headers={"Authorization": f"Bearer {self.api_key}"},
            )
        except requests.RequestException as exc:
            raise AnalyzeError(f"请求视觉模型失败：{exc}") from exc

        if response.status_code != 200:
            raise AnalyzeError(f"视觉模型返回 HTTP {response.status_code}：{response.text[:300]}")

        try:
            data = response.json()
            choice = data["choices"][0]
            content = choice["message"]["content"]
            finish_reason = choice.get("finish_reason")
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise AnalyzeError(f"无法解析视觉模型响应：{response.text[:300]}") from exc

        if content is None:
            content = ""
        if isinstance(content, list):  # 部分实现返回分块列表，拼接文本部分
            content = "".join(
                part.get("text", "") for part in content if isinstance(part, dict)
            )
        content = str(content).strip()
        if not content:
            # 空输出诊断：给出 finish_reason 线索（length 通常意味着输出预算被截断）
            hint = ""
            if finish_reason == "length":
                hint = "（输出被 max_tokens 截断：若为思考型模型，请配置 extra_payload 关闭 thinking 或提高 max_tokens）"
            raise AnalyzeError(f"视觉模型返回空内容，finish_reason={finish_reason}{hint}")
        return content


# ---------------------------------------------------------------- 解析与校验


def parse_observation(text: str) -> dict[str, Any]:
    """提取 + 校验，返回规范化的观察结果。"""
    return validate_observation(extract_json(text))


# ---------------------------------------------------------------- 工厂与主流程


def make_client(
    config: dict[str, Any],
    force_mock: bool = False,
    mock_file: Optional[str | Path] = None,
) -> MockVisionClient | OpenAIVisionClient:
    """按配置创建视觉客户端。

    force_mock=True、provider=mock 或提供 mock_file 时，使用离线假客户端。
    """
    vision_cfg = config.get("vision", {})
    provider = "mock" if force_mock else vision_cfg.get("provider", "openai_compat")

    if provider == "mock" or mock_file:
        sample = None
        if mock_file:
            sample = json.loads(Path(mock_file).read_text(encoding="utf-8"))
        return MockVisionClient(sample=sample)

    if provider != "openai_compat":
        raise AnalyzeError(f"不支持的 provider：{provider}（可选：openai_compat / mock）")

    base_url = vision_cfg.get("base_url") or ""
    model = vision_cfg.get("model") or ""
    if not base_url or not model:
        raise AnalyzeError(
            "尚未配置视觉模型。请编辑 config/agent.json 填写 vision.base_url 与 vision.model；"
            "离线自检可执行：python -m game_agent.cli observe --mock"
        )
    api_key = get_api_key(config, "vision")
    if not api_key:
        env_name = vision_cfg.get("api_key_env", "VISION_API_KEY")
        raise AnalyzeError(
            f"未找到 API Key。请设置环境变量 {env_name}（或 OPENAI_API_KEY）；"
            "离线自检可执行：python -m game_agent.cli observe --mock"
        )
    return OpenAIVisionClient(
        base_url=base_url,
        model=model,
        api_key=api_key,
        timeout=float(vision_cfg.get("timeout_seconds", 60)),
        max_tokens=int(vision_cfg.get("max_tokens", 1200)),
        temperature=float(vision_cfg.get("temperature", 0.2)),
        use_json_format=bool(vision_cfg.get("json_response_format", False)),
        max_image_side=int(vision_cfg.get("max_image_side", 1600)),
        extra_payload=dict(vision_cfg.get("extra_payload") or {}),
    )


def _compute_point_scale(client, image_path: Path) -> float:
    """计算「模型所见图像 → 原截图」的坐标还原系数。

    发送前若图像被等比缩小（vision.max_image_side），模型是按缩小后的图估算坐标的，
    因此返回的 point 需要乘以「原图长边 / 缩小后长边」才能还原到截图坐标空间。
    离线 Mock 客户端没有 max_image_side 属性，恒返回 1.0（不做缩放）。
    """
    max_side = getattr(client, "max_image_side", None)
    if not max_side:
        return 1.0
    try:
        with Image.open(image_path) as img:
            longest = max(img.size)
    except Exception:
        return 1.0
    return (longest / max_side) if longest > max_side else 1.0


def _rescale_observation_points(observation: dict[str, Any], scale: float) -> dict[str, Any]:
    """把观察结果中所有 point 坐标按 scale 还原到原截图坐标空间。"""
    for obj in observation.get("important_objects") or []:
        point = obj.get("point")
        if isinstance(point, list) and len(point) == 2:
            obj["point"] = [int(round(point[0] * scale)), int(round(point[1] * scale))]
    return observation


def analyze_image(
    image_path: str | Path,
    config: dict[str, Any],
    client: Optional[MockVisionClient | OpenAIVisionClient] = None,
    max_attempts: int = MAX_ATTEMPTS,
) -> tuple[dict[str, Any], str, dict[str, Any]]:
    """分析截图，返回 (规范化观察结果, 模型原始输出, 元信息)。

    解析失败时携带纠错提示重试，最多 max_attempts 次；全部失败抛 AnalyzeError。
    自动把模型输出的 point 坐标还原到原截图坐标空间（处理发送前缩图的情况）。
    """
    image_path = Path(image_path)
    client = client or make_client(config)
    prompt = build_observation_prompt()

    last_error: Optional[Exception] = None
    started = time.time()
    for attempt in range(1, max_attempts + 1):
        raw = client.analyze(image_path, prompt)
        try:
            observation = parse_observation(raw)
        except ValueError as exc:
            last_error = exc
            # 带着纠错信息重试
            prompt = (
                build_observation_prompt()
                + f"\n\n【纠错】你上一次的输出无法解析为合法 JSON（{str(exc)[:120]}）。"
                  "请只输出一个 JSON 对象本身，不要输出任何其他内容。"
            )
            continue
        # 坐标还原：模型按缩放后的图估算 point，换算回原截图坐标
        point_scale = _compute_point_scale(client, image_path)
        if point_scale != 1.0:
            observation = _rescale_observation_points(observation, point_scale)
        meta = {
            "provider": client.name,
            "model": getattr(client, "model", None),
            "image": str(image_path),
            "duration_ms": int((time.time() - started) * 1000),
            "attempts": attempt,
            "point_scale": point_scale,
        }
        return observation, raw, meta

    raise AnalyzeError(f"视觉模型输出无法解析为 JSON（已尝试 {max_attempts} 次）：{last_error}")
