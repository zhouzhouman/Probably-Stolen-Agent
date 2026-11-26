"""桥接客户端：与游戏内 DeepSpaceBridge 插件通信。

协议（与 bridge/ 目录下 C# 插件一一对应）：
    GET  /ping          探活：{"ok":true,"version":"0.2.0"}
    GET  /state         内存实时状态：in_game / cash / store_state / client / dialog /
                        table（交易桌）/ inventory（库存摘要）…
    GET  /debug/client  调试信息：当前议价物品 / 玩家与顾客的物品网格等
    GET  /decision      读回浮层当前状态（visible / has_decision / action …）
    POST /action        直调游戏 API，请求体 {"action":"open_shutter"}
    POST /decision      推送决策到浮层，请求体
                         {"action":"click","reason":"...","confidence":0.9,
                          "params_text":"x=1223 y=716","step":3,"ts":"..."}

设计要点：
- 零第三方依赖（urllib），与项目其余部分保持一致
- 所有失败统一抛 BridgeError（含连接拒绝 / 超时 / 响应非法 JSON）
- 超时默认 20s：插件侧主线程调度上限 15s，留足余量
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from datetime import datetime
from typing import Any, Optional

from game_agent.contracts import derive_actions
from game_agent.errors import BridgeError

DEFAULT_BASE_URL = "http://127.0.0.1:8965"

# 默认超时 20s。硬性约束：客户端超时必须大于服务端主线程派发上限 15s
# （bridge/BridgeApi.cs：RunOnMain(timeoutMs = 15000)）——若客户端先超时放弃，
# 服务端仍会执行完该动作，AI 随后的重试将造成重复执行（重复扣费/成交）。
# 配置值 < 15 时 config.py 的 load_config 会输出警告（BRIDGE_MIN_TIMEOUT_SECONDS）。
DEFAULT_TIMEOUT = 20.0

# 插件支持的动作白名单（唯一真相在 game_agent.contracts）。
#
# X01 同源化：这里原本是手工维护的 59 项元组，与 brain.schema.BRIDGE_ACTION_PARAMS、
# agent_loop 的各套白名单各自演进、多次漂移。现在从契约注册表派生，顺序即提示词
# 与 CLI 的展示顺序（contracts.SPECS 的登记顺序）。
#
# 注意包含 force_dismiss：它是 v1.5 卡店恢复专用动作（不进
# BRIDGE_ACTION_PARAMS，模型看不到，仅 agent_loop 的自动恢复路径直接构造）。
ACTIONS = derive_actions()


def format_params_text(params: Optional[dict[str, Any]], limit: int = 120) -> str:
    """把决策 params 压缩成一行短文本（浮层展示用）。

    {"x": 1223, "y": 716} → "x=1223 y=716"
    """
    if not params:
        return ""
    parts = []
    for key, value in params.items():
        text = str(value)
        if len(text) > 24:
            text = text[:24] + "…"
        parts.append(f"{key}={text}")
    return " ".join(parts)[:limit]


class BridgeClient:
    """DeepSpaceBridge HTTP 客户端。

    Args:
        base_url: 桥接服务地址（默认 http://127.0.0.1:8965）
        timeout:  单次请求超时秒数（默认 20）
    """

    def __init__(
        self,
        base_url: Optional[str] = None,
        timeout: Optional[float] = None,
    ) -> None:
        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self.timeout = float(timeout if timeout is not None else DEFAULT_TIMEOUT)
        # 显式禁用代理：桥接是 127.0.0.1 回环地址，绝不应走 http_proxy
        # （沙箱/部分桌面环境会设置全局代理变量，urllib 默认遵循之，导致本地连接被截胡）
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def __repr__(self) -> str:  # pragma: no cover - 调试辅助
        return f"BridgeClient(base_url={self.base_url!r}, timeout={self.timeout})"

    # ------------------------------------------------------------ 基础请求

    def _request(
        self,
        method: str,
        path: str,
        payload: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """发一次 HTTP 请求并解析 JSON 响应；任何失败都归一为 BridgeError。"""
        data: Optional[bytes] = None
        headers = {"Accept": "application/json"}
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"

        request = urllib.request.Request(
            self.base_url + path, data=data, headers=headers, method=method
        )
        try:
            with self._opener.open(request, timeout=self.timeout) as resp:
                body = resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            raise BridgeError(f"桥接返回 HTTP {exc.code}（{method} {path}）") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise BridgeError(
                f"桥接不可达（{self.base_url}{path}）：{exc}。"
                "请确认游戏正在运行且 DeepSpaceBridge 插件已加载。"
            ) from exc

        try:
            parsed = json.loads(body)
        except json.JSONDecodeError as exc:
            raise BridgeError(f"桥接响应不是合法 JSON（{method} {path}）：{body[:120]}") from exc
        if not isinstance(parsed, dict):
            raise BridgeError(f"桥接响应结构异常（{method} {path}）：期望 JSON 对象")
        return parsed

    # ------------------------------------------------------------ 探活与状态

    def ping(self) -> dict[str, Any]:
        """探活：返回 {"ok":true,"version":"..."}（版本与插件一致）。"""
        return self._request("GET", "/ping")

    def is_available(self) -> bool:
        """桥接是否就绪（不抛异常，供主循环 best-effort 判断）。"""
        try:
            return bool(self.ping().get("ok"))
        except BridgeError:
            return False

    def get_state(self) -> dict[str, Any]:
        """读内存实时状态（现金 / 门 / 当前顾客 / 对话状态 / 场景…）。

        Returns:
            状态 dict；游戏不在局内时 in_game=false。
        """
        response = self._request("GET", "/state")
        state = response.get("state")
        return state if isinstance(state, dict) else response

    def get_debug_client(self) -> dict[str, Any]:
        """读调试信息（当前议价物品 / 物品网格 / 可售物品列表）。"""
        response = self._request("GET", "/debug/client")
        debug = response.get("debug")
        return debug if isinstance(debug, dict) else response

    def get_machines(self) -> dict[str, Any]:
        """读机器列表与槽位内容（GET /machines）。

        返回 {"machines":[{index,identifier,name,slots:[{slot,items:[...]}]}]}。
        """
        response = self._request("GET", "/machines")
        machines = response.get("machines")
        return {"machines": machines if isinstance(machines, list) else []}

    def get_sellable(self) -> dict[str, Any]:
        """游戏本体判定的「当前顾客收不收」（GET /sellable，v2.4）。

        返回 {"sellable":[{identifier,count,value,budget_ok}], "estimated_total":int}。
        比 buying_tags ∩ item_types 准确：活鱼这类没有类型标签的物品，
        标签法永远算不出来，只有游戏自己的 CanSellThisItem 知道。
        """
        response = self._request("GET", "/sellable")
        sellable = response.get("sellable")
        return {
            "sellable": sellable if isinstance(sellable, list) else [],
            "estimated_total": response.get("estimated_total") or 0,
        }

    def get_ui_buttons(self) -> dict[str, Any]:
        """枚举当前界面全部活跃按钮（名称/文本/可交互/路径/坐标）。

        v0.9.0「UI 按钮化」配套查询：用于盘点界面按钮、定位目标按钮后
        再以 click_button 动作触发点击（等效玩家点击）。
        """
        return self._request("GET", "/ui/buttons")

    def get_debug_slots(self) -> dict[str, Any]:
        """读七类关键格子的占用情况（GET /debug/slots，T05）。

        返回 ``{"slots":[{label,identifier,count,gameObject,items}]}``，
        七个格子见 ``mod/src/BridgeApi.cs`` 的 ``BuildDebugSlots``：
        ``front``(前台) / ``back`` / ``counter``(台面) / ``ground``(拾荒地面) /
        ``pocketLeft`` / ``pocketRight`` / ``backpackSlot``。

        为什么需要它：拾荒时 AI 完全看不见「地面还有几件没捡」「口袋满了没」，
        只能凭提示词里的猜测行动；``/state`` 的 inventory 只汇总背包与顾客，
        不区分格子，也不含拾荒地面那几只临时容器。

        **没有容量字段**：``GameInventory`` 基类无 capacity，
        ``GameGridInventory.widthPixels/heightPixels/inventoryShape`` 与
        ``GameSlotInventory.defaultGridWidth/defaultGridHeight`` 全是
        private/readonly，反射读不到。所以只能给「件数」，给不了「满了没」。

        宽容返回：拿不到 ``slots`` 时返回 ``{"slots": None}``（**不是 ``[]``**），
        调用方不需要为「旧插件没这个端点」写分支，但必须把 None 当「不知道」。

        **为什么必须区分 None 与 []**（2026-11-15 实机抓出的回归）：
        ``BuildDebugSlots`` 在非店铺场景走的是 ``Err("EmporiumEntry 不可用（不在店铺场景）")``，
        响应体形如 ``{"ok":false,"error":"..."}`` —— **没有 ``slots`` 键，且 HTTP 状态码是 200**，
        所以 ``_request`` 不会抛 ``BridgeError``。原先这里写
        ``slots if isinstance(slots, list) else []`` 就把「端点报错」和「七格全空」压成同一个
        ``[]``，观察层于是把「不知道地上有没有货」渲染成「地上确认没货」—— 拾荒时模型据此
        ``leave_outside``，地上的东西全部消失（事故 r55 的形态）。
        故此处**只做形状归一**：``ok != False`` 且给了 list 才是真数据，其余一律 None。
        """
        response = self._request("GET", "/debug/slots")
        if response.get("ok") is False:
            return {"slots": None}
        slots = response.get("slots")
        return {"slots": slots if isinstance(slots, list) else None}

    # ------------------------------------------------------------ 动作直调

    def post_action(self, action: str, **params: Any) -> dict[str, Any]:
        """直调游戏 API（POST /action）。

        Args:
            action: 动作名，必须在 ACTIONS 白名单内
            **params: 附加参数（如 dialog_choose 的 index）
        Returns:
            插件响应（ok / detail / 执行后的最新 state）

        ``place_item`` 走「玩家路径优先」：先尝试 Ctrl+点击物品真上桌
        （ui_item_point → player_click modifier=LeftControl → 回读确认），
        失败才回退到原有的字段标记实现。这样"桌上幽灵物品"的根源被修掉，
        而物品被遮挡/无顾客等情况下仍有兜底。
        """
        if action not in ACTIONS:
            raise BridgeError(
                f"未知桥接动作：{action}（可用：{', '.join(ACTIONS)}）"
            )
        if action == "place_item" and params.get("identifier"):
            played = self._try_place_item_player_path(str(params["identifier"]))
            if played is not None:
                return played
        return self._request("POST", "/action", {"action": action, **params})

    def _try_place_item_player_path(self, identifier: str) -> Optional[dict[str, Any]]:
        """Ctrl+点击把物品真拖上交易台；不可行时返回 None 让调用方回退。

        判定标准（与项目铁律一致）：只认状态变化——回读后
        ``table.negociated_item`` 必须指向该物品，否则视为未成功。
        """
        try:
            before = self.get_state()
        except BridgeError:
            return None
        state = before.get("state", before)
        if not isinstance(state, dict):
            return None
        # 无顾客时游戏不收上桌；跳过玩家路径，交给原实现给出准确报错。
        if not state.get("client"):
            return None
        scene = state.get("scene")

        point = self._request("POST", "/action", {"action": "ui_item_point", "identifier": identifier})
        if not point.get("ok") or "x" not in point:
            return None

        click = self._request(
            "POST", "/action",
            {
                "action": "player_click",
                "x": point["x"],
                "y": point["y"],
                "modifier": "LeftControl",
                "scene": scene,
            },
        )
        if not click.get("ok"):
            return None

        try:
            after = self.get_state()
        except BridgeError:
            return None
        after_state = after.get("state", after)
        placed = None
        try:
            placed = (after_state.get("table") or {}).get("negociated_item")
        except AttributeError:
            placed = None
        placed_id = placed.get("identifier") if isinstance(placed, dict) else None
        if placed_id != identifier:
            return None  # 没真上桌 → 回退
        return {
            "ok": True,
            "detail": f"已用 Ctrl+点击把 {identifier} 拖上交易台（玩家路径，物品真实移动）",
            "state": after_state,
            "path": "player",
        }

    # ------------------------------------------------------------ 决策浮层

    def post_decision(
        self,
        decision: dict[str, Any],
        *,
        step: Optional[int] = None,
        params_text: Optional[str] = None,
    ) -> dict[str, Any]:
        """把一条 AI 决策推送到游戏内浮层（POST /decision）。

        Args:
            decision:   决策 dict（{action, params, reason, confidence}）
            step:       步骤序号（浮层显示 "#N"）
            params_text: 参数短文本（默认由 decision["params"] 自动生成）
        Returns:
            插件响应（ok / detail）
        """
        if not isinstance(decision, dict) or not decision.get("action"):
            raise BridgeError("decision 缺少 action 字段，无法推送浮层")

        if params_text is None:
            params_text = format_params_text(decision.get("params"))

        try:
            confidence = float(decision.get("confidence") or 0)
        except (TypeError, ValueError):
            confidence = 0.0

        payload: dict[str, Any] = {
            "action": str(decision["action"]),
            "reason": str(decision.get("reason") or ""),
            "confidence": round(confidence, 4),
            "params_text": params_text,
            "ts": datetime.now().isoformat(timespec="seconds"),
        }
        if step is not None:
            payload["step"] = int(step)
        return self._request("POST", "/decision", payload)

    # ------------------------------------------------------------ 模型配置（v3.7）
    # 游戏内 Ctrl+F9 面板与这几个方法写的是同一份 Mods/DeepSpaceBridge/model_config.ini，
    # 所以"面板改"和"命令行改"不会打架，也不会出现两套真相。

    def get_model_config(self) -> dict[str, Any]:
        """读取当前模型配置（地址/模型名/是否设置了 Key/token/温度）。"""
        return self._request("GET", "/modelconfig")

    def set_model_config(self, **fields: Any) -> dict[str, Any]:
        """写入模型配置。字段名与 /modelconfig 一致：base_url/model/api_key/max_tokens/temperature。"""
        payload = {k: v for k, v in fields.items() if v is not None}
        return self._request("POST", "/modelconfig", payload)

    def reset_model_config(self) -> dict[str, Any]:
        """删除 ini，回到「完全沿用 config/agent.json」。"""
        return self._request("POST", "/modelconfig/reset", {})

    def get_decision(self) -> dict[str, Any]:
        """读回浮层当前状态（visible / has_decision / action / reason …）。"""
        response = self._request("GET", "/decision")
        decision = response.get("decision")
        return decision if isinstance(decision, dict) else response


def make_bridge_client(config: Optional[dict[str, Any]] = None) -> BridgeClient:
    """按配置构造桥接客户端（config 缺省时用默认地址）。

    config 结构（config/agent.json 的 "bridge" 段）：
        {"base_url": "http://127.0.0.1:8965", "timeout_seconds": 20}

    注意：timeout_seconds 必须 > 15（服务端 RunOnMain 派发上限），否则客户端
    先放弃而服务端仍在执行动作，重试会重复执行；load_config 会对此告警。
    """
    config = config or {}
    bridge_cfg = config.get("bridge", {}) or {}
    return BridgeClient(
        base_url=bridge_cfg.get("base_url"),
        timeout=bridge_cfg.get("timeout_seconds"),
    )
