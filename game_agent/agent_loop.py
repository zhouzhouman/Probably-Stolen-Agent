"""Agent 主循环：观察 → 决策 → 执行。

职责：
- step_once：执行一个完整步骤（观察 → 决策 → 执行）并落盘到 artifacts/steps/
- run_loop：按步数上限连续执行；含 dry-run、错误熔断、STOP 文件中止；
  支持事件驱动模式（结构化通道下状态无变化则等待，不空转决策）

观察通道（source 参数，默认 auto 按优先级自动选择）：
- bridge：游戏内插件 HTTP 桥直读内存状态（毫秒级，决策可含 API 直调动作）
- statewatch：日志 + 存档文件直读（百毫秒级，无需插件）
- vision：截图 + 视觉模型（兜底；mock 模式强制走此通道）

防卡死 / 安全机制（与项目"绝不卡住"目标对应）：
- 默认 dry-run：live=False 时不产生任何真实鼠标键盘输入，也不直调游戏 API
- 步数上限：max_steps 控制循环长度，绝无死循环
- 错误熔断：连续 8 次系统级失败（异常）即停止；业务失败不计入
- STOP 文件：在 artifacts/ 下创建 STOP 文件即可让循环在下一步开始前退出
- 每步落盘：观察 / 决策 / 执行全量记录，可审计、可回放

操作纪律（v0.8.5，用户 2026-10-16 反馈「调用接口要让动画过完」）：
- 动画等待闸门（双通道，live 动作执行前）：
  ① 实时通道：/state 签名连续两次一致才算「稳定」（动画中关键字段持续变化）；
  ② 日志通道：GameLog 无在途动作/移动（action_started/ended、Arriving/Arrived）。
  两通道均超时（默认 30s）放行——绝不卡死；避免「抢调接口」造成物品残留与流程错位
- 失败自动恢复：卖出（sell_item）业务失败 → 自动 clear_table 清桌回收，
  防物品滞留谈判桌（见 _RECOVERY_ACTIONS）
- 成交后自动收纳（v0.9.18，规则：批发后的物品必须放回储物区）：批发
  买入成交 → 自动点 transferButton 把前台柜台物品搬回储物区（不收纳则
  front counter full，下一位顾客的物品被游戏直接销毁——r55 实锤）

反空转三层防线（按触发维度分工，均为「白名单硬剔除 + 提示词声明」）：
- v0.9.8 无效动作 streak：同名动作连续执行且状态签名零变化 ≥3 次 → 禁用
  （管「做了没效果」，如 accept_offer 空转 ×11）；
- v0.9.13 周期动作窗口：end_day/begin_day/end_day_confirm/pay_rent/buy_insurance
  在最近 6 步内成功执行过 → 禁用（管「不该重复做」——这类动作每次都真实
  改变状态，零变化 streak 拦不住；r49 实测 end_day 连选 4 次推进 4 次天数）；
- v0.9.13 卷帘门幂等守卫：门已处于目标态仍要开/关 → 业务失败不调桥接
  （r49 实测 open_shutter ×5 使 shutter_closed 抖动、签名持续变化）。
- v0.9.15 夜晚阶段白名单：end_day 成功后直到 begin_day 前，营业类动作全部
  从白名单剔除，只许夜晚流程动作（r53 实测：打烊后重新开门叫客 → 当天结算
  回滚、天数卡死——「没睡觉一直第 11 天」的根因）。
"""
from __future__ import annotations

import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from game_agent.brain.brain import decide
from game_agent.bridge.client import ACTIONS as BRIDGE_ACTIONS
from game_agent.bridge.client import BridgeClient, make_bridge_client
from game_agent.config import artifacts_dir, load_config
from game_agent.experience import Experience, ExperienceStore, pattern_of
from game_agent.contracts import (
    SIG_INDEX,
    derive_action_cn,
    derive_day_only,
    derive_night_only,
    derive_night_phase,
    derive_once_per_cycle,
)
from game_agent.control.controller import (
    ControlResult,
    click_image,
    ctrl_click_image,
    move_image,
    press_key,
    type_text,
)
from game_agent.errors import BridgeError, ControlError, GameAgentError, WindowNotFoundError
from game_agent.goal import BusinessGoal
from game_agent.knowledge import mechanic_block
from game_agent.statewatch import (
    find_latest_gamelog,
    parse_log_lines,
    read_recent_lines,
    resolve_data_dir,
)
from game_agent.structured.channels import (
    CHANNEL_BRIDGE,
    CHANNEL_STATEWATCH,
    CHANNEL_VISION,
    pick_source,
    state_signature,
)
from game_agent.structured.observer import StructuredObserver
from game_agent.vision.observer import observe
from game_agent.vision.window import find_game_window

# 连续「系统级错误」达到该值即熔断（v0.7.3b：2→8 放宽——桥偶发抖动/GLM 偶发超时
# 不应中断长跑；业务失败（动作 ok=false）不抛异常、不计数，由 AI 自行调整策略）
MAX_CONSECUTIVE_ERRORS = 8

# 反空转阈值（r36 回归）：模型连续选wait 达到该次数后，
# 决策提示词里禁用 wait，强制它选择推进类动作（禁鼠标模式生效）。
_WAIT_STREAK_LIMIT = 2

# 无效动作阈值（v0.9.8「无效动作 streak」）：同名动作连续执行、且状态签名零变化
# 达到该次数后，提示词禁用该动作（r45 实测 accept_offer 空转 ×11 催生）。
_DEAD_ACTION_STREAK_LIMIT = 3

# 周期动作窗口禁用（v0.9.13，r49 修复 A）：「每日/每周期仅一次」类动作。
# r49 实测：end_day 被连选 4 次且**每次都真实生效**（打烊结算、推进天数），
# 状态签名持续变化使 v0.9.8 的「零变化 streak」全程失明（拖到第 5 步才禁）。
# 对这类动作改用「动作计数」维度：最近窗口内成功执行过即禁用，与状态是否
# 变化无关——重复打一天烊永远不是合法行为。
#
# v3.8（CR-X01-3）：名单改由 game_agent.contracts 派生（唯一真相）。
# 原手工副本 5 项 = derive_once_per_cycle() 5 项，逐项相等；r49 的事故知识
# 保留在本注释里，派生只替换**数据**，不替换**知识**。
_ONCE_PER_CYCLE_ACTIONS: frozenset[str] = derive_once_per_cycle()
_CYCLE_FORBID_WINDOW = 6  # 步：成功执行后 N 步内禁止再选同周期动作

# 夜晚流程状态在 state_signature 元组中的下标（见 channels.state_signature）。
# v0.9.16 共 20 位；v2.2（CR-X01-2）末尾追加降级证据位后为 21 位，前 20 位顺序不变，
# 故 12 / 19 仍然有效。
#
# v3.8.1（task-11）：改由 game_agent.contracts.SIG_INDEX 派生，不再写死数字。
# 理由：state_signature 已经证明它会增长（20→21）。只要有人**在中间**插一位，
# 本文件所有写死下标的地方都会**静默读错位**——不报错、不打日志，守卫拿着隔壁
# 字段的值照常放行或拦截，事后从 artifacts 几乎无法还原。具名派生能直接避免这类故障。
_SIG_IS_OUTSIDE = SIG_INDEX["night.is_outside"]  # night.is_outside：人是否在外出地点
_SIG_MAP_OPEN = SIG_INDEX["night.map_open"]      # night.map_open：夜间地图面板是否打开
_SIG_STORE_STATE = SIG_INDEX["store_state"]      # store_state：CLOSED/OPEN/MORNING
_SIG_HAS_CLIENT = SIG_INDEX["client.identifier"]  # client.identifier：顾客在店则非空
_SIG_SHUTTER_CLOSED = SIG_INDEX["shutter_closed"]  # shutter_closed：卷帘门是否关着
_SIG_CASH = SIG_INDEX["cash"]                    # cash：玩家现金（租金红线用）
_SIG_CAN_NEXT = SIG_INDEX["queue.can_next"]      # queue.can_next：队列还能叫下一位吗

# 夜晚阶段白名单（v0.9.15，r53 催生）：end_day 打烊成功 → 直到 begin_day 前，
# 只允许夜晚流程动作。r53 实测：打烊后 AI 选 open_shutter 重新开门叫客，
# 游戏把「重新开门」当作放弃本次结账——**当天结算回滚**（dayUntilRent 2→3、
# 现金不动），天数卡死不再前进（用户报「怎么没睡觉，导致一直第11天」）。
# 提示词规则（v0.9.12 夜晚流程）约束不住模型，故从动作白名单硬剔除营业动作。
# begin_day 保留在名单内：既是夜晚终点，也是异常时的逃生出口。
# equip_backpack（v2.0）：出门前装背包——夜晚是唯一可用时机，此前被漏掉，
# 导致没带背包出门、只有 2 个口袋、拾荒战利品装不下（配合 stow_loot 铁律看）。
# click_button：夜晚界面按钮（EndOfDay/Map UI 的玩家路径），必须留在白名单内。
#
# v3.8（CR-X01-3）：名单改由 game_agent.contracts 派生（唯一真相，
# 判据是 ActionSpec.night_phase_ok）。原手工副本去重后 15 项，与派生值逐项相等。
# 注意两件**不能因为派生而改变**的事实（改了就是行为回归）：
#   1. machine_put / machine_take 原手工名单里被重复声明了两次（去重前后都是
#      15 项 → 去重后仍是 15）。它们的 phase 是 "both" 不是 "night"——
#      **属「夜晚可用」而非「夜晚专属」**，所以只会出现在本表（night_phase_ok），
#      绝不能进 _NIGHT_ONLY_ACTIONS。否则 _pre_action_state_guard 会在
#      store_state=CLOSED 时把它们误拒。
#   2. 本表只决定「夜晚阶段模型**能看见/可选**什么」，与 _NIGHT_ONLY_ACTIONS
#      的「只能在 CLOSED 执行」是两套语义，不要合并。
_NIGHT_PHASE_ACTIONS: frozenset[str] = derive_night_phase()

# 夜晚阶段跨批次标记（v0.9.16）：night_phase 是每批次的进程内状态，但游戏
# 状态跨批次延续——上批打完烊地图还开着，新批次必须知道「已在夜晚」，
# 否则首步白名单放行营业动作，又会回滚当天。标记文件记载进入夜晚的时间，
# 超过 _NIGHT_MARKER_MAX_AGE 视为陈旧（异常场景），忽略。
_NIGHT_MARKER_MAX_AGE = 2 * 3600  # 秒


def _night_marker_path(config: dict[str, Any]) -> Path:
    return artifacts_dir(config) / "night_phase.flag"


def _write_night_marker(config: dict[str, Any]) -> None:
    """标记「已进入夜晚阶段」（end_day 成功后调用；best-effort，失败不阻断）。"""
    try:
        _night_marker_path(config).write_text(
            datetime.now().isoformat(timespec="seconds"), encoding="utf-8"
        )
    except OSError:
        pass


def _clear_night_marker(config: dict[str, Any]) -> None:
    """清除夜晚标记（begin_day / stay_home 成功后调用；best-effort）。"""
    try:
        _night_marker_path(config).unlink(missing_ok=True)
    except OSError:
        pass


def _night_marker_valid(config: dict[str, Any]) -> bool:
    """标记存在且未陈旧（<_NIGHT_MARKER_MAX_AGE）→ True。"""
    try:
        ts = _night_marker_path(config).read_text(encoding="utf-8").strip()
        age = (datetime.now() - datetime.fromisoformat(ts)).total_seconds()
    except (OSError, ValueError):
        return False
    return 0 <= age <= _NIGHT_MARKER_MAX_AGE


# 阶段兜底动作（v1.1，r62 催生）：模型连续 2 次无法产出有效决策时，执行当前
# 阶段的唯一合理动作，避免整批烧在无效输出上（r62 实测夜晚阶段 open_shutter
# 被拒 7 次、批次耗光）。只覆盖已实证的死锁阶段，不猜玩法。
_STAGE_FALLBACK: dict[str, str] = {
    "closing": "end_day_confirm",  # 已打烊、服务面板开着 → 确认
    "outing": "stay_home",         # 已确认夜晚 → 睡觉进次日
}


def _fallback_action(night_stage: Optional[str], config: dict[str, Any],
                     source: str, bridge_client: Any) -> Optional[str]:
    """返回阶段兜底动作名；无合适兜底返回 None。"""
    if night_stage is not None:
        # v1.3：夜晚阶段的兜底按**实时状态**选（r71 实测：游戏已自动入睡到
        # MORNING 时仍兜底 stay_home，白跑一步）。MORNING → 直接开张；
        # CLOSED → end_day_confirm（= 入睡按钮，确认服务并进次日）。
        store_now = _live_store_state(bridge_client)
        if store_now == "MORNING":
            return "begin_day"
        if store_now == "CLOSED":
            return "end_day_confirm"
        return _STAGE_FALLBACK.get(night_stage)
    # 白天死锁：读实时签名判断（队列空+顾客在店 → 送客；无人+队列空 → 打烊）
    if source not in (CHANNEL_BRIDGE, CHANNEL_STATEWATCH):
        return None
    try:
        seed = StructuredObserver(config, client=bridge_client).observe(source, save=False)
    except (GameAgentError, ValueError, OSError):
        return None
    sig = tuple(seed.signature) if seed.signature else ()
    # 长度下限：必须能读到本函数要用的最靠后的那一位（queue.can_next）。
    # 原写死 `len(sig) <= 16`（即要求 ≥17 位）；20→21 扩位后仍是这个含义，
    # 这里只把数字换成具名下标的等价表达式，不改变判断结果（v3.8.1 / task-11）。
    if len(sig) <= _SIG_CAN_NEXT + 1:
        return None
    store_state = sig[_SIG_STORE_STATE]
    has_client = bool(sig[_SIG_HAS_CLIENT])
    can_next = sig[_SIG_CAN_NEXT]
    # 顾客在店且模型连续失败（无论队列还有没有下一位）→ 送客换人
    # （r63 实测：白名单只剩少数动作时模型仍输出被禁动作，必须打断纠缠）
    if has_client:
        return "dismiss_client"
    if not has_client and can_next is True:
        return "call_next"
    if not has_client and can_next is False and store_state == "OPEN":
        return "end_day"  # 无顾客可接——打烊走夜晚流程
    return None


def _seed_night_phase(
    config: dict[str, Any], source: str, bridge_client: Any
) -> bool:
    """从游戏实时状态 seed 夜晚标志（v0.9.16 跨批次续跑的第二保险）。

    地图开着（map_open）或人在外出地点（is_outside）⇒ 必然在夜晚阶段。
    StoreState 没有 NIGHT 值（只有 CLOSED/OPEN/MORNING），所以「夜晚但地图
    关着」（如上批 end_day 后正好用完步数）由标记文件覆盖，本函数兜底外场景。
    观察失败一律返回 False（按白天继续，不误锁）。
    """
    if source not in (CHANNEL_BRIDGE, CHANNEL_STATEWATCH):
        return False  # vision/mock 通道无结构化签名，无从判断
    try:
        seed = StructuredObserver(config, client=bridge_client).observe(source, save=False)
    except (GameAgentError, ValueError, OSError):
        return False
    sig = tuple(seed.signature) if seed.signature else ()
    if len(sig) <= _SIG_MAP_OPEN:
        return False
    # MORNING = 新一天早晨（夜晚已过）——即使 map_open 陈旧也照样开张。
    # r54 结尾亲证：AI 卡在 open_map 空转时游戏时间走过夜，次日 MORNING。
    if sig[_SIG_STORE_STATE] == "MORNING":
        return False
    return bool(sig[_SIG_MAP_OPEN]) or bool(sig[_SIG_IS_OUTSIDE])


def _update_cycle_forbid(steps: list[dict[str, Any]]) -> set[str]:
    """周期动作窗口计数（v0.9.13）：最近 _CYCLE_FORBID_WINDOW 步内**成功执行过**
    的「每周期一次」类动作 → 禁用集（调用方并入 forbid_actions）。

    与 _update_dead_action_tracker 的分工：那个管「做了没效果」（零变化 streak），
    这个管「不该重复做」（周期一次）。两者都从白名单硬剔除 + 提示词声明。

    Args:
        steps: run_loop 已累积的步骤记录列表。解析失败的错误步没有 decision，
            天然被跳过；只看 execution.ok=True 的成功执行。
    """
    if not steps:
        return set()
    forbidden: set[str] = set()
    for record in steps[-_CYCLE_FORBID_WINDOW:]:
        decision = record.get("decision") or {}
        execution = record.get("execution") or {}
        action = decision.get("action")
        if action in _ONCE_PER_CYCLE_ACTIONS and execution.get("ok"):
            forbidden.add(action)
    return forbidden


def _update_dead_action_tracker(
    prev_action: Optional[str],
    prev_sig: Optional[tuple],
    action: Optional[str],
    sig: Optional[tuple],
    prev_same_streak: int,
    prev_nochange_streak: int,
) -> tuple[int, int, set[str]]:
    """更新「无效动作」跟踪（v0.9.8）。

    语义：同名动作连续执行、且期间状态签名始终未变化 → 判定为「无效动作」。
    sig 为「执行前的观察签名」；相邻两步签名相同 ⇔ 中间的动作没有造成变化。

    Returns:
        (same_action_streak, no_change_streak, stuck_actions)
        stuck_actions 非空时，调用方应在下一步提示词中禁用该动作。
    """
    if action is not None and action == prev_action:
        same_streak = prev_same_streak + 1
    else:
        same_streak = 1
    if sig is not None and prev_sig is not None and sig == prev_sig:
        nc_streak = prev_nochange_streak + 1
    else:
        nc_streak = 0
    stuck: set[str] = set()
    if (
        action
        and same_streak >= _DEAD_ACTION_STREAK_LIMIT
        and nc_streak >= _DEAD_ACTION_STREAK_LIMIT
    ):
        stuck = {action}
    return same_streak, nc_streak, stuck


# 切换 NPC 类动作的动画等待（v0.7.5b）：游戏日志实测 Arriving→Client Arrived 约 2-3 秒，
# 动画没放完就 dismiss/dialog 会穿模。在基础停顿上额外追加。
_NPC_MOVE_EXTRA_DELAY: dict[str, float] = {
    "call_next": 3.5,
    "dismiss_client": 3.5,
}

# 失败自动恢复表（v0.8.5「操作纪律」）：业务失败的动作 → 需补做的收尾动作。
# 背景（用户 2026-10-16 14:17 反馈）：sell_item 失败后物品留在谈判桌未回收，
# 换下一位顾客时台上残留「上一个人的东西」——代码级强制清桌，不指望提示词。
_RECOVERY_ACTIONS: dict[str, str] = {
    "sell_item": "clear_table",
}

# 动作中文名（v0.7.3：浮层决策框显示全中文，方便测试与记录）
# v3.8（CR-X01-3）：改由 game_agent.contracts 派生（唯一真相 ActionSpec.cn_name）。
# 原手工副本 34 项（7 基础 + 27 桥接）= derive_action_cn() 34 项，逐项相等。
# 派生规则保留原语义：cn_name=None 的动作**不出现**在此表里（没有中文名就不编造），
# 因此 _zh_action() 对未知/未登记动作仍原样回显动作名（下游浮层展示依赖这一点）。
ACTION_CN: dict[str, str] = derive_action_cn()


def _zh_action(action: str) -> str:
    """动作名转中文（未知动作原样显示）。"""
    return ACTION_CN.get(action, action)


def _build_name_map(observation: Any) -> dict[str, str]:
    """从观察的 inventory 段建 identifier → 物品中文名映射（浮层显示用）。

    兼容两种位置：顶层 "inventory" 或 "visible_info.inventory"（bridge 观察契约
    把物品级库存放在 visible_info 内，见 observer.build_observation）。
    """
    name_map: dict[str, str] = {}
    if not isinstance(observation, dict):
        return name_map
    inv = observation.get("inventory")
    if not isinstance(inv, dict):
        vi = observation.get("visible_info")
        inv = vi.get("inventory") if isinstance(vi, dict) else None
    if isinstance(inv, dict):
        for key in ("owned", "not_owned"):
            items = inv.get(key) or []
            if isinstance(items, list):
                for it in items:
                    if isinstance(it, dict) and it.get("identifier") and it.get("name"):
                        name_map[it["identifier"]] = it["name"]
    return name_map


def _zh_params(params: Any, name_map: dict[str, str]) -> str:
    """params 转中文显示串：identifier 换成物品中文名。"""
    if not isinstance(params, dict) or not params:
        return ""
    parts: list[str] = []
    for key, value in params.items():
        if key == "identifier" and isinstance(value, str):
            parts.append("物品：%s" % name_map.get(value, value))
        elif key == "vendor" and isinstance(value, str):
            parts.append("供应商：%s" % value)
        else:
            parts.append("%s：%s" % (key, value))
    return "，".join(parts)


def push_decision_overlay(
    config: dict[str, Any],
    decision: dict[str, Any],
    *,
    step: Optional[int] = None,
    observation: Optional[Any] = None,
) -> bool:
    """把决策推送到游戏内展示浮层（best-effort，绝不阻断主循环）。

    浮层显示全中文（v0.7.3）：动作名与物品标识都转成中文，便于测试与记录。

    桥接服务随游戏进程就绪；游戏未运行 / 插件未加载时静默跳过，
    仅在步骤记录中留痕（由调用方写入 record）。

    Returns:
        True = 推送成功；False = 跳过（桥不可达等，属正常情况）。
    """
    try:
        client = make_bridge_client(config)
        zh_decision = dict(decision)
        zh_decision["action"] = _zh_action(str(decision.get("action", "")))
        params_text = _zh_params(decision.get("params"), _build_name_map(observation))
        client.post_decision(zh_decision, step=step, params_text=params_text or None)
        return True
    except BridgeError:
        return False


# ---------------------------------------------------------------- 库存守卫（v0.9.3）

def _inventory_guard(decision: dict[str, Any], observation: Any) -> Optional[str]:
    """卖出/收购前置校验（v0.9.3「凭空卖」修复，用户 2026-10-16 指令"看自己有什么"）：

    交易类动作的 identifier 必须真实存在于当前观察的库存段——
    - sell_item：identifier 必须在 inventory.owned（先看自己有什么再卖）；
    - buy_item：identifier 必须在 inventory.not_owned（顾客的物品）。

    观察中没有库存数据（vision 通道 / 离线测试注入）时返回 None（不校验、不误拦）。
    返回非 None 为拦截原因（调用方应构造业务失败、不得执行桥接调用、不得触发失败恢复）。
    """
    action = str((decision or {}).get("action") or "")
    if action not in ("sell_item", "buy_item"):
        return None
    ident = str(((decision.get("params") or {}).get("identifier")) or "").strip()
    if not ident:
        return None  # schema 层已校验非空
    if not isinstance(observation, dict):
        return None
    vi = observation.get("visible_info")
    if not isinstance(vi, dict):
        return None
    inv = vi.get("inventory")
    if not isinstance(inv, dict):
        return None
    key = "owned" if action == "sell_item" else "not_owned"
    items = inv.get(key)
    if not isinstance(items, list):
        return None  # 该段缺失：放行（不误拦）
    ids = {str(it.get("identifier")) for it in items if isinstance(it, dict)}
    if ident not in ids:
        if action == "buy_item":
            # v3.6（2026-11-10 实机）：正式版里顾客的货**要先开议价面板才会摆出来**——
            # 没开面板时 not_owned 是空的，旧文案会说"顾客物品里没有 X"，
            # 让模型以为自己记错了货，实际只是顺序不对，白烧一步。
            client = vi.get("client") if isinstance(vi.get("client"), dict) else {}
            table = vi.get("table") if isinstance(vi.get("table"), dict) else {}
            selling = bool(client.get("is_selling"))
            nego_open = bool(table.get("nego_open"))
            if selling and not nego_open:
                return (
                    "⚠️ 库存守卫：顾客在卖货但议价面板没开（nego_open=false）→ "
                    "先 open_negociation 让顾客把货摆上桌，再 buy_item；"
                    "此时 not_owned 为空是正常的，不代表顾客没货"
                )
            if selling:
                have = "、".join(
                    str(it.get("name")) for it in items[:6] if isinstance(it, dict)
                )
                return (
                    f"⚠️ 库存守卫：顾客物品里没有「{ident}」→ "
                    f"实际可收：{have or '（空）'}；照抄清单里的 identifier，不要凭空交易"
                )
        what = "库存（inventory.owned）" if action == "sell_item" else "顾客物品（inventory.not_owned）"
        return f"⚠️ 库存守卫：{what}中没有「{ident}」——先看清单里实际有什么，不要凭空交易"
    return None


# ---------------------------------------------------------------- 对话/送客守卫（v1.2）

def _dialog_guard(decision: dict[str, Any], observation: Any) -> Optional[str]:
    """对话与送客的前置守卫（v1.2，r65 治安盘查员卡店催生）。

    1) dialog_advance 在 dialog.is_end=true 时拒绝：对话已结束，强行推进会
       打断游戏的顾客离店协程——治安盘查员（dismissable=false 的重要 NPC）
       会永久卡在店里，队列和打烊全被堵死（r65 实锤 19:07 对话结束后卡住）。
    2) dismiss_client 在 client.dismissable=false 时拒绝：游戏不让遣散，
       按钮点击返回成功是假象（r65 连续 4 次"成功"送客但人没走）。
    """
    action = str((decision or {}).get("action") or "")
    if not isinstance(observation, dict):
        return None
    vi = observation.get("visible_info")
    vi = vi if isinstance(vi, dict) else observation
    if action == "dialog_advance":
        dialog = vi.get("dialog")
        if isinstance(dialog, dict) and dialog.get("is_end") is True:
            return ("⚠️ 对话已结束（dialog.is_end=true）——继续 dialog_advance 会打断"
                    "顾客离店协程，特殊 NPC（治安盘查员等）会永久卡店。"
                    "等待顾客自行离店；界面残留可用 click_button 处理")
    if action == "dismiss_client":
        client = vi.get("client")
        if isinstance(client, dict) and client.get("dismissable") is False:
            return ("⚠️ 该顾客不可遣散（dismissable=false，重要人物/盘查员）——"
                    "送客按钮无效（点击无反应）。走对话流程直至其自行离开；"
                    "在其离店前打烊会被游戏拒绝")
    return None


# ---------------------------------------------------------------- 卷帘门幂等守卫（v0.9.13）

# 门状态类动作的目标态：open_shutter 要的是「开」（shutter_closed=False），
# close_shutter 要的是「关」（shutter_closed=True）。门已处于目标态时，
# 重复调用不仅无意义，还会出事故——r49 实测打烊后连点 open_shutter ×5，
# 游戏的 OpenShutter() 在已开门状态下会来回翻转 shutter_closed
# （签名 True/False 抖动），v0.9.8「零变化 streak」追踪器全程失明，
# AI 就这样空转烧掉 5 步。
_SHUTTER_TARGET_CLOSED: dict[str, bool] = {
    "open_shutter": False,   # 开门：目标态=开（shutter_closed=False）
    "close_shutter": True,   # 关门：目标态=关（shutter_closed=True）
}


def _shutter_guard(
    decision: dict[str, Any], state_signature: Optional[tuple]
) -> Optional[str]:
    """卷帘门幂等守卫（v0.9.13，r49 修复 A 配套）：门已处于目标态 → 业务失败。

    数据源：state_signature 元组第 4 位（shutter_closed，见
    structured.channels.state_signature）——与 /state 实时同步，比观察文本可靠。
    签名不可用（vision 通道 / 离线测试）时返回 None 放行，与库存守卫同思路。

    返回非 None 为拦截原因（调用方构造业务失败、不调桥接）。
    """
    action = str((decision or {}).get("action") or "")
    if action not in _SHUTTER_TARGET_CLOSED:
        return None
    if not state_signature or len(state_signature) < _SIG_SHUTTER_CLOSED + 1:
        return None
    shutter_closed = state_signature[_SIG_SHUTTER_CLOSED]
    if shutter_closed is None:
        return None
    if bool(shutter_closed) == _SHUTTER_TARGET_CLOSED[action]:
        if action == "open_shutter":
            return (
                "⚠️ 门卫：卷帘门已是开启状态——无需重复 open_shutter"
                "（重复调用会让门状态抖动、浪费步数），应 call_next 叫号迎客"
            )
        return (
            "⚠️ 门卫：卷帘门已是关闭状态——无需重复 close_shutter，"
            "应 end_day 打烊或进入夜晚流程（open_map / stay_home）"
        )
    return None


# ---------------------------------------------------------------- 售前类别校验（v0.9.14）

def _category_guard(decision: dict[str, Any], observation: Any) -> Optional[str]:
    """sell_item 售前类别校验（v0.9.14，r51 催生）：顾客想收的类别与物品类别
    无交集 → 本地业务失败，不调桥接。

    背景（r51 实测）：治安部征用官只收 [FOOD, MEDICAL]，库存食物卖光后 AI 仍对
    水质检测试剂盒/厕纸连发 sell_item ×10，每次桥接跑一趟游戏逻辑才返回
    「类别不匹配」——现金零变化、步骤全废，顾客还卡在店里不走。

    数据源（r52 实测修正：bridge 观察契约把 client/inventory 放在 **visible_info**
    内，不在顶层——与 _inventory_guard 同路径）：
    - observation["visible_info"]["client"]：buying_tags / buying_ids / is_buying；
    - observation["visible_info"]["inventory"]["owned"]：物品 identifier + item_types
      （压缩规则 value>0，见 observer._compact_inventory）。

    两级校验：
    A. 指定物品在清单里 → 类别无交集且非顾客点名 → 拦（附具体类别对照）；
    B. 清单里没有任何一件匹配顾客想收的类别 → 卖啥都白搭，一并拦
       （防物品没进压缩清单但 bridge 找得到的漏网，如 r52 的 water_test_container）。
    信息缺失（无顾客/不在买/无类别偏好/清单为空）时一律放行，绝不误拦。

    返回非 None 为拦截原因（调用方构造业务失败、不调桥接）。
    """
    action = str((decision or {}).get("action") or "")
    if action != "sell_item":
        return None
    if not isinstance(observation, dict):
        return None
    vi = observation.get("visible_info")
    vi = vi if isinstance(vi, dict) else {}
    client = vi.get("client") if isinstance(vi.get("client"), dict) else observation.get("client")
    if not isinstance(client, dict) or not client.get("is_buying"):
        return None
    # v2.4：游戏本体（IsClientBuyingThisItem）已经给出可卖清单时，一切以它为准。
    # 标签法对没有 item_types 的物品（活鱼 tilapia / sturgeon）永远算不出交集，
    # 会把「顾客确实想买鱼」误判成「无货可卖」，逼 AI 送客——养殖链因此无法变现。
    game_sellable = [
        str(it.get("identifier"))
        for it in (vi.get("sellable_now") or [])
        if isinstance(it, dict) and it.get("identifier")
    ]
    if game_sellable:
        return None
    tags = {str(t).strip().upper() for t in (client.get("buying_tags") or []) if str(t).strip()}
    if not tags:
        return None  # 顾客没有类别偏好：游戏自己判定
    inv = vi.get("inventory") if isinstance(vi.get("inventory"), dict) else observation.get("inventory")
    owned = inv.get("owned") if isinstance(inv, dict) else None
    if not isinstance(owned, list) or not owned:
        return None
    buying_ids = {str(x) for x in (client.get("buying_ids") or [])}

    def _types(item: dict[str, Any]) -> set[str]:
        return {str(t).strip().upper() for t in (item.get("item_types") or []) if str(t).strip()}

    cname = client.get("name") or client.get("identifier") or "当前顾客"
    tags_text = ",".join(sorted(tags))

    # A. 指定物品的类别校验（物品在清单里才查；不在清单的交库存守卫/游戏判定）
    ident = str(((decision.get("params") or {}).get("identifier")) or "").strip()
    if ident:
        item = next(
            (it for it in owned if isinstance(it, dict) and str(it.get("identifier")) == ident),
            None,
        )
        if item is not None:
            item_types = _types(item)
            if item_types and not (item_types & tags) and ident not in buying_ids:
                return (
                    f"⛔ 类别不匹配（售前校验）：{cname} 只收 [{tags_text}]，"
                    f"而 {item.get('name') or ident} 是 [{','.join(sorted(item_types))}]——"
                    f"不必调桥接空跑；若库存中已无匹配类别物品，请 dismiss_client 送客换人，"
                    f"不要对其它不匹配物品重复 sell_item"
                )

    # B. 兜底：清单里没有任何一件匹配顾客想收的类别（含点名）→ 卖啥都白搭
    has_match = any(
        (_types(it) & tags) or str(it.get("identifier")) in buying_ids
        for it in owned if isinstance(it, dict)
    )
    if not has_match:
        return (
            f"⛔ 无货可卖（售前校验）：{cname} 只收 [{tags_text}]，"
            f"库存清单里没有任何匹配类别的物品——请 dismiss_client 送客换人"
            f"（想留客就 phone_order 补对应类别的货），不要对不匹配物品重复 sell_item"
        )
    return None


# ---------------------------------------------------------------- 交租储备红线守卫（v0.9.17）

# 交租日前 N 天内，预计花费会让现金低于租金的进货/收购动作 → 业务失败。
# r55 实测催生：现金 213 / 租金 210（2 天后），AI 对 150 批发买入照点——
# 买完只剩 63，交租日直接软锁（租金不够→end_day 被守卫拒→天数卡死）。
# 只管两条大额支出路径（批发买入 click_button AcceptWholesale / 顾客在卖时
# accept_offer）；buy_item 小额收购不拦（真差几块钱时卖货比拦购更优先）。
_RENT_RESERVE_DAYS = 2
_RENT_SPEND_ACTION_HINT = (
    "先卖货回笼资金（sell_item 成交 / 批发卖出），或准备迎接交租日（pay_rent）"
)


def _parse_leading_int(text: Any) -> Optional[int]:
    """从 '150<sprite ...> (-25%)' 这类文案提取前导整数；失败返回 None。"""
    m = re.match(r"\s*(\d+)", str(text or ""))
    return int(m.group(1)) if m else None


def _guard_log(message: str) -> None:
    """守卫侧的可观测日志（stderr，带时间戳，best-effort）。

    守卫只能返回「拦截原因 or None」，放行时没有任何输出通道——而
    「为什么放行」恰恰是复盘时最需要的信息（本项目三次「守卫恒放行」事故
    都是因为放行时静默无声）。这里与 brain/client.py:_log 同一写法：
    写 stderr，不影响决策链、不进提示词、失败即忽略。
    """
    try:
        stamp = time.strftime("%H:%M:%S")
        print(f"[guard {stamp}] {message}", file=sys.stderr, flush=True)
    except Exception:  # noqa: BLE001 —— 日志绝不能影响主流程
        pass


def _enrich_dialog_choice(decision: dict[str, Any], observation: Any) -> None:
    """把 dialog_choose 的「序号」换成**实际选项文字**（v2.9，原地修改）。

    背景：模型经常只给 `index`（实测交租时给 index=0），而按序号点选项是历史事故源——
    r75 盲选 index 0 走进「延期交租」分支，房东卡店、打烊不了。
    观察里本来就有每个选项的文字（dialog.choices），据此反查后改发文字：
    桥接按文字匹配，序号错位也点不歪；日志里也能直接看出选了哪句。
    """
    if str((decision or {}).get("action") or "") != "dialog_choose":
        return
    params = decision.setdefault("params", {})
    if params.get("text") or "index" not in params:
        return
    vi = observation.get("visible_info") if isinstance(observation, dict) else None
    vi = vi if isinstance(vi, dict) else {}
    dialog = vi.get("dialog") if isinstance(vi.get("dialog"), dict) else {}
    choices = dialog.get("choices") or []
    try:
        idx = int(params.get("index") or 0)
    except (TypeError, ValueError):
        return
    for c in choices:
        if not isinstance(c, dict):
            continue
        try:
            cidx = int(c.get("index", -1))
        except (TypeError, ValueError):
            continue
        if cidx != idx:
            continue
        text = str(c.get("text") or "").strip()
        if text:
            params["text"] = text
            params.pop("index", None)
        return


def _stalled_guard(decision: dict[str, Any], observation: Any) -> Optional[str]:
    """拦下「必然空转」的决策（v2.8）。

    两类实盘事故催生：
    1) wait 等顾客——顾客的需求/预算不会自己变。实测第 5 天一步选 wait{1.5}，
       局面一模一样，白烧一步（提示词早就写着"绝不用 wait"，模型仍照选）。
    2) click_button 猜「打发 / 遣散 / 送走 / 叫号 / 打烊」这类文字——这些都有
       专用动作。实测连猜「打发走当前顾客」5 次全失败，5 步全废。

    只拦「确认是死局」的情形，宁放不误拦：
    - wait：必须有顾客 + 对话已结束 + sellable_now 为空（三条同时成立才没有出路）；
    - click_button：只按关键词识别，且给出去向明确的替代动作。
    """
    action = str((decision or {}).get("action") or "")
    if action not in ("wait", "click_button"):
        return None
    if not isinstance(observation, dict):
        return None
    vi = observation.get("visible_info")
    vi = vi if isinstance(vi, dict) else {}

    if action == "wait":
        client = vi.get("client") if isinstance(vi.get("client"), dict) else {}
        dialog = vi.get("dialog") if isinstance(vi.get("dialog"), dict) else {}
        sellable = vi.get("sellable_now") or []
        if not client or not dialog.get("is_end") or sellable:
            return None
        who = client.get("name") or client.get("identifier") or "当前顾客"
        return (
            f"死局：顾客【{who}】要的货你一件都没有（sellable_now 为空），wait 不会改变任何东西。"
            "→ 立刻 dismiss_client 换下一位（想直接清场就 force_dismiss）"
        )

    params = decision.get("params") or {}
    blob = f"{params.get('text') or ''} {params.get('name') or ''}"
    for kw, suggest in (
        ("打发", "dismiss_client"),
        ("遣散", "dismiss_client"),
        ("送走", "dismiss_client"),
        ("呼叫", "call_next"),
        ("叫下", "call_next"),
        ("下一位", "call_next"),
        ("打烊", "end_day"),
        ("关门", "end_day"),
    ):
        if kw in blob:
            return (
                f"click_button 不能用来「{kw}」：请用专用动作 {suggest}。"
                "猜 tooltip 会连续空转（实盘曾连猜「打发走当前顾客」5 次全失败）。"
            )
    return None


def _rent_reserve_guard(
    decision: dict[str, Any],
    observation: Any,
    state_signature: Optional[tuple],
) -> Optional[str]:
    """交租储备红线（v0.9.17，r55 催生）：快交租了还大笔花钱 → 业务失败。

    触发条件（全部满足）：交租日 ≤ _RENT_RESERVE_DAYS 天内；动作属大额支出；
    预计花费明确；花费后现金 <  rent_value。信息缺失时放行（不误拦）。

    返回非 None 为拦截原因（调用方构造业务失败、不调桥接）。
    """
    action = str((decision or {}).get("action") or "")
    params = (decision or {}).get("params") or {}
    is_wholesale_buy = False
    is_offer_buy = False
    if action == "click_button":
        name = str(params.get("name") or "").lower()
        text = str(params.get("text") or "").lower()
        is_wholesale_buy = "wholesale" in name or "批发" in text
    elif action == "accept_offer":
        is_offer_buy = True  # 是否支出方向由下面 client.is_selling 判定
    # v1.8：buy_item（直接收购）/ buy_table（收桌上货）同样是现金支出——
    # r97 实测 AI 在租金到期前 2 天花 100 走 buy_item，绕过了批发/报价两条路径，
    # 现金 307→207 < 租金 210，交租日直接爆。这里一并纳入红线。
    is_direct_buy = action in ("buy_item", "buy_table")
    if not is_wholesale_buy and not is_offer_buy and not is_direct_buy:
        return None

    if not isinstance(observation, dict) or not state_signature:
        return None
    if len(state_signature) < _SIG_CASH + 1 or state_signature[_SIG_CASH] is None:
        return None
    cash = state_signature[_SIG_CASH]
    vi = observation.get("visible_info")
    vi = vi if isinstance(vi, dict) else {}
    rent_info = vi.get("rent")
    rent_info = rent_info if isinstance(rent_info, dict) else {}
    day_until = rent_info.get("day_until_rent")
    rent_value = rent_info.get("rent_value")
    if day_until is None or rent_value is None:
        return None
    # v1.5：真实应缴额（rent_due = temporaryRent，含延期累计/罚金）优先——
    # r78 实证：名义 242，实际应交 693，按名义值做红线会放行「看似够交」的支出
    rent_due = rent_info.get("rent_due")
    if isinstance(rent_due, (int, float)) and rent_due > 0:
        rent_value = max(int(rent_value), int(rent_due))

    # 预计花费：批发买入看 table.wholesale_cost（BridgeApi.cs:1234 发射，
    # 非批发报价时为 null）；accept_offer 仅在顾客在卖时算支出
    table = vi.get("table")
    table = table if isinstance(table, dict) else {}
    cost: Optional[int] = None
    if is_wholesale_buy:
        cost = _parse_leading_int(table.get("wholesale_cost"))
    elif is_direct_buy:
        cost = None
        if action == "buy_item":
            ident = str(params.get("identifier") or "")
            for it in (vi.get("inventory") or {}).get("not_owned") or []:
                if not isinstance(it, dict):
                    continue
                if str(it.get("identifier") or "") == ident:
                    val = it.get("value")
                    cost = int(val) if isinstance(val, (int, float)) else None
                    break
        else:  # buy_table：桌上物品的议价
            value = table.get("negociated_value")
            cost = int(value) if isinstance(value, (int, float)) else None
        if cost is None or cost <= 0:
            return None
    else:
        client = vi.get("client")
        client = client if isinstance(client, dict) else {}
        if not client.get("is_selling"):
            return None  # 顾客在买：accept_offer 是收款，不是支出
        value = table.get("negociated_value")
        cost = int(value) if isinstance(value, (int, float)) else None

    # —— v3.7 批发分支保守兜底（task-8 / CR-T04-2，r79 事故回归，P0）——
    # 事故：批发买入是唯一能一次性花掉大量现金的路径，却恰好是唯一没有租金红线
    # 保护的路径。根因是「花费不明 → 放行」这一条对批发分支等价于「花 0 元」：
    #   · 事故当时 ``BuildTable`` 不发射 ``wholesale_cost``，于是 cost 恒 None →
    #     落到下面的「花费不明：不误拦」→ 直接放行；
    #   · r79 实证：现金 442 < 真实应缴 693 时照点批发，出 335 进货 → 现金 107，
    #     交租日必爆；「延期」还会累计欠款让 rent_due 越滚越大（242→254→693）。
    # 因此：批发价不可得时**不能默认「花 0 元」**，改为最保守判定——只要现金已经
    # 付不起本期真实房租（rent_value 已被上面的 rent_due 抬过，口径同 v1.5b），
    # 就禁止任何采购；现金够了则放行但仍记录「批发价未知」（下次复盘能找到它）。
    # **2026-11-15 现状**：CR-T04-1 已兑现——``mod/src/BridgeApi.cs:1234`` 的
    # ``BuildTable`` 现在会发射 ``wholesale_cost``（源 ps.GetWholesalePrice()，
    # 非批发报价时发 null）。所以正常批发轮的 cost 已可得，本兜底只在
    # 「字段缺失 / 为 null / 为 0」时命中，即**真正的不可得**，不再常态化。
    # 保留本兜底的理由：``wholesale_cost`` 可能为 null（未进入批发报价阶段即
    # 点批发），此时仍不能当成 0 元放行。其余三条路径（buy_item / buy_table /
    # accept_offer）的判定语义完全不变。
    if is_wholesale_buy and (cost is None or cost <= 0):
        try:
            cash_i, floor_i = int(cash), int(rent_value)
        except (TypeError, ValueError):
            # 现金/房租无法转成整数 = 信息不明，沿用本守卫一贯的「缺失即放行」，
            # 不在这里抛异常打断决策链（交租日 end_day 守卫仍会兜底）。
            return None
        if cash_i < floor_i:
            return (
                f"⚠️ 现金 {cash_i} 已低于应交房租 {floor_i}，"
                f"且本次批发花费不可知（table.wholesale_cost 缺失或为 null）"
                f"——禁止任何采购，先卖货回血（sell_item），等现金 ≥ {floor_i} 再进货。"
                f"{_RENT_SPEND_ACTION_HINT}"
            )
        _guard_log(
            f"批发价不可得（table.wholesale_cost 缺失或为 null）——现金 {cash_i} ≥ "
            f"应交房租 {floor_i}，无法预估本次花费，放行"
        )
        return None

    if cost is None or cost <= 0:
        return None  # 花费不明：不误拦（交租日 end_day 守卫仍会兜底）

    # v1.5b：现金已低于真实应缴（rent_due）→ 任何采购都禁止，先卖货回血。
    # r79 实测：现金 442 < rent_due 693 时仍批发出 335 进货 → 现金 107，
    # 交租日必爆；而「延期」会累计欠款让 rent_due 越滚越大（242→254→693）。
    # 与剩余天数无关——只要付不起本期真实房租，就不允许再花钱进货。
    if isinstance(rent_due, (int, float)) and rent_due > 0 and int(cash) < int(rent_due):
        return (
            f"⚠️ 现金 {int(cash)} 已低于真实应缴房租 {int(rent_due)}——禁止任何采购，"
            f"先卖货回血（sell_item），等现金 ≥ {int(rent_due)} 再进货。{_RENT_SPEND_ACTION_HINT}"
        )

    if int(day_until) > _RENT_RESERVE_DAYS:
        return None  # 交租日还远：上面的「付不起本期房租」已拦，其余放行

    # v1.8：留一点缓冲（租金会逐期上涨），临近交租（≤1 天）多留 10%
    buffer = int(rent_value * 0.1) if int(day_until) <= 1 else 0
    if int(cash) - cost >= int(rent_value) + buffer:
        return None  # 花得起：放行
    return (
        f"⚠️ 交租储备红线：{day_until} 天后交租 {rent_value}，本次花费约 {cost} "
        f"后现金将只剩 {int(cash) - cost}（不够交租）——禁止这笔支出。{_RENT_SPEND_ACTION_HINT}"
    )


# ---------------------------------------------------------------- 终局风险守卫（T06-P0，v3.9）

# 为什么需要这一条：交租日到了、宽限用完、现金又付不起时，模型**唯一的可选项就是
# 「拒付租金」**——那个按钮点下去是整局结束，不是「再拖欠一期」。
#   · 房东对话分支 StoreClientList.cs:2650-2686：`if (!NetworkUpgrade.IsLandlordGraceUnlocked())`
#     → dialog_landlord_no_grace_1/2 → `PlayerStore.Instance.ExecuteGameOver()`（:2661，游戏结束）；
#     有宽限才走 dialog_landlord_grace_1..5 正常离场。
#   · 「付租」选项带 CanPayRent() 前置（StoreClientList.cs:2637），现金不足时它**根本不出现**；
#     「拒付」无前置（:2650）——所以「付不起」的当下，屏幕上的选项天然只剩那条死路。
#   · OnRentNotPaid() 在 if/else 之外无条件执行（:2685），即无论有没有宽限，本期都变成
#     「欠款 + 房租照涨」，只有结局那一步才取决于宽限。
#
# 机制事实（反编译核实；docs/25 与 docs/26 里「宽限次数」的说法是错的，宽限不是计数器）：
#   · 宽限是 NetworkUpgrade 的 state 状态机：`NetworkUpgrade.cs:873-880 IsUnlocked()`
#     只在 `state == 2` 返 true；`:854 IsReady()` 要求 `state == 1`
#     ⇒ state==1 = 可买/未持有，state==2 = 已持有/已解锁。
#   · 建造时 NetworkUpgradeList.cs:678-689 CreateLandlordGrace()：cost=100、
#     isRepeatable=true、state=2、cooldownDuration=15、heldUntilUsed=true。
#   · 开局是否自带由难度决定：StoreService.cs:218
#     `state = ((!rules.landlordGraceOwnedAtStart) ? 1 : 2)`；
#     Difficulty.cs:212/256 cozy/normal 为 true，:300/344 hard/challenge 为 false。
#   · **用掉宽限 = 被强制写回可买态**：PlayerStore.cs:1490-1497 OnRentNotPaid()
#     第一句就是 `GetUpgradeById("LANDLORD_GRACE").state = 1`。
#   · 判定口径必须**相加**：CanPayRent()（PlayerStore.cs:1470-1477）=
#     `playerCash >= rentValue + temporaryRent`，OnRentPaid()（:1479-1488）也是
#     `playerCash -= (rentValue + temporaryRent)`。注意这与 _rent_reserve_guard
#     的 max(rent_value, rent_due) 口径不同——那边是「攒钱目标」，这边是「实际要掏的钱」。
#
# ⚠️ 宽限状态目前**不可观测**（需 C# 侧补 /state.rent.grace）：BridgeApi.cs:420-435 的
# /state 只发 cash/store_state/day/day_until_rent/rent_value/rent_due/can_pay_rent/
# is_power_on/garbage_level，顶层 34 键里没有任何 grace 相关字段。因此本守卫按
# **最坏情况（无宽限）**判定：宁可早拦不可晚拦——拦错的代价是模型多花一步去卖货，
# 漏拦的代价是整局游戏结束、且不可恢复。
_DEFEAT_RISK_ALLOWED_ACTIONS = ("sell_item", "sell_table", "dialog_choose", "pay_rent", "wait")


def _defeat_risk_guard(
    decision: dict[str, Any],
    observation: Any,
    state_signature: Optional[tuple],
) -> Optional[str]:
    """终局风险守卫（T06-P0，v3.9）：过了交租日 + 付不起 + 无宽限 ⇒ 禁止一切非救命动作。

    触发条件（全部满足）：
      1. `rent.day_until_rent <= 0`（今天就是交租日，或已经拖欠）；
      2. `rent.can_pay_rent is False`（= C# `CanPayRent()` 原值，即 cash < rent_value+rent_due）；
      3. 若 state_signature 里现金可读，则要求 `cash < rent_value + rent_due` 也成立
         （双确认，防止 can_pay_rent 因降级/过期而误报）。
    放行五种「救命动作」：sell_item / sell_table / dialog_choose / pay_rent / wait。
    其中 pay_rent 与 dialog_choose 必须放行——房东在场时付租是走房东对话选项的，
    拦掉它们等于让 AI 无路可走（这与 r75「盲选 index 0 走进延期分支」不冲突：
    _enrich_dialog_choice 已把序号换成文字，且提示词要求只选含「交/付/租金」的选项）。

    返回非 None 为拦截原因（调用方构造业务失败、不调桥接）。
    """
    action = str((decision or {}).get("action") or "")
    if action in _DEFEAT_RISK_ALLOWED_ACTIONS:
        return None
    if not isinstance(observation, dict):
        return None
    vi = observation.get("visible_info")
    vi = vi if isinstance(vi, dict) else {}
    rent_info = vi.get("rent")
    rent_info = rent_info if isinstance(rent_info, dict) else {}

    day_until = rent_info.get("day_until_rent")
    if not isinstance(day_until, (int, float)) or isinstance(day_until, bool):
        return None
    if int(day_until) > 0:
        return None  # 交租日还没到：本期还不起也还有时间卖货，交给 _rent_reserve_guard 管

    # 主判据：can_pay_rent 是 C# CanPayRent() 的原值，权威口径。
    can_pay = rent_info.get("can_pay_rent")
    if can_pay is not False:
        # 缺失（降级 / 老版本 mod）时无法判定终局风险，沿用守卫一贯的「缺失即放行」，
        # 但必须留痕：交租日已到却判不了，是复盘时最该看到的一行。
        _guard_log(
            f"终局风险守卫：day_until_rent={int(day_until)} 已到/过了交租日，"
            f"但 rent.can_pay_rent={can_pay!r} 不可用（降级或字段缺失）——无法判定，放行"
        )
        return None

    # —— 双确认：把「要掏的钱」自己算一遍（rent_value + rent_due，不是取 max）——
    rent_value = rent_info.get("rent_value")
    rent_due = rent_info.get("rent_due")
    total: Optional[int] = None
    try:
        total = int(rent_value or 0) + int(rent_due or 0)
    except (TypeError, ValueError):
        total = None

    cash: Optional[int] = None
    if state_signature and len(state_signature) >= _SIG_CASH + 1:
        raw_cash = state_signature[_SIG_CASH]
        if isinstance(raw_cash, (int, float)) and not isinstance(raw_cash, bool):
            cash = int(raw_cash)

    if cash is not None and total is not None and cash >= total:
        # can_pay_rent 说付不起、但现金自算明明够 → 以可复算的证据为准，不拦。
        _guard_log(
            f"终局风险守卫：can_pay_rent=false 与现金自算矛盾（现金 {cash} ≥ 应缴 {total}）"
            f"——按可复算证据放行（can_pay_rent 可能来自降级块或已过期）"
        )
        return None

    if int(day_until) < 0:
        day_text = f"已拖欠租金 {-int(day_until)} 天"
    else:
        day_text = "今天就是交租日"

    if cash is not None and total is not None:
        gap = total - cash
        money_text = (
            f"现金 {cash}，应交 {total}"
            f"（本期 rent_value {rent_value or 0} + 累计欠款 rent_due {rent_due or 0}），还差 {gap}"
        )
        goal_text = f"凑够 {total}"
    else:
        money_text = "现金或应缴额本轮读不到具体数字（状态降级），但 can_pay_rent=false 已说明付不起"
        goal_text = "凑够应交总额"

    reason = (
        f"🚨 终局风险（不可恢复）：{day_text}，而{('你' + money_text)}。"
        "此刻房东对话里「付租」选项根本不会出现（现金不足时游戏不给这个选项），"
        "屏幕上剩下的就是「拒付租金」——而没有房东宽限时，"
        "**拒付 = 立刻游戏结束**（不是再拖欠一期）。宽限是否还在当前 /state 里读不到，"
        "因此必须按最坏情况（已经没有宽限）处理。"
        f"唯一出路只有两条：① 把东西卖成现金（sell_item / sell_table），{goal_text} 后再交租；"
        "② 现在就去点 pay_rent（或房东对话里选含「交/付/租金」的那一项）。"
        "**绝对不要选「拒付 / 不交租金」**，也不要再去进货或打烊。"
        f"{_RENT_SPEND_ACTION_HINT}"
    )
    _guard_log(
        f"终局风险守卫拦截 action={action!r}：{day_text}、can_pay_rent=false、"
        f"cash={cash} total={total}"
    )
    return reason


# ---------------------------------------------------------------- 成交后自动收纳判定（v0.9.18）

# 批发买入成交的按钮名/文本特征（点击成交路径，见 v0.9.9）
_WHOLESALE_ACCEPT_HINTS = ("wholesale", "接受批发")


#: 这些动作之后前台柜台可能留有物品，需要自动搬回储物区（v3.16）。
#: dismiss_client：顾客走了但收购的货还在柜台；
#: end_day：打烊前不清空会让物品滞留在前台；
#: accept_offer / buy_wholesale / accept_barter：成交后货落在前台。
_CLEANUP_AFTER_ACTIONS = frozenset({
    "dismiss_client", "end_day", "accept_offer", "buy_wholesale", "accept_barter",
    # call_next：叫下一位之前必须把上一位的残留清掉，否则新顾客一进门就看到旧货
    #（实机踩到：顾客走后 negociated 还挂着 pipe_weapon，接着叫客会串台）。
    "call_next",
})


def _needs_stow_after(decision: dict[str, Any]) -> bool:
    """成交后是否需要自动收纳（v0.9.18）：批发买入成交 → 把买入物品搬回储物区。

    背景（用户 2026-10-17 指令「批发之后的物品要放到储物区」+ r55 实锤）：
    批发买入的物品落在前台柜台（front inv），柜台占满后下一位顾客带来的
    物品会被游戏**直接销毁**（日志：Destroying unplaced item (front counter
    full)——r55 一战毁了 inventor 的 12 件物品）。收纳手段 = transferButton
    （按钮文本「转移所有物品」，实测把前台柜台的自有物品搬回储物区）。
    """
    action = str((decision or {}).get("action") or "")
    if action != "click_button":
        return False
    params = (decision or {}).get("params") or {}
    name = str(params.get("name") or "").lower()
    text = str(params.get("text") or "")
    return any(h in name for h in _WHOLESALE_ACCEPT_HINTS) or "接受批发" in text


def _needs_cleanup_after(decision: dict[str, Any]) -> bool:
    """这些动作之后要尝试「清柜 + 搬回储物区」（v3.16）。

    与 ``_needs_stow_after`` 的区别：那个是「批发成交后把买入的货搬走」，
    靠动作语义判定；本函数是**场景清理**——顾客离开/成交/打烊后，前台柜台
    和议价标记都可能留下残留，不清会让 UI 看起来"桌上的东西一直不动"，
    严重时下一位顾客的物品会被游戏销毁（r55 毁 12 件）。

    用户 2026-11-22 实机反馈「桌子上面的东西怎么一直在桌上，可以拿到仓库」即此问题。
    """
    action = str((decision or {}).get("action") or "")
    if action in _CLEANUP_AFTER_ACTIONS:
        return True
    return _needs_stow_after(decision)


# ---------------------------------------------------------------- 夜晚界面幂等守卫（v0.9.16）


def _night_ui_guard(decision: dict[str, Any], state_signature: Optional[tuple]) -> Optional[str]:
    """夜晚界面幂等守卫（v0.9.16，r54 催生）：界面已处于目标态 → 业务失败。

    r54 实测：夜间地图打开后 AI 连续 open_map ×5——地图开着还「打开地图」，
    bridge 每次返回 ok（OpenUI 幂等）但零推进，20 步批次耗光卡在出门前夜。
    与卷帘门守卫同思路：用 state_signature 的实时字段做前置校验，不调桥接。
    签名不可用（vision 通道/离线）时放行。

    返回非 None 为拦截原因（调用方构造业务失败、不调桥接）。
    """
    action = str((decision or {}).get("action") or "")
    if not state_signature or len(state_signature) <= _SIG_MAP_OPEN:
        return None
    if action == "open_map" and state_signature[_SIG_MAP_OPEN] is True:
        return (
            "⚠️ 夜间地图已打开——不要再调 open_map（空转，r54 实测卡死 5 步）。"
            "直接 go_out 选择去向（杰克逊博士=upgrade / 配给站=commissary / "
            "垃圾倾倒场=scavenging）或 stay_home 睡觉"
        )
    if action == "go_out" and state_signature[_SIG_IS_OUTSIDE] is True:
        return "⚠️ 人已在外出地点——先 leave_outside 返回店铺，再决定 stay_home 睡觉"
    if action == "leave_outside" and state_signature[_SIG_IS_OUTSIDE] is False:
        return "⚠️ 人已回到店铺（未在外出）——无需 leave_outside；直接 stay_home 睡觉进入次日"
    return None


# ---------------------------------------------------------------- 动画等待闸门（v0.8.5）

# 参与「在途动画」判定的日志事件（见 statewatch.events 的事件类型）
_ANIM_ACTION_START = ("action_started",)
_ANIM_ACTION_END = ("action_ended",)
_ANIM_MOVE_START = ("client_approaching",)
_ANIM_MOVE_END = ("client_arrived", "client_left")


_EXPERIENCE_STORE: Optional["ExperienceStore"] = None
_EXPERIENCE_PATH: Optional[str] = None


def _experience_store(config: dict[str, Any]) -> Optional["ExperienceStore"]:
    """按配置返回经验库单例；``experience.enabled=false`` 时返回 None。

    单例是为了避免每一步都重读 JSONL（文件会随运行变长）。
    路径变化时重建，便于测试注入临时目录。
    """
    global _EXPERIENCE_STORE, _EXPERIENCE_PATH
    cfg = config.get("experience") or {}
    if cfg.get("enabled") is False:
        return None
    try:
        path = Path(cfg.get("path") or (artifacts_dir(config) / "experiences.jsonl"))
    except (OSError, TypeError, ValueError):
        return None
    if _EXPERIENCE_STORE is None or str(path) != _EXPERIENCE_PATH:
        _EXPERIENCE_STORE = ExperienceStore(path)
        _EXPERIENCE_PATH = str(path)
    return _EXPERIENCE_STORE


def _read_state_signature(bridge_client: Any) -> Optional[tuple]:
    """读取一次 /state 并计算变化签名；桥不可达/异常时返回 None（调用方应放行）。"""
    try:
        state = bridge_client.get_state()
    except BridgeError:
        return None
    if not isinstance(state, dict):
        return None
    return state_signature(state)


def wait_for_state_settle(
    config: dict[str, Any],
    bridge_client: Any,
    *,
    stable_reads: Optional[int] = None,
    probe_interval: Optional[float] = None,
    timeout_seconds: Optional[float] = None,
) -> tuple[bool, float, str]:
    """等待游戏状态稳定（连续 stable_reads 次 /state 签名一致才算「可以安全操作」）。

    「动画等待闸门」的实时通道（v0.8.5）：游戏动画/流程进行中时 /state 的关键字段
    （顾客、队列、交易桌、对话、现金等）会持续变化——签名稳定即视为动画已播完。
    与日志通道（pending_animation_reason）互补：本通道无日志写盘延迟问题。

    桥不可达 / 读取异常时直接放行（settled=True, waited=0）——绝不阻塞主循环。

    Returns:
        (settled, waited_seconds, reason)；settled=False 表示超时放行（状态持续变化）。
    """
    gate_cfg = config.get("action_gate", {}) or {}
    if stable_reads is None:
        stable_reads = int(gate_cfg.get("state_stable_reads", 2))
    if probe_interval is None:
        probe_interval = float(gate_cfg.get("state_probe_interval_seconds", 1.0))
    if timeout_seconds is None:
        timeout_seconds = float(gate_cfg.get("settle_timeout_seconds", 30.0))
    probe_interval = max(0.05, probe_interval)

    prev = _read_state_signature(bridge_client)
    if prev is None:
        return True, 0.0, ""  # 桥不可达：不阻塞
    started = time.time()
    stable = 1
    while True:
        if time.time() - started >= timeout_seconds:
            return False, time.time() - started, "游戏状态持续变化中（超时放行）"
        time.sleep(probe_interval)
        cur = _read_state_signature(bridge_client)
        if cur is None:
            return True, time.time() - started, ""  # 桥断开：放行
        if cur == prev:
            stable += 1
            if stable >= max(1, stable_reads):
                return True, time.time() - started, ""
        else:
            stable = 1
            prev = cur


def _event_age_seconds(ts: str, now: datetime) -> Optional[float]:
    """日志事件时间（HH:MM:SS）与当前时间的秒差；解析失败返回 None。"""
    try:
        t = datetime.strptime(ts, "%H:%M:%S")
    except (ValueError, TypeError):
        return None
    then = now.replace(hour=t.hour, minute=t.minute, second=t.second, microsecond=0)
    age = (now - then).total_seconds()
    if age < -5.0:  # 跨午夜（日志时间为昨天）：按新鲜处理
        return 0.0
    return max(0.0, age)


def _latest_unpaired(events: list[Any], start_kinds: tuple, end_kinds: tuple):
    """倒序扫描：返回最近一个「有 start 却无后续 end」的事件（未配对），否则 None。"""
    for event in reversed(events):
        if event.kind in end_kinds:
            return None
        if event.kind in start_kinds:
            return event
    return None


def pending_animation_reason(
    config: dict[str, Any],
    *,
    window_seconds: float = 15.0,
    now: Optional[datetime] = None,
) -> Optional[str]:
    """检查最新 GameLog：是否存在「在途动画」（最近 window_seconds 内未配对的动作/移动）。

    用于「动画等待闸门」：返回 None 表示可以安全执行下一个动作；
    返回字符串表示有动画进行中（应等待），字符串即原因描述。

    日志不可读（游戏未运行 / 离线环境 / 无数据目录）时一律返回 None——
    不阻塞、不影响 dry-run 与离线测试。
    """
    try:
        data_dir = resolve_data_dir(config)
        if data_dir is None:
            return None
        log_path = find_latest_gamelog(data_dir)
        if log_path is None:
            return None
        events = parse_log_lines(read_recent_lines(log_path, n=150))
    except OSError:
        return None
    if not events:
        return None

    now = now or datetime.now()

    for start_kinds, end_kinds, label in (
        (_ANIM_ACTION_START, _ANIM_ACTION_END, "动作动画"),
        (_ANIM_MOVE_START, _ANIM_MOVE_END, "顾客移动"),
    ):
        unpaired = _latest_unpaired(events, start_kinds, end_kinds)
        if unpaired is None:
            continue
        age = _event_age_seconds(unpaired.ts, now)
        if age is not None and age <= window_seconds:
            return f"{label}进行中（{unpaired.ts} {unpaired.message}，{age:.1f}s 前）"
        # 未配对但太旧：视为历史残留，不再等待
    return None


def wait_for_animation_settle(
    config: dict[str, Any],
    *,
    timeout_seconds: Optional[float] = None,
    poll_interval: Optional[float] = None,
    window_seconds: Optional[float] = None,
) -> tuple[bool, float, str]:
    """等待游戏内动画播完（GameLog 中无在途动作/移动）。

    Returns:
        (settled, waited_seconds, reason)
        settled=False 表示超时放行（reason 为超时时的挂起原因；不抛异常、绝不卡死）。
    """
    gate_cfg = config.get("action_gate", {}) or {}
    if timeout_seconds is None:
        timeout_seconds = float(gate_cfg.get("settle_timeout_seconds", 30.0))
    if poll_interval is None:
        poll_interval = float(gate_cfg.get("poll_interval_seconds", 1.0))
    if window_seconds is None:
        window_seconds = float(gate_cfg.get("window_seconds", 15.0))
    poll_interval = max(0.05, poll_interval)

    started = time.time()
    reason = pending_animation_reason(config, window_seconds=window_seconds)
    while reason:
        if time.time() - started >= timeout_seconds:
            return False, time.time() - started, reason
        time.sleep(poll_interval)
        reason = pending_animation_reason(config, window_seconds=window_seconds)
    return True, time.time() - started, ""


def _find_game_window_or_fail(config: dict[str, Any]):
    game_cfg = config.get("game", {})
    win = find_game_window(
        game_cfg.get("window_title_keywords", []),
        game_cfg.get("exclude_title_keywords", []),
        tuple(game_cfg.get("min_window_size", (400, 300))),
    )
    if win is None:
        raise WindowNotFoundError("未找到游戏窗口，无法执行控制动作。请确认游戏正在运行。")
    return win


# ---------------------------------------------------------------- 实时状态前置校验（v1.3）
#
# v1.3（2026-10-19，r68 实锤）：模型的观察是决策前的快照，而模型单步耗时可达
# 30~120 秒——期间游戏可能自行推进（EndOfDay 面板停留约 2 分钟后自动入睡，
# store_state CLOSED→MORNING）。此时按旧观察执行夜晚动作会**再睡一天**（r68
# 实测 Day 14→16，一次 end_day_confirm 叠加游戏自动入睡 = 跳天）。
# 因此所有桥接动作在真正下发前，都用 /state 实时状态做一次前置校验：
#   - 夜晚专属动作必须 store_state=CLOSED（否则拒绝，避免在次日早晨点睡袋/确认）
#   - 白天动作必须 store_state=OPEN（否则拒绝，避免在夜晚卖货/叫客）
# 校验失败返回业务失败（ok=false），由模型据 detail 重新决策，不抛异常。

# v3.8（CR-X01-3）：两张表改由 game_agent.contracts 派生（唯一真相，
# 判据分别是 ActionSpec.phase == "night" / == "day"）。原手工副本
# _NIGHT_ONLY_ACTIONS 7 项、_DAY_ONLY_ACTIONS 9 项，与派生值逐项相等。
# 派生时**不得改变**的既有语义（改了就是 r68 那次跳天事故的回归）：
#   - 这两张表判的是「**这一刻**能不能执行」，与 _NIGHT_PHASE_ACTIONS 的
#     「夜晚阶段模型能看见什么」是两套语义，不要合并、不要互换。
#   - phase == "both" 的动作（machine_put / machine_take / scavenge /
#     stow_loot / click_button 等）**不进任何一张表**——它们昼夜都能执行，
#     塞进 _NIGHT_ONLY_ACTIONS 会让 _pre_action_state_guard 在
#     store_state=CLOSED 之外误拒它们。
#   - begin_day 不在这两张表里（它有自己的"夜晚终点/逃生出口"语义，
#     见 _NIGHT_PHASE_ACTIONS 注释），不要顺手加进来。
_NIGHT_ONLY_ACTIONS: set[str] = derive_night_only()
_DAY_ONLY_ACTIONS: set[str] = derive_day_only()


def _live_store_state(bridge_client: Any) -> Optional[str]:
    """读桥接实时 store_state（大写）；失败返回 None（不阻断动作）。"""
    if bridge_client is None:
        return None
    try:
        state = bridge_client.get_state()
    except Exception:  # noqa: BLE001 - 桥接不可用时不做校验（由动作自身报错）
        return None
    if not isinstance(state, dict) or not state.get("in_game"):
        return None
    value = state.get("store_state")
    return str(value).upper() if value is not None else None


_CALENDAR_PANEL_HINT = "_AdvCalendar"


def _morning_report_guard(bridge_client: Any) -> Optional[dict[str, Any]]:
    """检测晨报结算面板（日历/夜间报告/状态）是否开着；开着则强制下发关闭动作（v1.6）。

    用户 2026-10-20 指令：次日早晨的「日历/夜间报告/状态」面板（右上角 ×）出现时
    必须第一时间叉掉——它挡住开店接待，拖久了会「开店迟到、错过顾客」。
    截图实证：面板开着时模型仍去交易（第 19 步还在收购），提示词约束不可靠，
    故用 GET /ui/buttons 硬检测（面板开着时才有 path 含 _AdvCalendar 的按钮）+
    直接下发 click_button {name: CloseButton, path: _AdvCalendar}，绕过模型决策。

    Returns:
        强制决策 dict；None 表示面板未开 / 检测不可用。
    """
    if bridge_client is None:
        return None
    try:
        payload = bridge_client.get_ui_buttons()
    except Exception:  # noqa: BLE001 - 检测失败不阻断正常流程
        return None
    buttons = (payload or {}).get("buttons") or []
    for button in buttons:
        try:
            path = str(button.get("path") or "")
            active = button.get("active", True)
        except AttributeError:
            continue
        if active and _CALENDAR_PANEL_HINT in path:
            return {
                "action": "click_button",
                "params": {"name": "CloseButton", "path": _CALENDAR_PANEL_HINT},
                "reason": "晨报结算面板（日历/夜间报告/状态）挡路——先叉掉再营业",
                "confidence": 0.96,
            }
    return None


def _at_scavenging_site(observation: dict[str, Any]) -> bool:
    """当前是否在拾荒地点（night.is_outside 且 current_location 是拾荒点）。

    地点值两版都认：正式版是地点 **id 字符串**（`dumping_ground`，见
    mod/src/BridgeApi.cs:732 用 `map.Current.Id` 生产 current_location、
    BridgeApi.cs:2816-2817 把 scavenging/拾荒 映射成 dumping_ground）；
    Demo 期是枚举名 `SCAVENGING`。
    """
    visible = (observation or {}).get("visible_info") or {}
    night = visible.get("night") or {}
    if not isinstance(night, dict):
        return False
    # v3.5 修正（事故 r55：一夜毁 12 件战利品）：此判定原先只比 Demo 枚举名
    # "SCAVENGING"，而正式版生产的是 id 字符串 "dumping_ground"，于是恒为 False
    # → agent_loop.py:1436-1448 的「拾荒离场强制 stow_loot」守卫在正式版从未触发，
    # 模型在拾荒点选 leave_outside 就会把地面/台面战利品全丢光。
    # v3.4 已在 structured/observer.py:880 修过同类问题（写成
    # `loc in ("SCAVENGING", "DUMPING_GROUND")`）却漏了本处，这里是回归补齐。
    # 双值兼容：不删旧值不猜新值，与 observer.py:880 保持同一写法。
    loc = str(night.get("current_location") or "").upper()
    return bool(night.get("is_outside")) and loc in ("SCAVENGING", "DUMPING_GROUND")


def _last_action_was(history: Optional[list[dict[str, Any]]], action: str) -> bool:
    """上一步（或本批历史最后一条）是否就是该动作——用于一次性强制动作防循环。"""
    if not history:
        return False
    last = history[-1] or {}
    return last.get("action") == action


def _pre_action_state_guard(action: str, bridge_client: Any) -> Optional[str]:
    """按实时 store_state 拦截「观察已过期」的昼夜动作（v1.3）。

    Returns:
        拒绝原因（业务失败文案）；None 表示放行（含状态读不到的情况）。
    """
    store_state = _live_store_state(bridge_client)
    if store_state is None:
        return None
    if action in _NIGHT_ONLY_ACTIONS and store_state != "CLOSED":
        return (f"⚠️ 本日已结束（实时 store_state={store_state}）——{action} 是夜晚动作，"
                "此刻执行会重复换日/空转。白天请正常营业（call_next/open_shutter 等）")
    if action in _DAY_ONLY_ACTIONS and store_state != "OPEN":
        return (f"⚠️ 店铺当前不可接待（实时 store_state={store_state}）——{action} 需要营业中"
                "（OPEN）。夜晚请走夜晚流程（night_services/end_day_confirm 等）")
    if action == "open_shutter" and store_state == "CLOSED":
        return ("⚠️ 实时 store_state=CLOSED（已打烊）——此时开门会让当天结算回滚、天数卡死。"
                "夜晚请走夜晚流程，白天早晨用 begin_day 开张")
    if action == "begin_day" and store_state == "CLOSED":
        return ("⚠️ 实时 store_state=CLOSED（夜晚已打烊）——begin_day 需要早晨状态；"
                "请先完成夜晚流程（night_services→end_day_confirm）")
    return None


def execute_decision(
    config: dict[str, Any],
    decision: dict[str, Any],
    *,
    live: bool = False,
    window=None,
    bridge_client: Optional[BridgeClient] = None,
    allow_mouse: bool = True,
) -> ControlResult:
    """把一个决策转换为真实（或 dry-run 的）控制 / 桥接动作。

    Args:
        live: False（默认）为 dry-run，只校验与记录，不产生真实输入与游戏调用。
        window: 指定目标窗口（None 时按配置中的游戏关键词自动查找）。
        bridge_client: 桥接客户端（执行 API 直调动作用；None 时按配置构造）。
        allow_mouse: 是否允许真实鼠标键盘操作（默认 True）。False 且 live=True 时，
            鼠标/键盘类动作一律不执行——返回 ok=false 的**业务失败**（不抛异常、
            不计入熔断计数），模型会据 detail 改用游戏 API 直调动作。
            用于「只用控制接口、绝不操控鼠标」的运行模式。
    """
    action = decision["action"]
    params = decision.get("params") or {}
    dry_run = not live
    control_cfg = config.get("control", {})
    move_duration = float(control_cfg.get("move_duration", 0.35))

    if action == "stop":
        return ControlResult(
            action="stop", params={}, executed=False, ok=True,
            detail="模型指示停止操作", duration_ms=0,
        )
    if action == "wait":
        seconds = float(params.get("seconds", 1.0))
        if not dry_run:
            time.sleep(seconds)
        return ControlResult(
            action="wait", params={"seconds": seconds}, executed=not dry_run, ok=True,
            detail=("已等待" if not dry_run else "dry-run：计划等待") + f" {seconds}s",
            duration_ms=int(seconds * 1000) if not dry_run else 0,
        )

    # 游戏 API 直调动作（桥接白名单）：不经鼠标键盘，直接调用游戏逻辑
    if action in BRIDGE_ACTIONS:
        if dry_run:
            return ControlResult(
                action=action, params=dict(params), executed=False, ok=True,
                detail=f"dry-run：计划桥接直调 {action}", duration_ms=0,
            )
        client = bridge_client or make_bridge_client(config)
        # v1.3 实时状态前置校验：拒绝按过期观察执行的昼夜动作（r68 跳天实锤）
        guard_detail = _pre_action_state_guard(action, client)
        if guard_detail is not None:
            return ControlResult(
                action=action, params=dict(params), executed=False, ok=False,
                detail=guard_detail, duration_ms=0,
            )
        started = time.time()
        response = client.post_action(action, **params)
        ok = bool(response.get("ok"))
        # v0.9.9：失败时带上桥接的 error 文案（此前只显示"失败"，AI 看不到拒绝原因）
        detail_text = response.get("detail") or response.get("error") or ("成功" if ok else "失败")
        return ControlResult(
            action=action, params=dict(params), executed=True, ok=ok,
            detail=f"桥接直调 {action}：{detail_text}",
            duration_ms=int((time.time() - started) * 1000),
        )

    win = window or _find_game_window_or_fail(config)
    # 禁鼠标模式（allow_mouse=False 且 live）：真实输入一律拦截，转业务失败
    if not allow_mouse and not dry_run:
        return ControlResult(
            action=action, params=dict(params), executed=False, ok=False,
            detail=f"禁鼠标模式：{action} 未执行（本模式只用游戏 API 直调动作，"
                   "请改用 bridge/client.py 的 ACTIONS 白名单动作）",
            duration_ms=0,
        )
    if action == "click":
        return click_image(win, params["x"], params["y"], dry_run=dry_run, move_duration=move_duration)
    if action == "ctrl_click":
        return ctrl_click_image(win, params["x"], params["y"], dry_run=dry_run, move_duration=move_duration)
    if action == "move":
        return move_image(win, params["x"], params["y"], dry_run=dry_run, move_duration=move_duration)
    if action == "press_key":
        return press_key(win, params["key"], times=int(params.get("times", 1)), dry_run=dry_run)
    if action == "type_text":
        return type_text(win, params["text"], dry_run=dry_run)
    raise ControlError(f"无法执行的动作：{action}")


def step_once(
    config: Optional[dict[str, Any]] = None,
    *,
    live: bool = False,
    mock: bool = False,
    window=None,
    window_keywords: Optional[list[str]] = None,
    brain_mock_file: Optional[str | Path] = None,
    history: Optional[list[dict[str, Any]]] = None,
    tag: Optional[str] = None,
    obs_override: Optional[Any] = None,
    push_overlay: bool = True,
    save: bool = True,
    source: str = "auto",
    bridge_client: Optional[BridgeClient] = None,
    allow_mouse: bool = True,
    step: Optional[int] = None,
    recent_wait_streak: int = 0,
    forbid_actions: Optional[set[str]] = None,
    cycle_forbid: Optional[set[str]] = None,
    night_phase: bool = False,
    goal: Optional[BusinessGoal] = None,
) -> dict[str, Any]:
    """执行一个完整步骤：观察 → 决策 → 执行，并落盘记录。

    Args:
        window: 指定目标窗口（None 时按配置关键词自动查找）。
        window_keywords: 观察阶段的窗口关键词覆盖（调试用）。
        brain_mock_file: 决策的离线样例文件（配合 --mock 使用，调试用）。
        obs_override: 测试用：直接注入观察记录（跳过截图与视觉分析）。
        push_overlay: 是否把决策推送到游戏内展示浮层（默认 True；
            游戏未运行时自动跳过，不影响主循环）。
        source: 观察通道。"auto"（默认）按 structured.prefer 优先级自动选择；
            "bridge" / "statewatch" 强制结构化通道；"vision" 强制视觉通道。
            mock=True 时强制 vision（全离线语义）。
        bridge_client: 可注入的桥客户端（测试 / 复用连接用）。
        allow_mouse: 是否允许真实鼠标键盘操作（默认 True；False 时 live 模式
            拦截一切真实输入，只执行游戏 API 直调动作）。
        step: 步序号（浮层显示 #N）。此前未透传，导致浮层条目恒显示 #0
            而无法区分不同步骤的决策（用户 2026-10-16 12:33 报）。
    Returns:
        步骤记录 dict（observation / decision / execution / 元信息）。
    """
    config = config or load_config()
    # 单步命令（cli step）不会传 tracker——这里按配置补一个，保证单步也受 KPI 约束。
    if goal is None:
        goal = BusinessGoal(config)

    # 1) 观察：结构化直读（bridge/statewatch）或视觉分析（截图 + 模型）
    state_signature: Optional[tuple] = None
    if obs_override is not None:
        record = obs_override
        if hasattr(record, "observation"):  # ObservationRecord 对象
            observation = record.observation
            image_size = (record.capture or {}).get("image_size")
            screenshot = record.screenshot
        else:  # 纯 dict（测试注入）
            observation = record.get("observation", record)
            image_size = (record.get("capture") or {}).get("image_size")
            screenshot = record.get("screenshot")
        effective_source = CHANNEL_VISION
    elif source == "auto":
        effective_source = pick_source(config, mock=mock)
    else:
        effective_source = source

    # v2.0：桥接通道必须有一个真实客户端实例。此前 CLI（run/step）不注入 client，
    # 于是依赖 client 的守卫全部静默失效：
    #   - _morning_report_guard(None) → 晨报面板守卫永不触发（v1.6 形同虚设）；
    #   - run_loop 每步的 store_state 昼夜同步 / _fallback_action 读不到实时状态
    #     → r68 类「跳天 / 夜晚白名单带进白天」风险回归。
    # 这里按需构造（不连接、不产生副作用），下游统一复用同一个客户端。
    if bridge_client is None and effective_source == CHANNEL_BRIDGE:
        bridge_client = make_bridge_client(config)

    if obs_override is None and effective_source in (CHANNEL_BRIDGE, CHANNEL_STATEWATCH):
        # 结构化观察：直读游戏状态，无截图、无坐标（point 恒 null）
        struct_obs = StructuredObserver(config, client=bridge_client).observe(effective_source)
        observation = struct_obs.observation
        image_size = None
        screenshot = None
        state_signature = struct_obs.signature
    elif obs_override is None:
        record = observe(
            config, window_keywords=window_keywords, mock=mock, tag=tag
        )
        if hasattr(record, "observation"):  # ObservationRecord 对象
            observation = record.observation
            image_size = (record.capture or {}).get("image_size")
            screenshot = record.screenshot
        else:  # 纯 dict（测试注入）
            observation = record.get("observation", record)
            image_size = (record.get("capture") or {}).get("image_size")
            screenshot = record.get("screenshot")

    # 1.7) 机制知识检索（v2.2）：按当前局面挑 1~3 张机制卡（默认关闭）。
    #      知识库坏了不能拖垮决策——加载/检索失败一律降级为空。
    mechanics_block: Optional[str] = None
    mechanic_ids: list[str] = []
    try:
        mechanics_block, mechanic_ids = mechanic_block(
            config,
            observation,
            history=history or [],
            extra_text=str(getattr(goal, "label", "") or ""),
        )
        if not mechanics_block:
            mechanics_block = None
    except (OSError, ValueError, TypeError):
        mechanics_block = None

    # 1.8) 商业目标进度（v2.1）：把「目标利润 / 剩余天数」变成每步可见的 KPI。
    #      具体数字一律取自 goal.DEFAULT_GOAL 与 config/agent.json（运行期真值），
    #      这里不写死任何金额或天数（v3.7 起默认 30 天 / 3000，旧注释是 Demo 期口径）。
    #      目标跟踪失败不影响决策（best-effort），但进度会写进步骤记录便于复盘。
    #      ⚠️ 只认结构化通道（bridge/statewatch）：视觉通道的现金是模型读图猜的，
    #      离线 mock 更是假数据——用它们推进度会污染真实账本。
    goal_block: Optional[str] = None
    goal_progress: Optional[dict[str, Any]] = None
    if (
        goal is not None
        and getattr(goal, "enabled", False)
        and not mock
        and effective_source in (CHANNEL_BRIDGE, CHANNEL_STATEWATCH)
    ):
        try:
            _progress = goal.update(observation)
            goal_block = goal.render(_progress)
            goal_progress = _progress.to_dict()
        except (TypeError, ValueError, KeyError, AttributeError):
            goal_block = None

    # 1.9) 经验记忆（v3.15）：把「同一局面下某动作是否推进过局面」的历史统计注入提示词。
    #      纯本地统计，**不额外调模型**；文件不存在/未启用时整个功能静默不生效。
    #      为什么要它：knowledge 讲的是游戏规则、history 只在内存且仅 3 步，
    #      进程一退出 Agent 就变回零经验，会在同类局面反复做无用功。
    experience_block: Optional[str] = None
    exp_store: Optional[ExperienceStore] = None
    if not mock and effective_source in (CHANNEL_BRIDGE, CHANNEL_STATEWATCH):
        try:
            exp_store = _experience_store(config)
            if exp_store is not None:
                experience_block = exp_store.render(pattern_of(observation)) or None
        except (OSError, ValueError, TypeError, KeyError):
            exp_store = None

    # 2) 决策（桥接来源时允许游戏 API 直调动作）
    # 反空转加固（r36 回归）：模型连续选 wait 时，提示词劝导无效——推理模型会
    # 自洽地认为「还在加载」。达到阈值后在提示词里**禁用 wait**，只剩推进动作。
    forbid_wait = (not allow_mouse) and recent_wait_streak >= _WAIT_STREAK_LIMIT
    # 1.5) 晨报面板硬守卫（v1.6，用户 10-20 指令）：「日历/夜间报告/状态」结算面板
    #      开着时模型常去交易/叫号，导致开店迟到、错过顾客——检测到就强制叉掉，
    #      跳过本轮模型决策（面板关闭后下一轮恢复正常决策）。
    forced_decision = (
        _morning_report_guard(bridge_client)
        if effective_source == CHANNEL_BRIDGE
        else None
    )
    if forced_decision is not None:
        decision, brain_meta = forced_decision, {"forced_by": "morning_report_guard"}
    else:
        decision, brain_meta = decide(
            config, observation,
            image_size=image_size, history=history or [],
            mock=mock, mock_file=brain_mock_file,
            bridge_actions=(effective_source == CHANNEL_BRIDGE),
            no_mouse=not allow_mouse,
            forbid_wait=forbid_wait,
            forbid_actions=forbid_actions,
            cycle_forbid=cycle_forbid,
            night_phase=night_phase,
            goal_block=goal_block,
            mechanics_block=mechanics_block,
            experience_block=experience_block,
        )

    # 1.6) 拾荒离场强制收纳（v1.7，用户 10-20 实测铁律：物品放地上会消失）：
    #      在拾荒地点直接 leave_outside 会把台面/地面上的战利品全部丢掉。模型选离场时
    #      先替换成收纳动作，下一轮再离场；若上一步已经是收纳动作（成功或失败）则放行，
    #      避免收纳失败时无限循环。
    #      ⚠️ 下发的是 stow_loot（逐件搬 EmporiumEntry.afterhourInventory 的「地面」格子 →
    #      左/右口袋 → 背包位，BridgeApi.cs:2887-2947），不是 take_ground_items
    #      （后者走 map.TransferCounterToBackInv 且要求 map.Current.Id == "dumping_ground"，
    #      见 BridgeApi.cs:2994-3008）。选 stow_loot 是因为它不依赖地图状态、且失败会返回
    #      「失败 N 件」便于下一轮判断。这与下面 `_last_action_was(history, "stow_loot")`、
    #      以及 C# 的动作名完全一致，不要改名。
    if (
        effective_source == CHANNEL_BRIDGE
        and decision.get("action") == "leave_outside"
        and _at_scavenging_site(observation)
        and not _last_action_was(history, "stow_loot")
    ):
        decision = {
            "action": "stow_loot",
            "params": {},
            "reason": "离场前把地面上的拾荒战利品装进背包/口袋（放地上会消失）",
            "confidence": 0.95,
        }
        brain_meta = {"forced_by": "scavenge_pickup_guard"}

    # 3) 执行（默认 dry-run）
    #    执行前动画等待闸门（v0.8.5「操作纪律」，用户 2026-10-16 反馈「调用接口要让动画过完」）：
    #    ① 实时通道：/state 签名连续两次一致才算「稳定」（动画中字段会持续变化）；
    #    ② 日志通道：GameLog 无在途动作/移动（补充防御，覆盖「数据已静、动画仍播」残余场景）。
    #    两通道均超时放行——绝不卡死。
    gate_info: Optional[dict[str, Any]] = None
    if live and decision["action"] not in ("wait", "stop"):
        gate_cfg = config.get("action_gate", {}) or {}
        if gate_cfg.get("enabled", True):
            gate_client = bridge_client or make_bridge_client(config)
            settled_s, waited_s, reason_s = wait_for_state_settle(config, gate_client)
            settled_l, waited_l, reason_l = wait_for_animation_settle(config)
            total_waited = waited_s + waited_l
            settled = settled_s and settled_l
            if total_waited > 0.05 or not settled:
                gate_info = {
                    "settled": settled,
                    "waited_seconds": round(total_waited, 2),
                    "reason": reason_s or reason_l or "",
                }
    # 3.0) 库存守卫（v0.9.3）：交易 identifier 必须真实存在于库存——防「凭空卖」
    #      （用户 2026-10-16 指令"卖东西要看自己有什么"）。拦截时不执行桥接调用，
    #      也不触发失败恢复（未执行、桌上无物可清）。
    # 3.0.3) 售前类别校验（v0.9.14）：顾客想收类别与物品类别无交集 → 业务失败
    #      （r51 实测对不匹配物品连发 sell_item ×10，bridge 空跑、顾客卡店）。
    # 3.0.5) 卷帘门幂等守卫（v0.9.13）：门已处于目标态 → 业务失败，不调桥接
    #      （r49 实测 open_shutter ×5 让门状态抖动、零变化追踪器失明）。
    # 3.0.6) 夜晚界面幂等守卫（v0.9.16）：地图已开还 open_map / 已外出还
    #      go_out / 已回店还 leave_outside → 业务失败（r54 实测地图开着
    #      反复 open_map ×5 空转卡死，20 步批次耗光）。
    # 3.0.7) 交租储备红线（v0.9.17）：交租日前大额支出会让现金低于租金 →
    #      业务失败（r55 实测：现金 213 / 租金 210 两天后期，AI 仍点 150
    #      批发买入——买完只剩 63，交租日软锁）。
    guard_error = _inventory_guard(decision, observation)
    if guard_error is None:
        guard_error = _dialog_guard(decision, observation)
    if guard_error is None:
        guard_error = _category_guard(decision, observation)
    if guard_error is None:
        guard_error = _shutter_guard(decision, state_signature)
    if guard_error is None:
        guard_error = _night_ui_guard(decision, state_signature)
    if guard_error is None:
        guard_error = _stalled_guard(decision, observation)
    if guard_error is None:
        guard_error = _rent_reserve_guard(decision, observation, state_signature)
    # 3.0.8) 终局风险守卫（v3.9 / T06-P0）：**链尾**，只在上一条都放行后才轮到它。
    #      过了交租日 + can_pay_rent=false + 现金自算也付不起 ⇒ 只许救命动作
    #      （sell_item/sell_table/dialog_choose/pay_rent/wait），其余一律拦。
    #      为什么必须在链尾而不是并进 _rent_reserve_guard：那条是「别把钱花到付不起」，
    #      这条是「已经付不起了，别把最后那步点成死局」——语义不同、触发条件不同，
    #      并进去会改掉它现有的 13 条测试所保护的语义（docs/26 §5.4 不替换不 reorder）。
    if guard_error is None:
        guard_error = _defeat_risk_guard(decision, observation, state_signature)
    # v2.9：对话选项按文字下发（序号盲选是历史事故源）
    _enrich_dialog_choice(decision, observation)
    if guard_error is not None:
        execution = ControlResult(
            action=decision["action"], params=dict(decision.get("params") or {}),
            executed=False, ok=False, detail=guard_error, duration_ms=0,
        )
    else:
        execution = execute_decision(
            config, decision, live=live, window=window, bridge_client=bridge_client,
            allow_mouse=allow_mouse,
        )
    # 3.2) 失败自动恢复（v0.8.5）：卖出失败 → 自动清桌回收（防物品滞留谈判桌）
    #      仅当"真实执行过且失败"——库存守卫拦截不算（没执行、桌上无物可清）。
    recovery: Optional[dict[str, Any]] = None
    if live and execution.ok is False and guard_error is None:
        recovery_action = _RECOVERY_ACTIONS.get(decision["action"])
        if recovery_action:
            rec_result = execute_decision(
                config, {"action": recovery_action, "params": {}},
                live=True, window=window, bridge_client=bridge_client,
                allow_mouse=allow_mouse,
            )
            recovery = {
                "trigger": decision["action"],
                "action": recovery_action,
                "ok": rec_result.ok,
                "detail": rec_result.detail,
            }
            if push_overlay and not mock:
                push_decision_overlay(
                    config,
                    {
                        "action": recovery_action,
                        "params": {},
                        "reason": f"{_zh_action(decision['action'])}失败——自动清桌回收",
                        "confidence": 1.0,
                    },
                    step=step, observation=observation,
                )
    # 3.3) 成交后自动收纳（v0.9.18，规则：批发后的物品必须放回储物区）：
    #      批发买入成交 → 立即点 transferButton 把前台柜台物品搬回储物区。
    #      r55 实锤：不收纳则 front counter full，下一位顾客带来的物品会被
    #      游戏直接销毁（一战毁过 inventor 的 12 件）。与 3.2 失败恢复同一
    #      模式：live、真实执行过且成功（ok=true）才触发；best-effort，
    #      收纳本身失败不阻断主流程（记入 step_record.stow_after_deal）。
    stow: Optional[dict[str, Any]] = None
    if (
        live
        and execution.ok is True
        and guard_error is None
        and _needs_cleanup_after(decision)
    ):
        stow_result = execute_decision(
            config,
            {"action": "click_button", "params": {"name": "transferButton"}},
            live=True, window=window, bridge_client=bridge_client,
            allow_mouse=allow_mouse,
        )
        stow = {
            # trigger 用**实际动作名**：v3.16 起清柜不再只由批发触发，
            # 写死 "AcceptWholesale" 会让复盘日志误导（实机已踩到）。
            "trigger": str(decision.get("action") or "unknown"),
            "action": "click_button:transferButton",
            "ok": stow_result.ok,
            "detail": stow_result.detail,
        }
        if push_overlay and not mock:
            push_decision_overlay(
                config,
                {
                    "action": "click_button",
                    "params": {"name": "transferButton"},
                    "reason": "自动清柜：把前台柜台物品转移回储物区",
                    "confidence": 1.0,
                },
                step=step, observation=observation,
            )
    # 3.45) 议价标记残留自愈（v3.16，用户 2026-11-22「桌子上的东西一直在桌上」）：
    #   实机看到的现象是 negociated_item 仍指向某物品、但 nego_open=false（谈判面板已关），
    #   UI 就一直把"桌上"画成有货，其实物品早已在储物区。这类残留会误导后续决策
    #   （AI 以为桌上有货可成交），也会让玩家看着糊涂——检测到就清掉标记。
    #   只在「可能产生残留」的动作之后检查，避免每步多读一次状态。
    # v3.16b：不再按动作白名单判断——实机证明残留会在任意动作后存在
    #（AI 选 go_out 时 negociated 仍挂着 junk）。每步多读一次状态（约 20ms）
    # 远小于一次模型调用的 12 秒，值得。
    if (
        live
        and not mock
        and guard_error is None
        and bridge_client is not None
    ):
        try:
            _st = bridge_client.get_state()
            _table = (_st or {}).get("table") or {}
            # 残留判定：有议价标记，且「面板已关」或「顾客已经走了」。
            # 只看 nego_open 不够——实机 dismiss_client 后面板仍报 True，
            # 标记就一直挂着，玩家看到的就是"桌上的东西一直在桌上"。
            _residual = bool(_table.get("negociated_item")) and (
                not _table.get("nego_open") or not _st.get("client")
            )
            if _residual:
                _rc = execute_decision(
                    config, {"action": "clear_table", "params": {}},
                    live=True, window=window, bridge_client=bridge_client,
                    allow_mouse=allow_mouse,
                )
                if stow is None:
                    stow = {}
                stow["negociation_residue_cleared"] = {"ok": _rc.ok, "detail": _rc.detail}
        except (AttributeError, TypeError, ValueError, KeyError):
            pass

    # 3.5) 玩家式节奏（v0.7.3）：每个动作后统一停顿——桥直调动作也停，
    # 否则「NPC 刚来就被遣散 / 秒买秒卖」，不像真实玩家，也不便观察记录。
    # v0.7.5b：call_next / dismiss_client 后追加等待——游戏日志实测 NPC 移动动画
    # 约 2-3 秒（Arriving → Client Arrived），不等动画放完就操作会穿模/状态错乱。
    if live:
        base = float(config.get("control", {}).get("after_action_delay", 1.0))
        extra = _NPC_MOVE_EXTRA_DELAY.get(decision["action"], 0)
        time.sleep(base + extra)

    # 4) 推送到游戏内决策展示浮层（best-effort：桥不可达时静默跳过）。
    #    浮层显示全中文：动作名转中文、物品标识转中文名（方便测试与记录）。
    #    v2.9：mock 运行**绝不推送**——mock 决策是假数据，推到真机浮层会把玩家
    #    看到的面板刷成"（Mock）顾客报价合适，点击收购按钮"，而游戏里根本没这条命令。
    #    实盘证据：2026-10-16 一次 mock 测试推了一条，浮层冻在那儿 9 天。
    overlay_pushed = (
        push_decision_overlay(config, decision, step=step, observation=observation)
        if (push_overlay and not mock) else False
    )

    # 3.9) 经验落库（v3.15）：**只认状态变化，不认 ok**——项目已有「假成功」教训
    #      （ok=true 但现金/库存没动），所以用 state_signature 前后是否变化判定。
    #      失败/异常一律静默：经验记录绝不能影响主流程。
    if exp_store is not None and not mock and str(decision.get("action") or "") != "stop":
        try:
            sig_after = _read_state_signature(bridge_client)
            progressed = bool(
                state_signature is not None
                and sig_after is not None
                and sig_after != state_signature
            )
            exp_store.add(
                Experience(
                    pattern=pattern_of(observation),
                    action=str(decision.get("action") or ""),
                    progressed=progressed,
                    note=str(getattr(execution, "detail", "") or "")[:60],
                )
            )
        except (OSError, ValueError, TypeError, KeyError):
            pass

    step_record: dict[str, Any] = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "source": effective_source,
        "live": bool(live),
        "mock": bool(mock),
        "observation": observation,
        "screenshot": screenshot,
        "decision": decision,
        "brain_meta": brain_meta,
        "execution": execution.to_dict(),
        "overlay_pushed": overlay_pushed,
    }
    if gate_info is not None:
        step_record["action_gate"] = gate_info
    if goal_progress is not None:
        step_record["goal"] = goal_progress
    if mechanic_ids:
        step_record["mechanics"] = mechanic_ids
    if recovery is not None:
        step_record["recovery"] = recovery
    if stow is not None:
        step_record["stow_after_deal"] = stow
    if state_signature is not None:
        step_record["state_signature"] = list(state_signature)
    if save:
        step_record["step_file"] = str(_save_step(step_record, artifacts_dir(config) / "steps"))
    return step_record


def _save_step(record: dict[str, Any], out_dir: Path) -> Path:
    """把步骤记录保存为 JSON 文件（UTF-8，缩进 2）。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
    path = out_dir / f"step_{timestamp}.json"
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def stop_requested(config: dict[str, Any]) -> bool:
    """检查停止标志：artifacts/STOP 文件存在时，循环将在下一步前退出。"""
    return (artifacts_dir(config) / "STOP").exists()


def _compact_history(index: int, record: dict[str, Any]) -> dict[str, Any]:
    """把步骤记录压缩为历史条目（供决策提示词使用）。"""
    if "error" in record:
        return {"step": index, "error": record["error"]}
    decision = record.get("decision") or {}
    execution = record.get("execution") or {}
    return {
        "step": index,
        "action": decision.get("action"),
        "params": decision.get("params"),
        "reason": decision.get("reason"),
        "result": execution.get("detail"),
        "ok": execution.get("ok"),
        "screen_state": (record.get("observation") or {}).get("screen_state"),
    }


def _step_limit(max_steps: int | None) -> int:
    """把 max_steps 归一成 for 循环上限；0 / None / 负数都视为**不设上限**。

    为什么允许不设上限：游戏是长局（跨数十天净赚目标、攒钱买店），每跑几十步就要手动重启一次
    很蠢。取消上限后仍有三个停止出口：
      1. 面板「■ 停止 Agent」（直接结束进程）；
      2. artifacts/STOP 文件（循环每步检查）；
      3. 模型自己输出 action=stop。
    上限用一个"大数"而不是 while True，是为了保留 for 的 index 语义（日志/步号不变）。
    """
    try:
        value = int(max_steps) if max_steps is not None else 0
    except (TypeError, ValueError):
        value = 0
    return value if value > 0 else 2 ** 31 - 1


def run_loop(
    config: Optional[dict[str, Any]] = None,
    *,
    max_steps: int = 0,
    live: bool = False,
    mock: bool = False,
    delay_between: float = 0.4,
    window=None,
    window_keywords: Optional[list[str]] = None,
    brain_mock_file: Optional[str | Path] = None,
    push_overlay: bool = True,
    source: str = "auto",
    bridge_client: Optional[BridgeClient] = None,
    event_driven: bool = False,
    poll_interval: Optional[float] = None,
    idle_limit: Optional[float] = None,
    allow_mouse: bool = True,
    obs_override: Optional[Any] = None,
    goal: Optional[BusinessGoal] = None,
) -> list[dict[str, Any]]:
    """连续执行「观察 → 决策 → 执行」循环。

    Args:
        max_steps: 步数上限；0/None/负数 = 不设上限（默认）。停止出口：
            面板「■ 停止 Agent」/ artifacts/STOP 文件 / 模型输出 action=stop
        live: False（默认）为 dry-run；True 时真实操作鼠标键盘 / 直调游戏 API
        delay_between: 相邻步骤之间的等待秒数（给游戏留出响应时间）
        push_overlay: 是否把每步决策推送到游戏内展示浮层（默认 True）
        source: 观察通道（auto/bridge/statewatch/vision），见 step_once
        bridge_client: 可注入的桥客户端（测试 / 复用连接用）
        event_driven: 事件驱动模式（仅结构化通道生效）：状态签名无变化时
            等待而非空转决策；idle_limit 秒内一直无变化也执行一步（心跳兜底）。
            vision 通道下观测本身昂贵，自动退化为固定间隔循环。
        poll_interval: 事件驱动轮询间隔秒数（默认取 structured.poll_interval）
        idle_limit: 事件驱动无变化心跳上限秒数（默认取 structured.event_idle_limit）
        allow_mouse: 是否允许真实鼠标键盘操作（默认 True）。False 且 live=True 时，
            鼠标/键盘类动作一律拦截（返回 ok=false 的业务失败，不计入熔断计数），
            模型会据 detail 改用游戏 API 直调动作——用于「只用控制接口」的运行模式。
        obs_override: 测试用：直接注入观察记录（跳过真实观察）。
    Returns:
        每步的记录列表（含错误与停止条目）。
    """
    config = config or load_config()
    # 商业目标跟踪（v2.1）：整个批次共用一个 tracker，进度跨批次落盘续跑。
    goal_tracker = goal if goal is not None else BusinessGoal(config)
    structured_cfg = config.get("structured", {}) or {}
    poll_interval = float(
        poll_interval if poll_interval is not None else structured_cfg.get("poll_interval", 0.5)
    )
    idle_limit = float(
        idle_limit if idle_limit is not None else structured_cfg.get("event_idle_limit", 20.0)
    )

    steps: list[dict[str, Any]] = []
    history: list[dict[str, Any]] = []
    consecutive_errors = 0

    # 通道解析（事件驱动需要结构化观察器做变化预判）
    effective_source = pick_source(config, mock=mock) if source == "auto" else source
    # v2.0：同 step_once——CLI 未注入 client 时按需构造，否则下面所有
    # 「读实时 store_state」的守卫（昼夜同步 / 阶段兜底 / 晨报面板）全部静默失效。
    if bridge_client is None and effective_source == CHANNEL_BRIDGE:
        bridge_client = make_bridge_client(config)
    pre_observer: Optional[StructuredObserver] = None
    if event_driven and effective_source in (CHANNEL_BRIDGE, CHANNEL_STATEWATCH):
        pre_observer = StructuredObserver(config, client=bridge_client)

    last_signature: Optional[tuple] = None
    wait_streak = 0  # 连续 wait 计数（反空转用，r36）
    # v1.5 卡店自愈：跟踪「不可遣散且对话已结束」的驻店顾客
    stuck_client_id: Optional[str] = None
    stuck_client_steps = 0
    # v0.9.8「无效动作 streak」跟踪状态
    prev_action: Optional[str] = None
    prev_sig: Optional[tuple] = None
    same_action_streak = 0
    no_change_streak = 0
    dead_forbid: set[str] = set()
    # v0.9.15 夜晚阶段标志：end_day 成功 → True（只许夜晚流程动作）；
    # begin_day 成功 → False（新一天恢复营业动作）。打烊后重新开门会让当天
    # 结算回滚、天数卡死（r53 实测），故用白名单硬约束而非提示词劝导。
    # v0.9.16：跨批次续跑——先从标记文件 + 实时状态 seed（上批打完烊地图还
    # 开着，新批次首步就必须锁死在夜晚动作上，否则又一次回滚当天）。
    night_phase = _night_marker_valid(config) or _seed_night_phase(
        config, effective_source, bridge_client
    )
    # v1.1 夜晚子阶段：closing=已打烊待确认服务；outing=已确认待睡觉/外出
    night_stage: Optional[str] = "outing" if night_phase else None
    _limit = _step_limit(max_steps)
    for index in range(1, _limit + 1):
        # 停止标志：用户可随时创建 artifacts/STOP 文件中止
        if stop_requested(config):
            steps.append({
                "step": index, "stopped": True,
                "detail": "检测到 STOP 文件，循环提前退出",
            })
            break

        # 事件驱动：等待「世界发生变化」（或心跳超时）再决策
        if pre_observer is not None:
            waited = 0.0
            while waited < idle_limit:
                if stop_requested(config):
                    break
                try:
                    pre = pre_observer.observe(effective_source, save=False)
                except GameAgentError:
                    break  # 预观测失败：直接进完整步骤，让标准错误处理接管
                if last_signature is None or pre.signature != last_signature:
                    break
                time.sleep(poll_interval)
                waited += poll_interval

        try:
            # v1.3 状态驱动的昼夜阶段同步（r68 实锤）：end_day_confirm 让游戏直接
            # 进入次日 MORNING，若仍沿用「动作名」推断，夜晚白名单会被带进白天
            # （r68 实测次日早晨又点睡袋）。这里每步开跑前按实时 store_state 校正：
            #   MORNING → 夜晚结束；CLOSED → 夜晚开始（OPEN 不动，避免换日过渡抖动）
            if live and effective_source in (CHANNEL_BRIDGE, CHANNEL_STATEWATCH):
                store_now = _live_store_state(bridge_client)
                if store_now == "MORNING" and night_phase:
                    night_phase = False
                    night_stage = None
                    _clear_night_marker(config)
                elif store_now == "CLOSED" and not night_phase:
                    night_phase = True
                    night_stage = "closing"
                    _write_night_marker(config)
            # v0.9.13：周期动作窗口计数（防重复打烊）——每步开跑前重算，
            # 与 dead_forbid 合并后从白名单硬剔除 + 提示词声明。
            cycle_forbid = _update_cycle_forbid(steps)
            record = step_once(
                config, live=live, mock=mock, window=window,
                window_keywords=window_keywords,
                brain_mock_file=brain_mock_file,
                history=history, tag=f"run{index:02d}",
                push_overlay=push_overlay,
                source=effective_source,
                bridge_client=bridge_client,
                allow_mouse=allow_mouse,
                step=index,
                obs_override=obs_override,
                recent_wait_streak=wait_streak,
                forbid_actions=dead_forbid | cycle_forbid,
                cycle_forbid=cycle_forbid,
                night_phase=night_phase,
                goal=goal_tracker,
            )
            record["step"] = index
            consecutive_errors = 0
            # v0.9.15：夜晚阶段标志维护——end_day 成功进入夜晚（只许夜晚流程
            # 动作）；begin_day / stay_home 成功结束夜晚（恢复营业动作）。
            # v0.9.16：同步维护跨批次标记文件（打烊后地图可能还开着，下批要锁夜）。
            act_ok = (record.get("decision") or {}).get("action")
            act_success = (record.get("execution") or {}).get("ok")
            if act_ok == "end_day" and act_success:
                night_phase = True
                night_stage = "closing"
                _write_night_marker(config)
            elif act_ok == "end_day_confirm" and act_success:
                night_stage = "outing"
            elif act_ok in ("begin_day", "stay_home", "open_shutter") and act_success:
                night_phase = False
                night_stage = None
                _clear_night_marker(config)
            # 反空转计数：连续 wait 累积，任一推进类动作即清零（r36）
            if (record.get("decision") or {}).get("action") == "wait":
                wait_streak += 1
            else:
                wait_streak = 0
            # v0.9.8「无效动作 streak」：同名动作连续 + 状态零变化 → 下一步禁用
            action_now = (record.get("decision") or {}).get("action")
            sig_now = record.get("state_signature")
            sig_now_t = tuple(sig_now) if sig_now else None
            same_action_streak, no_change_streak, dead_forbid = _update_dead_action_tracker(
                prev_action, prev_sig, action_now, sig_now_t,
                same_action_streak, no_change_streak,
            )
            prev_action, prev_sig = action_now, sig_now_t

            # v1.5 卡店自愈（r79 实证：贸易委员会官员对话结束后仍赖着不走，
            # dismissable=false + business_complete=false，送客/打烊全被拒，
            # AI 只能反复 wait 直到批次耗尽）。检测同一不可遣散顾客连续 5 步
            # 仍在店且对话已结束 → 调 force_dismiss（恢复专用动作）清走。
            try:
                _vi = (record.get("observation") or {}).get("visible_info") or {}
                _cl = _vi.get("client") or {}
                _dlg = _vi.get("dialog") or {}
                _cid = _cl.get("identifier")
                if _cid and _dlg.get("is_end") and _cl.get("dismissable") is False:
                    if _cid == stuck_client_id:
                        stuck_client_steps += 1
                    else:
                        stuck_client_id, stuck_client_steps = _cid, 1
                    if stuck_client_steps >= 5:
                        fb_res = execute_decision(
                            config, {"action": "force_dismiss", "params": {}},
                            live=True, window=window,
                            bridge_client=bridge_client, allow_mouse=allow_mouse,
                        )
                        record["stuck_recovery"] = {
                            "client": _cid, "ok": fb_res.ok, "detail": fb_res.detail,
                        }
                        stuck_client_id, stuck_client_steps = None, 0
                else:
                    stuck_client_id, stuck_client_steps = None, 0
            except GameAgentError as _exc:
                record["stuck_recovery"] = {"ok": False, "detail": str(_exc)}
        except GameAgentError as exc:
            # 单步失败不中断整个循环，但连续失败达到上限即熔断
            consecutive_errors += 1
            record = {"step": index, "error": str(exc)}
            # v1.1 阶段兜底（r62 催生）：模型连续 2 次无法产出有效决策时，
            # 执行当前阶段的唯一合理动作（夜晚确认/睡觉、死锁顾客送客、
            # 无人可接打烊），保证批次不空转。best-effort，失败如实记录。
            if live and consecutive_errors >= 2:
                fb_action = _fallback_action(night_stage, config, effective_source, bridge_client)
                if fb_action is not None:
                    try:
                        fb_result = execute_decision(
                            config, {"action": fb_action, "params": {}},
                            live=True, window=window,
                            bridge_client=bridge_client, allow_mouse=allow_mouse,
                        )
                        record["fallback"] = {
                            "action": fb_action, "ok": fb_result.ok,
                            "detail": fb_result.detail,
                        }
                        if fb_result.ok:
                            consecutive_errors = 0
                            if fb_action == "end_day_confirm":
                                night_stage = "outing"
                            elif fb_action in ("stay_home", "begin_day", "open_shutter"):
                                night_phase = False
                                night_stage = None
                                _clear_night_marker(config)
                            elif fb_action == "end_day":
                                night_phase = True
                                night_stage = "closing"
                                _write_night_marker(config)
                    except GameAgentError as fb_exc:
                        record["fallback"] = {"action": fb_action, "ok": False,
                                              "detail": str(fb_exc)}
            steps.append(record)
            history.append(_compact_history(index, record))
            if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                break
            continue

        steps.append(record)
        history.append(_compact_history(index, record))

        # 记录本次状态签名，供下一轮事件驱动比较
        sig = record.get("state_signature")
        last_signature = tuple(sig) if sig else None

        # 模型主动停止
        if (record.get("decision") or {}).get("action") == "stop":
            break

        if index < _limit:
            time.sleep(max(0.0, float(delay_between)))

    return steps
