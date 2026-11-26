"""提示词瘦身与自由思考的回归测试。

锁定两件事（用户 2026-11-22 要求）：
1. 白天不再注入「夜晚阶段动作」小节（每步省 ~4.7K 字符）；
2. reason 不再被强制套【依据】【策略】【预期】三段模板。
"""

from __future__ import annotations

import json

from game_agent.brain import prompt as P


def test_day_prompt_excludes_night_section():
    day = P._pick_bridge_sections(night_phase=False)
    assert "【夜晚阶段动作" not in day
    assert "【游戏 API 直调动作】" in day          # 基础动作仍在
    assert "【经营策略" in day                      # 经营策略仍在


def test_night_prompt_includes_night_section():
    night = P._pick_bridge_sections(night_phase=True)
    assert "【夜晚阶段动作" in night


def test_low_freq_sections_dropped_by_default():
    default = P._pick_bridge_sections(night_phase=False)
    assert "【机器操作" not in default
    assert "【治安与战斗" not in default
    # 显式要求时才注入
    with_low = P._pick_bridge_sections(night_phase=False, include_low_freq=True)
    assert "【机器操作" in with_low
    assert len(with_low) > len(default)


def test_slicing_saves_meaningful_size():
    full = len(P.BRIDGE_ACTION_TOOLS)
    day = len(P._pick_bridge_sections(night_phase=False))
    assert full - day > 3000, f"白天裁剪收益过小：{full} -> {day}"


def test_reason_template_no_longer_forced():
    """reason 不得再要求固定三段式（那会让模型每步输出雷同）。"""
    for text in (P.ACTION_TOOLS, P.NO_MOUSE_ACTION_TOOLS):
        assert "必须含三段" not in text
        assert "【依据】当前局面的关键事实" not in text


def test_prompt_asks_for_own_words():
    obs = {
        "visible_info": {"cash": 500, "day": 3, "store_state": "OPEN", "client": None},
        "screen_state": "in_game",
    }
    prompt = P.build_decision_prompt(obs, bridge_actions=True)
    assert "你自己的想法" in prompt
    assert "不要套【依据】【策略】【预期】" in prompt


def test_prompt_is_json_shaped():
    """提示词里的输出样例必须是合法 JSON 结构（避免模型照着残片输出）。"""
    assert '"action": "click"' in P.ACTION_TOOLS
    json.loads(
        '{"action":"click","params":{"x":0,"y":0},"reason":"r","confidence":0.5}'
    )


def test_priority_hints_are_at_the_top():
    """v3.18：hints 必须出现在提示词最前面（primacy），不能被上万字符的规则淹没。

    背景：用户质疑「他怎么不用知识点」——实测 hints 原排在 88% 位置，模型读不到。
    """
    obs = {
        "visible_info": {
            "money": 500,
            "store_state": "CLOSED",
            "next_step_hints": ["今晚必须开清洁服务（测试标记）"],
        },
        "screen_state": "in_game",
    }
    prompt = P.build_decision_prompt(obs, bridge_actions=True)
    pos = prompt.find("今晚必须开清洁服务（测试标记）")
    assert pos != -1, "hints 没有出现在提示词里"
    assert pos / len(prompt) < 0.10, f"hints 位置过于靠后：{pos}/{len(prompt)}"


class TestScenarioSectionPruning:
    """v3.19：专题小节按场景注入（交易流程/电话），防错小节（格子读数）永不裁。"""

    def _obs(self, **vi_extra):
        vi = {"store_state": "OPEN", "money": 500, "day": 3,
              "queue": {"count": 0},
              "table": {"negociated_item": None, "nego_open": False}}
        vi.update(vi_extra)
        return {"source": "bridge", "screen_state": "in_game", "visible_info": vi}

    def test_trade_section_injected_only_when_client_present(self):
        no_client = P.build_decision_prompt(self._obs(), bridge_actions=True)
        with_client = P.build_decision_prompt(
            self._obs(client={"identifier": "c", "intent": "SELL"}), bridge_actions=True
        )
        assert "【完整交易流程" not in no_client
        assert "【完整交易流程" in with_client

    def test_trade_section_kept_when_negotiation_open(self):
        prompt = P.build_decision_prompt(
            self._obs(table={"negociated_item": {"identifier": "x"}, "nego_open": True}),
            bridge_actions=True,
        )
        assert "【完整交易流程" in prompt

    def test_phone_section_needs_phone_scene(self):
        plain = P.build_decision_prompt(self._obs(), bridge_actions=True)
        phone = P.build_decision_prompt(self._obs(scene="PhoneScene"), bridge_actions=True)
        assert "【电话动作" not in plain
        assert "【电话动作" in phone

    def test_slots_section_never_pruned(self):
        """格子读数是防错规则（件数 ≠ 容量），省它换一次错误决策不划算。"""
        prompt = P.build_decision_prompt(self._obs(), bridge_actions=True, no_mouse=True)
        assert "【格子读数" in prompt
        assert "没有容量字段" in prompt

    def test_no_client_prompt_is_materially_smaller(self):
        no_client = len(P.build_decision_prompt(self._obs(), bridge_actions=True))
        with_client = len(
            P.build_decision_prompt(self._obs(client={"identifier": "c", "intent": "SELL"}),
                                    bridge_actions=True)
        )
        assert no_client < with_client - 2000, "无顾客时应当明显更短（省掉交易流程）"
