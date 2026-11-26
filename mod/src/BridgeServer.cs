using System;
using System.Collections.Concurrent;
using System.IO;
using System.Net;
using System.Net.Sockets;
using System.Text;
using System.Threading;

namespace DeepSpaceBridge
{
    /// <summary>
    /// 极简 HTTP/1.1 服务器（TcpListener 实现，仅监听 127.0.0.1，无需 URL 权限）。
    /// 支持：GET /ping · GET /state · POST /action（JSON 请求体）。
    ///
    /// 【T01 平台合规改造，2026-11-13】为什么不能再直接建线程：
    /// 正式版（100-Rev14 起）把 `threads` 能力标为废弃，2026-12-10 之后的游戏更新
    /// 会把 `new Thread` / `ThreadPool.QueueUserWorkItem` / `Task.Run` / `Task.Delay` /
    /// `System.Threading.Timer` / `System.Timers.Timer` / `Parallel.*` / `ContinueWith`
    /// 全部算作 hard refusal（整个 mod 拒绝加载）。官方给的替代物是
    /// `ModHelper.StartCoroutine / Delay / RunInBackground`，loader 在 mod 停止时
    /// 会取消这些后台工作（ModdingGuide.md「Background work」节）。
    ///
    /// 所以这里：
    ///   · accept 循环  → ModHelper.RunInBackground(ModId, token => ...)
    ///   · 每个连接     → 同样走 ModHelper.RunInBackground（不再用线程池）
    ///   · accept 失败后的退避 → token.WaitHandle.WaitOne(200)：可取消的等待，不创建线程
    ///   · 游戏 API 仍由 BridgeApi.Route 内部 RunOnMain 派发回主线程，后台线程绝不碰 Unity
    ///   · Stop() 取消自己的 CancellationTokenSource 并关掉在途连接 ⇒ OnDisable 能真正停干净
    /// </summary>
    public class BridgeServer
    {
        /// <summary>
        /// mod id 必须与 manifest.xml 里 &lt;ID&gt; 的值完全一致：
        /// ModHelper 会用调用栈（ModLoader.CallingModId）核对调用方，id 不对会抛 ArgumentException。
        /// </summary>
        public const string ModId = "deepspace.bridge";

        private const int SocketTimeoutMs = 20000;
        private const int AcceptBackoffMs = 200;

        /// <summary>
        /// 接受循环启动重试的总次数上限。按 PumpRetryFrames 折算：
        /// 600 次 × 30 帧 ≈ 300 秒（60fps 下 5 分钟），足够覆盖「主菜单 → 读档进店」的加载时间。
        /// </summary>
        private const int MaxStartAttempts = 600;

        /// <summary>主线程泵每多少帧重试一次（30 帧 ≈ 0.5 秒）。</summary>
        private const int PumpRetryFrames = 30;

        private const int MaxConcurrentHandlers = 32;

        private readonly TcpListener _listener;
        private readonly CancellationTokenSource _stop = new CancellationTokenSource();
        private readonly ConcurrentDictionary<TcpClient, byte> _clients = new ConcurrentDictionary<TcpClient, byte>();
        private volatile bool _running;
        private volatile bool _acceptLoopStarted;
        private int _startAttempts;
        private int _handlers;

        /// <summary>接受循环是否已经成功挂到 ModHelper 后台（供主线程泵判断是否还要重试）。</summary>
        public bool AcceptLoopStarted => _acceptLoopStarted;

        public BridgeServer(int port)
        {
            _listener = new TcpListener(IPAddress.Loopback, port);
        }

        public void Start()
        {
            _running = true;
            try
            {
                _listener.Start();
            }
            catch (Exception e)
            {
                _running = false;
                ModLog.Error($"监听 127.0.0.1 失败：{e.Message}");
                return;
            }
            TryStartAcceptLoop();
        }

        /// <summary>
        /// 尝试把接受循环挂到 ModHelper 后台。成功返回 true。
        ///
        /// 【v3.10.4 修复，2026-11-14 实机】为什么不能靠 ModHelper.Delay 重试：
        /// BridgeServer.Start() 在 IMod.OnEnable 里被调用，此时 loader 还没把本 mod
        /// 置为 running，ModHelper.RunInBackground 的 `IsModRunning(modId)` 为假 → 返回 false。
        /// 原实现接着调 ModHelper.Delay(ModId, 1f, TryStartAcceptLoop) 做退避重试，但
        /// Delay 内部同样走 RequireCaller/StartCoroutineInternal，在 mod 未 running 时
        /// **静默不调度**（返回值是 Coroutine，失败只是 null，无异常、无日志），
        /// 于是重试链就此断掉、accept 循环永不启动 —— 端口处于 Listen 但没人 accept()，
        /// 内核完成三次握手后连接全部挂住，所有请求超时。
        /// 实机证据：Player.log 里只有「HTTP 桥已监听」，没有「接受循环已在 ModHelper 后台启动」。
        ///
        /// 所以现在改为**主线程泵驱动**：谁在调 Start()（ModEntry.OnEnable）负责每帧调 Pump()，
        /// 由 Pump 按 PumpRetryFrames 间隔重试，直到成功或超过 MaxStartAttempts。
        /// 主线程 Update 一定在跑，不依赖任何 loader 状态。
        /// </summary>
        public bool TryStartAcceptLoop()
        {
            if (_acceptLoopStarted)
            {
                return true;
            }
            if (!_running)
            {
                return false;
            }

            _startAttempts++;
            bool started;
            try
            {
                started = ModHelper.RunInBackground(ModId, AcceptLoop, () => ModLog.Info("HTTP 接受循环已退出"));
            }
            catch (Exception e)
            {
                ModLog.Error($"ModHelper.RunInBackground 调用失败（mod id 需与 manifest <ID> 一致，且 mod 处于启用状态）：{e.Message}");
                return false;
            }

            if (started)
            {
                _acceptLoopStarted = true;
                ModLog.Info($"接受循环已在 ModHelper 后台启动（第 {_startAttempts} 次尝试）");
                return true;
            }

            if (_startAttempts >= MaxStartAttempts)
            {
                ModLog.Error($"接受循环启动失败：ModHelper 始终认为 mod '{ModId}' 未在运行（已尝试 {_startAttempts} 次，约 {MaxStartAttempts * PumpRetryFrames / 60} 秒）");
            }
            return false;
        }

        /// <summary>
        /// 主线程泵：由挂载在宿主 GameObject 上的组件每帧调用。
        /// 只做「还没起来就定期重试」这一件事，成功后立即返回，几乎零开销。
        ///
        /// 【v3.10.4，2026-11-14 实机】取代 ModHelper.Delay 的退避重试（原因见 TryStartAcceptLoop）。
        /// 不建线程、不占线程池：调用者是 Unity 的 Update，本身就是主线程。
        /// </summary>
        public void Pump(int frameCount)
        {
            if (_acceptLoopStarted || !_running)
            {
                return;
            }
            if (frameCount % PumpRetryFrames != 0)
            {
                return;
            }
            TryStartAcceptLoop();
        }

        /// <summary>停止监听。mod 被禁用（OnDisable）时必须调用，否则端口会被占死。</summary>
        public void Stop()
        {
            _running = false;
            _acceptLoopStarted = false;

            try
            {
                _stop.Cancel();
            }
            catch (Exception e)
            {
                ModLog.Warn($"取消后台任务失败：{e.Message}");
            }

            try
            {
                _listener.Stop();
            }
            catch (Exception e)
            {
                ModLog.Warn($"停止监听失败：{e.Message}");
            }

            foreach (var client in _clients.Keys)
            {
                try
                {
                    client.Close();
                }
                catch
                {
                    // 关闭在途连接失败无所谓：进程还在，句柄由 GC 兜底
                }
            }
            _clients.Clear();
            ModLog.Info("HTTP 桥已停止：接受循环与在途连接均已取消");
        }

        /// <summary>
        /// 接受循环体，运行在 ModHelper 的后台任务里（loader 在 mod 停止时会取消 loaderToken）。
        /// 这里再与自己的 _stop 链接，保证 OnDisable 不必等 loader 也能立刻退出。
        /// </summary>
        private void AcceptLoop(CancellationToken loaderToken)
        {
            using (var linked = CancellationTokenSource.CreateLinkedTokenSource(loaderToken, _stop.Token))
            {
                var token = linked.Token;
                while (_running && !token.IsCancellationRequested)
                {
                    TcpClient client;
                    try
                    {
                        client = _listener.AcceptTcpClient();
                    }
                    catch (Exception e)
                    {
                        if (!_running || token.IsCancellationRequested)
                        {
                            break;
                        }
                        ModLog.Warn($"accept 失败：{e.Message}");
                        // 可取消的退避等待（不占线程池、不创建线程）
                        if (token.WaitHandle.WaitOne(AcceptBackoffMs))
                        {
                            break;
                        }
                        continue;
                    }

                    if (Interlocked.Increment(ref _handlers) > MaxConcurrentHandlers)
                    {
                        Interlocked.Decrement(ref _handlers);
                        ModLog.Warn($"并发请求超过 {MaxConcurrentHandlers}，直接关闭新连接");
                        CloseQuietly(client);
                        continue;
                    }

                    _clients[client] = 0;
                    bool queued;
                    try
                    {
                        queued = ModHelper.RunInBackground(
                            ModId,
                            t => HandleClient(client, t),
                            () =>
                            {
                                Interlocked.Decrement(ref _handlers);
                                _clients.TryRemove(client, out _);
                            });
                    }
                    catch (Exception e)
                    {
                        ModLog.Warn($"派发请求处理失败：{e.Message}");
                        queued = false;
                    }

                    if (!queued)
                    {
                        Interlocked.Decrement(ref _handlers);
                        ModLog.Warn("ModHelper 拒绝新的后台工作，直接关闭该连接");
                        if (_clients.TryRemove(client, out _))
                        {
                            CloseQuietly(client);
                        }
                        else
                        {
                            CloseQuietly(client);
                        }
                    }
                }
            }
        }

        private static void CloseQuietly(TcpClient client)
        {
            try
            {
                client?.Close();
            }
            catch
            {
                // 关闭失败无所谓
            }
        }

        private void HandleClient(TcpClient client, CancellationToken loaderToken)
        {
            using (var linked = CancellationTokenSource.CreateLinkedTokenSource(loaderToken, _stop.Token))
            {
                var token = linked.Token;
                try
                {
                    using (client)
                    using (var stream = client.GetStream())
                    {
                        stream.ReadTimeout = SocketTimeoutMs;
                        stream.WriteTimeout = SocketTimeoutMs;
                        if (token.IsCancellationRequested)
                        {
                            return;
                        }
                        if (!TryReadRequest(stream, out var method, out var path, out var body))
                        {
                            return;
                        }

                        string responseBody;
                        try
                        {
                            // Route 内部一律经 BridgeApi.RunOnMain → MainThreadDispatcher 回到主线程，
                            // 后台线程不直接触达任何 Unity API。
                            responseBody = BridgeApi.Route(method, path, body);
                        }
                        catch (Exception e)
                        {
                            responseBody = "{\"ok\":false,\"error\":" + BridgeApi.JsonString(e.Message) + "}";
                        }

                        if (token.IsCancellationRequested)
                        {
                            return;
                        }
                        WriteResponse(stream, responseBody);
                    }
                }
                catch (Exception e)
                {
                    ModLog.Warn($"请求处理失败：{e.Message}");
                }
            }
        }

        /// <summary>读取并解析请求行/头部（读到 \r\n\r\n），再按 Content-Length 读请求体。</summary>
        private static bool TryReadRequest(NetworkStream stream, out string method, out string path, out string body)
        {
            method = null;
            path = null;
            body = "";

            var headerBuf = new MemoryStream();
            var one = new byte[1];
            int matched = 0;
            while (matched < 4)
            {
                int n = stream.Read(one, 0, 1);
                if (n <= 0)
                {
                    return false;
                }
                byte b = one[0];
                headerBuf.WriteByte(b);
                if ((matched == 0 || matched == 2) && b == (byte)'\r')
                {
                    matched++;
                }
                else if ((matched == 1 || matched == 3) && b == (byte)'\n')
                {
                    matched++;
                }
                else
                {
                    matched = b == (byte)'\r' ? 1 : 0;
                }
                if (headerBuf.Length > 16384)
                {
                    return false;
                }
            }

            var headerText = Encoding.UTF8.GetString(headerBuf.ToArray());
            var lines = headerText.Split(new[] { "\r\n" }, StringSplitOptions.RemoveEmptyEntries);
            if (lines.Length == 0)
            {
                return false;
            }
            var requestLine = lines[0].Split(' ');
            if (requestLine.Length < 2)
            {
                return false;
            }
            method = requestLine[0].ToUpperInvariant();
            path = requestLine[1];

            int contentLength = 0;
            foreach (var line in lines)
            {
                if (line.StartsWith("Content-Length:", StringComparison.OrdinalIgnoreCase))
                {
                    int.TryParse(line.Substring("Content-Length:".Length).Trim(), out contentLength);
                }
            }
            if (contentLength > 0)
            {
                var buf = new byte[contentLength];
                int read = 0;
                while (read < contentLength)
                {
                    int n = stream.Read(buf, read, contentLength - read);
                    if (n <= 0)
                    {
                        break;
                    }
                    read += n;
                }
                body = Encoding.UTF8.GetString(buf, 0, read);
            }
            return true;
        }

        private static void WriteResponse(NetworkStream stream, string body)
        {
            var bodyBytes = Encoding.UTF8.GetBytes(body);
            var head = "HTTP/1.1 200 OK\r\n"
                       + "Content-Type: application/json; charset=utf-8\r\n"
                       + "Content-Length: " + bodyBytes.Length + "\r\n"
                       + "Connection: close\r\n\r\n";
            var headBytes = Encoding.ASCII.GetBytes(head);
            stream.Write(headBytes, 0, headBytes.Length);
            stream.Write(bodyBytes, 0, bodyBytes.Length);
            stream.Flush();
        }
    }
}
