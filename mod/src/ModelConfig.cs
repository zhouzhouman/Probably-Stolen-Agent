using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Text;

namespace DeepSpaceBridge
{
    /// <summary>
    /// 模型配置：游戏内面板（Ctrl+F9）与 Python 侧共用同一份 ini。
    ///
    /// 为什么用 ini 而不是改 config/agent.json：
    ///   · C# 侧没有 JSON 库，手写 JSON 解析/回写容易把用户配置写坏；
    ///   · ini 是逐行 key=value，写坏了也只影响一行，且人类可读、可手工修；
    ///   · Python 侧读取只需要 10 行代码，不引入依赖。
    ///
    /// 文件位置：Mods/DeepSpaceBridge/model_config.ini（与 mod DLL 同目录）。
    /// 存在即生效；某个键缺失表示"沿用 config/agent.json 的值"。
    /// </summary>
    public static class ModelConfig
    {
        public const string FileName = "model_config.ini";

        /// <summary>OpenAI 兼容的接口根地址，例如 https://api.example.com/v1</summary>
        public static string BaseUrl = "";
        /// <summary>模型名，例如 tokeness/free、gpt-4o-mini</summary>
        public static string Model = "";
        /// <summary>API Key（明文存本地文件；面板里以密码形式显示）</summary>
        public static string ApiKey = "";
        /// <summary>单次回复的最大 token 数，0 表示沿用配置文件</summary>
        public static int MaxTokens;
        /// <summary>温度，-1 表示沿用配置文件</summary>
        public static float Temperature = -1f;

        /// <summary>
        /// 最近用过的模型（"base_url|model"），最多 <see cref="HistoryMax"/> 条。
        /// 面板上的「最近使用」按钮就是从这儿来的——解决"每次都要重新填一遍"。
        /// 刻意**不存 Key**：历史记录会出现在截图里，Key 不该跟着到处跑。
        /// </summary>
        public static readonly List<string> History = new List<string>();
        public const int HistoryMax = 5;

        /// <summary>启动 agent 的完整命令行（含引号包裹的 python 路径）。</summary>
        public static string AgentCommand = "";
        /// <summary>agent 的工作目录（项目根，`-m game_agent.cli` 要靠它找包）。</summary>
        public static string AgentWorkdir = "";

        private static string _cachedPath;

        /// <summary>
        /// 运行期数据目录（**不能放在 Mods 里**）。
        ///
        /// 踩过的坑：Rev13 扫描器会遍历 mod 文件夹并读取每个文件，
        /// 而 agent_run.log 正被外部守护进程写着 → "Sharing violation" → 整个 mod 被拒。
        /// 所以 ini / 日志 / 请求文件 / 状态文件统一放到用户数据目录，mod 目录只留
        /// DLL 与 manifest 两个静态文件。
        /// </summary>
        public static string DataDir
        {
            get
            {
                string root;
                try
                {
                    root = Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData);
                }
                catch { root = null; }
                if (string.IsNullOrEmpty(root))
                {
                    root = ".";
                }
                var dir = Path.Combine(root, "DeepSpaceBridge");
                try { System.IO.Directory.CreateDirectory(dir); } catch { }
                return dir;
            }
        }

        public static string FilePath
        {
            get
            {
                if (_cachedPath == null)
                {
                    _cachedPath = Path.Combine(DataDir, FileName);
                }
                return _cachedPath;
            }
        }

        public static bool Exists
        {
            get
            {
                try { return File.Exists(FilePath); } catch { return false; }
            }
        }

        /// <summary>从 ini 载入（文件不存在则保持空值 = 沿用 Python 配置）。</summary>
        public static void Load()
        {
            if (!Exists)
            {
                return;
            }
            try
            {
                var lines = File.ReadAllLines(FilePath, Encoding.UTF8);
                for (var i = 0; i < lines.Length; i++)
                {
                    var line = lines[i];
                    if (line == null) continue;
                    line = line.Trim();
                    if (line.Length == 0 || line[0] == '#' || line[0] == ';') continue;
                    var eq = line.IndexOf('=');
                    if (eq <= 0) continue;
                    var key = line.Substring(0, eq).Trim().ToLowerInvariant();
                    var val = line.Substring(eq + 1).Trim();
                    switch (key)
                    {
                        case "base_url": BaseUrl = val; break;
                        case "model": Model = val; break;
                        case "api_key": ApiKey = val; break;
                        case "agent_command": AgentCommand = val; break;
                        case "agent_workdir": AgentWorkdir = val; break;
                        case "max_tokens":
                            int mt;
                            if (int.TryParse(val, NumberStyles.Integer, CultureInfo.InvariantCulture, out mt))
                            {
                                MaxTokens = mt;
                            }
                            break;
                        case "temperature":
                            float tp;
                            if (float.TryParse(val, NumberStyles.Float, CultureInfo.InvariantCulture, out tp))
                            {
                                Temperature = tp;
                            }
                            break;
                        default:
                            if (key.StartsWith("history_") && val.Length > 0 && !History.Contains(val))
                            {
                                History.Add(val);
                            }
                            break;
                    }
                }
                ModLog.Info($"[ModelConfig] 已载入 {FilePath}");
            }
            catch (Exception e)
            {
                ModLog.Warn($"[ModelConfig] 载入失败：{e.Message}");
            }
        }

        /// <summary>写回 ini（面板保存时调用）。</summary>
        public static bool Save(out string error)
        {
            error = null;
            try
            {
                var sb = new StringBuilder();
                sb.AppendLine("# DeepSpace Bridge 模型配置（游戏内 Ctrl+F9 面板写入）");
                sb.AppendLine("# 留空表示沿用 config/agent.json；修改后需重开一轮 agent 才生效");
                sb.AppendLine("base_url=" + (BaseUrl ?? ""));
                sb.AppendLine("model=" + (Model ?? ""));
                sb.AppendLine("api_key=" + (ApiKey ?? ""));
                sb.AppendLine("max_tokens=" + (MaxTokens > 0 ? MaxTokens.ToString(CultureInfo.InvariantCulture) : ""));
                sb.AppendLine("temperature=" + (Temperature >= 0f
                    ? Temperature.ToString("0.###", CultureInfo.InvariantCulture)
                    : ""));
                sb.AppendLine("agent_command=" + (AgentCommand ?? ""));
                sb.AppendLine("agent_workdir=" + (AgentWorkdir ?? ""));
                for (var i = 0; i < History.Count && i < HistoryMax; i++)
                {
                    sb.AppendLine("history_" + (i + 1) + "=" + History[i]);
                }
                File.WriteAllText(FilePath, sb.ToString(), new UTF8Encoding(false));
                ModLog.Info($"[ModelConfig] 已保存 {FilePath}");
                return true;
            }
            catch (Exception e)
            {
                error = e.Message;
                ModLog.Error($"[ModelConfig] 保存失败：{e}");
                return false;
            }
        }

        /// <summary>恢复默认：删除 ini（等价于"完全沿用 config/agent.json"）。</summary>
        public static bool ResetToDefault(out string error)
        {
            error = null;
            try
            {
                if (Exists) File.Delete(FilePath);
                BaseUrl = "";
                Model = "";
                ApiKey = "";
                MaxTokens = 0;
                Temperature = -1f;
                History.Clear();
                AgentCommand = "";
                AgentWorkdir = "";
                return true;
            }
            catch (Exception e)
            {
                error = e.Message;
                return false;
            }
        }

        /// <summary>
        /// 把当前 (base_url, model) 记进历史，最近的排最前，重复的提前。
        /// 空值不记——否则会存一堆 "|" 之类的垃圾条目。
        /// </summary>
        public static void PushHistory(string baseUrl, string model)
        {
            if (string.IsNullOrEmpty(baseUrl) && string.IsNullOrEmpty(model))
            {
                return;
            }
            var entry = (baseUrl ?? "").Trim() + "|" + (model ?? "").Trim();
            History.Remove(entry);
            History.Insert(0, entry);
            while (History.Count > HistoryMax)
            {
                History.RemoveAt(History.Count - 1);
            }
        }

        /// <summary>把 "base_url|model" 拆开；格式不对返回 null。</summary>
        public static bool TrySplitHistory(string entry, out string baseUrl, out string model)
        {
            baseUrl = null;
            model = null;
            if (string.IsNullOrEmpty(entry))
            {
                return false;
            }
            var idx = entry.IndexOf('|');
            if (idx < 0)
            {
                return false;
            }
            baseUrl = entry.Substring(0, idx).Trim();
            model = entry.Substring(idx + 1).Trim();
            return true;
        }

        /// <summary>GET /modelconfig 的响应体。</summary>
        public static string ToJson()
        {
            var sb = new StringBuilder(320);
            sb.Append('{');
            sb.Append("\"ok\":true");
            sb.Append(",\"file\":").Append(Quote(FilePath));
            sb.Append(",\"exists\":").Append(Exists ? "true" : "false");
            sb.Append(",\"base_url\":").Append(Quote(BaseUrl ?? ""));
            sb.Append(",\"model\":").Append(Quote(Model ?? ""));
            sb.Append(",\"api_key_set\":").Append(string.IsNullOrEmpty(ApiKey) ? "false" : "true");
            sb.Append(",\"max_tokens\":").Append(MaxTokens);
            sb.Append(",\"temperature\":").Append(Temperature.ToString("0.###", CultureInfo.InvariantCulture));
            sb.Append(",\"agent_command\":").Append(Quote(AgentCommand ?? ""));
            sb.Append(",\"agent_workdir\":").Append(Quote(AgentWorkdir ?? ""));
            sb.Append(",\"agent_running\":").Append(AgentLauncher.IsRunning ? "true" : "false");
            sb.Append(",\"agent_pid\":").Append(AgentLauncher.ProcessId);
            sb.Append(",\"history\":[");
            for (var i = 0; i < History.Count && i < HistoryMax; i++)
            {
                if (i > 0) sb.Append(',');
                sb.Append(Quote(History[i]));
            }
            sb.Append(']');
            sb.Append('}');
            return sb.ToString();
        }

        /// <summary>
        /// POST /modelconfig：从 JSON 覆盖字段并落盘。
        /// 只认这几个键，其余忽略——避免把别的东西写进配置文件。
        /// </summary>
        public static string ApplyJson(string body)
        {
            if (string.IsNullOrEmpty(body))
            {
                return "{\"ok\":false,\"error\":\"请求体为空\"}";
            }
            var baseUrl = ExtractString(body, "base_url");
            var model = ExtractString(body, "model");
            var key = ExtractString(body, "api_key");
            var agentCmd = ExtractString(body, "agent_command");
            var agentDir = ExtractString(body, "agent_workdir");
            var mtText = ExtractString(body, "max_tokens");
            var tpText = ExtractString(body, "temperature");

            if (baseUrl != null) BaseUrl = baseUrl;
            if (model != null) Model = model;
            if (agentCmd != null) AgentCommand = agentCmd;
            if (agentDir != null) AgentWorkdir = agentDir;
            if (key != null) ApiKey = key;
            if (mtText != null)
            {
                int mt;
                MaxTokens = int.TryParse(mtText, NumberStyles.Integer, CultureInfo.InvariantCulture, out mt) ? mt : 0;
            }
            if (tpText != null)
            {
                float tp;
                Temperature = float.TryParse(tpText, NumberStyles.Float, CultureInfo.InvariantCulture, out tp) ? tp : -1f;
            }

            PushHistory(BaseUrl, Model);

            string err;
            if (!Save(out err))
            {
                return "{\"ok\":false,\"error\":" + Quote(err ?? "保存失败") + "}";
            }
            return ToJson();
        }

        /// <summary>从扁平 JSON 里取字段的字符串值（数字也按字符串返回）。缺失返回 null。</summary>
        private static string ExtractString(string json, string key)
        {
            try
            {
                var marker = "\"" + key + "\"";
                var idx = json.IndexOf(marker, StringComparison.Ordinal);
                if (idx < 0) return null;
                var colon = json.IndexOf(':', idx + marker.Length);
                if (colon < 0) return null;
                var i = colon + 1;
                while (i < json.Length && char.IsWhiteSpace(json[i])) i++;
                if (i >= json.Length) return null;
                if (json[i] == '"')
                {
                    var sb = new StringBuilder();
                    i++;
                    while (i < json.Length && json[i] != '"')
                    {
                        if (json[i] == '\\' && i + 1 < json.Length)
                        {
                            i++;
                            switch (json[i])
                            {
                                case 'n': sb.Append('\n'); break;
                                case 't': sb.Append('\t'); break;
                                case 'r': sb.Append('\r'); break;
                                case '"': sb.Append('"'); break;
                                case '\\': sb.Append('\\'); break;
                                default: sb.Append(json[i]); break;
                            }
                            i++;
                            continue;
                        }
                        sb.Append(json[i]);
                        i++;
                    }
                    return sb.ToString();
                }
                // 数字 / true / false / null
                var start = i;
                while (i < json.Length && json[i] != ',' && json[i] != '}' && json[i] != ']') i++;
                var raw = json.Substring(start, i - start).Trim();
                if (raw == "null") return "";
                return raw;
            }
            catch
            {
                return null;
            }
        }

        /// <summary>对外暴露的 JSON 字符串转义（供 BridgeApi 组装错误响应）。</summary>
        public static string EscapeJson(string s)
        {
            return Quote(s);
        }

        private static string Quote(string s)
        {
            if (s == null) return "\"\"";
            var sb = new StringBuilder(s.Length + 8);
            sb.Append('"');
            for (var i = 0; i < s.Length; i++)
            {
                var c = s[i];
                switch (c)
                {
                    case '"': sb.Append("\\\""); break;
                    case '\\': sb.Append("\\\\"); break;
                    case '\n': sb.Append("\\n"); break;
                    case '\r': sb.Append("\\r"); break;
                    case '\t': sb.Append("\\t"); break;
                    default: sb.Append(c); break;
                }
            }
            sb.Append('"');
            return sb.ToString();
        }
    }
}
