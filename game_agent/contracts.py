"""动作 / 状态 / 观察契约的唯一真相（single source of truth）。

本模块是 X01「动作契约同源化」的落地点：项目里原本散落 5+ 处的手工白名单
（``bridge.client.ACTIONS``、``brain.schema.BRIDGE_ACTION_PARAMS``、
``brain.schema.ALLOWED_ACTIONS``、``agent_loop`` 的夜晚/昼夜/周期/恢复白名单、
``agent_loop.ACTION_CN``）全部改为由本模块**派生**，不再各写一份。

设计原则（docs/26 §1 红线）
---------------------------
1. **只同源化，不改运行期行为。** 本模块的派生结果与改动前的手工常量逐项相等
   （见 ``artifacts/r1/draft_registry.py`` 的 24 项对照自检）；唯一有意的差异是
   补上 ``click_button.op`` 与 ``machine_module_install.occurrence`` 两个 C# 早就在读、
   Python 侧却漏登记的参数。
2. **不放宽校验。** ``validate_decision`` 的白名单仍由本模块派生，非法动作照旧三层拒绝。
3. **不编造事实。** 现状没有中文名的动作 ``cn_name`` 记 ``None``；C# 没有的字段
   （``inventory_counts`` / ``dialogue``）不进 ``STATE_FIELDS``，
   只登记在 ``STATE_FIELDS_MISSING_IN_CSHARP`` 里。
   **2026-11-15 修正**：``wholesale_cost`` 已由 CR-T04-1 补进 C#（见下），
   不再是「C# 没有的字段」——它从该集合移出，改为登记在
   ``FIELD_CHANNELS`` 的「仅桥接通」里。
4. **不改 C#。** 与 C# 的一致性靠 ``check_contract()`` 在运行期比对 ``GET /contract``
   （端点由 R2 新增），不一致即告警，不静默降级。

三张表的语义区别（不要合并）
---------------------------
- ``in_python_actions``：是否进 ``bridge.client.ACTIONS``（能发到 ``POST /action``）。
- ``in_param_table``：是否进 ``brain.schema.BRIDGE_ACTION_PARAMS``（模型能写参数）。
- ``model_visible``：契约/提示词层面模型是否该看见（``/contract`` 与提示词生成用）。

三者不重合是**现状语义**，不是冗余：
- ``force_dismiss``：可执行、但模型不可见、也不在参数表（恢复路径专用）。
- ``expedition_send`` / ``hire_worker``：可执行、参数表里有，但 1.0 未实装
  → ``model_visible=False`` + ``gated_until="post-launch"``。
- ``close_negociation`` / ``toggle_power``：C# 有 case，Python 侧已弃用
  → 三张表都不进，但 ``implemented_in_csharp=True``。
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from typing import Any, Optional

__all__ = [
    # 数据模型
    "ParamSpec",
    "ActionSpec",
    "BaseSpec",
    "SPECS",
    "BASE_SPECS",
    "BY_NAME",
    "ACTION_NAMES",
    # 派生视图
    "derive_actions",
    "derive_params",
    "derive_allowed",
    "derive_night_only",
    "derive_day_only",
    "derive_night_phase",
    "derive_once_per_cycle",
    "derive_recovery",
    "derive_action_cn",
    "derive_csharp_cases",
    "derive_model_visible",
    "derive_gated_actions",
    "spec_requires_identifier",
    "is_model_callable",
    "derive_model_callable",
    "is_live_contract",
    # 动作枚举
    "DEST_ALIASES",
    "DEST_IDS",
    "DEST_ALL",
    "DEST_ALIAS_MAP",
    "CLICK_OPS",
    "ECONOMY_MUTATING_ACTIONS",
    "CHEAT_ACTION_PATTERNS",
    # 状态签名
    "SIG_FIELDS",
    "SIG_INDEX",
    "SIG_IS_OUTSIDE",
    "SIG_MAP_OPEN",
    "SIG_DEGRADATION",
    "SIG_DEGRADATION_PENDING",
    # /state 字段
    "STATE_FIELDS",
    "STATE_GUARDED_BLOCKS",
    "STATE_ERROR_FIELDS",
    "STATE_OBSERVER_SYNTHESIZED",
    "STATE_FIELDS_MISSING_IN_CSHARP",
    "ERROR_SEMANTICS",
    "SILENT_CATCH_KEYS",
    "SILENT_CATCH_PATHS",
    "FIELD_OK",
    "FIELD_NULL",
    "FIELD_MISSING",
    "FIELD_ERROR",
    "FIELD_DEGRADED",
    "DEGRADATION_MODES",
    "SIG_SENTINEL_MISSING",
    "SIG_SENTINEL_NULL",
    "SIG_SENTINEL_ERROR",
    "field_state",
    "is_degraded",
    "sig_encode",
    "is_forbidden_guard_input",
    "FORBIDDEN_GUARD_INPUTS",
    # 分通道可用性
    "CHANNEL_BRIDGE",
    "CHANNEL_STATEWATCH",
    "CHANNELS",
    "FIELD_CHANNELS",
    "field_channels",
    "field_available_on",
    # MCP 工具导出（双协议工具层的单一真相）
    "MCP_TOOL_DISCLOSURE",
    "READONLY_ENDPOINTS",
    "build_mcp_tools",
    # 只读端点契约（/risk/endings）
    "RISK_ENDINGS_FIELDS",
    "RISK_ENDINGS_SUBFIELDS",
    "RISK_ENDINGS_FACTION_FIELDS",
    "RISK_ENDINGS_NULL_KIND",
    "RISK_ENDINGS_NON_NULL_PATHS",
    "check_risk_endings",
    # 运行期比对
    "ContractMismatch",
    "check_contract",
    "assert_contract",
    "contract_payload",
    "CONTRACT_VERSION",
]

#: 契约版本（与 ``game_agent.__version__`` 保持一致；``/contract`` 回吐此值）。
CONTRACT_VERSION = "2.1.0"


# ==========================================================================
# 1. 数据模型
# ==========================================================================


@dataclass(frozen=True)
class ParamSpec:
    """一个动作参数的契约。

    Attributes:
        type: 期望类型名，``str`` / ``int`` / ``float`` / ``bool`` / ``"int|str"``。
            仅供文档与提示词生成使用——``validate_decision`` 历史上**不做类型强转**
            （保持校验语义不变），类型信息留给调用方与观察层。
        required: C# 侧缺失该参数会直接报错。
        note: 人类可读说明（会进提示词）。
        enum: 允许取值（非空时提示词只列这些；校验层不强制，见上）。
    """

    type: str
    required: bool
    note: str = ""
    enum: tuple[str, ...] = ()


@dataclass(frozen=True)
class ActionSpec:
    """一个桥接动作的完整契约。"""

    name: str
    cn_name: Optional[str] = None
    """中文名。``None`` = 现状无中文名（**不编造**，由 R5 决定是否补）。"""

    params: dict[str, ParamSpec] = field(default_factory=dict)
    phase: str = "both"
    """``day`` | ``night`` | ``both`` | ``recovery``。

    这是 ``agent_loop._DAY_ONLY_ACTIONS`` / ``_NIGHT_ONLY_ACTIONS`` 的判定依据，
    会被 ``_pre_action_state_guard`` 用来按实时 ``store_state`` 拒绝动作。
    **不要把「夜晚阶段也可能用到」的动作标成 night**——那会让它在白天被误拒。
    """

    night_phase_ok: bool = False
    """打烊后（``night_phase=True``）是否仍在模型可见白名单里。

    与 ``phase`` 是**两套语义**：``_NIGHT_PHASE_ACTIONS``(15 项) 是「夜晚阶段允许
    模型看到的动作」，``_NIGHT_ONLY_ACTIONS``(7 项) 是「只能在 CLOSED 状态执行的动作」。
    ``scavenge`` / ``take_ground_items`` / ``equip_backpack`` / ``stow_loot`` /
    ``machine_put`` / ``machine_take`` 属于前者但不属于后者 → ``phase="both"`` +
    ``night_phase_ok=True``。
    """

    once_per_cycle: bool = False
    """是否进 ``_ONCE_PER_CYCLE_ACTIONS``（同一营业周期内重复执行是纯浪费）。"""

    recovery_fallback: Optional[str] = None
    """失败后的兜底动作（``_RECOVERY_ACTIONS``）。"""

    model_visible: bool = True
    """契约/提示词层面是否该让模型看见。"""

    in_python_actions: bool = True
    """是否进 ``bridge.client.ACTIONS``。"""

    in_param_table: bool = True
    """是否进 ``brain.schema.BRIDGE_ACTION_PARAMS``。"""

    implemented_in_csharp: bool = True
    """C# ``HandleAction`` 是否有对应 ``case``。"""

    deprecated: bool = False
    """C# 仍有实现但 Python 侧已弃用。"""

    gated_until: Optional[str] = None
    """未实装门禁（``"post-launch"``）。"""

    csharp_line: int = 0
    """``mod/src/BridgeApi.cs`` 中 ``case`` 的行号（文档/排查用）。"""

    guard_hint: str = ""
    """守卫提示：为什么这个动作会被拒、什么时候能选。会进提示词。"""

    since_version: str = ""
    """引入版本（``removed@x.y.z`` 表示已移除）。"""


@dataclass(frozen=True)
class BaseSpec:
    """非桥接的基础动作（直接走 control 层，不发 ``POST /action``）。"""

    name: str
    cn_name: str
    params: dict[str, ParamSpec]


def P(
    t: str,
    required: bool = False,
    note: str = "",
    enum: tuple[str, ...] = (),
) -> ParamSpec:
    """``ParamSpec`` 简写构造器。"""
    return ParamSpec(type=t, required=required, note=note, enum=enum)


_ID = {"identifier": P("str", True, "物品标识，从 visible_info/state 照抄")}
# 可选 identifier：C# 允许缺省并自动挑选（equip_backpack 缺省时按 identifier 含
# backpack / fanny_pack / back_pack 自动选，见 BridgeApi.cs:2971-2978），且
# brain/schema.py 的 _IDENTIFIER_ACTIONS 不含它 → 现状不强制，保持 required=False。
_OPT_ID = {
    "identifier": P(
        "str", False, "可选；缺省时按 identifier 含 backpack/fanny_pack 自动选"
    )
}
_NONE: dict[str, ParamSpec] = {}


# ---------------------------------------------------------------- 枚举常量

#: ``go_out`` 接受的**地点别名**（C# `switch (dest)` 的 14 个别名去掉 4 个 upgrade
#: 家族成员，那 4 个见 ``DEST_ALIAS_MAP``）。出处：mod/src/BridgeApi.cs:2807-2814。
#:
#: 为什么用硬编码别名而不是 ``/state.locations[].id`` 动态枚举：
#: ``locations`` 是 ``AfterhourLocation.GetAll()`` 的**动态注册表**（可含 mod 地点），
#: 而 ``go_out`` 认的是上面这组**硬编码别名 + id 透传**。两者不是同一集合，
#: 只按 ``locations[].id`` 校验会漏掉别名（bazar / 黑市 / 拾荒 …），只按别名校验
#: 会漏掉 mod 注册的地点。因此契约取**并集**：别名 ∪ 已知 id，并保留一次「透传给
#: C# 让它用真实清单报错」的机会——这也是 C# 现在的行为（未知目的地由
#: ``AfterhourLocation.Get(locId) == null`` 兜底报错并回显真实 id 清单）。
DEST_ALIASES: tuple[str, ...] = (
    "commissary",
    "ration",
    "配给站",
    "杂货",
    "bazar",
    "黑市",
    "垃圾场",
    "scavenging",
    "scavenge",
    "拾荒",
)

#: 已知的注册表地点 id（``/state.locations[].id`` 的正式版最小集）。
DEST_IDS: tuple[str, ...] = ("commissary", "dumping_ground", "dr_jackson")

#: 旧名 → 正式版 id 的**映射表**（不是拒绝清单）。
#:
#: 2026-11-13 R4 发现的关键证据，推翻了早期结论：
#:
#: * ``tools/decompile_full/EmporiumAnalytics.cs:522-526``::
#:
#:       "dumping_ground" => "SCAVENGING",
#:       "commissary"     => "COMMISSARY",
#:       "dr_jackson"     => "UPGRADE_MERCHANT",
#:
#:   —— 升级商人就是 ``dr_jackson``，地点**没有被删，是被改名了**。
#: * ``MapUIManager.cs:25`` ``public GameObject UIUpgradeMerchantPanel;``
#: * ``MapUIManager.cs:412`` ``public void LeaveUpgradeMerchant()``
#: * ``DrJacksonLocation.cs:16`` ``public override string Id => "dr_jackson";``
#:
#: 早期依据 ``BridgeApi.cs:2813-2814`` 的注释「正式版地点表里没有『升级商人』
#: 独立地点……这里不猜，交给下面的 id 查找失败」判为「不存在、应拒绝」，
#: **该注释有误**（它说的是「没有独立地点」，而事实是「改名为 dr_jackson」）。
#: 因此本表语义是**映射**而非拒绝：这 4 个别名合法，指向 ``dr_jackson``。
#:
#: 重要：Python 侧**只登记这层语义，不做转换**。``go_out`` 的 destination 原样
#: 透传给 C#，由 C# 侧 ``switch (dest)`` 解析（R2 正在补 ``case "dr_jackson"``
#: 与 upgrade 别名分支）。在这里转换成 ``dr_jackson`` 会让「用户/模型写了什么」
#: 与「日志里记了什么」不一致，也会让 C# 侧的真实清单校验失去意义。
DEST_ALIAS_MAP: dict[str, str] = {
    "upgrade": "dr_jackson",
    "upgrade_merchant": "dr_jackson",
    "升级": "dr_jackson",
    "升级商人": "dr_jackson",
}

#: 契约允许的 destination 全集 = 别名 ∪ 已知 id ∪ 旧名映射键。
#:
#: 旧名（4 个 upgrade 家族成员）**在内**：它们是合法输入，只是会被 C# 解析到
#: ``dr_jackson``。这也是本任务**没有**行为收紧点的原因——早期版本曾把它们判为
#: 「必然失败、提前拒绝」，那个判断建立在错误证据上，已撤销。
DEST_ALL: tuple[str, ...] = DEST_ALIASES + DEST_IDS + tuple(DEST_ALIAS_MAP)

#: ``click_button`` / ``object_action`` 的 ``op`` 取值。
#: 出处：BridgeApi.cs:4656-4682 InvokeClickableAction——
#: `kind = IsNullOrEmpty(op) ? "click" : op.ToLowerInvariant()`，
#: 识别 double_click/doubleclick、right_click/rightclick，其余走 IClickable.OnClick。
CLICK_OPS: tuple[str, ...] = ("click", "double_click", "right_click")

_CLICK_LOCATOR = {
    "name": P("str", False, "对象名（精确）"),
    "text": P("str", False, "tooltip 文本（精确）"),
    "path": P("str", False, "层级路径子串（同名按钮消歧）"),
    "type": P("str", False, "组件类型名"),
    "op": P("str", False, "点击方式；空=click", CLICK_OPS),
}


# ==========================================================================
# 2. 动作登记表（61 条 = 59 个可达动作 + 2 个 C# 有实现但 Python 已弃用）
# ==========================================================================

SPECS: tuple[ActionSpec, ...] = (
    # ---------------------------------------------------- 营业开关
    ActionSpec(
        "open_shutter",
        "开门营业",
        _NONE,
        "both",
        csharp_line=1558,
        guard_hint="实时 store_state=CLOSED 时拒绝（会让当天结算回滚、天数卡死）",
        since_version="v0.2.0",
    ),
    ActionSpec(
        "close_shutter",
        "关门歇业",
        _NONE,
        "both",
        csharp_line=1573,
        since_version="v0.2.0",
    ),
    ActionSpec(
        "toggle_shutter",
        "切换门",
        _NONE,
        "both",
        csharp_line=1580,
        since_version="v0.2.0",
    ),
    # ---------------------------------------------------- 顾客队列
    ActionSpec(
        "call_next",
        "叫号迎客",
        _NONE,
        "day",
        csharp_line=1587,
        guard_hint="需 store_state=OPEN；空叫纯浪费",
        since_version="v0.2.0",
    ),
    ActionSpec(
        "dismiss_client",
        "送客离店",
        _NONE,
        "day",
        csharp_line=1624,
        guard_hint="需 store_state=OPEN",
        since_version="v0.2.0",
    ),
    ActionSpec(
        "force_dismiss",
        None,
        _NONE,
        "recovery",
        csharp_line=1667,
        model_visible=False,
        in_param_table=False,
        guard_hint=(
            "仅 stuck-client 自动恢复路径直调（agent_loop 连续 5 步卡客），模型不可见；"
            "不进 BRIDGE_ACTION_PARAMS"
        ),
        since_version="v1.5",
    ),
    # ---------------------------------------------------- 对话
    ActionSpec(
        "dialog_advance",
        "推进对话",
        _NONE,
        "both",
        csharp_line=1694,
        since_version="v0.2.0",
    ),
    ActionSpec(
        "dialog_choose",
        "对话选择",
        {
            "text": P("str", False, "选项文字（优先）"),
            "index": P("int", False, "选项下标（0 起，无 text 时用）"),
        },
        "both",
        csharp_line=1701,
        guard_hint="text 优先于 index",
        since_version="v0.2.0",
    ),
    # ---------------------------------------------------- UI 按钮化
    ActionSpec(
        "click_button",
        None,
        dict(_CLICK_LOCATOR),
        "both",
        night_phase_ok=True,
        csharp_line=1743,
        guard_hint="name/text/path/type 至少其一；夜晚 EndOfDay/Map UI 的玩家路径",
        since_version="v0.9.0",
    ),
    # ---------------------------------------------------- 成交
    ActionSpec(
        "accept_offer",
        "接受报价",
        _NONE,
        "both",
        csharp_line=1808,
        since_version="v0.2.0",
    ),
    ActionSpec(
        "accept_gun_order",
        None,
        _NONE,
        "both",
        csharp_line=1881,
        guard_hint="枪械订单先校验必选需求再成交",
        since_version="v3.3",
    ),
    ActionSpec(
        "accept_barter",
        None,
        _NONE,
        "both",
        csharp_line=1815,
        since_version="v3.6",
    ),
    ActionSpec(
        "buy_wholesale",
        "整批收购",
        _NONE,
        "both",
        csharp_line=2712,
        guard_hint="需 client.is_offer_wholesale=true（顾客主动整批出手）；现金须 ≥ table.wholesale_cost；"
                   "调游戏原生 OnWholesaleClick 一次买下全部货",
        since_version="v3.14",
    ),
    ActionSpec(
        "machine_module_install",
        None,
        {
            "machine": P("str", True, "机器标识"),
            "item": P("str", True, "模组物品标识"),
            "occurrence": P("int", False, "同名机器第几个（0 起）"),
        },
        "both",
        csharp_line=1837,
        guard_hint="occurrence 默认 0（BridgeApi.cs:1845 ParseIntOr(...,0)）",
        since_version="v3.6",
    ),
    ActionSpec(
        "open_negociation",
        "打开谈判",
        _NONE,
        "day",
        csharp_line=2162,
        guard_hint="需 store_state=OPEN",
        since_version="v0.2.0",
    ),
    ActionSpec(
        "clear_table",
        "清理展台",
        _NONE,
        "day",
        csharp_line=2181,
        guard_hint="需 store_state=OPEN",
        since_version="v0.2.0",
    ),
    # ---------------------------------------------------- 桌上 / 库存物品
    ActionSpec(
        "place_item",
        "展示物品",
        dict(_ID),
        "day",
        csharp_line=1939,
        guard_hint="identifier 必填；需 store_state=OPEN",
        since_version="v0.4.0",
    ),
    ActionSpec(
        "sell_item",
        "卖出",
        dict(_ID),
        "day",
        csharp_line=2197,
        recovery_fallback="clear_table",
        guard_hint="identifier 必填且必须来自 sellable_now；失败自动 clear_table",
        since_version="v0.2.0",
    ),
    ActionSpec(
        "buy_item",
        "收购",
        dict(_ID),
        "day",
        csharp_line=2257,
        guard_hint="identifier 必填；从 inventory.not_owned 选",
        since_version="v0.2.0",
    ),
    ActionSpec(
        "appraise",
        "鉴定估价",
        dict(_ID),
        "both",
        csharp_line=2306,
        guard_hint="identifier 必填",
        since_version="v0.2.0",
    ),
    ActionSpec(
        "display_case",
        "放入展示柜",
        dict(_ID),
        "both",
        csharp_line=2331,
        guard_hint="identifier 必填；从 inventory.owned 选",
        since_version="v0.2.0",
    ),
    # ---------------------------------------------------- 店铺经营
    ActionSpec(
        "clear_trash",
        "倒垃圾",
        _NONE,
        "both",
        csharp_line=2364,
        guard_hint="garbage_level 高时降吸引力；纯增益动作，无前置条件",
        since_version="v0.2.0",
    ),
    ActionSpec(
        "alway_call",
        "开启自动迎宾",
        _NONE,
        "both",
        csharp_line=2382,
        since_version="v0.2.0",
    ),
    ActionSpec(
        "pay_rent",
        "交房租",
        _NONE,
        "both",
        once_per_cycle=True,
        csharp_line=2390,
        guard_hint="现金不足被拒；成功后有租金预留守卫",
        since_version="v0.2.0",
    ),
    ActionSpec(
        "pay_loan",
        "还贷款",
        _NONE,
        "both",
        csharp_line=2426,
        guard_hint="loan_days 归零必须还，拖欠会被收回店铺",
        since_version="v0.2.0",
    ),
    ActionSpec(
        "phone_order",
        "电话订货",
        {"vendor": P("str", True, "供应商关键词")},
        "both",
        csharp_line=2447,
        guard_hint="vendor 必填（commissary/red_imp/mining/pharma/chem/wild/rev）",
        since_version="v0.8.0",
    ),
    ActionSpec(
        "fill_request",
        "交付委托",
        _NONE,
        "both",
        csharp_line=2484,
        since_version="v0.8.0",
    ),
    ActionSpec(
        "buy_insurance",
        None,
        _NONE,
        "both",
        once_per_cycle=True,
        csharp_line=2510,
        guard_hint="每周期仅一次（重复买=白花钱）；现金不留出租金时禁止",
        since_version="v0.8.0",
    ),
    ActionSpec(
        "buy_table",
        "收购桌上物品",
        _NONE,
        "day",
        csharp_line=2517,
        guard_hint="需 store_state=OPEN",
        since_version="v0.8.0",
    ),
    ActionSpec(
        "sell_table",
        "卖出桌上物品",
        _NONE,
        "day",
        csharp_line=2529,
        guard_hint="需 store_state=OPEN",
        since_version="v0.8.0",
    ),
    ActionSpec(
        "end_day",
        "打烊结账",
        _NONE,
        "both",
        once_per_cycle=True,
        csharp_line=2540,
        guard_hint="store_state=CLOSED 时绝不再选（重复换日）；成功后进入夜晚阶段",
        since_version="v0.2.0",
    ),
    ActionSpec(
        "begin_day",
        "开始新一天",
        _NONE,
        "both",
        night_phase_ok=True,
        once_per_cycle=True,
        csharp_line=2624,
        guard_hint="夜晚终点兼异常逃生出口；store_state=CLOSED 时拒绝",
        since_version="v0.2.0",
    ),
    ActionSpec(
        "save_game",
        "保存进度",
        _NONE,
        "both",
        csharp_line=2657,
        guard_hint="纯副作用动作，不推进游戏状态",
        since_version="v0.2.0",
    ),
    # ---------------------------------------------------- 夜晚流程
    ActionSpec(
        "night_services",
        None,
        _NONE,
        "night",
        night_phase_ok=True,
        csharp_line=2669,
        guard_hint="点睡袋打开夜晚服务面板；需 store_state=CLOSED",
        since_version="v0.8.0",
    ),
    ActionSpec(
        "night_service",
        None,
        {
            "service": P("str", True, "服务 id 或中文名关键词"),
            "enable": P("bool", False, "默认 true=选中；字符串 false/0/no/off 为假"),
        },
        "night",
        night_phase_ok=True,
        csharp_line=2689,
        guard_hint="service 必填；价格一律读 services[].cost",
        since_version="v0.8.0",
    ),
    ActionSpec(
        "end_day_confirm",
        None,
        _NONE,
        "night",
        night_phase_ok=True,
        once_per_cycle=True,
        csharp_line=2773,
        guard_hint="=睡觉，点完立刻进次日；必须最后执行，之后不得再做夜晚动作",
        since_version="v0.8.0",
    ),
    ActionSpec(
        "open_map",
        None,
        _NONE,
        "night",
        night_phase_ok=True,
        csharp_line=2783,
        guard_hint="地图已打开时再调用=空转（sig[19] map_open 幂等守卫）",
        since_version="v0.8.0",
    ),
    ActionSpec(
        "go_out",
        None,
        {"destination": P("str", True, "地点别名或注册表 id", DEST_ALL)},
        "night",
        night_phase_ok=True,
        csharp_line=2793,
        guard_hint="受重伤或 night.visit_left<=0 时拒绝；别名与旧名见 DEST_ALL（upgrade 家族 = dr_jackson 旧名）",
        since_version="v0.8.0",
    ),
    ActionSpec(
        "stay_home",
        None,
        _NONE,
        "night",
        night_phase_ok=True,
        csharp_line=2850,
        guard_hint="=关闭地图并打开睡前服务面板，不是直接入睡",
        since_version="v0.8.0",
    ),
    ActionSpec(
        "scavenge",
        None,
        _NONE,
        "both",
        night_phase_ok=True,
        csharp_line=3009,
        guard_hint=(
            "需先 go_out scavenging；可反复执行；"
            "不能列入 night-only（会被 store_state 守卫误拒）"
        ),
        since_version="v0.8.0",
    ),
    ActionSpec(
        "take_ground_items",
        None,
        _NONE,
        "both",
        night_phase_ok=True,
        csharp_line=2994,
        guard_hint="地面物品离开即消失——必须在 leave_outside 前完成",
        since_version="v1.7",
    ),
    ActionSpec(
        "equip_backpack",
        None,
        dict(_OPT_ID),
        "both",
        night_phase_ok=True,
        csharp_line=2948,
        guard_hint="出门前必做，否则只有两口袋会丢战利品；identifier 可缺省",
        since_version="v1.7",
    ),
    ActionSpec(
        "stow_loot",
        None,
        _NONE,
        "both",
        night_phase_ok=True,
        csharp_line=2887,
        guard_hint="拾荒后与 leave_outside 前必做（地面物品离开即消失）",
        since_version="v1.7",
    ),
    ActionSpec(
        "leave_outside",
        None,
        _NONE,
        "night",
        night_phase_ok=True,
        csharp_line=3026,
        guard_hint="需先 stow_loot；否则卡在外面",
        since_version="v0.8.0",
    ),
    # ---------------------------------------------------- 电话
    ActionSpec(
        "open_phone",
        None,
        _NONE,
        "both",
        csharp_line=3047,
        since_version="v0.8.0",
    ),
    ActionSpec(
        "phone_call",
        None,
        {
            "number": P("int", False, "电话号（转 int）"),
            "contact": P("str", False, "联系人名称关键词"),
        },
        "both",
        csharp_line=3056,
        guard_hint="number 与 contact 至少其一；number 非数字报错",
        since_version="v0.8.0",
    ),
    ActionSpec(
        "phone_book",
        None,
        {"show": P("bool", False, "默认 true=打开")},
        "both",
        csharp_line=3107,
        since_version="v0.8.0",
    ),
    ActionSpec(
        "hang_up",
        None,
        _NONE,
        "both",
        csharp_line=3124,
        since_version="v0.8.0",
    ),
    # ---------------------------------------------------- 治安与战斗
    ActionSpec(
        "report_client",
        None,
        _NONE,
        "both",
        csharp_line=3136,
        since_version="v0.8.0",
    ),
    ActionSpec(
        "enter_shooting_mode",
        None,
        _NONE,
        "both",
        csharp_line=3172,
        since_version="v0.8.0",
    ),
    ActionSpec(
        "shoot",
        None,
        _NONE,
        "both",
        csharp_line=3181,
        guard_hint="--no-mouse 下无法瞄准，仅保留只读状态",
        since_version="v0.8.0",
    ),
    ActionSpec(
        "exit_shooting_mode",
        None,
        _NONE,
        "both",
        csharp_line=3194,
        since_version="v0.8.0",
    ),
    # ---------------------------------------------------- 机器
    ActionSpec(
        "machine_put",
        None,
        {
            "machine": P("str", True, "机器标识"),
            "item": P("str", True, "放入的物品标识"),
            "occurrence": P("int", False, "同名机器第几个（0 起；缺省 0）"),
            "slot": P("int", False, "槽位下标（不写=自动选）"),
        },
        "both",
        night_phase_ok=True,
        csharp_line=3203,
        guard_hint="machine 必填；熔炉需通电；同名机器靠 occurrence 消歧",
        since_version="v2.3",
    ),
    ActionSpec(
        "machine_take",
        None,
        {
            "machine": P("str", True, "机器标识"),
            "occurrence": P("int", False, "同名机器第几个（0 起；缺省 0）"),
            "slot": P("int", False, "槽位下标（不写=取全部产出槽）"),
        },
        "both",
        night_phase_ok=True,
        csharp_line=3279,
        guard_hint="machine 必填；产出留在机器里不能卖，必须先 take 到库存",
        since_version="v2.3",
    ),
    # ---------------------------------------------------- 玩家输入管线
    ActionSpec(
        "ui_hit",
        None,
        {"x": P("int", True), "y": P("int", True)},
        "both",
        csharp_line=2078,
        guard_hint="坐标取整且非负；allow_mouse=False 模式下不执行",
        since_version="v2.5",
    ),
    ActionSpec(
        "ui_click",
        None,
        {
            "x": P("int", False),
            "y": P("int", False),
            "button": P("str", False, "鼠标键"),
            "double": P("bool", False),
        },
        "both",
        csharp_line=2082,
        guard_hint="坐标取整且非负（x/y 与 button/double 可缺省）",
        since_version="v2.5",
    ),
    ActionSpec(
        "ui_drag",
        None,
        {
            "from_x": P("int", True),
            "from_y": P("int", True),
            "to_x": P("int", True),
            "to_y": P("int", True),
        },
        "both",
        csharp_line=2122,
        guard_hint="四个坐标均取整且非负；拖拽派发需 UI 处于可交互态",
        since_version="v2.6",
    ),
    ActionSpec(
        "object_action",
        None,
        dict(_CLICK_LOCATOR),
        "both",
        csharp_line=2137,
        guard_hint="name/text/path/type 至少其一",
        since_version="v2.5",
    ),
    # ------------------------------------------- 远征 / 雇工（post-launch 门禁）
    ActionSpec(
        "expedition_send",
        None,
        {
            "location": P("str", False, "远征地点关键词"),
            "hireling": P("str", False, "雇工关键词"),
        },
        "both",
        csharp_line=2095,
        model_visible=False,
        gated_until="post-launch",
        guard_hint="1.0 未实装，C# 全部路径恒 Err、不改变任何状态",
        since_version="v2.5",
    ),
    ActionSpec(
        "hire_worker",
        None,
        {"hireling": P("str", False, "雇工关键词")},
        "both",
        csharp_line=2112,
        model_visible=False,
        gated_until="post-launch",
        guard_hint="1.0 未实装，C# 恒 Err",
        since_version="v2.5",
    ),
    # ----------------------------------- 虚拟输入（玩家输入链路，v3.12）
    # 走游戏完整 Input → EventSystem → 游戏逻辑链路：Harmony 改写 106 个 Input 调用点，
    # 逐帧喂入虚拟鼠标/键盘。坐标是 Unity 屏幕坐标（左下原点），可从 /ui/nodes 取。
    ActionSpec(
        "player_click",
        "虚拟点击",
        {
            "x": P("int", True, "Unity 屏幕坐标 X（左下原点，从 /ui/nodes 取）"),
            "y": P("int", True, "Unity 屏幕坐标 Y"),
            "scene": P("str", False, "上次状态读到的 scene；不匹配则拒绝（陈旧守卫）"),
        },
        "both",
        csharp_line=233,  # IsVirtualAction 独立分派（非 switch case）
        guard_hint="坐标须来自当前 UI 几何；场景变化即拒；执行约 3 帧",
        since_version="v3.12",
    ),
    ActionSpec(
        "player_drag",
        "虚拟拖拽",
        {
            "x": P("int", True, "起点 X"), "y": P("int", True, "起点 Y"),
            "to_x": P("int", True, "终点 X"), "to_y": P("int", True, "终点 Y"),
            "scene": P("str", False, "陈旧状态守卫"),
        },
        "both",
        csharp_line=233,  # IsVirtualAction 独立分派（非 switch case）
        guard_hint="走完整 IDrag/IDrop 链路，落点由游戏判定是否接受",
        since_version="v3.12",
    ),
    ActionSpec(
        "player_key",
        "虚拟按键",
        {
            "key": P("str", True, "Unity KeyCode 名（如 Return / Escape / W）"),
            "frames": P("int", False, "按住帧数，1~180，默认 1"),
        },
        "both",
        csharp_line=233,  # IsVirtualAction 独立分派（非 switch case）
        guard_hint="KeyCode 名须合法；frames 夹 1~180",
        since_version="v3.12",
    ),
    ActionSpec(
        "player_scroll",
        "虚拟滚动",
        {
            "x": P("int", True, "Unity 屏幕坐标 X"), "y": P("int", True, "Y"),
            "amount": P("float", False, "滚动量，默认 1"),
        },
        "both",
        csharp_line=233,  # IsVirtualAction 独立分派（非 switch case）
        guard_hint="用于滚动列表；须先确认坐标在目标面板内",
        since_version="v3.12",
    ),
    ActionSpec(
        "player_wait",
        "虚拟等待",
        {"frames": P("int", False, "等待帧数，1~180，默认 1")},
        "both",
        csharp_line=233,  # IsVirtualAction 独立分派（非 switch case）
        guard_hint="逐帧等待，供 UI 动画/转场完成",
        since_version="v3.12",
    ),
    ActionSpec(
        "player_stop",
        "停止虚拟输入",
        _NONE,
        "both",
        csharp_line=233,  # IsVirtualAction 独立分派（非 switch case）
        guard_hint="主动取消当前虚拟输入计划；始终可调用",
        since_version="v3.12",
    ),
    # ----------------------------------- 物品坐标校准（玩家路径前置，v3.13）
    ActionSpec(
        "ui_item_point",
        "物品屏幕坐标",
        {"identifier": P("str", True, "物品标识，从 visible_info.inventory 照抄")},
        "both",
        csharp_line=5984,
        guard_hint="只读；物品被遮挡时返回明确错误（需打开容器或关重叠面板）",
        since_version="v3.13",
    ),
    ActionSpec(
        "ui_grid_point",
        "库存网格屏幕坐标",
        {
            "area": P("str", False, "库存区域：front/back/counter/ground/pocketLeft/pocketRight/backpackSlot，默认 counter"),
            "column": P("int", False, "网格列（0 起，向右）"),
            "row": P("int", False, "网格行（0 起，向下）"),
        },
        "both",
        csharp_line=5984,
        guard_hint="只读；配合 player_drag 把物品拖到指定格；越界返回错误",
        since_version="v3.13",
    ),
    # --------------------------------- C# 有实现但 Python 侧已弃用（不在 ACTIONS）
    ActionSpec(
        "close_negociation",
        None,
        _NONE,
        "both",
        csharp_line=2171,
        model_visible=False,
        in_python_actions=False,
        in_param_table=False,
        deprecated=True,
        guard_hint="v0.9.4 已取消（用户令）；C# case 保留但 Python 不可达",
        since_version="v0.8.2-removed@v0.9.4",
    ),
    ActionSpec(
        "toggle_power",
        "切换电源",
        _NONE,
        "both",
        csharp_line=2350,
        model_visible=False,
        in_python_actions=False,
        in_param_table=False,
        deprecated=True,
        guard_hint="v1.0 移除；C# 恒 Err（电源只能走 night_service POWER_SERVICE）",
        since_version="removed@v1.0",
    ),
)


# ==========================================================================
# 3. 基础动作（非桥接，直接走 control 层）
# ==========================================================================

BASE_SPECS: tuple[BaseSpec, ...] = (
    BaseSpec("click", "点击", {"x": P("int", True), "y": P("int", True)}),
    BaseSpec("ctrl_click", "快速转移", {"x": P("int", True), "y": P("int", True)}),
    BaseSpec("move", "移动鼠标", {"x": P("int", True), "y": P("int", True)}),
    BaseSpec("press_key", "按键", {"key": P("str", True), "times": P("int", False)}),
    BaseSpec("type_text", "输入文本", {"text": P("str", True)}),
    BaseSpec("wait", "稍等片刻", {"seconds": P("float", False)}),
    BaseSpec("stop", "收工", _NONE),
)


# ==========================================================================
# 4. 派生视图（下游模块只允许用这些函数，不得再写手工副本）
# ==========================================================================

#: 名字 → ActionSpec 索引（含已弃用动作，供 /contract 与排查使用）。
BY_NAME: dict[str, ActionSpec] = {s.name: s for s in SPECS}

#: 全部已登记的动作名（61，含 2 个弃用）。
ACTION_NAMES: tuple[str, ...] = tuple(s.name for s in SPECS)


def derive_actions() -> tuple[str, ...]:
    """``bridge.client.ACTIONS`` 的唯一来源（59 项，顺序即提示词/CLI 展示顺序）。"""
    return tuple(s.name for s in SPECS if s.in_python_actions)


def derive_params() -> dict[str, set[str]]:
    """``brain.schema.BRIDGE_ACTION_PARAMS`` 的唯一来源（58 项）。"""
    return {s.name: set(s.params) for s in SPECS if s.in_param_table}


def derive_allowed() -> dict[str, set[str]]:
    """``brain.schema.ALLOWED_ACTIONS`` 的唯一来源（7 个基础动作）。"""
    return {b.name: set(b.params) for b in BASE_SPECS}


def derive_night_only() -> set[str]:
    """``agent_loop._NIGHT_ONLY_ACTIONS``（7 项）：只能在 store_state=CLOSED 时执行。"""
    return {s.name for s in SPECS if s.phase == "night"}


def derive_day_only() -> set[str]:
    """``agent_loop._DAY_ONLY_ACTIONS``（9 项）：只能在 store_state=OPEN 时执行。"""
    return {s.name for s in SPECS if s.phase == "day"}


def derive_night_phase() -> frozenset[str]:
    """夜晚阶段模型可见白名单（15 项）。

    同源于 ``agent_loop._NIGHT_PHASE_ACTIONS`` 与 ``brain.brain.NIGHT_PHASE_ACTIONS``
    ——两处原本各写一份，现在都从这里派生。
    """
    return frozenset(s.name for s in SPECS if s.night_phase_ok)


def derive_once_per_cycle() -> frozenset[str]:
    """``agent_loop._ONCE_PER_CYCLE_ACTIONS``（5 项）。"""
    return frozenset(s.name for s in SPECS if s.once_per_cycle)


def derive_recovery() -> dict[str, str]:
    """``agent_loop._RECOVERY_ACTIONS``（``{"sell_item": "clear_table"}``）。"""
    return {s.name: s.recovery_fallback for s in SPECS if s.recovery_fallback}


def derive_action_cn() -> dict[str, str]:
    """``agent_loop.ACTION_CN``（34 项：7 基础 + 27 桥接）。

    ``cn_name=None`` 的动作**不出现**在此表里（现状没有中文名，不编造）。
    """
    out = {b.name: b.cn_name for b in BASE_SPECS}
    out.update({s.name: s.cn_name for s in SPECS if s.cn_name})
    return out


def derive_csharp_cases() -> set[str]:
    """C# ``HandleAction`` 的 ``case`` 名单（61 项）。"""
    return {s.name for s in SPECS if s.implemented_in_csharp}


def derive_model_visible() -> set[str]:
    """模型在提示词里应看见的动作（供 R5 生成提示词动作清单）。"""
    return {s.name for s in SPECS if s.model_visible and s.in_python_actions}


def derive_gated_actions() -> dict[str, str]:
    """未实装门禁表（``{"expedition_send": "post-launch", ...}``）。"""
    return {s.name: s.gated_until for s in SPECS if s.gated_until}


def spec_requires_identifier(name: str) -> bool:
    """该动作是否**必须**携带非空 ``identifier``（T4 校验面）。

    只有 ``identifier`` 在注册表里标 ``required=True`` 的动作才算。
    ``equip_backpack`` 的 identifier 是可选的（C# 缺省时会自动挑背包，
    见 BridgeApi.cs 的 afterhourPocketSlotInvBackpack 分支），故返回 False。

    未知动作名返回 False（防御性：调用方可能传入基础动作名）。
    """
    spec = BY_NAME.get(name)
    if spec is None:
        return False
    param = spec.params.get("identifier")
    return bool(param is not None and param.required)


def is_model_callable(action: str) -> bool:
    """该动作**能否被模型在提示词里看到并选择**（T12 运行时兜底，P0）。

    判据（四项全满足）::

        in_python_actions  and  model_visible  and  not deprecated
                           and  gated_until is None

    为什么需要这个谓词：``gated_until="post-launch"`` 如果只是元数据，那叫
    「记了一笔」，不叫冻结 —— 下游（提示词生成、校验层、恢复路径）只能靠人肉
    遵守。docs/26 §1 硬约束⑥ 禁止实现派遣/招募，``expedition_send`` /
    ``hire_worker`` 在 1.0 未实装（官方 Roadmap：2026-12 中旬 Junker 开局+远征），
    因此它们必须做到「模型永远调不到」，而不只是「注释里写了 gated」。

    注意与相邻概念的区别，不要混用：

    * ``in_python_actions`` —— 动作名是否在 ``bridge.client.ACTIONS`` 里
      （``expedition_send``/``hire_worker`` 为 **True**：C# 有 case、Python 有
      名字，不能删，删了 C# 就成孤儿 case）。
    * ``model_visible`` —— 是否出现在给模型的提示词动作清单里。
    * ``is_model_callable`` —— 本函数，**运行时能不能被选**。这是三者中最严格的：
      物理在册（in_python_actions）不等于可被选。

    未知动作名返回 False（防御性：调用方可能传入 ``wait`` 等基础动作名，
    它们不在 ``SPECS`` 里）。
    """
    spec = BY_NAME.get(action)
    if spec is None:
        return False
    return bool(
        spec.in_python_actions
        and spec.model_visible
        and not spec.deprecated
        and spec.gated_until is None
    )


def derive_model_callable() -> set[str]:
    """模型在运行期真正可选的动作集合（``is_model_callable`` 的批量形式）。

    应恒等于 ``derive_model_visible()`` —— 两者语义一致，分开提供是为了让
    调用点读起来自解释（提示词生成用 ``derive_model_visible``，校验/门禁用
    ``derive_model_callable``）。
    """
    return {s.name for s in SPECS if is_model_callable(s.name)}


# ==========================================================================
# 5. 防作弊契约的机器可读依据（R7 的 `GAP: 禁止作弊接口` 直接消费）
# ==========================================================================

#: 会**直接**改动现金 / 天数的动作。这是给一致性测试用的**正向清单**：
#: 任何能改钱/改天的动作必须在此登记，未登记却命中 ``CHEAT_ACTION_PATTERNS`` 即为违规。
#: 判据（只读事实，不臆造）：BridgeApi.cs 中写 ``playerCash`` / ``dayUntilRent`` /
#: ``StoreStation.dayCounter`` 的 case。
ECONOMY_MUTATING_ACTIONS = frozenset(
    {
        "accept_offer",
        "accept_barter",
        "accept_gun_order",
        "buy_item",
        "buy_table",
        "buy_insurance",
        "phone_order",
        "sell_item",
        "sell_table",
        "appraise",
        "fill_request",
        "pay_rent",
        "pay_loan",
        "end_day",
        "end_day_confirm",
        "begin_day",
        "go_out",
    }
)

#: 明显属于「作弊接口」的命名模式——``/action`` 白名单里出现即视为契约违规
#: （docs/26 §1 硬约束：不得提供直接改现金/天数/声望的接口）。
CHEAT_ACTION_PATTERNS: tuple[str, ...] = (
    "set_cash",
    "add_cash",
    "add_money",
    "set_money",
    "give_money",
    "set_day",
    "jump_day",
    "skip_day",
    "add_day",
    "set_reputation",
    "set_stock",
    "set_inventory",
    "reveal",
    "truth",
    "real_condition",
    "cheat",
    "debug_",
)


# ==========================================================================
# 6. 状态签名（state_signature）的具名常量
# ==========================================================================

#: ``game_agent.structured.channels.state_signature()`` 返回的 21 元组逐位含义。
#: 出处：game_agent/structured/channels.py:94-149（原 20 位，末位 ``night.get("map_open")``）。
#:
#: **第 21 位（下标 20）是 v2.2 追加的降级证据**（task-7 / CR-X01-2 by R3）::
#:
#:     tuple(  # () = 无降级证据
#:       sorted((block, field, flag) for 每个不可信块)
#:     )
#:     flag ∈ {FIELD_ERROR, FIELD_MISSING, FIELD_NULL}   # 复用本模块的四态常量
#:
#: 三条设计约束（R3 提出，我核对后认可，写在这里防止后人"顺手优化"）：
#:
#: 1. **刻意不含 ``_error`` 文本**：错误消息每次可能带时间戳/新值，纳入签名会抖动
#:    → 事件驱动循环无限重规划。
#: 2. **前 20 位不套 ``sig_encode()``**：那会把「键缺失」的 ``None`` 变成
#:    ``SIG_SENTINEL_MISSING``，直接打破 ``tests/test_structured_module.py`` 的
#:    既有断言。所以「null ≠ missing」的区分落在第 21 位的 ``flag`` 上
#:    （模式 C → FIELD_NULL，键缺失 → FIELD_MISSING，``_error`` 非空 → FIELD_ERROR）。
#: 3. **必须可哈希且顺序稳定**：签名要参与 ``==`` 比较（agent_loop.py:298
#:    ``sig == prev_sig``），所以用 ``tuple(sorted(...))`` 而非 set / dict。
#:
#: 第 21 位是**追加**的，前 20 位语义与顺序一律不动 —— 因此 ``SIG_IS_OUTSIDE``(12)、
#: ``SIG_MAP_OPEN``(19) 仍然有效，``agent_loop.py`` 的既有下标全部不受影响。
#:
#: **R3 变更请求**：channels.py 目前各处用 ``sig[N]`` 裸下标访问
#: （agent_loop.py:198 sig[2]、:199 sig[5]、:200 sig[15]、:233 sig[2]、
#:  :517 state_signature[3]、:765 state_signature[4] 等），应改为按本表具名访问，
#: 并加一条 `len(state_signature({})) == len(SIG_FIELDS)` 的形状断言。
SIG_FIELDS: tuple[str, ...] = (
    "in_game",
    "scene",
    "store_state",
    "shutter_closed",
    "cash",
    "client.identifier",
    "client.is_selling",
    "client.is_buying",
    "dialog.is_end",
    "dialog.is_choice",
    "table.negociated_item.identifier",
    "table.negociated_value",
    "night.is_outside",
    "night.current_location",
    "client.sec_button_lit",
    "queue.can_next",
    "table.nego_open",
    "day_until_rent",
    "economy.stock_value",
    "night.map_open",
    # —— v2.2 降级证据（task-7 / CR-X01-2）——
    "degradation",
)

#: 字段名 → 下标。
SIG_INDEX: dict[str, int] = {name: i for i, name in enumerate(SIG_FIELDS)}

#: 12（与 ``agent_loop._SIG_IS_OUTSIDE`` 一致）。
SIG_IS_OUTSIDE: int = SIG_INDEX["night.is_outside"]

#: 19（与 ``agent_loop._SIG_MAP_OPEN`` 一致）。
SIG_MAP_OPEN: int = SIG_INDEX["night.map_open"]

#: 20 —— 降级证据位（v2.2 追加）。R3 的 channels.py 落位后由 ``SIG_DEGRADATION`` 消费。
SIG_DEGRADATION: int = SIG_INDEX["degradation"]

#: 迁移期开关：``channels.state_signature()`` 尚未追加第 21 位时为 True。
#:
#: 存在理由：``SIG_FIELDS`` 属 R1（本文件），``state_signature()`` 的实现属 R3
#: （``structured/channels.py``），两者**无法原子落地**。在 R3 把 channels.py 的
#: 返回元组从 20 位扩到 21 位之前，长度相等断言必然不成立。
#:
#: **状态：已于 2026-11-13 关闭**（R3 的 task-7 落地，``state_signature({})`` 实测
#: 返回 21 位且末位为 ``degradation`` 证据）。改回 True 只在需要重新分期时使用；
#: 长期为 True 等于自检永久停留在宽松模式（docs/27 §0.8 记过的「假信号训练人
#: 无视门禁」）。
SIG_DEGRADATION_PENDING: bool = False


# ==========================================================================
# 7. `/state` 顶层字段契约
# ==========================================================================

#: ``mod/src/BridgeApi.cs`` 的 ``BuildState()`` 实际发射的**深度 0** JSON 键，
#: 共 **56** 个，按 C# 发射顺序排列。
#:
#: 其中 34 个是业务块 / 标量，22 个是 ``<block>_error`` 兄弟键（见
#: ``STATE_GUARDED_BLOCKS``）。**不要把地点枚举、动作名混进来**——这里只收
#: ``/state`` 顶层键。
STATE_FIELDS: tuple[str, ...] = (
    "in_game",
    "scene",
    "cash",
    "store_state",
    "day",
    "day_until_rent",
    "rent_value",
    "rent_due",
    "can_pay_rent",
    "is_power_on",
    "garbage_level",
    "screen",
    "screen_error",
    "shutter_closed",
    "shutter_error",
    "client",
    "client_error",
    "dialog",
    "dialog_error",
    "table",
    "table_error",
    "inventory",
    "inventory_error",
    "economy",
    "economy_error",
    "reputations",
    "reputations_error",
    "requests",
    "requests_error",
    "unlocks",
    "services",
    "services_error",
    "difficulty",
    "difficulty_error",
    "perks",
    "perks_error",
    "mortgage",
    "mortgage_error",
    "afterhour_events",
    "afterhour_events_error",
    "locations",
    "locations_error",
    "appraisal",
    "appraisal_error",
    "risk",
    "risk_error",
    "aquaculture",
    "aquaculture_error",
    "night",
    "night_error",
    "phone",
    "phone_error",
    "queue",
    "queue_error",
    "shooting",
    "shooting_error",
)

#: ``<block>_error`` 键 → 它所守护的业务字段。
#:
#: **降级语义（重要，R3 必须遵守）**：``BuildState()`` 对每个业务块都是
#: ``try { 发射块 } catch (Exception e) { 发射 "<block>_error": e.Message }``
#: （例：mod/src/BridgeApi.cs:225 ``screen_error``）。因此：
#:
#: >>> ``_error`` 非空  ==  该业务块**本次未发射**（键缺失）或值为 undefined  ==
#: >>> 同名业务字段的「空/零值」**不可信**，不得当作「真实为空」参与决策。
#:
#: 典型静默失效：``inventory_error`` 非空时 ``inventory.owned`` 缺失，
#: 若下游按 ``owned == []`` 判断「库存空了」，会触发错误的进货/售卖决策。
STATE_GUARDED_BLOCKS: dict[str, str] = {
    "screen_error": "screen",
    "shutter_error": "shutter_closed",
    "client_error": "client",
    "dialog_error": "dialog",
    "table_error": "table",
    "inventory_error": "inventory",
    "economy_error": "economy",
    "reputations_error": "reputations",
    "requests_error": "requests",
    "services_error": "services",
    "difficulty_error": "difficulty",
    "perks_error": "perks",
    "mortgage_error": "mortgage",
    "afterhour_events_error": "afterhour_events",
    "locations_error": "locations",
    "appraisal_error": "appraisal",
    "risk_error": "risk",
    "aquaculture_error": "aquaculture",
    "night_error": "night",
    "phone_error": "phone",
    "queue_error": "queue",
    "shooting_error": "shooting",
}

#: 全部 ``*_error`` 兄弟键（22 项）。
STATE_ERROR_FIELDS: tuple[str, ...] = tuple(STATE_GUARDED_BLOCKS)

#: 模式 B（空 ``catch { }``）静默降级：键**照常发射**，但值是写死的默认值
#: （``false`` / ``-1`` / ``0``），下游**看不到任何失败信号**——连「出过错」都
#: 不知道。这比模式 A 危险：模式 A 至少有 ``_error`` 兄弟键。
#:
#: 生成方式（可复现）：``artifacts/r1/probe_silent_catch_keys.py``
#:
#:   1. 定位 ``BuildState()``（mod/src/BridgeApi.cs:181-812）
#:   2. 剥掉注释与字面量后按大括号配对，找出所有 ``try { ... } catch { }``
#:      （catch 体为空）的 try 块范围
#:   3. 收集这些 try 块内出现的键发射点 ``\"key\":``
#:   4. 键 → JSON 路径用「字面量发射流 + 深度扫描」得出。**字符字面量必须并入
#:      发射流**，因为 C# 用 ``sb.Append('}')`` / ``Append(']')`` 发射闭合括号
#:      （全文 136 处），漏掉会让括号栈永不回退、路径全部错位。
#:
#: 实测：46 处空 catch，落入 16 个 try 块，影响 **112 个键 / 17 个块**。
#:
#: **R3 守卫要求**：任何用下面这些键做判定的守卫，都必须意识到「值可能是默认值」。
#: 哨兵值（如 ``-1``）尚可识别，但 ``false`` / ``0`` 与真实情况**不可区分**；
#: 守卫的输入恰恰大量落在这里（``can_next`` / ``has_client`` / ``can_pay_rent`` /
#: ``rent_due`` / ``night.*`` / ``mortgage.*``）。

SILENT_CATCH_KEYS: dict[str, str] = {
    "afterhour_events":        "<top-level>",
    "appraisal":               "<top-level>",
    "aquaculture":             "<top-level>",
    "can_pay_rent":            "<top-level>",
    "dialog":                  "<top-level>",
    "difficulty":              "<top-level>",
    "economy":                 "<top-level>",
    "locations":               "<top-level>",
    "mortgage":                "<top-level>",
    "night":                   "<top-level>",
    "phone":                   "<top-level>",
    "queue":                   "<top-level>",
    "rent_due":                "<top-level>",
    "reputations":             "<top-level>",
    "requests":                "<top-level>",
    "risk":                    "<top-level>",
    "active":                  "afterhour_events",
    "closes_location":         "afterhour_events",
    "days_left":               "afterhour_events",
    "effects":                 "afterhour_events",
    "location":                "afterhour_events",
    "modifier_key":            "afterhour_events",
    "modifier_value":          "afterhour_events",
    "polarity":                "afterhour_events",
    "days_missed":             "appraisal",
    "introduced_day":          "appraisal",
    "progress":                "appraisal",
    "promotion_pending":       "appraisal",
    "rank":                    "appraisal",
    "rank_name":               "appraisal",
    "count":                   "aquaculture",
    "identifier":              "aquaculture",
    "items":                   "aquaculture",
    "name":                    "aquaculture",
    "choices":                 "dialog",
    "index":                   "dialog",
    "is_choice":               "dialog",
    "is_end":                  "dialog",
    "text":                    "dialog",
    "appraisal_join_cost_mult": "difficulty",
    "insurance_mult":          "difficulty",
    "permit_fee":              "difficulty",
    "power_mult":              "difficulty",
    "property_base_price":     "difficulty",
    "rent_increase":           "difficulty",
    "rules":                   "difficulty",
    "subtitle":                "difficulty",
    "attractiveness":          "economy",
    "attractiveness_base":     "economy",
    "attractiveness_projector": "economy",
    "contraband_markup":       "economy",
    "critical":                "economy",
    "deal_maker_bonus":        "economy",
    "high":                    "economy",
    "insurance_days_without":  "economy",
    "loan_days":               "economy",
    "loan_value":              "economy",
    "lottery_number":          "economy",
    "low":                     "economy",
    "mid":                     "economy",
    "retail_markup":           "economy",
    "stock_value":             "economy",
    "wild_favor":              "economy",
    "available":               "locations",
    "last_visit_day":          "locations",
    "source":                  "locations",
    "visits":                  "locations",
    "can_be_offered":          "mortgage",
    "down_payment":            "mortgage",
    "mortgage_weeks_offered":  "mortgage",
    "store_price":             "mortgage",
    "total_weeks":             "mortgage",
    "weekly_payment":          "mortgage",
    "weeks_left":              "mortgage",
    "current_location":        "night",
    "in_transit":              "night",
    "is_outside":              "night",
    "map_open":                "night",
    "player_location":         "night",
    "scavenging_attempts":     "night",
    "seriously_wounded":       "night",
    "visit_left":              "night",
    "wound_stable":            "night",
    "wound_state":             "night",
    "wounded_fresh":           "night",
    "book_shown":              "phone",
    "contacts":                "phone",
    "current_client":          "phone",
    "open":                    "phone",
    "can_next":                "queue",
    "has_client":              "queue",
    "faction":                 "reputations",
    "id":                      "reputations",
    "value":                   "reputations",
    "can_fill":                "requests",
    "credit_reward":           "requests",
    "duration":                "requests",
    "reputation_reward":       "requests",
    "requested_item":          "requests",
    "requester":               "requests",
    "required_count":          "requests",
    "required_item":           "requests",
    "title":                   "requests",
    "contraband_crime":        "risk",
    "crime_count":             "risk",
    "crime_value":             "risk",
    "liquid_luck_days_left":   "risk",
    "luck":                    "risk",
    "trafficking_crimes":      "risk",
    "can_shoot":               "shooting",
    "mode":                    "shooting",
    "shooting":                "unlocks",
}

SILENT_CATCH_PATHS: dict[str, tuple[str, ...]] = {
    "afterhour_events":        ("afterhour_events",),
    "appraisal":               ("appraisal",),
    "aquaculture":             ("aquaculture",),
    "can_pay_rent":            ("can_pay_rent",),
    "dialog":                  ("dialog",),
    "difficulty":              ("difficulty",),
    "economy":                 ("economy",),
    "locations":               ("locations",),
    "mortgage":                ("mortgage",),
    "night":                   ("night",),
    "phone":                   ("phone",),
    "queue":                   ("queue",),
    "rent_due":                ("rent_due",),
    "reputations":             ("reputations",),
    "requests":                ("requests",),
    "risk":                    ("risk",),
    "active":                  ("afterhour_events.active",),
    "closes_location":         ("afterhour_events.closes_location",),
    "days_left":               ("afterhour_events.days_left",),
    "effects":                 ("afterhour_events.effects",),
    "location":                ("afterhour_events.location",),
    "modifier_key":            ("afterhour_events.effects.modifier_key",),
    "modifier_value":          ("afterhour_events.effects.modifier_value",),
    "polarity":                ("afterhour_events.effects.polarity",),
    "days_missed":             ("appraisal.days_missed",),
    "introduced_day":          ("appraisal.introduced_day",),
    "progress":                ("appraisal.progress",),
    "promotion_pending":       ("appraisal.promotion_pending",),
    "rank":                    ("appraisal.rank",),
    "rank_name":               ("appraisal.rank_name",),
    "count":                   ("aquaculture.items.count",),
    "identifier":              ("aquaculture.items.identifier",),
    "items":                   ("aquaculture.items",),
    "name":                    ("aquaculture.items.name",),
    "choices":                 ("dialog.choices",),
    "index":                   ("dialog.choices.index",),
    "is_choice":               ("dialog.is_choice",),
    "is_end":                  ("dialog.is_end",),
    "text":                    ("dialog.text", "dialog.choices.text",),
    "appraisal_join_cost_mult": ("difficulty.rules.appraisal_join_cost_mult",),
    "insurance_mult":          ("difficulty.rules.insurance_mult",),
    "permit_fee":              ("difficulty.rules.permit_fee",),
    "power_mult":              ("difficulty.rules.power_mult",),
    "property_base_price":     ("difficulty.rules.property_base_price",),
    "rent_increase":           ("difficulty.rules.rent_increase",),
    "rules":                   ("difficulty.rules",),
    "subtitle":                ("difficulty.subtitle",),
    "attractiveness":          ("economy.attractiveness",),
    "attractiveness_base":     ("economy.attractiveness_base",),
    "attractiveness_projector": ("economy.attractiveness_projector",),
    "contraband_markup":       ("economy.contraband_markup",),
    "critical":                ("economy.contraband_markup.critical",),
    "deal_maker_bonus":        ("economy.deal_maker_bonus",),
    "high":                    ("economy.contraband_markup.high",),
    "insurance_days_without":  ("economy.insurance_days_without",),
    "loan_days":               ("economy.loan_days",),
    "loan_value":              ("economy.loan_value",),
    "lottery_number":          ("economy.lottery_number",),
    "low":                     ("economy.contraband_markup.low",),
    "mid":                     ("economy.contraband_markup.mid",),
    "retail_markup":           ("economy.retail_markup",),
    "stock_value":             ("economy.stock_value",),
    "wild_favor":              ("economy.wild_favor",),
    "available":               ("locations.available",),
    "last_visit_day":          ("locations.last_visit_day",),
    "source":                  ("locations.source",),
    "visits":                  ("locations.visits",),
    "can_be_offered":          ("mortgage.can_be_offered",),
    "down_payment":            ("mortgage.down_payment",),
    "mortgage_weeks_offered":  ("mortgage.mortgage_weeks_offered",),
    "store_price":             ("mortgage.store_price",),
    "total_weeks":             ("mortgage.total_weeks",),
    "weekly_payment":          ("mortgage.weekly_payment",),
    "weeks_left":              ("mortgage.weeks_left",),
    "current_location":        ("night.current_location",),
    "in_transit":              ("night.in_transit",),
    "is_outside":              ("night.is_outside",),
    "map_open":                ("night.map_open",),
    "player_location":         ("night.player_location",),
    "scavenging_attempts":     ("night.scavenging_attempts",),
    "seriously_wounded":       ("night.seriously_wounded",),
    "visit_left":              ("night.visit_left",),
    "wound_stable":            ("night.wound_stable",),
    "wound_state":             ("night.wound_state",),
    "wounded_fresh":           ("night.wounded_fresh",),
    "book_shown":              ("phone.book_shown",),
    "contacts":                ("phone.contacts",),
    "current_client":          ("phone.current_client",),
    "open":                    ("phone.open",),
    "can_next":                ("queue.can_next",),
    "has_client":              ("queue.has_client",),
    "faction":                 ("reputations.faction",),
    "id":                      ("reputations.id",),
    "value":                   ("reputations.value",),
    "can_fill":                ("requests.can_fill",),
    "credit_reward":           ("requests.credit_reward",),
    "duration":                ("requests.duration",),
    "reputation_reward":       ("requests.reputation_reward",),
    "requested_item":          ("requests.requested_item",),
    "requester":               ("requests.requester",),
    "required_count":          ("requests.required_count",),
    "required_item":           ("requests.required_item",),
    "title":                   ("requests.title",),
    "contraband_crime":        ("risk.contraband_crime",),
    "crime_count":             ("risk.crime_count",),
    "crime_value":             ("risk.crime_value",),
    "liquid_luck_days_left":   ("risk.liquid_luck_days_left",),
    "luck":                    ("risk.luck",),
    "trafficking_crimes":      ("risk.trafficking_crimes",),
    "can_shoot":               ("shooting.can_shoot",),
    "mode":                    ("shooting.mode",),
    "shooting":                ("unlocks.shooting",),
}

#: ``SILENT_CATCH_KEYS`` 的路径版：键名在 /state 里可能重名，守卫必须按
#: **完整路径**取值，不能只按键名。重名键 7 个：
#: ``count`` / ``faction`` / ``id`` / ``retail_markup`` / ``shooting`` /
#: ``text`` / ``title``。


# --------------------------------------------------------------------------
# 7.1 降级三态编码（``null`` 与「键缺失」必须可区分）
# --------------------------------------------------------------------------
#
# 二态（「有值 / 没值」）在本项目里反复出事：Python 侧 ``dict.get(key)`` 对
# 「显式 null」和「键压根没出现」都返回 ``None``，于是「把不知道当成没有」。

#: 键存在、值非 null、所在块 ``_error`` 为空 —— 唯一可信的状态。
FIELD_OK = "ok"

#: 键存在但值为显式 ``null``（模式 C，例：``BridgeApi.cs:204`` 的 ``day``）。
FIELD_NULL = "null"

#: 键压根没出现（模式 A 的块整体失败，或模式 B 的键未发射）。
FIELD_MISSING = "missing"

#: 所在块 ``_error`` 非空 —— 块内容整体不可信。
FIELD_ERROR = "error"

#: 全部非 ok 状态（守卫不得在这些状态下按「真实为空」处理）。
FIELD_DEGRADED: frozenset[str] = frozenset({FIELD_NULL, FIELD_MISSING, FIELD_ERROR})

#: 三种降级成因（与 ``STATE_GUARDED_BLOCKS`` / ``SILENT_CATCH_KEYS`` 配套）。
DEGRADATION_MODES: dict[str, str] = {
    "A": "try { 发射块 } catch (Exception e) { 发射 '<block>_error': e.Message }"
         " —— 22 处，下游看 _error 非空即知该块不可信（STATE_GUARDED_BLOCKS）",
    "B": "try { 取值 } catch { } —— 46 处空 catch，键照常发射但值是写死默认值"
         "（false / -1 / 0），下游完全看不到失败（SILENT_CATCH_KEYS，112 键）",
    "C": "catch { 发射 '<key>':null } —— 3 处，键发射了但值是显式 null"
         "（day / economy.attractiveness）",
}

#: ``state_signature`` 用的三个哨兵：使显式 ``null`` / 键缺失 / ``_error`` 在
#: 签名里**互不相等**，从而让「把不知道当成没有」在签名层面就可见。
SIG_SENTINEL_MISSING = "\x00missing"
SIG_SENTINEL_NULL = "\x00null"
SIG_SENTINEL_ERROR = "\x00error"


def field_state(
    container: Any,
    key: str,
    block_error: Optional[str] = None,
) -> str:
    """判定一个 /state 字段处于四态中的哪一种（供守卫与签名使用）。

    Args:
        container: 父对象（如 ``state["night"]``）；``None`` 视为缺失。
        key: 字段名。
        block_error: 兄弟 ``<block>_error`` 的值；非空则整块不可信。

    Returns:
        :data:`FIELD_OK` / :data:`FIELD_NULL` / :data:`FIELD_MISSING` /
        :data:`FIELD_ERROR`。
    """
    if block_error:
        return FIELD_ERROR
    if container is None:
        return FIELD_MISSING
    try:
        if key not in container:
            return FIELD_MISSING
        value = container[key]
    except TypeError:
        return FIELD_MISSING
    return FIELD_NULL if value is None else FIELD_OK


def is_degraded(state_flag: str) -> bool:
    """该字段状态是否属于「不可信」（非 ok）。"""
    return state_flag in FIELD_DEGRADED


def sig_encode(
    container: Any,
    key: str,
    block_error: Optional[str] = None,
) -> Any:
    """把字段编码成 ``state_signature`` 可直接入元组的可区分标记。

    与 :func:`field_state` 一一对应：``ok`` 返回原值，其余返回哨兵字符串
    （:data:`SIG_SENTINEL_*`），因此显式 ``None`` 与「键缺失」**不再相等**。
    """
    flag = field_state(container, key, block_error)
    if flag == FIELD_ERROR:
        return SIG_SENTINEL_ERROR
    if flag == FIELD_MISSING:
        return SIG_SENTINEL_MISSING
    if flag == FIELD_NULL:
        return SIG_SENTINEL_NULL
    return container[key]


#: 由 **observer 自行拼装**、并不来自 ``GET /state`` 的观察字段。
#:
#: - ``machines``：``structured/observer.py`` 调 **``GET /machines``**
#:   （C# ``MachinesJson()``）后注入。
#: - ``sellable`` / ``sellable_total``：``structured/observer.py`` 调
#:   **``GET /sellable``**（C# ``SellableJson()``）后注入，``sellable_total``
#:   是 Python 侧聚合出来的派生值、C# 完全没有。
#:
#: （此处只写端点名、不写行号：行号已漂移过多次，端点名才是稳定契约。）
#:
#: ``/contract`` 比对时**必须排除**这些名字，否则会误报「C# BuildState 多出/缺少字段」。
STATE_OBSERVER_SYNTHESIZED: tuple[str, ...] = (
    "machines",
    "sellable",
    "sellable_total",
)

#: 观察层引用、但 C# ``BuildState()`` 里**根本不存在**的字段（只登记事实，不补假值）。
#:
#: - ``inventory_counts``：C# 全文 0 次；``structured/observer.py`` 读
#:   ``state.get("inventory_counts")`` → bridge 通道恒 ``None``（T05 P0）；
#:   statewatch 通道由 ``statewatch/state.py`` 的 summary 提供。
#: - ``dialogue``：C# 全文 0 次；``structured/observer.py`` 写死 ``"dialogue": None``。
#:   **注意**：对话内容并非不可得——``/state.dialog`` 有
#:   ``is_end`` / ``is_choice`` / ``text`` / ``choices`` / ``index`` 与 ``dialog_error``，
#:   真实对话走 ``dialog`` 这条路径；``dialogue`` 只是历史占位字段。
#:
#: **``wholesale_cost`` 已于 2026-11-15 移出本集合**（CR-T04-1 兑现）：
#: ``mod/src/BridgeApi.cs:1234`` 的 ``BuildTable`` 现在真会发射它，源
#: ``ps.GetWholesalePrice()``（``< 0`` 时发 ``null``）。实机 ``/state`` 的
#: ``table`` 键里确实有它。它此前被列在这里，导致
#: ``FIELD_CHANNELS["wholesale_cost"] = frozenset()``（两通道都判不可用）、
#: ``prompt.py`` 明令「不要读它」、``agent_loop.py`` 文案断言「正式版无此字段」——
#: 三处消费方全部停在补字段之前的状态。**这就是「只改生产方、不回改消费方」
#: 的典型漂移**，由 ``artifacts/_lead_probe/probe_state_subkeys.py`` 抓出。
STATE_FIELDS_MISSING_IN_CSHARP: frozenset[str] = frozenset(
    {"inventory_counts", "dialogue"}
)

#: 供文档 / 提示词 / CR 引用的一句话语义。
ERROR_SEMANTICS = (
    "`/state` 的每个业务块都有 `<block>_error` 兄弟键；`_error` 非空表示该块"
    "本次读取失败、业务字段缺失或为 undefined，其值不可信，消费方必须先看 `_error`"
    "再决定是否使用该块内容（不得把降级的空值当成真实为空）。"
)


# --------------------------------------------------------------------------
# 7.2 禁区：恒不可得的字段不得作为守卫依据
# --------------------------------------------------------------------------


def is_forbidden_guard_input(path: str) -> bool:
    """恒不可得字段作为守卫输入 = 守卫静默失效（见 docs/27 §0.7 CR-T04-1/2/3）。

    ``inventory_counts`` / ``dialogue`` 在 ``mod/src/BridgeApi.cs`` 全文 **0 次**，
    观察层却仍在读它们（bridge 通道恒 ``None``）。任何 ``_guard*`` 函数以它们为
    判定输入，都会因「读到 None → 提前 return None（放行）」而**静默失效**。

    ``wholesale_cost`` **已于 2026-11-15 移出**：CR-T04-1 兑现、
    ``BridgeApi.cs:1234`` 真会发射它。因此「读 ``wholesale_cost`` 就禁止」这条
    规则现在**不再成立**——这正是原先 I3 断言要防的「补完字段后规则变假」。
    守卫仍不得**只**依赖它（批发价可能为 ``null``），但那是行为约束，
    见 ``agent_loop._rent_reserve_guard`` 的批发兜底分支。

    Args:
        path: 字段路径，可用点号分隔（``"table.wholesale_cost"``）或裸键名。

    Returns:
        True 表示该路径依赖了恒不可得字段，守卫不得使用。
    """
    if not path:
        return False
    return any(seg in STATE_FIELDS_MISSING_IN_CSHARP for seg in str(path).split("."))


#: :func:`is_forbidden_guard_input` 的判定集合（供文档 / 测试引用）。
FORBIDDEN_GUARD_INPUTS: frozenset[str] = STATE_FIELDS_MISSING_IN_CSHARP


# --------------------------------------------------------------------------
# 7.3 只读端点契约：``GET /risk/endings``（T07 / docs/27 §3.9）
# --------------------------------------------------------------------------
#
# 为什么单独立一节而不是塞进 ``STATE_FIELDS``：``/risk/endings`` 是**独立路由**，
# 不是 ``BuildState()`` 的顶层块。把它算进 ``STATE_FIELDS``（56）会让
# ``/contract`` 的 ``state_fields`` 与实际 ``/state`` 不符，三处硬编码断言
# （``tests/test_contracts.py`` / 本文件 ``:2220`` 一带 / 本文件 §7 注释）也会
# 统统对不上。**独立端点要有独立的字段契约。**
#
# 门禁 1→2 的原文是「H1/H2/H3 交接物齐全，**每个新字段有 diag 与 null 语义**」。
# 本节就是给 ``/risk/endings`` 补这一半：字段清单 + 每个 null 到底是什么意思。

#: ``/risk/endings`` 的顶层键。**任何情况下都必须全部出现**（包括
#: ``PlayerStore.Instance == null`` 的主菜单态）——形状随状态切换会让调用方被迫
#: 在两种 schema 间分支，这正是 ``/contract`` 把 ``state_fields``（声明）与
#: ``state_fields_live``（实测）分开报的同一条教训。出处：
#: ``mod/src/BridgeApi.cs`` 的 ``BuildRiskEndings()``（见
#: ``artifacts/_lead_probe/extract_risk_endings_keys.py`` 机械抽取，29 个键）。
RISK_ENDINGS_FIELDS: tuple[str, ...] = (
    "ok",
    "grace",
    "factions",
    "sentinel",
    "aug",
    "mortgage",
    "notes",
)

#: 子对象的键。``factions`` 是数组，元素键见 :data:`RISK_ENDINGS_FACTION_FIELDS`。
RISK_ENDINGS_SUBFIELDS: dict[str, tuple[str, ...]] = {
    "grace": ("owned", "purchasable", "cost", "cooldown_left"),
    "sentinel": ("pool", "ending_at", "days_since_injection", "is_sentinel"),
    "aug": ("presence", "ending_at", "ending_gate"),
    "mortgage": (
        "signed",
        "collateral_seized",
        "paid_off",
        "weeks_left",
        "weekly_payment",
        "total_weeks",
        "repo_possible",
    ),
}

#: ``factions[]`` 元素的键。
RISK_ENDINGS_FACTION_FIELDS: tuple[str, ...] = (
    "id",
    "name",
    "value",
    "ending_perk",
    "ending_at",
    "ending_unlocked",
)

#: **null 语义**：同一个端点里的 ``null`` 有两种完全不同的意思，消费方必须按字段区分。
#:
#: ``"unknown"``
#:     读不到（不在局内 / 反射失败 / 该版本无此成员）。**不得当成 0 或 False。**
#:     典型：主菜单态整个 ``grace``/``sentinel``/``aug``/``mortgage`` 全 null。
#: ``"not_applicable"``
#:     结构上不适用（该派系没有对应的结局 perk）。这是**事实**，不是缺失。
#:     典型：``FACTION_UPPER_LEVEL`` 的 ``ending_perk``/``ending_at``/``ending_unlocked``
#:     三个全 null —— 上层区**没有**派系结局线，不是读不到。
#:
#: 把前者当后者 = 「不知道」被渲染成「确实没有」，与 ``visible_info.slots``
#: 的 ``None``/``[]`` 事故（docs/27 §3.7b）是同一个失效模式。
RISK_ENDINGS_NULL_KIND: dict[str, str] = {
    "grace.owned": "unknown",
    "grace.purchasable": "unknown",
    "grace.cost": "unknown",
    "grace.cooldown_left": "unknown",
    "sentinel.pool": "unknown",
    "sentinel.days_since_injection": "unknown",
    "sentinel.is_sentinel": "unknown",
    "aug.presence": "unknown",
    "aug.ending_gate": "unknown",
    "mortgage.signed": "unknown",
    "mortgage.collateral_seized": "unknown",
    "mortgage.paid_off": "unknown",
    "mortgage.weeks_left": "unknown",
    "mortgage.weekly_payment": "unknown",
    "mortgage.total_weeks": "unknown",
    "mortgage.repo_possible": "unknown",
    "factions[].name": "unknown",
    "factions[].value": "unknown",
    "factions[].ending_perk": "not_applicable",
    "factions[].ending_at": "not_applicable",
    "factions[].ending_unlocked": "not_applicable",
}

#: **不属于 null 语义表**的字段——它们要么不可能为 null，要么是常量。
#:
#: ``factions[].id``
#:     ``SafeStr`` 兜底成 ``""`` 而非 ``null``：派系 id 是**身份**，用一个空串
#:     比用 null 更能让调用方立刻发现异常（``null`` 会被误认为「这个派系不存在」，
#:     而空串明显是坏数据）。任何拿 id 做 key 的地方都必须跳过空串。
#: 其余四项见 :data:`RISK_ENDINGS_CONSTANT_PATHS`。
RISK_ENDINGS_NON_NULL_PATHS: frozenset[str] = frozenset({"factions[].id"})


def check_risk_endings(remote: dict[str, Any]) -> list[str]:
    """校验 ``GET /risk/endings`` 载荷的**形状与 null 自洽性**，返回问题清单。

    与 :func:`check_contract` 的分工：那个比对的是「Python 注册表 vs C# 能力清单」，
    这个校验的是「C# 实际返回的这一份载荷是否自洽」。前者防漂移，后者防
    **同一个端点里 null 的两种含义被混用**。

    检查项：

    1. 顶层键必须**全部出现**（``ps == null`` 的主菜单态也一样）——形状随状态切换
       会让调用方写两套分支，而两套分支里总有一套没被测到。
    2. 每个子对象的键集必须**齐全**（同上理由）。
    3. ``factions[]`` 元素键必须齐全。
    4. ``ending_perk`` 与 ``ending_at``/``ending_unlocked`` 必须**同生同灭**：
       有 perk 就必有阈值与解锁态，没有就三者全 null。半边 null 说明生产者
       在一个分支里漏发或错发。
    5. 全部业务块都是 null（不在局内）时 ``notes`` **不得为空**——否则是
       「整体降级但一言不发」，调用方只能靠猜。这条直接对应本项目的铁律：
       **降噪的正确做法是折叠，不是删除；任何情况下都不能返回空内容却宣称一切正常。**

    Args:
        remote: ``/risk/endings`` 的 JSON 载荷。

    Returns:
        问题清单（空 = 自洽）。
    """
    problems: list[str] = []
    if not isinstance(remote, dict):
        return [f"/risk/endings 载荷不是对象：{type(remote).__name__}"]

    for key in RISK_ENDINGS_FIELDS:
        if key not in remote:
            problems.append(f"/risk/endings 缺顶层键：{key}（形状随状态切换会让调用方分支）")
    if problems:
        return problems

    for block, keys in RISK_ENDINGS_SUBFIELDS.items():
        got = remote.get(block)
        if not isinstance(got, dict):
            problems.append(f"{block} 不是对象：{type(got).__name__}（应为对象或全 null 对象）")
            continue
        missing = [k for k in keys if k not in got]
        if missing:
            problems.append(f"{block} 缺子键：{missing}")

    factions = remote.get("factions")
    if not isinstance(factions, list):
        problems.append(f"factions 不是数组：{type(factions).__name__}")
    else:
        for i, item in enumerate(factions):
            if not isinstance(item, dict):
                problems.append(f"factions[{i}] 不是对象：{type(item).__name__}")
                continue
            missing = [k for k in RISK_ENDINGS_FACTION_FIELDS if k not in item]
            if missing:
                problems.append(f"factions[{i}] 缺子键：{missing}")
                continue
            # 4. perk / 阈值 / 解锁态三者同生同灭
            perk, at, unlocked = (
                item["ending_perk"],
                item["ending_at"],
                item["ending_unlocked"],
            )
            trio = (perk is None, at is None, unlocked is None)
            if len(set(trio)) != 1:
                problems.append(
                    f"factions[{i}]（{item.get('id')}）的 ending_perk/ending_at/"
                    f"ending_unlocked 应当同时为 null 或同时非 null，实际 "
                    f"{perk!r}/{at!r}/{unlocked!r}"
                )

    # 5. 整体降级必须带话
    #
    # 判定「整体降级」不能写 ``all(remote.get(b) is None ...)``——主菜单态的
    # ``grace``/``sentinel``/``aug``/``mortgage`` 都是**对象**（形状不随状态切换，
    # 见第 1 条），只是里面的业务值全 null。首版就是按「块本身是 None」判的，
    # 结果自检里那条「全 null 但 notes 为空必报」直接抓不到，红在自检里而非测试里
    # ——这正是自检存在的意义：**断言本身先写错，就会得到一个永远为真的检查。**
    #
    # 正确口径：只看 :data:`RISK_ENDINGS_NULL_KIND` 里标成 ``unknown`` 的字段
    # ——``sentinel.ending_at``(20)、``aug.ending_at``(100) 是**常量**，主菜单态也
    # 照样有值，把它们算进「是否全 null」会让这条检查永远为假。
    degraded_values: list[Any] = []
    for path, kind in RISK_ENDINGS_NULL_KIND.items():
        if kind != "unknown" or path.startswith("factions["):
            continue
        block, _, sub = path.partition(".")
        got = remote.get(block)
        if isinstance(got, dict):
            degraded_values.append(got.get(sub))
        else:
            degraded_values.append(got)
    if degraded_values and all(v is None for v in degraded_values):
        if not (remote.get("notes") or []):
            problems.append(
                "全部业务值均为 null（不在局内）但 notes 为空——整体降级不得一言不发"
            )

    return problems


# ==========================================================================
# 8. 分通道可用性（bridge vs statewatch）
# ==========================================================================

#: 数据直读通道（``POST /action`` + ``GET /state``）。
CHANNEL_BRIDGE = "bridge"

#: 日志事件流通道（``game_agent/statewatch/``）。
CHANNEL_STATEWATCH = "statewatch"

#: 全部观察通道。
CHANNELS: frozenset[str] = frozenset({CHANNEL_BRIDGE, CHANNEL_STATEWATCH})

#: 只在某个通道有值的观察字段。**未出现在此表的字段默认两通道都可用。**
#:
#: 背景（Lead m00489 核实）：``client_id`` / ``intent`` / ``stage`` 在
#: ``mod/src/BridgeApi.cs`` 全文 **0 次**，但 ``structured/observer.py:1189-1191``
#: 读 ``client.get("client_id")`` / ``("intent")`` / ``("stage")``。它们的来源是
#: statewatch 通道（``statewatch/state.py:34-36`` 初始化、``:47-49`` 从
#: ``client_intent`` 事件填），**不是 C# 生产**。所以这不是 bug，但阶段 3 的 T13
#: 横切整合若不知道这个差异，会把「statewatch 有、bridge 恒 None」误判成数据缺失。
FIELD_CHANNELS: dict[str, frozenset[str]] = {
    # 规范化观察里的 client.intent / client.stage 只有 statewatch 通道会填
    "client.intent": frozenset({CHANNEL_STATEWATCH}),
    "client.stage": frozenset({CHANNEL_STATEWATCH}),
    # client.identifier 两通道都有：bridge 取 /state.client.identifier，
    # statewatch 取 summary.client_id（observer.py:1389）
    "client.identifier": CHANNELS,
    # 恒 None（C# 无此字段），只有 statewatch summary 提供
    "inventory_counts": frozenset({CHANNEL_STATEWATCH}),
    # **仅桥接通**（2026-11-15 更正）：``BridgeApi.cs:1234`` 发射它（源
    # ``ps.GetWholesalePrice()``，非批发报价时为 ``null``）；statewatch
    # 日志流里没有任何批发价线索，所以是 frozenset({CHANNEL_BRIDGE})——
    # 此前误写 ``frozenset()``（两通道都不可用），等于告诉下游「永远别读它」。
    "wholesale_cost": frozenset({CHANNEL_BRIDGE}),
    "dialogue": frozenset(),
    # observer 自行拼装（独立端点）
    "machines": CHANNELS,
    "sellable": CHANNELS,
    "sellable_total": CHANNELS,
}


def field_channels(path: str) -> frozenset[str]:
    """返回某观察字段在哪些通道可用（未登记 = 两通道都可用）。"""
    return FIELD_CHANNELS.get(path, CHANNELS)


def field_available_on(path: str, channel: str) -> bool:
    """该字段在指定通道是否可用。"""
    return channel in field_channels(path)


# ==========================================================================
# 9. 运行期契约比对（``GET /contract``）
# ==========================================================================


class ContractMismatch(RuntimeError):
    """C# ``GET /contract`` 与本地注册表不一致——必须告警，不得静默降级。"""

    def __init__(self, problems: list[str]) -> None:
        self.problems = list(problems)
        super().__init__(
            "契约不一致（%d 项）：\n  - %s"
            % (len(self.problems), "\n  - ".join(self.problems))
        )


def is_live_contract(action: str) -> bool:
    """该动作的 ``params`` 是「活契约」（C# 现在就真会读）还是「意向声明」。

    区别的由来：``expedition_send`` 与 ``hire_worker`` 是 1.0 未实装的门禁内容
    （``gated_until="post-launch"``），它们在 ``mod/src/BridgeApi.cs:2112-2138`` 的
    case 里读完 ``ps``/``expeditionData``/``isExpeditionUnlocked`` 之后**无条件**
    ``return Err(...)``，从不读 body 里的 ``location``/``hireling``。我们在注册表里
    登记这两个参数，描述的是**未来实现的意图**（提示词要教模型怎么调），不是
    C# 当前行为。

    因此参数比对必须区分两类：对活契约做双向严格比对；对意向声明只比对「C#
    多读了本地没登记的参数」（那是真的漂移——说明实现已经跑在我们前面了），
    不比对「本地登记了但 C# 还没读」（那是尚未兑现的意图，不是错误）。

    理由：无法消除的告警会训练人无视门禁（本项目的 ``docs/27 §0.8`` 日期戳就是
    同一个坑）。门禁宁可少报一类「必然存在」的差异，也不能常年挂着 2 条红字。
    """
    spec = BY_NAME.get(action)
    if spec is None:
        return False
    return spec.gated_until is None and not spec.deprecated


def check_contract(remote: dict[str, Any]) -> list[str]:
    """比对 ``GET /contract`` 回吐清单与本地注册表，返回问题清单（空 = 一致）。

    Args:
        remote: 形如::

            {
              "version": "2.1.0",
              "actions": [{"name": "click_button", "params": ["name", "op", ...]}, ...],
              "state_fields": ["in_game", "scene", ...],
            }

        字段缺失按空处理（便于灰度：R2 尚未实现 ``/contract`` 时不会误报）。

    Returns:
        问题清单。三种不一致场景（V6）都会被覆盖：
          - **C# 多一个 case** → ``remote.actions`` 含本地未登记的名字；
          - **Python 多一个 action** → 本地 ``implemented_in_csharp=True`` 但 remote 无此 case；
          - **参数表缺项** → 同一动作两侧参数集合不等。

    Note:
        **动作名集合全量比对，参数集合只对「活契约」双向比对。** 门禁
        （``gated_until`` 非空）与已废弃（``deprecated``）的动作，其 ``params`` 是
        「未来实现意图」的声明——C# 现在无条件 ``return Err``、根本不读这些参数，
        拿它做双向活比对必然产生无法消除的假告警，而无法消除的告警会训练人无视
        门禁（见 ``docs/27 §0.8``）。这类动作**仍会**报「C# 读了本地没登记的参数」，
        因为那说明实现已经跑在声明前面，是真漂移。判定见 :func:`is_live_contract`。

    Note:
        ``state_fields`` 比对会自动排除 ``STATE_OBSERVER_SYNTHESIZED``
        （``machines`` / ``sellable`` / ``sellable_total`` 由 observer 从独立端点拼装，
        不是 ``BuildState()`` 的产物）。

    Note:
        ``state_fields``（声明）与 ``state_fields_live``（实测）**不可混用**。R2 的
        ``/contract`` 拆分这两个字段就是因为：单次采样看不到 22 个只在块读失败时才
        发射的 ``*_error`` 键，拿采样当能力清单会让 55 个字段被误判成缺失。这里对
        live 只做单向硬约束 ``set(live) ⊆ set(declared)``——live 出现声明之外的键，
        说明源码已新增字段而静态表没跟上（那份「能力清单」已过时，比不报更糟）；
        反向（declared 有而 live 没有）是**正常**的，不报。
    """
    problems: list[str] = []

    remote_actions: dict[str, set[str]] = {}
    for item in remote.get("actions") or []:
        if isinstance(item, str):
            remote_actions[item] = set()
        elif isinstance(item, dict) and item.get("name"):
            remote_actions[str(item["name"])] = {
                str(x) for x in (item.get("params") or [])
            }

    if remote_actions:
        local_cs = derive_csharp_cases()
        for name in sorted(set(remote_actions) - local_cs):
            problems.append(f"C# 多出本地未登记的动作：{name}")
        for name in sorted(local_cs - set(remote_actions)):
            problems.append(f"本地登记 implemented_in_csharp=True 但 C# 无此 case：{name}")
        for name in sorted(set(remote_actions) & local_cs):
            local_params = set(BY_NAME[name].params)
            missing = local_params - remote_actions[name]
            extra = remote_actions[name] - local_params
            live = is_live_contract(name)
            # 「本地有、C# 不读」：活契约是漂移；意向声明是未兑现的意图，不报。
            if missing and live:
                problems.append(f"{name} 参数缺失：本地有 C# 不读的 {sorted(missing)}")
            # 「C# 读、本地未登记」：任何动作都是漂移——实现跑在声明前面了。
            if extra:
                problems.append(f"{name} 参数缺失：C# 读但本地未登记 {sorted(extra)}")

    remote_fields = {str(x) for x in (remote.get("state_fields") or [])}
    if remote_fields:
        remote_fields -= set(STATE_OBSERVER_SYNTHESIZED)
        local_fields = set(STATE_FIELDS)
        for name in sorted(local_fields - remote_fields):
            problems.append(f"契约登记了 C# BuildState 没有的 state 字段：{name}")
        for name in sorted(remote_fields - local_fields):
            problems.append(f"C# BuildState 多出未登记的 state 字段：{name}")

    # state_fields_live = 本次采样**真的发射了**的顶层键（``BuildState()`` 的实测输出）。
    # 它与 state_fields（声明）的分工：声明是权威能力清单，live 只用来发现「源码新增
    # 了字段而静态声明没跟上」。之所以必须分开，是因为 22 个 ``*_error`` 键只在对应块
    # 读失败时才发射——任何一次采样都看不到它们，拿单次采样当能力清单会让 55 个字段
    # 被误判成缺失（这正是不能把 live 直接当 ``state_fields`` 用的原因）。
    #
    # 判定：``live ⊆ declared`` 是硬约束。live 出现声明之外的键 = 源码有字段但静态表
    # 没登记 → 下一次 ``/contract`` 就会少报能力，比不报更糟（它看上去权威）。故这里
    # 是**硬失败**而非提示。反向（declared 有而 live 没有）**正常**，不报：没在局内时
    # 只有 ``in_game``，块级字段本就不发射。
    remote_live = remote.get("state_fields_live")
    if remote_live is not None:
        live_fields = {str(x) for x in (remote_live or [])}
        declared = set(STATE_FIELDS) | set(STATE_OBSERVER_SYNTHESIZED)
        for name in sorted(live_fields - declared):
            problems.append(
                f"state_fields_live 出现声明之外的字段：{name}"
                f"（源码已发射但 STATE_FIELDS 未登记，静态能力清单已过时）"
            )

    # 模式 B 静默降级台账：先自检本地台账，再与 C# 回吐的清单比对。
    problems.extend(_check_silent_catch_registry())
    remote_silent = remote.get("silent_catch_keys")
    if remote_silent:
        remote_keys = {str(k) for k in remote_silent}
        local_keys = set(SILENT_CATCH_KEYS)
        for name in sorted(remote_keys - local_keys):
            problems.append(f"C# 报出静默降级键但本地未登记：{name}")
        for name in sorted(local_keys - remote_keys):
            problems.append(f"本地登记为静默降级但 C# 未报：{name}")

    return problems


def _check_silent_catch_registry() -> list[str]:
    """``SILENT_CATCH_KEYS`` 自身的自洽性（漏登记 / 块名写错 / 路径悬空即报错）。

    这是模式 B 的机器可读台账：它必须**完整覆盖** C# 中所有「值可能退化成默认值」
    的键，否则下游守卫会以为读到的是真实值。任何一项不成立都说明台账与
    ``BuildState()`` 已经脱节。
    """
    problems: list[str] = []
    top_level = set(STATE_FIELDS)
    for key, block in sorted(SILENT_CATCH_KEYS.items()):
        if not block:
            problems.append(f"{key} 未标注所属块")
            continue
        if block != "<top-level>" and block not in top_level:
            problems.append(f"{key} 的所属块 {block!r} 不在 STATE_FIELDS 里（块名写错？）")
        paths = SILENT_CATCH_PATHS.get(key)
        if not paths:
            problems.append(f"{key} 缺 SILENT_CATCH_PATHS 登记（重名键必须给完整路径）")
            continue
        for path in paths:
            segs = path.split(".")
            if segs[0] not in top_level:
                problems.append(f"{key} 的路径 {path!r} 首段不是已登记的 state 字段")
            if ".".join(segs) != path or any(not s for s in segs):
                problems.append(f"{key} 的路径 {path!r} 格式非法")
        if key.endswith("_error"):
            problems.append(f"{key} 是 _error 键，不该出现在静默降级台账里")
    for key in sorted(set(SILENT_CATCH_PATHS) - set(SILENT_CATCH_KEYS)):
        problems.append(f"SILENT_CATCH_PATHS 多出未在 SILENT_CATCH_KEYS 登记的键：{key}")
    return problems


def assert_contract(remote: dict[str, Any]) -> None:
    """``check_contract`` 的严格版：有问题即抛 :class:`ContractMismatch`。"""
    problems = check_contract(remote)
    if problems:
        raise ContractMismatch(problems)


def contract_payload() -> dict[str, Any]:
    """本地注册表序列化为 ``GET /contract`` 的**期望形状**。

    既是给 R2 的接口约定，也是测试的 golden 数据。
    """
    return {
        "version": CONTRACT_VERSION,
        "actions": [
            {
                "name": s.name,
                "params": sorted(s.params),
                "phase": s.phase,
                "night_phase_ok": s.night_phase_ok,
                "model_visible": s.model_visible,
                "implemented_in_csharp": s.implemented_in_csharp,
                "deprecated": s.deprecated,
                "gated_until": s.gated_until,
            }
            for s in SPECS
        ],
        "state_fields": list(STATE_FIELDS),
        "state_error_fields": list(STATE_ERROR_FIELDS),
        "observer_synthesized_fields": list(STATE_OBSERVER_SYNTHESIZED),
        "sig_fields": list(SIG_FIELDS),
        "silent_catch_keys": {
            k: list(SILENT_CATCH_PATHS.get(k, ())) for k in SILENT_CATCH_KEYS
        },
    }


# ==========================================================================
# 9b. MCP 工具导出（docs/28 · 简历向双协议工具层）
# ==========================================================================
#
# 设计约束（与创意工坊 GameMcp 的关键差异）：
#   · 工具 schema **由本模块派生**，不另写一份硬编码清单；C# adapter 只负责协议，
#     工具定义从本文件导出的 JSON 读取。这样 ACTIONS / 参数表 / MCP 工具三者同源，
#     不会出现「模型看见的动作与校验层接受的动作不一致」。
#   · 只导出 model_visible 且未 deprecated / 未 gated 的动作，避免把
#     expedition_send 这类未实装动作暴露给外部 AI。
#   · readOnlyHint / destructiveHint 由动作是否改变游戏状态决定：这里采用保守口径——
#     所有 game_action 都视为可能改变状态，只读端点一律 readOnly。

#: 对外暴露的只读端点。路径必须与 mod/src/BridgeApi.cs::Route 的 GET case 一致。
#: 不暴露 /modelconfig（含 API Key）、/agent/start|stop（进程控制）、/decision（内部浮层）。
READONLY_ENDPOINTS: tuple[str, ...] = ("/state",)

#: MCP 工具描述里写入的操作纪律（docs/28 §2：把约束下沉到工具契约，而非只在提示词里求自觉）。
MCP_TOOL_DISCLOSURE: str = (
    "这是真实运行中的游戏，不是模拟器。"
    "先用 game_state 读取当前状态再决定动作；每个动作只做一件事，执行后重新读取受影响的状态。"
    "不接受屏幕坐标；物品用 identifier、地点用 locations 里的 id。"
    "不要跳过天数：夜晚流程是 end_day → night_service → end_day_confirm。"
    "交租日不要遣散房东；现金不足时只卖不买。"
)

_TYPE_MAP: dict[str, str] = {
    "str": "string",
    "int": "integer",
    "float": "number",
    "bool": "boolean",
    "json": "object",
}


def _mcp_param(spec: ParamSpec) -> dict[str, Any]:
    """把 ParamSpec 转成 MCP inputSchema 的单个 property。"""
    out: dict[str, Any] = {
        "type": _TYPE_MAP.get(spec.type, "string"),
        "description": spec.note or spec.type,
    }
    if spec.enum:
        out["enum"] = list(spec.enum)
    return out


def build_mcp_tools() -> dict[str, Any]:
    """导出 MCP ``tools/list`` 响应体（schema 由契约派生，零手工副本）。

    返回 ``{"tools": [...]}``，每个工具含 ``name`` / ``description`` /
    ``inputSchema`` / ``annotations``。C# 侧 adapter 启动时读取本函数导出的 JSON，
    因此新增/删除动作只需改 :data:`SPECS`，无需同步改 C# 工具表。
    """
    tools: list[dict[str, Any]] = []

    tools.append(
        {
            "name": "game_state",
            "description": (
                "读取游戏当前状态（现金/天数/场景/顾客/库存/夜晚/风险等）。"
                "可选 section 只取顶层字段以节省上下文；返回 JSON 文本。"
                "这是所有动作的前置步骤——行动后必须重新读取。"
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "section": {
                        "type": "string",
                        "description": "只取 state 的某个顶层段（如 client / inventory / night / rent / risk）；省略则返回全部。",
                    }
                },
                "additionalProperties": False,
            },
            "annotations": {
                "readOnlyHint": True,
                "destructiveHint": False,
                "idempotentHint": True,
                "openWorldHint": False,
            },
        }
    )

    # 动作枚举与参数表都来自 SPECS：只导出模型可见、未弃用、未 gated 的动作。
    visible = [
        s
        for s in SPECS
        if s.model_visible and not s.deprecated and not s.gated_until and s.in_python_actions
    ]
    all_params: dict[str, ParamSpec] = {}
    for s in visible:
        for name, spec in s.params.items():
            # 同名参数若在不同动作里类型不同，以先出现者为准并记录冲突（自检会报）。
            all_params.setdefault(name, spec)

    props: dict[str, Any] = {
        "action": {
            "type": "string",
            "enum": [s.name for s in visible],
            "description": "要执行的原子动作名；每次只执行一个。",
        },
        "scene": {
            "type": "string",
            "description": "上次 game_state 读到的 scene 名；若当前场景已变化则拒绝执行（陈旧状态守卫）。",
        },
    }
    props.update({name: _mcp_param(spec) for name, spec in sorted(all_params.items())})

    tools.append(
        {
            "name": "game_action",
            "description": (
                "执行一个原子游戏动作。" + MCP_TOOL_DISCLOSURE
                + " 返回 {ok, dispatched, verified, before, after}："
                "verified 只反映能自动核对的状态变化（现金/库存/天数），不可盲目信任 ok。"
            ),
            "inputSchema": {
                "type": "object",
                "properties": props,
                "required": ["action"],
                "additionalProperties": False,
            },
            "annotations": {
                "readOnlyHint": False,
                "destructiveHint": True,
                "idempotentHint": False,
                "openWorldHint": False,
            },
        }
    )

    return {"tools": tools}


# ==========================================================================
# 10. 自检入口（开发期对照真实代码；运行时不会触发）
# ==========================================================================


def selfcheck(verbose: bool = True) -> int:
    """对照**真实代码**校验派生结果（开发/CI 用，返回失败项数）。

    ``python -m game_agent.contracts`` 可在本地跑一遍。
    """
    import game_agent.agent_loop as agent_loop
    import game_agent.brain.brain as brain_mod
    from game_agent.brain.schema import ALLOWED_ACTIONS, BRIDGE_ACTION_PARAMS
    from game_agent.bridge.client import ACTIONS
    from game_agent.structured.channels import state_signature

    results: list[tuple[str, bool, str]] = []

    def chk(label: str, cond: bool, detail: str = "") -> None:
        results.append((label, bool(cond), detail))

    act = derive_actions()
    par = derive_params()

    chk(
        f"derive_actions() == ACTIONS（{len(ACTIONS)}，顺序一致）",
        act == tuple(ACTIONS) and len(act) == len(ACTIONS),
        f"derived={len(act)} live={len(ACTIONS)} same_order={act == tuple(ACTIONS)}",
    )
    chk(
        "derive_params() 键集 == BRIDGE_ACTION_PARAMS（58）",
        set(par) == set(BRIDGE_ACTION_PARAMS),
        f"only_derived={sorted(set(par) - set(BRIDGE_ACTION_PARAMS))} "
        f"only_live={sorted(set(BRIDGE_ACTION_PARAMS) - set(par))}",
    )
    # 迁移完成后 BRIDGE_ACTION_PARAMS 已是 derive_params() 的产物，两者必须
    # 逐动作逐项完全相等（落盘前那条「差异仅 click_button/machine_module_install」
    # 是迁移期检查，迁移完成后必然为空，故改为迁移后真正该成立的性质）。
    diffs = {
        k: (sorted(par[k]), sorted(BRIDGE_ACTION_PARAMS[k]))
        for k in par
        if par[k] != BRIDGE_ACTION_PARAMS[k]
    }
    chk(
        "派生参数表与线上 BRIDGE_ACTION_PARAMS 逐项相等（迁移后应无差异）",
        diffs == {},
        f"diffs={diffs}",
    )
    # 双向自洽：derive_params() 的值 == 对应 ActionSpec.params 的键集（正反双向）。
    spec_mismatch = []
    for name, spec in BY_NAME.items():
        if name not in par:
            continue
        if set(par[name]) != set(spec.params):
            spec_mismatch.append((name, sorted(set(par[name]) ^ set(spec.params))))
    reverse_missing = sorted(set(par) - set(act))
    chk(
        "derive_params() 与 ActionSpec.params 双向一致（无孤儿参数、参数表 ⊆ ACTIONS）",
        not spec_mismatch and not reverse_missing,
        f"param_set_mismatch={spec_mismatch} params_not_in_actions={reverse_missing}",
    )
    chk("click_button 含 op（C# BridgeApi.cs:1760 读取）", "op" in par["click_button"], "")
    chk(
        "machine_module_install 含 occurrence（C# BridgeApi.cs:1845 读取）",
        "occurrence" in par["machine_module_install"],
        "",
    )
    chk(
        "derive_allowed() == ALLOWED_ACTIONS（7）",
        derive_allowed() == {k: set(v) for k, v in ALLOWED_ACTIONS.items()},
        "",
    )
    chk(
        "derive_night_only() == _NIGHT_ONLY_ACTIONS（7）",
        derive_night_only() == set(agent_loop._NIGHT_ONLY_ACTIONS),
        f"derived={sorted(derive_night_only())}",
    )
    chk(
        "derive_day_only() == _DAY_ONLY_ACTIONS（9）",
        derive_day_only() == set(agent_loop._DAY_ONLY_ACTIONS),
        f"derived={sorted(derive_day_only())}",
    )
    chk(
        "derive_night_phase() == _NIGHT_PHASE_ACTIONS == brain.NIGHT_PHASE_ACTIONS（15）",
        derive_night_phase() == agent_loop._NIGHT_PHASE_ACTIONS
        == brain_mod.NIGHT_PHASE_ACTIONS,
        f"derived={len(derive_night_phase())} "
        f"loop={len(agent_loop._NIGHT_PHASE_ACTIONS)} "
        f"brain={len(brain_mod.NIGHT_PHASE_ACTIONS)}",
    )
    chk(
        "derive_once_per_cycle() == _ONCE_PER_CYCLE_ACTIONS（5）",
        derive_once_per_cycle() == agent_loop._ONCE_PER_CYCLE_ACTIONS,
        "",
    )
    chk(
        "derive_recovery() == _RECOVERY_ACTIONS",
        derive_recovery() == agent_loop._RECOVERY_ACTIONS,
        f"derived={derive_recovery()}",
    )
    chk(
        "derive_action_cn() == ACTION_CN（34）",
        derive_action_cn() == agent_loop.ACTION_CN,
        f"derived={len(derive_action_cn())} live={len(agent_loop.ACTION_CN)} "
        f"only_derived={sorted(set(derive_action_cn()) - set(agent_loop.ACTION_CN))} "
        f"only_live={sorted(set(agent_loop.ACTION_CN) - set(derive_action_cn()))}",
    )
    chk(
        f"C# case {len(derive_csharp_cases())}（含 close_negociation / toggle_power；"
        "player_* 走 IsVirtualAction 独立分派）",
        derive_csharp_cases() >= set(derive_actions()),
        f"n={len(derive_csharp_cases())}",
    )
    chk(
        "C# ⊇ ACTIONS ⊇ PARAMS ∪ {force_dismiss}",
        derive_csharp_cases() >= set(act) >= set(par) | {"force_dismiss"},
        "",
    )
    chk(
        "每条 ActionSpec 都有 csharp_line + since_version",
        all(s.csharp_line and s.since_version for s in SPECS),
        "",
    )
    live_len = len(state_signature({}))
    if SIG_DEGRADATION_PENDING:
        # 迁移期：SIG_FIELDS 已 21 位，channels.py 仍 20 位。只断言「前 20 位对齐 + 差 1 位」，
        # 且标签里明写 PENDING，避免这条宽松断言被误当成「已经对齐」。
        chk(
            "SIG_FIELDS 长度 == state_signature({}) + 1"
            "（PENDING：等 R3 扩到 21 位后改 21）",
            len(SIG_FIELDS) == live_len + 1 == 21
            and SIG_FIELDS[live_len] == "degradation",
            f"SIG_FIELDS={len(SIG_FIELDS)} live={live_len}",
        )
    else:
        chk(
            "SIG_FIELDS 长度 == state_signature({}) == 21",
            len(SIG_FIELDS) == live_len == 21
            and SIG_FIELDS[20] == "degradation",
            f"SIG_FIELDS={len(SIG_FIELDS)} live={live_len}",
        )
    chk(
        "SIG_IS_OUTSIDE / SIG_MAP_OPEN 与 agent_loop 常量一致",
        (SIG_IS_OUTSIDE, SIG_MAP_OPEN)
        == (agent_loop._SIG_IS_OUTSIDE, agent_loop._SIG_MAP_OPEN),
        f"registry=({SIG_IS_OUTSIDE},{SIG_MAP_OPEN}) "
        f"live=({agent_loop._SIG_IS_OUTSIDE},{agent_loop._SIG_MAP_OPEN})",
    )
    cheats = sorted(
        n for n in ACTIONS for pat in CHEAT_ACTION_PATTERNS if pat in n
    )
    chk("ACTIONS 内无作弊接口命名", not cheats, f"hits={cheats}")
    chk(
        "ECONOMY_MUTATING_ACTIONS ⊆ ACTIONS",
        ECONOMY_MUTATING_ACTIONS <= set(ACTIONS),
        f"missing={sorted(ECONOMY_MUTATING_ACTIONS - set(ACTIONS))}",
    )
    chk(
        "/state 顶层字段 56 个，其中 22 个 _error 兄弟键",
        len(STATE_FIELDS) == 56 and len(STATE_ERROR_FIELDS) == 22,
        f"fields={len(STATE_FIELDS)} errors={len(STATE_ERROR_FIELDS)}",
    )
    chk(
        "STATE_FIELDS 与 STATE_GUARDED_BLOCKS 自洽（每个 _error 都在表内）",
        set(STATE_ERROR_FIELDS) <= set(STATE_FIELDS),
        f"missing={sorted(set(STATE_ERROR_FIELDS) - set(STATE_FIELDS))}",
    )
    chk(
        "observer 拼装字段不在 STATE_FIELDS 内",
        not (set(STATE_OBSERVER_SYNTHESIZED) & set(STATE_FIELDS)),
        "",
    )
    chk(
        "恒 None 三字段未混入 STATE_FIELDS",
        not (STATE_FIELDS_MISSING_IN_CSHARP & set(STATE_FIELDS)),
        "",
    )
    from game_agent.brain.schema import _IDENTIFIER_ACTIONS

    wrong_req = sorted(
        s.name
        for s in SPECS
        if "identifier" in s.params
        and s.params["identifier"].required
        and s.name not in _IDENTIFIER_ACTIONS
    )
    chk(
        "identifier 必填性与 _IDENTIFIER_ACTIONS 一致",
        not wrong_req,
        f"过度必填={wrong_req}",
    )
    risky = [
        s
        for s in SPECS
        if (
            any(p.required for p in s.params.values())
            or s.deprecated
            or bool(s.gated_until)
            or s.once_per_cycle
            or bool(s.recovery_fallback)
            or s.name in derive_night_only()
            or s.name in derive_day_only()
            or s.night_phase_ok
        )
    ]
    chk(
        "guard_hint 覆盖全部危险动作",
        all(s.guard_hint for s in risky),
        f"覆盖 {sum(1 for s in risky if s.guard_hint)}/{len(risky)}；"
        f"缺失={[s.name for s in risky if not s.guard_hint]}",
    )
    # T12 运行时兜底（P0）：gated_until 必须是可执行判定，不能只是元数据。
    # 关键性质：物理在册（in_python_actions=True）≠ 模型可选。
    gated_names = set(derive_gated_actions())
    chk(
        "is_model_callable 排除 gated / deprecated / 恢复专用，但保留物理在册",
        gated_names == {"expedition_send", "hire_worker"}
        and set(derive_actions()) >= gated_names          # 不能删：C# 有 case
        and not is_model_callable("expedition_send")
        and not is_model_callable("hire_worker")
        and not is_model_callable("force_dismiss")
        and not is_model_callable("toggle_power")
        and is_model_callable("sell_item")
        and is_model_callable("end_day")
        and is_model_callable("no_such_action") is False,
        f"gated={sorted(gated_names)} model_callable n={len(derive_model_callable())}",
    )
    chk(
        "derive_model_callable() == derive_model_visible()（两个视图语义一致）",
        derive_model_callable() == derive_model_visible(),
        f"only_callable={sorted(derive_model_callable() - derive_model_visible())} "
        f"only_visible={sorted(derive_model_visible() - derive_model_callable())}",
    )
    chk(
        "模式 B 静默降级台账完整覆盖（112 键 / 17 块）",
        len(SILENT_CATCH_KEYS) == 112
        and len(set(SILENT_CATCH_KEYS.values())) == 17
        and set(SILENT_CATCH_KEYS) == set(SILENT_CATCH_PATHS),
        f"keys={len(SILENT_CATCH_KEYS)} blocks={len(set(SILENT_CATCH_KEYS.values()))} "
        f"paths={len(SILENT_CATCH_PATHS)}",
    )
    sc_problems = _check_silent_catch_registry()
    chk(
        "SILENT_CATCH_KEYS 自洽（块名/路径/无 _error 混入）",
        not sc_problems,
        f"problems={sc_problems[:4]} n={len(sc_problems)}",
    )
    chk(
        "守卫输入高危键均已登记（can_next / has_client / can_pay_rent / rent_due）",
        {"can_next", "has_client", "can_pay_rent", "rent_due"} <= set(SILENT_CATCH_KEYS),
        f"missing={sorted({'can_next','has_client','can_pay_rent','rent_due'} - set(SILENT_CATCH_KEYS))}",
    )
    chk(
        "SILENT_CATCH_KEYS 与 STATE_GUARDED_BLOCKS 不重叠（模式 B 与模式 A 互斥）",
        not (set(SILENT_CATCH_KEYS) & set(STATE_ERROR_FIELDS)),
        f"overlap={sorted(set(SILENT_CATCH_KEYS) & set(STATE_ERROR_FIELDS))}",
    )
    # 三态语义：ok / null / missing / error 四态互不相等，且 null 与 missing
    # 在签名里必须可区分（这正是「把不知道当成没有」的防线）。
    s_ok = field_state({"k": 1}, "k")
    s_null = field_state({"k": None}, "k")
    s_missing = field_state({}, "k")
    s_err = field_state({"k": 1}, "k", "boom")
    chk(
        "降级四态互异且 ok 唯一可信",
        len({s_ok, s_null, s_missing, s_err}) == 4
        and s_ok == FIELD_OK
        and s_null == FIELD_NULL
        and s_missing == FIELD_MISSING
        and s_err == FIELD_ERROR
        and not is_degraded(s_ok)
        and is_degraded(s_null)
        and is_degraded(s_missing)
        and is_degraded(s_err),
        f"{s_ok}/{s_null}/{s_missing}/{s_err}",
    )
    chk(
        "state_signature 哨兵使 null 与 missing 可区分",
        sig_encode({"k": None}, "k") != sig_encode({}, "k")
        and sig_encode({}, "k") == SIG_SENTINEL_MISSING
        and sig_encode({"k": None}, "k") == SIG_SENTINEL_NULL
        and sig_encode({"k": 1}, "k", "x") == SIG_SENTINEL_ERROR
        and sig_encode({"k": 1}, "k") == 1,
        "",
    )
    chk(
        "is_forbidden_guard_input 命中恒不可得两字段、放过正常字段",
        all(is_forbidden_guard_input(p) for p in (
            "inventory_counts", "dialogue", "inventory_counts.x"))
        # ``wholesale_cost`` 已于 2026-11-15 移出禁区（CR-T04-1 兑现，
        # ``BridgeApi.cs:1234`` 真会发射它）——这里必须断言它**不再**被禁，
        # 否则等于把「补完字段后规则变假」固化成门禁。
        and not is_forbidden_guard_input("wholesale_cost")
        and not is_forbidden_guard_input("table.wholesale_cost")
        and not is_forbidden_guard_input("")
        and not is_forbidden_guard_input("night.map_open")
        and not is_forbidden_guard_input("cash"),
        f"FORBIDDEN={sorted(FORBIDDEN_GUARD_INPUTS)}",
    )
    chk(
        "健康契约无问题 + 三种不一致场景均被捕获",
        check_contract(contract_payload()) == []
        and bool(check_contract({"actions": [{"name": "ghost_action", "params": []}]}))
        and bool(check_contract({"actions": []}) is not None)
        and bool(check_contract({"silent_catch_keys": ["bogus_silent_key"]})),
        "",
    )

    # ---- 活比对 vs 意向声明（docs/27 §0.8：无法消除的告警会训练人无视门禁）----
    # 「意向声明」= gated_until 非空（1.0 未实装）或 deprecated。这些动作的 params
    # 描述未来实现意图，C# 现在无条件 return Err、根本不读，做双向活比对必然报假红。
    _gated_no_params = {
        "version": CONTRACT_VERSION,
        "actions": [
            {
                "name": s.name,
                # 门禁/废弃动作：C# 一个参数都不读（模拟真实实现意图前的状态）
                "params": [] if not is_live_contract(s.name) else sorted(s.params),
            }
            for s in SPECS
        ],
        "state_fields": list(STATE_FIELDS),
        "silent_catch_keys": {k: list(SILENT_CATCH_PATHS.get(k, ())) for k in SILENT_CATCH_KEYS},
    }
    chk(
        "门禁/废弃动作的 params 不参与双向活比对（不产生无法消除的假告警）",
        check_contract(_gated_no_params) == [],
        f"gated={sorted(derive_gated_actions())} "
        f"deprecated={sorted(n for n in BY_NAME if BY_NAME[n].deprecated)}",
    )

    # 但放宽不能把真漂移一起放过：活契约少读一个参数，必须报。
    _drift = json.loads(json.dumps(_gated_no_params))
    for item in _drift["actions"]:
        if item["name"] == "sell_item":
            item["params"] = []  # C# 突然不读 identifier 了 → 真漂移
    _drift_problems = check_contract(_drift)
    chk(
        "放宽后仍能捕获真漂移（活契约卖东西少读 identifier）",
        any("sell_item" in p for p in _drift_problems),
        f"problems={_drift_problems[:2]}",
    )

    # 动作名集合必须照旧全量比对：C# 少一个 case（哪怕它自带 gated）也必须报。
    _missing_case = json.loads(json.dumps(_gated_no_params))
    _missing_case["actions"] = [
        a for a in _missing_case["actions"] if a["name"] != "expedition_send"
    ]
    _mc_problems = check_contract(_missing_case)
    chk(
        "动作名集合全量比对（C# 少 expedition_send case 必报，不因 gated 而放过）",
        any("expedition_send" in p for p in _mc_problems),
        f"problems={_mc_problems[:2]}",
    )

    # 反向：C# 读了本地没登记的参数 → 实现跑在声明前面，任何动作（含 gated）都要报。
    _extra = json.loads(json.dumps(_gated_no_params))
    for item in _extra["actions"]:
        if item["name"] == "expedition_send":
            item["params"] = ["location", "hireling", "target"]
    _extra_problems = check_contract(_extra)
    chk(
        "C# 读了本地未登记的参数必报（含 gated 动作——实现跑在声明前面）",
        any("expedition_send" in p and "target" in p for p in _extra_problems),
        f"problems={_extra_problems[:2]}",
    )

    # ---- state_fields_live（R2 的 /contract 拆分出的「实测」字段）----
    # live ⊆ declared 是硬约束：live 出现声明之外的键 = 源码已发射但静态表没登记，
    # 那份「能力清单」就过时了（比不报更糟，因为它看上去权威）。
    _live_ok = json.loads(json.dumps(_gated_no_params))
    _live_ok["state_fields_live"] = ["in_game", "scene", "cash"]  # 全部在声明内
    chk(
        "state_fields_live ⊆ state_fields 时零问题（实测字段是声明的子集）",
        check_contract(_live_ok) == [],
        f"live={_live_ok['state_fields_live']}",
    )
    _live_bad = json.loads(json.dumps(_gated_no_params))
    _live_bad["state_fields_live"] = ["in_game", "brand_new_field"]
    _live_problems = check_contract(_live_bad)
    chk(
        "state_fields_live 出现声明之外的键必报（静态能力清单已过时）",
        any("brand_new_field" in p for p in _live_problems),
        f"problems={_live_problems[:2]}",
    )
    # 反向不报：声明有而 live 没有是正常的（不在局内时只有 in_game）。
    _live_subset = json.loads(json.dumps(_gated_no_params))
    _live_subset["state_fields_live"] = ["in_game"]
    chk(
        "声明的字段未出现在 live 中不报（块级字段本就不发射，正常）",
        check_contract(_live_subset) == [],
        f"live={_live_subset['state_fields_live']} declared={len(STATE_FIELDS)}",
    )
    # observer 拼装字段若出现在 live 里也不算漂移（它们是合法观察字段，只是不在 BuildState）。
    _live_synth = json.loads(json.dumps(_gated_no_params))
    _live_synth["state_fields_live"] = ["in_game", *STATE_OBSERVER_SYNTHESIZED]
    chk(
        "state_fields_live 含 observer 拼装字段不报（合法观察字段）",
        check_contract(_live_synth) == [],
        f"synth={sorted(STATE_OBSERVER_SYNTHESIZED)}",
    )

    # ---- /risk/endings 载荷自洽性（T07 只读端点的字段契约）----
    # 这份 golden 直接抄自实机（2026-11-15 19:48 在局内真值），不是手搓的理想形状。
    _risk_menu = {
        "ok": True,
        "grace": {"owned": None, "purchasable": None, "cost": None, "cooldown_left": None},
        "factions": [],
        "sentinel": {"pool": None, "ending_at": 20, "days_since_injection": None, "is_sentinel": None},
        "aug": {"presence": None, "ending_at": 100, "ending_gate": None},
        "mortgage": {
            "signed": None,
            "collateral_seized": None,
            "paid_off": None,
            "weeks_left": None,
            "weekly_payment": None,
            "total_weeks": None,
            "repo_possible": None,
        },
        "notes": [
            "不在局内（PlayerStore.Instance 为 null），所有段发 null",
        ],
    }
    chk(
        "/risk/endings 主菜单态（全 null + notes 非空）自洽",
        check_risk_endings(_risk_menu) == [],
        f"problems={check_risk_endings(_risk_menu)[:2]}",
    )

    _risk_live = json.loads(json.dumps(_risk_menu))
    _risk_live["grace"] = {"owned": False, "purchasable": True, "cost": 100, "cooldown_left": 0}
    _risk_live["factions"] = [
        {
            "id": "FACTION_SECURITY",
            "name": "治安部",
            "value": -2,
            "ending_perk": "SEC_IMMEDIATE_ARREST",
            "ending_at": -100,
            "ending_unlocked": False,
        },
        {
            "id": "FACTION_UPPER_LEVEL",
            "name": "上层区",
            "value": -15,
            "ending_perk": None,
            "ending_at": None,
            "ending_unlocked": None,
        },
    ]
    _risk_live["sentinel"] = {
        "pool": 0,
        "ending_at": 20,
        "days_since_injection": 0,
        "is_sentinel": False,
    }
    _risk_live["aug"] = {"presence": 15, "ending_at": 100, "ending_gate": True}
    _risk_live["mortgage"] = {
        "signed": False,
        "collateral_seized": None,
        "paid_off": False,
        "weeks_left": 0,
        "weekly_payment": 0,
        "total_weeks": 0,
        "repo_possible": True,
    }
    chk(
        "/risk/endings 在局内态（含无结局线的派系三连 null）自洽",
        check_risk_endings(_risk_live) == [],
        f"problems={check_risk_endings(_risk_live)[:2]}",
    )

    # 形状随状态切换 = 调用方被迫写两套分支，而总有一套没被测到。
    _risk_short = {k: v for k, v in _risk_live.items() if k != "aug"}
    chk(
        "/risk/endings 缺顶层键必报（形状不随状态切换）",
        any("缺顶层键" in p and "aug" in p for p in check_risk_endings(_risk_short)),
        f"problems={check_risk_endings(_risk_short)[:2]}",
    )

    # ending_perk 与 ending_at/ending_unlocked 必须同生同灭。
    _risk_half = json.loads(json.dumps(_risk_live))
    _risk_half["factions"][1]["ending_perk"] = "LLLYNCH"  # 只给了 perk，没有阈值/解锁态
    chk(
        "/risk/endings 派系三连半边 null 必报（perk/阈值/解锁态同生同灭）",
        any("同生同灭" in p or "应当同时" in p for p in check_risk_endings(_risk_half)),
        f"problems={check_risk_endings(_risk_half)[:2]}",
    )

    # 整体降级不得一言不发：这正是本项目「不能返回空内容却宣称一切正常」的铁律。
    _risk_mute = json.loads(json.dumps(_risk_menu))
    _risk_mute["notes"] = []
    chk(
        "/risk/endings 全 null 但 notes 为空必报（整体降级不得一言不发）",
        any("notes" in p for p in check_risk_endings(_risk_mute)),
        f"problems={check_risk_endings(_risk_mute)[:2]}",
    )

    # --- MCP 工具导出自检（docs/28）---
    _mcp = build_mcp_tools()
    chk(
        "MCP 工具集为 2 个（game_state + game_action）",
        [t["name"] for t in _mcp["tools"]] == ["game_state", "game_action"],
        f"tools={[t['name'] for t in _mcp['tools']]}",
    )
    _mcp_action = next(t for t in _mcp["tools"] if t["name"] == "game_action")
    _mcp_enum = set(_mcp_action["inputSchema"]["properties"]["action"]["enum"])
    chk(
        "MCP game_action 枚举 == derive_model_callable()（不外泄恢复专用/gated/deprecated）",
        _mcp_enum == derive_model_callable(),
        f"mcp_only={sorted(_mcp_enum - derive_model_callable())[:5]} "
        f"py_only={sorted(derive_model_callable() - _mcp_enum)[:5]}",
    )
    chk(
        "MCP game_action 带 scene 陈旧状态守卫参数",
        "scene" in _mcp_action["inputSchema"]["properties"],
        "",
    )
    chk(
        "MCP 工具带 annotations（readOnly / destructive）",
        _mcp["tools"][0]["annotations"]["readOnlyHint"] is True
        and _mcp_action["annotations"]["destructiveHint"] is True,
        "",
    )

    failures = [r for r in results if not r[1]]
    if verbose:
        for label, passed, detail in results:
            mark = "PASS" if passed else "FAIL"
            print(f"[{mark}] {label}" + (f"  {detail}" if detail else ""))
        print(f"\n合计 {len(results)} 项，失败 {len(failures)} 项")
    return len(failures)


if __name__ == "__main__":
    if "--export-mcp" in sys.argv:
        # docs/28：把 MCP tools schema 写到 stdout（由部署脚本落到 mod 数据目录），
        # 保证工具定义与 ACTIONS / 参数表同源。
        print(json.dumps(build_mcp_tools(), ensure_ascii=False, indent=2))
        raise SystemExit(0)
    raise SystemExit(0 if selfcheck() == 0 else 1)
