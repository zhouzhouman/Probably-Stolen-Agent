"""视觉观察模块单元测试（全离线：不依赖真实游戏窗口、不调用外部 API）。"""
import copy
import json

import pytest
from PIL import Image

from game_agent.config import DEFAULT_CONFIG, load_config
from game_agent.errors import AnalyzeError
from game_agent.vision.analyzer import (
    MOCK_SAMPLE,
    MockVisionClient,
    analyze_image,
    build_observation_prompt,
    extract_json,
    make_client,
)
from game_agent.vision.capture import image_stats, is_image_valid, save_image
from game_agent.vision.models import validate_observation
from game_agent.vision.window import WIN32_AVAILABLE, find_game_window, list_windows


def _valid_payload(**overrides):
    payload = {
        "screen_state": "customer_offer",
        "available_actions": ["出价收购"],
        "important_objects": [
            {"name": "机械表", "type": "item", "value": 120, "position": "center"}
        ],
        "confidence": 0.8,
    }
    payload.update(overrides)
    return payload


# ---------------------------------------------------------------- 输出契约校验


class TestValidateObservation:
    def test_valid_payload(self):
        out = validate_observation(_valid_payload())
        assert out["screen_state"] == "customer_offer"
        assert out["available_actions"] == ["出价收购"]
        assert out["important_objects"][0]["name"] == "机械表"
        assert out["confidence"] == 0.8
        assert out["visible_info"] is None

    def test_missing_required_key_raises(self):
        with pytest.raises(ValueError, match="缺少必需字段"):
            validate_observation({"screen_state": "shop_counter"})

    def test_non_dict_raises(self):
        with pytest.raises(ValueError):
            validate_observation(["not", "a", "dict"])

    def test_confidence_clamped_high_and_low(self):
        assert validate_observation(_valid_payload(confidence=3))["confidence"] == 1.0
        assert validate_observation(_valid_payload(confidence=-2))["confidence"] == 0.0
        assert validate_observation(_valid_payload(confidence="abc"))["confidence"] == 0.0

    def test_string_actions_coerced_to_list(self):
        out = validate_observation(_valid_payload(available_actions="拒绝"))
        assert out["available_actions"] == ["拒绝"]

    def test_single_object_coerced_to_list(self):
        out = validate_observation(_valid_payload(important_objects={"name": "x"}))
        assert isinstance(out["important_objects"], list)
        assert out["important_objects"][0]["name"] == "x"

    def test_visible_info_kept_when_dict(self):
        out = validate_observation(_valid_payload(visible_info={"money": "100"}))
        assert out["visible_info"] == {"money": "100"}

    def test_point_normalized_and_invalid_to_none(self):
        out = validate_observation(_valid_payload(important_objects=[
            {"name": "按钮", "type": "ui", "point": [10.4, 20.6]}
        ]))
        assert out["important_objects"][0]["point"] == [10, 21]
        out2 = validate_observation(_valid_payload(important_objects=[
            {"name": "按钮", "type": "ui", "point": "bad"}
        ]))
        assert out2["important_objects"][0]["point"] is None
        out3 = validate_observation(_valid_payload(important_objects=[{"name": "无point"}]))
        assert out3["important_objects"][0]["point"] is None


# ---------------------------------------------------------------- JSON 提取


class TestExtractJson:
    def test_plain_json(self):
        assert extract_json('{"a": 1}')["a"] == 1

    def test_markdown_fenced_json(self):
        text = '```json\n{"screen_state": "loading"}\n```'
        assert extract_json(text)["screen_state"] == "loading"

    def test_noisy_text_around_json(self):
        text = '这是分析结果：{"confidence": 0.5} 希望有帮助'
        assert extract_json(text)["confidence"] == 0.5

    def test_invalid_text_raises(self):
        with pytest.raises(ValueError):
            extract_json("完全不是 JSON 的内容")


# ---------------------------------------------------------------- 客户端与流水线


class TestClients:
    def test_mock_client_output_parses_and_validates(self, tmp_path):
        raw = MockVisionClient().analyze(tmp_path / "x.png", "prompt")
        observation = validate_observation(extract_json(raw))
        assert observation["screen_state"] == MOCK_SAMPLE["screen_state"]
        assert observation["confidence"] == MOCK_SAMPLE["confidence"]

    def test_analyze_image_with_mock_client(self, tmp_path):
        image_path = tmp_path / "shot.png"
        Image.new("RGB", (320, 200), (90, 120, 160)).save(image_path)

        config = copy.deepcopy(DEFAULT_CONFIG)
        config["vision"]["provider"] = "mock"
        observation, raw, meta = analyze_image(image_path, config)

        assert observation["screen_state"] == MOCK_SAMPLE["screen_state"]
        assert meta["provider"] == "mock"
        assert meta["attempts"] == 1
        assert raw.strip().startswith("{")

    def test_make_client_force_mock(self):
        client = make_client(copy.deepcopy(DEFAULT_CONFIG), force_mock=True)
        assert isinstance(client, MockVisionClient)

    def test_make_client_mock_file(self, tmp_path):
        sample_file = tmp_path / "sample.json"
        sample_file.write_text(json.dumps({"screen_state": "market"}), encoding="utf-8")
        client = make_client(copy.deepcopy(DEFAULT_CONFIG), mock_file=sample_file)
        assert isinstance(client, MockVisionClient)
        assert "market" in client.analyze(tmp_path / "x.png", "")

    def test_make_client_missing_config_raises(self):
        config = copy.deepcopy(DEFAULT_CONFIG)
        config["vision"]["provider"] = "openai_compat"
        config["vision"]["base_url"] = ""
        config["vision"]["model"] = ""
        with pytest.raises(AnalyzeError, match="尚未配置视觉模型"):
            make_client(config)

    def test_prompt_contains_contract(self):
        prompt = build_observation_prompt()
        for key in ("screen_state", "available_actions", "important_objects", "confidence"):
            assert key in prompt


# ---------------------------------------------------------------- 图像处理


class TestImageHandling:
    def test_black_image_is_invalid(self):
        assert not is_image_valid(image_stats(Image.new("RGB", (100, 60), (0, 0, 0))))

    def test_solid_color_is_invalid(self):
        assert not is_image_valid(image_stats(Image.new("RGB", (100, 60), (120, 60, 200))))

    def test_gradient_image_is_valid(self):
        gradient = Image.new("L", (160, 90))
        gradient.putdata([(i * 7) % 256 for i in range(160 * 90)])
        assert is_image_valid(image_stats(gradient.convert("RGB")))

    def test_save_image_creates_png(self, tmp_path):
        image = Image.new("RGB", (120, 80), (30, 60, 90))
        path = save_image(image, tmp_path, prefix="t")
        assert path.exists()
        assert path.suffix == ".png"
        with Image.open(path) as im:  # 显式关闭，避免 ResourceWarning（X03）
            assert im.size == (120, 80)


# ---------------------------------------------------------------- 窗口（依赖 Windows，自动跳过）


@pytest.mark.skipif(not WIN32_AVAILABLE, reason="需要 Windows + pywin32")
def test_find_game_window_no_match_returns_none():
    assert find_game_window(["绝对不存在的窗口标题XYZQWE123"], min_size=(10, 10)) is None


@pytest.mark.skipif(not WIN32_AVAILABLE, reason="需要 Windows + pywin32")
def test_list_windows_returns_list():
    windows = list_windows(min_size=(10, 10))
    assert isinstance(windows, list)


# ---------------------------------------------------------------- 配置


class TestConfig:
    def test_load_config_has_required_sections(self):
        config = load_config()
        assert "game" in config
        assert "vision" in config
        assert "paths" in config
        assert isinstance(config["game"]["window_title_keywords"], list)
