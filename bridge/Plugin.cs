using System;
using System.Collections.Concurrent;
using BepInEx;
using BepInEx.Logging;
using BepInEx.Unity.IL2CPP;
using Il2CppInterop.Runtime.Injection;
using UnityEngine;

namespace DeepSpaceBridge
{
    /// <summary>
    /// DeepSpaceBridge：把游戏内部逻辑桥接给外部 AI 玩家。
    ///
    /// - HTTP 服务（127.0.0.1:8965）：GET /state 读状态 · POST /action 执行操作
    ///   · POST /decision 推送决策（游戏内浮层展示）· GET /decision 读回当前浮层决策
    /// - 所有游戏调用派发到 Unity 主线程（MainThreadDispatcher）
    /// - 直接调用游戏 API（PlayerStore / StoreShutterButton / DialogUIManager / NegociationUIManager）
    ///   完全不依赖截图与鼠标模拟
    /// - DecisionOverlay：游戏内 AI 决策展示浮层（uGUI，数据来自 HTTP 推送）
    /// </summary>
    [BepInPlugin(Guid, Name, Version)]
    public class BridgePlugin : BasePlugin
    {
        public const string Guid = "deepspace.bridge";
        public const string Name = "DeepSpace Bridge";
        // v2.1：与 Python 包版本对齐（原 0.9.14 与实际功能脱节，/ping 无法用来
        // 判断部署的是哪一版）。新增 /state.day 与 /debug/client JSON 加固后置 2.1.0。
        public const string Version = "2.1.0";

        internal static ManualLogSource Logger;

        public override void Load()
        {
            Logger = Log;
            Logger.LogInfo($"{Name} v{Version} 正在加载…");

            try
            {
                // 注册主线程调度组件（IL2CPP 需要 ClassInjector）
                ClassInjector.RegisterTypeInIl2Cpp<MainThreadDispatcher>();
                var go = new GameObject("DeepSpaceBridge");
                UnityEngine.Object.DontDestroyOnLoad(go);
                go.hideFlags = HideFlags.HideAndDontSave;
                go.AddComponent<MainThreadDispatcher>();

                // 注册决策展示浮层（uGUI / Update 驱动，独立组件，只读 DecisionStore）
                ClassInjector.RegisterTypeInIl2Cpp<DecisionOverlay>();
                go.AddComponent<DecisionOverlay>();
                Logger.LogInfo("决策展示浮层已注册（游戏内右上角，Ctrl+F10 显隐）");

                var server = new BridgeServer(8965);
                server.Start();
                Logger.LogInfo("HTTP 桥已监听 http://127.0.0.1:8965 （/state · /action · /decision）");
            }
            catch (Exception e)
            {
                Logger.LogError($"初始化失败：{e}");
            }
        }
    }

    /// <summary>
    /// 主线程执行器：Unity API 只能在主线程调用，外部（HTTP 线程）操作经此队列派发。
    /// </summary>
    public class MainThreadDispatcher : MonoBehaviour
    {
        public MainThreadDispatcher(IntPtr ptr) : base(ptr) { }

        private static readonly ConcurrentQueue<Action> Queue = new ConcurrentQueue<Action>();

        /// <summary>把动作排入主线程队列（可在任意线程调用）。</summary>
        public static void Enqueue(Action action) => Queue.Enqueue(action);

        private void Update()
        {
            // 每帧最多处理 64 个任务，避免卡帧
            for (int i = 0; i < 64; i++)
            {
                if (!Queue.TryDequeue(out var action))
                {
                    break;
                }
                try
                {
                    action();
                }
                catch (Exception e)
                {
                    BridgePlugin.Logger.LogError($"主线程任务异常：{e}");
                }
            }
        }
    }
}
