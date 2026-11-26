"""宽容 JSON 解析（兼容 Easy Save 3 存档的「近似 JSON」格式）。

游戏存档（save_*.es3）本质是 ES3 序列化出的 JSON 文本，但实测存在两类
不符合 JSON 标准、却稳定出现的写法：

1. **数字做对象键**：  {"PhoneClientDict":{8376:{"a":1}}}
   JSON 标准要求对象键必须是字符串，数字键是典型的 JavaScript 风格。

2. **字符串内非法转义**：  "\“泥炭\”合成肉"
   游戏对中文全角引号也加了反斜杠（\\“ / \\”），而 JSON 只允许
   \\" \\\\ \\/ \\b \\f \\n \\r \\t \\uXXXX 这几种转义序列。

repair_es3_json() 用「字符串感知」的单遍状态机同时修复两类问题：
- 仅在字符串**外部**处理数字键（`{` 或 `,` 之后 空白* 纯数字 `:`）；
- 仅在字符串**内部**清理非法转义（丢弃非法转义前的反斜杠、保留字符本身）。

因此不会误伤字符串内容——例如值里恰好出现 ",123:" 这样的文本会原样保留。

修复器是**可重入**的：ES3 会把嵌套对象再序列化成「字符串形式的 JSON」
（如 playerStore.mainInvJSON），对解析出的内层字符串再次调用
parse_lenient() 即可按层修复，与修复外层时互不干扰。
"""
from __future__ import annotations

import json
from typing import Any

__all__ = ["parse_lenient", "repair_es3_json"]

# JSON 标准允许的转义字符（反斜杠之后的字符）
_ALLOWED_ESCAPES = frozenset('"\\/bfnrtu')


def repair_es3_json(text: str) -> str:
    """修复 ES3 存档中两类非标准 JSON 写法，返回修复后的文本。

    状态机扫描，区分「字符串内 / 字符串外」两种上下文：
    - 字符串外：把 `{`/`,` 之后紧跟的「纯数字键」补上双引号；
    - 字符串内：遇到非法转义（反斜杠 + 非法字符）时丢弃多余的反斜杠。
    """
    out: list[str] = []
    i = 0
    n = len(text)
    in_string = False

    while i < n:
        ch = text[i]

        if not in_string:
            if ch == '"':
                in_string = True
                out.append(ch)
                i += 1
            elif ch in "{,":
                out.append(ch)
                i += 1
                # 跳过（并保留）空白，定位到可能的键名开头
                j = i
                while j < n and text[j] in " \t\r\n":
                    out.append(text[j])
                    j += 1
                # 尝试识别数字键：可选负号 + 纯数字序列 + 可选空白 + 冒号
                # 例：{8376:...} / ,-103:...（负数键出现在窗口位置缓存等字段）
                k = j
                if k < n and text[k] == "-":
                    k += 1
                digits_start = k
                while k < n and text[k].isdigit():
                    k += 1
                k2 = k
                while k2 < n and text[k2] in " \t\r\n":
                    k2 += 1
                if k > digits_start and k2 < n and text[k2] == ":":
                    out.append('"')
                    out.append(text[j:k])
                    out.append('"')
                    out.append(text[k:k2])  # 数字与冒号之间的空白原样保留
                    i = k2
                else:
                    i = j
            else:
                out.append(ch)
                i += 1
            continue

        # —— 字符串内部 ——
        if ch == "\\":
            nxt = text[i + 1] if i + 1 < n else ""
            if nxt in _ALLOWED_ESCAPES:
                # 合法转义：整体保留（\\ 与 \" 均在此通过，不受影响）
                out.append(ch)
                out.append(nxt)
                i += 2
            elif nxt:
                # 非法转义（如 \“ \”）：丢弃反斜杠，保留字符本身
                i += 1
            else:
                # 文件在孤立反斜杠处结束（截断文件）：原样保留，交由解析器报错
                out.append(ch)
                i += 1
        elif ch == '"':
            in_string = False
            out.append(ch)
            i += 1
        else:
            out.append(ch)
            i += 1

    return "".join(out)


def parse_lenient(text: str) -> Any:
    """解析（可能非标准的）JSON 文本：先按标准解析，失败后再修复重试。

    Args:
        text: JSON 文本（ES3 存档 / 其中嵌套的 JSON 字符串均可）。

    Returns:
        解析后的 Python 对象。

    Raises:
        TypeError: 输入不是字符串。
        ValueError: 修复后仍无法解析（附带底层解析器错误信息）。
    """
    if not isinstance(text, str):
        raise TypeError(f"parse_lenient 需要 str 输入，收到 {type(text).__name__}")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    repaired = repair_es3_json(text)
    try:
        return json.loads(repaired)
    except json.JSONDecodeError as exc:
        raise ValueError(f"ES3 JSON 修复后仍无法解析：{exc}") from exc
