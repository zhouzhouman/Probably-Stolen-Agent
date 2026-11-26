"""把编译好的 mod 部署到正式版的 Mods 目录。

游戏内 mod 布局（以三个真实 Workshop mod 为准）：
    <游戏根>/Mods/<任意文件夹名>/
        manifest.xml      <- 必填，ID/Assemblies/Capabilities
        <Assembly>.dll    <- manifest 里 <Assemblies> 列到的 DLL

用法:
    python scripts/deploy_mod.py                  # 部署
    python scripts/deploy_mod.py --check          # 只看会做什么，不落盘
    python scripts/deploy_mod.py --clean-payloads # 顺手清掉目标目录里的载荷残留
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GAME = Path(r"D:\SteamLibrary\steamapps\common\Probably Stolen")
BUILD = ROOT / "mod" / "bin" / "Debug" / "netstandard2.1" / "DeepSpaceBridge.dll"
MANIFEST = ROOT / "mod" / "manifest.xml"
TARGET = GAME / "Mods" / "DeepSpaceBridge"

#: 旧 DLL 与被清出的载荷残留的归档目录。
#:
#: 必须在 mod 目录**之外** —— 2026-11-14 实机把备份留在
#: ``Mods/DeepSpaceBridge/`` 里，被扫描器判 ``payload-file`` 整包拒载。
#: 单独提成模块级常量是为了让测试能 monkeypatch，否则跑测试会往仓库里写备份。
BACKUP_DIR = ROOT / "artifacts" / "mod_backups"

#: 扫描器会当作「可执行载荷」拒载的后缀形态。
#:
#: 为什么必须在这里拦：2026-11-14 实机把旧 DLL 备份成
#: ``DeepSpaceBridge.dll.bak-177152`` 留在 mod 目录里，游戏内扫描器报
#: ``payload-file: DeepSpaceBridge.dll.bak-177152 -> executable content``
#: 并**直接拒载整个 mod**。症状是「编译成功、部署脚本也说成功、游戏里就是不出现」，
#: 三者同时成立时几乎不会有人先怀疑「目录里多了个备份文件」。
#:
#: 注意 ``.dll.bak-177152`` 这种**双重后缀**最容易漏 —— 文件名不以 ``.dll`` 结尾，
#: 但内容就是 DLL 字节，扫描器**按内容判定**，改成什么名字都照样拒载。
PAYLOAD_SUFFIXES = (".bak", ".old", ".orig", ".save", ".backup", ".tmp", ".disabled")

#: 部署后目标目录里**允许**存在的文件（相对 TARGET 的文件名）。
#: 其余一切文件都算「多余」，其中载荷型的直接拒绝部署。
ALLOWED_TARGET_FILES = ("manifest.xml",)


def _data_dir() -> Path:
    local = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    d = Path(local) / "DeepSpaceBridge"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _is_payload_residue(path: Path) -> bool:
    """文件名是否像「被改名过的可执行载荷」。

    **纯 ``.dll`` 不算残留** —— 那是 manifest ``<Assemblies>`` 正式声明的程序集，
    是必须存在的。真正危险的是**被改名过的**载荷：``.dll.bak-177152``（双重后缀）、
    ``x.dll.old``、``y.bak`` 等；扫描器按内容判定，改名不当也照样拒载。
    """
    name = path.name.lower()
    if any(part in name for part in PAYLOAD_SUFFIXES):
        return True
    # ``.dll.bak-177152``：名字中间出现 ``.dll.`` 即视为载荷改名（纯 .dll 结尾不算）
    return ".dll." in name


def _scan_target_residue(declared: tuple[str, ...]) -> tuple[list[Path], list[Path]]:
    """扫目标目录，返回 ``(载荷残留, 其它多余文件)``。

    判据分两档：
      * **载荷残留**（``.bak`` / ``.dll.`` / ``.old`` …）—— 必拒，扫描器按内容判定；
      * **其它多余文件** —— 不拒（可能是运行期文件，稍后由 :func:`_migrate_runtime_files`
        迁出），但打印提醒，因为游戏也会读目录里的每个文件。
    """
    if not TARGET.is_dir():
        return [], []

    allowed = set(ALLOWED_TARGET_FILES) | set(declared)
    payload: list[Path] = []
    others: list[Path] = []
    for p in sorted(TARGET.iterdir()):
        if not p.is_file() or p.name in allowed:
            continue
        (payload if _is_payload_residue(p) else others).append(p)
    return payload, others


def _backup_path(src: Path) -> Path:
    """给「要被覆盖的旧文件」挑一个 mod 目录**之外**的备份路径。

    备份必须放在 ``artifacts/mod_backups/``：mod 目录里任何多余的 DLL 字节文件
    都会被扫描器当成 payload（2026-11-14 实机拒载事故的直接原因）。
    """
    out_dir = BACKUP_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = src.stat().st_size if src.exists() else 0
    return out_dir / f"{src.name}.bak-{stamp}"


def _migrate_runtime_files() -> list[str]:
    """把旧版放在 Mods 里的运行期文件搬到数据目录，并从 Mods 里删掉。

    必须删：扫描器会读取 mod 文件夹里的每个文件，日志被占用就会整包被拒
    （实测 Sharing violation on agent_run.log → mod 拒绝加载）。
    """
    moved = []
    for name in ("model_config.ini", "agent_run.log", "agent.request", "agent.status.json"):
        src = TARGET / name
        if not src.exists():
            continue
        dst = _data_dir() / name
        try:
            if name == "model_config.ini" and dst.exists():
                pass                      # 新位置已有配置：以新的为准，下面统一删旧
            else:
                shutil.move(str(src), str(dst))
                moved.append(name)
        except OSError as e:
            print(f"  × 迁移 {name} 失败：{e}")
        try:
            if src.exists():
                src.unlink()
        except OSError:
            pass
    return moved


def _seed_agent_keys() -> list[str]:
    """把 agent 的启动命令/工作目录写进 model_config.ini（仅补齐缺失键）。

    为什么由部署脚本填：mod 跑在游戏进程里，不可能知道项目根在哪；
    而部署脚本两个路径都握着（ROOT 与当前解释器）。
    """
    ini = _data_dir() / "model_config.ini"
    lines = []
    if ini.exists():
        lines = ini.read_text(encoding="utf-8-sig", errors="replace").splitlines()

    have = set()
    for line in lines:
        if "=" in line and not line.strip().startswith("#"):
            have.add(line.split("=", 1)[0].strip().lower())

    python = str(Path(sys.executable))
    desired = {
        "agent_workdir": str(ROOT),
        "agent_command": f'"{python}" -m game_agent.cli run --live --source bridge --no-mouse --max-steps 0',
    }

    added = []
    for key, value in desired.items():
        if key in have:
            continue
        lines.append(f"{key}={value}")
        added.append(key)

    if added:
        if not ini.exists():
            lines.insert(0, "# DeepSpace Bridge 模型配置（游戏内 Ctrl+F9 面板写入）")
        ini.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return added


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="只检查，不写盘")
    ap.add_argument("--clean-payloads", action="store_true",
                    help="把目标目录里的载荷型残留（.bak/.dll./.old）移到 artifacts/mod_backups/ 再部署")
    args = ap.parse_args()

    if not BUILD.exists():
        print(f"× 找不到编译产物：{BUILD}\n  先跑：dotnet build mod/DeepSpaceMod.csproj")
        return 1
    if not MANIFEST.exists():
        print(f"× 找不到 manifest：{MANIFEST}")
        return 1
    if not GAME.exists():
        print(f"× 找不到游戏目录：{GAME}")
        return 1

    declared = tuple(read_assemblies(MANIFEST))
    print(f"源 DLL  ：{BUILD}  ({BUILD.stat().st_size} 字节)")
    print(f"manifest：{MANIFEST}")
    print(f"目标目录：{TARGET}")
    print(f"声明程序集：{', '.join(declared)}")

    # ── 写盘前防线：目标目录里不得有「扫描器会当成可执行载荷」的文件 ──
    # 2026-11-14 实机事故：旧 DLL 备份成 DeepSpaceBridge.dll.bak-177152 留在 mod
    # 目录里，扫描器报 payload-file 并拒载整个 mod。编译成功、部署成功、游戏里没有，
    # 三者同时成立 —— 必须靠脚本自己挡住，不能靠人记得。
    payload, others = _scan_target_residue(declared)
    if payload:
        print("\n× 目标目录里有载荷型残留，游戏内扫描器会拒载整个 mod：")
        for p in payload:
            print(f"    · {p.name}")
        if args.clean_payloads:
            out_dir = BACKUP_DIR
            out_dir.mkdir(parents=True, exist_ok=True)
            print(f"\n  正在移到 {out_dir}（该目录不参与部署）：")
            for p in payload:
                dst = out_dir / p.name
                try:
                    if dst.exists():
                        dst.unlink()
                    shutil.move(str(p), str(dst))
                    print(f"    ✓ {p.name} -> {dst.name}")
                except OSError as e:
                    print(f"    × 移动 {p.name} 失败：{e}")
                    return 2
            payload, others = _scan_target_residue(declared)
            if payload:
                print("\n× 仍有载荷残留，中止部署：")
                for p in payload:
                    print(f"    · {p.name}")
                return 2
        else:
            print("\n  请手工移走，或加 --clean-payloads 让脚本自动移到 artifacts/mod_backups/。")
            print("  备份**不要**留在 mod 目录里：扫描器按内容判定，改名也照样拒载。")
            return 2
    if others:
        print(f"\n  提示：目标目录还有 {len(others)} 个非声明文件"
              f"（{', '.join(p.name for p in others)}）——运行期文件稍后会被迁出；")
        print("  其它文件虽然不一定拒载，但都会参与扫描，建议只留 manifest + 声明的 DLL。")

    if args.check:
        print("\n（--check：未写盘）")
        return 0

    TARGET.mkdir(parents=True, exist_ok=True)

    # ── 覆盖前先备份到 mod 目录之外（备份留在 mod 目录 = 制造一次拒载事故）──
    old_dll = TARGET / BUILD.name
    if old_dll.is_file():
        bak = _backup_path(old_dll)
        try:
            shutil.copy2(old_dll, bak)
            print(f"\n旧 DLL 已备份（mod 目录之外）：{bak}")
        except OSError as e:
            print(f"  提示：备份旧 DLL 失败（不影响部署）：{e}")

    try:
        shutil.copy2(BUILD, TARGET / BUILD.name)
    except OSError as e:
        print(f"\n× 写入 DLL 失败：{e}")
        print("  最常见的原因为「游戏正在运行」——MOD 的 DLL 被游戏进程映射住了，无法覆盖。")
        print("  请先完全退出游戏（不是退回主菜单），再重新执行本脚本。")
        return 3
    shutil.copy2(MANIFEST, TARGET / "manifest.xml")

    migrated = _migrate_runtime_files()
    if migrated:
        print(f"\n运行期文件已迁出 Mods：{', '.join(migrated)}")

    # 预填「启动 Agent」用的命令与工作目录（只在缺失时写，绝不覆盖用户在面板里改过的值）
    seeded = _seed_agent_keys()
    print("\n已部署：")
    for f in sorted(TARGET.iterdir()):
        print(f"  · {f.name}  ({f.stat().st_size} 字节)")
    if seeded:
        print(f"  （model_config.ini 补齐了：{', '.join(seeded)}）")

    # ── 写盘后再自查一次：确保最终状态就是扫描器看到的状态 ──
    payload_after, _ = _scan_target_residue(declared)
    if payload_after:
        print("\n× 部署后目标目录仍有载荷残留（这次是脚本自己造成的？）：")
        for p in payload_after:
            print(f"    · {p.name}")
        return 2

    caps = read_capabilities(MANIFEST)
    print(f"\n申明的能力：{', '.join(caps) if caps else '（无）'}")
    print("下一步（必须人工一次）：启动游戏 → 模组界面 → 启用「DeepSpace Bridge」，"
          "\n  首次启用会列出上面这些能力并请求确认；确认后 mod 才会加载。")
    return 0


def read_assemblies(manifest: Path) -> list[str]:
    """从 manifest 读 ``<Assemblies>`` 声明的 DLL 文件名（部署后允许留在目标目录的）。"""
    import xml.etree.ElementTree as ET

    root = ET.parse(manifest).getroot()
    node = root.find("Assemblies")
    if node is None:
        return []
    return [(a.text or "").strip() for a in node.findall("Assembly") if (a.text or "").strip()]


def read_capabilities(manifest: Path) -> list[str]:
    """从 manifest 读实际申明的能力（别再在提示里写死人肉维护的清单）。"""
    import xml.etree.ElementTree as ET

    root = ET.parse(manifest).getroot()
    node = root.find("Capabilities")
    if node is None:
        return []
    return [(c.text or "").strip() for c in node.findall("Capability") if (c.text or "").strip()]


if __name__ == "__main__":
    sys.exit(main())
