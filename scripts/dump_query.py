"""dump.cs 查询工具：从 Il2CppDumper 输出中抽取指定类的完整定义。

用法：
    python scripts/dump_query.py list "Store*"          # 列出类名（支持通配）
    python scripts/dump_query.py show PlayerStore       # 输出类完整定义（字段+方法）
    python scripts/dump_query.py methods PlayerStore    # 只输出方法签名列表
    python scripts/dump_query.py search "TryCallNext"   # 全文件搜索符号

dump 文件默认取 tools/Il2CppDumper/dump.cs（可用 --dump 指定）。
"""
from __future__ import annotations

import argparse
import fnmatch
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DUMP = PROJECT_ROOT / "tools" / "Il2CppDumper" / "dump.cs"

# 类/结构体定义行：public class Foo : Bar // TypeDefIndex: 123
_CLASS_RE = re.compile(r"^(?:public |internal |private |sealed |abstract |static )*(?:class|struct|enum|interface)\s+([A-Za-z_][\w`<>.]*)")


def load_lines(dump_path: Path) -> list[str]:
    return dump_path.read_text(encoding="utf-8", errors="replace").splitlines()


def list_classes(lines: list[str], pattern: str) -> list[str]:
    hits = []
    for line in lines:
        m = _CLASS_RE.match(line.strip())
        if m and fnmatch.fnmatch(m.group(1), pattern):
            hits.append(line.strip())
    return hits


def extract_class(lines: list[str], class_name: str) -> list[str]:
    """抽取类定义块（从类声明行到下一个顶层声明行）。"""
    start = None
    for i, line in enumerate(lines):
        m = _CLASS_RE.match(line.strip())
        if m and m.group(1) == class_name:
            start = i
            break
    if start is None:
        return []
    depth = 0
    started = False
    block: list[str] = []
    for line in lines[start:]:
        stripped = line.strip()
        block.append(line)
        if "{" in line:
            depth += line.count("{")
            started = True
        if "}" in line:
            depth -= line.count("}")
        if started and depth <= 0:
            break
    return block


def class_methods(block: list[str]) -> list[str]:
    """从类块中提取方法签名行（含注释行 RVA 之后的声明行）。"""
    methods = []
    for line in block:
        s = line.strip()
        if not s or s.startswith("//") or s.startswith("["):
            continue
        if s.endswith("}") and ("(" in s) and not s.startswith("}"):
            methods.append(s)
    return methods


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except Exception:
            pass

    parser = argparse.ArgumentParser(description="dump.cs 查询工具")
    parser.add_argument("--dump", default=str(DEFAULT_DUMP), help="dump.cs 路径")
    sub = parser.add_subparsers(dest="command", required=True)

    p_list = sub.add_parser("list", help="列出类名（通配符）")
    p_list.add_argument("pattern", nargs="?", default="*")

    p_show = sub.add_parser("show", help="输出类完整定义")
    p_show.add_argument("class_name")

    p_methods = sub.add_parser("methods", help="只输出方法签名")
    p_methods.add_argument("class_name")

    p_search = sub.add_parser("search", help="全文件搜索符号（正则）")
    p_search.add_argument("pattern")
    p_search.add_argument("--limit", type=int, default=40)

    args = parser.parse_args(argv)
    dump_path = Path(args.dump)
    if not dump_path.is_file():
        print(f"dump 文件不存在：{dump_path}", file=sys.stderr)
        return 1
    lines = load_lines(dump_path)

    if args.command == "list":
        for hit in list_classes(lines, args.pattern):
            print(hit)
    elif args.command == "show":
        block = extract_class(lines, args.class_name)
        if not block:
            print(f"未找到类：{args.class_name}", file=sys.stderr)
            return 2
        print("\n".join(block))
    elif args.command == "methods":
        block = extract_class(lines, args.class_name)
        if not block:
            print(f"未找到类：{args.class_name}", file=sys.stderr)
            return 2
        for sig in class_methods(block):
            print(sig)
    elif args.command == "search":
        pattern = re.compile(args.pattern, re.IGNORECASE)
        count = 0
        for i, line in enumerate(lines, 1):
            if pattern.search(line):
                print(f"{i}: {line.strip()}")
                count += 1
                if count >= args.limit:
                    print(f"...（达到上限 {args.limit}）")
                    break
    return 0


if __name__ == "__main__":
    sys.exit(main())
