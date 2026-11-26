using System;
using System.Collections.Concurrent;
using UnityEngine;

namespace DeepSpaceBridge
{
    /// <summary>
    /// DeepSpaceBridge 的游戏原生 mod 入口（Mono）。
    ///
    /// 与旧版 BepInEx 插件的差别：
    ///   · 不再需要 BepInEx / doorstop / IL2CPP interop，直接由游戏内置 ModLoader 加载；
    ///   · 入口契约由 BepInEx 的 BasePlugin 改为游戏自带的 <see cref="IMod"/>；
    ///   · 不再需要 ClassInjector（那是 IL2CPP 专属），Mono 下直接 AddComponent。
    ///
    /// 关卡：mod 必须先在游戏「模组」界面启用（本地 mod 首次启用会有能力申明提示）。
    /// </summary>
    public sealed class DeepSpaceBridgeMod : IMod
    {
        /// <summary>mod 自身版本，随 /ping 一起返回，用于确认跑的是哪一版。</summary>
        public const string Version = "3.10.0-mod";

        public const int Port = 8965;

        private GameObject _host;
        private static BridgeServer _server;

        public void Init(ModManifest manifest)
        {
            ModLog.Info($"Init：ID={manifest?.ID} 版本={manifest?.ModVersion}（代码版本 {Version}）");
        }

        public void OnEnable()
        {
            try
            {
                // 宿主对象：主线程调度器与决策浮层都挂在它上面
                _host = new GameObject("DeepSpaceBridge");
                UnityEngine.Object.DontDestroyOnLoad(_host);
                _host.hideFlags = HideFlags.HideAndDontSave;
                _host.AddComponent<MainThreadDispatcher>();
                _host.AddComponent<DecisionOverlay>();
                ModLog.Info("决策展示浮层已注册（Ctrl+F10 显隐）");

                // 虚拟输入（玩家输入链路）：补丁在游戏启用时由 Harmony 自动应用；
                // 驱动器只挂组件，不主动激活 —— VirtualInput.Active 默认 false，
                // 玩家自己玩时全部透传真实 Input，只有 AI 下发 player_* 动作才接管。
                if (LegacyInputPatch.IsSupported)
                {
                    VirtualInputDriver.Ensure(_host);
                    ModLog.Info($"虚拟输入补丁目标版本匹配（Build 26525552），玩家输入链路可用");
                }
                else
                {
                    ModLog.Warn("虚拟输入补丁未激活：游戏程序集版本不匹配，player_* 动作将拒绝执行");
                }

                // 模型设置窗口：游戏内直接改 base_url / model / api_key，保存到 model_config.ini
                ModelConfig.Load();
                _host.AddComponent<ModelConfigPanel>();
                ModLog.Info("模型设置窗口已注册（Ctrl+F9 显隐）");

                _server = new BridgeServer(Port);
                _server.Start();

                // v3.10.4（2026-11-14 实机）：accept 循环的启动重试不能靠 ModHelper.Delay
                // （OnEnable 期间 mod 尚未 running，Delay 静默不调度），改由主线程 Update 泵驱动。
                var pump = _host.AddComponent<BridgePump>();
                pump.Server = _server;

                ModLog.Info($"HTTP 桥已监听 http://127.0.0.1:{Port} （/state · /action · /decision）");
            }
            catch (Exception e)
            {
                ModLog.Error($"OnEnable 失败：{e}");
            }
        }

        public void OnDisable()
        {
            try
            {
                _server?.Stop();
                _server = null;
            }
            catch (Exception e)
            {
                ModLog.Warn($"停止 HTTP 桥失败：{e.Message}");
            }

            if (_host != null)
            {
                UnityEngine.Object.Destroy(_host);
                _host = null;
            }
            ModLog.Info("OnDisable：已停桥并卸载宿主对象");
        }
    }

    /// <summary>
    /// 日志出口。旧版走 BepInEx 的 ManualLogSource，mod 版统一走 Unity 日志，
    /// 落点是 %LocalLow%/Questing Goose Studio/Probably Stolen/Player.log。
    /// </summary>
    public static class ModLog
    {
        private const string Prefix = "[DSBridge] ";

        public static void Info(string message) => Debug.Log(Prefix + message);

        public static void Warn(string message) => Debug.LogWarning(Prefix + message);

        public static void Error(string message) => Debug.LogError(Prefix + message);
    }

    /// <summary>
    /// 主线程执行器：Unity API 只能在主线程调用，外部（HTTP 线程）操作经此队列派发。
    /// </summary>
    public class MainThreadDispatcher : MonoBehaviour
    {
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
                    ModLog.Error($"主线程任务异常：{e}");
                }
            }
        }
    }

    /// <summary>
    /// 主线程泵：每帧给 BridgeServer 一次「还没起来就重试」的机会。
    ///
    /// 【v3.10.4，2026-11-14 实机】为什么需要它：BridgeServer.Start() 在 IMod.OnEnable
    /// 里调用，此时 loader 还没把本 mod 标成 running，ModHelper.RunInBackground 返回 false；
    /// 原先用 ModHelper.Delay 做退避重试，但在该状态下 Delay 会静默不调度（返回 null、
    /// 无异常无日志），重试链断开 ⇒ accept 循环永不启动、端口 Listen 但所有请求超时。
    /// Unity 的 Update 一定在跑，所以把重试挂在这里最可靠。
    /// </summary>
    public class BridgePump : MonoBehaviour
    {
        public BridgeServer Server;

        private int _frames;

        private void Update()
        {
            if (Server == null)
            {
                return;
            }
            _frames++;
            try
            {
                Server.Pump(_frames);
            }
            catch (Exception e)
            {
                ModLog.Error($"接受循环泵异常：{e}");
            }
        }
    }
}
