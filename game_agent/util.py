"""通用工具函数（跨模块共享）。"""
from __future__ import annotations

import json
import re
from typing import Any


def extract_json(text: str) -> dict[str, Any]:
    """从模型输出文本中提取 JSON 对象。

    依次尝试：整体解析 → 去 Markdown 围栏 → 截取首尾花括号的子串。
    供视觉模块（观察结果）与决策模块（动作决策）共用。

    Raises:
        ValueError: 输出为空或找不到任何合法 JSON 对象。
    """
    if not text or not text.strip():
        raise ValueError("模型输出为空")
    stripped = text.strip()

    candidates: list[str] = [stripped]
    fence = re.search(r"```(?:json)?\s*(.*?)```", stripped, re.S | re.I)
    if fence:
        candidates.append(fence.group(1).strip())
    start, end = stripped.find("{"), stripped.rfind("}")
    if start != -1 and end > start:
        candidates.append(stripped[start:end + 1])

    for candidate in candidates:
        try:
            obj = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            return obj
    raise ValueError("未能从模型输出中找到合法 JSON 对象")
