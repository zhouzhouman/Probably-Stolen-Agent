"""游戏内模型配置面板（Ctrl+F9）的 ini 读写与叠加。

背景：模型名/地址/Key 原来只能改 config/agent.json 与 .env。
现在游戏里 Ctrl+F9 直接改，保存到 Mods/DeepSpaceBridge/model_config.ini，
Python 侧在 load_config 时叠加到 brain 段。

这些测试盯三件事：
1. ini 解析宽容（注释、空行、坏行都不能炸）；
2. 空值不覆盖（面板留空 = 沿用配置文件）；
3. 面板填的 Key 优先于 .env（用户换了模型通常 Key 一起换）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from game_agent.config import (
    get_api_key,
    load_config,
    read_model_ini,
    resolve_model_ini,
)


def _write_ini(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


class TestReadModelIni:
    def test_parses_key_value_and_ignores_comments(self, tmp_path):
        ini = _write_ini(tmp_path / "m.ini", """
# 注释
; 另一种注释
base_url=https://api.example.com/v1
model=gpt-4o-mini
max_tokens=4000
temperature=0.2
""")
        got = read_model_ini(ini)
        assert got["base_url"] == "https://api.example.com/v1"
        assert got["model"] == "gpt-4o-mini"
        assert got["max_tokens"] == "4000"
        assert got["temperature"] == "0.2"

    def test_missing_file_is_empty_not_error(self, tmp_path):
        assert read_model_ini(tmp_path / "nope.ini") == {}
        assert read_model_ini(None) == {}

    def test_broken_lines_are_skipped(self, tmp_path):
        ini = _write_ini(tmp_path / "m.ini", "没有等号的行\n=空键\nmodel=x\n")
        got = read_model_ini(ini)
        assert got == {"model": "x"}


class TestApplyOverride:
    def test_ini_overrides_brain(self, tmp_path, monkeypatch):
        ini = _write_ini(tmp_path / "m.ini",
                         "base_url=https://custom.example/v1\nmodel=my-model\nmax_tokens=1234\n")
        monkeypatch.setenv("GAME_AGENT_MODEL_INI", str(ini))
        cfg = load_config()
        assert cfg["brain"]["base_url"] == "https://custom.example/v1"
        assert cfg["brain"]["model"] == "my-model"
        assert cfg["brain"]["max_tokens"] == 1234

    def test_empty_value_keeps_original(self, tmp_path, monkeypatch):
        """面板把模型名清空 = 沿用配置文件，不能把 brain 置空。

        自带 baseline：项目默认 agent.json 的 brain 已刻意留空（内置免费模型已删除，
        模型由 Ctrl+F9 面板或用户自填提供），所以不能依赖它的非空默认值。
        """
        cfg_path = tmp_path / "agent.json"
        cfg_path.write_text(json.dumps({
            "brain": {"base_url": "https://baseline.example/v1", "model": "baseline-model"}
        }), encoding="utf-8")
        ini = _write_ini(tmp_path / "m.ini", "base_url=\nmodel=\n")
        monkeypatch.setenv("GAME_AGENT_MODEL_INI", str(ini))
        cfg = load_config(cfg_path)
        assert cfg["brain"]["base_url"] == "https://baseline.example/v1"  # 仍是配置文件里的值
        assert cfg["brain"]["model"] == "baseline-model"

    def test_vision_section_untouched(self, tmp_path, monkeypatch):
        """面板只改决策模型：视觉通道保持原配置，避免把纯文本模型当图像模型用。"""
        ini = _write_ini(tmp_path / "m.ini", "base_url=https://custom.example/v1\n")
        monkeypatch.setenv("GAME_AGENT_MODEL_INI", str(ini))
        cfg = load_config()
        assert cfg["vision"]["base_url"] != "https://custom.example/v1"

    def test_bad_numbers_fall_back(self, tmp_path, monkeypatch):
        ini = _write_ini(tmp_path / "m.ini", "max_tokens=不是数字\ntemperature=abc\n")
        monkeypatch.setenv("GAME_AGENT_MODEL_INI", str(ini))
        cfg = load_config()
        assert isinstance(cfg["brain"]["max_tokens"], int)
        assert isinstance(cfg["brain"]["temperature"], float)


class TestResolveAndKey:
    def test_explicit_config_path_wins(self, tmp_path):
        ini = _write_ini(tmp_path / "m.ini", "model=x\n")
        cfg = {"bridge": {"model_config": str(ini)}}
        assert resolve_model_ini(cfg) == ini

    def test_missing_explicit_path_returns_none(self, tmp_path):
        cfg = {"bridge": {"model_config": str(tmp_path / "nope.ini")}}
        assert resolve_model_ini(cfg) is None

    def test_api_key_from_ini_beats_env(self, tmp_path, monkeypatch):
        ini = _write_ini(tmp_path / "m.ini", "api_key=from-panel\n")
        monkeypatch.setenv("GAME_AGENT_MODEL_INI", str(ini))
        monkeypatch.setenv("BRAIN_API_KEY", "from-dotenv")
        cfg = load_config()
        assert get_api_key(cfg, "brain") == "from-panel"

    def test_api_key_env_still_used_when_ini_blank(self, tmp_path, monkeypatch):
        ini = _write_ini(tmp_path / "m.ini", "api_key=\n")
        monkeypatch.setenv("GAME_AGENT_MODEL_INI", str(ini))
        monkeypatch.setenv("BRAIN_API_KEY", "from-dotenv")
        cfg = load_config()
        assert get_api_key(cfg, "brain") == "from-dotenv"


class TestRuntimeDirLocation:
    """v3.10：运行期文件搬到了 %LOCALAPPDATA%\\DeepSpaceBridge。

    背景：Rev13 扫描器会读取 mod 文件夹里的每个文件，agent_run.log 被守护进程
    占用时直接 "Sharing violation" → 整个 mod 拒绝加载。所以 ini 也搬走了，
    Python 侧必须跟着找新位置，否则"面板改了模型但 agent 还用旧的"。
    """

    def test_finds_ini_in_localappdata(self, tmp_path, monkeypatch):
        monkeypatch.delenv("GAME_AGENT_MODEL_INI", raising=False)
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
        d = tmp_path / "DeepSpaceBridge"
        d.mkdir()
        ini = d / "model_config.ini"
        ini.write_text("model=deepseek-flash\n", encoding="utf-8")

        assert resolve_model_ini({"bridge": {}}) == ini

    def test_env_still_wins_over_localappdata(self, tmp_path, monkeypatch):
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
        local_ini = tmp_path / "DeepSpaceBridge" / "model_config.ini"
        local_ini.parent.mkdir(parents=True)
        local_ini.write_text("model=from-local\n", encoding="utf-8")

        env_ini = _write_ini(tmp_path / "env.ini", "model=from-env\n")
        monkeypatch.setenv("GAME_AGENT_MODEL_INI", str(env_ini))

        assert resolve_model_ini({"bridge": {}}) == env_ini

    def test_reads_values_from_new_location(self, tmp_path, monkeypatch):
        monkeypatch.delenv("GAME_AGENT_MODEL_INI", raising=False)
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
        d = tmp_path / "DeepSpaceBridge"
        d.mkdir()
        (d / "model_config.ini").write_text(
            "base_url=https://api.deepseek.com\nmodel=deepseek-flash\n", encoding="utf-8"
        )
        cfg = load_config()
        assert cfg["brain"]["base_url"] == "https://api.deepseek.com"
        assert cfg["brain"]["model"] == "deepseek-flash"


