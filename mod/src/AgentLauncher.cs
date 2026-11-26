using System;
using System.IO;
using System.Text;

namespace DeepSpaceBridge
{
    /// <summary>
    /// 游戏内启动 / 停止 agent（v3.9）。
    ///
    /// 为什么不是直接 Process.Start：游戏 Rev13 的模组扫描器把 **process-start 定为 HardFlag**
    /// （ModScanner.Hard），不是可申明的能力——只要 DLL 里出现 Process.Start，整个 mod 就会被
    /// 拒绝加载。实测 3.8.0 就是这样把桥接一起搞挂了。
    ///
    /// 所以改成「信号文件 + 外部守护进程」：
    ///   游戏内按钮 → 写 agent.request（action=start/stop）
    ///   外部 scripts/agent_hub.py 轮询该文件 → 真正 spawn python
    ///   hub 持续把运行状态写 agent.status.json → 面板据此显示 PID 与最近输出
    ///
    /// 这样 mod 侧只用到 files 能力（可申明），不越权、不绕过安全模型。
    /// </summary>
    public static class AgentLauncher
    {
        private const string RequestFile = "agent.request";
        private const string StatusFile = "agent.status.json";

        public static string LogPath
        {
            get { return Path.Combine(ModelConfig.DataDir, "agent_run.log"); }
        }

        private static string RequestPath
        {
            get { return Path.Combine(ModelConfig.DataDir, RequestFile); }
        }

        private static string StatusPath
        {
            get { return Path.Combine(ModelConfig.DataDir, StatusFile); }
        }

        /// <summary>读 hub 写的状态文件（不缓存：面板每秒读一次，文件很小）。</summary>
        private static string ReadStatus()
        {
            try
            {
                return File.Exists(StatusPath) ? File.ReadAllText(StatusPath, Encoding.UTF8) : "";
            }
            catch
            {
                return "";
            }
        }

        private static bool ReadBool(string json, string key)
        {
            var marker = "\"" + key + "\"";
            var i = json.IndexOf(marker, StringComparison.Ordinal);
            if (i < 0) return false;
            var colon = json.IndexOf(':', i + marker.Length);
            if (colon < 0) return false;
            var rest = json.Substring(colon + 1).TrimStart();
            return rest.StartsWith("true", StringComparison.OrdinalIgnoreCase);
        }

        private static int ReadInt(string json, string key)
        {
            var marker = "\"" + key + "\"";
            var i = json.IndexOf(marker, StringComparison.Ordinal);
            if (i < 0) return 0;
            var colon = json.IndexOf(':', i + marker.Length);
            if (colon < 0) return 0;
            var j = colon + 1;
            while (j < json.Length && (json[j] == ' ' || json[j] == '\t')) j++;
            var start = j;
            while (j < json.Length && (char.IsDigit(json[j]) || json[j] == '-')) j++;
            int val;
            return int.TryParse(json.Substring(start, j - start), out val) ? val : 0;
        }

        /// <summary>
        /// 状态文件是否新鲜（hub 每 0.5 秒写一次）。
        /// 没有这道判断，hub 被杀后状态文件会永远停在 running=true，
        /// 面板显示一个已死的 PID，点「停止」还会报错——实测过一次。
        /// </summary>
        private static bool StatusFresh
        {
            get
            {
                var json = ReadStatus();
                if (json.Length == 0) return false;
                var marker = "\"at\"";
                var i = json.IndexOf(marker, StringComparison.Ordinal);
                if (i < 0) return false;
                var colon = json.IndexOf(':', i);
                var q1 = json.IndexOf('"', colon + 1);
                var q2 = q1 < 0 ? -1 : json.IndexOf('"', q1 + 1);
                if (q1 < 0 || q2 < 0) return false;
                var stamp = json.Substring(q1 + 1, q2 - q1 - 1);
                DateTime at;
                if (!DateTime.TryParse(stamp, out at)) return false;
                return (DateTime.Now - at).TotalSeconds <= 6.0;
            }
        }

        /// <summary>hub 是否在跑（alive=true 且状态文件新鲜）。</summary>
        public static bool HubAlive
        {
            get { return StatusFresh && ReadBool(ReadStatus(), "alive"); }
        }

        public static bool IsRunning
        {
            get { return StatusFresh && ReadBool(ReadStatus(), "running"); }
        }

        public static int ProcessId
        {
            get { return ReadInt(ReadStatus(), "pid"); }
        }

        public static string RecentOutput
        {
            get
            {
                var json = ReadStatus();
                var marker = "\"tail\"";
                var i = json.IndexOf(marker, StringComparison.Ordinal);
                if (i < 0) return "";
                var colon = json.IndexOf(':', i);
                if (colon < 0) return "";
                var q1 = json.IndexOf('"', colon + 1);
                if (q1 < 0) return "";
                // hub 写的是转义后的单行串，这里做最小还原（\\n → 换行）
                var sb = new StringBuilder();
                for (var k = q1 + 1; k < json.Length; k++)
                {
                    var c = json[k];
                    if (c == '"') break;
                    if (c == '\\' && k + 1 < json.Length)
                    {
                        k++;
                        switch (json[k])
                        {
                            case 'n': sb.Append('\n'); break;
                            case 't': sb.Append('\t'); break;
                            case '"': sb.Append('"'); break;
                            case '\\': sb.Append('\\'); break;
                            default: sb.Append(json[k]); break;
                        }
                        continue;
                    }
                    sb.Append(c);
                }
                return sb.ToString();
            }
        }

        /// <summary>给用户看的启动器路径提示（面板里直接显示，省得去翻文档）。</summary>
        public static string LauncherHint()
        {
            var dir = ModelConfig.AgentWorkdir;
            if (!string.IsNullOrEmpty(dir))
            {
                return Path.Combine(dir, "scripts", "start_agent_hub.bat");
            }
            return "scripts\\start_agent_hub.bat";
        }

        /// <summary>写请求文件。返回 null 表示已投递，否则是错误说明。</summary>
        private static string Send(string action, string command, string workDir)
        {
            if (!HubAlive)
            {
                return "启动器没在运行 → 双击 " + LauncherHint();
            }
            try
            {
                var sb = new StringBuilder();
                sb.Append("action=").Append(action).Append('\n');
                sb.Append("command=").Append(command ?? "").Append('\n');
                sb.Append("workdir=").Append(workDir ?? "").Append('\n');
                sb.Append("at=").Append(DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss")).Append('\n');

                // 先写临时文件再替换：hub 可能正读到一半，写到一半的文件会让它解析出空命令
                var tmp = RequestPath + ".tmp";
                File.WriteAllText(tmp, sb.ToString(), new UTF8Encoding(false));
                if (File.Exists(RequestPath)) File.Delete(RequestPath);
                File.Move(tmp, RequestPath);
                return null;
            }
            catch (Exception e)
            {
                return "写入请求失败：" + e.Message;
            }
        }

        public static string Start(string command, string workDir)
        {
            if (IsRunning)
            {
                return $"agent 已在运行（PID {ProcessId}），先停止再启动";
            }
            if (string.IsNullOrEmpty(command))
            {
                return "启动命令为空——在面板里填，或让 deploy_mod.py 自动写入";
            }
            if (string.IsNullOrEmpty(workDir) || !System.IO.Directory.Exists(workDir))
            {
                return $"工作目录不存在：{workDir}";
            }
            return Send("start", command, workDir);
        }

        public static string Stop()
        {
            if (!IsRunning)
            {
                return "agent 没有在运行";
            }
            return Send("stop", "", "");
        }
    }
}
