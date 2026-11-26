"""校验 `mod/src/BridgeApi.cs` 里的 `GET /contract` 静态表没有漂移。

为什么需要它
------------
`/contract` 的动作表**只能**是编译期静态表（C# 无法运行期枚举 `switch (action)` 的
case）。静态表一旦和源码脱节，`/contract` 就会自报一份过时的「能力清单」——比不报还糟，
因为它看上去权威。所以：

1. 从 `mod/src/BridgeApi.cs` 的 `HandleAction` 重新机械抽取「动作 → 实际读取的参数」；
2. 与文件里的 `ContractActionParams` 逐条比对（多一条、少一条、参数集合不同都 FAIL）；
3. 再把 `ContractStateFieldsFallback` 与 `game_agent.contracts.STATE_FIELDS` 比对。

设计约束（别改回去）
--------------------
* 抽取参数必须**只看该 case 自己的语句范围**，不能全文扫 —— `ExtractValue(body, "x")`
  在几十个 case 里重复出现，全文扫会把所有参数混到一起。
* case 名必须回**原文**读：先做「抹掉字符串/注释」的净版用于结构分析（找 switch、
  算大括号深度），但 case 后面的那个字面量在净版里已经变成空格了，必须到原文偏移去读。
* 只认「作用对象是 `action` 的那个 switch」的**直属** case：`go_out` / `leave_outside`
  内部还有 `switch (dest)` / `switch (here)`，把它们的 case 算进来会凭空多出 20 个动作。

用法::

    python scripts/check_contract_tables.py            # 校验，不一致则 exit 1
    python scripts/check_contract_tables.py -v         # 打印抽取结果

退出码：0 = 一致；1 = 漂移（消息里给出具体差异与修复提示）。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

CS_PATH = ROOT / "mod" / "src" / "BridgeApi.cs"
PARAM_RE = re.compile(r'ExtractValue\s*\(\s*body\s*,\s*"([^"]+)"')
CASE_RE = re.compile(r'\bcase\s+"([^"]+)"\s*:')
SWITCH_ACTION_RE = re.compile(r"\bswitch\s*\(\s*action\s*\)")
CASE_LINE_RE = re.compile(r'^\s*case\s+"([^"]+)"\s*:\s*$')


def strip_literals_and_comments(src: str) -> str:
    """把字符串/字符字面量与注释替换成空格（保留换行与偏移长度）。"""
    out = list(src)
    i, n = 0, len(src)
    while i < n:
        c = src[i]
        if c == "/" and i + 1 < n and src[i + 1] == "/":
            while i < n and src[i] != "\n":
                out[i] = " "
                i += 1
            continue
        if c == "/" and i + 1 < n and src[i + 1] == "*":
            while i < n and not (src[i] == "*" and i + 1 < n and src[i + 1] == "/"):
                if src[i] != "\n":
                    out[i] = " "
                i += 1
            if i < n:
                out[i] = " "
                if i + 1 < n:
                    out[i + 1] = " "
                i += 2
            continue
        if c == '"':
            # 逐字符串 @"..." 少见，这里按普通串处理足够。
            out[i] = " "
            i += 1
            while i < n:
                if src[i] == "\\" and i + 1 < n:
                    out[i] = out[i + 1] = " "
                    i += 2
                    continue
                if src[i] == '"':
                    out[i] = " "
                    i += 1
                    break
                if src[i] != "\n":
                    out[i] = " "
                i += 1
            continue
        if c == "'":
            out[i] = " "
            i += 1
            while i < n:
                if src[i] == "\\" and i + 1 < n:
                    out[i] = out[i + 1] = " "
                    i += 2
                    continue
                if src[i] == "'":
                    out[i] = " "
                    i += 1
                    break
                if src[i] != "\n":
                    out[i] = " "
                i += 1
            continue
        i += 1
    return "".join(out)


def _match_brace(clean: str, open_at: int) -> int:
    """从 clean[open_at] == '{' 起做配对，返回匹配 '}' 的下标（找不到返回 -1）。"""
    depth = 0
    for i in range(open_at, len(clean)):
        if clean[i] == "{":
            depth += 1
        elif clean[i] == "}":
            depth -= 1
            if depth == 0:
                return i
    return -1


def find_handle_action_range(clean: str) -> tuple[int, int]:
    """定位 `HandleAction` 的函数体范围 [start, end)。"""
    m = re.search(r"\bstatic\s+string\s+HandleAction\s*\(", clean)
    if not m:
        raise SystemExit("找不到 HandleAction 方法")
    open_at = clean.find("{", m.end())
    if open_at < 0:
        raise SystemExit("HandleAction 之后找不到函数体起始 '{'")
    close_at = _match_brace(clean, open_at)
    if close_at < 0:
        raise SystemExit("HandleAction 函数体大括号不平衡")
    return open_at + 1, close_at


def find_action_switch(clean: str, start: int, end: int) -> tuple[int, int]:
    """定位作用对象是 ``action`` 的那个 switch 的语句范围 [start, end)。"""
    m = SWITCH_ACTION_RE.search(clean, start, end)
    if not m:
        raise SystemExit("HandleAction 里找不到 switch (action)")
    open_at = clean.find("{", m.end())
    if open_at < 0:
        raise SystemExit("switch (action) 之后找不到 '{'")
    close_at = _match_brace(clean, open_at)
    if close_at < 0:
        raise SystemExit("switch (action) 大括号不平衡")
    return open_at + 1, close_at


def action_cases(clean: str, src: str, start: int, end: int) -> list[tuple[str, int, int]]:
    """列出 switch(action) 的**直属** case：(名称, 语句起点, 语句终点)。

    直属 = 大括号深度相对 switch 体起点为 0。嵌套的 `case "commissary":`（在
    go_out 的 `switch (dest)` 里）深度为 2，会被排除。
    """
    depth = 0
    hits: list[tuple[int, int]] = []  # (case 名起点, case 标签结束位置)
    i = start
    while i < end:
        c = clean[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
        elif depth == 0:
            m = CASE_RE.match(src, i) or CASE_RE.match(clean, i)
            if m and src.startswith(m.group(0), i):
                # 用原文读 case 名（clean 版里字面量已被抹成空格）。
                hits.append((i, m.end()))
                i = m.end()
                continue
        i += 1

    out: list[tuple[str, int, int]] = []
    for idx, (name_at, label_end) in enumerate(hits):
        line_end = src.find("\n", name_at)
        raw = src[name_at:label_end]
        name = CASE_RE.search(raw).group(1) if CASE_RE.search(raw) else ""
        stmt_end = hits[idx + 1][0] if idx + 1 < len(hits) else end
        if name:
            out.append((name, label_end if line_end < 0 else max(label_end, line_end), stmt_end))
    return out


def extract(src: str) -> dict[str, list[str]]:
    clean = strip_literals_and_comments(src)
    start, end = find_handle_action_range(clean)
    sw_start, sw_end = find_action_switch(clean, start, end)
    result: dict[str, list[str]] = {}
    for name, body_start, body_end in action_cases(clean, src, sw_start, sw_end):
        # 参数只在**该 case 自己**的语句范围里找，避免全文混算。
        segment = src[body_start:body_end]
        params: list[str] = []
        for p in PARAM_RE.findall(segment):
            if p not in params:
                params.append(p)
        result[name] = params
    return result


def parse_cs_tables(src: str) -> tuple[dict[str, list[str]], list[str]]:
    """从 C# 源码里读回 `ContractActionParams` 与 `ContractStateFieldsFallback`。"""
    m = re.search(r"ContractActionParams\s*=\s*new Dictionary<string, string\[\]>[^\n]*\n\s*\{", src)
    if not m:
        raise SystemExit("找不到 ContractActionParams 表")
    open_at = src.index("{", m.end() - 1)
    close_at = _match_brace(src, open_at)
    block = src[open_at + 1:close_at]
    table: dict[str, list[str]] = {}
    for line in block.splitlines():
        mm = re.match(r'\s*\{\s*"([^"]+)"\s*,\s*(.*?)\s*\}\s*,\s*$', line)
        if not mm:
            continue
        name, arr = mm.group(1), mm.group(2)
        if "Array.Empty" in arr:
            table[name] = []
        else:
            table[name] = re.findall(r'"([^"]+)"', arr)

    fm = re.search(r"ContractStateFieldsFallback\s*=\s*\{", src)
    if not fm:
        raise SystemExit("找不到 ContractStateFieldsFallback 表")
    f_open = src.index("{", fm.end() - 1)
    f_close = _match_brace(src, f_open)
    fields = re.findall(r'"([^"]+)"', src[f_open + 1:f_close])
    return table, fields


def main(argv: list[str]) -> int:
    verbose = "-v" in argv or "--verbose" in argv
    src = CS_PATH.read_text(encoding="utf-8")

    actual = extract(src)
    declared, fallback_fields = parse_cs_tables(src)

    from game_agent import contracts as C

    # 虚拟输入动作走 VirtualInputDriver.IsVirtualAction 独立分派（Route 里先拦截），
    # 不在 HandleAction 的 switch 内 —— 但 ContractActionParams 仍须上报它们供 /contract 用。
    # 从驱动源码机械抽取认可名单，避免这里再写一份手工清单。
    driver_src = (ROOT / "mod" / "src" / "VirtualInputDriver.cs").read_text(
        encoding="utf-8-sig", errors="replace"
    )
    virtual_actions = set(re.findall(r'action\s*==\s*"([^"]+)"', driver_src))
    # ui_item_point / ui_grid_point 同样是 Route 里独立分派（只读坐标查询，不在 HandleAction switch 内）。
    virtual_actions.update({"ui_item_point", "ui_grid_point"})
    for name in virtual_actions:
        actual.setdefault(name, [])  # 视为"源码已实现"，参数由契约表单独校验

    problems: list[str] = []

    only_src = sorted(set(actual) - set(declared))
    only_decl = sorted(set(declared) - set(actual))
    for n in only_src:
        problems.append(f"C# 源码有 case 但 ContractActionParams 未登记：{n}")
    for n in only_decl:
        problems.append(f"ContractActionParams 登记了 C# 源码里不存在的 case：{n}")

    for name in sorted(set(actual) & set(declared)):
        a, d = set(actual[name]), set(declared[name])
        if name in virtual_actions:
            # 虚拟动作的参数在 VirtualInputDriver.BuildPlan 里读（ExtractFloat/ExtractInt/
            # ExtractText 包装），本脚本的 PARAM_RE 只认 BridgeApi 的 ExtractValue(body,…)。
            # 这些参数由 game_agent.contracts 的 MCP schema 自检覆盖，此处不重复校验。
            continue
        if a != d:
            problems.append(
                f"{name} 参数不一致：源码={sorted(a)} 表={sorted(d)}"
                f"（源码多 {sorted(a - d)}，表多 {sorted(d - a)}）"
            )

    for lost in sorted(set(C.STATE_FIELDS) - set(fallback_fields)):
        problems.append(f"ContractStateFieldsFallback 少了 contracts.STATE_FIELDS 里的：{lost}")
    for extra in sorted(set(fallback_fields) - set(C.STATE_FIELDS)):
        problems.append(f"ContractStateFieldsFallback 多了 contracts 未登记的：{extra}")

    if verbose:
        print(f"源码动作 {len(actual)} 个 / 表 {len(declared)} 个 / "
              f"兜底 state 字段 {len(fallback_fields)} 个")
        for n in sorted(actual):
            print(f"  {n}: {actual[n]}")

    if problems:
        print(f"× 契约静态表与源码不一致（{len(problems)} 处）：")
        for p in problems:
            print(f"  - {p}")
        print("\n修复：重跑 `python artifacts/_lead_probe/gen_contract_tables.py` 并把"
              "产出的表贴回 mod/src/BridgeApi.cs。")
        return 1

    print(f"✓ 契约静态表与源码一致（动作 {len(actual)}，"
          f"兜底 state 字段 {len(fallback_fields)}，"
          f"contracts.STATE_FIELDS {len(C.STATE_FIELDS)}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
