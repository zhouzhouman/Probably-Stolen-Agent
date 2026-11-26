"""部署侧防线：mod 目录里不得出现「扫描器会当成 payload」的杂乱文件（X03 补测）。

事故（R0 实机，2026-11-14）
--------------------------
旧 DLL 被备份成 `DeepSpaceBridge.dll.bak-177152` 留在 mod 目录里，游戏内扫描器
报 `payload-file: DeepSpaceBridge.dll.bak-177152 -> executable content` 并
**直接拒载整个 mod**。症状是「插件明明编译成功、部署脚本也说成功，游戏里就是不出现」，
排查成本极高。

两类防线，本文件各测一条：

1. **仓库侧（可执行，现在就该绿）**：仓库的 `mod/` 目录只能有「源文件 + manifest」，
   任何 `.dll.<后缀>` / `.bak` / `.old` / `.orig` 之类的**载荷型残留**都会被扫描器
   当成可执行内容。备份应放在 `artifacts/mod_backups/` 这类不被部署的目录。

2. **部署脚本侧（行为断言，CR-X03-2 已由 R0 补齐）**：`scripts/deploy_mod.py`
   现在在写盘前扫目标目录，有载荷残留 → 返回 2 拒绝部署；覆盖旧 DLL 前把备份
   送到 `artifacts/mod_backups/`（mod 目录之外）；写盘后再自查一次。
   本文件对 tmp 目录真跑 `main()`，覆盖「有残留要拒」与「没残留要成」两侧。

纪律：`scripts/**` 不在本角色写范围，所以只读、不改；缺口报给 R0。
"""

from __future__ import annotations

import ast
import builtins
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
MOD_DIR = REPO_ROOT / "mod"
DEPLOY_SCRIPT = REPO_ROOT / "scripts" / "deploy_mod.py"

#: 会被游戏内扫描器视为「可执行载荷」的后缀形态。
#: 注意 `.dll.bak-177152` 这种**双重后缀**是最容易漏的：文件本身不是 `.dll`
#: 结尾，但内容就是 DLL 字节，扫描器按内容判定照样拒载。
PAYLOAD_SUFFIXES = (".bak", ".old", ".orig", ".save", ".backup", ".tmp", ".disabled")


def _is_payload_residue(path: Path) -> bool:
    """文件名是否像「被改名的可执行载荷」。

    注意**纯 `.dll` 不算残留** —— `mod/bin/Debug/**/DeepSpaceBridge.dll` 是
    `dotnet build` 的正常产物，仓库里本来就有。真正危险的是**被改名过的**载荷：
    `.dll.bak-177152`（双重后缀）、`x.dll.old`、`y.bak` 等；扫描器按内容判定，
    改成什么名字都照样拒载。
    """
    name = path.name.lower()
    if any(part in name for part in PAYLOAD_SUFFIXES):
        return True
    # ``.dll.bak-177152``：中间出现 .dll. 即视为载荷改名（纯 .dll 结尾不算）
    return ".dll." in name


def test_mod_dir_has_no_payload_residue():
    """GAP: mod 目录混入改名 DLL 会整包被拒载 —— 仓库侧要先自查干净。

    2026-11-14 实机：`DeepSpaceBridge.dll.bak-177152` 留在 mod 目录 → 扫描器报
    `payload-file ... -> executable content` → 整个 mod 拒载。排查时「编译成功、
    部署成功、游戏里没有」三者同时成立，极易误判成代码问题。

    这里扫 `mod/` 全树（含子目录），只允许源文件与 `manifest.xml`：
    合法产物是 `mod/manifest.xml`、`mod/DeepSpaceMod.csproj`、`mod/src/**`。
    备份应放 `artifacts/mod_backups/`（该目录不会被部署），本测试不扫它。
    """
    assert MOD_DIR.is_dir(), f"找不到 mod 目录：{MOD_DIR}"

    offenders = [
        p.relative_to(REPO_ROOT).as_posix()
        for p in MOD_DIR.rglob("*")
        if p.is_file() and _is_payload_residue(p)
    ]
    assert not offenders, (
        "mod/ 目录里有载荷型残留，游戏内扫描器会以 "
        "`payload-file ... -> executable content` 拒载整个 mod：\n  "
        + "\n  ".join(offenders)
        + "\n请把它们移到 artifacts/mod_backups/（该目录不参与部署）"
    )


def test_mod_manifest_is_the_only_assembly_declaration():
    """GAP: manifest 声明的程序集必须唯一 —— 多一个 DLL 就是多一个载荷面。

    `mod/manifest.xml` 的 `<Assemblies>` 是游戏加载的唯一依据。若有人手工往
    mod 目录塞第二个 DLL 并写进 manifest，扫描器会逐个做内容判定，风险面翻倍。
    这里断言声明的程序集恰好一个，且它与 `mod/DeepSpaceMod.csproj` 的
    `AssemblyName` 指向同一个名字。
    """
    import xml.etree.ElementTree as ET

    manifest = MOD_DIR / "manifest.xml"
    assert manifest.is_file(), f"找不到 manifest：{manifest}"
    root = ET.parse(manifest).getroot()

    node = root.find("Assemblies")
    assert node is not None, "manifest 缺少 <Assemblies> 段"
    assemblies = [(a.text or "").strip() for a in node.findall("Assembly")]
    assemblies = [a for a in assemblies if a]
    assert len(assemblies) == 1, (
        f"manifest 应只声明 1 个程序集，实际 {assemblies}；"
        "每多声明一个 DLL，扫描器的内容判定面就多一份"
    )

    csproj = MOD_DIR / "DeepSpaceMod.csproj"
    assert csproj.is_file(), f"找不到 csproj：{csproj}"
    text = csproj.read_text(encoding="utf-8-sig")
    # manifest 写的是文件名（``DeepSpaceBridge.dll``），csproj 写的是程序集名
    # （``DeepSpaceBridge``）—— 比对时去掉 .dll 后缀。
    declared = assemblies[0]
    if declared.lower().endswith(".dll"):
        declared = declared[:-4]
    assert "AssemblyName" in text, "csproj 未显式声明 AssemblyName"
    assert f"<AssemblyName>{declared}</AssemblyName>" in text, (
        f"manifest 声明的程序集 {declared!r} 与 csproj 的 AssemblyName 不一致"
    )


def _load_deploy_mod():
    """把 `scripts/deploy_mod.py` 当模块加载（`scripts/` 不是包，不能直接 import）。

    `main()` 里所有路径都是模块级常量（`BUILD` / `MANIFEST` / `GAME` / `TARGET`），
    所以加载后 monkeypatch 这几个名字就能把整条部署流程指到 tmp 目录 —— 不碰实机。
    """
    import importlib.util

    assert DEPLOY_SCRIPT.is_file(), f"找不到部署脚本：{DEPLOY_SCRIPT}"
    spec = importlib.util.spec_from_file_location("_r7_deploy_mod", DEPLOY_SCRIPT)
    assert spec and spec.loader, f"无法加载 {DEPLOY_SCRIPT}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_MANIFEST_XML = (
    '<?xml version="1.0" encoding="utf-8"?>\n'
    "<ModManifest>\n"
    "  <Assemblies><Assembly>DeepSpaceBridge.dll</Assembly></Assemblies>\n"
    "  <Capabilities><Capability>GameModify</Capability></Capabilities>\n"
    "</ModManifest>\n"
)


@pytest.fixture()
def deploy_env(tmp_path, monkeypatch):
    """造一套假部署环境：源 DLL / manifest / 游戏目录 / 目标 Mods 目录。

    全部落在 `tmp_path` 里，`main()` 无论怎么跑都不会写到真实游戏目录，也不会把
    备份塞进真实 `artifacts/mod_backups/`。`LOCALAPPDATA` 一并改掉，因为
    `_seed_agent_keys()` 会往用户数据目录写 `model_config.ini`。

    `BACKUP_DIR` 也要换（`scripts/deploy_mod.py:33` 的模块级常量，R0 专为可测性提取）：
    不换的话，目标目录里只要有旧 DLL，部署就会往**真实仓库**里写备份文件。
    """
    module = _load_deploy_mod()

    build = tmp_path / "build" / "DeepSpaceBridge.dll"
    build.parent.mkdir(parents=True)
    build.write_bytes(b"NEW-DLL-BYTES-X03")

    manifest = tmp_path / "manifest.xml"
    manifest.write_text(_MANIFEST_XML, encoding="utf-8")

    game = tmp_path / "game"
    game.mkdir()
    target = game / "Mods" / "DeepSpaceBridge"
    backup_dir = tmp_path / "mod_backups"

    monkeypatch.setattr(module, "BUILD", build, raising=True)
    monkeypatch.setattr(module, "MANIFEST", manifest, raising=True)
    monkeypatch.setattr(module, "GAME", game, raising=True)
    monkeypatch.setattr(module, "TARGET", target, raising=True)
    monkeypatch.setattr(module, "BACKUP_DIR", backup_dir, raising=True)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localappdata"))
    monkeypatch.setattr("sys.argv", ["deploy_mod.py"])

    return {
        "module": module,
        "build": build,
        "manifest": manifest,
        "target": target,
        "backup_dir": backup_dir,
    }


def test_deploy_script_rejects_extra_files_in_target(deploy_env):
    """GAP: 部署脚本必须自己挡住「目标目录里的载荷残留」（CR-X03-2，实机拒载回归）。

    实机事故链（2026-11-14）：旧 DLL 被改名备份成
    `DeepSpaceBridge.dll.bak-177152` 留在 `Mods/DeepSpaceBridge/` 里 → 游戏内
    扫描器报 `payload-file ... -> executable content` → **整包拒载**。症状是
    「编译成功、部署脚本也说成功、游戏里就是不出现」，排查成本极高。

    这里跑一次**真实**的 `main()`（所有路径指向 tmp，不碰实机），断言两件事：

      1. 返回码是 **2**（拒绝部署）——不是 0，也不是异常；
      2. **残留文件仍在原地** —— 「拒绝」不等于「偷偷删掉」。若脚本悄悄删了它，
         用户会以为备份还在，其实是脚本吃掉了，比拒载更危险。

    注意 `--check` 走不到这条防线（它在写盘前就 return 0 了），所以必须真跑。
    """
    module = deploy_env["module"]
    target = deploy_env["target"]
    target.mkdir(parents=True)
    (target / "manifest.xml").write_text(_MANIFEST_XML, encoding="utf-8")
    (target / "DeepSpaceBridge.dll").write_bytes(b"OLD-DLL")
    residue = target / "DeepSpaceBridge.dll.bak-177152"
    residue.write_bytes(b"OLD-DLL")

    rc = module.main()

    assert rc == 2, (
        f"目标目录里有载荷残留时 main() 必须以 2 拒绝部署，实际返回 {rc}"
        "（返回 0 会让『部署成功』与『游戏里不出现』同时成立）"
    )
    assert residue.is_file(), (
        "拒绝部署时必须把残留文件**留在原地** —— 偷偷删掉会让人以为备份还在，"
        "实际上是脚本吃掉了"
    )
    assert (target / "DeepSpaceBridge.dll").read_bytes() == b"OLD-DLL", (
        "拒绝部署时不得覆盖目标目录里的旧 DLL"
    )
    assert deploy_env["build"].read_bytes() not in (
        (target / "DeepSpaceBridge.dll").read_bytes()
    ), "拒绝部署时源 DLL 不得被写入目标目录"


def test_deploy_script_happy_path_succeeds(deploy_env):
    """GAP: 没有残留的**正常部署路径**必须跑通 —— 防线不能挡住自己。

    这条和上一条是互补的：上一条守「有残留要拒」，这条守「没残留要成」。
    只测前者，会让「拒载防线把整条部署流程一起挡死」也看不出来。

    回归背景（CR-X03-3，R0 已修）：`scripts/deploy_mod.py` 的 `_backup_path()`
    曾在 `return` 之后残留 `_migrate_runtime_files` 的 docstring 与函数体，
    `def _migrate_runtime_files() -> list[str]:` 那一行丢失，于是 `main()` 第 247 行
    调用它时抛 `NameError` —— 正常部署路径直接崩，而 `--check` 在写盘前就
    `return 0`、拒载分支也在第 247 行之前 `return 2`，两者**都测不出**。
    本用例就是补这个盲区：真的跑一遍 `main()`，走到底。

    同时覆盖「覆盖旧 DLL 前要备份到 mod 目录**之外**」：目标目录里预置一个旧 DLL，
    备份必须落在 `BACKUP_DIR`（测试里指向 tmp），绝不能在目标目录留 `.bak`。
    """
    module = deploy_env["module"]
    target = deploy_env["target"]
    target.mkdir(parents=True)
    (target / "manifest.xml").write_text(_MANIFEST_XML, encoding="utf-8")
    (target / "DeepSpaceBridge.dll").write_bytes(b"OLD-DLL")

    rc = module.main()

    assert rc == 0, f"正常部署路径应返回 0，实际 {rc}"
    assert (target / "DeepSpaceBridge.dll").read_bytes() == deploy_env["build"].read_bytes(), (
        "部署成功时源 DLL 必须被拷进目标目录（覆盖旧 DLL）"
    )
    # 备份必须落在 mod 目录之外（mod 目录里任何 DLL 字节文件都会被扫描器拒载）
    leftovers = sorted(p.name for p in target.iterdir())
    assert not [n for n in leftovers if ".bak" in n or ".dll." in n], (
        f"部署后目标目录不得留有载荷残留，实际 {leftovers}"
    )
    backups = sorted(p.name for p in deploy_env["backup_dir"].glob("*"))
    assert backups, "覆盖旧 DLL 前必须备份（备份目录里一个文件都没有）"
    assert all("DeepSpaceBridge.dll" in n for n in backups), (
        f"备份文件名应基于被覆盖的 DLL，实际 {backups}"
    )


def test_deploy_module_exposes_every_function_it_calls():
    """GAP: 部署脚本调用的每个自家函数都必须真的定义过（CR-X03-3 的静态盲区）。

    CR-X03-3 的形态很特殊：`_backup_path()` 的 `return` 之后残留了另一个函数的
    docstring 与函数体，`def _migrate_runtime_files(...)` 那一行丢失——**Python 不报错**
    （`return` 之后的语句合法，只是永远不执行），于是那个名字根本不存在，
    而 `main()` 仍在第 247 行调用它。语法错至少在 import 时就炸，这种静默死代码不会。

    所以这条不做行为测试，直接对账「被调用的名字」与「模块里定义/导入的名字」：
    任何被调用但既没定义也没导入的名字都会现形，且**不依赖能不能走到那段代码**。

    用 `ast` 而不是 `hasattr` 是刻意的：`hasattr` 只能验证「模块里有没有」，
    验不出「那个函数是不是被写成了另一个函数体内的死代码」——
    这里要的正是「源码里有没有 `def`」这个事实。
    """
    tree = ast.parse(DEPLOY_SCRIPT.read_text(encoding="utf-8-sig"))

    defined = {
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    # 模块级赋值/导入的名字（`from pathlib import Path` → Path；`import shutil` → shutil）
    bound: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                bound.add((alias.asname or alias.name).split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                bound.add(alias.asname or alias.name)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for t in targets:
                if isinstance(t, ast.Name):
                    bound.add(t.id)
    # 函数形参/局部变量也算「合法名字」，避免把同名局部调用误判
    local_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = node.args
            for a in (*args.posonlyargs, *args.args, *args.kwonlyargs):
                local_names.add(a.arg)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            local_names.add(node.id)

    called = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    missing = sorted(
        n
        for n in called - defined - bound - local_names
        if not hasattr(builtins, n)
    )

    assert not missing, (
        f"deploy_mod.py 调用了既未定义也未导入的名字：{missing} —— "
        "典型成因是编辑时把某行的 `def xxx():` 整行删掉，函数体变成上一个函数里的"
        "死代码（Python 不报 IndentationError，只在真正运行到那行时 NameError）"
    )
