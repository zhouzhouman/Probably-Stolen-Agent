"""v2.3 机器操作接入的回归测试（水商/养殖/熔炉）。"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from game_agent.brain.schema import BRIDGE_ACTION_PARAMS, validate_decision  # noqa: E402
from game_agent.bridge.client import ACTIONS  # noqa: E402
from game_agent.structured.observer import build_observation  # noqa: E402

STATE = {
    "in_game": True, "cash": 15000, "day": 75, "store_state": "OPEN",
    "client": None, "table": None, "inventory": {"owned": [], "not_owned": []},
    "services": [], "night": {}, "phone": {}, "economy": {}, "reputations": [],
    "requests": [], "unlocks": {},
    "machines": [
        {"index": 0, "identifier": "moisture_farm", "name": "水分收集器",
         "slots": [
             {"slot": 0, "items": [{"identifier": "energy_credit", "count": 1, "value": 0}]},
             {"slot": 2, "items": [{"identifier": "water_pitcher", "count": 1, "value": 119}]},
         ]},
        {"index": 1, "identifier": "ras", "name": "循环水养殖系统",
         "slots": [{"slot": 7, "items": [{"identifier": "tilapia", "count": 3, "value": 0}]}]},
        {"index": 2, "identifier": "powerblock", "name": "能量阵列", "slots": []},
    ],
}


class TestWhitelists:
    def test_actions_registered(self):
        assert "machine_put" in ACTIONS and "machine_take" in ACTIONS

    def test_schema_params(self):
        assert BRIDGE_ACTION_PARAMS["machine_take"] == {"machine", "slot", "occurrence"}
        assert BRIDGE_ACTION_PARAMS["machine_put"] == {"machine", "item", "slot", "occurrence"}

    def test_normalize_keeps_machine_params(self):
        out = validate_decision(
            {"action": "machine_take", "params": {"machine": 0, "slot": 2},
             "reason": "取水", "confidence": 0.9},
            extra_actions=BRIDGE_ACTION_PARAMS,
        )
        assert out["action"] == "machine_take"
        assert out["params"]["machine"] == 0 and out["params"]["slot"] == 2


class TestObservation:
    def test_machines_compacted(self):
        info = build_observation(STATE, source="bridge")["visible_info"]
        machines = info["machines"]
        assert [m["id"] for m in machines] == ["moisture_farm", "ras"]
        # 空槽（powerblock）被过滤
        assert machines[0]["i"] == 0
        assert machines[0]["slots"][0]["s"] == 0
        assert machines[0]["slots"][1]["items"][0]["id"] == "water_pitcher"

    def test_machines_absent_degrades(self):
        state = dict(STATE)
        state.pop("machines")
        assert build_observation(state, source="bridge")["visible_info"]["machines"] == []
