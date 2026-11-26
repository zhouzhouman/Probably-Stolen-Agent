"""正式版迁移：试玩版 dump.cs 与正式版反编译源码的结构化 diff。

试玩版 = tools/Il2CppDumper/dump.cs（IL2CPP，TypeDef 级签名）
正式版 = tools/decompile_full/*.cs（Mono，ILSpy 反编译的真实 C# 源码）

两版后端不同（IL2CPP -> Mono），dump.cs 与 C# 源码的语法外形也不一样，
因此本脚本做**成员名集合级**比对：字段 / 方法 / 属性 三个桶各自取名字集合，
报告「正式版缺失」「正式版新增」。名字一致再谈签名，避免一上来就啃类型系统。

用法:
    python scripts/diff_dump.py                # 全部关键类型
    python scripts/diff_dump.py PlayerStore    # 单个类型
    python scripts/diff_dump.py --json out.json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEMO_DUMP = ROOT / "tools" / "Il2CppDumper" / "dump.cs"
FULL_SRC = ROOT / "tools" / "decompile_full"

# 桥接实际依赖的类型（bridge/*.cs 静态引用的游戏类型）
KEY_TYPES = [
    "PlayerStore",
    "EmporiumEntry",
    "StoreClient",
    "StoreClientManager",
    "GameItem",
    "GameInventory",
    "GameGridInventory",
    "DialogUIManager",
    "MapUIManager",
    "HealthData",
]

TYPE_DECL = re.compile(
    r"^\s*(?:public|internal|private|protected)?\s*(?:sealed\s+|abstract\s+|static\s+|partial\s+)*"
    r"(?:class|struct|interface|enum)\s+(\w+)"
)


def brace_block(lines: list[str], start: int) -> tuple[list[str], int]:
    """从 start 行开始，向上找到第一个 '{'，返回配对块内部的行与结束行号。"""
    i = start
    while i < len(lines) and "{" not in lines[i]:
        i += 1
    if i >= len(lines):
        return [], start
    depth = 0
    body: list[str] = []
    while i < len(lines):
        line = lines[i]
        depth += line.count("{") - line.count("}")
        body.append(line)
        if depth <= 0:
            return body, i
        i += 1
    return body, i


# ---------------------------------------------------------------- 试玩版 dump.cs

def demo_members(type_name: str) -> dict[str, set[str]] | None:
    text = DEMO_DUMP.read_text(encoding="utf-8", errors="replace").splitlines()
    head = re.compile(
        rf"^\s*(?:public|internal)?\s*\w*\s*(?:class|struct|interface|enum)\s+{re.escape(type_name)}"
        rf"\s*(?::[^{{]*)?(?://|\{{|$)"
    )
    for idx, line in enumerate(text):
        if head.match(line):
            body, _ = brace_block(text, idx)
            return _parse_dump_body(body)
    return None


def _parse_dump_body(body: list[str]) -> dict[str, set[str]]:
    fields: set[str] = set()
    methods: set[str] = set()
    props: set[str] = set()
    for raw in body:
        # dump.cs 每行尾部都带 "// 0x10" 偏移注释，必须先剥注释再判断语句形态
        code = re.sub(r"//.*$", "", raw).strip()
        if not code:
            continue
        if "(" in code and (code.endswith(";") or code.endswith("{ }") or code.endswith("{}")):
            # dump.cs 的方法签名以 "{ }" 结尾（无方法体），不是 ";"
            name = re.search(r"(\w+)\s*\(", code)
            if name and name.group(1) not in {"if", "for", "while", "return", "new"}:
                methods.add(name.group(1))
            continue
        if (m := re.match(r"^[\w\.<>\[\],\?]+\s+(?:operator\s+)?(\w+)\s*\{", code)):
            props.add(m.group(1))  # 属性
            continue
        if code.endswith(";"):  # 字段（可能有 = 初始值）
            decl = code.rstrip(";").split("=")[0].strip()
            name = re.search(r"(\w+)\s*$", decl)
            if name:
                fields.add(name.group(1))
    return {"fields": fields, "methods": methods, "props": props}


# ------------------------------------------------------------- 正式版 C# 源码

def full_members(type_name: str) -> dict[str, set[str]] | None:
    for path in FULL_SRC.rglob(f"{type_name}.cs"):
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        for idx, line in enumerate(lines):
            m = TYPE_DECL.match(line)
            if m and m.group(1) == type_name:
                body, _ = brace_block(lines, idx)
                result = _parse_cs_body(body)
                result["_file"] = path.relative_to(ROOT).as_posix()
                return result
    return None


FIELD_RE = re.compile(
    r"^\s*(?:\[[^\]]*\]\s*)*(?:public|private|protected|internal)\s+"
    r"(?:static\s+|readonly\s+|const\s+|volatile\s+|new\s+|unsafe\s+|event\s+)*"
    r"[\w\.<>\[\],\?]+\s+(\w+)\s*(?:=|;)",
)
METHOD_RE = re.compile(
    r"^\s*(?:\[[^\]]*\]\s*)*(?:public|private|protected|internal)\s+"
    r"(?:static\s+|virtual\s+|override\s+|abstract\s+|sealed\s+|async\s+|extern\s+|partial\s+|new\s+|unsafe\s+)*"
    r"[\w\.<>\[\],\?]+\s+(\w+)\s*\(",
)
PROP_RE = re.compile(
    r"^\s*(?:public|private|protected|internal)\s+"
    r"(?:static\s+|virtual\s+|override\s+|abstract\s+|sealed\s+|new\s+)*"
    r"[\w\.<>\[\],\?]+\s+(\w+)\s*\{",
)


def _parse_cs_body(body: list[str]) -> dict[str, set[str]]:
    fields: set[str] = set()
    methods: set[str] = set()
    props: set[str] = set()
    for raw in body:
        line = raw.strip()
        if not line or line.startswith("//") or line.startswith("["):
            continue
        if m := METHOD_RE.match(line):
            methods.add(m.group(1))
        elif m := PROP_RE.match(line):
            props.add(m.group(1))
        elif m := FIELD_RE.match(line):
            fields.add(m.group(1))
    return {"fields": fields, "methods": methods, "props": props}


# ------------------------------------------------------------------ 比对与输出

NOISE_METHOD = re.compile(r"^(?:b__.*|cctor|ctor|\.ctor|<.*>.*)$")


def _real_lost(demo: dict[str, set[str]], full: dict[str, set[str]]) -> list[str]:
    """去掉编译产物噪音与「字段↔属性」形态转换，只留真实消失的成员名。

    IL2CPP dump 里 `get_Foo()` 是方法，ILSpy 反编译后会还原成属性 Foo，
    这种不是丢失，是形态变化。闭包 lambda / backing field / ctor 也一律忽略。
    """
    full_names = full["fields"] | full["methods"] | full["props"]
    lost: list[str] = []
    for name in demo["fields"] | demo["methods"] | demo["props"]:
        if name in full_names or name == "k__BackingField" or NOISE_METHOD.match(name):
            continue
        m = re.match(r"^(?:get|set)_(\w+)$", name)
        if m and m.group(1) in full_names:
            continue  # 访问器 -> 属性
        lost.append(name)
    return sorted(set(lost))


def diff_type(type_name: str) -> dict:
    demo = demo_members(type_name)
    full = full_members(type_name)
    if demo is None or full is None:
        return {"type": type_name, "status": "missing", "in_demo": demo is not None,
                "in_full": full is not None}
    out = {"type": type_name, "status": "ok", "file": full.get("_file")}
    for bucket in ("fields", "methods", "props"):
        d, f = demo[bucket], full[bucket]
        out[bucket] = {
            "demo": len(d),
            "full": len(f),
            "lost": sorted(d - f),
            "added": sorted(f - d),
        }
    out["real_lost"] = _real_lost(demo, full)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("types", nargs="*", default=None)
    ap.add_argument("--json", default=None)
    args = ap.parse_args()
    names = args.types or KEY_TYPES

    results = [diff_type(n) for n in names]
    if args.json:
        Path(args.json).write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")

    for r in results:
        if r["status"] == "missing":
            print(f"\n### {r['type']}: 无法比对（试玩版存在={r['in_demo']} / 正式版存在={r['in_full']}）")
            continue
        print(f"\n### {r['type']}  ({r['file']})")
        for bucket, label in (("fields", "字段"), ("methods", "方法"), ("props", "属性")):
            b = r[bucket]
            flag = "" if not b["lost"] else f"  ⚠ 丢失 {len(b['lost'])}"
            print(f"  {label}: 试玩 {b['demo']} -> 正式 {b['full']}{flag}")
            if b["lost"]:
                print(f"    丢失: {', '.join(b['lost'][:40])}{' …' if len(b['lost']) > 40 else ''}")
            if b["added"]:
                print(f"    新增: {', '.join(b['added'][:25])}{' …' if len(b['added']) > 25 else ''}")
    print("\n=== 真实缺失（已剔除编译产物与字段/属性形态转换） ===")
    total = 0
    for r in results:
        if r["status"] != "ok" or not r["real_lost"]:
            continue
        total += len(r["real_lost"])
        print(f"  {r['type']}: {len(r['real_lost'])} -> {', '.join(r['real_lost'])}")
    print(f"\n合计真实缺失 {total} 个")
    return 0


if __name__ == "__main__":
    sys.exit(main())
