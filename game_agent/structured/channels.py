"""结构化观察通道：探测、选择与状态签名。

三条观察通道（按优先级）：
- bridge：游戏内 BepInEx 插件 HTTP 桥，内存实时状态，毫秒级、零猜测；
- statewatch：游戏日志 + 存档文件直读，无需游戏内插件，百毫秒级；
- vision：截图 + 视觉模型（视觉通道，兜底；不属本模块，由 agent_loop 回退）。

本模块只做「探测哪个通道可用」与「选哪个」，不产生观察数据；
另提供 state_signature()：把桥状态压缩为可比较元组（21 字段，含交易桌与
夜晚/治安/队列/谈判面板字段 + 末位降级证据位），供事件驱动主循环判断
「世界是否发生了变化」。

**降级检测（T13）**：``degradation_evidence()`` 是「某块没读到」的唯一判定入口，
observer 与签名共用同一份实现。常量一律取自 :mod:`game_agent.contracts`
（``STATE_GUARDED_BLOCKS`` / ``SIG_FIELDS`` / ``field_state``），本模块**不再自己
维护第二份白名单**。
"""
from __future__ import annotations

from typing import Any, Optional

from game_agent.bridge.client import BridgeClient, make_bridge_client
from game_agent.contracts import (
    FIELD_ERROR,
    FIELD_MISSING,
    FIELD_NULL,
    FIELD_OK,
    STATE_GUARDED_BLOCKS,
    field_state,
)
from game_agent.statewatch.paths import resolve_data_dir

# 通道名（有序即默认优先级）
CHANNEL_BRIDGE = "bridge"
CHANNEL_STATEWATCH = "statewatch"
CHANNEL_VISION = "vision"

DEFAULT_PREFER = (CHANNEL_BRIDGE, CHANNEL_STATEWATCH, CHANNEL_VISION)


def probe_channels(
    config: dict[str, Any],
    *,
    client: Optional[BridgeClient] = None,
) -> dict[str, bool]:
    """探测各结构化通道可用性（vision 恒 True——是否需要窗口由后续环节判断）。

    Args:
        config: 配置字典。
        client: 可注入的桥客户端（测试用；None 时按配置构造）。

    Returns:
        {"bridge": bool, "statewatch": bool, "vision": True}
    """
    bridge_ok = False
    try:
        bridge = client or make_bridge_client(config)
        bridge_ok = bridge.is_available()
    except Exception:
        # 探测本身失败（客户端构造异常等）按不可用处理，绝不外抛
        bridge_ok = False

    statewatch_ok = False
    try:
        data_dir = resolve_data_dir(config)
        statewatch_ok = bool(data_dir) and data_dir.is_dir()
    except Exception:
        statewatch_ok = False

    return {
        CHANNEL_BRIDGE: bridge_ok,
        CHANNEL_STATEWATCH: statewatch_ok,
        CHANNEL_VISION: True,
    }


def pick_source(
    config: dict[str, Any],
    *,
    prefer: Optional[list[str]] = None,
    probe: Optional[dict[str, bool]] = None,
    mock: bool = False,
) -> str:
    """按优先级选择观察通道。

    Args:
        config: 配置字典（读取 structured.prefer）。
        prefer: 覆盖优先序（测试/调试用）。
        probe: 覆盖探测结果（测试用；None 时实时探测）。
        mock: True 时强制 vision（mock 语义 = 全离线，不碰网络与游戏）。

    Returns:
        通道名：bridge / statewatch / vision。
    """
    if mock:
        return CHANNEL_VISION
    if probe is None:
        probe = probe_channels(config)
    order = prefer or (config.get("structured", {}) or {}).get("prefer") or DEFAULT_PREFER
    for channel in order:
        if channel not in (CHANNEL_BRIDGE, CHANNEL_STATEWATCH, CHANNEL_VISION):
            continue  # 忽略未知通道名，保持健壮
        if probe.get(channel):
            return channel
    return CHANNEL_VISION


# ---------------------------------------------------------------- 降级检测（T13）

#: 「读了但读失败 / 压根没读到」的块 → 成因模式。**只做映射，不做判断**：
#: 键名与块名一律来自 :data:`game_agent.contracts.STATE_GUARDED_BLOCKS`。
_MODE_BY_FLAG: dict[str, str] = {
    FIELD_ERROR: "A",    # `<block>_error` 非空（C# 捕获后显式上报）
    FIELD_MISSING: "B",  # 键整个没出现（46 处空 catch{} 静默吞掉）
    FIELD_NULL: "C",     # 键在、值是显式 null（catch { 写死 null }）
}

#: 模式 B 逐块上报的条数上限。超过就折叠成一条聚合项——
#: 逐块报警的价值在「定位到具体哪块坏了」，条数一多这个价值就没了，
#: 但**「大面积坏了」这个事实必须留下**，故折叠而非删除。
_MISSING_FOLD_THRESHOLD = 5

#: 折叠项的 ``block`` 占位名（不是真实块名，故用尖括号形态）。
_PAYLOAD_BLOCK = "<payload>"

#: 一份**真实 bridge ``/state``** 必然携带的键（``in_game: true`` 分支下无条件发射）。
#:
#: 来源逐条实证（``mod/src/BridgeApi.cs``，均在 ``if (ps == null) return`` 之后）：
#: ``in_game`` :196、``scene`` :197、``cash`` :198、``store_state`` :199、
#: ``day`` :203-204（**try 与 catch 两条分支都 append**，故必然存在，只是值可能为
#: null）、``day_until_rent`` :205、``rent_value`` :206、``is_power_on`` :212、
#: ``garbage_level`` :213。
#:
#: 为什么需要它：只有当载荷**确实是完整 bridge 载荷**时，「某块没发射」才是降级
#: 证据。手写的部分状态（测试夹具、statewatch 归一态）缺块是**天生如此**，把它们
#: 当降级会刷满假警报。``rent_due``/``can_pay_rent``/``unlocks`` 不在表内——它们
#: 分别在 :210/:211/:400-408 的空 ``catch { }`` 里，本身就可能不发射。
_BRIDGE_UNCONDITIONAL_KEYS: tuple[str, ...] = (
    "in_game",
    "scene",
    "cash",
    "store_state",
    "day",
    "day_until_rent",
    "rent_value",
    "is_power_on",
    "garbage_level",
)


def _is_full_bridge_payload(state: dict[str, Any]) -> bool:
    """判断载荷是否是「完整 bridge /state」——决定「块缺失」能不能当证据。

    用无条件发射的 9 个键做判据，而不是「在场块数超过某个比例」：
    前者有 C# 行号为证、不随夹具变化；后者是启发式，实测在「10 块在场 +
    12 块静默缺失」时会把系统全盲误判成一切正常。
    """
    return all(key in state for key in _BRIDGE_UNCONDITIONAL_KEYS)


def _block_flag(state: dict[str, Any], block: str, error: Any) -> str:
    """判定一个业务块的可信状态。

    复用 :func:`game_agent.contracts.field_state` 的四态判定，只补一条**业务块
    特有**的归并：块值为 ``null`` 一律算 :data:`FIELD_OK`。

    理由是块级 ``null`` 在这个游戏里几乎全是**合法的「空」**而非「没读到」：
    ``client: null`` = 当前没有顾客（``mod/src/BridgeApi.cs:240`` 用
    ``instance != null ? BuildClient(instance) : "null"``）、``table: null`` =
    桌上没货。若把它们当降级，每一次「无顾客」观察都会报降级，
    模型会被淹没在假警报里——真降级反而看不见了。

    模式 C（显式 null 的真实降级）在 /state 里只有**标量**键
    （``day``，见 ``mod/src/BridgeApi.cs:203-204``），由 :func:`degradation_report`
    单独判定，不走这里。
    """
    if error:
        return FIELD_ERROR
    flag = field_state(state, block)
    return FIELD_OK if flag == FIELD_NULL else flag


def degradation_report(
    state: Any, *, channel: str = CHANNEL_BRIDGE
) -> list[dict[str, Any]]:
    """列出本次观察里**不可信**的块（T13 唯一判定入口）。

    observer 的 ``visible_info["degraded"]`` 与 :func:`state_signature` 的末位
    都取自这里，**不各写一份判断**。

    Args:
        state: ``GET /state`` 的原始 dict（非 dict 视作空）。
        channel: 观察通道。``"statewatch"`` 时**不报模式 C**：那条规则的依据是
            C# ``catch { sb.Append(",\\"day\\":null"); }``（``BridgeApi.cs:203-204``），
            而 statewatch 的 ``None`` 是 Python 归一态的「本通道读不到」
            （``observer.py`` 的 ``_observe_statewatch`` 显式写 ``scene/store_state/
            shutter_closed/dialog.* = None``）。两者形状相同、成因不同，
            按同一把尺子量会产生假警报。

    Returns:
        ``[{"block": str, "mode": "A"|"B"|"C", "error": str|None,
        "flag": "error"|"missing"|"null"}, ...]``，顺序稳定（22 块按
        ``STATE_GUARDED_BLOCKS`` 声明序，``day`` 恒在其后）；全部可信时 ``[]``。

    判定规则（四档，顺序即优先级）：

    1. **整体不可读**：载荷里**一个键都没有**（``{}`` / 非 dict）→ 只报一条
       ``in_game`` 模式 B。这是「连自己在不在游戏里都不知道」的黑屏态。
    2. **合法极简载荷**：``in_game`` 明确为假 → ``/state`` 只有 ``in_game``
       一个键是 C# 的正常早退分支（``mod/src/BridgeApi.cs:190-194``），
       **整体豁免**，返回 ``[]``——否则每次开游戏前都会刷 22 条假降级。
    3. **有 ``in_game`` 且为真**（bridge 正常态）：逐个比对 22 对
       ``<block>_error → block``：``_error`` 非空 → 模式 A；块键整个缺失 →
       模式 B（46 处空 ``catch { }`` 就是这样静默吞键的，连错误都没有）。
    4. **缺 ``in_game`` 但有其它键**：载荷形状异常，**只报显式证据**
       （``_error`` 非空 = 模式 A），**不推断**其它块「缺失」——因为此时
       无法区分「该块读失败」与「这个通道本来就不产出该块」（statewatch
       归一后只有 12 个键，见 ``observer.py`` 的 ``_observe_statewatch``；
       若在此处推断缺失，statewatch 每一帧都会报 10 条假降级）。

    另有一条标量规则（任何非豁免分支都生效）：``day`` 键在、值为显式 ``null``
    → 模式 C（``mod/src/BridgeApi.cs:203-204`` 的 ``catch { sb.Append(",\"day\":null"); }``）。
    这是 **null ≠ 缺失** 的实证点，故它单独成项、``flag`` 为
    :data:`FIELD_NULL` 而非 :data:`FIELD_MISSING`。
    """
    if not isinstance(state, dict):
        return []
    if state and "in_game" not in state:
        # 分支 4：形状异常但有内容——只认显式 _error，不推断缺失
        report = [
            {
                "block": block,
                "mode": _MODE_BY_FLAG[FIELD_ERROR],
                "error": str(state.get(error_key)),
                "flag": FIELD_ERROR,
            }
            for error_key, block in STATE_GUARDED_BLOCKS.items()
            if state.get(error_key)
        ]
        return _append_explicit_nulls(report, state, channel=channel)
    if not state:
        # 分支 1：一个键都没有
        return [{
            "block": "in_game", "mode": _MODE_BY_FLAG[FIELD_MISSING],
            "error": None, "flag": FIELD_MISSING,
        }]
    if not state.get("in_game"):
        # 分支 2：合法极简载荷
        return []

    # 分支 3：有 in_game 且为真
    #
    # 这里**绝不返回空列表来宣称「一切正常」**：降噪的正确做法是「折叠成一条
    # 聚合项」，不是「删掉」。曾经用「过半块缺失就整体抑制」的启发式，实测有硬
    # 盲区——10 块在场 + 12 块静默缺失 + 无任何 _error 时报告为空，与完全健康
    # 逐字节相同，正好是模式 B 最坏的情形（系统全盲却看起来正常）。
    #
    # 但「块缺失」只有在载荷**确实是完整 bridge /state** 时才是证据。手写的部分
    # 状态（测试夹具、statewatch 归一态）缺块是天生如此，把它们当降级会刷满假
    # 警报——所以先做一道 :func:`_is_full_bridge_payload` 形状判定，而不是靠
    # 缺失比例去猜。
    #
    # statewatch 通道额外整体豁免模式 B：该通道结构上就不产出那 22 块
    # （observer.py 的 _observe_statewatch 只归一 12 个键），缺失不构成证据。
    report: list[dict[str, Any]] = []
    hard_failures: list[dict[str, Any]] = []
    silent_missing: list[str] = []
    for error_key, block in STATE_GUARDED_BLOCKS.items():
        error = state.get(error_key)
        flag = _block_flag(state, block, error)
        if flag == FIELD_OK:
            continue
        if flag == FIELD_MISSING:
            silent_missing.append(block)
            continue
        # 模式 A 是**显式证据**：C# 亲口说这块读失败了，与载荷形状无关，永远上报。
        hard_failures.append({
            "block": block,
            "mode": _MODE_BY_FLAG[flag],
            "error": str(error) if error else None,
            "flag": flag,
        })

    report = hard_failures
    infer_missing = (
        channel != CHANNEL_STATEWATCH and _is_full_bridge_payload(state)
    )
    if silent_missing and infer_missing:
        if len(silent_missing) <= _MISSING_FOLD_THRESHOLD:
            report.extend({
                "block": block,
                "mode": _MODE_BY_FLAG[FIELD_MISSING],
                "error": None,
                "flag": FIELD_MISSING,
            } for block in silent_missing)
        else:
            # 大面积静默缺失：折叠成一条聚合项（保留「异常」这个事实与规模，
            # 但不在 visible_info 里刷十几条），签名末位仍与健康态不同。
            report.append({
                "block": _PAYLOAD_BLOCK,
                "mode": _MODE_BY_FLAG[FIELD_MISSING],
                "error": None,
                "flag": FIELD_MISSING,
                # ``blocks`` 是机器可读的真实块名列表。只给人看的 ``detail``
                # 不够：observer 要据此说出「哪一块坏了、坏了意味着什么」，
                # 若只留一个 ``<payload>`` 占位名，提示里会退化成
                # 「1 段状态没读到：<payload>」——段数记错、块名全丢。
                "blocks": tuple(silent_missing),
                "detail": (
                    f"{len(STATE_GUARDED_BLOCKS)} 段业务块里有 {len(silent_missing)} 段"
                    f"没发射且无 _error：{', '.join(silent_missing)}"
                ),
            })
    return _append_explicit_nulls(report, state, channel=channel)


def _append_explicit_nulls(
    report: list[dict[str, Any]],
    state: dict[str, Any],
    *,
    channel: str = CHANNEL_BRIDGE,
) -> list[dict[str, Any]]:
    """追加模式 C（键在、值是显式 null）的**标量**项。

    属于 bridge 通道：这是 C# ``catch { sb.Append(",\\"day\\":null"); }``
    （``mod/src/BridgeApi.cs:203-204``），即「游戏侧算 day 失败」。
    statewatch 归一态的 ``day: None`` 含义是「这个通道没解析出天数」
    （见 ``observer.py`` 的 ``_observe_statewatch``），把它按模式 C 上报，
    会在每次 statewatch 观察里伪造一条降级——**警报要有意义就不能乱报**。
    """
    if channel == CHANNEL_STATEWATCH:
        return report
    if "day" in state and state.get("day") is None:
        report.append({
            "block": "day",
            "mode": _MODE_BY_FLAG[FIELD_NULL],
            "error": None,
            "flag": FIELD_NULL,
        })
    return report


def signature_degradation(state: Any, *, channel: str = CHANNEL_BRIDGE) -> tuple:
    """把降级证据压成签名可用、**可哈希且不抖动**的元组。

    刻意**不含** ``_error`` 文本：错误消息里可能带时间戳/新数值，纳入签名会让
    每一步都「看起来变了」，事件驱动循环就被拖进无限重规划。
    """
    return tuple(sorted(
        (item["block"], item["flag"])
        for item in degradation_report(state, channel=channel)
    ))


def state_signature(
    state: dict[str, Any], *, channel: str = CHANNEL_BRIDGE
) -> tuple:
    """把桥 /state 响应压缩为「变化签名」元组。

    只收录会影响 AI 决策的字段：场景 / 营业状态 / 卷帘门 / 现金 /
    当前顾客（身份 + 买卖意图）/ 对话状态 / 交易桌（桌上物品 + 议价值）。
    现金与议价值纳入签名——议价过程中报价变化应当触发重新决策；
    桌上物品纳入签名——place_item 放置成功应当唤醒事件驱动循环。
    v0.8.0 起增补 3 个夜晚/治安字段：是否外出 / 外出位置 / 顾客可举报
    （治安部按钮点亮——举报窗口期必须唤醒循环，机不可失）。
    v0.8.1 起增补顾客队列判定 can_next（队列空→打烊的转换必须被感知）。
    v0.8.2 起增补谈判面板开闭态 nego_open（开/关面板必须唤醒循环——
    硬规则「未关闭界面优先关闭」需要感知面板状态才能执行）。
    v0.8.3 起增补租约倒计时 day_until_rent 与库存总值 stock_value
    （交租日切换、库存显著变化都应触发重新决策）。

    返回 tuple（可哈希、可比较）；字段缺失时以 None 占位，保证同构可比。

    **末位降级证据位（T13）**：第 21 位为 :func:`signature_degradation` 的返回值，
    形如 ``(("night", "missing"), ("queue", "error"))``，无降级时为 ``()``。
    前 20 位语义与取值**保持 v0.9.16 原样不变**（下游守卫按下标取值，见
    ``agent_loop.py`` 的 ``_shutter_guard``/``_rent_reserve_guard``/``_night_ui_guard``）。

    为什么降级要进签名：某块从「读得到」变成「读不到」（或反之）时若签名不变，
    事件驱动循环不会重新规划，模型会拿着基于旧数据的计划硬套——这正是
    「把不知道当成没有」在系统层面的表现形式。
    """
    if not isinstance(state, dict):
        return ()
    client = state.get("client") or {}
    dialog = state.get("dialog") or {}
    table = state.get("table") or {}
    table_item = table.get("negociated_item") or {}
    night = state.get("night") or {}
    queue = state.get("queue") or {}
    economy = state.get("economy") or {}
    return (
        state.get("in_game"),
        state.get("scene"),
        state.get("store_state"),
        state.get("shutter_closed"),
        state.get("cash"),
        client.get("identifier"),
        client.get("is_selling"),
        client.get("is_buying"),
        dialog.get("is_end"),
        dialog.get("is_choice"),
        table_item.get("identifier") if isinstance(table_item, dict) else None,
        table.get("negociated_value"),
        # —— v0.8.0 夜晚/治安 ——
        night.get("is_outside"),
        night.get("current_location"),
        client.get("sec_button_lit"),
        # —— v0.8.1 顾客队列 ——
        queue.get("can_next"),
        # —— v0.8.2 谈判面板开闭态 ——
        table.get("nego_open"),
        # —— v0.8.3 租约倒计时与库存总值 ——
        # 交租日切换 / 库存显著变化都应唤醒循环重新决策
        state.get("day_until_rent"),
        economy.get("stock_value"),
        # —— v0.9.16 夜间地图开闭态 ——
        # open_map 幂等守卫用：地图开着还调 open_map = 空转（r54 实测 ×5 卡死）；
        # 纳入签名后「重复 open_map 且地图已开」也能被零变化 streak 兜底捕获。
        night.get("map_open"),
        # —— T13 降级证据位（第 21 位，v1.0）——
        # 只放 (block, flag)，不放 _error 文本：错误消息可能带时间戳/新数值，
        # 纳入签名会让每一步都「看起来变了」，把事件驱动循环拖进无限重规划。
        signature_degradation(state, channel=channel),
    )
