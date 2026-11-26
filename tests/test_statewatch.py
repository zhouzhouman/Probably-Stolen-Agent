"""状态监听模块单元测试（全离线：用 fixtures 中的真实游戏数据样本）。

样本来源（2026-10-13 从本机游戏目录提取）：
- save_sample.es3    —— 真实存档切片（ES3 明文 JSON，含数字键与非法转义）
- gamelog_sample.txt —— 真实运行日志前 260 行（覆盖顾客全生命周期/保存/价格跟踪）
"""
import json
import os
import time
from pathlib import Path

import pytest

from game_agent.statewatch import (
    GameStateBuilder,
    LogTailer,
    StateWatcher,
    build_snapshot,
    derive_current_client,
    find_latest_gamelog,
    find_latest_save,
    list_saves,
    parse_lenient,
    parse_log_line,
    parse_log_lines,
    parse_save,
    read_recent_lines,
    repair_es3_json,
)

FIXTURES = Path(__file__).parent / "fixtures"
SAVE_SAMPLE = FIXTURES / "save_sample.es3"
GAMELOG_SAMPLE = FIXTURES / "gamelog_sample.txt"


# ================================================================ 宽容 JSON


class TestLenientJson:
    def test_standard_json_fast_path(self):
        """标准 JSON 直接解析，不经过修复。"""
        data = parse_lenient('{"a": 1, "b": [1, 2, 3]}')
        assert data == {"a": 1, "b": [1, 2, 3]}

    def test_numeric_keys_repaired(self):
        """数字做对象键（ES3 常见写法）应被补引号后解析成功。"""
        text = '{"PhoneClientDict":{8376:{"name":"Joe"},"x":1}}'
        data = parse_lenient(text)
        assert data["PhoneClientDict"]["8376"]["name"] == "Joe"

    def test_numeric_key_after_comma_and_whitespace(self):
        text = '{"a": 1, "b": {  45 :  "v" }}'
        data = parse_lenient(text)
        assert data["b"]["45"] == "v"

    def test_negative_numeric_keys(self):
        """负数做键（窗口位置缓存等字段，实测于大型存档）。"""
        text = '{"pos":{"950":{"x":1},-103:{"x":2},-102:{"x":3}}}'
        data = parse_lenient(text)
        assert data["pos"]["950"]["x"] == 1
        assert data["pos"]["-103"]["x"] == 2
        assert data["pos"]["-102"]["x"] == 3

    def test_array_numbers_untouched(self):
        """数组中的纯数字不能被当成键处理。"""
        data = parse_lenient('{"arr":[1, 2, 3], "n": 99}')
        assert data["arr"] == [1, 2, 3]
        assert data["n"] == 99

    def test_sentinel_string_with_colon_pattern(self):
        """字符串内 `,123:` 形态的文本必须保持原样（哨兵测试）。"""
        text = '{"s":"x,123:y", "d":{45:1}}'
        data = parse_lenient(text)
        assert data["s"] == "x,123:y"
        assert data["d"]["45"] == 1

    def test_cjk_quote_bad_escape_repaired(self):
        """中文全角引号被加上反斜杠（\\“ \\”）时应清理后解析。"""
        text = '{"n":"\\“泥炭\\”合成肉"}'
        data = parse_lenient(text)
        assert data["n"] == "“泥炭”合成肉"

    def test_legal_escapes_preserved(self):
        """合法转义（\\\\ 与 \\"）不能被破坏。"""
        text = '{"p":"C:\\\\Temp","q":"say \\"hi\\""}'
        data = parse_lenient(text)
        assert data["p"] == "C:\\Temp"
        assert data["q"] == 'say "hi"'

    def test_nested_layer_repair(self):
        """嵌套的「字符串形式的 JSON」可以分层修复（模拟 mainInvJSON）。"""
        outer = '{"inv":"{\\"name\\":\\"\\“泥炭\\”\\"}"}'
        data = parse_lenient(outer)
        inner = parse_lenient(data["inv"])
        assert inner["name"] == "“泥炭”"

    def test_repair_is_string_aware(self):
        """修复器不触碰字符串内部的数字冒号形态。"""
        text = '{"a":"{\\"x\\":123:bad}", "b":{7:1}}'
        # 字符串内是非法的，但修复器只处理 {,/ 之后的数字键（字符串外）
        repaired = repair_es3_json(text)
        assert '"7"' in repaired          # 数字键被修复
        assert '123:bad' in repaired      # 字符串内容原样保留

    def test_invalid_input_raises(self):
        with pytest.raises(ValueError):
            parse_lenient("{definitely not json")
        with pytest.raises(TypeError):
            parse_lenient(None)  # type: ignore[arg-type]


# ================================================================ 日志事件


class TestLogEvents:
    def test_exact_kinds(self):
        assert parse_log_line("[09:19:10] [Log] Arriving").kind == "client_approaching"
        assert parse_log_line("[09:19:12] [Log] Client Arrived").kind == "client_arrived"
        assert parse_log_line("[09:19:54] [Log] Leaving").kind == "client_leaving"
        assert parse_log_line("[09:19:56] [Log] Client Left").kind == "client_left"
        # 注意：原文带感叹号，也应命中（rstrip("!") 归一化）
        assert parse_log_line("[09:19:17] [Log] Conversation Ended!").kind == "conversation_ended"
        assert parse_log_line("[09:19:44] [Log] Business Not Done").kind == "business_not_done"

    def test_client_intent(self):
        ev = parse_log_line("[09:19:12] [Log] wanted2 SELL")
        assert ev.kind == "client_intent"
        assert ev.data == {"client_id": "wanted2", "intent": "sell"}
        ev = parse_log_line("[09:20:29] [Log] pettyThief SELL")
        assert ev.data["client_id"] == "pettyThief"

    def test_game_saved_and_price(self):
        ev = parse_log_line("[09:21:44] [Log] Game Saved in Slot: 15")
        assert ev.kind == "game_saved" and ev.data["slot"] == 15
        ev = parse_log_line("[09:18:36] [Log] tracked price: 101")
        assert ev.kind == "price_tracked" and ev.data["price"] == 101

    def test_client_dialogue_and_save_loaded(self):
        """实测补充：mentor 导师顾客对话、载入存档事件。"""
        ev = parse_log_line("[11:32:39] [Log] mentor DIALOGUE")
        assert ev.kind == "client_dialogue"
        assert ev.data == {"client_id": "mentor"}
        ev = parse_log_line("[11:31:05] [Log] Loaded save slot: 16")
        assert ev.kind == "save_loaded"
        assert ev.data["slot"] == 16

    def test_game_started(self):
        ev = parse_log_line(
            "[22:00:28] [Log] Started From The Main Menu as APPRENTICE with skipIntro=False endlessMode=False"
        )
        assert ev.kind == "game_started"
        assert ev.data == {"profession": "APPRENTICE", "skip_intro": False, "endless_mode": False}

    def test_rich_text_stripped(self):
        ev = parse_log_line("[09:19:10] [Log] <color=orange>ClientFlagUp</color>")
        assert ev.message == "ClientFlagUp"
        assert ev.kind == "other"

    def test_warning_level_and_unknown_message(self):
        ev = parse_log_line("[22:00:28] [Warning] Sprite name 'play_d' not found")
        assert ev.level == "Warning"
        assert ev.kind == "other"

    def test_unparsable_lines(self):
        assert parse_log_line("") is None
        assert parse_log_line("   ") is None
        assert parse_log_line("no timestamp here") is None

    def test_parse_real_gamelog_sample(self):
        """真实日志样本：事件流丰富且关键类型齐全。"""
        lines = GAMELOG_SAMPLE.read_text(encoding="utf-8").splitlines()
        events = parse_log_lines(lines)
        assert len(events) > 150
        kinds = {e.kind for e in events}
        for expected in (
            "client_approaching",
            "client_arrived",
            "client_intent",
            "client_left",
            "conversation_ended",
            "price_tracked",
            "game_saved",
        ):
            assert expected in kinds, f"缺少事件类型 {expected}"
        assert all("<color" not in e.message for e in events)


# ================================================================ 日志跟随


class TestLogTailer:
    def _write_line(self, path: Path, line: str) -> None:
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")

    def test_incremental_read(self, tmp_path):
        log = tmp_path / "GameLog_2026-01-01_10-00-00.txt"
        log.write_text("[10:00:00] [Log] Arriving\n", encoding="utf-8")
        tailer = LogTailer(tmp_path, start_at_end=False)

        events = tailer.poll()
        assert [e.kind for e in events] == ["client_approaching"]

        self._write_line(log, "[10:00:05] [Log] Client Arrived")
        events = tailer.poll()
        assert [e.kind for e in events] == ["client_arrived"]
        assert tailer.poll() == []  # 无新增

    def test_partial_line_buffered(self, tmp_path):
        """不以换行结尾的尾部行暂存，补全后才产出。"""
        log = tmp_path / "GameLog_2026-01-01_10-00-00.txt"
        log.write_text("", encoding="utf-8")
        tailer = LogTailer(tmp_path, start_at_end=False)

        with open(log, "a", encoding="utf-8") as f:
            f.write("[10:00:10] [Log] Client Le")   # 半行
        assert tailer.poll() == []

        with open(log, "a", encoding="utf-8") as f:
            f.write("ft\n")                          # 补全
        events = tailer.poll()
        assert [e.kind for e in events] == ["client_left"]

    def test_start_at_end_skips_history(self, tmp_path):
        log = tmp_path / "GameLog_2026-01-01_10-00-00.txt"
        log.write_text("[10:00:00] [Log] Arriving\n[10:00:01] [Log] Client Arrived\n", encoding="utf-8")
        tailer = LogTailer(tmp_path, start_at_end=True)
        assert tailer.poll() == []  # 历史被跳过

        self._write_line(log, "[10:01:00] [Log] Client Left")
        assert [e.kind for e in tailer.poll()] == ["client_left"]

    def test_switch_to_new_file(self, tmp_path):
        """新启日志出现后自动切换，并从新文件开头读。"""
        old = tmp_path / "GameLog_2026-01-01_10-00-00.txt"
        old.write_text("[10:00:00] [Log] Arriving\n", encoding="utf-8")
        tailer = LogTailer(tmp_path, start_at_end=False)
        assert len(tailer.poll()) == 1

        new = tmp_path / "GameLog_2026-01-01_11-00-00.txt"
        new.write_text("[11:00:00] [Log] Leaving\n[11:00:01] [Log] Client Left\n", encoding="utf-8")
        events = tailer.poll()
        assert [e.kind for e in events] == ["client_leaving", "client_left"]
        assert tailer.current_file == new

    def test_find_latest_and_recent_lines(self, tmp_path):
        (tmp_path / "GameLog_2026-01-01_10-00-00.txt").write_text("old", encoding="utf-8")
        newest = tmp_path / "GameLog_2026-01-02_09-00-00.txt"
        newest.write_text("l1\nl2\nl3\n", encoding="utf-8")
        assert find_latest_gamelog(tmp_path) == newest
        assert read_recent_lines(newest, n=2) == ["l2", "l3"]
        assert find_latest_gamelog(tmp_path / "nonexistent") is None


# ================================================================ 存档解析


class TestSaveParsing:
    def test_parse_real_save_sample(self):
        """真实存档样本：核心字段与库存全部可读。"""
        snap = parse_save(SAVE_SAMPLE)
        assert snap.slot == 16
        assert snap.cash == 550
        assert snap.run_number == 17
        assert snap.day == 1
        assert snap.day_until_rent == 6
        assert snap.rent_value == 200
        assert snap.day_until_loan == 12
        assert snap.is_power_on is True
        assert snap.garbage_level == 0

        # 库存：11 个容器可解析，主库存含中文物品名（全角引号）
        assert snap.inventory_count("main") >= 9
        names = [item.name for item in snap.inventories["main"]]
        assert any("合成肉" in n for n in names)

        # 阵营声望
        assert len(snap.reputations) == 5
        lower = next(r for r in snap.reputations if r["faction_id"] == "FACTION_LOWER_LEVEL")
        assert lower["faction"] == "下层区"

        # to_dict 可序列化
        payload = snap.to_dict()
        json.dumps(payload, ensure_ascii=False)
        assert payload["cash"] == 550

    def test_list_and_find_latest_save(self, tmp_path):
        (tmp_path / "SaveFile.es3").write_text("{}", encoding="utf-8")
        (tmp_path / "saves_index.es3").write_text("{}", encoding="utf-8")
        s3 = tmp_path / "save_3.es3"
        s3.write_text("{}", encoding="utf-8")
        s16 = tmp_path / "save_16.es3"
        s16.write_text("{}", encoding="utf-8")
        now = time.time()
        os.utime(s3, (now - 500, now - 500))
        os.utime(s16, (now, now))

        infos = list_saves(tmp_path)
        assert [i.slot for i in infos] == [3, 16]           # 设置/索引文件被排除
        latest = find_latest_save(tmp_path)
        assert latest is not None and latest.slot == 16
        assert find_latest_save(tmp_path / "nonexistent") is None

    def test_minimal_save_with_quirks(self):
        """构造的最小存档（含数字键 + 非法转义）走完整提取管线。"""
        text = (
            '{"playerStore":{"__type":"PlayerStore,Assembly-CSharp","value":{'
            '"playerCash":123,"saveSlotId":5,"dayUntilRent":3,"rentValue":150,'
            '"isPowerOn":true,"storeReputations":[{"factionId":"F_1","factionDisplay":"测试区","amount":7}],'
            '"mainInvJSON":"{\\"saveItems\\":[{\\"name\\":\\"\\“旧怀表\\”\\",\\"identifier\\":\\"watch\\",\\"unitValue\\":45}]}"'
            '}},"storeStation":{"__type":"StoreStation,Assembly-CSharp","value":{"dayCounter":9,"happiness":70}}}'
        )
        snap = build_snapshot(parse_lenient(text))
        assert snap.cash == 123
        assert snap.day == 9
        assert snap.slot == 5
        assert snap.happiness == 70
        items = snap.inventories["main"]
        assert len(items) == 1 and items[0].name == "“旧怀表”" and items[0].unit_value == 45
        assert snap.reputations == [{"faction_id": "F_1", "faction": "测试区", "amount": 7}]


# ================================================================ 状态组装


class TestStateAssembly:
    def test_derive_current_client_lifecycle(self):
        def ev(line):
            return parse_log_line(line)

        events = [
            ev("[10:00:00] [Log] Arriving"),
            ev("[10:00:02] [Log] Client Arrived"),
            ev("[10:00:02] [Log] wanted2 SELL"),
        ]
        client = derive_current_client(events)
        assert client is not None
        assert client["client_id"] == "wanted2"
        assert client["intent"] == "sell"
        assert client["stage"] == "intent"

        events.append(ev("[10:00:30] [Log] Client Left"))
        assert derive_current_client(events) is None

    def test_derive_client_absent(self):
        assert derive_current_client([]) is None

    def test_builder_snapshot_dedup(self):
        builder = GameStateBuilder()
        snap = build_snapshot(
            {"playerStore": {"__type": "x", "value": {"playerCash": 100}}},
            mtime=12345.0,
        )
        assert builder.update_snapshot(snap) is True
        assert builder.update_snapshot(snap) is False  # 相同 path+mtime 不重复

    def test_state_summary(self):
        builder = GameStateBuilder()
        builder.apply_events([
            parse_log_line("[10:00:00] [Log] Arriving"),
            parse_log_line("[10:00:02] [Log] Client Arrived"),
        ])
        snap = build_snapshot(
            {
                "playerStore": {"__type": "x", "value": {"playerCash": 42}},
                "storeStation": {"__type": "y", "value": {"dayCounter": 3}},
            },
            mtime=0.0,
        )
        builder.update_snapshot(snap)
        state = builder.state()
        summary = state.summary()
        assert summary["cash"] == 42
        assert summary["day"] == 3
        assert summary["client"]["stage"] == "in_store"
        json.dumps(summary, ensure_ascii=False)


# ================================================================ 监听器集成


class TestStateWatcher:
    def _setup_dir(self, tmp_path: Path) -> Path:
        log = tmp_path / "GameLog_2026-01-01_10-00-00.txt"
        log.write_text(
            "[10:00:00] [Log] Arriving\n"
            "[10:00:02] [Log] Client Arrived\n"
            "[10:00:02] [Log] wanted2 SELL\n"
            "[10:00:30] [Log] Client Left\n",
            encoding="utf-8",
        )
        save = tmp_path / "save_5.es3"
        save.write_text(
            '{"playerStore":{"__type":"P","value":{"playerCash":123,"saveSlotId":5}},'
            '"storeStation":{"__type":"S","value":{"dayCounter":2}}}',
            encoding="utf-8",
        )
        return tmp_path

    def test_collect_once(self, tmp_path):
        data_dir = self._setup_dir(tmp_path)
        watcher = StateWatcher(data_dir)
        update = watcher.collect_once(from_start=True)
        assert len(update.new_events) == 4
        assert update.state.snapshot is not None
        assert update.state.snapshot.cash == 123
        assert update.state.snapshot.day == 2

    def test_poll_incremental(self, tmp_path):
        data_dir = self._setup_dir(tmp_path)
        watcher = StateWatcher(data_dir, start_at_end=False)
        # 构造 watcher 时会预加载最新存档
        assert watcher.builder.snapshot is not None
        assert watcher.builder.snapshot.cash == 123

        update = watcher.poll_once()
        assert len(update.new_events) == 4
        assert update.snapshot_updated is False  # 存档没有变化
        assert update.state.current_client() is None  # 末尾事件是 Client Left

        # 追加新事件 + 重写存档
        with open(data_dir / "GameLog_2026-01-01_10-00-00.txt", "a", encoding="utf-8") as f:
            f.write("[10:01:00] [Log] Arriving\n")
        new_save = data_dir / "save_5.es3"
        new_save.write_text(
            '{"playerStore":{"__type":"P","value":{"playerCash":999,"saveSlotId":5}}}',
            encoding="utf-8",
        )
        os.utime(new_save, (time.time() + 5, time.time() + 5))

        update = watcher.poll_once()
        assert [e.kind for e in update.new_events] == ["client_approaching"]
        assert update.snapshot_updated is True
        assert update.state.snapshot.cash == 999

    def test_missing_dir_is_safe(self, tmp_path):
        watcher = StateWatcher(tmp_path / "does_not_exist")
        update = watcher.poll_once()
        assert update.new_events == []
        assert update.snapshot_updated is False
        assert update.state.snapshot is None
