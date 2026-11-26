"""商业目标跟踪：把「30 天内净赚 3000（攒够买房首付）」变成每一步都可见的硬指标。

为什么需要它
------------
决策模型每一步只看得到「当前状态 + 最近 6 步历史」，没有跨天的目标记忆：
它会认真做单笔生意，却不会为「30 天后要净赚 3000」调整节奏（该清库存时进货、
该收手时接低价单）。本模块把目标变成可计算、可持久化、可复述的进度条，
由主循环每步更新，并渲染成提示词里的一段硬性 KPI。

记分口径（关键设计）
--------------------
赚到的钱 = **现金净增**：profit = 当前现金 − 期初现金。

这是玩家语境里「赚了多少钱」的直白含义，也避免了一个会计陷阱：
库存估值（economy.stock_value）是游戏自己估的零售价，不是可变现的确定收益，
用它算净值会把「还没卖出去的货」提前记成利润（囤货刷分）。

库存估值仍然展示（资产合计 = 现金 + 库存），只作参考，不计入本期利润。
因此：进货是支出（会暂时压低进度，但换来可卖的货），卖货是收入，
租金/夜间服务/清洁同样是支出。

天数口径
--------
优先用桥接 /state 的 day（v2.1 起提供）；拿不到时退化为「清晨计数」
（每次 store_state 由非 MORNING 变为 MORNING 记 1 天），保证旧插件也能用。
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from game_agent.config import artifacts_dir, load_config

__all__ = ["DEFAULT_GOAL", "GoalProgress", "BusinessGoal"]

DEFAULT_GOAL: dict[str, Any] = {
    "enabled": False,
    # 口径与 config/agent.json 的生效值一致（30 天 / 3000）。
    # v3.7：此前默认写的是试玩版口径「十日内净赚 1000 元」，与 agent.json 生效值
    # 不一致——config 缺 goal 块时会静默退化成试玩版目标，且目标文案会误导读模型。
    # 这里只统一默认值，不改变读取逻辑：config 有 goal 块时仍以 config 为准。
    # T06-P0：label 不再自带数字与买店叙事（此前是「30 天内净赚 3000（正式版：攒够
    # 买房首付）」）。两个理由：①金额/天数的唯一真源是下面的 target_profit / days，
    # 文案里重复一遍会在改配置后立刻过期；②「攒够买房首付」是把长期目标混进 30 天
    # 目标的叙事，会让模型把「30 天净赚 3000」误解成「30 天必须买下店」。
    "label": "净赚目标（金额与天数见 target_profit / days）",
    "target_profit": 3000,
    "days": 30,
    # 期初锚点：留空 = 首次观察时自动锚定（也可手动写死，便于复盘/重跑）
    "start_day": None,
    "start_cash": None,
    "start_stock": None,
    "start_equity": None,
}


def _int_or_none(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


@dataclass
class GoalProgress:
    """一次观察下的目标进度（纯数据，便于落盘与测试）。"""

    label: str
    target_profit: int
    window_days: int
    start_day: Optional[int]
    day: Optional[int]
    elapsed_days: int
    days_left: int
    start_cash: float
    start_stock: float
    start_equity: float
    equity: float
    cash: float
    stock_value: float
    profit: float
    pct: float
    required_daily: float
    rebased: bool = False

    @property
    def done(self) -> bool:
        return self.profit >= self.target_profit

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["done"] = self.done
        return data


class BusinessGoal:
    """目标进度跟踪器（每步 update，随时 render 成提示词块）。

    Args:
        config: 配置字典（读取 "goal" 段）。
        path: 进度文件路径（默认 artifacts/goal_state.json）。
    """

    def __init__(
        self,
        config: Optional[dict[str, Any]] = None,
        *,
        path: Optional[str | Path] = None,
    ) -> None:
        config = config or load_config()
        goal_cfg = dict(DEFAULT_GOAL)
        goal_cfg.update(config.get("goal") or {})
        self.config = config
        self.enabled = bool(goal_cfg.get("enabled"))
        self.label = str(goal_cfg.get("label") or DEFAULT_GOAL["label"])
        try:
            self.target_profit = int(goal_cfg.get("target_profit") or 0)
        except (TypeError, ValueError):
            self.target_profit = 0
        try:
            self.window_days = max(
                1, int(goal_cfg.get("days") or DEFAULT_GOAL["days"])
            )
        except (TypeError, ValueError):
            self.window_days = int(DEFAULT_GOAL["days"])
        self.path = Path(path) if path else artifacts_dir(config) / "goal_state.json"

        self.start_day: Optional[int] = _int_or_none(goal_cfg.get("start_day"))
        self.start_cash: Optional[float] = (
            float(goal_cfg["start_cash"])
            if goal_cfg.get("start_cash") not in (None, "")
            else None
        )
        self.start_stock: Optional[float] = (
            float(goal_cfg["start_stock"])
            if goal_cfg.get("start_stock") not in (None, "")
            else None
        )
        self.start_stock: Optional[float] = (
            float(goal_cfg["start_stock"])
            if goal_cfg.get("start_stock") not in (None, "")
            else None
        )
        self.start_equity: Optional[float] = (
            float(goal_cfg["start_equity"])
            if goal_cfg.get("start_equity") not in (None, "")
            else None
        )
        self.elapsed_days = 0
        self.mornings = 0
        self.last_store_state: Optional[str] = None
        self.last_day: Optional[int] = None
        self.rebased: bool = False
        self._last: Optional[GoalProgress] = None
        self._load()

    # ------------------------------------------------------------ 持久化

    def _load(self) -> None:
        """读取上次进度（目标参数变了则视为新周期，重新锚定）。"""
        if not self.path.is_file():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(data, dict):
            return
        if data.get("target_profit") != self.target_profit or data.get("window_days") != self.window_days:
            return  # 目标改了：重新开始
        if self.start_day is None:
            self.start_day = _int_or_none(data.get("start_day"))
        if self.start_cash is None and data.get("start_cash") is not None:
            try:
                self.start_cash = float(data["start_cash"])
            except (TypeError, ValueError):
                self.start_cash = None
        if self.start_stock is None and data.get("start_stock") is not None:
            try:
                self.start_stock = float(data["start_stock"])
            except (TypeError, ValueError):
                self.start_stock = None
        if self.start_stock is None and data.get("start_stock") is not None:
            try:
                self.start_stock = float(data["start_stock"])
            except (TypeError, ValueError):
                self.start_stock = None
        if self.start_equity is None and data.get("start_equity") is not None:
            try:
                self.start_equity = float(data["start_equity"])
            except (TypeError, ValueError):
                self.start_equity = None
        self.elapsed_days = max(0, _int_or_none(data.get("elapsed_days")) or 0)
        self.mornings = max(0, _int_or_none(data.get("mornings")) or 0)
        self.last_store_state = data.get("last_store_state")
        self.last_day = _int_or_none(data.get("last_day"))

    def reset(self) -> None:
        """清空进度（重新锚定起点）。"""
        self.start_day = None
        self.start_cash = None
        self.start_stock = None
        self.start_equity = None
        self.elapsed_days = 0
        self.mornings = 0
        self.last_store_state = None
        self.last_day = None
        try:
            self.path.unlink(missing_ok=True)
        except OSError:
            pass

    def _save(self, progress: GoalProgress) -> None:
        payload = {
            "label": self.label,
            "target_profit": self.target_profit,
            "window_days": self.window_days,
            "start_day": self.start_day,
            "start_cash": self.start_cash,
            "start_stock": self.start_stock,
            "start_equity": self.start_equity,
            "elapsed_days": self.elapsed_days,
            "mornings": self.mornings,
            "last_store_state": self.last_store_state,
            "last_day": self.last_day,
            "last_progress": progress.to_dict(),
            "updated_at": datetime.now().isoformat(timespec="seconds"),
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except OSError:
            pass  # best-effort：进度落盘失败不阻断主循环

    # ------------------------------------------------------------ 观察解析

    @staticmethod
    def _numbers(observation: Any) -> tuple[Optional[float], Optional[float], Optional[int], Optional[str]]:
        """从观察契约里取 (现金, 库存估值, 天数, 营业状态)。缺失一律 None。"""
        if not isinstance(observation, dict):
            return None, None, None, None
        vi = observation.get("visible_info")
        vi = vi if isinstance(vi, dict) else {}
        cash = vi.get("money")
        try:
            cash = float(cash) if cash is not None else None
        except (TypeError, ValueError):
            cash = None
        economy = vi.get("economy")
        stock = economy.get("stock_value") if isinstance(economy, dict) else None
        try:
            stock = float(stock) if stock is not None else None
        except (TypeError, ValueError):
            stock = None
        day = _int_or_none(vi.get("day"))
        if day is None:
            # 兼容旧观察：从 "第 73 天" 文案里抠数字
            text = str(vi.get("time_or_day") or "")
            digits = "".join(ch for ch in text if ch.isdigit())
            day = _int_or_none(digits) if digits else None
        store_state = vi.get("store_state")
        return cash, stock, day, (str(store_state).upper() if store_state is not None else None)

    # ------------------------------------------------------------ 每步更新

    def update(self, observation: Any) -> GoalProgress:
        """吸收一次观察，返回最新进度（不启用的目标仅做零成本解析）。"""
        cash, stock, day, store_state = self._numbers(observation)
        cash = cash if cash is not None else 0.0
        stock = stock if stock is not None else 0.0
        equity = cash + stock

        # 天数：day 优先；否则用清晨计数兜底
        if store_state == "MORNING" and self.last_store_state not in (None, "MORNING"):
            self.mornings += 1
        if store_state is not None:
            self.last_store_state = store_state
        self.rebased = False
        if (
            day is not None
            and self.last_day is not None
            and abs(day - self.last_day) > 1
        ):
            # 正常推进只会 +1；跨多天（含倒退）视为换档/读档，重新锚定期初
            self.start_day = day
            self.start_cash = cash
            self.start_stock = stock
            self.start_equity = equity
            self.elapsed_days = 0
            self.mornings = 0
            self.rebased = True
        if day is not None:
            self.last_day = day
        if self.start_day is None and day is not None:
            self.start_day = day
        if self.start_cash is None:
            self.start_cash = cash
        if self.start_stock is None:
            self.start_stock = stock
        if self.start_equity is None:
            self.start_equity = equity

        by_day = (self.last_day - self.start_day) if (self.last_day is not None and self.start_day is not None) else 0
        self.elapsed_days = max(self.elapsed_days, self.mornings, by_day, 0)
        self.elapsed_days = min(self.elapsed_days, self.window_days)

        # 口径：赚 = 现金净增（库存估值只作参考，见模块文档）
        profit = cash - float(self.start_cash or 0.0)
        # 剩余天数可以为 0（第 10 天结束）；计算日均时按 1 天兜底，避免除零。
        days_left = max(0, self.window_days - self.elapsed_days)
        required = max(0.0, (self.target_profit - profit) / max(1, days_left))
        pct = (profit / self.target_profit) if self.target_profit else 0.0

        progress = GoalProgress(
            label=self.label,
            target_profit=self.target_profit,
            window_days=self.window_days,
            start_day=self.start_day,
            day=self.last_day,
            elapsed_days=self.elapsed_days,
            days_left=days_left,
            start_cash=round(float(self.start_cash or 0.0), 1),
            start_stock=round(float(self.start_stock or 0.0), 1),
            start_equity=round(float(self.start_equity or 0.0), 1),
            equity=round(equity, 1),
            cash=round(cash, 1),
            stock_value=round(stock, 1),
            profit=round(profit, 1),
            pct=round(pct, 4),
            required_daily=round(required, 1),
            rebased=self.rebased,
        )
        self._last = progress
        self._save(progress)
        return progress

    # ------------------------------------------------------------ 提示词渲染

    @property
    def last(self) -> Optional[GoalProgress]:
        """最近一次 update() 的进度（供连续跑批的驱动器读取）。"""
        return self._last

    def render(self, progress: Optional[GoalProgress] = None) -> str:
        """渲染成提示词里的 KPI 段（未启用返回空串）。"""
        if not self.enabled or self.target_profit <= 0:
            return ""
        p = progress or self._last
        if p is None:
            # 还没有任何观察：只给目标，不给假进度（避免 0 进度误导模型）
            return (
                f"【商业目标（硬性 KPI）】{self.label}——周期 {self.window_days} 天，"
                f"目标现金净增 +{self.target_profit:,}（赚 = 现金变多）。"
            )
        day_text = (
            f"第 {p.day} 天（周期第 {p.elapsed_days + 1}/{p.window_days} 天）"
            if p.day is not None
            else f"周期第 {p.elapsed_days + 1}/{p.window_days} 天（按清晨计数）"
        )
        sign = "+" if p.profit >= 0 else ""
        lines = []
        if p.rebased:
            lines.append("（检测到读档/换档：进度已按当前局面重新锚定，之前的累计作废）")
        lines += [
            f"【商业目标（硬性 KPI，每步都要服从）】{p.label}",
            f"- 进度：{day_text} ｜ 现金 {p.start_cash:,.0f} → {p.cash:,.0f}"
            f"（{sign}{p.profit:,.0f} / 目标 +{p.target_profit:,}，完成 {p.pct * 100:.0f}%）",
            f"- 口径：赚 = **现金净增**（不含尚未卖出的库存）。当前库存估值 {p.stock_value:,.0f}、"
            f"资产合计 {p.equity:,.0f}——库存只有卖出去才会变成进度。"
            "租金/夜间服务/清洁/进货都是支出，卖货才是收入。",
        ]
        if p.done:
            lines.append(
                "- 状态：**已达标**。转入守成——现金始终 ≥ rent_due×3，"
                "只接高毛利单，不囤货、不冒险、不贱卖设备。"
            )
        elif p.days_left <= 3 and p.profit < p.target_profit * 0.6:
            lines.append(
                f"- 状态：**落后**（剩 {p.days_left} 天，日均需 +{p.required_daily:,.0f}）。"
                "立即转入变现模式：优先服务买家、清高值滞销品，暂停非必要进货与升级。"
            )
        else:
            lines.append(
                f"- 今日节奏：剩余 {p.days_left} 天，日均需 +{p.required_daily:,.0f}；"
                "先成交再补货，别让顾客空手走。"
            )
        if p.start_stock > 0:
            lines.append(
                f"- 余额底线：期末库存估值不得低于 {p.start_stock * 0.7:,.0f}"
                f"（期初 {p.start_stock:,.0f} 的 70%）——卖货变现可以，清仓刷分不行。"
            )
        if p.start_stock > 0:
            lines.append(
                f"- 余额底线：期末库存估值不得低于 {p.start_stock * 0.7:,.0f}"
                f"（期初 {p.start_stock:,.0f} 的 70%）——卖货变现可以，清仓刷分不行。"
            )
        lines.append(
            "- 禁止：为凑数贱卖产能设备（MACHINE / STORAGE / 养殖系统 / 机器区 / 能量阵列）"
            "——它们是赚钱工具，不是商品；也不许突破交租现金红线。"
        )
        return "\n".join(lines)
