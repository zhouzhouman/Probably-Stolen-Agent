"""结构化观察组装：把「桥接状态 / 状态监听状态」翻译成 brain 的观察契约。

观察契约与视觉通道保持一致（brain 无需区分数据来源）：
    {
      "source": "bridge" | "statewatch",
      "screen_state": "<视觉枚举同款：main_menu | shop_counter | ...>",
      "available_actions": ["中文动词短语", ...],
      "important_objects": [{"name","type","value","position","point"}, ...],
      "confidence": 1.0,            # 直读数据，无画面猜测
      "visible_info": {...},
    }

与视觉观察的差异：
- 没有截图坐标（point 一律 null）——目标位置不可知，需要点具体 UI 元素时
  brain 应优先选用「游戏 API 直调动作」（见 brain.prompt 的桥动作组）；
- confidence 恒为 1.0（内存直读，不存在看错）；
- 附带 state_signature 供事件驱动主循环做脏检查（不透传给模型）；
- P4 起纳入交易桌状态（table：桌上物品/议价值）与物品级库存
  （inventory：place_item 的 identifier 来源），交易闭环才完整。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from game_agent.bridge.client import BridgeClient, make_bridge_client
from game_agent.config import artifacts_dir, load_config
from game_agent.errors import BridgeError
from game_agent.statewatch.paths import resolve_data_dir
from game_agent.statewatch.watcher import StateWatcher
from game_agent.structured.channels import (
    CHANNEL_BRIDGE,
    CHANNEL_STATEWATCH,
    degradation_report,
    state_signature,
)

__all__ = ["StructuredObservation", "StructuredObserver", "derive_screen_state"]


# ---------------------------------------------------------------- screen_state 推导

def derive_screen_state(state: dict[str, Any]) -> str:
    """把桥 /state 推导为视觉契约的 screen_state 枚举值。

    枚举（与 vision.models 保持一致）：main_menu | creation | day_start_news |
    shop_counter | customer_offer | negotiation | inventory | market |
    upgrade_menu | dialogue | loading | unknown

    P4 起：顾客在场且桌上已有物品 → negotiation（议价中），
    区别于 customer_offer（顾客刚到场、尚未放物品）。
    v0.8.0 起：夜间地图打开 → night_map；人在外面 → outside。
    v0.8.2 起：谈判面板打开（table.nego_open=true）→ negotiation
    （面板开着即视为谈判界面活跃，与桌上是否有物品无关）。
    """
    if not isinstance(state, dict) or not state.get("in_game"):
        return "main_menu" if not state else "loading"

    # 谈判面板开闭态（v0.8.2）：面板开着优先判定为 negotiation——
    # 多 Agent 硬规则「未关闭界面必须优先关闭」需要 screen_state 如实反映
    table0 = state.get("table") or {}
    if isinstance(table0, dict) and table0.get("nego_open"):
        return "negotiation"

    scene = str(state.get("scene") or "")
    if "Inventory" in scene:
        return "inventory"
    if "Market" in scene:
        return "market"
    if "Upgrade" in scene:
        return "upgrade_menu"

    # 夜晚/外出阶段（v0.8.0）：地图面板或人在外面优先于店内状态
    night = state.get("night") or {}
    night = night if isinstance(night, dict) else {}
    if night.get("is_outside"):
        return "outside"
    if night.get("map_open"):
        return "night_map"

    dialog = state.get("dialog") or {}
    if dialog.get("is_choice"):
        return "dialogue"

    client = state.get("client") or {}
    table = state.get("table") or {}
    table_item = table.get("negociated_item") if isinstance(table, dict) else None
    if client.get("identifier"):
        if table_item:
            return "negotiation"  # 顾客在场且桌上已有物品：议价中
        if client.get("is_selling") or client.get("is_buying"):
            return "customer_offer"
        return "dialogue"

    if str(state.get("store_state") or "").upper() == "MORNING":
        return "day_start_news"
    return "shop_counter"


# ---------------------------------------------------------------- 地点判定

# 拾荒地点的名字有两代：
#   Demo 期 = 枚举名 "SCAVENGING"
#   正式版 = AfterhourLocation 注册表的 id 字符串 "dumping_ground"
#            （mod/src/BridgeApi.cs:2816-2817 `case "scavenging": ... locId = "dumping_ground";`；
#             BridgeApi.cs:732 生产 current_location = map.Current.Id；
#             BridgeApi.cs:3002/3014/3037 自身守卫也按 "dumping_ground" 比对）
# 只比老值会让拾荒判定彻底失效。**所有**拾荒地点判定必须共用本函数，避免第三次分叉
# （历史：v3.4 只修了 _next_step_hints 一处，_derive_available_actions 漏改，导致
#  正式版「开始拾荒」提示恒不出现 —— 事故 r55 的同族 bug，task T08-P1）。
_SCAVENGING_LOCATION_IDS = frozenset({"SCAVENGING", "DUMPING_GROUND"})


def _is_scavenging_location(night: Any) -> bool:
    """night.current_location 是否指向拾荒地点（Demo 枚举名 + 正式版 id 双值兼容）。

    只读 current_location，不判断 is_outside（调用方各自决定语义：
    可用动作提示要求人在外面，装包提示也要求人在外面）。
    """
    if not isinstance(night, dict):
        return False
    return str(night.get("current_location") or "").upper() in _SCAVENGING_LOCATION_IDS


def _derive_available_actions(state: dict[str, Any]) -> list[str]:
    """按状态推导玩家当前可执行的操作（中文短语，风格对齐视觉观察）。

    P4 起纳入交易桌状态：桌空且有顾客 → 可放物品；桌上有物品 → 可成交。
    v0.8.0 起纳入夜晚阶段（服务面板/夜间地图/外出中）与治安动作
    （举报可逮捕顾客/击毙伪人）。
    """
    if not state.get("in_game"):
        return ["进入游戏", "载入存档"]

    actions: list[str] = []
    dialog = state.get("dialog") or {}
    client = state.get("client") or {}
    table = state.get("table") or {}
    table = table if isinstance(table, dict) else {}
    table_item = table.get("negociated_item") if isinstance(table, dict) else None
    night = state.get("night") or {}
    night = night if isinstance(night, dict) else {}
    phone = state.get("phone") or {}
    phone = phone if isinstance(phone, dict) else {}
    queue = state.get("queue") or {}
    queue = queue if isinstance(queue, dict) else {}

    # —— 未关闭界面优先（v0.8.2：谈判面板开着时，关闭面板必须是第一个候选）——
    # v2.0 修正：v0.9.4 已按用户指令取消 close_negociation（「面板是交易操作台，禁止关闭」），
    # 但这里仍在提示「关闭谈判面板」——模型据此幻觉出一个白名单里不存在的动作，白丢一步。
    # 现在改为提示面板内的真实成交手段（批发 / 接受报价）。
    # 2026-11-15：去掉「接受多个」——正式版谈判面板只有 primaryButton /
    # secondaryButton 两个大按钮（tools/decompile_full/NegociationUIManager.cs:59/61），
    # 没有任何「接受多个」按钮，`nego_buttons` 里也没有 accept_multiple 键。
    if table.get("nego_open"):
        actions.append("在谈判面板内成交（批发 / 接受报价）")

    # —— 夜晚/外出阶段（v0.8.0）——
    is_outside = bool(night.get("is_outside"))
    map_open = bool(night.get("map_open"))
    if is_outside:
        actions.append("返回商店")
        if _is_scavenging_location(night):
            actions.append("开始拾荒")
    elif map_open:
        if night.get("visit_left"):
            actions.append("外出-配给站")
            actions.append("外出-升级商人")
            actions.append("外出-拾荒")
        actions.append("睡觉不出门")

    # —— 治安/战斗（v0.8.0：优先提示，机不可失）——
    if client.get("sec_button_lit") or client.get("can_report"):
        if client.get("is_wanted") or client.get("is_aug"):
            actions.append("举报当前顾客（拿悬赏）")
    if client.get("is_aug"):
        actions.append("击毙伪人（射击模式）")

    if dialog.get("is_choice"):
        actions.append("选择对话选项")
    elif client.get("identifier"):
        actions.append("继续对话")
        actions.append("打发走当前顾客")
        if table_item:
            # 桌上已有物品：进入议价/成交阶段
            actions.append("接受当前报价")
            if client.get("is_selling"):
                actions.append("收购桌上物品")
            if client.get("is_buying"):
                actions.append("出售桌上物品")
        else:
            # 桌空：交易第一步是放物品
            actions.append("把物品放上交易桌")
            if client.get("is_selling"):
                actions.append("收购顾客物品")
            if client.get("is_buying"):
                actions.append("出售物品给顾客")
    elif not is_outside and not map_open:
        # 以 store_state 判断营业状态（shutter_closed 与之可能不同步——
        # begin_day 后日状态 CLOSED 但按钮 isClosed=False，曾误导 AI 从不开门）
        # 进入本分支时无顾客在场（有顾客走上方的交易分支）
        store_state_text = str(state.get("store_state") or "").upper()
        if store_state_text == "CLOSED":
            actions.append("开门营业")
        elif store_state_text == "MORNING":
            actions.append("开始营业")
        elif queue.get("can_next") is False:
            # v0.8.1：队列已空（权威信号）——不再提示叫号，直接引导打烊进夜晚流程
            actions.append("打烊进入夜晚流程")
        else:
            actions.append("呼叫下一位顾客")
            actions.append("关门歇业")
        actions.append("查看库存")

    # 电话面板打开时的动作
    if phone.get("open"):
        actions.append("拨打电话")
        actions.append("查看通讯录")
        actions.append("挂断电话")

    # v2.0：删掉尾部的重复「开始营业」——MORNING 且无顾客时上面分支已加过一次，
    # 重复项会白白占提示词、也让模型误以为有两个开张动作。
    return actions


def _build_important_objects(state: dict[str, Any]) -> list[dict[str, Any]]:
    """把状态中的关键数值整理为 important_objects（point 恒 null）。"""
    objects: list[dict[str, Any]] = []
    queue = state.get("queue") or {}
    queue = queue if isinstance(queue, dict) else {}

    if state.get("cash") is not None:
        objects.append({
            "name": "现金", "type": "money", "value": str(state.get("cash")),
            "position": "top", "point": None,
        })
    if state.get("rent_value") is not None:
        # v1.5：真实应缴额 rent_due（temporaryRent，累计延期欠款/罚金）优先展示——
        # r78 实证：名义租金 242，实际应交 693，现金 442 时房东只给「拒付」选项
        due = state.get("rent_due")
        due_text = f"；实际应交 {due}" if due is not None and due != state.get("rent_value") else ""
        objects.append({
            "name": "房租", "type": "money",
            "value": f"{state.get('rent_value')}（{state.get('day_until_rent')} 天后{due_text}）",
            "position": "top", "point": None,
        })

    client = state.get("client") or {}
    if client.get("identifier"):
        intent = []
        if client.get("is_selling"):
            intent.append("出售")
        if client.get("is_buying"):
            intent.append("购买")
        value = f"{client.get('identifier')}（{'/'.join(intent) or '到店'}）"
        # 想收类别与剩余预算（AI 选品依据：类别不匹配的物品放了也卖不掉）
        if client.get("is_buying"):
            buying_tags = client.get("buying_tags") or []
            if buying_tags:
                value += f"·想收:{','.join(str(t) for t in buying_tags)}"
            if client.get("budget_left") is not None:
                value += f"·预算:{client.get('budget_left')}"
        objects.append({
            "name": "当前顾客", "type": "npc",
            "value": value,
            "position": "center", "point": None,
        })

    # 交易桌：桌上物品与议价值（P4；桌空时无此对象）
    table = state.get("table") or {}
    table = table if isinstance(table, dict) else {}
    table_item = table.get("negociated_item") if isinstance(table, dict) else None
    if isinstance(table_item, dict) and table_item:
        value_text = str(table_item.get("name") or table_item.get("identifier") or "?")
        negociated = table.get("negociated_value")
        if negociated is not None:
            value_text += f"（议价 {negociated}）"
        objects.append({
            "name": "桌上物品", "type": "item",
            "value": value_text,
            "position": "center", "point": None,
        })

    # 谈判面板开闭态（v0.8.2；v0.9.11：开着≠需关闭——面板是交易操作台，禁止关闭）
    if table.get("nego_open") is not None:
        nb = table.get("nego_buttons") or {}
        if table.get("nego_open"):
            tips = []
            if nb.get("wholesale"):
                tips.append("批发按钮亮（点 AcceptWholesale 成交）")
            # 注：正式版谈判面板只有 primary/secondary 两个大按钮
            # （tools/decompile_full/NegociationUIManager.cs:59/61，
            # OnSellAllClick/OnBuyAllClick/OnWholesaleClick），**没有「接受多个」
            # 按钮**——``accept_multiple`` 是历史幽灵键，C# 从不发射它
            # （BridgeApi.cs:1185-1191 只发 primary/secondary/wholesale/accept_offer）。
            if nb.get("accept_offer"):
                tips.append("接受报价可用")
            value = "已打开——" + ("；".join(tips) if tips else "当前无可成交项（按铃换下一位）")
        else:
            value = "已关闭"
        objects.append({
            "name": "谈判面板", "type": "ui",
            "value": value,
            "position": "center", "point": None,
        })

    dialog = state.get("dialog") or {}
    if dialog.get("is_end") is not None:
        stage = "待选择" if dialog.get("is_choice") else ("已结束" if dialog.get("is_end") else "进行中")
        objects.append({
            "name": "对话状态", "type": "text", "value": stage,
            "position": "center", "point": None,
        })

    shutter = state.get("shutter_closed")
    if shutter is not None:
        objects.append({
            "name": "卷帘门", "type": "ui",
            "value": "关闭（未营业）" if shutter else "开启（营业中）",
            "position": "center", "point": None,
        })

    # 顾客队列（v0.8.1：队列空且无顾客 = 今日收工，别再叫号）
    client0 = state.get("client") or {}
    if queue.get("can_next") is False and not (isinstance(client0, dict) and client0.get("identifier")):
        objects.append({
            "name": "顾客队列", "type": "ui",
            "value": "已空——今日无更多顾客，直接打烊走夜晚流程",
            "position": "center", "point": None,
        })

    # 夜晚/外出状态（v0.8.0：夜间地图打开或人在外面时高价值信息）
    night = state.get("night") or {}
    night = night if isinstance(night, dict) else {}
    if night.get("is_outside"):
        objects.append({
            "name": "外出位置", "type": "ui",
            "value": f"人在外面·{night.get('current_location')}（办好事要返回商店）",
            "position": "center", "point": None,
        })
    elif night.get("map_open"):
        objects.append({
            "name": "夜间地图", "type": "ui",
            "value": f"已打开·今晚还可外出 {night.get('visit_left')} 次",
            "position": "center", "point": None,
        })
    if night.get("seriously_wounded"):
        objects.append({
            "name": "伤势", "type": "status",
            "value": "受重伤——今夜无法外出拾荒",
            "position": "center", "point": None,
        })

    # 通缉/伪人顾客（v0.8.0：举报与射击的决策依据）
    if client.get("is_wanted") or client.get("is_aug"):
        kind = "伪人（改造体）" if client.get("is_aug") else "通缉犯"
        value = f"{kind}·悬赏 {client.get('report_reward')}"
        if client.get("reputation_penalty"):
            value += f"·举报代价:声望-{client.get('reputation_penalty')}"
        objects.append({
            "name": "高危顾客", "type": "npc",
            "value": value,
            "position": "center", "point": None,
        })
    return objects


def _compact_inventory(inventory: Any) -> dict[str, list[dict[str, Any]]]:
    """把 /state 的 inventory 段压缩为「有价值物品」清单。

    只保留 value > 0 的物品（过滤储藏区/垃圾桶/档案箱等家具与任务物品——
    它们不该被放上交易桌）；owned = 玩家自己的，not_owned = 当前顾客的。

    v2.0 修复「同一物品按格子逐条重复」：桥接的 FindAllItem 返回的是**格子级**
    列表（19 个滤水器 = 19 条完全相同的记录），后期一局 144 条/侧、观察 JSON
    35KB、整条提示词 65KB（每步都要重发），既烧 token 又淹没真正的高价值品。
    这里按 identifier 聚合（count 求和）、按「总值 = 单价 × 件数」降序排列；
    identifier 全集不变，库存守卫/类别守卫的判定能力不受影响。
    """
    if not isinstance(inventory, dict):
        return {}
    result: dict[str, list[dict[str, Any]]] = {}
    for key in ("owned", "not_owned"):
        items = inventory.get(key)
        if not isinstance(items, list):
            continue
        merged: dict[str, dict[str, Any]] = {}
        for item in items:
            if not isinstance(item, dict):
                continue
            if (item.get("value") or 0) <= 0:
                continue
            identifier = str(item.get("identifier") or "")
            if not identifier:
                continue
            try:
                count = int(item.get("count") or 1)
            except (TypeError, ValueError):
                count = 1
            entry = merged.get(identifier)
            if entry is None:
                merged[identifier] = {
                    "identifier": identifier,
                    "name": item.get("name"),
                    "value": item.get("value"),
                    "count": count,
                    # 类别标签（如 food/toiletries）——与 client.buying_tags 对照选品
                    "item_types": list(item.get("item_types") or []),
                }
            else:
                entry["count"] += count
                for tag in item.get("item_types") or []:
                    if tag not in entry["item_types"]:
                        entry["item_types"].append(tag)
        # 保持游戏原始格子顺序（首次出现顺序）——下游 sellable_now 的清单顺序
        # 与游戏库存一致，模型看到的「第一件」就是背包里的第一件。
        result[key] = list(merged.values())
    return result


#: ``/debug/slots`` 的七个格子标签——顺序即 ``BuildDebugSlots`` 的发射顺序。
#: 来源 ``mod/src/BridgeApi.cs`` 的 ``BuildDebugSlots``：
#: ``front``/``back``/``counter``/``ground``/``pocketLeft``/``pocketRight``/``backpackSlot``。
_SLOT_LABELS: tuple[str, ...] = (
    "front",
    "back",
    "counter",
    "ground",
    "pocketLeft",
    "pocketRight",
    "backpackSlot",
)


def _slots_summary(slots: Any) -> tuple[list[dict[str, Any]] | None, int | None]:
    """把 ``/debug/slots`` 的七格原始载荷压成「还装着东西的格子」。

    Returns:
        ``(非空格子列表, 总件数)``；**载荷不可用（``None`` 或非列表）时返回
        ``(None, None)``**，与「七格全空 ``([], 0)``」严格区分。

    为什么必须区分：``None`` 的含义是「不知道地面有没有货」（端点不可用、
    旧插件、非店铺场景），``[]`` 的含义是「确实一件都没有」。把前者渲染成
    后者会让模型在拾荒时认为地上空了、直接离场——正是 r55 丢东西的形态。

    只保留 ``count > 0`` 的格子并只留前 3 个物品名——**空格子不携带决策
    信息**（模型要知道的是「地面还有几件没捡」「口袋满了没」，不是「哪格是空的」），
    而 7 格 × 12 名会白占几百 token。

    **没有容量字段**：``GameInventory`` 基类无 capacity，
    ``GameGridInventory.widthPixels/heightPixels/inventoryShape`` 与
    ``GameSlotInventory.defaultGridWidth/defaultGridHeight`` 全是
    private/readonly，反射读不到。所以只能给件数，给不了「满了没」——
    模型据此只能说「这一格里还有 N 件」，不能说「还能再塞 M 件」。
    """
    if not isinstance(slots, list):
        return None, None
    out: list[dict[str, Any]] = []
    total = 0
    for slot in slots:
        if not isinstance(slot, dict):
            continue
        try:
            count = int(slot.get("count") or 0)
        except (TypeError, ValueError):
            count = 0
        if count <= 0:
            continue
        items = [str(x) for x in (slot.get("items") or []) if x]
        out.append(
            {
                "label": str(slot.get("label") or ""),
                "identifier": str(slot.get("identifier") or ""),
                "count": count,
                "items": items[:3],
            }
        )
        total += count
    return out, total


def _inventory_counts(inventory: Any) -> dict[str, Any]:
    """由 ``/state`` 的 ``inventory`` 段推出 ``{owned_count, not_owned_count, by_tag}``。

    口径**严格对齐** :func:`_compact_inventory`：按 ``identifier`` 聚合、
    ``(item.get("value") or 0) <= 0`` 过滤、``count`` 求和。两处若不一致，
    「摘要说有 5 件、清单里只列 3 件」会同时出现在同一份观察里，
    模型无法判断该信哪个。

    为什么不要求 C# 侧发射 ``inventory_counts``：``BuildState`` 只发
    ``inventory.owned`` / ``inventory.not_owned``（``mod/src/BridgeApi.cs``），
    而 Python 侧已在此处拿到完整清单，再让 C# 重算一遍等于把同一口径
    写在两种语言里——正是 docs/26 X01 要消灭的那类重复。
    """
    result: dict[str, Any] = {"owned_count": 0, "not_owned_count": 0, "by_tag": {}}
    if not isinstance(inventory, dict):
        return result
    by_tag: dict[str, int] = {}
    for key, out_key in (("owned", "owned_count"), ("not_owned", "not_owned_count")):
        items = inventory.get(key)
        if not isinstance(items, list):
            continue
        sub_total = 0
        for item in items:
            if not isinstance(item, dict):
                continue
            if (item.get("value") or 0) <= 0:
                continue
            if not str(item.get("identifier") or ""):
                continue
            try:
                count = int(item.get("count") or 1)
            except (TypeError, ValueError):
                count = 1
            sub_total += count
            if out_key == "owned_count":
                for tag in item.get("item_types") or []:
                    name = str(tag or "")
                    if name:
                        by_tag[name] = by_tag.get(name, 0) + count
        result[out_key] = sub_total
    result["by_tag"] = dict(sorted(by_tag.items()))
    return result


def _showcase_view(state: dict[str, Any]) -> dict[str, Any]:
    """展柜陈列的**诚实降级**占位（T05-P1）。

    游戏侧目前没有任何「陈列了哪些品类、客流档位、招牌概率」的只读入口：
    ``mod/src/BridgeApi.cs`` 的 20 个路由里没有 ``/showcase``，
    ``/state`` 也没有对应块。所以这里**只能**返回不可观测。

    **不许编造 tier/probability/signs**：知识卡里那套「V≥350 且 d≥14 → 高价档」
    是攻略推断，不是本机可读的运行期状态；把它当观测喂给模型，模型会据此
    调整陈列却得不到任何反馈，比明确说「看不见」更糟。
    """
    return {
        "observable": False,
        "reason": "游戏侧未提供展柜陈列只读接口（/state 无该块，mod 无 /showcase 路由）",
        "unlocks_known": isinstance(state.get("unlocks"), dict)
        and bool(state.get("unlocks")),
    }




def _sellable_from_game(raw: Any) -> list[dict[str, Any]]:
    """把 /sellable 的结果转成与标签法同构的 sellable_now（v2.4）。

    游戏判定优先于标签推断：活鱼（tilapia/sturgeon）没有 item_types，
    标签法永远返回空，导致 AI 把「顾客其实想买鱼」误判成「无货可卖」。
    """
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        ident = item.get("identifier")
        if not ident or ident in seen:
            continue
        seen.add(ident)
        out.append({
            "identifier": ident,
            "name": item.get("name"),
            "value": item.get("value"),
            "count": item.get("count"),
            "budget_ok": bool(item.get("budget_ok", True)),
        })
        if len(out) >= 8:
            break
    return out


def _sellable_now(client: Any, inventory: Any) -> list[dict[str, Any]]:
    """当前顾客想收 ∩ 玩家库存 = 可立刻卖出的物品清单（v1.1）。

    r59 实测催生：模型连续 7 次输出「sell_item 缺少 identifier」——它知道要卖
    但不知道卖哪个（顾客 buyer_tags 与 71 件库存的交叉计算超出模型能力）。
    观察层直接算好交集，identifier 照抄即可；清单为空 = 顾客不收任何东西
    （或顾客是纯卖家），应 dismiss_client 换下一位。
    """
    if not isinstance(client, dict) or not isinstance(inventory, dict):
        return []
    want = client.get("buying_tags") or []
    if not want:
        return []  # 顾客没有收购需求（纯卖家/闲聊）
    # v2.0：大小写归一——桥接偶尔会给出小写标签，而 _category_guard 用的是
    # upper()，两处口径不一致会让「可卖清单」与「售前校验」互相打脸。
    want_set = {str(t).strip().upper() for t in want if str(t).strip()}
    if not want_set:
        return []
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in inventory.get("owned") or []:
        if not isinstance(item, dict):
            continue
        ident = item.get("identifier")
        if not ident or ident in seen:
            continue
        types = {str(t).strip().upper() for t in (item.get("item_types") or [])}
        if types & want_set:
            seen.add(ident)
            result.append({
                "identifier": ident,
                "name": item.get("name"),
                "value": item.get("value"),
                "count": item.get("count"),
                "item_types": item.get("item_types") or [],
            })
            if len(result) >= 8:
                break
    return result


def _compact_afterhour_events(events: Any) -> list[dict[str, Any]]:
    """夜间事件压缩（v3.1 正式版适配）。

    事件表可能很长（含未触发的通用事件），全塞进提示词会撑爆。
    只保留两类：
      · active=True          —— 正在生效，会影响今晚的判断；
      · closes_location=True —— 会让地点关门，直接决定能不能外出。
    每项：id/location/title/active/days_left/closes_location。
    """
    if not isinstance(events, list):
        return []
    result: list[dict[str, Any]] = []
    for e in events:
        if not isinstance(e, dict):
            continue
        if not (e.get("active") or e.get("closes_location")):
            continue
        result.append({
            "id": e.get("id"),
            "location": e.get("location"),
            "title": e.get("title"),
            "active": e.get("active"),
            "days_left": e.get("days_left"),
            "closes_location": e.get("closes_location"),
            # v3.5：事件「改了什么」才是决策依据（价格/风险/产出修正）
            "effects": [
                {
                    "text": x.get("text"),
                    "polarity": x.get("polarity"),
                    "key": x.get("modifier_key"),
                    "value": x.get("modifier_value"),
                }
                for x in (e.get("effects") or [])[:4]
                if isinstance(x, dict)
            ] or None,
        })
    return result


def _compact_services(services: Any) -> list[dict[str, Any]]:
    """把 /state 的 services 段压缩为夜晚服务选购清单（v0.8.0）。

    只保留已解锁且已解锁介绍的服务（未介绍的服务买了也用不上/看不到）。
    每项：id/name/cost/is_using/mandatory/cooldown。
    """
    if not isinstance(services, list):
        return []
    result: list[dict[str, Any]] = []
    for s in services:
        if not isinstance(s, dict):
            continue
        if not s.get("unlocked") or not s.get("introduced"):
            continue
        result.append({
            "id": s.get("id"),
            "name": s.get("name"),
            "cost": s.get("cost"),
            "is_using": s.get("is_using"),
            "mandatory": s.get("mandatory"),
            "cooldown": s.get("cooldown"),
        })
    return result


def _compact_phone(phone: Any) -> dict[str, Any]:
    """把 /state 的 phone 段压缩（v0.8.0：面板开关 + 通讯录）。"""
    if not isinstance(phone, dict):
        return {}
    contacts = []
    for c in phone.get("contacts") or []:
        if isinstance(c, dict):
            contacts.append({
                "number": c.get("number"),
                "name": c.get("name"),
                "state": c.get("state"),
                "cooldown": c.get("cooldown"),
            })
    return {
        "open": phone.get("open"),
        "book_shown": phone.get("book_shown"),
        "current_client": phone.get("current_client"),
        "contacts": contacts,
    }


def _compact_night(night: Any) -> dict[str, Any]:
    """把 /state 的 night 段压缩（v0.8.0：外出次数/位置/伤势）。"""
    if not isinstance(night, dict):
        return {}
    return {
        "visit_left": night.get("visit_left"),
        "is_outside": night.get("is_outside"),
        "current_location": night.get("current_location"),
        # v0.9.16：夜间地图开闭态——地图开着就别再 open_map（r54 空转卡死），
        # 直接 go_out/stay_home。
        "map_open": night.get("map_open"),
        "seriously_wounded": night.get("seriously_wounded"),
        "scavenging_attempts": night.get("scavenging_attempts"),
    }


def _compact_machines(machines: Any, limit: int = 8) -> list[dict[str, Any]]:
    """机器槽位压缩（v2.3）：只列有物品的槽，最多 8 台 × 4 槽 × 6 件。"""
    if not isinstance(machines, list):
        return []
    out: list[dict[str, Any]] = []
    for m in machines:
        if not isinstance(m, dict):
            continue
        slots_out = []
        for slot in m.get("slots") or []:
            if not isinstance(slot, dict):
                continue
            items = [
                {"id": it.get("identifier"), "n": it.get("count"), "v": it.get("value")}
                for it in (slot.get("items") or [])[:6]
                if isinstance(it, dict)
            ]
            if items:
                slots_out.append({"s": slot.get("slot"), "items": items})
        if not slots_out:
            continue
        out.append(
            {
                "i": m.get("index"),
                "id": m.get("identifier"),
                "name": m.get("name"),
                # v3.2（正式版）：机器类别与供电状态——正式版机器种类暴增，
                # 不知道类型/能不能通电就只能盲目投料。
                "type": m.get("type"),
                "can_power": m.get("can_power"),
                "has_battery": m.get("has_battery"),
                "modules": [x.get("identifier") for x in (m.get("modules") or []) if isinstance(x, dict)][:6] or None,
                # v3.3：配方只保留「名字 + 每项投入的 id/标签 + 数量」，
                # 产出由游戏 craftFunc 动态算，静态读不到，所以不提供。
                "recipes": [
                    {
                        "name": r.get("name"),
                        "in": [
                            {
                                "n": f.get("amount"),
                                "ids": (f.get("ids") or [])[:3] or None,
                                "tags": (f.get("tags") or [])[:3] or None,
                                "label": f.get("name"),
                            }
                            for f in (r.get("materials") or [])[:6]
                            if isinstance(f, dict)
                        ] or None,
                    }
                    for r in (m.get("recipes") or [])[:5]
                    if isinstance(r, dict)
                ] or None,
                "slots": slots_out[:4],
            }
        )
        if len(out) >= limit:
            break
    return out


def _compact_reputations(reputations: Any) -> list[dict[str, Any]]:
    """派系声望（v2.2）：权衡「举报拿赏金」与「得罪派系」的唯一依据。

    此前 ``/state.reputations`` 早已存在，却没进观察层——AI 举报通缉犯时
    完全看不到声望代价（实测 r100 拿 75 悬赏、掉 6 点治安部声望）。
    """
    if not isinstance(reputations, list):
        return []
    result: list[dict[str, Any]] = []
    for rep in reputations[:8]:
        if not isinstance(rep, dict):
            continue
        fid = rep.get("id") or rep.get("faction")
        if not fid:
            continue
        result.append(
            {
                "id": str(fid),
                "faction": rep.get("faction") or str(fid),
                "value": rep.get("value"),
            }
        )
    return result


def _compact_requests(requests: Any, limit: int = 6) -> list[dict[str, Any]]:
    """派系委托（v2.2）：``fill_request`` 的目标，可交付的排最前。

    同样早就存在于 ``/state``，但没进观察层——动作白名单里有 ``fill_request``，
    AI 却永远看不到任何委托（结构性空转）。
    """
    if not isinstance(requests, list):
        return []
    rows: list[dict[str, Any]] = []
    for req in requests:
        if not isinstance(req, dict):
            continue
        rid = req.get("id")
        if not rid:
            continue
        rows.append(
            {
                "id": rid,
                "title": req.get("title"),
                "faction": req.get("faction"),
                "required_item": req.get("required_item"),
                "required_count": req.get("required_count"),
                "duration": req.get("duration"),
                "credit_reward": req.get("credit_reward"),
                "reputation_reward": req.get("reputation_reward"),
                "can_fill": bool(req.get("can_fill")),
            }
        )
    rows.sort(
        key=lambda r: (
            not r["can_fill"],
            r["required_count"] if isinstance(r["required_count"], (int, float)) else 999,
        )
    )
    return rows[:limit]


def _compact_unlocks(unlocks: Any) -> dict[str, bool]:
    """设施解锁状态（v2.2）：未解锁的设施禁止生成对应行动（硬规则②）。"""
    if not isinstance(unlocks, dict):
        return {}
    keys = ("vending_machine", "vending_fountain", "shooting", "gene_scanner")
    return {key: bool(unlocks.get(key)) for key in keys if key in unlocks}


def _compact_economy(economy: Any) -> dict[str, Any]:
    """经济段精简映射（经营决策必需：加价率/吸引力/库存总值/贷款等）。

    缺这段时模型对「收购是否划算」只能靠猜（曾导致批量买入 JUNK 垃圾货）。
    """
    if not isinstance(economy, dict):
        return {}
    return {
        # 零售加价率（%）：判断进货是否划算的核心参数
        "retail_markup": economy.get("retail_markup"),
        # 库存总值：判断家底与是否该变现
        "stock_value": economy.get("stock_value"),
        # 店铺吸引力：直接影响客流
        "attractiveness": economy.get("attractiveness"),
        # 基础吸引力（v3.17）：脏乱扣减按「基础值」计算，缺了它就无法算准损失比例
        "attractiveness_base": economy.get("attractiveness_base"),
        # 贷款：loan_days 归零必须 pay_loan
        "loan_days": economy.get("loan_days"),
        "loan_value": economy.get("loan_value"),
        # 彩票中奖号码（无本收入机会）
        "lottery_number": economy.get("lottery_number"),
        # 派系好感（走关系优惠）
        "wild_favor": economy.get("wild_favor"),
        # 连续未购保险天数（夜晚服务决策依据）
        "insurance_days_without": economy.get("insurance_days_without"),
        # v3.11（2026-11-15，T02-P0）：今晚已选服务合计与「付不付得起」。
        # 口径与游戏 UI 的「合计」行完全一致（`EndOfDayUIManager.cs:108-117`
        # 只累加 `unlocked && isUsing` 的 cost），所以模型看到这个数就等于看到
        # 服务面板右下角那个数字——它不必自己把 services[] 里十几项加一遍，
        # 更不会因为漏掉 mandatory 的保护费而误判「钱够」。
        # 两者可能为 None（C# 求和异常）：None 是「算不出来」，不是「0 元」，
        # 也不是「买不起」——由提示词负责讲清这点。
        "service_total": economy.get("service_total"),
        "service_affordable": economy.get("service_affordable"),
        # v2.2：违禁品加价档位（low/mid/high/critical）——判断违禁品该不该碰、
        # 碰哪一档的收益风险比（wiki: contraband）
        "contraband_markup": economy.get("contraband_markup"),
    }


# 「本次外出拾了几次」的基线追踪（v2.9）。
# night.scavenging_attempts 是**跨天累计**的（实测：Day 6 拾到 3，Day 7 一开局仍是 3），
# 直接拿它做"拾满 3 次就收工"的判断，会让第二天一出门就立刻收工——把拾荒收益全丢掉。
_scavenge_trips: dict[str, Any] = {"day": None, "baseline": 0, "trip_base": None}


def _attempts_tonight(info: dict[str, Any], tracker: Optional[dict] = None) -> int:
    """本次外出的拾取次数（用累计值减去出门时的基线）。

    tracker 可注入（测试用），默认用模块级 `_scavenge_trips`：
    - 换天 → 用当天首次看到的累计值当**日基线**；
    - 刚出门（is_outside False→True）→ 把当时的累计值记为**本次基线**；
    - 回店后清空本次基线，下次出门重新取。
    """
    t = tracker if tracker is not None else _scavenge_trips
    night = info.get("night") if isinstance(info.get("night"), dict) else {}
    try:
        att = int(night.get("scavenging_attempts") or 0)
    except (TypeError, ValueError):
        att = 0
    day = info.get("day")
    if t.get("day") != day:
        t["day"] = day
        t["baseline"] = att
        t["trip_base"] = None
    outside = bool(night.get("is_outside"))
    if outside and t.get("trip_base") is None:
        t["trip_base"] = att
    if not outside:
        t["trip_base"] = None
    base = t["trip_base"] if t.get("trip_base") is not None else t.get("baseline", 0)
    return max(0, att - int(base or 0))


def _next_step_hints(
    info: dict[str, Any], attempts_tonight: Optional[int] = None
) -> list[str]:
    """按当前局面算出「本轮必做」的确定性提示（v2.8）。

    为什么放在观察层而不是只写进提示词：实盘反复出现**知识卡命中但不行动**——
    scavenge 卡被命中 21 次仍未出门、顾客要的货没有却连续 5 步猜按钮文字。
    模型能读到状态，但缺少「这个局面下一步该干什么」的结论。这里用代码把结论算出来，
    跟着状态一起下发给模型，避免把关键判断寄托在模型自觉上。

    返回按优先级排序的短句（最多 6 条），每条都给出**具体动作名**。
    """
    hints: list[str] = []
    client = info.get("client") if isinstance(info.get("client"), dict) else {}
    sellable = info.get("sellable_now") or []
    queue = info.get("queue") if isinstance(info.get("queue"), dict) else {}
    night = info.get("night") if isinstance(info.get("night"), dict) else {}
    dialog = info.get("dialog") if isinstance(info.get("dialog"), dict) else {}

    who = (client.get("name") or client.get("identifier") or "这位顾客") if client else ""
    buying = bool(client.get("is_buying"))
    selling = bool(client.get("is_selling"))

    # 0) 枪械订单（v3.3 正式版新增）：可交/不可交都由 met 字段确定，优先级高于普通卖货——
    #    先判断能不能交，避免它在"要不要卖别的东西"上绕圈。
    order = client.get("gun_order") if isinstance(client.get("gun_order"), dict) else None
    if order:
        tier = order.get("tier")
        if order.get("accepted"):
            hints.append(
                f"枪械订单（tier {tier}）必选需求已全部满足 → 用 accept_gun_order 成交"
                f"（基础奖励 {order.get('base_reward')}；不要用普通 accept_offer）"
            )
        else:
            unmet = [
                str(r.get("label") or r.get("id") or "?")
                for r in (order.get("must") or [])
                if isinstance(r, dict) and not r.get("met")
            ]
            detail = "、".join(unmet[:4]) if unmet else "未找到符合要求的枪"
            hints.append(
                f"枪械订单（tier {tier}）必选需求未满足：{detail} → 先换枪或改配件；"
                "此时 accept_gun_order 会被拒绝，禁止硬交"
            )

    # 0.5) 按揭周供（v3.4 正式版）：与租约并行的第二套负债，现金不够要先回款而不是进货
    mortgage = info.get("mortgage") if isinstance(info.get("mortgage"), dict) else {}
    if mortgage:
        try:
            weeks_left = int(mortgage.get("weeks_left") or 0)
            weekly = int(mortgage.get("weekly_payment") or 0)
        except (TypeError, ValueError):
            weeks_left, weekly = 0, 0
        money_now = info.get("money")
        if weeks_left >= 1 and weekly > 0 and isinstance(money_now, (int, float)) and money_now < weekly:
            hints.append(
                f"按揭还剩 {weeks_left} 周、本周要还 {weekly}，现金只有 {money_now} → "
                "优先回款（卖货/接单），暂时不要进货"
            )


    # 0.55) 贷款到期（v3.18）：loan_days 归零不还就滚利息/违约，属于硬性负债，
    #     与租金同级。只在「现金确实还得起」时提示，否则会变成每步重复的噪声。
    _econ = info.get("economy") if isinstance(info.get("economy"), dict) else {}
    try:
        _loan_days = _econ.get("loan_days")
        _loan_days = int(_loan_days) if _loan_days is not None else None
        _loan_value = int(_econ.get("loan_value") or 0)
    except (TypeError, ValueError):
        _loan_days, _loan_value = None, 0
    _cash_now = info.get("money")
    if (
        _loan_days is not None
        and _loan_days <= 2
        and _loan_value > 0
        and isinstance(_cash_now, (int, float))
        and _cash_now >= _loan_value
    ):
        hints.append(
            f"贷款还剩 {_loan_days} 天到期、本金 {_loan_value}，现金 {int(_cash_now)} 够还 → "
            "优先 pay_loan，别拖到逾期"
        )

    # 0.58) 保险缺口（v3.18）：连续多天不买，遇上受伤/被抢就没有赔付。
    #     只在「连续 ≥3 天」且今晚还有得选服务时提示（避免刚开局就催）。
    try:
        _ins_gap = int(_econ.get("insurance_days_without") or 0)
    except (TypeError, ValueError):
        _ins_gap = 0
    if _ins_gap >= 3 and str(info.get("store_state") or "") in ("CLOSED", "CLOSING"):
        hints.append(
            f"已连续 {_ins_gap} 天没买保险 → 夜晚服务里 buy_insurance"
            "（受伤/被抢才赔付，成本很低，长期裸奔不划算）"
        )

    # 0.6) 脏乱降吸引力（v3.17，用户 2026-11-22 质疑「不清理垃圾第二天会掉吸引力」）：
    #      garbage_level 在 rent 段里。实测 Day 9 已到 10（满级）、吸引力从 250 掉到 125
    #      （正好 -50% 上限），但「垃圾降吸引力」这张卡被关键词命中的常驻卡挤出 max_cards，
    #      模型完全没意识到。这类「数值已经在恶化、但机制藏在卡里」的情况必须由代码算成结论置顶，
    #      不能指望模型自己翻卡。
    rent_info = info.get("rent") if isinstance(info.get("rent"), dict) else {}
    try:
        garbage = int(rent_info.get("garbage_level") or 0)
    except (TypeError, ValueError):
        garbage = 0
    if garbage >= 4:
        # 官方公式（按卡内实测数据反推并校验）：
        #   G=2 不扣；G=4→200(-20%)、G=6→175(-30%)、G=8→150(-40%)、G=10→125(-50%)
        #   ⇒ 扣减比例 = G × 5%，G≤2 视为 0，上限 50%。
        # ⚠️ 早先写成 (G-2)×5 会低估（G=10 算成 40%，实际已满 50%）。
        lost_pct = 0 if garbage <= 2 else min(garbage * 5, 50)
        econ = info.get("economy") if isinstance(info.get("economy"), dict) else {}
        attr, attr_base = econ.get("attractiveness"), econ.get("attractiveness_base")
        detail = f"脏乱等级 {garbage}"
        if isinstance(attr, (int, float)) and isinstance(attr_base, (int, float)):
            detail += f"、吸引力 {attr}/{attr_base}"
        detail += f"（每超 1 级扣 5% 基础，已扣约 {lost_pct}%"
        detail += "，已到上限" if lost_pct >= 50 else ""
        detail += "）"
        hints.append(
            f"{detail} → **今晚打烊后必须开清洁服务**：night_services 开面板 → "
            f'night_service {{"service": "JANITORIAL_SERVICE", "enable": true}} → end_day_confirm。'
            "不开则明天客流继续下降、直接少接一位顾客"
        )

    # 1) 死局顾客：他要买，但你一件他能收的都没有——必须换人，不能等
    if client and buying and not sellable:
        tags = "、".join(str(t) for t in (client.get("buying_tags") or [])) or "未知类别"
        hints.append(
            f"顾客【{who}】想买 {tags}，而 sellable_now 为空（你没有任何他要的货）"
            "→ 立即 dismiss_client 换下一位；禁止 wait、禁止猜按钮文字"
        )
    # 2) 顾客在卖货：该收货而不是送客
    #    v3.6 实机：正式版顾客的货要**先开议价面板**才摆出来，否则 not_owned 是空的，
    #    直接 buy_item 会被库存守卫拦下，白烧一步。
    if client and selling and not buying:
        table = info.get("table") if isinstance(info.get("table"), dict) else {}
        if not table.get("nego_open"):
            hints.append(
                f"顾客【{who}】在卖货但议价面板没开 → 先 open_negociation 让货摆上桌，"
                "下一轮再 buy_item（此时 not_owned 为空是正常的）"
            )
        else:
            hints.append(
                f"顾客【{who}】在卖货 → 用 buy_item 按报价收购（看 table.negociated_item）；"
                "确实不想要才 dismiss_client"
            )
    # 3) 有货可卖：优先成交
    if client and buying and sellable:
        hints.append(
            f"顾客能收 {len(sellable)} 件货 → 立刻 sell_item，identifier 照抄 sellable_now（一次一件）"
        )
    # 4) 队列有人但还没叫号
    if queue.get("can_next") and not client:
        hints.append("队列里有人在等 → call_next")
    # 5) 对话还有未选选项
    choices = dialog.get("choices") if isinstance(dialog.get("choices"), list) else []
    if dialog and not dialog.get("is_end") and choices:
        head = str(choices[0])[:16]
        hints.append(f"对话还有 {len(choices)} 个选项未选 → dialog_choose，text 照抄选项原文（如「{head}」）")
    # 6) 夜晚还有外出次数：拾荒是零成本货源（实盘连续多晚全放弃，是最大浪费）
    #    只在**打烊后**提示：营业中跑出去会丢客人，宁可等打烊再出门。
    visit_left = night.get("visit_left")
    store_state = str(info.get("store_state") or "").upper()
    night_time = store_state in ("", "CLOSED")
    # 5.5) 地点被夜间事件关闭（v3.4 正式版）：不看事件就 go_out 等于白跑一趟
    _closed_locs = [
        str(e.get("location") or "")
        for e in (info.get("afterhour_events") or [])
        if isinstance(e, dict) and e.get("closes_location") and e.get("location")
    ]
    if _closed_locs and night_time and isinstance(visit_left, (int, float)) and visit_left > 0:
        hints.append(
            f"今晚被事件关闭的地点：{'、'.join(dict.fromkeys(_closed_locs))} → go_out 别选它们，"
            "改去 available=true 的其他地点（看 locations）"
        )
    if isinstance(visit_left, (int, float)) and visit_left > 0 and not night.get("is_outside") and night_time:
        hints.append(
            f"今晚还有 {int(visit_left)} 次外出没用（visit_left）→ 拾荒流程：open_map → "
            "go_out scavenging → scavenge（次数内尽量多拾）→ stow_loot 装包 → leave_outside 带回。"
            "这是零成本货源，不用等于白丢"
        )
    # 7) 人已经在外面：先把战利品装包再回店（留地上会消失）
    if night.get("is_outside"):
        # 地点判定统一走 _is_scavenging_location()（模块顶部）：
        # v3.4 修正了正式版地点从枚举名改成 id 字符串（dumping_ground）这件事，
        # 但当时只改了这一处；_derive_available_actions 里的同名判定被漏改，
        # 正式版「开始拾荒」提示因此恒不出现（T08-P1）。现已抽成共用 helper。
        if _is_scavenging_location(night):
            # scavenge 没有服务端硬上限（走游戏自己的按钮），必须由提示控制收工点，
            # 否则 AI 会一直拾下去、把整批步数烧在一个地点上。
            # 注意用「本次外出」的次数：累计值跨天不清零，拿它判断会让第二天秒收工。
            try:
                raw_att = int(night.get("scavenging_attempts") or 0)
            except (TypeError, ValueError):
                raw_att = 0
            # v3.20（实机踩坑：一次跑了 11 步都在拾荒）：_attempts_tonight 依赖进程内基线，
            # **重启后恒为 0**，于是提示一直说「已拾 0 次，再拾一次」，模型就无限拾下去。
            # 而 scavenging_attempts 是跨天累计的，拿绝对值判断又会让第二天秒收工。
            # 结论：次数不可信时不要给错误数字，改成给**退出条件**。
            trip_known = attempts_tonight is not None and not (
                int(attempts_tonight) == 0 and raw_att > 0
            )
            if trip_known:
                attempts = max(0, int(attempts_tonight))
                if attempts >= 3:
                    hints.append(
                        f"已拾 {attempts} 次 → 收益递减，现在收工："
                        "stow_loot 把地面战利品装包 → leave_outside 回店（留在地上会消失）"
                    )
                else:
                    hints.append(
                        f"人在拾荒点（已拾 {attempts} 次，3 次后收工）→ scavenge 再拾一次，"
                        "随后 stow_loot 装包（留在地上回店即消失）→ leave_outside"
                    )
            elif raw_att >= 6:
                # 累计值已经很高（含前几晚），基本没有继续拾的价值
                hints.append(
                    f"拾荒累计已 {raw_att} 次、收益明显递减 → 现在收工："
                    "stow_loot 装包 → leave_outside 回店（今晚还要开清洁服务）"
                )
            else:
                hints.append(
                    "人在拾荒点（本次外出已拾次数不可读，不要纠结次数）→ 判断标准是收获："
                    "地面有货就先 stow_loot 装包；连续两次 scavenge 都没有新增战利品就收工，"
                    "leave_outside 回店（今晚还要开清洁服务，别把步数烧在拾荒上）"
                )
    # 8) 委托可交付：信用点/声望白拿，过期作废
    fillable = [
        r for r in (info.get("requests") or [])
        if isinstance(r, dict) and r.get("can_fill")
    ]
    if fillable:
        hints.append(f"有 {len(fillable)} 个委托现在就能交付 → fill_request（信用点+声望，过期作废）")
    # 9) 鉴定升职待处理（v3.4 正式版）：放着会过期，等级决定能接什么品级的货
    appraisal = info.get("appraisal") if isinstance(info.get("appraisal"), dict) else {}
    if appraisal.get("promotion_pending"):
        hints.append(
            f"鉴定升职待处理（当前 {appraisal.get('rank_name') or appraisal.get('rank')}）→ "
            "优先处理升职，别让它过期"
        )
    # 上限 8：正式版新增（枪械订单/按揭/事件关地点/鉴定升职）会挤占名额，
    # 6 条时忙碌局面会把「今晚要出门拾荒」这类夜间提示直接挤掉，那是最贵的丢失。
    # T13：降级告警插在**队首**——它约束的是「能不能用这批数据」，是前置条件；
    # 放队尾会被上面 8 条挤掉，等于白写。
    return _degradation_hints(info) + hints[:8]


#: 降级块 → 「这块坏了会静默废掉哪个守卫」的具名后果。只登记**已实证**的路径，
#: 不臆测。键是 ``STATE_GUARDED_BLOCKS`` 的 block 名。
_DEGRADED_HINT_EFFECT: dict[str, str] = {
    "night": (
        "夜战状态读不到 → current_location/is_outside 不可信 → "
        "_at_scavenging_site() 判 False → 「拾荒离场强制 stow_loot」守卫失效"
        "（r55：一夜毁 12 件）。**不要假定不在拾荒点；离场前自己先 stow_loot**"
        "（正式版 location 是 id：store/dumping_ground/commissary/dr_jackson）"
    ),
    "queue": (
        "顾客队列读不到 → can_next/has_client 可能是写死的默认值，"
        "**不要假定队列空、也不要假定有人**，叫号前先看 client 是否有内容"
    ),
    "economy": (
        "经济块读不到 → 库存总值/吸引力未知，**不要据此判断「库存还够/不够」**"
    ),
    "inventory": (
        "库存块读不到 → owned 可能是空数组而非真的空，"
        "**不要据此进补货或判断卖完了**"
    ),
    "client": (
        "顾客块读不到 → 当前有没有顾客都不确定，"
        "**不要假定「没有顾客」就去做别的事**"
    ),
    "table": (
        "交易桌块读不到 → 桌上有没有货不确定，**不要假定桌空而重复 place_item**"
    ),
    "mortgage": (
        "按揭块读不到 → 周供金额与剩余周数未知，**不要假定还完了或不用交**"
    ),
    "shutter_closed": (
        "卷帘门状态读不到 → 开关门守卫失效，重复调用会让门状态抖动，"
        "**开/关门各做一次即可，别连点**"
    ),
    # —— 以下为剩余 14 块：不写「该块不可信」这种泛泛一句（等于白报），
    # 每条都锚在 observation.py / agent_loop.py 里实际读它的地方。
    "screen": (
        "界面块读不到 → screen_state 只能靠 scene 猜，可能把「界面还开着」判成已关（或反之），"
        "**关闭界面前先用 dialog/table 的证据二次确认**"
    ),
    "dialog": (
        "对话块读不到 → 「未关闭对话框优先关闭」硬规则失效（is_end/is_choice 可能是 None），"
        "**既不要假定对话已结束，也不要假定还开着**"
    ),
    "services": (
        "夜晚服务块读不到 → 服务清单可能是空列表而非真的没有，"
        "**不要据此断定「今夜没有可用服务」**"
    ),
    "reputations": (
        "声誉块读不到 → 举报通缉犯的依据缺失，**不要凭猜测举报**"
    ),
    "requests": (
        "委托块读不到 → can_fill 可能全为假，**不要据此断定「没有可交付的委托」**"
    ),
    "difficulty": (
        "难度块读不到 → 规则参数未知，**不要假定当前难度下的数值门槛**"
    ),
    "perks": (
        "特权块读不到 → 可用特权未知，**不要假定某个特权没有**"
    ),
    "afterhour_events": (
        "营业后事件块读不到 → 有事件关了地点也看不到，"
        "**出门前先看 locations 再决定**"
    ),
    "locations": (
        "地点块读不到 → 可去地点清单未知，**不要假定没地方可去**"
    ),
    "appraisal": (
        "鉴定块读不到 → promotion_pending 未知，**不要假定没有升职可领**"
    ),
    "aquaculture": (
        "水产养殖块读不到 → 养殖状态未知，**不要据此做投喂/收获决策**"
    ),
    "risk": (
        "风险块读不到 → 风险指标未知，**不要据此判断「安全/危险」**"
    ),
    "phone": (
        "手机块读不到 → 面板开闭与通讯录未知，**不要假定手机没开着**"
    ),
    "shooting": (
        "枪械块读不到 → 靶场/枪械订单状态未知，**不要据此判断枪械任务能不能做**"
    ),
}

#: 降级提示的条数预算。普通提示被 ``hints[:8]`` 截断，降级提示不受该上限
#: 约束（它必须排在计划之前），所以必须自带上限，否则 22 块全坏时会把
#: 上下文挤爆。实测最坏：head 1 条 + 具名后果 3 条 = 4 条 / 428 字符。
_MAX_HINT_EFFECTS = 3
#: head 里最多点名的块数，超出折叠为「…等 N 段」。完整名单在
#: ``visible_info["degraded"]`` 里，模型随时可查，不必挤在提示里。
_MAX_HINT_NAMES = 6
#: 具名后果的展示优先级：**静默废掉一个守卫**的块排前面。只有前
#: ``_MAX_HINT_EFFECTS`` 条能进提示，按声明序取会让无关块把关键块挤掉
#: （实测：night 在 STATE_GUARDED_BLOCKS 里排第 18，按顺序取永远上不了榜）。
_DEGRADED_HINT_PRIORITY: tuple[str, ...] = (
    "night",        # 拾荒/夜战守卫：_at_scavenging_site 返回否 → 守卫静默失效
    "queue",        # can_next/has_client：叫号与打烊判定
    "shutter_closed",  # 开关门守卫：重复调用会让门状态抖动
    "client",       # 有没有顾客：几乎所有动作的前置
    "table",        # place_item 幂等
    "dialog",       # 「未关闭界面优先关闭」硬规则
    "inventory",    # 补货/卖完判定
    "economy",      # 进货是否划算
    "mortgage",     # 周供
    "screen",       # screen_state 推导
)


def _degradation_hints(info: dict[str, Any]) -> list[str]:
    """把「这块是不知道，不是没有」翻译成模型能照做的约束（T13）。

    只在 ``info["is_degraded"]`` 为真时产出——直接调 :func:`_next_step_hints`
    且未注入降级信息的调用方（含既有测试）行为**完全不变**。

    为什么必须显式说：``visible_info`` 里降级块的值是 ``None`` / ``[]`` /
    写死的 ``false``，模型无法从 JSON 形状上区分「读失败」与「真为空」——
    这正是本项目反复踩的「把不知道当成没有」。
    """
    degraded = info.get("degraded")
    if not degraded or not info.get("is_degraded"):
        return []
    by_mode: dict[str, list[str]] = {"A": [], "B": [], "C": []}
    for item in degraded:
        if not isinstance(item, dict):
            continue
        block = str(item.get("block") or "")
        mode = str(item.get("mode") or "")
        if block and mode in by_mode:
            by_mode[mode].append(block)
    if not any(by_mode.values()):
        return []

    blocks: list[str] = []
    for mode in ("A", "B", "C"):
        blocks.extend(by_mode[mode])
    # 折叠项只带 ``<payload>`` 占位名与一份给人看的 ``detail``；提示要逐块讲
    # 后果，所以这里按 ``blocks`` 展开成真实块名。不展开的话提示会退化成
    # 「1 段没读到：<payload>」——段数记错、块名全丢，模型拿不到任何行动约束。
    expanded: list[str] = []
    for item in degraded:
        if not isinstance(item, dict):
            continue
        block = str(item.get("block") or "")
        # 折叠项带 ``blocks``（真实块名元组），普通项不带 —— 据此展开，
        # 不必依赖 channels 里的私有占位名。
        if item.get("blocks"):
            names = item.get("blocks") or ()
            if isinstance(names, str):
                names = (names,)
            expanded.extend(str(n) for n in names if n)
        elif block:
            expanded.append(block)
    if expanded:
        blocks = expanded
    # 去重但保序，再按「坏了最危险」重排：具名后果只有前 _MAX_HINT_EFFECTS 条
    # 上得了提示，必须把静默废守卫的块排前面（如 night → 拾荒守卫），
    # 否则顺序靠前的无关块（screen/dialog）会把 night 挤掉。
    seen: set[str] = set()
    deduped = [b for b in blocks if not (b in seen or seen.add(b))]
    ordered = [b for b in _DEGRADED_HINT_PRIORITY if b in deduped]
    ordered += [b for b in deduped if b not in _DEGRADED_HINT_PRIORITY and b in _DEGRADED_HINT_EFFECT]
    ordered += [b for b in deduped if b not in _DEGRADED_HINT_EFFECT]
    named = ordered[:_MAX_HINT_NAMES]
    rest = len(ordered) - len(named)
    listing = "、".join(named)
    if rest > 0:
        listing += f"…等共 {len(ordered)} 段"
    head = (
        f"⚠️ 本轮有 {len(ordered)} 段状态**没读到**（不是空的）：{listing}。"
        "这些字段的值不可信——只能当「未知」处理，绝不能当「没有/为空/为假」："
        "先做不依赖它们的动作，或先换手段确认，不要基于它们下结论。"
    )
    hints = [head]
    for block in ordered[:_MAX_HINT_EFFECTS]:
        effect = _DEGRADED_HINT_EFFECT.get(block)
        if effect:
            hints.append(f"⚠️ {block}：{effect}")
    return hints


def build_observation(state: dict[str, Any], *, source: str) -> dict[str, Any]:
    """把桥 /state（或 statewatch 摘要）组装为 brain 观察契约 dict。"""
    if not isinstance(state, dict):
        state = {}
    table = state.get("table") or {}
    table = table if isinstance(table, dict) else {}
    table_item = table.get("negociated_item") if isinstance(table.get("negociated_item"), dict) else None
    queue = state.get("queue") or {}
    queue = queue if isinstance(queue, dict) else {}
    # T05-P0：格子摘要只算一次——两处引用必须是同一份结果，
    # 否则「slots 列表」与「slots_total」在同一份观察里可能不一致。
    _slot_rows, _slot_total = _slots_summary(state.get("slots"))
    visible_info = {
        "money": state.get("cash"),
        # v2.1：经营天数（桥接新增字段；旧插件为 None）——AI 据此核对
        # 「十天内赚 X」这类跨天目标的进度，不再只能靠 day_until_rent 猜。
        "day": state.get("day"),
        "time_or_day": (
            f"第 {state.get('day')} 天" if state.get("day") is not None else None
        ),
        "inventory_summary": state.get("inventory_counts"),
        # P4：物品级库存（place_item 的 identifier 来源）与交易桌状态
        "inventory": _compact_inventory(state.get("inventory")),
        # T05-P1：库存件数摘要。口径与 _compact_inventory 严格同源（同一次聚合），
        # 否则「摘要 5 件、清单 3 件」会同时出现在一份观察里，模型无从取舍。
        # 注意：它不是守卫输入（contracts.FORBIDDEN_GUARD_INPUTS 含 inventory_counts）。
        "inventory_counts": _inventory_counts(state.get("inventory")),
        # v1.1：当前顾客可卖清单（buying_tags ∩ 库存）——sell_item 的 identifier 直接从这里取
        "sellable_now": (
            _sellable_from_game(state.get("sellable"))
            or _sellable_now(
                (state.get("client") if isinstance(state.get("client"), dict) else {}),
                _compact_inventory(state.get("inventory")),
            )
        ),
        "table": (
            {
                "negociated_item": (
                    {
                        "identifier": table_item.get("identifier"),
                        "name": table_item.get("name"),
                        "value": table_item.get("value"),
                    }
                    if table_item else None
                ),
                "negociated_value": table.get("negociated_value"),
                # v0.8.2：谈判面板开闭态观测（v0.9.4 起：开着≠需关闭——AI 在面板里
                #         做批发/成交，面板由游戏在换客/打烊时自然处理；close_negociation 已取消）
                "nego_open": table.get("nego_open"),
                # v0.9.11：谈判按钮状态（批发/接受报价）——成交决策依据
                #（r48 实测：批发场景 AI 用 accept_offer 零成交——它看不到按钮状态）
                # 2026-11-15 注：`nego_buttons` 只有 primary/secondary/wholesale/
                # accept_offer 四个键（BridgeApi.cs:1185-1191），没有 accept_multiple。
                "nego_buttons": table.get("nego_buttons"),
                "wholesale_cost": table.get("wholesale_cost"),
                # v2.2：批发方向的显式语义标注——AcceptWholesale 是「我方付钱买入」，
                # 2026-10-24 实测 AI 把它当卖出按钮，卖 100 元货却付出 162、净亏 62。
                "wholesale_note": (
                    "AcceptWholesale=你支付 wholesale_cost 收购对方整批货（现金流出）；"
                    "卖自己的货用 accept_offer / sell_item"
                    if table.get("wholesale_cost") else None
                ),
            }
            if table else None
        ),
        "dialogue": None,
        "client": state.get("client"),
        # v1.2：原始对话段（dialog_advance 守卫用——is_end=true 时禁止推进，
        # 否则打断离店协程，特殊 NPC 永久卡店：r65 检查官实锤）
        "dialog": state.get("dialog"),
        "store_state": state.get("store_state"),
        "scene": state.get("scene"),
        # v0.8.0：夜晚服务选购清单 / 夜间外出状态 / 电话通讯录
        "services": _compact_services(state.get("services")),
        "night": _compact_night(state.get("night")),
        "phone": _compact_phone(state.get("phone")),
        # v0.8.1：顾客队列（can_next=false 且无顾客 = 今日已空，直接打烊）
        "queue": {
            "can_next": queue.get("can_next"),
            "has_client": queue.get("has_client"),
            "count": queue.get("count"),
        },
        # v0.8.3：租约与店铺运维（现金红线 day_until_rent / rent_value 由此计算）
        "rent": {
            "day_until_rent": state.get("day_until_rent"),
            "rent_value": state.get("rent_value"),
            # v1.5：真实应缴额（含延期累计），红线与交租判定都用它
            "rent_due": state.get("rent_due"),
            "can_pay_rent": state.get("can_pay_rent"),
            "garbage_level": state.get("garbage_level"),
            "is_power_on": state.get("is_power_on"),
            "shutter_closed": state.get("shutter_closed"),
        },
        # v0.8.3：经济段（此前完全缺失，导致模型无法判断进货是否划算）
        "economy": _compact_economy(state.get("economy")),
        # v2.2：此前「已采集但未观测」的三个机制段——AI 因此看不到声望代价、
        # 看不到任何委托（fill_request 永远不会被调用）、也不知道设施是否解锁。
        "reputations": _compact_reputations(state.get("reputations")),
        "requests": _compact_requests(state.get("requests")),
        "unlocks": _compact_unlocks(state.get("unlocks")),
        # v2.3：机器与槽位（只保留有内容的机器，避免提示词膨胀）
        "machines": _compact_machines(state.get("machines")),
        # v3.1（正式版适配）：试玩版完全没有的四段——
        #   difficulty/perks 决定「规则与可用特权」，mortgage 是与 7 天租约并行的周供负债，
        #   afterhour_events 会临时改规则/关地点/改价格，locations 是外出目的地的真实来源。
        # 缺了这些，模型会按试玩版规则瞎决策（例如不知道地点已关闭就反复 go_out）。
        "difficulty": state.get("difficulty"),
        "perks": state.get("perks"),
        "mortgage": state.get("mortgage"),
        "afterhour_events": _compact_afterhour_events(state.get("afterhour_events")),
        "locations": state.get("locations"),
        # v3.2（正式版）：鉴定等级（决定鉴定费/可接受品级/吸引力）与养殖产出汇总
        "appraisal": state.get("appraisal"),
        "aquaculture": state.get("aquaculture"),
        # v3.6（正式版）：犯罪值与幸运值——"要不要卖违禁品、要不要举报"的依据；
        # 此前完全不可见，AI 只能凭感觉踩风险。
        "risk": state.get("risk"),
        # T05-P0：七类格子的占用摘要（拾荒地面/左右口袋/背包位…）。
        # 此前 AI 完全看不见「地面还有几件没捡」「口袋满了没」，只能靠提示词猜。
        # 只出非空格子（空格子不携带决策信息），且**没有容量**——见 _slots_summary。
        "slots": _slot_rows,
        "slots_total": _slot_total,
        # T05-P1：展柜陈列的诚实降级（游戏侧无可读入口，不许编造档位/概率）
        "showcase": _showcase_view(state),
    }
    # v2.8：从可卖清单里剔掉顾客**明确拒收**的物品。
    # 实盘证据（Day 5 第 10 步）：sellable_now 给出 packet_red_cigarette，
    # 而同一位顾客的 client.refused 里也有它——两条来源都会漏：
    #   · 游戏判定列表（/sellable）在预算/多样性变化后可能残留旧结果；
    #   · 标签回退路径（buying_tags ∩ item_types）根本不知道 refused。
    # 后果是 AI 照清单去卖、被拦一次才改道，白烧一步。
    _refused_ids = {
        str(x) for x in ((visible_info.get("client") or {}).get("refused") or []) if x
    }
    if _refused_ids:
        visible_info["sellable_now"] = [
            it for it in (visible_info.get("sellable_now") or [])
            if str(it.get("identifier")) not in _refused_ids
        ]

    # v2.8：确定性「本轮优先动作」——见 _next_step_hints 的说明（治「知道不行动」）
    # T13：降级信息必须先算出并放进 visible_info，_next_step_hints 才看得到它。
    # channel 必须传：statewatch 的 None 是「本通道不产出该字段」，与 C# 的
    # `catch { day: null }` 同形不同因，按同一把尺子量会伪造降级（详见 channels.py）。
    _degraded = degradation_report(state, channel=source)
    visible_info["degraded"] = _degraded
    visible_info["is_degraded"] = bool(_degraded)
    visible_info["next_step_hints"] = _next_step_hints(
        visible_info, attempts_tonight=_attempts_tonight(visible_info)
    )
    return {
        "source": source,
        "screen_state": derive_screen_state(state),
        "available_actions": _derive_available_actions(state),
        "important_objects": _build_important_objects(state),
        "confidence": 1.0,
        "visible_info": visible_info,
    }


# ---------------------------------------------------------------- 观察记录

@dataclass
class StructuredObservation:
    """一次结构化观察的结果（含原始状态与签名，供主循环与审计使用）。"""

    source: str                       # bridge / statewatch
    observation: dict[str, Any]       # brain 观察契约
    state: dict[str, Any]             # 原始状态（桥响应或 statewatch 摘要）
    signature: tuple                  # 变化签名（见 channels.state_signature）
    file: Optional[str] = None        # 落盘路径（save=True 时）

    def to_dict(self) -> dict[str, Any]:
        return {
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "source": self.source,
            "observation": self.observation,
            "state": self.state,
            "state_signature": list(self.signature),
        }


class StructuredObserver:
    """结构化观察器：按指定通道反复产出观察。

    - bridge 通道：每次 GET /state（毫秒级）；
    - statewatch 通道：每次 collect_once()（读最近日志 + 最新存档，百毫秒级）。

    两个通道的产出都归一为 StructuredObservation；任通道异常统一抛
    GameAgentError 子类，由主循环的错误熔断机制处理。
    """

    def __init__(
        self,
        config: Optional[dict[str, Any]] = None,
        *,
        client: Optional[BridgeClient] = None,
    ) -> None:
        self.config = config or load_config()
        self._client = client
        self._watcher: Optional[StateWatcher] = None

    # ------------------------------------------------------------ 通道实现

    def _bridge_client(self) -> BridgeClient:
        if self._client is None:
            self._client = make_bridge_client(self.config)
        return self._client

    def _statewatch(self) -> StateWatcher:
        if self._watcher is None:
            data_dir = resolve_data_dir(self.config)
            if data_dir is None or not data_dir.is_dir():
                raise BridgeError(
                    "statewatch 通道不可用：未找到游戏持久化目录"
                    "（%USERPROFILE%/AppData/LocalLow/<公司>/<产品>/）。"
                )
            statewatch_cfg = self.config.get("statewatch", {}) or {}
            self._watcher = StateWatcher(
                data_dir,
                interval=float(statewatch_cfg.get("poll_interval", 0.5)),
                max_events=int(statewatch_cfg.get("max_events", 500)),
            )
        return self._watcher

    def _observe_bridge(self) -> tuple[dict[str, Any], dict[str, Any]]:
        client = self._bridge_client()
        state = client.get_state()
        # v2.3：机器槽位（水商/养殖/熔炉）——失败不影响主观察
        try:
            machines = client.get_machines().get("machines") or []
            if machines:
                state["machines"] = machines
        except (AttributeError, BridgeError, OSError, ValueError):
            # AttributeError：测试里的假客户端没有 get_machines（保持向后兼容）
            pass
        # v2.4：游戏本体判定的可卖清单——失败时回退到标签法
        try:
            got = client.get_sellable()
            state["sellable"] = got.get("sellable") or []
            state["sellable_total"] = got.get("estimated_total") or 0
        except (AttributeError, BridgeError, OSError, ValueError):
            pass
        # T05-P0：七类格子占用（拾荒地面/左右口袋/背包位…）——失败不影响主观察。
        # 注意：拿不到时**不写键**，观察层会把「无键」渲染成 None（不可观测），
        # 而不是 []（确实全空）——两者对模型的含义完全不同，见 _slots_summary。
        # 同理 client.get_debug_slots() 拿不到时返回 {"slots": None}，
        # 这里**不能用 `or []`** 兜底，否则又把「不知道」压成「确实空」（2026-11-15 回归）。
        try:
            got_slots = client.get_debug_slots().get("slots")
            if isinstance(got_slots, list):
                state["slots"] = got_slots
        except (AttributeError, BridgeError, OSError, ValueError):
            # AttributeError：测试里的假客户端没有 get_debug_slots（向后兼容）
            pass
        return state, build_observation(state, source=CHANNEL_BRIDGE)

    def _observe_statewatch(self) -> tuple[dict[str, Any], dict[str, Any]]:
        update = self._statewatch().collect_once(history_lines=60)
        game_state = update.state
        summary = game_state.summary()
        # statewatch 摘要没有桥的 store_state/client 结构，补一层归一：
        # summary 的 client 是推导结果（dict 或 None），转成桥同款字段名。
        client = summary.get("client") or None
        normalized = {
            "in_game": summary.get("cash") is not None or client is not None,
            "scene": None,
            "store_state": None,
            "shutter_closed": None,
            "cash": summary.get("cash"),
            "day": summary.get("day"),
            "day_until_rent": summary.get("day_until_rent"),
            "rent_value": summary.get("rent_value"),
            "is_power_on": summary.get("is_power_on"),
            "inventory_counts": summary.get("inventory_counts"),
            "client": (
                {
                    "identifier": client.get("client_id"),
                    "intent": client.get("intent"),
                    "stage": client.get("stage"),
                    "is_selling": None,
                    "is_buying": None,
                }
                if client else None
            ),
            "dialog": {"is_end": None, "is_choice": None},
        }
        return normalized, build_observation(normalized, source=CHANNEL_STATEWATCH)

    # ------------------------------------------------------------ 公开接口

    def observe(self, source: str, *, save: bool = True) -> StructuredObservation:
        """执行一次观察。

        Args:
            source: "bridge" 或 "statewatch"。
            save: 是否落盘到 artifacts/observations/（struct_obs_*.json）。

        Raises:
            BridgeError: statewatch 通道数据目录不可用等。
            GameAgentError: 各通道底层异常（桥不可达等）。
        """
        if source == CHANNEL_BRIDGE:
            state, observation = self._observe_bridge()
        elif source == CHANNEL_STATEWATCH:
            state, observation = self._observe_statewatch()
        else:
            raise ValueError(f"StructuredObserver 不支持通道：{source!r}")

        record = StructuredObservation(
            source=source,
            observation=observation,
            state=state,
            # T13：签名末位是降级证据位，必须与 build_observation 用同一个 channel，
            # 否则同一份 state 在 visible_info 与 signature 里会得出不同的降级结论。
            signature=state_signature(state, channel=source),
        )
        if save:
            record.file = str(self._save(record))
        return record

    def _save(self, record: StructuredObservation) -> Path:
        out_dir = artifacts_dir(self.config) / "observations"
        out_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
        path = out_dir / f"struct_obs_{record.source}_{timestamp}.json"
        path.write_text(
            json.dumps(record.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return path
