"""决策流水线：观察 + 历史 → 决策（含解析重试与校验）。"""
from __future__ import annotations

import time
from typing import Any, Optional

from game_agent.brain.client import MockBrainClient, OpenAITextClient, make_brain_client
from game_agent.brain.prompt import build_decision_prompt
from game_agent.brain.schema import BRIDGE_ACTION_PARAMS, validate_decision
from game_agent.contracts import derive_night_phase
from game_agent.errors import AnalyzeError
from game_agent.util import extract_json

# v0.9.15 夜晚阶段白名单（原为手写 frozenset；v2.1 起从 game_agent.contracts
# 单一真相派生，与 agent_loop._NIGHT_PHASE_ACTIONS 同源，不再有手工副本。
# 放这里供 decide() 做白名单收紧——避免 brain 反向依赖 agent_loop）
#
# 事故备忘（保留原因，便于后人判断哪些动作属于夜晚）：
#   - r53：打烊后重开门会把当天结算回滚 → 夜晚绝不能放 open_shutter/call_next。
#   - v2.0：提示词要求「出门前先 equip_backpack」，但白名单曾漏掉它，模型选了
#     会被校验层拒绝（拾荒只能空手出门）→ 现已由 ActionSpec.night_phase_ok 覆盖。
NIGHT_PHASE_ACTIONS = derive_night_phase()

# 单次决策允许的最大尝试次数（解析失败后用纠错提示重试一次）
MAX_ATTEMPTS = 2

# 限流退避（RATE_LIMIT）：模型服务返回 HTTP 429 时的重试上限与退避基数（秒）。
# 退避按次数递增（3s → 6s → 9s…），只针对限流，其他错误立即抛出。
RATE_LIMIT_MAX_RETRIES = 3
RATE_LIMIT_BACKOFF_SECONDS = 3.0

# 上游瞬时故障（HTTP 5xx / temporarily_unavailable）：与限流不同——不是我们调太快，
# 是对方渠道挂了。但同样是"等一会儿就好"，不该直接抛错白掉一步
# （实测 2026-11-10 模型商 503 导致整步决策失败）。
SERVICE_MAX_RETRIES = 4
SERVICE_BACKOFF_SECONDS = 5.0


def _is_rate_limited(exc: Exception) -> bool:
    """判断异常是否为限流（HTTP 429 / 访问量过大）。"""
    text = str(exc)
    return "429" in text or "访问量过大" in text or "rate limit" in text.lower()


def _is_service_unavailable(exc: Exception) -> bool:
    """判断异常是否为上游瞬时故障（5xx / 渠道暂不可用）。

    与限流分开判断：限流是"我们调太快"，5xx 是"对方挂了"。两者都该退避，
    但分开计数便于诊断到底是哪一类在拖慢决策。
    """
    text = str(exc).lower()
    return (
        "503" in text
        or "502" in text
        or "504" in text
        or "temporarily_unavailable" in text
        or "service temporarily unavailable" in text
        or "服务暂时不可用" in text
    )






def _dead_end_customer(observation: Any) -> bool:
    """当前顾客是否死局（v1.1，r61 催生）：无可卖 AND 买不起。

    r61 实测：纯卖家在店 + 现金 < 租金 → 模型 13/14 步空转（sell_item/call_next
    反复被拒），**从不选 dismiss_client**。提示词约束不住推理模型，改为白名单
    硬过滤：死局时只保留 dismiss_client / end_day / wait / stop 等收尾动作。
    """
    if not isinstance(observation, dict):
        return False
    vi = observation.get("visible_info")
    vi = vi if isinstance(vi, dict) else observation
    client = vi.get("client") or {}
    if not isinstance(client, dict) or not client.get("identifier"):
        return False  # 无顾客在场，不算死局
    sellable = _extract_sellable_now(observation)
    if sellable is None or len(sellable) > 0:
        return False  # 有可卖物品（或观察缺失时不做判断）
    cash = vi.get("money") or 0
    rent = ((vi.get("rent") or {}) if isinstance(vi.get("rent"), dict) else {}).get("rent_value") or 0
    not_owned = ((vi.get("inventory") or {}) if isinstance(vi.get("inventory"), dict) else {}).get("not_owned") or []
    cheapest = None
    for it in not_owned:
        if isinstance(it, dict) and isinstance(it.get("value"), (int, float)):
            v = it["value"]
            cheapest = v if cheapest is None else min(cheapest, v)
    if cheapest is not None and cash - rent >= cheapest:
        return False  # 至少有一件买得起——交给交租红线精确判定
    return True


def _extract_sellable_now(observation: Any) -> Optional[list]:
    """从观察里取 sellable_now（visible_info 或顶层）；缺失返回 None（不启用过滤）。"""
    if not isinstance(observation, dict):
        return None
    vi = observation.get("visible_info")
    if isinstance(vi, dict) and "sellable_now" in vi:
        value = vi.get("sellable_now")
        return value if isinstance(value, list) else None
    value = observation.get("sellable_now")
    return value if isinstance(value, list) else None


def decide(
    config: dict[str, Any],
    observation: dict[str, Any],
    *,
    image_size: Optional[tuple[int, int] | list[int]] = None,
    history: Optional[list[dict[str, Any]]] = None,
    mock: bool = False,
    mock_file: Optional[str | Path] = None,
    client: Optional[MockBrainClient | OpenAITextClient] = None,
    max_attempts: int = MAX_ATTEMPTS,
    bridge_actions: bool = False,
    no_mouse: bool = False,
    forbid_wait: bool = False,
    forbid_actions: Optional[set[str]] = None,
    cycle_forbid: Optional[set[str]] = None,
    night_phase: bool = False,
    goal_block: Optional[str] = None,
    mechanics_block: Optional[str] = None,
    experience_block: Optional[str] = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """根据当前观察与历史，产出下一步动作决策。

    Args:
        bridge_actions: 是否允许「游戏 API 直调动作」。由调用方（agent_loop）
            按观察通道显式置位（仅 bridge 通道为 True）；本函数与
            build_decision_prompt 均不做按 source 的自动推断（T1 修复）。
        goal_block: 商业目标 KPI 段（v2.1）——由 BusinessGoal.render() 生成，
            原样插入提示词；None 时不插入。
        mechanics_block: 机制知识段（v2.2）——由 MechanicRouter.render() 生成。
        experience_block: 经验段（v3.15）——由 ExperienceStore.render() 生成，
            内容是「同一局面下该动作历史上是否推进过局面」的统计。

    Returns:
        (决策, 元信息)：决策为 {action, params, reason, confidence}（已校验）；
        元信息含 provider/model/耗时/尝试次数/原始输出。

    Raises:
        AnalyzeError: 模型未配置、请求失败或输出多次无法解析。
    """
    client = client or make_brain_client(config, force_mock=mock, mock_file=mock_file)
    history = history or []
    history_limit = int(config.get("brain", {}).get("history_size", 6))
    extra_actions = BRIDGE_ACTION_PARAMS if bridge_actions else None
    # v0.9.10/v0.9.13：无效动作 + 周期动作都从白名单硬剔除（提示词声明对
    # 推理模型约束不足——r47/r49 实测被声明禁用后仍连选；剔除后模型输出会被
    # 校验层拒绝、带纠错重试，强制它换策略）。
    all_forbid = set(forbid_actions or set()) | set(cycle_forbid or set())
    if extra_actions is not None and night_phase:
        # v0.9.15 夜晚阶段：打烊后只保留夜晚流程动作。r53 实测打烊后 AI 选
        # open_shutter 重新开门 → 当天结算回滚、天数卡死——营业动作必须从
        # 白名单拿掉，模型选了会被校验层拒绝并被迫换夜晚动作。
        extra_actions = {
            k: v for k, v in extra_actions.items()
            if k in NIGHT_PHASE_ACTIONS and k not in all_forbid
        }
    elif extra_actions is not None and all_forbid:
        extra_actions = {k: v for k, v in extra_actions.items() if k not in all_forbid}
    # v1.1 事实过滤（r60 催生）：当前顾客没有可卖物品（sellable_now 为空=顾客
    # 想收类别与库存无交集/纯卖家）→ sell_item 必败，从白名单移除，断掉
    # 「反复卖已卖掉的东西」死循环（r60 实测 sell_item 空转 ×7）。
    if extra_actions is not None:
        sellable = _extract_sellable_now(observation)
        if sellable is not None and len(sellable) == 0:
            extra_actions = {k: v for k, v in extra_actions.items() if k != "sell_item"}
        # v1.1 死局硬过滤：无可卖且买不起 → 只留收尾动作（r61 模型从不会主动送客）
        if _dead_end_customer(observation):
            dead_end_ok = {"dismiss_client", "end_day", "end_day_confirm",
                           "dialog_advance", "dialog_choose", "wait", "stop"}
            extra_actions = {k: v for k, v in extra_actions.items() if k in dead_end_ok}
    prompt = build_decision_prompt(
        observation, image_size, history, history_limit,
        bridge_actions=bridge_actions, no_mouse=no_mouse, forbid_wait=forbid_wait,
        forbid_actions=forbid_actions, cycle_forbid=cycle_forbid,
        night_phase=night_phase, goal_block=goal_block,
        mechanics_block=mechanics_block,
        experience_block=experience_block,
    )

    last_error: Optional[Exception] = None
    started = time.time()
    attempt = 0
    rate_limit_waits = 0
    service_waits = 0
    while attempt < max_attempts:
        try:
            raw = client.chat(prompt)
        except AnalyzeError as exc:
            # 限流（HTTP 429）退避重试：外部服务繁忙不是决策失败，不该立刻
            # 冒泡打断主循环（智谱 GLM 高频调用时常见）。退避不消耗
            # max_attempts（那是「解析纠错」预算，与限流无关）。
            if _is_rate_limited(exc) and rate_limit_waits < RATE_LIMIT_MAX_RETRIES:
                rate_limit_waits += 1
                time.sleep(RATE_LIMIT_BACKOFF_SECONDS * rate_limit_waits)
                continue
            # 5xx / 渠道暂不可用：退避后重试，不消耗解析预算，也不直接中止本步
            if _is_service_unavailable(exc) and service_waits < SERVICE_MAX_RETRIES:
                service_waits += 1
                time.sleep(SERVICE_BACKOFF_SECONDS * service_waits)
                continue
            raise
        try:
            decision = validate_decision(extract_json(raw), extra_actions=extra_actions)
        except ValueError as exc:
            last_error = exc
            attempt += 1
            if attempt >= max_attempts:
                break
            # 带着纠错信息重试
            prompt = (
                build_decision_prompt(
                    observation, image_size, history, history_limit,
                    bridge_actions=bridge_actions, no_mouse=no_mouse,
                    forbid_wait=forbid_wait, forbid_actions=forbid_actions,
                    cycle_forbid=cycle_forbid, night_phase=night_phase,
                    goal_block=goal_block, mechanics_block=mechanics_block,
                    experience_block=experience_block,
                )
                + f"\n\n【纠错】你上一次的输出不是合法决策（{str(exc)[:150]}）。"
                  "请检查动作白名单与参数，只输出一个合法 JSON 对象，不要输出其他内容。"
            )
            continue
        meta = {
            "provider": client.name,
            "model": getattr(client, "model", None),
            "duration_ms": int((time.time() - started) * 1000),
            "attempts": attempt + 1,
            "rate_limit_waits": rate_limit_waits,
            "service_waits": service_waits,
            "bridge_actions": bool(bridge_actions),
            "raw_response": raw,  # 保留原始输出便于排查
        }
        return decision, meta

    raise AnalyzeError(f"决策模型输出无法解析（已尝试 {max_attempts} 次）：{last_error}")
