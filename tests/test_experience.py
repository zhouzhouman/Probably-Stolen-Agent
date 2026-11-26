"""跨会话经验记忆的单元测试。

盯四件事：
1. 局面特征稳定（同局面不同金额/物品应匹配得上）；
2. 噪声防护（只观测 1 次不注入）；
3. 落盘可跨进程复用（写→新建实例读回）；
4. 坏数据不炸（单行损坏、写盘失败都静默）。
"""

from __future__ import annotations

import json

from game_agent.experience import (
    MIN_SAMPLES,
    Experience,
    ExperienceStore,
    pattern_of,
)


def _obs(cash: int = 500, visible_info: dict | None = None, **kw):
    """构造一个最小观察。cash / visible_info 覆盖默认值。"""
    vi = {
        "store_state": "OPEN",
        "cash": cash,
        "client": {"intent": "SELL"},
        "table": {"negociated_item": None},
        "queue": {"count": 2},
        "night": {"player_location": "IN_STORE", "is_outside": False},
    }
    vi.update(visible_info or {})
    return {"visible_info": vi, **kw}


def test_pattern_is_stable_across_amounts():
    """现金 300 与 500 同属 mid 档，局面应完全一致（否则经验永远命中不了）。"""
    a = pattern_of(_obs(cash=300))
    b = pattern_of(_obs(cash=500))
    assert a == b
    assert a["cash"] == "mid"


def test_pattern_distinguishes_cash_buckets():
    assert pattern_of(_obs(cash=50))["cash"] == "low"
    assert pattern_of(_obs(cash=5000))["cash"] == "high"


def test_pattern_detects_phase_and_intent():
    day = pattern_of(_obs())
    assert day["phase"] == "day" and day["intent"] == "SELL"
    night = pattern_of(_obs(visible_info={"night": {"player_location": "OUTSIDE", "is_outside": True}}))
    assert night["phase"] == "night"


def test_pattern_nego_flag():
    assert pattern_of(_obs())["nego"] == "no"
    on_table = pattern_of(_obs(visible_info={"table": {"negociated_item": {"identifier": "x"}}}))
    assert on_table["nego"] == "yes"


def test_store_persists_across_instances(tmp_path):
    path = tmp_path / "exp.jsonl"
    store = ExperienceStore(path)
    pat = pattern_of(_obs())
    store.add(Experience(pattern=pat, action="sell_item", progressed=True))
    store.add(Experience(pattern=pat, action="sell_item", progressed=False))

    reloaded = ExperienceStore(path)
    total, ok = reloaded.stats(pat, "sell_item")
    assert (total, ok) == (2, 1)


def test_noise_floor_hides_single_observation(tmp_path):
    """只观测 1 次不应进提示词（一次偶然不能钉死结论）。"""
    store = ExperienceStore(tmp_path / "e.jsonl")
    pat = pattern_of(_obs())
    store.add(Experience(pattern=pat, action="buy_item", progressed=False))
    assert store.render(pat) == ""

    for _ in range(MIN_SAMPLES - 1):
        store.add(Experience(pattern=pat, action="buy_item", progressed=False))
    text = store.render(pat)
    assert "buy_item" in text and "全部无效" in text


def test_render_shows_always_useless_first(tmp_path):
    store = ExperienceStore(tmp_path / "e.jsonl")
    pat = pattern_of(_obs())
    for _ in range(3):
        store.add(Experience(pattern=pat, action="sell_item", progressed=True))
        store.add(Experience(pattern=pat, action="buy_item", progressed=False))
    text = store.render(pat)
    # 全无效的动作排在"都有效"之前
    assert text.index("buy_item") < text.index("sell_item")
    assert "都推进了局面" in text


def test_render_respects_char_cap(tmp_path):
    store = ExperienceStore(tmp_path / "e.jsonl")
    pat = pattern_of(_obs())
    for i in range(20):
        for _ in range(2):
            store.add(Experience(pattern=pat, action=f"act_{i}", progressed=i % 2 == 0))
    text = store.render(pat, max_chars=120)
    assert len(text) <= 160  # 头部 + 上限余量


def test_corrupt_line_is_skipped(tmp_path):
    path = tmp_path / "e.jsonl"
    path.write_text(
        '{"pattern": {}, "action": "x", "progressed": true, "ts": 1}\n'
        "这不是 JSON\n"
        '\n',
        encoding="utf-8",
    )
    store = ExperienceStore(path)  # 不应抛错
    assert len(store._items) == 1


def test_missing_file_is_harmless(tmp_path):
    store = ExperienceStore(tmp_path / "nope.jsonl")
    assert store.render(pattern_of(_obs())) == ""


def test_unwritable_path_does_not_raise(tmp_path):
    """落盘失败只丢磁盘那一步，内存统计照常。"""
    store = ExperienceStore(tmp_path / "no_such_dir" / "deep" / "e.jsonl")
    pat = pattern_of(_obs())
    store.add(Experience(pattern=pat, action="a", progressed=True))
    assert store.stats(pat, "a") == (1, 1)


def test_jsonl_line_shape(tmp_path):
    path = tmp_path / "e.jsonl"
    store = ExperienceStore(path)
    store.add(Experience(pattern=pattern_of(_obs()), action="call_next", progressed=True, note="队列还有货"))
    raw = json.loads(path.read_text(encoding="utf-8").strip())
    assert raw["action"] == "call_next" and raw["progressed"] is True
    assert raw["note"] == "队列还有货"


# ---------------------------------------------------------------- 匹配策略

def test_matches_requires_same_intent():
    """买家与卖家是完全不同的局面，经验不能互用。"""
    from game_agent.experience import matches
    a = {"phase": "day", "store": "OPEN", "intent": "BUY", "nego": "yes", "cash": "mid", "queue": "has"}
    b = dict(a, intent="SELL")
    assert matches(a, b) is False


def test_matches_requires_same_nego():
    from game_agent.experience import matches
    a = {"phase": "day", "store": "OPEN", "intent": "SELL", "nego": "yes", "cash": "mid", "queue": "has"}
    assert matches(a, dict(a, nego="no")) is False


def test_matches_tolerates_non_core_difference():
    """门店开关/队列变化不应让经验失效（4/6 一致即可）。"""
    from game_agent.experience import matches
    a = {"phase": "day", "store": "OPEN", "intent": "none", "nego": "yes", "cash": "mid", "queue": "has"}
    b = dict(a, store="CLOSED", queue="empty")
    assert matches(a, b) is True


def test_matches_rejects_too_many_differences():
    from game_agent.experience import matches
    a = {"phase": "day", "store": "OPEN", "intent": "none", "nego": "yes", "cash": "mid", "queue": "has"}
    b = dict(a, phase="night", store="CLOSED", cash="low", queue="empty")  # 4 项不同
    assert matches(a, b) is False
