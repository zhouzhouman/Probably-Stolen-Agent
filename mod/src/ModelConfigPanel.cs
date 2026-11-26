using System;
using System.IO;
using System.Net;
using System.Text;
using System.Threading;
using TMPro;
using UnityEngine;
using UnityEngine.UI;

namespace DeepSpaceBridge
{
    /// <summary>
    /// 游戏内「模型设置」窗口（v3.7.1，Ctrl+F9 显隐）。
    ///
    /// 为什么要有这个窗口：模型名/地址/Key 原来只能改 config/agent.json 与 .env，
    /// 换一个模型要退出游戏、找文件、对齐 JSON 格式——试错成本太高。
    /// 现在游戏里直接改，保存即写 model_config.ini，Python 下次构造客户端时读取。
    ///
    /// v3.7.1 布局重写：原实现手算 anchoredPosition，实机截图里标签与输入框串位
    /// （输入框 x 少算左边距，且各行高度靠魔法数字）。现在全部交给
    /// VerticalLayoutGroup / HorizontalLayoutGroup 排版，不再猜像素。
    ///
    /// 设计取舍：
    ///   · 用 TMP_InputField 做真输入框（游戏自身也在用，程序化创建可行）；
    ///   · Key 用密码模式显示，避免截图/录屏泄露；
    ///   · 「测试连接」走后台线程请求 {base_url}/models，结果回主线程显示——
    ///     绝不能在主线程同步等网络，那会卡住整个游戏。
    /// </summary>
    public class ModelConfigPanel : MonoBehaviour
    {
        public ModelConfigPanel() { }

        private const float PanelW = 520f;
        private const float PanelH = 628f;
        private const float LabelW = 112f;
        private const float RowH = 28f;
        private const float BtnH = 30f;
        private const float DragBandH = 34f;   // 只有标题条能拖动，避免抢输入框点击
        private const int SortingOrder = 5001;

        private static readonly Color PanelColor = new Color(0.075f, 0.065f, 0.050f, 0.98f);
        private static readonly Color Amber = new Color(0.93f, 0.66f, 0.25f);
        private static readonly Color TextColor = new Color(0.96f, 0.94f, 0.90f);
        private static readonly Color DimColor = new Color(0.67f, 0.65f, 0.61f);
        private static readonly Color ErrColor = new Color(0.95f, 0.55f, 0.45f);
        private static readonly Color BtnColor = new Color(1f, 1f, 1f, 0.12f);
        private static readonly Color InputBg = new Color(1f, 1f, 1f, 0.10f);

        private static bool _built;
        private static GameObject _root;
        private static RectTransform _panelRt;
        private static TMP_FontAsset _font;
        private static bool _visible;

        private static TMP_InputField _urlInput;
        private static TMP_InputField _modelInput;
        private static TMP_InputField _keyInput;
        private static TMP_InputField _maxTokensInput;
        private static TMP_InputField _tempInput;
        private static TextMeshProUGUI _statusText;
        private static TMP_InputField _cmdInput;
        private static TMP_InputField _workdirInput;
        private int _statusTick;
        private static readonly GameObject[] _historyButtons = new GameObject[ModelConfig.HistoryMax];
        private static readonly TextMeshProUGUI[] _historyLabels = new TextMeshProUGUI[ModelConfig.HistoryMax];

        private bool _dragging;
        private Vector2 _dragOffset;

        private void Update()
        {
            try
            {
                if (!_built)
                {
                    if (!Build()) return;
                }

                var ctrl = Input.GetKey(KeyCode.LeftControl) || Input.GetKey(KeyCode.RightControl);
                if (ctrl && Input.GetKeyDown(KeyCode.F9))
                {
                    _visible = !_visible;
                    ApplyVisibility();
                }

                if (_visible)
                {
                    UpdateDrag();
                    TickStatus();
                }
            }
            catch (Exception e)
            {
                ModLog.Warn($"[ModelConfigPanel] Update 异常：{e.Message}");
            }
        }

        private static void ApplyVisibility()
        {
            if (_root != null) _root.SetActive(_visible);
            if (_visible)
            {
                RefreshFromConfig();
                // 打开面板就说清楚守护在不在——否则用户点按钮才发现启动失败
                if (!AgentLauncher.HubAlive)
                {
                    SetStatus("启动器未运行：双击 " + AgentLauncher.LauncherHint()
                              + "\n（已设为开机自启，重启电脑后会自动出现）", true);
                }
                else if (AgentLauncher.IsRunning)
                {
                    SetStatus($"agent 运行中（PID {AgentLauncher.ProcessId}）", false);
                }
                else
                {
                    SetStatus("启动器就绪，可以点「▶ 启动 Agent」", false);
                }
            }
        }

        // ------------------------------------------------------------ 拖拽

        private void UpdateDrag()
        {
            if (_panelRt == null)
            {
                return;
            }
            var mp = Input.mousePosition;
            if (Input.GetMouseButtonDown(0))
            {
                // 只在标题条区域开始拖动：否则点输入框会被当成拖拽，输入框永远点不进去
                var inside = RectTransformUtility.RectangleContainsScreenPoint(_panelRt, mp, null);
                var inBand = inside && (mp.y >= _panelRt.position.y - DragBandH);
                if (inBand)
                {
                    _dragOffset = (Vector2)_panelRt.position - (Vector2)mp;
                    _dragging = true;
                }
            }
            if (_dragging && Input.GetMouseButton(0))
            {
                _panelRt.position = (Vector2)mp + _dragOffset;
            }
            if (Input.GetMouseButtonUp(0))
            {
                _dragging = false;
            }
        }

        // ------------------------------------------------------------ 构建

        private static bool Build()
        {
            _font = ResolveFont();
            if (_font == null)
            {
                return false; // 字体未就绪，下一帧再试
            }

            var canvasGo = new GameObject("DeepSpaceModelConfigCanvas",
                typeof(Canvas), typeof(CanvasScaler), typeof(GraphicRaycaster));
            UnityEngine.Object.DontDestroyOnLoad(canvasGo);
            canvasGo.hideFlags = HideFlags.HideAndDontSave;
            var canvas = canvasGo.GetComponent<Canvas>();
            canvas.renderMode = RenderMode.ScreenSpaceOverlay;
            canvas.sortingOrder = SortingOrder;
            var scaler = canvasGo.GetComponent<CanvasScaler>();
            scaler.uiScaleMode = CanvasScaler.ScaleMode.ConstantPixelSize;
            _root = canvasGo;

            // 面板：固定尺寸，内容全部交给 VerticalLayoutGroup 排
            var panelGo = new GameObject("Panel", typeof(RectTransform), typeof(Image));
            _panelRt = (RectTransform)panelGo.transform;
            _panelRt.SetParent(canvasGo.transform, false);
            _panelRt.anchorMin = new Vector2(1f, 1f);
            _panelRt.anchorMax = new Vector2(1f, 1f);
            _panelRt.pivot = new Vector2(1f, 1f);
            _panelRt.anchoredPosition = new Vector2(-40f, -100f);
            _panelRt.sizeDelta = new Vector2(PanelW, PanelH);
            panelGo.GetComponent<Image>().color = PanelColor;

            var contentGo = new GameObject("Content", typeof(RectTransform), typeof(VerticalLayoutGroup));
            var contentRt = (RectTransform)contentGo.transform;
            contentRt.SetParent(_panelRt, false);
            contentRt.anchorMin = Vector2.zero;
            contentRt.anchorMax = Vector2.one;
            contentRt.offsetMin = new Vector2(16f, 14f);
            contentRt.offsetMax = new Vector2(-16f, -12f);
            var vlg = contentGo.GetComponent<VerticalLayoutGroup>();
            vlg.spacing = 8f;
            vlg.childAlignment = TextAnchor.UpperLeft;
            vlg.childControlWidth = true;
            vlg.childControlHeight = true;
            vlg.childForceExpandWidth = true;
            vlg.childForceExpandHeight = false;

            AddTextBlock(contentRt, "模型设置", 15f, Amber, 22f, TextAlignmentOptions.Left);
            AddTextBlock(contentRt, "Ctrl+F9 显隐 · 只改决策模型(brain) · 保存后重开一轮 agent 生效",
                11f, DimColor, 16f, TextAlignmentOptions.Left);

            _urlInput = AddRow(contentRt, "API 地址", ModelConfig.BaseUrl);
            _modelInput = AddRow(contentRt, "模型名", ModelConfig.Model);
            _keyInput = AddRow(contentRt, "API Key", ModelConfig.ApiKey, true);
            _maxTokensInput = AddRow(contentRt, "Max Tokens",
                ModelConfig.MaxTokens > 0 ? ModelConfig.MaxTokens.ToString() : "");
            _tempInput = AddRow(contentRt, "Temperature",
                ModelConfig.Temperature >= 0f ? ModelConfig.Temperature.ToString("0.###") : "");

            AddTextBlock(contentRt, "Agent 启动（点按钮直接跑一轮，日志写 agent_run.log）",
                10f, DimColor, 14f, TextAlignmentOptions.Left);
            _cmdInput = AddRow(contentRt, "启动命令", ModelConfig.AgentCommand);
            _workdirInput = AddRow(contentRt, "工作目录", ModelConfig.AgentWorkdir);

            // 按钮行
            var btnRowGo = new GameObject("Buttons", typeof(RectTransform), typeof(HorizontalLayoutGroup));
            var btnRowRt = (RectTransform)btnRowGo.transform;
            btnRowRt.SetParent(contentRt, false);
            var hlg = btnRowGo.GetComponent<HorizontalLayoutGroup>();
            hlg.spacing = 8f;
            hlg.childAlignment = TextAnchor.MiddleLeft;
            hlg.childControlWidth = true;
            hlg.childControlHeight = true;
            hlg.childForceExpandWidth = true;
            hlg.childForceExpandHeight = false;
            var btnRowLe = btnRowGo.AddComponent<LayoutElement>();
            btnRowLe.preferredHeight = BtnH;
            btnRowLe.minHeight = BtnH;

            AddButton(btnRowRt, "保存并应用", OnSave);
            AddButton(btnRowRt, "重新载入", OnReload);
            AddButton(btnRowRt, "测试连接", OnTest);
            AddButton(btnRowRt, "恢复默认", OnReset);

            var runRowGo = new GameObject("RunButtons", typeof(RectTransform), typeof(HorizontalLayoutGroup));
            var runRowRt = (RectTransform)runRowGo.transform;
            runRowRt.SetParent(contentRt, false);
            var hlgRun = runRowGo.GetComponent<HorizontalLayoutGroup>();
            hlgRun.spacing = 8f;
            hlgRun.childAlignment = TextAnchor.MiddleLeft;
            hlgRun.childControlWidth = true;
            hlgRun.childControlHeight = true;
            hlgRun.childForceExpandWidth = true;
            hlgRun.childForceExpandHeight = false;
            var runLe = runRowGo.AddComponent<LayoutElement>();
            runLe.preferredHeight = BtnH;
            runLe.minHeight = BtnH;
            AddButton(runRowRt, "▶ 启动 Agent", OnStartAgent);
            AddButton(runRowRt, "■ 停止 Agent", OnStopAgent);

            // 最近使用（v3.7.2）：解决"每次都要重新填一遍模型名"
            AddTextBlock(contentRt, "最近使用（点一下填回地址与模型名，不含 Key）", 10f, DimColor, 14f,
                TextAlignmentOptions.Left);
            var histRowGo = new GameObject("History", typeof(RectTransform), typeof(HorizontalLayoutGroup));
            var histRowRt = (RectTransform)histRowGo.transform;
            histRowRt.SetParent(contentRt, false);
            var hlgH = histRowGo.GetComponent<HorizontalLayoutGroup>();
            hlgH.spacing = 6f;
            hlgH.childAlignment = TextAnchor.MiddleLeft;
            hlgH.childControlWidth = true;
            hlgH.childControlHeight = true;
            hlgH.childForceExpandWidth = true;
            hlgH.childForceExpandHeight = false;
            var histLe = histRowGo.AddComponent<LayoutElement>();
            histLe.preferredHeight = 24f;
            histLe.minHeight = 24f;
            for (var hi = 0; hi < ModelConfig.HistoryMax; hi++)
            {
                AddHistoryItem(histRowRt, hi);
            }

            _statusText = AddTextBlock(contentRt, "", 11f, DimColor, 62f, TextAlignmentOptions.TopLeft);
            _statusText.enableWordWrapping = true;

            RefreshFromConfig();
            _built = true;
            _root.SetActive(_visible);
            ModLog.Info("[ModelConfigPanel] 模型设置窗口已就绪（Ctrl+F9 显隐）");
            return true;
        }

        /// <summary>一行 = 标签 + 输入框，交给 HorizontalLayoutGroup 排版。</summary>
        private static TMP_InputField AddRow(RectTransform parent, string label, string value, bool password = false)
        {
            var rowGo = new GameObject("Row_" + label, typeof(RectTransform), typeof(HorizontalLayoutGroup));
            var rowRt = (RectTransform)rowGo.transform;
            rowRt.SetParent(parent, false);
            var hlg = rowGo.GetComponent<HorizontalLayoutGroup>();
            hlg.spacing = 8f;
            hlg.childAlignment = TextAnchor.MiddleLeft;
            hlg.childControlWidth = true;
            hlg.childControlHeight = true;
            hlg.childForceExpandWidth = false;   // 宽度由 LayoutElement 决定：标签固定、输入框伸展
            hlg.childForceExpandHeight = false;
            var rowLe = rowGo.AddComponent<LayoutElement>();
            rowLe.preferredHeight = RowH;
            rowLe.minHeight = RowH;

            // 标签
            var labelGo = new GameObject("Label", typeof(RectTransform), typeof(TextMeshProUGUI));
            var labelRt = (RectTransform)labelGo.transform;
            labelRt.SetParent(rowRt, false);
            var le = labelGo.AddComponent<LayoutElement>();
            le.preferredWidth = LabelW;
            le.minWidth = LabelW;
            le.preferredHeight = RowH;
            le.minHeight = RowH;
            var labelTmp = labelGo.GetComponent<TextMeshProUGUI>();
            labelTmp.font = _font;
            labelTmp.fontSize = 12f;
            labelTmp.color = TextColor;
            labelTmp.text = label;
            labelTmp.alignment = TextAlignmentOptions.MidlineLeft;
            labelTmp.raycastTarget = false;
            labelTmp.enableWordWrapping = false;

            // 输入框
            var go = new GameObject("Input", typeof(RectTransform), typeof(Image), typeof(TMP_InputField));
            var rt = (RectTransform)go.transform;
            rt.SetParent(rowRt, false);
            var le2 = go.AddComponent<LayoutElement>();
            le2.flexibleWidth = 1f;
            le2.minWidth = 120f;
            le2.preferredHeight = RowH;
            le2.minHeight = RowH;

            var bg = go.GetComponent<Image>();
            bg.color = InputBg;

            var area = new GameObject("TextArea", typeof(RectTransform), typeof(RectMask2D));
            var areaRt = (RectTransform)area.transform;
            areaRt.SetParent(rt, false);
            areaRt.anchorMin = Vector2.zero;
            areaRt.anchorMax = Vector2.one;
            areaRt.offsetMin = new Vector2(8f, 3f);
            areaRt.offsetMax = new Vector2(-8f, -3f);

            var textGo = new GameObject("Text", typeof(RectTransform), typeof(TextMeshProUGUI));
            var trt = (RectTransform)textGo.transform;
            trt.SetParent(areaRt, false);
            trt.anchorMin = Vector2.zero;
            trt.anchorMax = Vector2.one;
            trt.offsetMin = Vector2.zero;
            trt.offsetMax = Vector2.zero;
            var tmp = textGo.GetComponent<TextMeshProUGUI>();
            tmp.font = _font;
            tmp.fontSize = 13f;
            tmp.color = TextColor;
            tmp.alignment = TextAlignmentOptions.MidlineLeft;
            tmp.enableWordWrapping = false;
            tmp.richText = false;

            var input = go.GetComponent<TMP_InputField>();
            input.targetGraphic = bg;
            input.textViewport = areaRt;
            input.textComponent = tmp;
            input.lineType = TMP_InputField.LineType.SingleLine;
            if (password)
            {
                input.contentType = TMP_InputField.ContentType.Password;
            }
            input.text = value ?? "";
            return input;
        }

        private static TextMeshProUGUI AddTextBlock(RectTransform parent, string text, float size,
            Color color, float height, TextAlignmentOptions align)
        {
            var go = new GameObject("Text", typeof(RectTransform), typeof(TextMeshProUGUI));
            var rt = (RectTransform)go.transform;
            rt.SetParent(parent, false);
            var le = go.AddComponent<LayoutElement>();
            le.preferredHeight = height;
            le.minHeight = height;
            le.flexibleWidth = 1f;
            var tmp = go.GetComponent<TextMeshProUGUI>();
            tmp.font = _font;
            tmp.fontSize = size;
            tmp.color = color;
            tmp.text = text;
            tmp.alignment = align;
            tmp.raycastTarget = false;
            return tmp;
        }

        private static void AddButton(RectTransform parent, string label, Action onClick)
        {
            var go = new GameObject("Btn_" + label, typeof(RectTransform), typeof(Image), typeof(Button));
            var rt = (RectTransform)go.transform;
            rt.SetParent(parent, false);
            var le = go.AddComponent<LayoutElement>();
            le.flexibleWidth = 1f;
            le.minWidth = 96f;
            le.preferredHeight = BtnH;
            le.minHeight = BtnH;

            var img = go.GetComponent<Image>();
            img.color = BtnColor;

            var textGo = new GameObject("Text", typeof(RectTransform), typeof(TextMeshProUGUI));
            var trt = (RectTransform)textGo.transform;
            trt.SetParent(rt, false);
            trt.anchorMin = Vector2.zero;
            trt.anchorMax = Vector2.one;
            trt.offsetMin = Vector2.zero;
            trt.offsetMax = Vector2.zero;
            var tmp = textGo.GetComponent<TextMeshProUGUI>();
            tmp.font = _font;
            tmp.fontSize = 12f;
            tmp.color = TextColor;
            tmp.text = label;
            tmp.alignment = TextAlignmentOptions.Center;
            tmp.raycastTarget = false;

            var btn = go.GetComponent<Button>();
            btn.targetGraphic = img;
            btn.onClick.AddListener(() =>
            {
                try { onClick(); }
                catch (Exception e) { SetStatus("操作失败：" + e.Message, true); }
            });
        }

        private static void AddHistoryItem(RectTransform parent, int index)
        {
            var go = new GameObject("Hist" + index, typeof(RectTransform), typeof(Image), typeof(Button));
            var rt = (RectTransform)go.transform;
            rt.SetParent(parent, false);
            var le = go.AddComponent<LayoutElement>();
            le.flexibleWidth = 1f;
            le.minWidth = 60f;
            le.preferredHeight = 24f;
            le.minHeight = 24f;
            go.GetComponent<Image>().color = new Color(1f, 1f, 1f, 0.06f);

            var textGo = new GameObject("Text", typeof(RectTransform), typeof(TextMeshProUGUI));
            var trt = (RectTransform)textGo.transform;
            trt.SetParent(rt, false);
            trt.anchorMin = Vector2.zero;
            trt.anchorMax = Vector2.one;
            trt.offsetMin = new Vector2(4f, 0f);
            trt.offsetMax = new Vector2(-4f, 0f);
            var tmp = textGo.GetComponent<TextMeshProUGUI>();
            tmp.font = _font;
            tmp.fontSize = 11f;
            tmp.color = TextColor;
            tmp.alignment = TextAlignmentOptions.Center;
            tmp.enableWordWrapping = false;
            tmp.overflowMode = TextOverflowModes.Ellipsis;
            tmp.raycastTarget = false;

            var btn = go.GetComponent<Button>();
            btn.targetGraphic = go.GetComponent<Image>();
            var captured = index;
            btn.onClick.AddListener(() => ApplyHistory(captured));

            _historyButtons[index] = go;
            _historyLabels[index] = tmp;
            go.SetActive(false);
        }

        /// <summary>点历史条目：只填地址与模型名，Key 不跟着填（安全）。</summary>
        private static void ApplyHistory(int index)
        {
            string url, model;
            if (!ModelConfig.TrySplitHistory(ModelConfig.History[index], out url, out model))
            {
                return;
            }
            if (_urlInput != null) _urlInput.text = url ?? "";
            if (_modelInput != null) _modelInput.text = model ?? "";
            SetStatus($"已填入历史 #{index + 1}：{(string.IsNullOrEmpty(model) ? url : model)}"
                      + "\n（Key 不在历史里，需要时补上再保存）", false);
        }

        private static void OnStartAgent()
        {
            // 全量落盘（模型地址/Key/命令都要）：用户往往改完就直接点启动，
            // 只存命令会让 agent 用旧模型跑起来。
            PullFromUi();
            ModelConfig.PushHistory(ModelConfig.BaseUrl, ModelConfig.Model);
            string err;
            ModelConfig.Save(out err);

            var fail = AgentLauncher.Start(ModelConfig.AgentCommand, ModelConfig.AgentWorkdir);
            if (fail != null)
            {
                SetStatus("启动失败：" + fail, true);
                return;
            }
            SetStatus($"已启动 agent（PID {AgentLauncher.ProcessId}）\n日志：{AgentLauncher.LogPath}", false);
        }

        private static void OnStopAgent()
        {
            var fail = AgentLauncher.Stop();
            SetStatus(fail == null ? "已停止 agent" : fail, fail != null);
        }

        /// <summary>运行中时定期把最近输出刷到状态行，省得用户去翻日志。</summary>
        private void TickStatus()
        {
            _statusTick++;
            if (_statusTick % 60 != 0) return;   // 约 1 秒一次（60fps 下）
            if (!AgentLauncher.IsRunning) return;
            var tail = AgentLauncher.RecentOutput;
            SetStatus($"agent 运行中（PID {AgentLauncher.ProcessId}）"
                      + (string.IsNullOrEmpty(tail) ? "" : "\n" + tail), false);
        }

        // ------------------------------------------------------------ 按钮行为

        private static void RefreshFromConfig()
        {
            if (_urlInput == null) return;
            _urlInput.text = ModelConfig.BaseUrl ?? "";
            _modelInput.text = ModelConfig.Model ?? "";
            _keyInput.text = ModelConfig.ApiKey ?? "";
            _maxTokensInput.text = ModelConfig.MaxTokens > 0 ? ModelConfig.MaxTokens.ToString() : "";
            _tempInput.text = ModelConfig.Temperature >= 0f ? ModelConfig.Temperature.ToString("0.###") : "";
            if (_cmdInput != null) _cmdInput.text = ModelConfig.AgentCommand ?? "";
            if (_workdirInput != null) _workdirInput.text = ModelConfig.AgentWorkdir ?? "";
            for (var i = 0; i < ModelConfig.HistoryMax; i++)
            {
                var has = i < ModelConfig.History.Count;
                if (_historyButtons[i] != null) _historyButtons[i].SetActive(has);
                if (!has || _historyLabels[i] == null) continue;
                string u, m;
                ModelConfig.TrySplitHistory(ModelConfig.History[i], out u, out m);
                var label = string.IsNullOrEmpty(m) ? (u ?? "") : m;
                _historyLabels[i].text = label;
            }
        }

        /// <summary>
        /// 把面板上所有输入框的值同步到静态字段。
        /// 单独抽出来是因为「启动 Agent」也必须先落盘——否则用户改了模型直接点启动，
        /// agent 读到的还是 ini 里的旧模型（实测踩过：面板显示 deepseek，进程却连 tokeness）。
        /// </summary>
        private static void PullFromUi()
        {
            ModelConfig.BaseUrl = (_urlInput.text ?? "").Trim();
            ModelConfig.Model = (_modelInput.text ?? "").Trim();
            ModelConfig.ApiKey = (_keyInput.text ?? "").Trim();
            ModelConfig.AgentCommand = (_cmdInput.text ?? "").Trim();
            ModelConfig.AgentWorkdir = (_workdirInput.text ?? "").Trim();

            int mt;
            ModelConfig.MaxTokens = int.TryParse((_maxTokensInput.text ?? "").Trim(), out mt) ? mt : 0;
            float tp;
            ModelConfig.Temperature = float.TryParse((_tempInput.text ?? "").Trim(), out tp) ? tp : -1f;
        }

        private static void OnSave()
        {
            PullFromUi();
            ModelConfig.PushHistory(ModelConfig.BaseUrl, ModelConfig.Model);

            string err;
            if (ModelConfig.Save(out err))
            {
                RefreshFromConfig();
                SetStatus($"已保存到 {ModelConfig.FileName}\n下一轮 agent 启动时生效（当前进程不会热切换）", false);
            }
            else
            {
                SetStatus("保存失败：" + err, true);
            }
        }

        private static void OnReload()
        {
            ModelConfig.Load();
            RefreshFromConfig();
            SetStatus("已从文件重新载入", false);
        }

        private static void OnReset()
        {
            string err;
            if (ModelConfig.ResetToDefault(out err))
            {
                RefreshFromConfig();
                SetStatus("已恢复默认（删除 model_config.ini，全部沿用 config/agent.json）", false);
            }
            else
            {
                SetStatus("恢复默认失败：" + err, true);
            }
        }

        private static void OnTest()
        {
            var baseUrl = (_urlInput.text ?? "").Trim().TrimEnd('/');
            var model = (_modelInput.text ?? "").Trim();
            var key = (_keyInput.text ?? "").Trim();
            if (string.IsNullOrEmpty(baseUrl))
            {
                SetStatus("API 地址为空，先填一个 OpenAI 兼容的 /v1 地址", true);
                return;
            }
            SetStatus("正在测试连接…", false);

            // 发请求必须离开主线程：主线程同步等网络会卡住整个游戏。
            // v3.11（T01 / CR-T01-2）：原先自己 new Thread，那需要 threads 能力，
            // 而 2026-12-10 之后 threads 被官方硬拒（manifest 里声明也没用）。
            // 改用官方后台原语 ModHelper.RunInBackground —— 语义等价、免能力声明，
            // 且 loader 在 mod 停止时会取消这些后台工作。
            // Probe 是同步阻塞 HTTP，在回调线程里跑没问题；结果仍回主线程显示。
            bool started;
            try
            {
                started = ModHelper.RunInBackground(
                    BridgeServer.ModId,
                    ct =>
                    {
                        var msg = Probe(baseUrl, model, key);
                        MainThreadDispatcher.Enqueue(() => SetStatus(msg, msg.StartsWith("连接失败")));
                    },
                    null);
            }
            catch (Exception e)
            {
                SetStatus("连接测试启动失败：" + e.Message, true);
                return;
            }
            if (!started)
            {
                SetStatus("连接测试启动失败：ModHelper 认为 mod 未在运行", true);
            }
        }

        /// <summary>探测 {baseUrl}/models（标准 OpenAI 兼容端点）。返回人类可读结果。</summary>
        private static string Probe(string baseUrl, string model, string key)
        {
            try
            {
                var req = (HttpWebRequest)WebRequest.Create(baseUrl + "/models");
                req.Method = "GET";
                req.Timeout = 8000;
                req.ReadWriteTimeout = 8000;
                req.Proxy = null; // 本机直连，禁用系统代理（与 Python 侧一致）
                if (!string.IsNullOrEmpty(key))
                {
                    req.Headers["Authorization"] = "Bearer " + key;
                }
                using (var resp = (HttpWebResponse)req.GetResponse())
                using (var reader = new StreamReader(resp.GetResponseStream(), Encoding.UTF8))
                {
                    var body = reader.ReadToEnd();
                    var count = CountModels(body);
                    var head = $"连接成功（HTTP {(int)resp.StatusCode}）";
                    var modelLine = string.IsNullOrEmpty(model)
                        ? "模型名为空，Python 侧会沿用配置文件里的模型"
                        : $"将使用模型：{model}";
                    var countLine = count > 0
                        ? $"服务端返回 {count} 个模型"
                        : "服务端未列出模型（部分中转站不实现 /models，属正常）";
                    return head + "\n" + countLine + "\n" + modelLine;
                }
            }
            catch (WebException we)
            {
                var code = we.Response is HttpWebResponse hr ? (int)hr.StatusCode : 0;
                return $"连接失败：HTTP {code} {we.Status}——检查地址是否需要以 /v1 结尾、Key 是否有效";
            }
            catch (Exception e)
            {
                return "连接失败：" + e.Message;
            }
        }

        private static int CountModels(string json)
        {
            if (string.IsNullOrEmpty(json)) return 0;
            if (json.IndexOf("\"id\"", StringComparison.Ordinal) < 0) return 0;
            var count = 0;
            var pos = 0;
            while (true)
            {
                var i = json.IndexOf("\"id\"", pos, StringComparison.Ordinal);
                if (i < 0) break;
                count++;
                pos = i + 4;
            }
            return count;
        }

        private static void SetStatus(string text, bool isError)
        {
            if (_statusText == null) return;
            _statusText.text = text ?? "";
            _statusText.color = isError ? ErrColor : DimColor;
        }

        private static TMP_FontAsset ResolveFont()
        {
            try
            {
                var loader = FontLoader.Instance;
                if (loader != null)
                {
                    if (loader.simplifiedChineseFont != null) return loader.simplifiedChineseFont;
                    if (loader.notoSans != null) return loader.notoSans;
                }
            }
            catch { }

            try
            {
                var all = Resources.FindObjectsOfTypeAll<TMP_FontAsset>();
                if (all != null)
                {
                    for (var i = 0; i < all.Length; i++)
                    {
                        var f = all[i].TryCast<TMP_FontAsset>();
                        if (f != null) return f;
                    }
                }
            }
            catch { }
            return null;
        }
    }
}
