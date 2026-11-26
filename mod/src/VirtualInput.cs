using System;
using System.Collections.Generic;
using UnityEngine;
using UnityEngine.EventSystems;

namespace DeepSpaceBridge
{
    /// <summary>
    /// 虚拟输入核心（移植自创意工坊 mod「游戏 MCP · AI 游玩」v1.2.7 的设计）。
    ///
    /// 【原理】游戏里大量旧代码直接读 <see cref="UnityEngine.Input"/> 的静态成员
    /// （GetKey / GetMouseButton / mousePosition…）。本类提供同名同签名的替代实现：
    ///   · VirtualInput.Active == false 时**完全透传**真实 Input，玩家自己玩不受影响；
    ///   · Active == true 时返回脚本逐帧喂入的状态，游戏以为真有鼠标键盘在动。
    /// 配合 LegacyInputPatch（Harmony Transpiler）把这些调用点改写过来，
    /// 就形成完整的 Input → EventSystem → 游戏逻辑链路 —— 这才是"像玩家一样操作"。
    ///
    /// 【合规】本类只用基础类型与 UnityEngine，不引入任何 threads/dynamic-code 能力。
    /// 【版权】设计思路来自公开 mod 的反编译分析；实现为本项目独立重写。
    /// </summary>
    internal static class VirtualInput
    {
        private static Vector2 _delta;
        private static Vector2 _scroll;
        private static HashSet<KeyCode> _held = new HashSet<KeyCode>();
        private static readonly HashSet<KeyCode> _pressed = new HashSet<KeyCode>();
        private static readonly HashSet<KeyCode> _released = new HashSet<KeyCode>();

        internal static bool Active { get; private set; }
        internal static Vector2 Point { get; private set; }

        internal static void Begin(Vector2 point)
        {
            Point = point;
            Active = true;
            Frame(point, new HashSet<KeyCode>(), Vector2.zero);
        }

        internal static void Frame(Vector2 point, HashSet<KeyCode> next, Vector2 wheel)
        {
            _delta = point - Point;
            Point = point;
            _scroll = wheel;
            _pressed.Clear();
            _released.Clear();
            _pressed.UnionWith(next);
            _pressed.ExceptWith(_held);
            _released.UnionWith(_held);
            _released.ExceptWith(next);
            _held = next;
        }

        internal static void End()
        {
            Active = false;
            Frame(Point, new HashSet<KeyCode>(), Vector2.zero);
        }

        internal static Vector3 Position()
        {
            return Active ? (Vector3)Point : Input.mousePosition;
        }

        internal static Vector2 ScrollDelta()
        {
            return Active ? _scroll : Input.mouseScrollDelta;
        }

        internal static bool AnyKeyDown()
        {
            return Active ? _pressed.Count > 0 : Input.anyKeyDown;
        }

        internal static bool Key(KeyCode key) => Active ? _held.Contains(key) : Input.GetKey(key);

        internal static bool KeyDown(KeyCode key) => Active ? _pressed.Contains(key) : Input.GetKeyDown(key);

        internal static bool KeyUp(KeyCode key) => Active ? _released.Contains(key) : Input.GetKeyUp(key);

        internal static bool NamedKeyDown(string key)
        {
            if (Active)
            {
                return Enum.TryParse(key, true, out KeyCode code) && KeyDown(code);
            }
            return Input.GetKeyDown(key);
        }

        private static KeyCode MouseKey(int button) => button >= 0 && button <= 6 ? (KeyCode)(323 + button) : 0;

        internal static bool Button(int button) => Active ? Key(MouseKey(button)) : Input.GetMouseButton(button);

        internal static bool ButtonDown(int button) => Active ? KeyDown(MouseKey(button)) : Input.GetMouseButtonDown(button);

        internal static bool ButtonUp(int button) => Active ? KeyUp(MouseKey(button)) : Input.GetMouseButtonUp(button);

        internal static float Axis(string name)
        {
            if (!Active) return Input.GetAxis(name);
            switch (name)
            {
                case "Mouse X": return _delta.x;
                case "Mouse Y": return _delta.y;
                case "Mouse ScrollWheel": return _scroll.y;
                case "Horizontal": return (Key(KeyCode.RightArrow) || Key(KeyCode.D) ? 1 : 0)
                                       - (Key(KeyCode.LeftArrow) || Key(KeyCode.A) ? 1 : 0);
                case "Vertical": return (Key(KeyCode.UpArrow) || Key(KeyCode.W) ? 1 : 0)
                                     - (Key(KeyCode.DownArrow) || Key(KeyCode.S) ? 1 : 0);
                default: return 0f;
            }
        }
    }

    /// <summary>
    /// uGUI 的 BaseInput 替代品。挂到 EventSystem 的 StandaloneInputModule.inputOverride
    /// 后，所有走 EventSystem 的 UI 交互（含点击、拖拽、输入）都从 VirtualInput 读数。
    /// 非激活状态透传父类实现，玩家自己玩不受影响。
    /// </summary>
    public sealed class GameMcpUiInput : BaseInput
    {
        public override Vector2 mousePosition => VirtualInput.Active
            ? VirtualInput.Point
            : base.mousePosition;

        public override Vector2 mouseScrollDelta => VirtualInput.Active
            ? VirtualInput.ScrollDelta()
            : base.mouseScrollDelta;

        public override bool mousePresent => !VirtualInput.Active || base.mousePresent;

        public override bool touchSupported => !VirtualInput.Active && base.touchSupported;

        public override int touchCount => VirtualInput.Active ? 0 : base.touchCount;

        public override bool GetMouseButton(int button) => VirtualInput.Button(button);

        public override bool GetMouseButtonDown(int button) => VirtualInput.ButtonDown(button);

        public override bool GetMouseButtonUp(int button) => VirtualInput.ButtonUp(button);

        public override float GetAxisRaw(string axisName) => VirtualInput.Active
            ? VirtualInput.Axis(axisName)
            : base.GetAxisRaw(axisName);

        public override bool GetButtonDown(string buttonName)
        {
            if (!VirtualInput.Active) return base.GetButtonDown(buttonName);
            if (buttonName == "Submit")
            {
                return VirtualInput.KeyDown(KeyCode.Return)
                    || VirtualInput.KeyDown(KeyCode.KeypadEnter)
                    || VirtualInput.KeyDown(KeyCode.Space);
            }
            if (buttonName == "Cancel") return VirtualInput.KeyDown(KeyCode.Escape);
            return false;
        }
    }
}
