"""把 bridge/ 的 BepInEx(IL2CPP) 插件机械移植为游戏原生 mod(Mono)。

只做**可机械判定**的替换，凡是需要人判断的地方一律保留原样并在结束时列出，
交给编译器和人工处理——不做"猜着改"。

用法:
    python scripts/port_bridge_to_mod.py
"""

from __future__ import annotations

import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "bridge"
DST = ROOT / "mod" / "src"

# 需要移植的文件（Plugin.cs 不机械移植，改为手写 ModEntry.cs）
FILES = ["BridgeApi.cs", "BridgeServer.cs", "DecisionOverlay.cs"]

# (正则, 替换, 说明)
RULES = [
    # IL2CPP 用对象指针做身份；Mono 下对象引用本身就是身份，直接去掉 .Pointer
    (r"\.Pointer\b", "", "IL2CPP 对象指针 -> 原生引用身份"),
    (r"HashSet<System\.IntPtr>", "HashSet<GameInventory>",
     "槽位身份集合 IntPtr -> GameInventory 引用"),
    # PlayerStore.gridInv 是私有字段，正式版提供公开的 GetStoreInventory()
    (r"\b(ps|store|playerStore)\.gridInv\b", r"\1.GetStoreInventory()",
     "PlayerStore.gridInv -> GetStoreInventory()"),
    (r"new Il2CppSystem\.Collections\.Generic\.List<", "new List<",
     "Il2Cpp 泛型 List -> System.Collections.Generic.List"),
    (r"Il2CppSystem\.Collections\.Generic\.List<", "List<",
     "Il2Cpp 泛型 List（签名位置）-> List"),
    (r"Resources\.FindObjectsOfTypeAll\(Il2CppType\.Of<([^<>]+)>\(\)\)",
     r"Resources.FindObjectsOfTypeAll<\1>()",
     "Il2CppType.Of<T> 版 FindObjectsOfTypeAll -> 泛型重载"),
    (r"Il2CppType\.Of<([^<>]+)>\(\)", r"typeof(\1)",
     "Il2CppType.Of<T>() -> typeof(T)"),
    (r"\.GetIl2CppType\(\)", ".GetType()",
     "GetIl2CppType() -> GetType()"),
    (r"public (DecisionOverlay|MainThreadDispatcher)\(IntPtr ptr\) : base\(ptr\) \{ \}",
     r"public \1() { }",
     "IL2CPP 的 IntPtr 构造 -> 默认构造"),
    (r"见 BepInEx 日志", "见 Unity Player.log", "日志指向修正"),
    (r"new Il2CppStructArray<Vector3>\((\d+)\)", r"new Vector3[\1]",
     "Il2CppStructArray<Vector3> -> Vector3[]"),
    (r"new Il2CppReferenceArray<Il2CppSystem\.Type>\((\d+)\)", r"new System.Type[\1]",
     "Il2CppReferenceArray<Type> -> Type[]"),
    (r"\bIl2CppSystem\.Type\b", "System.Type", "Il2CppSystem.Type -> System.Type"),
    (r"Il2CppReferenceArray<([A-Za-z0-9_.]+)>", r"\1[]",
     "Il2CppReferenceArray<T> -> T[]"),
    (r"BridgePlugin\.Logger\.LogInfo\(", "ModLog.Info(",
     "BepInEx 日志 -> ModLog"),
    (r"BridgePlugin\.Logger\.LogWarning\(", "ModLog.Warn(",
     "BepInEx 日志 -> ModLog"),
    (r"BridgePlugin\.Logger\.LogError\(", "ModLog.Error(",
     "BepInEx 日志 -> ModLog"),
    (r"BridgePlugin\.Version", "DeepSpaceBridgeMod.Version", "插件版本常量 -> mod 版本常量"),
]

# TryCast 不做文本替换：改为在 mod/src/Il2CppCompat.cs 提供签名一致的扩展方法
# （正则切"接收者表达式"的边界很容易出错，扩展方法能做到零改动且语义一致）

# 应当彻底消失的 using（IL2CPP 互操作）
DROP_USINGS = [
    "using Il2CppInterop.Runtime;",
    "using Il2CppInterop.Runtime.InteropTypes;",
    "using Il2CppInterop.Runtime.InteropTypes.Arrays;",
    "using Il2CppInterop.Runtime.Injection;",
    "using BepInEx;",
    "using BepInEx.Logging;",
    "using BepInEx.Unity.IL2CPP;",
]

# 移植后仍残留、必须人工确认的标记
SUSPECT = re.compile(r"Il2Cpp|BepInEx|ClassInjector|IntPtr ptr")


def port_one(path: Path) -> tuple[str, list[str]]:
    text = path.read_text(encoding="utf-8")
    applied: list[str] = []
    for pattern, repl, note in RULES:
        text, n = re.subn(pattern, repl, text)
        if n:
            applied.append(f"{note} ×{n}")
    for using in DROP_USINGS:
        if using in text:
            text = text.replace(using + "\n", "")
            text = text.replace(using, "")
            applied.append(f"移除 {using}")
    return text, applied


def main() -> int:
    DST.mkdir(parents=True, exist_ok=True)
    existing = [f for f in FILES if (DST / f).exists()]
    if existing and "--force" not in sys.argv:
        print("× mod/src 下已存在移植产物：" + ", ".join(existing))
        print("  本脚本只做一次性机械移植；移植后 mod/src 是唯一真源（已手工适配正式版 API）。")
        print("  重新执行会覆盖这些手工修改。确认要重来请加 --force。")
        return 2
    for name in FILES:
        src = SRC / name
        text, applied = port_one(src)
        (DST / name).write_text(text, encoding="utf-8")
        print(f"\n### {name} -> mod/src/{name}")
        for a in applied:
            print(f"  - {a}")

    print("\n=== 残留、需人工确认的行 ===")
    leftover = 0
    for name in FILES:
        for i, line in enumerate((DST / name).read_text(encoding="utf-8").splitlines(), 1):
            if SUSPECT.search(line):
                leftover += 1
                print(f"  {name}:{i}: {line.strip()[:120]}")
    print(f"\n合计残留 {leftover} 处")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
