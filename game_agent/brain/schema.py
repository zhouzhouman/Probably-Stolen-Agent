"""决策数据结构与校验（对 brain 输出的稳定契约）。

设计原则：
- 动作有白名单，白名单外的动作直接拒绝（防止模型幻觉出不存在的操作）；
- 参数按动作逐项校验与规范化（坐标转整数、次数夹取范围、等待时长夹取范围）；
- 未知参数静默丢弃（模型偶尔会输出多余字段，不应因此整体失败）；
- 桥接可用时可通过 extra_actions 注入「游戏 API 直调动作」白名单
  （见 bridge.client.ACTIONS），让模型在结构化观察下直调游戏逻辑。
"""
from __future__ import annotations

from typing import Any, Optional

from game_agent.contracts import (
    derive_allowed,
    derive_params,
    spec_requires_identifier,
)

# 决策必须包含的核心字段（对 brain 输出的稳定契约）。
# v0.9.13（r49 修复 B）：reason / confidence 降级为「可选、缺省容错」——
# r49 实测 GLM-5.3-Flash 连续 12 步输出 {"action","params"} 而缺
# reason/confidence，硬性报错白白浪费整轮（纠错重试也修不好字段缺失，
# 模型就是不爱写）。缺省值见 validate_decision 尾部；action 仍硬性必填。
REQUIRED_DECISION_KEYS = ("action",)
OPTIONAL_DECISION_KEYS = ("reason", "confidence")

# ---- 动作白名单：唯一真相在 game_agent.contracts（X01 同源化）----
#
# 这两张表历史上是手工维护的副本，与 bridge.client.ACTIONS、agent_loop 的各套
# 白名单各自演进、多次漂移。现在全部由 game_agent.contracts 派生，本模块不再
# 持有任何动作名/参数名的字面量。
#
# 派生结果与改造前逐项相等（唯一差异：补上 C# 早就在读、此前被白名单静默丢弃的
# click_button.op 与 machine_module_install.occurrence，见 BridgeApi.cs:1743/1837）。

# 动作白名单：action -> 允许出现的参数字段（基础动作，走 control 层）。
ALLOWED_ACTIONS: dict[str, set[str]] = derive_allowed()

# 桥接直调动作的允许参数（无参数动作为空集）。
# 与 bridge.client.ACTIONS 同源，注入时经 extra_actions 传入。
BRIDGE_ACTION_PARAMS: dict[str, set[str]] = derive_params()

# 需要物品标识的动作（T4）：统一走 _require_identifier 校验，
# 防止模型幻觉出的空/缺 identifier 静默放行到桥接层。
#
# 由契约派生：identifier 在 ActionSpec 里标 required=True 的动作。
# 改造前是手写 {"place_item","sell_item","buy_item","appraise","display_case"}，
# 派生结果与之完全相同（equip_backpack 的 identifier 是可选的，见 contracts.py
# 中 _OPT_ID 的说明：C# 缺省时会自动挑背包，故不在此列）。
_IDENTIFIER_ACTIONS = frozenset(
    name for name in BRIDGE_ACTION_PARAMS if spec_requires_identifier(name)
)


def _require_identifier(action: str, params: dict[str, Any]) -> str:
    """校验并归一化物品标识（T4 公共校验）。

    identifier 必须是非空字符串（来自观察 inventory 段的 identifier 字段），
    两侧空白会被去除。缺失或为空一律抛 ValueError——由 decide() 触发纠错重试，
    而不是让幻觉参数直达游戏。
    """
    identifier = str(params.get("identifier") or "").strip()
    if not identifier:
        raise ValueError(f"{action} 缺少 identifier 参数（物品标识）")
    return identifier


def validate_decision(
    data: dict[str, Any],
    *,
    extra_actions: Optional[dict[str, set[str]]] = None,
) -> dict[str, Any]:
    """校验并规范化决策输出。

    Args:
        data: 模型输出的决策 dict。
        extra_actions: 额外动作白名单 {action: 允许参数字段集合}（桥接直调动作）。
            仅接受 BRIDGE_ACTION_PARAMS 中已定义的动作，防止调用方注入任意动作。

    Raises:
        ValueError: 非 dict、缺少必需字段、动作不在白名单、关键参数缺失或非法。
    """
    if not isinstance(data, dict):
        raise ValueError(f"决策必须是 JSON 对象，实际为 {type(data).__name__}")

    missing = [key for key in REQUIRED_DECISION_KEYS if key not in data]
    if missing:
        raise ValueError(f"决策缺少必需字段：{missing}")
    allowed_actions = dict(ALLOWED_ACTIONS)
    for name, params in (extra_actions or {}).items():
        # 只接受已知桥动作；未知动作名静默忽略（防御性）
        if name in BRIDGE_ACTION_PARAMS:
            allowed_actions[name] = set(BRIDGE_ACTION_PARAMS[name]) | set(params or set())

    action = str(data["action"]).strip().lower()
    if action not in allowed_actions:
        raise ValueError(
            f"动作不在白名单内：{action!r}（可选：{sorted(allowed_actions)}）"
        )

    raw_params = data.get("params") or {}
    if not isinstance(raw_params, dict):
        raise ValueError("params 必须是 JSON 对象")
    allowed = allowed_actions[action]
    params: dict[str, Any] = {key: value for key, value in raw_params.items() if key in allowed}

    # —— 逐动作校验与规范化 ——
    if action in ("click", "move", "ctrl_click"):
        for key in ("x", "y"):
            if key not in params:
                raise ValueError(f"{action} 缺少坐标参数 {key!r}")
            try:
                params[key] = int(round(float(params[key])))
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{action} 的 {key!r} 不是合法数字：{params[key]!r}") from exc
            if params[key] < 0:
                raise ValueError(f"{action} 的坐标不能为负数：{key}={params[key]}")

    elif action == "press_key":
        if not str(params.get("key") or "").strip():
            raise ValueError("press_key 缺少 key 参数")
        params["key"] = str(params["key"]).strip().lower()
        try:
            params["times"] = max(1, min(10, int(params.get("times", 1))))
        except (TypeError, ValueError):
            params["times"] = 1

    elif action == "type_text":
        text = str(params.get("text") or "")
        if not text:
            raise ValueError("type_text 缺少 text 参数")
        params["text"] = text

    elif action == "wait":
        try:
            seconds = float(params.get("seconds", 1.0))
        except (TypeError, ValueError):
            seconds = 1.0
        params["seconds"] = round(max(0.1, min(8.0, seconds)), 2)

    elif action == "dialog_choose":
        # 桥接动作：优先按**选项文字**选（v2.5，玩家点什么选什么）；
        # 没给 text 时才退回序号（0 起，缺失按 0）。
        text = str(params.get("text") or "").strip()
        if text:
            params["text"] = text
            params.pop("index", None)
        else:
            try:
                params["index"] = max(0, int(round(float(params.get("index", 0)))))
            except (TypeError, ValueError):
                params["index"] = 0

    elif action in _IDENTIFIER_ACTIONS:
        # 统一物品标识校验（T4）：place_item / sell_item / buy_item / appraise /
        # display_case 必须携带非空 identifier，否则模型幻觉出的参数会静默放行。
        params["identifier"] = _require_identifier(action, params)

    elif action == "night_service":
        # 桥接动作：服务关键词必填；enable 归一为布尔（默认 True=选中）
        service = str(params.get("service") or "").strip()
        if not service:
            raise ValueError("night_service 缺少 service 参数（服务 id 或名称关键词）")
        params["service"] = service
        enable_raw = params.get("enable", True)
        if isinstance(enable_raw, str):
            params["enable"] = enable_raw.strip().lower() not in ("false", "0", "no", "off")
        else:
            params["enable"] = bool(enable_raw)

    elif action == "go_out":
        # 桥接动作：目的地关键词必填。
        #
        # v2.1（X01 同源化）：目的地合法性以 game_agent.contracts 的
        #   DEST_ALL（10 个别名 ∪ 3 个已知地点 id ∪ 4 个旧名映射键）为文档事实。
        #
        # v2.2（2026-11-13 撤销早期收紧）：upgrade / upgrade_merchant / 升级 /
        #   升级商人 **不是**「不存在的地点」，而是 dr_jackson（升级商人）的旧名：
        #     · EmporiumAnalytics.cs:522-526  "dr_jackson" => "UPGRADE_MERCHANT"
        #     · MapUIManager.cs:25  public GameObject UIUpgradeMerchantPanel;
        #     · MapUIManager.cs:412 public void LeaveUpgradeMerchant()
        #   早期依据 BridgeApi.cs:2813-2814 的注释「正式版地点表里没有『升级商人』
        #   独立地点……这里不猜，交给下面的 id 查找失败」把它们判为必然失败并提前
        #   拒绝，该注释有误（事实是「改名为 dr_jackson」而非「没有」）。现已恢复
        #   接受，映射关系登记在 contracts.DEST_ALIAS_MAP。
        #
        # 为什么不把 DEST_ALL 当白名单做硬校验：
        #   /state.locations 是 AfterhourLocation.GetAll() 的**动态注册表**，
        #   可含 mod 注册的地点，硬白名单会把它们误拒（行为收紧、且拦错了对象）。
        #   因此这里只校验「必填非空」，合法性交给 C# 用真实清单判定并回显。
        #   **Python 侧不做别名转换**：destination 原样透传，日志里记的与模型写的
        #   必须一致；转换会让 C# 的真实清单校验失去意义。
        destination = str(params.get("destination") or "").strip()
        if not destination:
            raise ValueError("go_out 缺少 destination 参数（commissary/scavenging 等）")
        # 注意：**不做大小写归一**——保持改造前「原样透传」的行为，
        # C# 侧自己会 ToLowerInvariant（BridgeApi.cs:2795），归一只会改变
        # 决策记录里的可读性而没有任何收益。
        params["destination"] = destination

    elif action == "phone_call":
        # 桥接动作：number 与 contact 至少其一（号码或联系人关键词）
        number = params.get("number")
        contact = str(params.get("contact") or "").strip()
        if number in (None, "") and not contact:
            raise ValueError("phone_call 需要 number（号码）或 contact（名称关键词）参数")
        if number not in (None, ""):
            try:
                params["number"] = int(number)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"phone_call 的 number 不是合法号码：{number!r}") from exc
        if contact:
            params["contact"] = contact

    elif action == "phone_book":
        # 桥接动作：show 归一为布尔（默认 True=打开）
        show_raw = params.get("show", True)
        if isinstance(show_raw, str):
            params["show"] = show_raw.strip().lower() not in ("false", "0", "no", "off")
        else:
            params["show"] = bool(show_raw)

    elif action == "click_button":
        # v0.9.0：按钮名（name）或按钮文本（text）至少其一——
        # 对应桥接 GET /ui/buttons 返回的字段，用于精确触发界面按钮点击。
        # v2.0：path（层级路径子串）/ type（组件类型）同样保留——同名按钮
        # （多个 CloseButton）必须靠 path 消歧，否则会点到别的面板。
        name = str(params.get("name") or "").strip()
        text = str(params.get("text") or "").strip()
        path = str(params.get("path") or "").strip()
        btn_type = str(params.get("type") or "").strip()
        if not name and not text and not path and not btn_type:
            raise ValueError(
                "click_button 需要 name / text / type / path 至少其一"
                "（按钮对象名、按钮文本、组件类型或层级路径子串）"
            )
        if name:
            params["name"] = name
        if text:
            params["text"] = text
        if path:
            params["path"] = path
        if btn_type:
            params["type"] = btn_type

    elif action == "phone_order":
        # T4：供应商关键词必填——缺失时曾静默放行，导致桥接按空供应商下单
        vendor = str(params.get("vendor") or "").strip()
        if not vendor:
            raise ValueError("phone_order 缺少 vendor 参数（供应商名称关键词）")
        params["vendor"] = vendor

    # reason / confidence（v0.9.13：可选字段容错——缺失/非法均给默认值，不报错）
    reason = str(data.get("reason") or "").strip() or "（模型未提供理由）"
    raw_confidence = data.get("confidence")
    if raw_confidence is None:
        confidence = 0.5  # 缺失按「中性置信」处理（0 分会被下游当作明确看空）
    else:
        try:
            confidence = float(raw_confidence)
        except (TypeError, ValueError):
            confidence = 0.5
    confidence = max(0.0, min(1.0, confidence))

    return {
        "action": action,
        "params": params,
        "reason": reason,
        "confidence": round(confidence, 3),
    }
