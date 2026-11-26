"""命令行入口。

用法示例（项目根目录执行；pip install -e . 后可在任意目录执行）：

    # —— 状态监听（直接读游戏底层数据：日志事件流 + 存档快照）——
    python -m game_agent.cli watch --once              # 单次采集：当前快照 + 最近事件
    python -m game_agent.cli watch                     # 实时监听（Ctrl+C 停止）
    python -m game_agent.cli watch --json              # 机器可读（NDJSON）
    python -m game_agent.cli watch --once --from-start # 离线回放：读整个日志文件
    python -m game_agent.cli watch --duration 60       # 监听 60 秒后自动退出

    # —— 观察（视觉方案，保留作调试与兜底）——
    python -m game_agent.cli list-windows            # 列出所有可见窗口
    python -m game_agent.cli capture                 # 捕获游戏窗口截图
    python -m game_agent.cli observe --mock          # 完整观察（离线自检）
    python -m game_agent.cli observe                 # 完整观察（真实视觉模型）

    # —— 控制（鼠标 / 键盘）——
    python -m game_agent.cli click --x 1219 --y 796 --preview   # 点击前目视预览（不点击）
    python -m game_agent.cli click --x 1219 --y 796             # 真实点击（截图坐标系）
    python -m game_agent.cli click --x 1219 --y 796 --dry-run   # 只校验不执行
    python -m game_agent.cli key --name escape                  # 按键
    python -m game_agent.cli type --text hello                  # 输入 ASCII 文本

    # —— 主循环（观察 → 决策 → 执行）——
    python -m game_agent.cli step --mock                # 单步（默认 dry-run，不真实操作）
    python -m game_agent.cli step --mock --live         # 单步（真实操作鼠标键盘）
    python -m game_agent.cli run --max-steps 5 --mock   # 连续步进（默认 dry-run）

    # —— BepInEx 桥接（游戏需运行、插件已加载；决策同时展示在游戏内浮层）——
    python -m game_agent.cli bridge state               # 读内存实时状态
    python -m game_agent.cli bridge decision            # 读回浮层当前状态
    python -m game_agent.cli bridge action open_shutter # 直调游戏 API（白名单动作）
    python -m game_agent.cli bridge action place_item --identifier toothpaste  # 把物品放上交易桌

    # —— 夜晚流程（v0.8.0：打烊 → 选服务 → 定去向 → 睡觉/外出）——
    python -m game_agent.cli bridge action end_day            # 打烊
    python -m game_agent.cli bridge action night_services     # 打开夜晚服务面板
    python -m game_agent.cli bridge action night_service --service INSURANCE_SERVICE  # 勾选保险服务
    python -m game_agent.cli bridge action end_day_confirm    # 确认服务并扣费
    python -m game_agent.cli bridge action open_map           # 打开夜间地图
    python -m game_agent.cli bridge action go_out --destination scavenging  # 外出拾荒
    python -m game_agent.cli bridge action stay_home          #  or 睡觉不出门
    python -m game_agent.cli bridge action leave_outside      # 从外出地点返回

    # —— 电话 / 举报 / 射击（v0.8.0）——
    python -m game_agent.cli bridge action open_phone         # 打开电话面板
    python -m game_agent.cli bridge action phone_call --number 8376     # 打给王尔德助手
    python -m game_agent.cli bridge action report_client      # 举报当前顾客（通缉犯/伪人）
    python -m game_agent.cli bridge action enter_shooting_mode && \
    python -m game_agent.cli bridge action shoot              # 击毙伪人

    python -m game_agent.cli bridge push --action click --reason "测试浮层" \
        --confidence 0.92 --params-text "x=1223 y=716"  # 手动推送一条决策到浮层

坐标约定：click / move 的 x, y 是「游戏窗口截图坐标系」（原点 = 窗口左上角，
与 artifacts/screenshots 中的 PNG 像素一致）；执行时自动换算为屏幕坐标。

退出码：0 成功 | 1 未知错误 | 2 未找到窗口 | 3 截图失败 | 4 分析失败 |
        5 控制失败 | 6 未找到游戏数据目录 | 7 桥接失败
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from game_agent.agent_loop import run_loop, step_once
from game_agent.bridge.client import ACTIONS, BridgeClient, format_params_text
from game_agent.config import artifacts_dir, load_config
from game_agent.control import controller
from game_agent.errors import (
    AnalyzeError,
    BridgeError,
    CaptureError,
    ControlError,
    GameAgentError,
    WindowNotFoundError,
)
from game_agent.goal import DEFAULT_GOAL, BusinessGoal
from game_agent.mcp_server import main as mcp_main
from game_agent.statewatch import StateWatcher, WatchUpdate, resolve_data_dir
from game_agent.structured.channels import (
    CHANNEL_BRIDGE,
    CHANNEL_STATEWATCH,
    CHANNEL_VISION,
    pick_source,
)
from game_agent.structured.observer import StructuredObserver
from game_agent.vision.capture import capture_fullscreen, capture_window
from game_agent.vision.observer import observe
from game_agent.vision.window import find_game_window, list_windows

EXIT_OK = 0
EXIT_UNKNOWN = 1
EXIT_NO_WINDOW = 2
EXIT_CAPTURE = 3
EXIT_ANALYZE = 4
EXIT_CONTROL = 5
EXIT_NO_DATA = 6
EXIT_BRIDGE = 7


def _print_json(data) -> None:
    """把结果以 UTF-8 JSON 输出到 stdout（供脚本消费）。"""
    print(json.dumps(data, ensure_ascii=False, indent=2))


def _info(message: str) -> None:
    """人类可读的提示信息输出到 stderr，保持 stdout 只有纯 JSON。"""
    print(message, file=sys.stderr)


def _find_game_window_or_fail(config, window_arg=None):
    """定位游戏窗口（window_arg 可覆盖配置中的关键词），找不到时抛 WindowNotFoundError。"""
    game_cfg = config.get("game", {})
    keywords = [window_arg] if window_arg else game_cfg.get("window_title_keywords", [])
    excludes = [] if window_arg else game_cfg.get("exclude_title_keywords", [])
    win = find_game_window(keywords, excludes, tuple(game_cfg.get("min_window_size", (400, 300))))
    if win is None:
        raise WindowNotFoundError(
            f"未找到标题包含 {keywords} 的窗口。"
            "可执行 `python -m game_agent.cli list-windows` 查看当前所有窗口标题。"
        )
    return win


# ---------------------------------------------------------------- 观察类命令


def cmd_list_windows(args) -> int:
    windows = list_windows(min_size=(0, 0))
    rows = [
        {
            "hwnd": win.hwnd,
            "title": win.title,
            "size": [win.width, win.height],
            "foreground": win.is_foreground,
            "minimized": win.is_minimized,
            "pid": win.process_id,
        }
        for win in windows
    ]
    _print_json({"count": len(rows), "windows": rows})
    return EXIT_OK


def cmd_capture(args) -> int:
    config = load_config()
    shots_dir = artifacts_dir(config) / "screenshots"

    if args.fullscreen:
        result = capture_fullscreen(shots_dir, tag=args.tag)
    else:
        win = _find_game_window_or_fail(config, args.window)
        result = capture_window(win, shots_dir, tag=args.tag)

    _info(
        f"截图已保存：{result.image_path}"
        f"（方式：{result.method}，尺寸：{result.image_size[0]}x{result.image_size[1]}，"
        f"亮度均值：{result.stats['mean_brightness']}）"
    )
    for warning in result.warnings:
        _info(f"提示：{warning}")
    _print_json(result.to_dict())
    return EXIT_OK


def cmd_observe(args) -> int:
    config = load_config()
    source = getattr(args, "source", "auto") or "auto"

    # 结构化通道：直读游戏状态（无截图、无视觉 API 调用）
    if source in (CHANNEL_BRIDGE, CHANNEL_STATEWATCH) or (
        source == "auto" and not args.mock and not args.fullscreen
    ):
        if args.mock:
            source = CHANNEL_VISION
        else:
            effective = pick_source(config) if source == "auto" else source
            if effective in (CHANNEL_BRIDGE, CHANNEL_STATEWATCH):
                record = StructuredObserver(config).observe(effective)
                _info(f"通道：{effective}（结构化直读，无截图）")
                _info(f"状态签名：{list(record.signature)}")
                if record.file:
                    _info(f"观察记录：{record.file}")
                _print_json(record.observation)
                return EXIT_OK
            source = CHANNEL_VISION

    record = observe(
        config,
        window_keywords=[args.window] if args.window else None,
        fullscreen=args.fullscreen,
        mock=args.mock,
        mock_file=args.mock_file,
        tag=args.tag,
    )
    meta = record.meta["analyze"]
    _info(f"截图：{record.screenshot}")
    _info(f"分析：provider={meta['provider']} model={meta['model']} 耗时={meta['duration_ms']}ms")
    if record.meta.get("observation_file"):
        _info(f"观察记录：{record.meta['observation_file']}")
    if record.meta.get("mock"):
        _info("提示：当前为离线 Mock 模式，输出的是固定样例数据，不代表对截图的真实理解")
    _print_json(record.observation)
    return EXIT_OK


# ---------------------------------------------------------------- 控制类命令


def cmd_click(args) -> int:
    config = load_config()
    win = _find_game_window_or_fail(config, args.window)

    if args.preview:
        shots_dir = artifacts_dir(config) / "screenshots"
        cap = capture_window(win, shots_dir, tag="preview")
        full, zoom = controller.save_click_preview(
            Path(cap.image_path), args.x, args.y, artifacts_dir(config) / "clicks"
        )
        _info(f"预览全图（红十字标记目标点）：{full}")
        _info(f"预览放大图（2x）：{zoom}")
        _info("确认落点无误后，去掉 --preview 重新执行即可真实点击")
        _print_json({"preview": True, "full": str(full), "zoom": str(zoom), "image_size": list(cap.image_size)})
        return EXIT_OK

    result = controller.click_image(
        win, args.x, args.y,
        dry_run=args.dry_run,
        move_duration=float(config.get("control", {}).get("move_duration", 0.35)),
    )
    _info(("[dry-run] " if not result.executed else "") + result.detail)
    _print_json(result.to_dict())
    return EXIT_OK


def cmd_move(args) -> int:
    config = load_config()
    win = _find_game_window_or_fail(config, args.window)
    result = controller.move_image(
        win, args.x, args.y,
        dry_run=args.dry_run,
        move_duration=float(config.get("control", {}).get("move_duration", 0.35)),
    )
    _info(("[dry-run] " if not result.executed else "") + result.detail)
    _print_json(result.to_dict())
    return EXIT_OK


def cmd_key(args) -> int:
    config = load_config()
    win = _find_game_window_or_fail(config, args.window)
    result = controller.press_key(win, args.name, times=args.times, dry_run=args.dry_run)
    _info(("[dry-run] " if not result.executed else "") + result.detail)
    _print_json(result.to_dict())
    return EXIT_OK


def cmd_type(args) -> int:
    config = load_config()
    win = _find_game_window_or_fail(config, args.window)
    result = controller.type_text(win, args.text, dry_run=args.dry_run)
    _info(("[dry-run] " if not result.executed else "") + result.detail)
    _print_json(result.to_dict())
    return EXIT_OK


# ---------------------------------------------------------------- 主循环命令


def cmd_step(args) -> int:
    config = load_config()
    if args.live:
        _info("警告：--live 模式将真实操作鼠标与键盘（或直调游戏 API）")
    win = _find_game_window_or_fail(config, args.window) if args.window else None
    record = step_once(
        config, live=args.live, mock=args.mock, tag="step",
        window=win,
        window_keywords=[args.window] if args.window else None,
        brain_mock_file=args.brain_mock_file,
        push_overlay=not args.no_overlay,
        source=args.source,
    )
    decision = record.get("decision") or {}
    execution = record.get("execution") or {}
    _info(f"观察通道：{record.get('source')}")
    if record.get("screenshot"):
        _info(f"截图：{record.get('screenshot')}")
    _info(
        f"决策：{decision.get('action')} {json.dumps(decision.get('params') or {}, ensure_ascii=False)}"
        f" ｜ {decision.get('reason')}（confidence={decision.get('confidence')}）"
    )
    _info(f"执行：{execution.get('detail')}")
    if getattr(args, "no_overlay", False):
        _info("浮层推送：已禁用（--no-overlay）")
    else:
        _info(f"浮层推送：{'成功' if record.get('overlay_pushed') else '跳过（游戏未运行/插件未加载）'}")
    if record.get("step_file"):
        _info(f"步骤记录：{record['step_file']}")
    _print_json(record)
    return EXIT_OK


def cmd_run(args) -> int:
    config = load_config()
    if args.live:
        _info("警告：--live 模式将真实操作鼠标与键盘（或直调游戏 API，连续步进）")
    win = _find_game_window_or_fail(config, args.window) if args.window else None
    if args.live and args.no_mouse:
        _info("禁鼠标模式：鼠标/键盘动作将被拦截，只执行游戏 API 直调动作")
    if args.event_driven:
        _info("事件驱动模式：状态无变化时等待，不空转决策（心跳兜底见 structured.event_idle_limit）")
    steps = run_loop(
        config,
        max_steps=None if args.max_steps == 0 else args.max_steps,
        live=args.live,
        mock=args.mock,
        delay_between=args.delay,
        window=win,
        window_keywords=[args.window] if args.window else None,
        brain_mock_file=args.brain_mock_file,
        push_overlay=not args.no_overlay,
        source=args.source,
        event_driven=args.event_driven,
        allow_mouse=not args.no_mouse,
    )
    for item in steps:
        if item.get("error"):
            _info(f"[{item.get('step')}] 错误：{item['error']}")
        elif item.get("stopped"):
            _info(f"[{item.get('step')}] 停止：{item['detail']}")
        else:
            decision = item.get("decision") or {}
            execution = item.get("execution") or {}
            _info(
                f"[{item.get('step', '?')}][{item.get('source', '?')}] "
                f"{decision.get('action')} → {execution.get('detail')}"
            )
    _print_json({"steps_count": len(steps), "steps": steps})
    return EXIT_OK


# ---------------------------------------------------------------- 机制知识命令


def _knowledge_dir(config):
    """解析机制卡目录（相对路径按项目根目录）。"""
    from pathlib import Path

    from game_agent.config import project_root

    cfg = config.get("knowledge") or {}
    path = Path(str(cfg.get("dir") or "knowledge/mechanics"))
    return path if path.is_absolute() else project_root() / path


def cmd_knowledge(args) -> int:
    """机制知识库：列出 / 查看 / 校验 / 按当前局面检索。"""
    from game_agent.knowledge.cards import MechanicError, load_cards
    from game_agent.knowledge.router import MechanicRouter

    config = load_config()
    directory = _knowledge_dir(config)
    try:
        cards = load_cards(directory)
    except MechanicError as exc:
        _info(f"[机制卡校验失败] {exc}")
        return EXIT_UNKNOWN

    if args.id:
        card = cards.get(args.id)
        if card is None:
            _info(f"未找到机制卡：{args.id}（共 {len(cards)} 张，用 --list 查看）")
            return EXIT_UNKNOWN
        _print_json(card.to_dict())
        return EXIT_OK

    if args.select:
        source = pick_source(config)
        if source not in (CHANNEL_BRIDGE, CHANNEL_STATEWATCH):
            _info("按局面检索需要结构化通道（bridge / statewatch）——请确认游戏正在运行")
            return EXIT_NO_DATA
        record = StructuredObserver(config).observe(source, save=False)
        cfg = config.get("knowledge") or {}
        router = MechanicRouter(
            cards,
            max_cards=int(cfg.get("max_cards") or 3),
            max_chars=int(cfg.get("max_chars") or 1800),
            min_score=float(cfg.get("min_score") or 3.0),
        )
        block, selected = router.block(
            record.observation,
            extra_text=str((config.get("goal") or {}).get("label") or ""),
        )
        _info(f"机制卡 {len(cards)} 张，命中 {len(selected)} 张：{', '.join(selected) or '（无）'}")
        _print_json({
            "source": source,
            "directory": str(directory),
            "selected": selected,
            "block": block,
            "coverage": router.coverage(),
        })
        return EXIT_OK

    # 默认：列出全部卡片（--validate 也走这里，加载成功即校验通过）
    rows = [
        {
            "id": card.id,
            "title": card.title,
            "category": card.category,
            "confidence": card.confidence,
            "verified_at": card.verified_at,
            "facts": len(card.facts),
        }
        for card in sorted(cards.values(), key=lambda c: (c.category, c.id))
    ]
    coverage: dict[str, int] = {}
    for card in cards.values():
        coverage[card.category] = coverage.get(card.category, 0) + 1
    _info(f"机制卡目录：{directory}")
    _info(f"共 {len(cards)} 张，分类分布：{dict(sorted(coverage.items()))}")
    _print_json({
        "directory": str(directory),
        "valid": True,
        "count": len(cards),
        "coverage": dict(sorted(coverage.items())),
        "cards": rows,
    })
    return EXIT_OK


# ---------------------------------------------------------------- 商业目标命令


def cmd_modelconf(args) -> int:
    """查看/修改游戏内模型配置（与游戏里 Ctrl+F9 面板写同一份 ini）。

    为什么要留这条命令行：面板要在游戏里点，脚本化切换（比如临时换成便宜的模型跑批）
    用命令更快；两者写的是同一个文件，不会出现"面板改了但脚本还读旧值"。
    """
    config = load_config()
    ini = config.get("_model_ini") or {}
    ini_path = ini.get("path") or ""
    effective = config.get("brain", {})

    wants_change = any(
        v is not None
        for v in (args.base_url, args.model, args.api_key, args.max_tokens, args.temperature)
    )

    client = _bridge_client()
    try:
        if args.reset:
            result = client.reset_model_config()
        elif wants_change:
            result = client.set_model_config(
                base_url=args.base_url,
                model=args.model,
                api_key=args.api_key,
                max_tokens=str(args.max_tokens) if args.max_tokens is not None else None,
                temperature=str(args.temperature) if args.temperature is not None else None,
            )
        else:
            result = client.get_model_config()
    except BridgeError as exc:
        _info(f"桥接不可用：{exc}")
        _info("（游戏没运行也能看本地生效值，见下）")
        result = None

    failed = False
    if result is not None:
        if not result.get("ok"):
            # 桥在跑但路由不存在（旧版 mod）时，仍然把本地生效值打出来——
            # 否则用户只看到一句"未知路由"，不知道现在到底用的哪个模型。
            _info(f"操作失败：{result.get('error')}")
            failed = True
        else:
            _info("模型配置：")
            _info(f"  文件      {result.get('file')}（exists={result.get('exists')}）")
            _info(f"  base_url  {result.get('base_url') or '（沿用配置文件）'}")
            _info(f"  model     {result.get('model') or '（沿用配置文件）'}")
            _info(f"  api_key   {'已设置' if result.get('api_key_set') else '（沿用 .env）'}")
            _info(f"  max_tok   {result.get('max_tokens')}")
            _info(f"  temp      {result.get('temperature')}")

    _info("当前实际生效（Python 侧叠加后）：")
    _info(f"  base_url  {effective.get('base_url') or '（空）'}")
    _info(f"  model     {effective.get('model') or '（空）'}")
    _info(f"  ini 路径  {ini_path or '（未找到 model_config.ini）'}")
    return EXIT_NO_DATA if failed else EXIT_OK


def _goal_default_label() -> str:
    """商业目标的默认口径描述，从 ``goal.DEFAULT_GOAL`` 派生，不写死数字。

    历史教训：cli.py 曾把「十日内净赚 1000」写死两处（:496 docstring、:831 help），
    R5 把 ``goal.py`` 的 DEFAULT_GOAL 改成 30 天/3000 后，这两处成了旧口径残留，
    用户看到的帮助与实际判定不一致。改成派生后，口径只有一个真相。
    """
    return str(DEFAULT_GOAL.get("label") or "商业目标")


def cmd_goal(args) -> int:
    """查看/重置商业目标进度（口径取自 ``goal.DEFAULT_GOAL``）。"""
    config = load_config()
    goal = BusinessGoal(config)
    if args.reset:
        goal.reset()
        _info("目标进度已重置：下次观察将重新锚定期初净值")

    source = pick_source(config)
    if source not in (CHANNEL_BRIDGE, CHANNEL_STATEWATCH):
        _info("目标进度需要结构化通道（bridge / statewatch）——请确认游戏正在运行，"
              "或至少存在可读存档")
        return EXIT_NO_DATA
    record = StructuredObserver(config).observe(source, save=False)
    progress = goal.update(record.observation)
    _info(
        f"目标：{progress.label} ｜ 第 {progress.day if progress.day is not None else '?'} 天"
        f"（周期第 {progress.elapsed_days + 1}/{progress.window_days} 天）"
    )
    _info(
        f"现金 {progress.start_cash:,.0f} → {progress.cash:,.0f}"
        f"（{progress.profit:+,.0f} / {progress.target_profit:,}，"
        f"完成 {progress.pct * 100:.1f}%，剩余 {progress.days_left} 天，"
        f"日均需 +{progress.required_daily:,.0f}）｜ 库存估值 {progress.stock_value:,.0f}"
    )
    _print_json({
        "source": source,
        "progress": progress.to_dict(),
        "prompt_block": goal.render(progress),
    })
    return EXIT_OK


# ---------------------------------------------------------------- 状态监听命令


def _watch_emit_once(update: WatchUpdate, *, events_only: bool, as_json: bool) -> None:
    """单次采集的输出：stderr 人读摘要 + stdout 完整 JSON。"""
    state = update.state
    snapshot = state.snapshot
    if not as_json:
        if not events_only:
            if snapshot is not None:
                _info(
                    f"存档：{Path(snapshot.path).name}（槽位 {snapshot.slot}，"
                    f"{snapshot.age_seconds:.0f} 秒前更新）"
                )
                _info(
                    f"现金：{snapshot.cash} ｜ 经营天数：{snapshot.day} ｜ "
                    f"租金：{snapshot.day_until_rent} 天后 {snapshot.rent_value} ｜ "
                    f"电力：{'开' if snapshot.is_power_on else '关'}"
                )
                counts = {k: len(v) for k, v in snapshot.inventories.items() if v}
                _info(f"库存件数：{counts}")
            else:
                _info("（未找到存档快照）")
            client = state.current_client()
            _info(f"当前顾客：{client if client else '无'}")
        _info(f"事件 {len(update.new_events)} 条：")
        for event in update.new_events:
            _info(f"  [{event.ts}] {event.kind:<24} {event.message}")

    payload = {
        "snapshot": snapshot.to_dict() if snapshot is not None else None,
        "client": state.current_client(),
        "events": [e.to_dict() for e in update.new_events],
    }
    if events_only:
        payload = {"events": payload["events"]}
    _print_json(payload)


def _cmd_mcp(args) -> int:
    """启动 MCP 工具服务（docs/28）。服务本身阻塞直到 Ctrl+C。"""
    conn_file = args.connection_file
    if conn_file is None:
        conn_file = str(artifacts_dir(load_config()) / "mcp.connection.json")
    return mcp_main(["--port", str(args.port), "--connection-file", conn_file]
                    + (["--bridge-url", args.bridge_url] if args.bridge_url else []))


def cmd_watch(args) -> int:
    config = load_config()
    data_dir = resolve_data_dir(config, override=args.data_dir)
    if data_dir is None or not data_dir.is_dir():
        _info(
            f"未找到游戏数据目录：{data_dir}。\n"
            "请确认游戏至少运行过一次（会生成日志/存档），或用 --data-dir 指定目录。\n"
            "本游戏默认目录：%USERPROFILE%/AppData/LocalLow/Questing Goose Studio/Probably Stolen"
        )
        return EXIT_NO_DATA

    statewatch_cfg = config.get("statewatch", {})
    interval = args.interval if args.interval is not None else float(statewatch_cfg.get("poll_interval", 0.5))
    watcher = StateWatcher(
        data_dir,
        interval=interval,
        start_at_end=not args.from_start,
        max_events=int(statewatch_cfg.get("max_events", 500)),
    )

    # —— 单次采集模式 ——
    if args.once:
        update = watcher.collect_once(history_lines=args.history, from_start=args.from_start)
        _watch_emit_once(update, events_only=args.events_only, as_json=args.json)
        return EXIT_OK

    # —— 实时监听模式 ——
    snapshot = watcher.builder.snapshot
    if not args.json:
        _info(f"监听目录：{data_dir}")
        _info(f"轮询间隔：{interval}s ｜ Ctrl+C 停止" + (f" ｜ {args.duration}s 后自动退出" if args.duration else ""))
        if snapshot is not None and not args.events_only:
            _info(f"当前快照：现金={snapshot.cash} ｜ day={snapshot.day} ｜ 槽位={snapshot.slot}")

    def on_update(update: WatchUpdate) -> None:
        if args.json:
            payload = {
                "type": "update",
                "snapshot_updated": update.snapshot_updated,
                "events": [e.to_dict() for e in update.new_events],
                "summary": update.state.summary(),
            }
            print(json.dumps(payload, ensure_ascii=False), flush=True)
            return
        for event in update.new_events:
            _info(f"[{event.ts}] {event.kind:<24} {event.message}")
        if update.snapshot_updated and update.state.snapshot is not None and not args.events_only:
            snap = update.state.snapshot
            _info(f"[watch] 快照已更新（{Path(snap.path).name}）：现金={snap.cash} ｜ day={snap.day}")

    watcher.run(on_update=on_update, duration=args.duration)
    if not args.json:
        _info("已停止监听")
    return EXIT_OK


# ---------------------------------------------------------------- 桥接命令（BepInEx 直调 + 决策浮层）


def _bridge_client() -> BridgeClient:
    return BridgeClient(
        base_url=(load_config().get("bridge", {}) or {}).get("base_url"),
        timeout=(load_config().get("bridge", {}) or {}).get("timeout_seconds"),
    )


def cmd_bridge_state(args) -> int:
    client = _bridge_client()
    _print_json(client.get_state())
    return EXIT_OK


def cmd_bridge_decision(args) -> int:
    client = _bridge_client()
    _print_json(client.get_decision())
    return EXIT_OK


def cmd_bridge_buttons(args) -> int:
    """枚举当前界面全部按钮（v0.9.0「UI 按钮化」盘点/定位用）。"""
    client = _bridge_client()
    _print_json(client.get_ui_buttons())
    return EXIT_OK


def cmd_bridge_action(args) -> int:
    client = _bridge_client()
    params = {}
    if args.index is not None:
        params["index"] = args.index
    if getattr(args, "identifier", None):
        params["identifier"] = args.identifier
    # v0.8.0 新动作参数（按动作取用，未提供的忽略）
    for attr, key in (("service", "service"), ("destination", "destination"),
                      ("number", "number"), ("contact", "contact"),
                      ("vendor", "vendor")):
        value = getattr(args, attr, None)
        if value:
            params[key] = value
    # 布尔参数（字符串转 bool）
    for attr, key in (("enable", "enable"), ("show", "show")):
        value = getattr(args, attr, None)
        if value is not None:
            params[key] = str(value).strip().lower() in ("1", "true", "yes", "on")
    # 通用 key=value 参数（数字自动转 int）
    for item in getattr(args, "param", None) or []:
        if "=" not in item:
            continue
        key, _, value = item.partition("=")
        key = key.strip()
        if not key:
            continue
        if value.lstrip("-").isdigit():
            params[key] = int(value)
        else:
            params[key] = value
    result = client.post_action(args.action, **params)
    if not result.get("ok"):
        _info(f"[桥接拒绝] {result.get('error')}")
        return EXIT_BRIDGE
    _print_json(result)
    return EXIT_OK


def cmd_bridge_push(args) -> int:
    """手动推送一条决策到游戏内浮层（测试/演示用）。"""
    client = _bridge_client()
    decision = {
        "action": args.action,
        "reason": args.reason or "",
        "confidence": args.confidence,
    }
    if args.params_text:
        decision["params"] = {"text": args.params_text}
    result = client.post_decision(
        decision, step=args.step, params_text=args.params_text
    )
    if not result.get("ok"):
        _info(f"[桥接拒绝] {result.get('error')}")
        return EXIT_BRIDGE
    _print_json(result)
    return EXIT_OK


# ---------------------------------------------------------------- 参数解析


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="game_agent",
        description="深空当铺 AI Agent —— 视觉观察 + 鼠标键盘控制 + 决策主循环",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_list = sub.add_parser("list-windows", help="列出当前所有可见窗口（排查窗口标题）")
    p_list.set_defaults(func=cmd_list_windows)

    p_cap = sub.add_parser("capture", help="捕获截图（不做视觉分析）")
    p_cap.add_argument("--window", help="窗口标题关键词（默认为配置中的游戏关键词）")
    p_cap.add_argument("--fullscreen", action="store_true", help="抓取全屏（自检用途）")
    p_cap.add_argument("--tag", help="截图文件名标签")
    p_cap.set_defaults(func=cmd_capture)

    p_obs = sub.add_parser(
        "observe",
        help="完整观察：截图 + 视觉分析 → 结构化状态（--source 可改走结构化直读）",
    )
    p_obs.add_argument("--window", help="窗口标题关键词（默认为配置中的游戏关键词）")
    p_obs.add_argument("--fullscreen", action="store_true", help="抓取全屏（自检用途）")
    p_obs.add_argument("--mock", action="store_true", help="离线假客户端，不调用任何 API")
    p_obs.add_argument("--mock-file", help="mock 模式下改用自定义样例 JSON 文件")
    p_obs.add_argument("--tag", help="截图文件名标签")
    p_obs.add_argument(
        "--source",
        choices=["auto", "bridge", "statewatch", "vision"],
        default="auto",
        help="观察通道：auto（默认，按优先级自动选择）/ bridge（游戏内桥接直读）/"
             "statewatch（日志+存档直读）/ vision（截图+视觉模型）",
    )
    p_obs.set_defaults(func=cmd_observe)

    p_click = sub.add_parser("click", help="点击游戏窗口（截图坐标系；--preview 可先目视确认）")
    p_click.add_argument("--x", type=int, required=True, help="截图坐标 x")
    p_click.add_argument("--y", type=int, required=True, help="截图坐标 y")
    p_click.add_argument("--window", help="窗口标题关键词（默认用配置中的游戏关键词）")
    p_click.add_argument("--preview", action="store_true", help="只生成带标记的预览图，不点击")
    p_click.add_argument("--dry-run", action="store_true", help="只校验不执行")
    p_click.set_defaults(func=cmd_click)

    p_move = sub.add_parser("move", help="移动鼠标到截图坐标（不点击）")
    p_move.add_argument("--x", type=int, required=True, help="截图坐标 x")
    p_move.add_argument("--y", type=int, required=True, help="截图坐标 y")
    p_move.add_argument("--window", help="窗口标题关键词（默认用配置中的游戏关键词）")
    p_move.add_argument("--dry-run", action="store_true", help="只校验不执行")
    p_move.set_defaults(func=cmd_move)

    p_key = sub.add_parser("key", help="向游戏窗口发送按键")
    p_key.add_argument("--name", required=True, help="按键名（escape/enter/space/up/down/字母/数字/f1..f12）")
    p_key.add_argument("--times", type=int, default=1, help="按下次数（默认 1）")
    p_key.add_argument("--window", help="窗口标题关键词（默认用配置中的游戏关键词）")
    p_key.add_argument("--dry-run", action="store_true", help="只校验不执行")
    p_key.set_defaults(func=cmd_key)

    p_type = sub.add_parser("type", help="向游戏窗口输入 ASCII 文本")
    p_type.add_argument("--text", required=True, help="要输入的文本（仅 ASCII）")
    p_type.add_argument("--window", help="窗口标题关键词（默认用配置中的游戏关键词）")
    p_type.add_argument("--dry-run", action="store_true", help="只校验不执行")
    p_type.set_defaults(func=cmd_type)

    p_step = sub.add_parser("step", help="执行一个完整步骤：观察 → 决策 → 执行（默认 dry-run）")
    p_step.add_argument("--live", action="store_true", help="真实执行鼠标键盘操作（默认只 dry-run）")
    p_step.add_argument("--mock", action="store_true", help="观察与决策均用离线假客户端（不调用 API）")
    p_step.add_argument("--window", help="窗口标题关键词（默认用配置中的游戏关键词）")
    p_step.add_argument("--brain-mock-file", help="决策的离线样例 JSON 文件（配合 --mock 使用）")
    p_step.add_argument("--no-overlay", action="store_true", help="不推送决策到游戏内浮层")
    p_step.add_argument(
        "--source",
        choices=["auto", "bridge", "statewatch", "vision"],
        default="auto",
        help="观察通道（默认 auto：桥接 → 状态监听 → 视觉，按优先级自动选择）",
    )
    p_step.set_defaults(func=cmd_step)

    p_run = sub.add_parser("run", help="连续步进「观察 → 决策 → 执行」（默认 dry-run）")
    p_run.add_argument(
        "--max-steps", type=int, default=0,
        help="步数上限（默认不设上限）；传正数可限制，0/负数表示不设上限"
             "（靠停止按钮 / STOP 文件 / 模型 stop 结束）",
    )
    p_run.add_argument("--delay", type=float, default=0.4, help="步骤间隔秒数（默认 0.4）")
    p_run.add_argument("--live", action="store_true", help="真实执行鼠标键盘操作（默认只 dry-run）")
    p_run.add_argument("--mock", action="store_true", help="观察与决策均用离线假客户端（不调用 API）")
    p_run.add_argument("--window", help="窗口标题关键词（默认用配置中的游戏关键词）")
    p_run.add_argument("--brain-mock-file", help="决策的离线样例 JSON 文件（配合 --mock 使用）")
    p_run.add_argument("--no-overlay", action="store_true", help="不推送决策到游戏内浮层")
    p_run.add_argument(
        "--source",
        choices=["auto", "bridge", "statewatch", "vision"],
        default="auto",
        help="观察通道（默认 auto：桥接 → 状态监听 → 视觉，按优先级自动选择）",
    )
    p_run.add_argument(
        "--event-driven",
        action="store_true",
        help="事件驱动：结构化通道下状态无变化则等待，不空转决策（心跳兜底）",
    )
    p_run.add_argument(
        "--no-mouse",
        action="store_true",
        help="禁鼠标模式：live 下也拦截一切鼠标键盘输入，只执行游戏 API 直调动作",
    )
    p_run.set_defaults(func=cmd_run)

    p_know = sub.add_parser(
        "knowledge",
        help="机制知识库：--list 列出 / --id 查看 / --validate 校验 / --select 按当前局面检索",
    )
    p_know.add_argument("--list", action="store_true", help="列出全部机制卡（默认行为）")
    p_know.add_argument("--id", help="查看指定 id 的完整卡片")
    p_know.add_argument("--validate", action="store_true", help="校验全部卡片（加载失败即报错）")
    p_know.add_argument("--select", action="store_true", help="用实时局面检索命中的卡片与提示词块")
    p_know.set_defaults(func=cmd_knowledge)

    p_goal = sub.add_parser(
        "goal",
        help=f"查看商业目标进度：{_goal_default_label()} ｜ 净值/天数/日均缺口",
    )
    p_goal.add_argument("--reset", action="store_true", help="重置进度，重新锚定期初净值")
    p_goal.set_defaults(func=cmd_goal)

    # docs/28：标准 MCP 工具层（Streamable HTTP / JSON-RPC 2.0），供任意 AI 客户端接入。
    p_mcp = sub.add_parser(
        "mcp",
        help="启动 MCP 工具服务（127.0.0.1，供外部 AI 客户端接入；需游戏与 8965 桥已运行）",
    )
    p_mcp.add_argument("--port", type=int, default=28739, help="监听端口（默认 28739）")
    p_mcp.add_argument(
        "--connection-file", default=None,
        help="连接信息（url + token）路径；默认 artifacts/mcp.connection.json",
    )
    p_mcp.add_argument("--bridge-url", default=None, help="桥接地址，默认 http://127.0.0.1:8965")
    p_mcp.set_defaults(func=_cmd_mcp)

    p_watch = sub.add_parser(
        "watch",
        help="监听游戏底层数据（日志事件流 + 存档快照），替代视觉观察",
    )
    p_watch.add_argument("--data-dir", help="游戏持久化目录（默认按配置/自动推导）")
    p_watch.add_argument("--once", action="store_true", help="单次采集后退出（不进入轮询）")
    p_watch.add_argument("--from-start", action="store_true", help="读取日志文件全部内容（离线回放）")
    p_watch.add_argument("--history", type=int, default=30, help="单次采集时回眸的日志行数（默认 30）")
    p_watch.add_argument("--interval", type=float, default=None, help="轮询间隔秒数（默认取配置 0.5）")
    p_watch.add_argument("--duration", type=float, default=None, help="最长运行秒数（默认不限，Ctrl+C 停止）")
    p_watch.add_argument("--events-only", action="store_true", help="只输出事件流（不带快照摘要）")
    p_watch.add_argument("--json", action="store_true", help="机器可读输出（单次=JSON，实时=NDJSON）")
    p_watch.set_defaults(func=cmd_watch)

    p_model = sub.add_parser(
        "modelconf",
        help="查看/修改游戏内模型配置（与 Ctrl+F9 面板同一份 ini）",
    )
    p_model.add_argument("--base-url", help="OpenAI 兼容根地址，如 https://api.example.com/v1")
    p_model.add_argument("--model", help="模型名，如 gpt-4o-mini")
    p_model.add_argument("--api-key", help="API Key（留空=沿用 .env）")
    p_model.add_argument("--max-tokens", type=int, help="单次回复最大 token")
    p_model.add_argument("--temperature", type=float, help="温度（0~1）")
    p_model.add_argument("--reset", action="store_true", help="删除 ini，恢复为完全沿用 config/agent.json")
    p_model.set_defaults(func=cmd_modelconf)

    p_bridge = sub.add_parser(
        "bridge",
        help="BepInEx 桥接：直调游戏 API + 游戏内决策浮层（游戏需运行且插件已加载）",
    )
    p_bridge_sub = p_bridge.add_subparsers(dest="bridge_command", required=True)

    p_bs = p_bridge_sub.add_parser("state", help="读内存实时状态（现金/门/顾客/对话）")
    p_bs.set_defaults(func=cmd_bridge_state)

    p_bd = p_bridge_sub.add_parser("decision", help="读回游戏内浮层当前状态")
    p_bd.set_defaults(func=cmd_bridge_decision)

    p_bb = p_bridge_sub.add_parser("buttons", help="枚举当前界面全部按钮（「UI 按钮化」盘点/定位用）")
    p_bb.set_defaults(func=cmd_bridge_buttons)

    p_ba = p_bridge_sub.add_parser("action", help="直调游戏 API（白名单动作）")
    p_ba.add_argument("action", choices=list(ACTIONS), help="动作名")
    p_ba.add_argument("--index", type=int, default=None, help="选项下标（dialog_choose 用）")
    p_ba.add_argument("--identifier", default=None, help="物品标识（place_item 用，如 toothpaste）")
    # v0.8.0 新动作参数
    p_ba.add_argument("--service", default=None, help="夜晚服务 id 或名称（night_service 用，如 POWER_SERVICE）")
    p_ba.add_argument("--enable", default=None, help="true/false（night_service 用，默认 true=选中）")
    p_ba.add_argument("--destination", default=None, help="外出去向（go_out 用：commissary/scavenging/dr_jackson 等；upgrade/升级 是 dr_jackson 的旧名，见 contracts.DEST_ALL）")
    p_ba.add_argument("--number", default=None, help="电话号码（phone_call 用，如 8376）")
    p_ba.add_argument("--contact", default=None, help="联系人名称关键词（phone_call 用，如 王尔德）")
    p_ba.add_argument("--show", default=None, help="true/false（phone_book 用，默认 true=打开）")
    p_ba.add_argument("--vendor", default=None, help="供应商名称关键词（phone_order 用）")
    p_ba.add_argument("--param", action="append", default=None,
                      help="通用参数 key=value（可多次，用于未列出的参数）")
    p_ba.set_defaults(func=cmd_bridge_action)

    p_bp = p_bridge_sub.add_parser("push", help="手动推送一条决策到游戏内浮层（测试用）")
    p_bp.add_argument("--action", required=True, help="动作名（如 click / wait / open_shutter）")
    p_bp.add_argument("--reason", default="", help="决策理由")
    p_bp.add_argument("--confidence", type=float, default=0.9, help="置信度 0~1（默认 0.9）")
    p_bp.add_argument("--step", type=int, default=None, help="步骤序号")
    p_bp.add_argument("--params-text", default=None, help="参数短文本（如 'x=1223 y=716'）")
    p_bp.set_defaults(func=cmd_bridge_push)
    return parser


def main(argv=None) -> int:
    # Windows 控制台中文输出保护（stdout/stderr 强制 UTF-8）
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except Exception:
            pass

    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except WindowNotFoundError as exc:
        _info(f"[未找到窗口] {exc}")
        return EXIT_NO_WINDOW
    except CaptureError as exc:
        _info(f"[截图失败] {exc}")
        return EXIT_CAPTURE
    except AnalyzeError as exc:
        _info(f"[分析失败] {exc}")
        return EXIT_ANALYZE
    except ControlError as exc:
        _info(f"[控制失败] {exc}")
        return EXIT_CONTROL
    except BridgeError as exc:
        _info(f"[桥接失败] {exc}")
        return EXIT_BRIDGE
    except GameAgentError as exc:
        _info(f"[错误] {exc}")
        return EXIT_UNKNOWN


if __name__ == "__main__":
    sys.exit(main())
