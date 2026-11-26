"""观察流水线：把「定位窗口 → 截图 → 视觉分析 → 落盘」串成一步。

对上层只暴露 observe() 一个入口，返回 ObservationRecord（含结构化状态与全部溯源信息），
后续的决策模块（brain）与控制器模块（controller）都消费该结构。
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from game_agent.config import artifacts_dir, load_config
from game_agent.errors import WindowNotFoundError
from game_agent.vision.analyzer import analyze_image, make_client
from game_agent.vision.capture import capture_fullscreen, capture_window
from game_agent.vision.models import ObservationRecord
from game_agent.vision.window import find_game_window


def observe(
    config: Optional[dict[str, Any]] = None,
    *,
    window_keywords: Optional[list[str]] = None,
    fullscreen: bool = False,
    mock: bool = False,
    mock_file: Optional[str | Path] = None,
    tag: Optional[str] = None,
    save: bool = True,
) -> ObservationRecord:
    """执行一次完整的「观察」：截图 + 视觉分析 → 结构化游戏状态。

    Args:
        config: 配置字典；None 时自动加载 config/agent.json。
        window_keywords: 自定义窗口匹配关键词；None 时用配置中的默认值。
        fullscreen: True 时抓取全屏（自检用途），默认抓游戏窗口。
        mock: True 时使用离线假客户端（不调用任何 API）。
        mock_file: mock 模式改为加载自定义样例 JSON 文件。
        tag: 截图文件名标签。
        save: 是否把观察记录写入 artifacts/observations/。

    Returns:
        ObservationRecord：结构化状态 + 截图路径 + 溯源信息。

    Raises:
        WindowNotFoundError: 未找到游戏窗口。
        CaptureError: 截图失败（黑屏 / 窗口失效等）。
        AnalyzeError: 视觉分析失败（未配置 / 网络错误 / 输出无法解析）。
    """
    config = config or load_config()
    shots_dir = artifacts_dir(config) / "screenshots"

    # 1) 定位窗口并截图
    if fullscreen:
        capture = capture_fullscreen(shots_dir, tag=tag)
    else:
        game_cfg = config.get("game", {})
        keywords = window_keywords or game_cfg.get("window_title_keywords", [])
        # 显式指定关键词时不做排除，方便对任意窗口做调试
        excludes = [] if window_keywords else game_cfg.get("exclude_title_keywords", [])
        win = find_game_window(
            keywords,
            excludes,
            tuple(game_cfg.get("min_window_size", (400, 300))),
        )
        if win is None:
            raise WindowNotFoundError(
                "未找到游戏窗口。请确认：① 游戏正在运行；② 窗口标题包含关键词 "
                f"{keywords}。注：最小化窗口会被自动恢复，但也请确认游戏是"
                "「窗口化 / 无边框」模式（独占全屏无法截图）。"
                "可执行 `python -m game_agent.cli list-windows` 查看当前所有窗口标题。"
            )
        capture = capture_window(win, shots_dir, tag=tag)

    # 2) 视觉模型分析
    client = make_client(config, force_mock=mock, mock_file=mock_file)
    observation, raw_response, analyze_meta = analyze_image(
        capture.image_path, config, client=client
    )

    record = ObservationRecord(
        observation=observation,
        screenshot=capture.image_path,
        capture=capture.to_dict(),
        meta={
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "analyze": analyze_meta,
            "raw_response": raw_response,  # 保留模型原始输出，便于排查误读
            "mock": bool(mock or mock_file),
        },
    )

    # 3) 落盘（观察记录可审计、可回放）
    if save:
        out_dir = artifacts_dir(config) / "observations"
        record.meta["observation_file"] = str(save_observation(record, out_dir))
    return record


def save_observation(record: ObservationRecord, out_dir: Path) -> Path:
    """把观察记录保存为 JSON 文件（UTF-8，缩进 2），返回文件路径。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
    path = out_dir / f"obs_{timestamp}.json"
    path.write_text(
        json.dumps(record.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return path
