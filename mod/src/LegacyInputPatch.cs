using System;
using System.Collections.Generic;
using System.Linq;
using System.Reflection;
using System.Reflection.Emit;
using HarmonyLib;
using UnityEngine;
using UnityEngine.EventSystems;

namespace DeepSpaceBridge
{
    /// <summary>
    /// 把游戏内**直接读 UnityEngine.Input 静态成员**的旧代码改写为 VirtualInput 的同名实现。
    ///
    /// 【为什么需要它】uGUI 的 BaseInput.inputOverride 只能覆盖走 EventSystem 的交互；
    /// 游戏里大量机制（拾荒、拖拽、机器面板、快捷键）直接调 Input.GetKey/GetMouseButton，
    /// 绕过了 EventSystem。只有用 Harmony Transpiler 在 IL 层把这些 Call 换掉，
    /// 虚拟输入才能真正驱动"完整玩家链路"。
    ///
    /// 【版本锁】Callers 是 106 个 token，来自 Steam Build 26525552（100-Rev13）的
    /// Assembly-CSharp.dll。已实测本机 MVID = 911508a6-e0eb-4cc0-91e8-e14b16ea28d7。
    /// 版本不匹配时 TargetMethods 返回空集，补丁静默不生效（安全性优先）。
    /// 游戏更新后必须重新提取 token，否则虚拟输入对旧代码失效。
    ///
    /// 【合规】目标是游戏自身代码（Assembly-CSharp.dll），按官方 ModdingGuide.md:1463
    /// 属于免费补丁，不需要 reflection-external 能力。不瞄准任何 guard 类型。
    /// </summary>
    [HarmonyPatch]
    internal static class LegacyInputPatch
    {
        private static readonly Guid Build = new Guid("911508a6-e0eb-4cc0-91e8-e14b16ea28d7");

        private static readonly int[] Callers = new int[106]
        {
            100666312, 100666313, 100666615, 100666616, 100666627, 100666628, 100666629, 100666637, 100668603, 100668607,
            100668608, 100668609, 100668613, 100684560, 100668796, 100669616, 100669617, 100671000, 100671472, 100671864,
            100686355, 100671966, 100671967, 100672127, 100672215, 100672216, 100672217, 100672219, 100672220, 100672221,
            100672222, 100672232, 100672270, 100672315, 100686724, 100672527, 100672704, 100672705, 100672706, 100673312,
            100673483, 100673754, 100673755, 100673756, 100673808, 100674131, 100674132, 100674133, 100676112, 100676113,
            100676115, 100676117, 100676147, 100676148, 100676150, 100676151, 100677384, 100677420, 100677421, 100677423,
            100677425, 100677433, 100677434, 100677435, 100677441, 100677442, 100677443, 100677459, 100677464, 100677466,
            100677471, 100677472, 100677473, 100677481, 100677483, 100677484, 100677490, 100677493, 100677514, 100677515,
            100677516, 100677520, 100677526, 100677532, 100677533, 100677540, 100677541, 100677549, 100677550, 100677567,
            100677571, 100677573, 100677581, 100677595, 100677599, 100677605, 100677941, 100687941, 100678326, 100678327,
            100678328, 100678329, 100678420, 100678421, 100679263, 100679287
        };

        private static readonly Dictionary<Tuple<string, Type>, MethodInfo> Replacements =
            new Dictionary<Tuple<string, Type>, MethodInfo>
            {
                [Tuple.Create("get_mousePosition", (Type)null)] = new Func<Vector3>(VirtualInput.Position).Method,
                [Tuple.Create("get_mouseScrollDelta", (Type)null)] = new Func<Vector2>(VirtualInput.ScrollDelta).Method,
                [Tuple.Create("get_anyKeyDown", (Type)null)] = new Func<bool>(VirtualInput.AnyKeyDown).Method,
                [Tuple.Create("GetAxis", typeof(string))] = new Func<string, float>(VirtualInput.Axis).Method,
                [Tuple.Create("GetKey", typeof(KeyCode))] = new Func<KeyCode, bool>(VirtualInput.Key).Method,
                [Tuple.Create("GetKeyDown", typeof(KeyCode))] = new Func<KeyCode, bool>(VirtualInput.KeyDown).Method,
                [Tuple.Create("GetKeyUp", typeof(KeyCode))] = new Func<KeyCode, bool>(VirtualInput.KeyUp).Method,
                [Tuple.Create("GetKeyDown", typeof(string))] = new Func<string, bool>(VirtualInput.NamedKeyDown).Method,
                [Tuple.Create("GetMouseButton", typeof(int))] = new Func<int, bool>(VirtualInput.Button).Method,
                [Tuple.Create("GetMouseButtonDown", typeof(int))] = new Func<int, bool>(VirtualInput.ButtonDown).Method,
                [Tuple.Create("GetMouseButtonUp", typeof(int))] = new Func<int, bool>(VirtualInput.ButtonUp).Method
            };

        internal static bool IsSupported => typeof(InputActionManager).Module.ModuleVersionId == Build;

        private static IEnumerable<MethodBase> TargetMethods()
        {
            if (!IsSupported) return Array.Empty<MethodBase>();
            Module module = typeof(InputActionManager).Module;
            var result = new List<MethodBase>(Callers.Length);
            foreach (int token in Callers)
            {
                try { result.Add(module.ResolveMethod(token)); }
                catch (Exception e) { ModLog.Warn($"虚拟输入 token {token} 解析失败：{e.Message}"); }
            }
            return result;
        }

        private static IEnumerable<CodeInstruction> Transpiler(IEnumerable<CodeInstruction> instructions)
        {
            foreach (CodeInstruction instruction in instructions)
            {
                if (instruction.opcode == OpCodes.Call && instruction.operand is MethodInfo method
                    && method.DeclaringType == typeof(Input))
                {
                    Type arg = method.GetParameters().FirstOrDefault()?.ParameterType;
                    if (Replacements.TryGetValue(Tuple.Create(method.Name, arg), out MethodInfo replacement))
                    {
                        instruction.operand = replacement;
                    }
                }
                yield return instruction;
            }
        }
    }

    /// <summary>
    /// EventSystem.isFocused 替换：虚拟输入激活时，即使游戏窗口不是前台焦点
    /// （例如 AI 在后台跑、用户切到别的窗口），也认为 UI 有焦点，否则
    /// StandaloneInputModule 会因 isFocused=false 丢弃所有输入事件。
    ///
    /// 【2026-11-18 实机教训】当初为避免合规 WARN 删掉了这个补丁，结果 player_click
    /// 在正确坐标点 ShutterButton 毫无反应；对照 ui_click（直接派发事件）却成功。
    /// 一查前台窗口是酷狗音乐——游戏失焦，虚拟输入被 EventSystem 丢掉了。
    /// 补丁目标是 UnityEngine.EventSystems.StandaloneInputModule（游戏自带 UI），
    /// 属官方认可的免费目标；实机已证明 Harmony 补丁游戏自身代码扫描器放行。
    /// </summary>
    [HarmonyPatch]
    internal static class MenuFocusPatch
    {
        private static IEnumerable<MethodBase> TargetMethods()
        {
            // 字面类型 + 字面成员名（官方认可的免费反射形态，游戏自带 UnityEngine.UI 目标）。
            var flags = BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic;
            var a = typeof(StandaloneInputModule).GetMethod("UpdateModule", flags);
            var b = typeof(StandaloneInputModule).GetMethod("ActivateModule", flags);
            var c = typeof(StandaloneInputModule).GetMethod("Process", flags);
            if (a != null) yield return a;
            if (b != null) yield return b;
            if (c != null) yield return c;
        }

        private static IEnumerable<CodeInstruction> Transpiler(IEnumerable<CodeInstruction> instructions)
        {
            MethodInfo replacement = new Func<EventSystem, bool>(HasFocus).Method;
            foreach (CodeInstruction instruction in instructions)
            {
                if (instruction.operand is MethodInfo method
                    && method.DeclaringType == typeof(EventSystem)
                    && method.Name == "get_isFocused")
                {
                    instruction.opcode = OpCodes.Call;
                    instruction.operand = replacement;
                }
                yield return instruction;
            }
        }

        private static bool HasFocus(EventSystem system)
        {
            if (system.isFocused) return true;
            if (!VirtualInput.Active) return false;
            var module = system.currentInputModule;
            return module != null && module.inputOverride is GameMcpUiInput;
        }
    }
}
