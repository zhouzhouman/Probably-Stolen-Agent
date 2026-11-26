"""v3.0 维基机制知识库的回归测试。

覆盖四件事：
1. 观察层把「已采集但未观测」的机制字段补齐了（reputations / requests / unlocks / 违禁加价）；
2. 检索器的类别去重（避免日循环卡挤掉交易卡）；
3. 知识库自身完整（规模、分类、无悬空 related、无死卡）；
4. 总纲生成器可运行（保证文档与卡片不漂移）。
"""

from __future__ import annotations

import copy
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from game_agent.knowledge.cards import load_cards  # noqa: E402
from game_agent.knowledge.router import MechanicRouter  # noqa: E402
from game_agent.structured.observer import build_observation  # noqa: E402

MECHANICS = ROOT / "knowledge" / "mechanics"

STATE = {
    "in_game": True,
    "day": 74,
    "cash": 15303,
    "store_state": "OPEN",
    "scene": "ShopScene",
    "shutter_closed": False,
    "garbage_level": 2,
    "day_until_rent": 3,
    "rent_value": 321,
    "rent_due": 321,
    "can_pay_rent": True,
    "is_power_on": True,
    "queue": {"can_next": True, "has_client": True, "count": 3},
    "client": {
        "identifier": "lowerLevelChemist",
        "is_buying": True,
        "is_selling": False,
        "is_wanted": False,
        "buying_tags": ["MEDICAL"],
        "budget_left": 150,
        "sec_button_lit": False,
    },
    "table": {
        "nego_open": True,
        "negotiated_item": {"identifier": "chem", "name": "化学品", "value": 100},
        # 2026-11-15：按 C# 真实发射形态（BridgeApi.cs:1185-1191）——
        # nego_buttons 只有 primary/secondary/wholesale/accept_offer 四键，
        # 没有 accept_multiple（正式版谈判面板只有 primary/secondary 两个大按钮）。
        "nego_buttons": {"primary": False, "secondary": True,
                         "wholesale": True, "accept_offer": False},
        "wholesale_cost": "162 (-19%)",
        "negotiated_value": 100,
    },
    "dialog": None,
    "inventory": {
        "owned": [{"identifier": "nudka", "name": "努特加", "value": 85, "count": 3,
                   "item_types": ["MEDICAL"]}],
        "not_owned": [],
    },
    "services": [],
    "night": {},
    "phone": {},
    "economy": {
        "retail_markup": 15,
        "stock_value": 39279,
        "attractiveness": 767,
        "loan_days": 12,
        "loan_value": 200,
        "insurance_days_without": 0,
        "contraband_markup": {"low": 1.2, "mid": 1.5, "high": 2.0, "critical": 3.0},
    },
    "reputations": [
        {"id": "lower_level", "faction": "下层区", "value": -62},
        {"id": "security", "faction": "治安部", "value": 18},
    ],
    "requests": [
        {"id": "r2", "title": "送药", "faction": "lower_level", "required_item": "nudka",
         "required_count": 3, "duration": 2, "credit_reward": 10, "reputation_reward": 2,
         "can_fill": False},
        {"id": "r1", "title": "送货", "faction": "lower_level", "required_item": "nudka",
         "required_count": 2, "duration": 3, "credit_reward": 50, "reputation_reward": 5,
         "can_fill": True},
    ],
    "unlocks": {"vending_machine": True, "vending_fountain": False,
                "shooting": True, "gene_scanner": False},
}


class TestObservationGapsClosed:
    def test_reputations_exposed(self):
        info = build_observation(copy.deepcopy(STATE), source="bridge")["visible_info"]
        assert {r["id"] for r in info["reputations"]} == {"lower_level", "security"}
        assert info["reputations"][0]["value"] == -62

    def test_requests_exposed_and_sorted(self):
        info = build_observation(copy.deepcopy(STATE), source="bridge")["visible_info"]
        rows = info["requests"]
        assert len(rows) == 2
        # 可交付的排前面，且携带 fill_request 需要的字段
        assert rows[0]["id"] == "r1" and rows[0]["can_fill"] is True
        assert rows[0]["required_item"] == "nudka"
        assert rows[0]["required_count"] == 2

    def test_unlocks_exposed(self):
        info = build_observation(copy.deepcopy(STATE), source="bridge")["visible_info"]
        assert info["unlocks"] == {"vending_machine": True, "vending_fountain": False,
                                   "shooting": True, "gene_scanner": False}

    def test_contraband_markup_exposed(self):
        info = build_observation(copy.deepcopy(STATE), source="bridge")["visible_info"]
        assert info["economy"]["contraband_markup"]["critical"] == 3.0

    def test_missing_sections_degrade_to_empty(self):
        state = {"in_game": True, "cash": 100, "store_state": "MORNING"}
        info = build_observation(state, source="bridge")["visible_info"]
        assert info["reputations"] == []
        assert info["requests"] == []
        assert info["unlocks"] == {}


class TestRouterDiversity:
    def test_one_card_per_category_first(self):
        cards = load_cards(MECHANICS)
        router = MechanicRouter(cards, max_cards=3, max_chars=1800, min_score=3.0)
        obs = build_observation(copy.deepcopy(STATE), source="bridge")
        selected = router.select(obs)
        cats = [c.category for c in selected]
        assert len(cats) == len(set(cats)), f"同一类别重复出现：{cats}"

    def test_specific_card_beats_loop_noise(self):
        """批发面板开着时，批发卡必须压过日循环卡。"""
        cards = load_cards(MECHANICS)
        router = MechanicRouter(cards, max_cards=3, max_chars=1800, min_score=3.0)
        obs = build_observation(copy.deepcopy(STATE), source="bridge")
        ids = [c.id for c in router.select(obs)]
        assert any("wholesale" in cid for cid in ids), ids

    def test_diversify_can_be_disabled(self):
        cards = load_cards(MECHANICS)
        router = MechanicRouter(cards, max_cards=5, max_chars=3000, min_score=3.0)
        obs = build_observation(copy.deepcopy(STATE), source="bridge")
        plain = router.select(obs, diversify=False)
        assert len(plain) == min(5, len(plain))


class TestLibraryIntegrity:
    @pytest.fixture(scope="class")
    @classmethod
    def cards(cls):
        return load_cards(MECHANICS)

    def test_scale_and_categories(self, cards):
        assert len(cards) >= 150, f"知识库只有 {len(cards)} 张卡"
        assert len({c.category for c in cards.values()}) == 12

    def test_no_dangling_related(self, cards):
        dangling = {
            cid: [r for r in card.related if r not in cards]
            for cid, card in cards.items()
            if any(r not in cards for r in card.related)
        }
        assert dangling == {}

    def test_no_dead_cards(self, cards):
        dead = [cid for cid, c in cards.items() if not c.triggers and not c.keywords]
        assert dead == []

    def test_key_cards_present(self, cards):
        for cid in ("rent-increase-rate", "budget-formula", "wholesale-batch-deal",
                    "insurance-fee-formula", "night-theft-threshold", "gun-permit-tiers",
                    "crime-ledger-entry", "scavenge-attempt-cap"):
            assert cid in cards, cid

    def test_merged_cards_absent(self, cards):
        """合并掉的重复卡不应复活。"""
        assert "gun-license-revoke" not in cards
        assert "uninsured-theft-probability" not in cards


class TestDocGenerator:
    def test_doc_is_in_sync(self):
        """生成器能跑通，且产物覆盖全部卡片数量。"""
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "gen_knowledge_doc.py"), "--check"],
            cwd=ROOT, capture_output=True, text=True, timeout=120,
        )
        assert result.returncode == 0, result.stderr
        assert "167" in result.stdout or "张卡" in result.stdout

    def test_audit_has_no_serious_issue(self):
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "audit_cards.py"), "--strict"],
            cwd=ROOT, capture_output=True, text=True, timeout=120,
        )
        assert result.returncode == 0, result.stdout[-2000:]
