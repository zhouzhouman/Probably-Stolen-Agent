using System;
using System.Collections;
using System.Collections.Concurrent;
using System.Collections.Generic;
using System.Threading;
using UnityEngine;
using UnityEngine.EventSystems;
using UnityEngine.SceneManagement;

namespace DeepSpaceBridge
{
    /// <summary>
    /// 虚拟输入的逐帧驱动器。HTTP 动作在 worker 线程入队，Unity 主线程 LateUpdate
    /// 出队并按帧喂给 VirtualInput —— 每帧调一次 Frame()，游戏在 Update 里读到的
    /// 就是"玩家正在按鼠标/键盘"的状态。
    ///
    /// 关键约束（来自实机教训）：
    ///   · 不能在 RunOnMain 里同步等待跨帧计划，会死锁主线程 → 动作必须绕过 RunOnMain；
    ///   · 场景在计划执行中变化时必须中断，否则会按陈旧状态操作（mod 的 WasInterrupted）；
    ///   · 计划结束或中断都必须 VirtualInput.End() + 还原 inputOverride，绝不留悬挂状态。
    /// </summary>
    public sealed class VirtualInputDriver : MonoBehaviour
    {
        private sealed class Step
        {
            internal Vector2 Point;
            internal Vector2 Scroll;
            internal HashSet<KeyCode> Keys;
            internal int Frames = 1;
        }

        private sealed class Plan
        {
            internal string Kind;
            internal string Scene;
            internal List<Step> Steps = new List<Step>();
            internal ManualResetEventSlim Done = new ManualResetEventSlim(false);
            internal string Result;
            internal bool Ok;
        }

        private sealed class Call
        {
            internal Plan Plan;
        }

        private static readonly ConcurrentQueue<Call> Queue = new ConcurrentQueue<Call>();
        private static VirtualInputDriver _instance;
        private static GameMcpUiInput _uiInput;
        private static readonly Dictionary<BaseInputModule, BaseInput> SavedInputs =
            new Dictionary<BaseInputModule, BaseInput>();

        private Plan _running;

        internal static void Ensure(GameObject host)
        {
            if (_instance != null) return;
            _instance = host.AddComponent<VirtualInputDriver>();
            ModLog.Info("虚拟输入驱动器已挂载（玩家输入链路可用）");
        }

        internal static bool IsVirtualAction(string action)
        {
            return action == "player_click" || action == "player_drag"
                || action == "player_key" || action == "player_wait"
                || action == "player_scroll" || action == "player_stop";
        }

        /// <summary>worker 线程调用：入队计划并等待主线程逐帧执行完（最长 20s）。</summary>
        internal static string Dispatch(string body)
        {
            if (_instance == null) return "{\"ok\":false,\"error\":\"虚拟输入驱动器未初始化\"}";
            if (!LegacyInputPatch.IsSupported)
                return "{\"ok\":false,\"error\":\"虚拟输入需要 Steam Build 26525552 / 100-Rev13，当前程序集版本不匹配\"}";

            string action = BridgeApi.ExtractText(body, "action");
            if (string.IsNullOrEmpty(action)) return "{\"ok\":false,\"error\":\"缺少 action\"}";
            if (action == "player_stop")
            {
                MainThreadDispatcher.Enqueue(() => _instance.StopCurrent("主动取消"));
                return "{\"ok\":true,\"detail\":\"已请求停止当前虚拟输入计划\"}";
            }

            Plan plan;
            try { plan = BuildPlan(action, body); }
            catch (Exception e) { return "{\"ok\":false,\"error\":" + BridgeApi.JsonString(e.Message) + "}"; }
            if (plan == null) return "{\"ok\":false,\"error\":\"无法构造输入计划\"}";

            Queue.Enqueue(new Call { Plan = plan });
            if (!plan.Done.Wait(TimeSpan.FromSeconds(20)))
            {
                MainThreadDispatcher.Enqueue(() => _instance.StopCurrent("超时"));
                return "{\"ok\":false,\"error\":\"虚拟输入计划超时（20s）\"}";
            }
            return plan.Result ?? "{\"ok\":false,\"error\":\"虚拟输入无结果\"}";
        }

        private static Plan BuildPlan(string action, string body)
        {
            var plan = new Plan { Kind = action, Scene = BridgeApi.ExtractText(body, "scene") };
            float x = BridgeApi.ExtractFloat(body, "x", float.NaN);
            float y = BridgeApi.ExtractFloat(body, "y", float.NaN);
            float x2 = BridgeApi.ExtractFloat(body, "to_x", float.NaN);
            float y2 = BridgeApi.ExtractFloat(body, "to_y", float.NaN);
            int frames = BridgeApi.ExtractInt(body, "frames", 1);
            if (frames < 1) frames = 1;
            if (frames > 180) frames = 180;

            switch (action)
            {
                case "player_click":
                    if (float.IsNaN(x) || float.IsNaN(y))
                        throw new ArgumentException("player_click 需要 x/y（Unity 屏幕坐标，左下原点）");
                    // modifier：按住某键再点（如 LeftControl = 游戏里的 Ctrl+点击快速转移/上桌）。
                    string modName = BridgeApi.ExtractText(body, "modifier");
                    KeyCode mod = 0;
                    if (!string.IsNullOrEmpty(modName) && !Enum.TryParse(modName, true, out mod))
                        throw new ArgumentException($"未知 modifier KeyCode：{modName}");
                    var modKeys = mod == 0
                        ? new HashSet<KeyCode>()
                        : new HashSet<KeyCode> { mod };
                    var downKeys = new HashSet<KeyCode>(modKeys) { KeyCode.Mouse0 };
                    plan.Steps.Add(new Step { Point = new Vector2(x, y), Keys = modKeys, Frames = 1 });   // 移到目标、按住修饰键
                    plan.Steps.Add(new Step { Point = new Vector2(x, y), Keys = downKeys, Frames = 1 });  // 按下
                    plan.Steps.Add(new Step { Point = new Vector2(x, y), Keys = modKeys, Frames = 1 });   // 抬起鼠标，修饰键仍在
                    break;
                case "player_drag":
                    if (float.IsNaN(x) || float.IsNaN(y) || float.IsNaN(x2) || float.IsNaN(y2))
                        throw new ArgumentException("player_drag 需要 x/y/to_x/to_y");
                    plan.Steps.Add(StepAt(x, y, 1));
                    const int dragSteps = 8;
                    for (int i = 1; i <= dragSteps; i++)
                        plan.Steps.Add(StepAt(Mathf.Lerp(x, x2, (float)i / dragSteps),
                                              Mathf.Lerp(y, y2, (float)i / dragSteps), 1));
                    plan.Steps.Add(StepAt(x2, y2, 2));
                    break;
                case "player_key":
                    string key = BridgeApi.ExtractText(body, "key");
                    if (string.IsNullOrEmpty(key)) throw new ArgumentException("player_key 需要 key（KeyCode 名）");
                    if (!Enum.TryParse(key, true, out KeyCode code))
                        throw new ArgumentException($"未知 KeyCode：{key}");
                    var held = new HashSet<KeyCode> { code };
                    plan.Steps.Add(new Step { Point = VirtualInput.Point, Keys = held, Frames = frames });
                    plan.Steps.Add(new Step { Point = VirtualInput.Point, Keys = new HashSet<KeyCode>(), Frames = 1 });
                    break;
                case "player_scroll":
                    if (float.IsNaN(x) || float.IsNaN(y))
                        throw new ArgumentException("player_scroll 需要 x/y");
                    float amount = BridgeApi.ExtractFloat(body, "amount", 1f);
                    plan.Steps.Add(new Step { Point = new Vector2(x, y), Keys = new HashSet<KeyCode>(),
                                              Frames = 1, Scroll = new Vector2(0, amount) });
                    break;
                case "player_wait":
                    plan.Steps.Add(new Step { Point = VirtualInput.Point, Keys = new HashSet<KeyCode>(), Frames = frames });
                    break;
                default:
                    return null;
            }
            return plan;
        }

        private static Step StepAt(float x, float y, int frames)
        {
            return new Step
            {
                Point = new Vector2(x, y),
                Keys = frames >= 2 ? new HashSet<KeyCode>() : new HashSet<KeyCode> { KeyCode.Mouse0 },
                Frames = frames
            };
        }

        private void LateUpdate()
        {
            if (_running != null) return;
            if (!Queue.TryDequeue(out var call)) return;
            _running = call.Plan;
            StartCoroutine(Play(_running));
        }

        private IEnumerator Play(Plan plan)
        {
            InstallInput();
            VirtualInput.Begin(plan.Steps[0].Point);
            int played = 0;
            try
            {
                foreach (var step in plan.Steps)
                {
                    for (int i = 0; i < step.Frames; i++)
                    {
                        if (WasInterrupted(plan))
                        {
                            Finish(plan, false, "场景变化或计划被取消，已中断");
                            yield break;
                        }
                        VirtualInput.Frame(step.Point, step.Keys, i == 0 ? step.Scroll : Vector2.zero);
                        played++;
                        yield return null;
                    }
                }
                if (WasInterrupted(plan))
                {
                    Finish(plan, false, "计划结束时场景已变化");
                    yield break;
                }
                Finish(plan, true, $"已逐帧执行 {played} 帧（{plan.Kind}）");
            }
            finally
            {
                if (_running == plan)
                {
                    VirtualInput.End();
                    RestoreInput();
                    _running = null;
                }
            }
        }

        private static bool WasInterrupted(Plan plan)
        {
            if (plan.Done.IsSet) return true;
            return !string.IsNullOrEmpty(plan.Scene) && plan.Scene != SceneManager.GetActiveScene().name;
        }

        private void Finish(Plan plan, bool ok, string detail)
        {
            if (plan.Done.IsSet) return;
            plan.Ok = ok;
            plan.Result = "{\"ok\":" + (ok ? "true" : "false")
                + ",\"dispatched\":true,\"frames\":" + plan.Steps.Count
                + ",\"detail\":" + BridgeApi.JsonString(detail) + "}";
            plan.Done.Set();
        }

        internal void StopCurrent(string reason)
        {
            if (_running != null) Finish(_running, false, reason);
        }

        private static void InstallInput()
        {
            var es = EventSystem.current;
            if (es == null) return;
            if (_uiInput == null) _uiInput = es.gameObject.AddComponent<GameMcpUiInput>();
            foreach (var module in es.GetComponents<StandaloneInputModule>())
            {
                var input = module.inputOverride;
                if (input is GameMcpUiInput) continue;
                SavedInputs[module] = input;
                module.inputOverride = _uiInput;
            }
        }

        private static void RestoreInput()
        {
            foreach (var kv in SavedInputs)
            {
                if (kv.Key != null) kv.Key.inputOverride = kv.Value;
            }
            SavedInputs.Clear();
        }

        private void OnDestroy()
        {
            if (VirtualInput.Active) VirtualInput.End();
            RestoreInput();
            _instance = null;
        }
    }
}
