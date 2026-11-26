"""控制模块单元测试（全离线；不会产生真实鼠标键盘操作）。"""
import pytest
from PIL import Image

from game_agent.control.controller import (
    click_image,
    move_image,
    save_click_preview,
    screenshot_to_screen,
    validate_point,
)
from game_agent.control.keyboard import resolve_vk, type_text
from game_agent.errors import ControlError
from game_agent.vision.models import WindowInfo


def _fake_window() -> WindowInfo:
    """1000x600 的虚拟窗口（屏幕位置 (100, 50)）。"""
    return WindowInfo(hwnd=123456, title="TestWindow", rect=(100, 50, 1100, 650))


class TestCoordinate:
    def test_screenshot_to_screen_mapping(self):
        win = _fake_window()
        assert screenshot_to_screen(win, 10, 20) == (110, 70)
        assert screenshot_to_screen(win, 0, 0) == (100, 50)

    def test_validate_point_in_bounds(self):
        validate_point(_fake_window(), 999, 599)  # 不抛错即通过

    def test_validate_point_out_of_bounds(self):
        with pytest.raises(ControlError, match="坐标越界"):
            validate_point(_fake_window(), 1000, 10)
        with pytest.raises(ControlError):
            validate_point(_fake_window(), 10, 600)

    def test_validate_point_negative(self):
        with pytest.raises(ControlError):
            validate_point(_fake_window(), -1, 10)


class TestDryRun:
    def test_click_dry_run_does_not_execute(self):
        result = click_image(_fake_window(), 100, 100, dry_run=True)
        assert result.executed is False
        assert result.ok is True
        assert "dry-run" in result.detail

    def test_click_dry_run_out_of_bounds_still_raises(self):
        with pytest.raises(ControlError):
            click_image(_fake_window(), 99999, 100, dry_run=True)

    def test_move_dry_run(self):
        result = move_image(_fake_window(), 500, 300, dry_run=True)
        assert result.executed is False
        assert result.action == "move"


class TestKeyboard:
    def test_resolve_known_keys(self):
        assert resolve_vk("escape") == 0x1B
        assert resolve_vk("ESC") == 0x1B
        assert resolve_vk("a") == ord("A")
        assert resolve_vk("space") == 0x20
        assert resolve_vk("f12") == 0x7B

    def test_resolve_unknown_key_raises(self):
        with pytest.raises(ControlError, match="未知按键"):
            resolve_vk("notakey")

    def test_type_text_rejects_non_ascii_before_typing(self):
        # 中文应在发送任何按键之前直接抛错（不会输入一半）
        with pytest.raises(ControlError, match="ASCII"):
            type_text("中文")


class TestPreview:
    def test_save_click_preview_creates_files(self, tmp_path):
        image_path = tmp_path / "shot.png"
        Image.new("RGB", (400, 300), (60, 80, 120)).save(image_path)
        full, zoom = save_click_preview(image_path, 200, 150, tmp_path / "clicks")
        assert full.exists() and zoom.exists()
        with Image.open(full) as im:  # 显式关闭，避免 ResourceWarning（X03）
            assert im.size == (400, 300)

    def test_save_click_preview_out_of_image_raises(self, tmp_path):
        image_path = tmp_path / "shot.png"
        Image.new("RGB", (400, 300), (60, 80, 120)).save(image_path)
        with pytest.raises(ControlError):
            save_click_preview(image_path, 10000, 10000, tmp_path / "clicks")
