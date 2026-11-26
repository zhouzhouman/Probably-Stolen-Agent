"""机制知识库测试（全离线）。

覆盖：
1. 卡片 schema 校验（分类/触发器算子/必填字段/置信度）；
2. 触发器求值（eq/in/contains/truthy/gte…）；
3. 检索：状态谓词命中优先、字段名不产生假命中、按局面命中正确的卡；
4. 渲染：字符预算、段落截断；
5. 集成：配置关闭时不注入、开启时进入 decide 的 mechanics_block。
"""
from __future__ import annotations

import copy
import json

import pytest

from game_agent.brain.prompt import build_decision_prompt
from game_agent.config import DEFAULT_CONFIG
from game_agent.knowledge.cards import (
    MechanicCard,
    MechanicError,
    evaluate_trigger,
    load_cards,
)
from game_agent.knowledge.router import MechanicRouter, render_cards


def _card(**overrides):
    data = {
        "id": "demo-card",
        "title": "示例机制",
        "category": "trade",
        "facts": ["事实一"],
        "triggers": {"all": [{"field": "visible_info.table.nego_buttons.wholesale",
                              "op": "truthy"}]},
        "keywords": ["AcceptWholesale"],
        "confidence": 0.9,
    }
    data.update(overrides)
    return MechanicCard.from_dict(data, origin="test")


# ---------------------------------------------------------------- schema


class TestSchema:
    def test_minimal_card_ok(self):
        card = _card()
        assert card.id == "demo-card"
        assert card.confidence == 0.9

    @pytest.mark.parametrize("bad", [
        {"id": "Bad ID"},
        {"category": "nope"},
        {"facts": []},
        {"confidence": 1.5},
        {"triggers": {"all": [{"field": "a", "op": "wat"}]}},
        {"triggers": {"all": [{"op": "eq", "value": 1}]}},
    ])
    def test_invalid_cards_rejected(self, bad):
        with pytest.raises(MechanicError):
            _card(**bad)

    def test_repository_cards_are_valid(self):
        from game_agent.config import project_root
        cards = load_cards(project_root() / "knowledge" / "mechanics")
        assert len(cards) >= 8
        # 每张卡必须有事实与证据（证据是知识可维护性的底线）
        for card in cards.values():
            assert card.facts, card.id
            assert card.evidence, card.id


# ---------------------------------------------------------------- triggers


class TestTriggers:
    OBS = {"visible_info": {"client": {"is_wanted": True, "buying_tags": ["FOOD"]},
                            "night": {"is_outside": False},
                            "rent": {"day_until_rent": 2},
                            "table": {"nego_buttons": {"wholesale": True}}}}

    def test_eq(self):
        assert evaluate_trigger({"field": "visible_info.rent.day_until_rent", "op": "eq", "value": 2}, self.OBS)

    def test_contains_list(self):
        assert evaluate_trigger({"field": "visible_info.client.buying_tags", "op": "contains", "value": "FOOD"}, self.OBS)

    def test_truthy_and_falsy(self):
        assert evaluate_trigger({"field": "visible_info.client.is_wanted", "op": "truthy"}, self.OBS)
        assert evaluate_trigger({"field": "visible_info.night.is_outside", "op": "falsy"}, self.OBS)

    def test_gte_lte(self):
        assert evaluate_trigger({"field": "visible_info.rent.day_until_rent", "op": "lte", "value": 3}, self.OBS)
        assert not evaluate_trigger({"field": "visible_info.rent.day_until_rent", "op": "gte", "value": 5}, self.OBS)

    def test_missing_path_is_false(self):
        assert not evaluate_trigger({"field": "visible_info.nope.x", "op": "truthy"}, self.OBS)

    def test_trigger_score_requires_all(self):
        card = _card(triggers={"all": [
            {"field": "visible_info.client.is_wanted", "op": "truthy"},
            {"field": "visible_info.night.is_outside", "op": "truthy"},
        ]})
        assert card.trigger_score(self.OBS) == 0


# ---------------------------------------------------------------- router


class TestRouter:
    def test_selects_wholesale_card_by_state(self):
        from game_agent.config import project_root
        cards = load_cards(project_root() / "knowledge" / "mechanics")
        router = MechanicRouter(cards, max_cards=3)
        obs = {"visible_info": {"table": {"nego_buttons": {"wholesale": True,
                                                          "accept_offer": False},
                                          "wholesale_cost": "162 (-19%)"}}}
        selected = router.select(obs)
        ids = [c.id for c in selected]
        # v3.0 起批发拆成两张卡（方向 + 整批规则），命中任意一张都算对；
        # 类别去重会只保留 trade 里最相关的一张。
        assert ids and any(cid.startswith("wholesale") for cid in ids), ids

    def test_selects_wanted_card(self):
        from game_agent.config import project_root
        cards = load_cards(project_root() / "knowledge" / "mechanics")
        router = MechanicRouter(cards, max_cards=3)
        obs = {"visible_info": {"client": {"identifier": "wanted7", "is_wanted": True,
                                          "sec_button_lit": True}}}
        ids = [c.id for c in router.select(obs)]
        assert "wanted-report" in ids

    def test_field_names_do_not_false_positive(self):
        """关键回归：rent_due 只是字段名（值为 null）时，不能命中租金卡。"""
        from game_agent.config import project_root
        cards = load_cards(project_root() / "knowledge" / "mechanics")
        router = MechanicRouter(cards, max_cards=3)
        obs = {"visible_info": {"rent": {"rent_due": None, "rent_value": 321,
                                        "day_until_rent": 6}}}
        assert router.select(obs) == []

    def test_max_cards_and_budget(self):
        cards = {c.id: c for c in [_card(id=f"c{i}", title=f"卡{i}", confidence=0.9)
                                   for i in range(5)]}
        router = MechanicRouter(cards, max_cards=2)
        obs = {"visible_info": {"table": {"nego_buttons": {"wholesale": True}}}}
        selected = router.select(obs)
        assert len(selected) == 2
        block = router.render(selected)
        assert len(block) <= router.max_chars + 200

    def test_render_respects_char_budget(self):
        cards = [_card(id=f"c{i}", title=f"卡{i}", facts=["很长的事实" * 30]) for i in range(3)]
        text = render_cards(cards, max_chars=400)
        assert len(text) <= 600
        assert text.count("◆") >= 1

    def test_no_match_returns_empty(self):
        router = MechanicRouter({_card().id: _card()}, max_cards=3)
        assert router.select({"visible_info": {"nothing": 1}}) == []


# ---------------------------------------------------------------- 集成


class TestIntegration:
    def test_prompt_includes_block(self):
        prompt = build_decision_prompt(
            {"source": "bridge", "screen_state": "shop_counter",
             "available_actions": [], "important_objects": [],
             "confidence": 1.0, "visible_info": {}},
            bridge_actions=False,
            mechanics_block="【机制知识（测试）】\n◆ 示例",
        )
        assert "【机制知识（测试）】" in prompt

    def test_disabled_config_returns_empty(self, tmp_path):
        from game_agent.knowledge import mechanic_block
        cfg = copy.deepcopy(DEFAULT_CONFIG)
        cfg["knowledge"] = {"enabled": False, "dir": str(tmp_path)}
        block, ids = mechanic_block(cfg, {"visible_info": {}})
        assert block == "" and ids == []

    def test_enabled_config_selects(self, tmp_path):
        from game_agent.knowledge import mechanic_block, reload_router
        kb = tmp_path / "kb"
        kb.mkdir()
        (kb / "card.json").write_text(json.dumps({
            "id": "night-service-cost", "title": "夜间服务扣费", "category": "night",
            "facts": ["is_using 每晚扣费"], "confidence": 0.9,
            "triggers": {"all": [{"field": "visible_info.store_state", "op": "eq",
                                  "value": "CLOSED"}]},
        }, ensure_ascii=False), encoding="utf-8")
        reload_router()
        cfg = copy.deepcopy(DEFAULT_CONFIG)
        cfg["knowledge"] = {"enabled": True, "dir": str(kb), "min_score": 3.0}
        block, ids = mechanic_block(cfg, {"visible_info": {"store_state": "CLOSED"}})
        assert ids == ["night-service-cost"]
        assert "夜间服务扣费" in block


class TestStepIntegration:
    def test_step_once_records_mechanics(self, tmp_path, monkeypatch):
        """开启知识库后，step 记录应带命中卡 id，且 decide 收到提示词块。"""
        from game_agent import agent_loop
        from game_agent.knowledge import reload_router
        from game_agent.structured.observer import StructuredObservation

        kb = tmp_path / "kb"
        kb.mkdir()
        (kb / "night.json").write_text(json.dumps({
            "id": "night-service-cost", "title": "夜间服务扣费", "category": "night",
            "facts": ["is_using 每晚扣费"], "confidence": 0.9,
            "triggers": {"all": [{"field": "visible_info.store_state", "op": "eq",
                                  "value": "CLOSED"}]},
        }, ensure_ascii=False), encoding="utf-8")
        reload_router()

        def _fake_observe(self, source, save=True):
            obs = {"source": "bridge", "screen_state": "shop_counter",
                   "available_actions": [], "important_objects": [],
                   "confidence": 1.0,
                   "visible_info": {"money": 1000, "store_state": "CLOSED",
                                    "day": 74, "inventory": {"owned": [], "not_owned": []},
                                    "client": None, "table": None}}
            return StructuredObservation("bridge", obs, {"in_game": True},
                                         (True, None, "CLOSED", True, 1000, None, None,
                                          None, True, False, None, None, False, "STORE",
                                          False, True, False, 3, 100, False))

        captured = {}

        def _fake_decide(cfg, obs, **kwargs):
            captured["block"] = kwargs.get("mechanics_block")
            return ({"action": "stop", "params": {}, "reason": "t", "confidence": 1.0}, {})

        class _StubClient:
            def get_ui_buttons(self):
                return {"buttons": []}

            def get_state(self):
                return {"in_game": True, "store_state": "CLOSED"}

        monkeypatch.setattr(agent_loop.StructuredObserver, "observe", _fake_observe)
        monkeypatch.setattr(agent_loop, "decide", _fake_decide)
        monkeypatch.setattr(agent_loop, "push_decision_overlay", lambda *a, **k: False)

        cfg = copy.deepcopy(DEFAULT_CONFIG)
        cfg["paths"]["artifacts_dir"] = str(tmp_path / "art")
        cfg["knowledge"] = {"enabled": True, "dir": str(kb), "min_score": 3.0}
        cfg["goal"] = {"enabled": False}

        record = agent_loop.step_once(cfg, source="bridge", save=False,
                                      push_overlay=False, allow_mouse=False,
                                      bridge_client=_StubClient())
        assert record["mechanics"] == ["night-service-cost"]
        assert "夜间服务扣费" in (captured.get("block") or "")
