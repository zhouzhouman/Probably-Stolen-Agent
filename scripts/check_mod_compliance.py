"""mod 平台合规离线自检（T01）。

目的：在**不启动游戏、不联网**的前提下，把官方 Modding 规则里「会在 2026-12-10
被硬拒绝」的东西在源码层面先扫一遍，让 `dt` 之前就能知道 mod 还差什么。

它不能替代游戏内 `mod-scan <id>`：真正的判定发生在游戏里，靠 Mono.Cecil 读 IL，
能穿透单次赋值的局部变量；这里只做正则近似。所以本脚本的定位是
「快速回归 + 把已知的坑固定在列表里」，V5（游戏内 mod-scan）仍然是唯一权威。

用法：
    python scripts/check_mod_compliance.py           # 常规自检，FAIL 时退出码 1
    python scripts/check_mod_compliance.py --strict  # WARN 也算失败（R2 改完后用）
    python scripts/check_mod_compliance.py -v        # 打印每条命中所在的整行源码
    python scripts/check_mod_compliance.py --json    # 机器可读输出（CI 用）
    python scripts/check_mod_compliance.py --baseline artifacts/mod_compliance_baseline.json
                                                     # 只报「比基线新增」的违规（不许变差闸门）
    python scripts/check_mod_compliance.py --write-baseline artifacts/mod_compliance_baseline.json
                                                     # 重写基线快照（清完一批违规后手动执行）

退出码：
    0 = PASS；1 = FAIL；2 = 环境错误（找不到 manifest / 源码目录）。
    **带 --baseline 时口径改变**：0 = 与基线相比没有新增违规（已知旧问题不算红），
    1 = 出现了基线里没有的新违规、或新出现了未声明能力。这是「不许变差」的回归闸门，
    不等于合规达标 —— 合规达标要看「不带 --baseline 且带 --strict」跑绿。

基线身份（key）是怎么算的：`文件|规则名|能力|归一化源码行`，**不含行号**。
这样 R2 在别处加几行代码导致整片行号平移时不会误报「新增」；同一文件里逐字相同的
重复命中靠 `#1`/`#2` 序号区分。

规则来源（核对日期 2026-11-13）：
    https://github.com/dragonpan2/probably-stolen-modding/blob/main/ModdingGuide.md
    https://github.com/dragonpan2/probably-stolen-modding/blob/main/APIChanges.md
另外用 System.Reflection.Metadata 直接读了本机
`D:\\SteamLibrary\\steamapps\\common\\Probably Stolen\\Probably Stolen_Data\\Managed\\Assembly-CSharp.dll`
（构建时间 2026-11-09）里的 ModCapability / ModScanner 元数据做交叉核对，
下面各条注释里的「元数据」指的就是这次核对。**本脚本自身不做任何网络访问。**
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import re
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import date, datetime, timezone, timedelta
from pathlib import Path

# --------------------------------------------------------------------------- #
# 常量：能力清单（官方规则 + 本机元数据交叉核对）
# --------------------------------------------------------------------------- #

SOURCE_MODDING_GUIDE = (
    "https://github.com/dragonpan2/probably-stolen-modding/blob/main/ModdingGuide.md"
)
SOURCE_API_CHANGES = (
    "https://github.com/dragonpan2/probably-stolen-modding/blob/main/APIChanges.md"
)
RULES_VERIFIED_ON = "2026-11-13"
DEPRECATION_HARD_DATE = date(2026, 12, 10)

#: 官方 ModdingGuide.md「Declaring capabilities」表里列出的能力名。
#: 元数据核对：ModCapability 的静态字段正是这 9 个（cctor 里逐个 push），
#: 名称与字符串值一一对应，见 ModCapability.Files/Network/Process/DynamicCode/
#: Native/ReflectionExternal/Steam/SteamAuth/Threads。
KNOWN_CAPABILITIES: dict[str, str] = {
    "files": "读写 mod 目录以外的文件（官方列出的 DseCecil / CsvExport.ExportToFile / JsonUtils 也算 files）",
    "network": "任何出站网络连接",
    "process": "仅 Application.OpenURL + 字面 http(s) 地址；Process/ProcessStartInfo 永不许可",
    "steam": "Steam 相关调用中允许的那部分",
    "steam-auth": "Steam 身份/鉴权相关调用",
    "dynamic-code": "Assembly.Load* / Reflection.Emit / 表达式树编译 / BinaryFormatter 等",
    "native": "DllImport / unsafe / calli / Marshal / UnsafeUtility",
    "reflection-external": "扫描器读不懂目标（或目标非游戏代码）的反射与补丁",
    "threads": "new Thread / ThreadPool / Task.Run / Timer / Parallel / ContinueWith 等",
}

#: 指南里出现、但**未能在本机 Assembly-CSharp.dll 元数据中核实**的能力名：
#: ModCapability 的 cctor 只 push 上面 9 个，没有 window；ModWindowControl 类型存在。
#: 因此这里单列，声明了也只是 WARN，不当作已知能力。
UNVERIFIED_CAPABILITIES = {
    "window": "指南「Declaring capabilities」表里出现过（窗口/多显示器），"
              "但本机 ModCapability 元数据无此字段 → 未核实",
}

#: 2026-12-10 起被**直接拒绝**的四个能力（引用 ModdingGuide.md:1455-1457、:1494、
#: 「DEPRECATED 行应视同 hard flag」、「Activation is refused: declares capabilities
#: mods can no longer use」）。
DEPRECATED_CAPABILITIES = {
    "dynamic-code",
    "native",
    "reflection-external",
    "threads",
}

#: 允许被反射/补丁的「免费」目标前缀。元数据核对：
#: ModScanner.ReflectionAllowedExternalPrefixes.cctor 只 push 了 3 个 ldstr ——
#: "UnityEngine.UI." / "TMPro." / "UnityEngine.EventSystems."，另有空串与 Facades 目录判定。
FREE_NAMESPACE_PREFIXES = (
    "UnityEngine.UI",
    "TMPro",
    "UnityEngine.EventSystems",
    "Febucci.",
    "DG.Tweening",
    "DG.DemiLib",
)

#: 游戏自带的第三方库：调用其公开 API 免费，但补丁/无名反射在 12-10 起算
#: reflection-external（ModdingGuide.md:1463）。
SHIPPED_THIRD_PARTY = (
    "Newtonsoft.Json",
    "QFSW.QC",
    "RNGNeeds",
    "DseCecil",
    "CsvExport",
    "StarphaseTools.Core",
)

#: 扫描器 GuardTypes（元数据实测 15 条）：补丁或反射瞄准这些类型直接硬拒。
GUARD_TYPES = (
    "ModLoader",
    "ModTrust",
    "ModTrustRecord",
    "ModFingerprint",
    "ModTrustList",
    "ModScanner",
    "ModCapability",
    "ModSecurityDebug",
    "ModSteamRules",
    "ModSteamGuard",
    "ModSteamAuth",
    "ModWorkshop",
    "SteamManager",
    "PaperBookRegistry",
    "ModReflect",
)

#: ModHelper 里 mod 侧**能**调用的后台 API（元数据实测签名，2026-11-09 构建）：
#:   Coroutine StartCoroutine(String, IEnumerator)
#:   Void      StopCoroutine(String, Coroutine)
#:   Void      StopAllCoroutines(String)
#:   Coroutine Delay(String, Single, Action)
#:   Boolean   RunInBackground(String, Action<CancellationToken>, Action)
#:   Void      RunOnMainThread(String, Action)
#: DrainMainThread / StopBackgroundWork 是 Assembly 可见性，mod 调不到。
MODHELPER_BACKGROUND_API = {
    "StartCoroutine": "Coroutine StartCoroutine(String, IEnumerator)",
    "StopCoroutine": "Void StopCoroutine(String, Coroutine)",
    "StopAllCoroutines": "Void StopAllCoroutines(String)",
    "Delay": "Coroutine Delay(String, Single, Action)",
    "RunInBackground": "Boolean RunInBackground(String, Action<CancellationToken>, Action)",
    "RunOnMainThread": "Void RunOnMainThread(String, Action)",
}

ROOT = Path(__file__).resolve().parent.parent
MANIFEST_PATH = ROOT / "mod" / "manifest.xml"
SRC_DIR = ROOT / "mod" / "src"
BUILD_DIR = ROOT / "mod" / "bin" / "Debug" / "netstandard2.1"
# 只用于在报告里注明「规则来自哪个游戏构建」——本脚本从不读它，更不加载它。
GAME_ASSEMBLY = (
    Path(r"D:\SteamLibrary\steamapps\common\Probably Stolen")
    / "Probably Stolen_Data" / "Managed" / "Assembly-CSharp.dll"
)


# --------------------------------------------------------------------------- #
# 扫描规则
# --------------------------------------------------------------------------- #

@dataclass
class Rule:
    """一条源码扫描规则。severity=FAIL 会让脚本以退出码 1 结束。"""

    name: str
    pattern: re.Pattern[str]
    capability: str
    severity: str            # "FAIL" | "WARN"
    why: str


def _r(name: str, pattern: str, capability: str, severity: str, why: str) -> Rule:
    return Rule(name, re.compile(pattern), capability, severity, why)


# threads：指南 1461 行逐项列出的构造，12-10 起一律硬拒。
THREAD_RULES = [
    _r("new Thread", r"\bnew\s+Thread\s*\(", "threads", "FAIL",
       "指南 1461：改用 ModHelper.RunInBackground / StartCoroutine / Delay"),
    _r("ThreadPool", r"\bThreadPool\s*\.", "threads", "FAIL",
       "指南 1461：QueueUserWorkItem 在封锁清单里"),
    _r("Task.Run", r"\bTask\s*\.\s*Run\s*\(", "threads", "FAIL",
       "指南 1461：改用 ModHelper.RunInBackground"),
    _r("Task.Factory", r"\bTask\s*\.\s*Factory\s*\.\s*StartNew", "threads", "FAIL",
       "指南 1461"),
    _r("Task.Delay", r"\bTask\s*\.\s*Delay\s*\(", "threads", "FAIL",
       "指南 1461：改用 ModHelper.Delay（协程，不建线程）"),
    _r("new Task", r"\bnew\s+Task\s*[<(]", "threads", "FAIL", "指南 1461"),
    _r("ContinueWith", r"\.\s*ContinueWith\s*\(", "threads", "FAIL", "指南 1461"),
    _r("Parallel", r"\bParallel\s*\.\s*(?:For|ForEach|Invoke)\b", "threads", "FAIL",
       "指南 1461"),
    _r("System.Threading.Timer", r"\bnew\s+System\s*\.\s*Threading\s*\.\s*Timer\b",
       "threads", "FAIL", "指南 1461"),
    _r("System.Timers.Timer", r"\bnew\s+System\s*\.\s*Timers\s*\.\s*Timer\b",
       "threads", "FAIL", "指南 1461"),
    _r("new Timer", r"\bnew\s+Timer\s*\(", "threads", "WARN",
       "指南 1461：System.Threading.Timer / System.Timers.Timer 均算 threads；需人工确认"),
    # 对**别人的** MonoBehaviour 调 StartCoroutine/Invoke/InvokeRepeating 也算 threads；
    # 自己组件内 this.* 免费 —— 静态文本分不清，只报 WARN 交人工看。
    _r("external StartCoroutine", r"(?<!this)\.\s*StartCoroutine\s*\(", "threads", "WARN",
       "指南 1461：对非自己组件调 StartCoroutine 算 threads，自己组件内 this.* 免费 —— 需人工确认"),
    _r("Invoke/InvokeRepeating", r"\.\s*Invoke(?:Repeating)?\s*\(", "threads", "WARN",
       "指南 1461：对非自己组件调 MonoBehaviour.Invoke/InvokeRepeating 算 threads —— 需人工确认"),
]

# 已知的**不算** threads 的形态，扫描前先抹掉，避免误报：
# `b.onClick.Invoke()` 调的是 UnityEngine.Events.UnityEvent.Invoke（同步立即触发监听器），
# 不是 MonoBehaviour.Invoke/InvokeRepeating（那才是「游戏把你关掉后还在跑」的构造）。
# 指南 1461 的 threads 清单只点名 StartCoroutine / Invoke / InvokeRepeating on a MonoBehaviour。
# 本仓库 4 处 `onClick.Invoke()`（BridgeApi.cs:1805/4117/4577/4585）接收者都是游戏的
# UnityEngine.UI.Button（FindButton 的返回类型，见 BridgeApi.cs:4527），不是本 mod 的组件，
# 但 UnityEvent.Invoke 本身不在任何能力清单里，故不算违规。
NOT_THREADS_PATTERNS = [
    ("UnityEvent.onClick.Invoke()", re.compile(r"\bonClick\s*\.\s*Invoke\s*\(")),
    # Harmony Transpiler 需要 OpCodes 枚举（System.Reflection.Emit）来描述 IL 操作码，
    # 但只**引用枚举**不等于运行期生成代码（dynamic-code 的真值是 AssemblyBuilder /
    # ILGenerator.Emit / DynamicMethod，见 DYNAMIC_CODE_RULES 其余各条）。
    # 这一条只抹掉 `using ...Reflection.Emit;` 引入行，不放宽任何实际 API 调用。
    ("Reflection.Emit using 指令", re.compile(
        r"^\s*using\s+(?:System\s*\.\s*)?Reflection\s*\.\s*Emit\s*;")),
    # [HarmonyPatch] 无参属性本身不指定目标；本项目唯一一处（LegacyInputPatch.cs）
    # 由 TargetMethods() 返回 typeof(InputActionManager) 所在的 Assembly-CSharp.dll 方法，
    # 官方 ModdingGuide.md:1627 明确「补丁游戏自身代码免费」，不占 reflection-external。
    # 已人工核实：全仓库仅此一处 HarmonyPatch，目标为游戏自身类型，非 guard/第三方库。
    # 若将来新增 HarmonyPatch 指向别处，此掩码会一并放行 —— 故新增时必须人工复核并收紧本规则。
    ("游戏自身类型 HarmonyPatch 属性", re.compile(r"^\s*\[\s*HarmonyPatch\s*\]\s*$")),
    # MenuFocusPatch.TargetMethods 用字面类型 + 字面成员名查 UnityEngine.UI 的三个方法，
    # 目标为游戏自带 UI（官方 ModdingGuide.md:1463 明确免费），非 guard/第三方库。
    # 只豁免这三行字面调用，不放宽任何变量成员名/运行时拼名字的 computed 反射。
    # 注：掩码作用于「抹掉字符串后的净版」，故这里不写成员名字面量，
    # 靠精确前缀 typeof(StandaloneInputModule).GetMethod( 限定；三行成员名由人工核对
    # （UpdateModule / ActivateModule / Process，见源码），不是任意 computed 反射。
    ("字面游戏类型 GetMethod", re.compile(
        r'typeof\s*\(\s*StandaloneInputModule\s*\)\s*\.\s*GetMethod\s*\(')),
]

# process：**永不**许可，与能力声明无关。
PROCESS_RULES = [
    _r("Process.Start", r"\bProcess\s*\.\s*Start\b", "process", "FAIL",
       "指南 hard refusals：DLL 里出现 Process.Start 直接整包拒绝（这是当前实现刻意避开的路径，"
       "AgentLauncher 走信号文件）"),
    _r("ProcessStartInfo", r"\bProcessStartInfo\b", "process", "FAIL",
       "指南 hard refusals：与 Process.Start 同罪"),
    _r("Process 类型引用", r"\bSystem\s*\.\s*Diagnostics\s*\.\s*Process\b", "process", "FAIL",
       "指南 hard refusals"),
]

# dynamic-code：12-10 起硬拒（指南 1455）。
DYNAMIC_CODE_RULES = [
    _r("Assembly.Load*", r"\bAssembly\s*\.\s*Load(?:From|File|Bytes|Module)?\s*\(",
       "dynamic-code", "FAIL", "指南 1455：Assembly.Load*"),
    _r("AppDomain.Load", r"\bAppDomain\s*\.\s*\w+\s*\.\s*Load\s*\(", "dynamic-code", "FAIL",
       "指南 1455：AppDomain.Load"),
    _r("Reflection.Emit", r"\bReflection\s*\.\s*Emit\b|\bSystem\s*\.\s*Reflection\s*\.\s*Emit\b",
       "dynamic-code", "FAIL", "指南 1455"),
    _r("DynamicMethod", r"\bDynamicMethod\b", "dynamic-code", "FAIL", "指南 1455"),
    _r("TypeBuilder/AssemblyBuilder",
       r"\b(?:TypeBuilder|AssemblyBuilder|ModuleBuilder|MethodBuilder)\b",
       "dynamic-code", "FAIL", "指南 1455"),
    _r("Activator.CreateInstanceFrom", r"\bActivator\s*\.\s*CreateInstanceFrom\b",
       "dynamic-code", "FAIL", "指南 1455"),
    _r("Expression.Compile", r"\.\s*Compile\s*\(\s*\)", "dynamic-code", "FAIL",
       "指南 1455：编译表达式树算 dynamic-code，替代物是 ModReflect.Method<TDelegate>"),
    _r("BinaryFormatter 等",
       r"\b(?:BinaryFormatter|NetDataContractSerializer|LosFormatter|ObjectStateFormatter|SoapFormatter)\b",
       "dynamic-code", "FAIL", "指南 1455：Formatters 反序列化算 dynamic-code"),
    _r("CSharpScript/Roslyn",
       r"\bCSharpScript\b|\bMicrosoft\s*\.\s*CodeAnalysis\b|\bCSharpCodeProvider\b|\bVBCodeProvider\b",
       "dynamic-code", "FAIL",
       "指南 1455：运行期编译源码 = dynamic-code；本仓库不引入脚本引擎"),
    _r("dynamic 关键字", r"\bdynamic\s+[A-Za-z_]", "dynamic-code", "FAIL",
       "指南 1457：C# dynamic 关键字算 reflection-external；1455 起 dynamic-code 也拒"),
]

# native：12-10 起硬拒（指南 1456）。
NATIVE_RULES = [
    _r("DllImport", r"\bDllImport\b", "native", "FAIL", "指南 1456"),
    _r("unsafe", r"\bunsafe\b", "native", "FAIL", "指南 1456（csproj 里 AllowUnsafeBlocks 仅为兼容历史，源码不得用）"),
    _r("Marshal", r"\bMarshal\s*\.", "native", "FAIL", "指南 1456"),
    _r("stackalloc", r"\bstackalloc\b", "native", "FAIL", "指南 1456"),
    _r("fixed 语句", r"\bfixed\s*\(", "native", "FAIL", "指南 1456"),
    _r("calli", r"\bcalli\b", "native", "WARN", "指南 1456"),
]

# reflection-external：区分「扫描器读得懂」与「读不懂」。
# 读得懂 = typeof(字面类型) 或字面类型名 + 字面成员名 → 免费（目标是游戏自身类型时）。
REFLECTION_FAIL_RULES = [
    _r("Type.GetType", r"\bType\s*\.\s*GetType\s*\(", "reflection-external", "FAIL",
       "指南 1457：Type.GetType 在 System./UnityEngine./HarmonyLib./Steamworks./ES3 下算 "
       "reflection-external；仅字面游戏类型名免费 —— 需人工确认，默认按 FAIL 处理"),
    _r("Assembly.GetType/GetTypes", r"\bAssembly\s*\.\s*(?:GetType|GetTypes)\b",
       "reflection-external", "FAIL", "指南 1457"),
    _r("AccessTools/Traverse", r"\b(?:AccessTools|Traverse)\s*\.", "reflection-external",
       "FAIL", "指南 1457；游戏自身类型 + 字面成员名才免费"),
    _r("Harmony 补丁/反射辅助",
       r"\[?\s*HarmonyPatch\b|\bHarmony\s*\.\s*(?:Unpatch|Patch|Create)",
       "reflection-external", "WARN",
       "指南 1627：补丁游戏自身代码免费；补丁 System./UnityEngine./第三方库算 "
       "reflection-external —— 需人工确认目标"),
    _r("UnpatchAll 无 id", r"UnpatchAll\s*\(\s*(?:null|\"\*\")\s*\)", "reflection-external",
       "FAIL", "指南 hard refusals：UnpatchAll(null) 或 id=\"*\" 直接硬拒"),
    _r("COM 激活",
       r"\b(?:GetTypeFromProgID|GetTypeFromCLSID|Marshal\s*\.\s*GetActiveObject|BindToMoniker)\b",
       "reflection-external", "FAIL", "指南 1479：COM 激活算 reflection-external 且 12-10 拒"),
    _r("TypeNameHandling 非 None",
       r"TypeNameHandling\s*\.\s*(?!None\b)\w+", "dynamic-code", "FAIL",
       "指南 1479：Newtonsoft TypeNameHandling≠None 算 dynamic-code"),
]

# computed receiver：`someObject.GetType()` 或运行期拼的成员名 → 扫描器读不懂 = reflection-external。
# 这类点在仓库里目前存在于 R2 独占文件（GameAccess.cs / BridgeApi.cs），
# 已作为变更请求 CR-T01-2 提交，故脚本按 WARN 报出并注明归属，不阻断 PASS。
#
# 精度说明（对着指南 1457/1463 逐字读）：
#   1457 触发清单是「`Type.GetType`、`GetMethod` / `GetField` / `GetProperty` /
#   `InvokeMember`、`Assembly.GetType` / `GetTypes`、`AccessTools`/`Traverse` with a
#   **computed** name or type（`someObject.GetType()`、运行期拼的字符串）」——
#   `someObject.GetType()` 是作为「computed type 的例子」出现的，落点是**拿它去查成员**；
#   1463 的改法建议「prefer ... `typeof(X)` over `someObject.GetType()`」同理。
#   所以脚本分三级：
#     ① 查表发生在同一行（`.GetType().GetField(` / `X.GetType()` 当实参）→ WARN（真实违规）
#     ② 成员按（可能运行期才知道的）名字查表 → WARN（真实违规）
#     ③ 只取类型名（`obj.GetType().Name`）→ INFO（列出供人工/实机 `mod-scan` 核对，不算违规）
REFLECTION_COMPUTED_RULES = [
    _r("computed receiver (GetType() 进反射查表)",
       r"\.\s*GetType\s*\(\s*\)\s*(?:\.\s*(?:GetField|GetFields|GetMethod|GetMethods|"
       r"GetProperty|GetProperties|GetMember|GetMembers|GetConstructor|GetConstructors|"
       r"InvokeMember)\s*\(|[,)])",
       "reflection-external", "WARN",
       "指南 1457/1463：把 someObject.GetType() 当类型去做成员查找就是 computed → "
       "改成 typeof(字面游戏类型)，或整段改用 ModReflect.Get/Set/Call（无需能力）"),
    _r("运行时按名字查成员", r"\.\s*(?:GetField|GetMethod|GetProperty|InvokeMember)\s*\(",
       "reflection-external", "WARN",
       "指南 1457/1463：成员名不是字面量、或类型不是 typeof(字面游戏类型) 时算 computed；"
       "ModReflect.Get/Set/Call 是官方给的免能力替代"),
]

# 只取类型名：不算违规，但要列出来让人对一遍（尤其 12-10 前用 `mod-scan` 实机确认）。
REFLECTION_INFO_RULES = [
    _r("GetType() 仅用于取类型名", r"\.\s*GetType\s*\(\s*\)", "reflection-external", "INFO",
       "指南 1457 把 computed 的落点定在「拿它去查成员」；仅 obj.GetType().Name 没有查表。"
       "但扫描器口径只能实机 `mod-scan` 确认，故按 INFO 列出"),
]

# 其余 hard refusal。
HARD_REFUSAL_RULES = [
    # 精度说明（2026-11-14 按 ModScanner/Pass 的 IL 逐分支实测后收紧）：
    #   GuardTypes 只在 Pass.ClassifyTypeReference / Pass.ClassifyLiteralTypeName 里被查——
    #   也就是「这个 Type 被拿去查成员了」才判 loader-target。
    #   直接调用 `ModReflect.Get(target, "member")` 的接收者是 ModReflect 类本身、
    #   是普通静态调用，根本不进反射分类分支（Pass.TypeMemberLookups 只匹配
    #   接收者类型 == System.Type 的调用），因此**合法且免能力**——
    #   游戏 DLL 内明文写着 "Replace computed reflection with typeof(...) or ModReflect"。
    #   所以这里只匹配「拿 guard 类型当反射目标」的三种写法：
    #     ① typeof(ModReflect) / typeof(ModLoader) 等
    #     ② 反射语境里出现的字符串字面量 "ModReflect"（"ModReflect" 后紧跟 . 或 ) 或 ,）
    #     ③ HarmonyPatch(typeof(...)) / AccessTools.Method(typeof(...)) 等
    _r("补丁/反射瞄准 guard 类型",
       r"typeof\s*\(\s*(?:" + "|".join(GUARD_TYPES) + r")\s*\)"
       r"|\b(?:HarmonyPatch|AccessTools|Traverse)\b[^\n]*\b(?:"
       + "|".join(GUARD_TYPES) + r")\b",
       "loader-target", "FAIL",
       "指南 1476 + Pass 的 IL 实测：把 ModLoader/ModTrust/ModScanner/ModReflect 等 guard 类型"
       "**当反射目标**（typeof(...) 去查成员、或 HarmonyPatch/AccessTools 瞄准它们）直接硬拒；"
       "**直接调用** ModReflect.Get/Set/Call 合法且免能力"),
    _r("反射语境里的 guard 类型字符串",
       r"\"(?:" + "|".join(GUARD_TYPES) + r")\"\s*(?:\)|,|;|\.\s*(?:Get|Set|Call)\w*\s*\()",
       "loader-target", "WARN",
       "指南 1476：以字符串形式把 guard 类型喂给反射查表（GetType/GetMethod 等）会判 loader-target"
       "—— 需人工确认该字符串确实进了反射调用"),
    _r("ES3 写 modTrust", r"\bmodTrust\b", "files", "FAIL",
       "指南 1476：写 modTrust 等信任层键是硬拒"),
    _r("Workshop/SteamCloud 写",
       r"\b(?:WorkshopPublish|SteamCloud|SteamRemoteStorage|Workshop\s*\.\s*CreateItem)\b",
       "steam", "FAIL", "指南 hard refusals：Workshop / SteamCloud 写入"),
    _r("桌面浏览器 profile / Steam 登录 / Discord / 钱包 字符串",
       r"(?i)\b(?:loginusers\.vdf|ssfn\d|discord|wallet\.dat|Keystore)\b", "reflection-external",
       "WARN", "指南 hard refusals：指向这些目标的字符串常量属硬拒特征 —— 需人工确认"),
    _r("QFSW.QC 调用", r"\bQFSW\s*\.\s*QC\b", "reflection-external", "WARN",
       "指南 1485：12-10 起 QFSW.QC* 调用算 reflection-external（仅 Instance/Activate/"
       "Deactivate/Toggle/LogToConsole/ClearConsole 例外）"),
    _r("DseCecil/CsvExport/JsonUtils", r"\bDseCecil\b|\bCsvExport\s*\.\s*ExportToFile\b|\bJsonUtils\b",
       "files", "WARN", "指南 1485：这三个文件辅助算 files"),
]

ALL_RULES: list[Rule] = (
    THREAD_RULES
    + PROCESS_RULES
    + DYNAMIC_CODE_RULES
    + NATIVE_RULES
    + REFLECTION_FAIL_RULES
    + REFLECTION_COMPUTED_RULES
    + REFLECTION_INFO_RULES
    + HARD_REFUSAL_RULES
)


# --------------------------------------------------------------------------- #
# 源码预处理：把注释与字符串字面量抹成空格（保留行号）
# --------------------------------------------------------------------------- #
# 为什么必须抹：扫描器读的是 IL，注释与字符串永远不会被当成调用；
# 而本项目在注释里大量引用 `new Thread` / `ThreadPool.QueueUserWorkItem` 作说明，
# 不抹掉就会整片误报。抹成等长空格是为了让行号不变。

def strip_comments_and_strings(text: str) -> str:
    out = list(text)
    i = 0
    n = len(text)

    def blank(a: int, b: int) -> None:
        for k in range(a, min(b, n)):
            if out[k] != "\n":
                out[k] = " "

    while i < n:
        c = text[i]
        nxt = text[i + 1] if i + 1 < n else ""
        # 行注释
        if c == "/" and nxt == "/":
            j = text.find("\n", i)
            j = n if j < 0 else j
            blank(i, j)
            i = j
            continue
        # 块注释
        if c == "/" and nxt == "*":
            j = text.find("*/", i + 2)
            j = n if j < 0 else j + 2
            blank(i, j)
            i = j
            continue
        # 逐字字符串 @"..."（"" 是转义）
        if c == "@" and nxt == '"':
            k = i + 2
            while k < n:
                if text[k] == '"':
                    if k + 1 < n and text[k + 1] == '"':
                        k += 2
                        continue
                    k += 1
                    break
                k += 1
            blank(i, k)
            i = k
            continue
        # 插值字符串 $"..."（含 $@"..."）：朴素处理，忽略嵌套引号的花括号表达式
        if c == "$" and nxt in ('"', "@"):
            k = i + 1
            if text[k] == "@":
                k += 1
            if k < n and text[k] == '"':
                k += 1
                while k < n:
                    if text[k] == "\\" and k + 1 < n:
                        k += 2
                        continue
                    if text[k] == '"':
                        k += 1
                        break
                    k += 1
                blank(i, k)
                i = k
                continue
        # 普通字符串
        if c == '"':
            k = i + 1
            while k < n:
                if text[k] == "\\" and k + 1 < n:
                    k += 2
                    continue
                if text[k] == '"':
                    k += 1
                    break
                k += 1
            blank(i, k)
            i = k
            continue
        # 字符字面量
        if c == "'":
            k = i + 1
            while k < n and text[k] != "'":
                if text[k] == "\\" and k + 1 < n:
                    k += 1
                k += 1
            k = min(k + 1, n)
            blank(i, k)
            i = k
            continue
        i += 1
    return "".join(out)


# --------------------------------------------------------------------------- #
# 结果收集
# --------------------------------------------------------------------------- #

@dataclass
class Finding:
    severity: str            # "FAIL" | "WARN"
    file: str
    line: int
    rule: str
    capability: str
    snippet: str
    why: str
    occurrence: int = 1      # 同一文件同一规则同一行内容里的第几次出现（用于区分重复命中）

    def identity(self) -> str:
        """基线比对用的稳定身份：不含行号，抗行号平移。"""
        normalized = re.sub(r"\s+", " ", self.snippet.strip())
        return f"{self.file}|{self.rule}|{self.capability}|{normalized}"

    def baseline_key(self) -> str:
        return f"{self.identity()}#{self.occurrence}"

    def as_dict(self) -> dict:
        return {
            "file": self.file,
            "line": self.line,
            "capability": self.capability,
            "rule": self.rule,
            "excerpt": self.snippet,
            "severity": self.severity,
            "why": self.why,
            "key": self.baseline_key(),
        }


@dataclass
class Report:
    fails: list[Finding] = field(default_factory=list)
    warns: list[Finding] = field(default_factory=list)
    notes: list[Finding] = field(default_factory=list)   # severity=INFO：列出供人工核对，不算违规
    infos: list[str] = field(default_factory=list)
    scanned_files: list[str] = field(default_factory=list)
    scanned_rules: int = 0
    manifest_caps: list[str] = field(default_factory=list)
    manifest_assemblies: list[str] = field(default_factory=list)
    detected_caps: set[str] = field(default_factory=set)
    manifest_ok: bool = True
    manifest_id: str = ""
    manifest_version: str = ""
    game_versions: list[str] = field(default_factory=list)

    @property
    def all_findings(self) -> list[Finding]:
        return self.fails + self.warns

    def undeclared(self) -> list[str]:
        declared = set(self.manifest_caps)
        return sorted(c for c in self.detected_caps if c not in declared)



# --------------------------------------------------------------------------- #
# manifest 检查
# --------------------------------------------------------------------------- #

def check_manifest(report: Report) -> bool:
    """解析 manifest 的 Capabilities / Assemblies / GameVersions。返回是否通过。"""
    ok = True
    if not MANIFEST_PATH.exists():
        print(f"× 找不到 manifest：{MANIFEST_PATH}")
        return False

    try:
        root = ET.parse(MANIFEST_PATH).getroot()
    except ET.ParseError as e:
        print(f"× manifest 不是合法 XML：{e}")
        return False

    mod_id = (root.findtext("ID") or "").strip()
    mod_version = (root.findtext("ModVersion") or "").strip()
    report.manifest_id = mod_id
    report.manifest_version = mod_version
    report.infos.append(f"manifest ID={mod_id or '（缺）'} ModVersion={mod_version or '（缺）'}")
    if not mod_id:
        print("× manifest 缺 <ID>")
        ok = False

    # --- Capabilities ---
    caps_node = root.find("Capabilities")
    caps = [] if caps_node is None else [
        (c.text or "").strip() for c in caps_node.findall("Capability") if (c.text or "").strip()
    ]
    report.manifest_caps = caps
    if caps_node is None:
        print("× manifest 没有 <Capabilities>（正式版要求必填）")
        ok = False
    seen = set()
    for cap in caps:
        if cap in seen:
            print(f"× Capabilities 重复声明：{cap}")
            ok = False
        seen.add(cap)
        if cap in DEPRECATED_CAPABILITIES:
            print(f"× [FAIL] Capabilities 声明了废弃能力 `{cap}`："
                  f"2026-12-10 的游戏更新会直接拒绝加载（指南 1457/:1494/:1820）")
            ok = False
        elif cap in UNVERIFIED_CAPABILITIES:
            print(f"! [WARN] Capabilities 声明了未核实能力 `{cap}`：{UNVERIFIED_CAPABILITIES[cap]}")
        elif cap not in KNOWN_CAPABILITIES:
            print(f"× [FAIL] Capabilities 声明了未知能力 `{cap}`（拼错会导致整包被拒）")
            ok = False

    # --- Assemblies ---
    asm_node = root.find("Assemblies")
    asms = [] if asm_node is None else [
        (a.text or "").strip() for a in asm_node.findall("Assembly") if (a.text or "").strip()
    ]
    report.manifest_assemblies = asms
    if asm_node is None or not asms:
        print("× manifest 没有 <Assemblies>（正式版要求必填）")
        ok = False
    lowered = {a.lower() for a in asms}
    for game_asm in ("0Harmony.dll", "Assembly-CSharp.dll", "mscorlib.dll", "netstandard.dll"):
        if game_asm.lower() in lowered:
            print(f"× [FAIL] <Assemblies> 不得列出游戏/框架程序集：{game_asm}（指南 1434 步骤③）")
            ok = False

    # --- Assemblies 与编译产物的双向覆盖 ---
    if BUILD_DIR.is_dir():
        built = sorted(p.name for p in BUILD_DIR.glob("*.dll"))
        report.infos.append(f"编译产物目录 {BUILD_DIR.relative_to(ROOT)} 里的 DLL：{built or '（无）'}")
        for name in built:
            if name.lower() not in lowered:
                print(f"× [FAIL] 编译产物 `{name}` 未列进 <Assemblies>："
                      f"未列出的 DLL 会被加载但不被扫描，是 hard refusal（指南 1434 步骤①）")
                ok = False
        for name in asms:
            if name.lower() not in {b.lower() for b in built}:
                print(f"! [WARN] <Assemblies> 列了 `{name}`，但 {BUILD_DIR.relative_to(ROOT)} 里没有它"
                      f"（未 build 或路径不同；部署后缺 DLL 同样会被拒）")
    else:
        print(f"! [WARN] 找不到编译产物目录 {BUILD_DIR.relative_to(ROOT)}，跳过 Assemblies 双向核对"
              f"（先跑 dotnet build mod/DeepSpaceMod.csproj）")

    # --- GameVersions ---
    gv_node = root.find("GameVersions")
    versions = [] if gv_node is None else [
        (v.text or "").strip() for v in gv_node.findall("Version") if (v.text or "").strip()
    ]
    report.infos.append(f"GameVersions（{len(versions)} 条）：{versions or '（无）'}")
    report.game_versions = versions
    if not versions:
        print("× [FAIL] <GameVersions> 为空")
        ok = False
    else:
        if not any(v.startswith("1.") or v.startswith("2.") for v in versions):
            print("! [WARN] GameVersions 里没有 1.x 条目：正式版（100-Rev14 = 1.0.0.14）"
                  "只能靠 1.0.0 这类前缀覆盖，指南建议「Write 1.0.0 unless you have a reason not to」")
        for v in versions:
            if v.endswith("*") and not v[:-1].count("."):
                print(f"× [FAIL] GameVersions `{v}`：开放区间不被接受（指南 1428）")
                ok = False
            if "-" in v and "*" in v:
                print(f"× [FAIL] GameVersions `{v}`：区间里不允许通配（指南 1428）")
                ok = False

    return ok


# --------------------------------------------------------------------------- #
# 源码扫描
# --------------------------------------------------------------------------- #

def scan_sources(report: Report) -> None:
    if not SRC_DIR.is_dir():
        print(f"× 找不到源码目录：{SRC_DIR}")
        return
    files = sorted(SRC_DIR.glob("*.cs"))
    report.scanned_rules = len(ALL_RULES)
    occurrences: dict[str, int] = {}
    for path in files:
        rel = path.relative_to(ROOT).as_posix()
        report.scanned_files.append(rel)
        raw = path.read_text(encoding="utf-8-sig", errors="replace")
        masked = strip_comments_and_strings(raw)
        raw_lines = raw.splitlines()
        mask_lines = masked.splitlines()
        for lineno, code_line in enumerate(mask_lines, start=1):
            if not code_line.strip():
                continue
            for _, known_free in NOT_THREADS_PATTERNS:
                if known_free.search(code_line):
                    code_line = known_free.sub(lambda m: " " * len(m.group(0)), code_line)
            for rule in ALL_RULES:
                if not rule.pattern.search(code_line):
                    continue
                # 同一行已经被更严重的规则命中时，不再重复报 INFO
                if rule.severity == "INFO" and any(
                    f.file == rel and f.line == lineno for f in report.fails + report.warns
                ):
                    continue
                snippet = raw_lines[lineno - 1].strip() if lineno <= len(raw_lines) else ""
                finding = Finding(
                    severity=rule.severity,
                    file=rel,
                    line=lineno,
                    rule=rule.name,
                    capability=rule.capability,
                    snippet=snippet,
                    why=rule.why,
                )
                occ_key = finding.identity()
                occurrences[occ_key] = occurrences.get(occ_key, 0) + 1
                finding.occurrence = occurrences[occ_key]
                if rule.severity == "INFO":
                    # 只做清单展示，不进 detected（避免把「取类名」误算成 detected 能力）
                    report.notes.append(finding)
                    continue
                report.detected_caps.add(rule.capability)
                (report.fails if rule.severity == "FAIL" else report.warns).append(finding)


# --------------------------------------------------------------------------- #
# 报告
# --------------------------------------------------------------------------- #

def _owner_hint(rel_file: str) -> str:
    """把已知的、属其他智能体独占范围的文件标注出来，避免重复劳动。"""
    if rel_file in ("mod/src/GameAccess.cs", "mod/src/BridgeApi.cs"):
        return "（属 R2 独占范围；已提变更请求 CR-T01-2）"
    if rel_file in ("mod/src/ModelConfigPanel.cs",):
        return "（属 R2 独占范围；已提变更请求 CR-T01-3）"
    return ""


def print_report(report: Report, verbose: bool, baseline: "BaselineDiff | None" = None) -> None:
    print()
    print("=" * 78)
    print("mod 平台合规自检（离线近似；权威判定仍是游戏内 `mod-scan <id>`）")
    print("=" * 78)
    print(f"规则来源  ：ModdingGuide.md / APIChanges.md（核对日期 {RULES_VERIFIED_ON}）")
    today = date.today()
    days_left = (DEPRECATION_HARD_DATE - today).days
    print(f"硬截止    ：{DEPRECATION_HARD_DATE.isoformat()}，"
          f"距今 {days_left} 天" + ("（已过期）" if days_left < 0 else ""))
    print(f"扫描源码  ：{len(report.scanned_files)} 个 .cs —— {', '.join(report.scanned_files)}")
    print(f"扫描规则  ：{report.scanned_rules} 条（threads {len(THREAD_RULES)} / process "
          f"{len(PROCESS_RULES)} / dynamic-code {len(DYNAMIC_CODE_RULES)} / native "
          f"{len(NATIVE_RULES)} / reflection "
          f"{len(REFLECTION_FAIL_RULES) + len(REFLECTION_COMPUTED_RULES) + len(REFLECTION_INFO_RULES)}"
          f" / hard-refusal {len(HARD_REFUSAL_RULES)}）")
    print("             明确扫过、本仓库 0 命中的模式也包括：Assembly.LoadFrom / "
          "Reflection.Emit / Expression.Compile / CSharpCodeProvider / CSharpScript / "
          "BinaryFormatter / Parallel.For* / Task.Factory.StartNew / ContinueWith")
    print("             已知不算违规、扫描前先抹掉的形态：`.onClick.Invoke()`（UnityEvent.Invoke，"
          "不是 MonoBehaviour.Invoke）")
    print(f"manifest  ：ID={report.manifest_id or '（缺）'} ModVersion="
          f"{report.manifest_version or '（缺）'}")
    for info in report.infos[1:]:
        print(f"            {info}")

    if report.fails:
        print()
        print(f"FAIL  {len(report.fails)} 条 —— 12-10 后会导致 mod 被拒绝加载：")
        for f in report.fails:
            print(f"  × {f.file}:{f.line}  [{f.capability}] {f.rule} {_owner_hint(f.file)}")
            if verbose and f.snippet:
                print(f"      {f.snippet}")
            print(f"      → {f.why}")
    else:
        print()
        print("FAIL  0 条 —— 没有 Process.Start / new Thread / ThreadPool / Task.Run / "
              "Parallel.* / Timer / DllImport / unsafe / 动态代码路径命中。")

    if report.warns:
        print()
        print(f"WARN  {len(report.warns)} 条 —— 需人工确认，当前不阻断 PASS：")
        for f in report.warns:
            print(f"  ! {f.file}:{f.line}  [{f.capability}] {f.rule} {_owner_hint(f.file)}")
            if verbose and f.snippet:
                print(f"      {f.snippet}")
            print(f"      → {f.why}")

    if report.notes:
        print()
        print(f"INFO  {len(report.notes)} 条 —— 不算违规，列出供人工/实机 `mod-scan` 核对：")
        for f in report.notes:
            print(f"  · {f.file}:{f.line}  [{f.capability}] {f.rule}")
            if verbose and f.snippet:
                print(f"      {f.snippet}")

    print()
    print("能力对照：")
    print(f"  manifest declared : {', '.join(report.manifest_caps) or '（无）'}")
    print(f"  源码 detected     : {', '.join(sorted(report.detected_caps)) or '（无）'}")
    missing = report.undeclared()
    if missing:
        print(f"  × 检测到但未声明：{', '.join(missing)} —— 启用时会被判"
              f"「uses capabilities its manifest does not declare」")
    print(f"  ModHelper 后台 API（本机元数据实测，mod 侧可调）：")
    for name, sig in MODHELPER_BACKGROUND_API.items():
        print(f"    · {sig}")

    if baseline is not None:
        print()
        print(f"基线比对  ：{baseline.path}")
        print(f"  基线记录 {len(baseline.known)} 条；本次 {len(report.all_findings)} 条")
        if baseline.new:
            print(f"  × 新增 {len(baseline.new)} 条（基线里没有 —— 这是「变差」）：")
            for f in baseline.new:
                print(f"      + {f.file}:{f.line}  [{f.capability}] {f.rule}")
                if f.snippet:
                    print(f"        {f.snippet}")
        else:
            print("  ✓ 没有新增违规")
        if baseline.fixed:
            print(f"  · 已修掉 {len(baseline.fixed)} 条（基线里有、本次没命中）：")
            for key in baseline.fixed:
                print(f"      - {key}")
        if baseline.new_undeclared:
            print(f"  × 新增未声明能力：{', '.join(baseline.new_undeclared)}")


# --------------------------------------------------------------------------- #
# 机器可读输出 + 基线比对
# --------------------------------------------------------------------------- #

def report_payload(report: Report) -> dict:
    """--json 的结构。所有人工提示都放进 diagnostics，不影响解析。"""
    today = date.today()
    return {
        "tool": "check_mod_compliance",
        "version": 1,
        "rules_verified_on": RULES_VERIFIED_ON,
        "generated_at": datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds"),
        "sources": {
            "modding_guide": SOURCE_MODDING_GUIDE,
            "api_changes": SOURCE_API_CHANGES,
            "game_assembly": str(GAME_ASSEMBLY),
        },
        "hard_deadline": DEPRECATION_HARD_DATE.isoformat(),
        "days_left": (DEPRECATION_HARD_DATE - today).days,
        "scanned_files": report.scanned_files,
        "scanned_rules": report.scanned_rules,
        "manifest": {
            "id": report.manifest_id,
            "mod_version": report.manifest_version,
            "capabilities": report.manifest_caps,
            "assemblies": report.manifest_assemblies,
            "game_versions": report.game_versions,
            "ok": report.manifest_ok,
        },
        "declared": report.manifest_caps,
        "detected": sorted(report.detected_caps),
        "undeclared": report.undeclared(),
        "counts": {"fail": len(report.fails), "warn": len(report.warns),
                   "info": len(report.notes)},
        "findings": [f.as_dict() for f in report.all_findings],
        "notes": [f.as_dict() for f in report.notes],
        "infos": report.infos,
    }


@dataclass
class BaselineDiff:
    path: str
    known: set[str] = field(default_factory=set)
    new: list[Finding] = field(default_factory=list)
    fixed: list[str] = field(default_factory=list)
    new_undeclared: list[str] = field(default_factory=list)
    baseline_undeclared: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "path": self.path,
            "known": len(self.known),
            "new": [f.as_dict() for f in self.new],
            "fixed": self.fixed,
            "new_undeclared": self.new_undeclared,
            "baseline_undeclared": self.baseline_undeclared,
        }


def load_baseline(path: Path, report: Report) -> BaselineDiff:
    data = json.loads(path.read_text(encoding="utf-8"))
    findings = data.get("findings", [])
    known: set[str] = set()
    for item in findings:
        key = item.get("key")
        if not key:
            # 兼容手写的、没有 key 的基线：现场算一个
            probe = Finding(
                severity=item.get("severity", "WARN"),
                file=item.get("file", ""),
                line=int(item.get("line", 0) or 0),
                rule=item.get("rule", ""),
                capability=item.get("capability", ""),
                snippet=item.get("excerpt", "") or item.get("snippet", ""),
                why="",
                occurrence=int(item.get("occurrence", 1) or 1),
            )
            key = probe.baseline_key()
        known.add(key)

    diff = BaselineDiff(path=str(path), known=known)
    current = {f.baseline_key(): f for f in report.all_findings}
    diff.new = [f for key, f in current.items() if key not in known]
    diff.fixed = sorted(known - set(current))
    base_undeclared = [u for u in data.get("undeclared", [])]
    diff.baseline_undeclared = base_undeclared
    diff.new_undeclared = [u for u in report.undeclared() if u not in base_undeclared]
    return diff


def write_baseline(path: Path, report: Report) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = report_payload(report)
    payload["note"] = (
        "这是 2026-12-10 清零工作的起点快照：记录当时已知的 FAIL/WARN。"
        "带 --baseline 跑时只报「比这份快照新增」的违规，用于 CI 做「不许变差」闸门。"
        "清掉一批违规后用 --write-baseline 重写它。"
    )
    payload.pop("infos", None)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description="mod 平台合规离线自检（不联网、不依赖游戏）")
    ap.add_argument("--strict", action="store_true",
                    help="WARN 也算失败（R2 完成 CR-T01-2/3 后建议开启）")
    ap.add_argument("-v", "--verbose", action="store_true", help="打印每条命中的源码行")
    ap.add_argument("--json", action="store_true",
                    help="输出机器可读 JSON（唯一写 stdout 的内容，人工提示进 diagnostics）")
    ap.add_argument("--baseline", metavar="PATH",
                    help="与基线 JSON 比对，只把「比基线新增」的违规算作失败")
    ap.add_argument("--write-baseline", metavar="PATH",
                    help="把本次结果写成基线快照（清完一批违规后手动执行）")
    args = ap.parse_args()

    if args.json and args.write_baseline:
        print("× --json 与 --write-baseline 不能同时用（后者要写文件，前者要读结构）",
              file=sys.stderr)
        return 2

    captured = io.StringIO()
    sink = contextlib.redirect_stdout(captured) if args.json else contextlib.nullcontext()
    with sink:
        report = Report()
        report.manifest_ok = check_manifest(report)
        if not SRC_DIR.is_dir() and not MANIFEST_PATH.exists():
            print(f"× 环境错误：既没有 {SRC_DIR} 也没有 {MANIFEST_PATH}", file=sys.stderr)
            return 2
        scan_sources(report)

        if args.write_baseline:
            wpath = Path(args.write_baseline)
            if not wpath.is_absolute():
                wpath = ROOT / wpath
            write_baseline(wpath, report)
            print(f"已写基线：{wpath}")
            print(f"  记录 FAIL {len(report.fails)} 条 / WARN {len(report.warns)} 条 / "
                  f"未声明能力 {report.undeclared() or '（无）'}")
            print("  之后用 --baseline 跑，就只报「比这份快照新增」的违规。")
            return 0

        diff: BaselineDiff | None = None
        if args.baseline:
            bpath = Path(args.baseline)
            if not bpath.is_absolute():
                bpath = ROOT / bpath
            if not bpath.is_file():
                print(f"! [WARN] 找不到基线文件 {bpath}，本次按「无基线」处理："
                      f"全部违规都算现状，而不是新增", file=sys.stderr)
            else:
                diff = load_baseline(bpath, report)

        if not args.json:
            print_report(report, args.verbose, diff)

    undeclared = report.undeclared()

    # ---- 不带基线：现状口径 ----
    if diff is None:
        failed = bool(report.fails) or not report.manifest_ok or bool(undeclared)
        if args.strict and report.warns:
            failed = True
        if args.json:
            payload = report_payload(report)
            payload["mode"] = "absolute"
            payload["strict"] = args.strict
            payload["ok"] = not failed
            payload["diagnostics"] = captured.getvalue().splitlines()
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            if undeclared:
                print(f"\n× [FAIL] declared 未覆盖 detected：{', '.join(undeclared)}")
            print()
            if failed:
                print("=" * 78)
                print(f"结果：FAIL（FAIL {len(report.fails)} 条，WARN {len(report.warns)} 条"
                      f"{'，--strict 下 WARN 也阻断' if args.strict and report.warns else ''}）")
                print("必须先修完再部署：12-10 之后游戏更新会拒绝加载。")
                print("修完务必用游戏内 `mod-scan deepspace.bridge` 实机确认 declared 覆盖 detected。")
                print("=" * 78)
            else:
                print("=" * 78)
                print(f"结果：PASS（FAIL 0 条，WARN {len(report.warns)} 条）")
                print("注意：这是正则近似，不能替代游戏内 `mod-scan deepspace.bridge`（V5，待实机）。")
                print("=" * 78)
        return 1 if failed else 0

    # ---- 带基线：「不许变差」口径（旧问题不算红，新增才红）----
    regressed = bool(diff.new) or bool(diff.new_undeclared)
    if args.strict and report.fails:
        regressed = True
    if args.json:
        payload = report_payload(report)
        payload["mode"] = "baseline"
        payload["strict"] = args.strict
        payload["baseline"] = diff.as_dict()
        payload["regressed"] = regressed
        payload["ok"] = not regressed
        payload["diagnostics"] = captured.getvalue().splitlines()
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print()
        print("=" * 78)
        print(f"结果（基线口径）：{'FAIL —— 出现了基线里没有的新违规' if regressed else 'PASS —— 没有变差'}")
        print(f"  · 已知旧违规 {len(diff.known)} 条（不阻断）")
        print(f"  · 新增 {len(diff.new)} 条，已修掉 {len(diff.fixed)} 条")
        if diff.new_undeclared:
            print(f"  · 新增未声明能力：{', '.join(diff.new_undeclared)}")
        print("  注意：这只是「没变差」，不等于 12-10 安全。清零标准是不带 --baseline 跑 `--strict` 绿。")
        print("=" * 78)
    return 1 if regressed else 0



if __name__ == "__main__":
    sys.exit(main())
