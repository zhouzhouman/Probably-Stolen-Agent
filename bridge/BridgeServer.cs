using System;
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
    /// </summary>
    public class BridgeServer
    {
        private readonly TcpListener _listener;
        private volatile bool _running;

        public BridgeServer(int port)
        {
            _listener = new TcpListener(IPAddress.Loopback, port);
        }

        public void Start()
        {
            _running = true;
            _listener.Start();
            var thread = new Thread(AcceptLoop)
            {
                IsBackground = true,
                Name = "DeepSpaceBridge-Accept",
            };
            thread.Start();
        }

        /// <summary>停止监听。mod 被禁用（OnDisable）时必须调用，否则端口会被占死。</summary>
        public void Stop()
        {
            _running = false;
            try
            {
                _listener.Stop();
            }
            catch (Exception e)
            {
                BridgePlugin.Logger.LogWarning($"停止监听失败：{e.Message}");
            }
        }

        private void AcceptLoop()
        {
            while (_running)
            {
                TcpClient client;
                try
                {
                    client = _listener.AcceptTcpClient();
                }
                catch (Exception e)
                {
                    if (!_running)
                    {
                        break;
                    }
                    BridgePlugin.Logger.LogWarning($"accept 失败：{e.Message}");
                    Thread.Sleep(200);
                    continue;
                }
                ThreadPool.QueueUserWorkItem(_ => HandleClient(client));
            }
        }

        private static void HandleClient(TcpClient client)
        {
            try
            {
                using (client)
                using (var stream = client.GetStream())
                {
                    stream.ReadTimeout = 20000;
                    stream.WriteTimeout = 20000;
                    if (!TryReadRequest(stream, out var method, out var path, out var body))
                    {
                        return;
                    }

                    string responseBody;
                    try
                    {
                        responseBody = BridgeApi.Route(method, path, body);
                    }
                    catch (Exception e)
                    {
                        responseBody = "{\"ok\":false,\"error\":" + BridgeApi.JsonString(e.Message) + "}";
                    }
                    WriteResponse(stream, responseBody);
                }
            }
            catch (Exception e)
            {
                BridgePlugin.Logger.LogWarning($"请求处理失败：{e.Message}");
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
