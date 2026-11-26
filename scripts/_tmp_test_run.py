"""临时测试脚本：用指定模型跑 N 步 live，验证提示词瘦身后的决策质量。

不修改 config/agent.json 与游戏内 ini，只在内存里覆盖 brain.model。
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from game_agent.agent_loop import run_loop
from game_agent.config import load_config

MODEL = sys.argv[1] if len(sys.argv) > 1 else "glm-5.3-flash"
STEPS = int(sys.argv[2]) if len(sys.argv) > 2 else 5

cfg = load_config()
cfg.setdefault("brain", {})["model"] = MODEL
print(f"[test] model={MODEL} steps={STEPS}", flush=True)

records = run_loop(
    cfg,
    max_steps=STEPS,
    live=True,
    delay_between=0.5,
    source="bridge",
    event_driven=True,
    push_overlay=False,
    allow_mouse=False,
)

print(f"\n[test] 完成 {len(records)} 步\n")
for i, r in enumerate(records, 1):
    dec = r.get("decision") or {}
    ex = r.get("execution") or {}
    meta = r.get("brain_meta") or {}
    print(f"--- 第 {i} 步 ---")
    print(f"动作: {dec.get('action')} {json.dumps(dec.get('params') or {}, ensure_ascii=False)}")
    print(f"理由: {str(dec.get('reason'))[:300]}")
    print(f"结果: ok={ex.get('ok')} {str(ex.get('detail') or '')[:100]}")
    print(f"耗时: {meta.get('duration_ms')}ms  attempts={meta.get('attempts')}")
    if r.get("stow_after_deal"):
        print(f"清理: {json.dumps(r['stow_after_deal'], ensure_ascii=False)[:160]}")
    print()
