using System;
using System.Collections.Generic;
using System.Globalization;
using System.Text;
using TMPro;
using UnityEngine;
using UnityEngine.UI;

namespace DeepSpaceBridge
{
    /// <summary>
    /// 决策快照：不可变副本，供 UI（主线程）安全读取。
    /// HTTP 线程写入 DecisionStore，Update 每帧取快照刷新，两边不共享可变状态。
    /// </summary>
    public sealed class DecisionSnapshot
    {
        public long Version;
        public bool Visible;
        public bool HasDecision;
        public string Action;
        public string ParamsText;
        public string Reason;
        public double Confidence;
        public int Step;
        public string Ts;
        public List<string> HistoryLines;
    }

    /// <summary>
    /// 决策存储（线程安全）：HTTP 线程写（POST /decision），主线程读（Update）。
    /// Version 单调递增，UI 据此做脏检查，无变化时零刷新成本。
    /// </summary>
    public static class DecisionStore
    {
        private static readonly object Gate = new object();
        private static long _version;
        private static bool _visible = true;
        private static bool _has;
        private static string _action = "";
        private static string _paramsText = "";
        private static string _reason = "";
        private static string _ts = "";
        private static double _confidence;
        private static int _step;
        private static readonly List<string> _history = new List<string>();
        private const int MaxHistory = 8;

        /// <summary>记录一条决策（来自 Python 推送）。</summary>
        public static void Update(string action, string paramsText, string reason,
                                  double confidence, int step, string ts)
        {
            lock (Gate)
            {
                _has = true;
                _action = action ?? "";
                _paramsText = paramsText ?? "";
                _reason = reason ?? "";
                _confidence = confidence;
                _step = step;
                _ts = ts ?? "";
                _version++;
                var line = "#" + _step + "  " + _action;
                if (!string.IsNullOrEmpty(_paramsText))
                {
                    line += "  " + _paramsText;
                }
                _history.Insert(0, line);
                if (_history.Count > MaxHistory)
                {
                    _history.RemoveAt(_history.Count - 1);
                }
            }
        }

        /// <summary>显示 / 隐藏浮层。</summary>
        public static void SetVisible(bool visible)
        {
            lock (Gate)
            {
                if (_visible != visible)
                {
                    _version++;
                }
                _visible = visible;
            }
        }

        /// <summary>取当前状态副本（Update 每帧调用；锁内只做浅拷贝，成本可忽略）。</summary>
        public static DecisionSnapshot Snapshot()
        {
            lock (Gate)
            {
                return new DecisionSnapshot
                {
                    Version = _version,
                    Visible = _visible,
                    HasDecision = _has,
                    Action = _action,
                    ParamsText = _paramsText,
                    Reason = _reason,
                    Confidence = _confidence,
                    Step = _step,
                    Ts = _ts,
                    HistoryLines = new List<string>(_history),
                };
            }
        }
    }

    /// <summary>
    /// 游戏内 AI 决策展示浮层 v2.0（uGUI / Update 驱动）。
    ///
    /// 设计背景：IMGUI 的 OnGUI 被 IL2CPP 链接器剥离（游戏自身不用 IMGUI，
    /// 日志报 Method unstripping failed），故改用 uGUI：ScreenSpaceOverlay Canvas
    /// + Image 面板 + TextMeshProUGUI 文本，由 Update() 脏检查驱动。
    ///
    /// v2.0 视觉设计（当铺深夜风）：
    /// - 程序化圆角九宫格面板 + 投影层（构建失败自动降级为方角，绝不影响功能）
    /// - 分区排版：标题栏（琥珀强调块）/ 当前动作 / 参数 / 理由 / 最近决策 / 页脚
    /// - 页脚含置信度进度条（绿≥0.8 / 琥珀≥0.5 / 红&lt;0.5）与步骤·时间·置信度元信息
    /// - Ctrl+F10 切换显隐；「—」收起为「AI」小徽章；面板可拖动
    /// - 不加 GraphicRaycaster：不接收也不拦截游戏输入（交互全靠屏幕坐标判定）
    /// - 字体：优先游戏 FontLoader.Instance.simplifiedChineseFont（简体中文），
    ///   回退 notoSans / 场景内任意 TMP_FontAsset
    /// </summary>
    public class DecisionOverlay : MonoBehaviour
    {
        public DecisionOverlay() { }

        // ---- 布局常量（v2.0）----
        private const float PanelWidth = 340f;
        private const float TextPad = 14f;        // 文本左右内边距
        private const float HeaderH = 38f;        // 标题栏高度
        private const float ActionH = 30f;        // 动作行（16px 字号）
        private const float ParamH = 18f;         // 参数行（12px）
        private const float ReasonLineH = 17f;    // 理由行高（12px）
        private const float HistLineH = 16f;      // 历史行高（11px）
        private const float SectionGap = 8f;      // 分区间距
        private const float FooterH = 30f;        // 页脚（元信息 + 置信度条）
        private const float PadBottom = 12f;
        private const int OverlaySortingOrder = 5000; // 高于游戏自身 UI，保证可见
        private const int MaxHistoryShown = 6;
        private const int BuildRetryFrames = 120;     // 构建失败重试间隔（帧）

        // ---- 配色（深色当铺风）----
        private static readonly Color PanelColor = new Color(0.075f, 0.065f, 0.050f, 0.92f);
        private static readonly Color ShadowColor = new Color(0f, 0f, 0f, 0.38f);
        private static readonly Color AmberColor = new Color(0.93f, 0.66f, 0.25f);
        private static readonly Color ActionColor = new Color(0.96f, 0.94f, 0.90f);
        private static readonly Color ParamColor = new Color(0.82f, 0.73f, 0.55f);
        private static readonly Color ReasonColor = new Color(0.67f, 0.65f, 0.61f);
        private static readonly Color HistColor = new Color(0.55f, 0.53f, 0.50f);
        private static readonly Color MetaColor = new Color(0.52f, 0.50f, 0.47f);
        private static readonly Color DividerColor = new Color(1f, 1f, 1f, 0.07f);
        private static readonly Color BarBgColor = new Color(1f, 1f, 1f, 0.08f);

        // uGUI 结构（静态：组件随桥 GameObject 跨场景存活，只构建一次）
        private static bool _uiBuilt;
        private static int _nextBuildFrame;
        private static GameObject _root;
        private static GameObject _panelGo;
        private static RectTransform _panelRt;
        private static TextMeshProUGUI _titleText;
        private static TextMeshProUGUI _actionText;
        private static TextMeshProUGUI _paramsText;
        private static TextMeshProUGUI _reasonText;
        private static TextMeshProUGUI _historyText;
        private static TextMeshProUGUI _metaText;
        private static RectTransform _confFillRt;   // 置信度进度条填充
        private static Image _confFillImg;
        private static TextMeshProUGUI _collapseText; // 「—」收起按钮文本
        private static GameObject _badgeGo;          // 收起时显示的「AI」小方块
        private static RectTransform _badgeRt;
        private static bool _collapsed;              // 面板收起状态（v0.6.0）
        private static bool _collapseDownHandled;    // 同一次点击只触发一个按钮
        private static TMP_FontAsset _font;
        private static Sprite _roundedSprite;        // 程序化圆角九宫格 sprite

        // 运行时脏检查状态
        private long _lastVersion = -1;
        private bool _visibleApplied = true;

        // 面板拖动状态（v0.4.2）
        private bool _dragging;
        private Vector2 _dragOffset;

        /// <summary>
        /// 面板拖动：在面板上按住左键拖到任意位置。
        /// ScreenSpaceOverlay 且无 CanvasScaler（像素 1:1），屏幕坐标与
        /// anchoredPosition（右上锚点）可直接换算：rel = mouse - screenSize。
        /// </summary>
        private void UpdateDrag()
        {
            if (_panelRt == null || !DecisionStore.Snapshot().Visible)
            {
                _dragging = false;
                return;
            }
            var mp = Input.mousePosition; // 原点左下，与 anchoredPosition 同坐标系
            if (Input.GetMouseButtonDown(0))
            {
                var pos = _panelRt.anchoredPosition;
                var size = _panelRt.sizeDelta;
                // 面板左下角锚点：屏幕坐标直接等于 anchoredPosition 空间
                var left = pos.x;
                var bottom = pos.y;
                if (mp.x >= left && mp.x <= left + size.x && mp.y >= bottom && mp.y <= bottom + size.y)
                {
                    _dragging = true;
                    _dragOffset = new Vector2(pos.x, pos.y) - new Vector2(mp.x, mp.y);
                }
            }
            if (_dragging && Input.GetMouseButton(0))
            {
                _panelRt.anchoredPosition = new Vector2(mp.x, mp.y) + _dragOffset;
            }
            if (Input.GetMouseButtonUp(0))
            {
                _dragging = false;
            }
        }

        // ------------------------------------------------------------------
        // 收起 / 恢复按钮（v0.6.0）
        // ------------------------------------------------------------------

        /// <summary>「—」按钮的屏幕矩形（面板右上角内侧；屏幕坐标 y 向上）。
        /// v0.6.1：热区放大到 44×32（视觉「—」不变），降低点击命中难度。</summary>
        private static Rect GetCollapseButtonRect()
        {
            var pos = _panelRt.anchoredPosition;
            float right = pos.x + _panelRt.sizeDelta.x;  // 面板右边缘
            float top = pos.y + _panelRt.sizeDelta.y;    // 面板上边缘
            return Rect.MinMaxRect(right - 44f, top - 36f, right - 4f, top - 4f);
        }

        /// <summary>「AI」小方块的屏幕矩形（收起时显示在原面板位置）。</summary>
        private static Rect GetBadgeRect()
        {
            var pos = _badgeRt.anchoredPosition;
            var size = _badgeRt.sizeDelta;
            return Rect.MinMaxRect(pos.x, pos.y, pos.x + size.x, pos.y + size.y);
        }

        /// <summary>点击「—」收起面板 / 点击「AI」恢复（每帧左键按下时检测一次）。</summary>
        private void UpdateCollapseButton()
        {
            if (_panelRt == null || !DecisionStore.Snapshot().Visible)
            {
                return;
            }
            if (!Input.GetMouseButtonDown(0))
            {
                _collapseDownHandled = false;
                return;
            }
            if (_collapseDownHandled)
            {
                return; // 同一次点击已处理
            }
            var mp = Input.mousePosition;
            if (_collapsed)
            {
                if (_badgeRt != null && GetBadgeRect().Contains(mp))
                {
                    _collapsed = false;
                    ApplyCollapsed();
                    _collapseDownHandled = true;
                }
            }
            else
            {
                if (GetCollapseButtonRect().Contains(mp))
                {
                    _collapsed = true;
                    _dragging = false; // 收起同时取消拖动
                    ApplyCollapsed();
                    _collapseDownHandled = true;
                }
            }
        }

        /// <summary>应用收起状态：面板隐藏 + 小方块显示（或反向）。</summary>
        private static void ApplyCollapsed()
        {
            try
            {
                if (_panelGo != null) _panelGo.SetActive(!_collapsed);
                if (_badgeGo != null) _badgeGo.SetActive(_collapsed);
            }
            catch (Exception e)
            {
                ModLog.Warn($"浮层收起切换失败：{e.Message}");
            }
        }

        private void Update()
        {
            // Ctrl+F10 切换显隐（键盘状态查询失败不致命，静默跳过）
            try
            {
                if ((Input.GetKey(KeyCode.LeftControl) || Input.GetKey(KeyCode.RightControl))
                    && Input.GetKeyDown(KeyCode.F10))
                {
                    var cur = DecisionStore.Snapshot();
                    DecisionStore.SetVisible(!cur.Visible);
                }
            }
            catch (Exception)
            {
                // Input 异常时忽略，不影响其余逻辑
            }

            // 鼠标拖动面板（v0.4.2：按住面板拖到任意位置；Input 异常静默跳过）
            try
            {
                UpdateDrag();
            }
            catch (Exception)
            {
                // 拖动失败不致命
            }

            // 「—」收起 / 「AI」恢复按钮（v0.6.0：点击热区检测，与拖动同模式）
            try
            {
                UpdateCollapseButton();
            }
            catch (Exception)
            {
                // 按钮检测失败不致命
            }

            if (!_uiBuilt)
            {
                // 字体依赖游戏 FontLoader，主菜单/加载完成前可能尚未就绪：退避重试
                if (Time.frameCount < _nextBuildFrame) return;
                TryBuildUI();
                if (!_uiBuilt) return;
            }

            var snap = DecisionStore.Snapshot();

            if (snap.Visible != _visibleApplied)
            {
                _visibleApplied = snap.Visible;
                ApplyVisible(snap.Visible);
            }

            // 脏检查：无新决策时不做任何 TMP 重建
            if (snap.Version != _lastVersion)
            {
                _lastVersion = snap.Version;
                Refresh(snap);
            }
        }

        private void OnDestroy()
        {
            try
            {
                if (_root != null)
                {
                    UnityEngine.Object.Destroy(_root);
                }
            }
            catch (Exception)
            {
                // 忽略销毁期异常
            }
            finally
            {
                _root = null;
                _uiBuilt = false;
            }
        }

        // ------------------------------------------------------------------
        // 程序化圆角九宫格 sprite（v2.0）
        // ------------------------------------------------------------------

        /// <summary>
        /// 生成 64×64 圆角白色 sprite（带 1px 抗锯齿 + 九宫格边框）。
        /// IL2CPP 下 Sprite.Create / Texture2D 一般可用；失败返回 null（降级方角）。
        /// </summary>
        private static Sprite MakeRoundedSprite(int size, int radius)
        {
            try
            {
                var tex = new Texture2D(size, size, TextureFormat.RGBA32, false);
                tex.wrapMode = TextureWrapMode.Clamp;
                var c = (size - 1) / 2f;
                var inner = c - radius;
                for (var y = 0; y < size; y++)
                {
                    for (var x = 0; x < size; x++)
                    {
                        var qx = Mathf.Max(Mathf.Abs(x - c) - inner, 0f);
                        var qy = Mathf.Max(Mathf.Abs(y - c) - inner, 0f);
                        var d = Mathf.Sqrt(qx * qx + qy * qy) - radius;
                        var a = Mathf.Clamp01(0.5f - d); // ≈1px 边缘抗锯齿
                        tex.SetPixel(x, y, new Color(1f, 1f, 1f, a));
                    }
                }
                tex.Apply();
                var sp = Sprite.Create(tex, new Rect(0f, 0f, size, size),
                                       new Vector2(0.5f, 0.5f), 100f, 0u,
                                       SpriteMeshType.FullRect,
                                       new Vector4(radius, radius, radius, radius));
                sp.name = "DeepSpaceOverlayRounded";
                return sp;
            }
            catch (Exception e)
            {
                ModLog.Warn($"圆角 sprite 生成失败（降级方角面板）：{e.Message}");
                return null;
            }
        }

        /// <summary>创建带圆角背景的 Image（sprite 不可用时降级纯色方角）。</summary>
        private static Image CreatePanelImage(GameObject go, Color color)
        {
            var img = go.AddComponent<Image>();
            img.color = color;
            img.raycastTarget = false;
            if (_roundedSprite != null)
            {
                img.sprite = _roundedSprite;
                img.type = Image.Type.Sliced;
            }
            return img;
        }

        // ------------------------------------------------------------------
        // uGUI 构建
        // ------------------------------------------------------------------

        /// <summary>
        /// 构建浮层 UI。任何一步失败都整体放弃并安排重试（字体未就绪是常见原因）。
        /// </summary>
        private static void TryBuildUI()
        {
            _nextBuildFrame = Time.frameCount + BuildRetryFrames;

            // 1. 字体（必须先就绪，否则文本渲染为空）
            if (_font == null)
            {
                _font = ResolveFont();
            }
            if (_font == null)
            {
                ModLog.Warn("决策浮层：尚未找到可用中文字体，稍后重试");
                return;
            }

            try
            {
                if (_roundedSprite == null)
                {
                    _roundedSprite = MakeRoundedSprite(64, 14);
                }

                // 2. 根节点 + Canvas（ScreenSpaceOverlay，无需 CanvasScaler，像素坐标 1:1）
                var rectTypes = new System.Type[1];
                rectTypes[0] = typeof(RectTransform);
                var root = new GameObject("DeepSpaceDecisionOverlay", rectTypes);
                UnityEngine.Object.DontDestroyOnLoad(root);
                root.hideFlags = HideFlags.HideAndDontSave;

                var canvas = root.AddComponent<Canvas>();
                canvas.renderMode = RenderMode.ScreenSpaceOverlay;
                canvas.sortingOrder = OverlaySortingOrder;

                // 3. 投影层（面板后 3px 的暗色圆角底，制造悬浮感）
                var shadowGo = new GameObject("Shadow", rectTypes);
                shadowGo.transform.SetParent(root.transform, false);
                var shadowRt = shadowGo.GetComponent<RectTransform>();
                shadowRt.anchorMin = new Vector2(0f, 0f);
                shadowRt.anchorMax = new Vector2(0f, 0f);
                shadowRt.pivot = new Vector2(0f, 0f);
                shadowRt.anchoredPosition = new Vector2(17f, 11f);
                shadowRt.sizeDelta = new Vector2(PanelWidth, 120f);
                CreatePanelImage(shadowGo, ShadowColor);

                // 4. 主面板（圆角深色半透明，锚定右上角）
                var panelGo = new GameObject("Panel", rectTypes);
                panelGo.transform.SetParent(root.transform, false);
                _panelGo = panelGo;
                _panelRt = panelGo.GetComponent<RectTransform>();
                // v2.1：默认贴左下角空白区（右上角会挡住游戏的购买/谈判面板）
                _panelRt.anchorMin = new Vector2(0f, 0f);
                _panelRt.anchorMax = new Vector2(0f, 0f);
                _panelRt.pivot = new Vector2(0f, 0f);
                _panelRt.anchoredPosition = new Vector2(14f, 14f);
                _panelRt.sizeDelta = new Vector2(PanelWidth, 120f);
                CreatePanelImage(panelGo, PanelColor);

                // 4.1 标题栏：琥珀强调块 + 标题 + 「—」收起
                var accentGo = new GameObject("HeaderAccent", rectTypes);
                accentGo.transform.SetParent(panelGo.transform, false);
                var accentRt = accentGo.GetComponent<RectTransform>();
                accentRt.anchorMin = new Vector2(0f, 1f);
                accentRt.anchorMax = new Vector2(0f, 1f);
                accentRt.pivot = new Vector2(0f, 1f);
                accentRt.anchoredPosition = new Vector2(14f, -11f);
                accentRt.sizeDelta = new Vector2(4f, 16f);
                var accentImg = accentGo.AddComponent<Image>();
                accentImg.color = AmberColor;
                accentImg.raycastTarget = false;

                _titleText = CreateText(panelGo, "Title", 24f, -12f, HeaderH, 13f, AmberColor);
                _titleText.text = "深空当铺 · AI 决策";

                _collapseText = CreateText(panelGo, "CollapseBtn", 0f, -13f, HeaderH, 15f,
                    new Color(0.62f, 0.60f, 0.56f));
                _collapseText.text = "—";
                _collapseText.alignment = TextAlignmentOptions.Center;
                var ctRt = _collapseText.rectTransform;
                ctRt.anchorMin = new Vector2(1f, 1f);
                ctRt.anchorMax = new Vector2(1f, 1f);
                ctRt.pivot = new Vector2(1f, 1f);
                ctRt.anchoredPosition = new Vector2(-26f, -6f);
                ctRt.sizeDelta = new Vector2(20f, 24f);

                // 4.2 标题栏下的分隔线
                CreateDivider(panelGo, rectTypes, HeaderH);

                // 4.3 当前动作行（大号亮白，置信度着色在 Refresh 里做）
                var bodyTop = HeaderH + 10f;
                _actionText = CreateText(panelGo, "Action", TextPad, -bodyTop, ActionH, 16f,
                    ActionColor);
                _actionText.text = "";

                // 4.4 参数 / 理由 / 最近决策（各自独立节点，Refresh 按内容显隐排版）
                var paramTop = bodyTop + ActionH;
                _paramsText = CreateText(panelGo, "Params", TextPad, -paramTop, ParamH, 12f,
                    ParamColor);
                _paramsText.text = "";

                var reasonTop = paramTop + ParamH;
                _reasonText = CreateText(panelGo, "Reason", TextPad, -reasonTop, 0f, 12f,
                    ReasonColor);
                _reasonText.text = "";

                var histTop = reasonTop + SectionGap;
                _historyText = CreateText(panelGo, "History", TextPad, -histTop, 0f, 11f,
                    HistColor);
                _historyText.text = "";

                // 4.5 页脚：分隔线 + 置信度进度条 + 元信息
                var footerDivider = new GameObject("FooterDivider", rectTypes);
                footerDivider.transform.SetParent(panelGo.transform, false);
                var fdRt = footerDivider.GetComponent<RectTransform>();
                fdRt.anchorMin = new Vector2(0f, 0f);
                fdRt.anchorMax = new Vector2(0f, 0f);
                fdRt.pivot = new Vector2(0f, 0f);
                fdRt.anchoredPosition = new Vector2(TextPad, FooterH - 6f);
                fdRt.sizeDelta = new Vector2(PanelWidth - TextPad * 2f, 1f);
                var fdImg = footerDivider.AddComponent<Image>();
                fdImg.color = DividerColor;
                fdImg.raycastTarget = false;

                var barBgGo = new GameObject("ConfBarBg", rectTypes);
                barBgGo.transform.SetParent(panelGo.transform, false);
                var barBgRt = barBgGo.GetComponent<RectTransform>();
                barBgRt.anchorMin = new Vector2(0f, 0f);
                barBgRt.anchorMax = new Vector2(0f, 0f);
                barBgRt.pivot = new Vector2(0f, 0f);
                barBgRt.anchoredPosition = new Vector2(TextPad, 8f);
                barBgRt.sizeDelta = new Vector2(PanelWidth - TextPad * 2f, 3f);
                var barBgImg = barBgGo.AddComponent<Image>();
                barBgImg.color = BarBgColor;
                barBgImg.raycastTarget = false;

                var barFillGo = new GameObject("ConfBarFill", rectTypes);
                barFillGo.transform.SetParent(barBgGo.transform, false);
                _confFillRt = barFillGo.GetComponent<RectTransform>();
                _confFillRt.anchorMin = new Vector2(0f, 0f);
                _confFillRt.anchorMax = new Vector2(0f, 0f);
                _confFillRt.pivot = new Vector2(0f, 0f);
                _confFillRt.anchoredPosition = Vector2.zero;
                _confFillRt.sizeDelta = new Vector2(0f, 3f);
                _confFillImg = barFillGo.AddComponent<Image>();
                _confFillImg.color = AmberColor;
                _confFillImg.raycastTarget = false;

                _metaText = CreateText(panelGo, "Meta", TextPad, 13f, 16f, 11f, MetaColor);
                _metaText.text = "";

                // 5. 收起时显示的「AI」小徽章（点击恢复）
                var badgeGo = new GameObject("Badge", rectTypes);
                badgeGo.transform.SetParent(root.transform, false);
                _badgeGo = badgeGo;
                _badgeRt = badgeGo.GetComponent<RectTransform>();
                _badgeRt.anchorMin = new Vector2(0f, 0f);
                _badgeRt.anchorMax = new Vector2(0f, 0f);
                _badgeRt.pivot = new Vector2(0f, 0f);
                _badgeRt.anchoredPosition = new Vector2(14f, 14f);
                _badgeRt.sizeDelta = new Vector2(60f, 28f);
                CreatePanelImage(badgeGo, PanelColor);
                var badgeAccent = new GameObject("BadgeAccent", rectTypes);
                badgeAccent.transform.SetParent(badgeGo.transform, false);
                var baRt = badgeAccent.GetComponent<RectTransform>();
                baRt.anchorMin = new Vector2(0f, 1f);
                baRt.anchorMax = new Vector2(0f, 1f);
                baRt.pivot = new Vector2(0f, 1f);
                baRt.anchoredPosition = new Vector2(10f, 6f);
                baRt.sizeDelta = new Vector2(3f, 16f);
                var baImg = badgeAccent.AddComponent<Image>();
                baImg.color = AmberColor;
                baImg.raycastTarget = false;
                var badgeText = CreateText(badgeGo, "BadgeText", 0f, 0f, 28f, 13f, AmberColor);
                badgeText.text = "AI";
                badgeText.alignment = TextAlignmentOptions.Center;
                var btRt = badgeText.rectTransform;
                btRt.anchorMin = new Vector2(0f, 0f);
                btRt.anchorMax = new Vector2(1f, 1f);
                btRt.offsetMin = new Vector2(6f, 2f);
                btRt.offsetMax = new Vector2(-4f, -2f);
                badgeGo.SetActive(false); // 默认不显示（未收起）

                _root = root;
                _uiBuilt = true;
                ModLog.Info(
                    $"决策展示浮层 v2.0 已就绪（uGUI 圆角面板，右上角，Ctrl+F10 显隐，字体 {_font.name}）");
            }
            catch (Exception e)
            {
                ModLog.Warn($"决策浮层构建失败，稍后重试：{e}");
                _font = null; // 字体可能失效，下次重新解析
                _roundedSprite = null;
                _panelRt = null;
                _titleText = _actionText = _paramsText = _reasonText = null;
                _historyText = _metaText = _collapseText = null;
                _confFillRt = null;
                _confFillImg = null;
                _panelGo = null;
                _badgeGo = null;
                _badgeRt = null;
                if (_root != null)
                {
                    try { UnityEngine.Object.Destroy(_root); } catch (Exception) { }
                    _root = null;
                }
            }
        }

        /// <summary>创建 1px 水平分隔线（面板全宽减内边距）。</summary>
        private static void CreateDivider(GameObject panel,
                                          System.Type[] rectTypes,
                                          float topY)
        {
            var go = new GameObject("HeaderDivider", rectTypes);
            go.transform.SetParent(panel.transform, false);
            var rt = go.GetComponent<RectTransform>();
            rt.anchorMin = new Vector2(0f, 1f);
            rt.anchorMax = new Vector2(0f, 1f);
            rt.pivot = new Vector2(0f, 1f);
            rt.anchoredPosition = new Vector2(TextPad, -topY);
            rt.sizeDelta = new Vector2(PanelWidth - TextPad * 2f, 1f);
            var img = go.AddComponent<Image>();
            img.color = DividerColor;
            img.raycastTarget = false;
        }

        /// <summary>
        /// 创建一个 TextMeshProUGUI 子节点。x/y 为相对父节点锚点的偏移，
        /// anchor/pivot 默认父左上角（footer 类节点由调用方再调整）。
        /// </summary>
        private static TextMeshProUGUI CreateText(GameObject parent, string name,
                                                  float x, float y, float height,
                                                  float fontSize, Color color)
        {
            var rectTypes = new System.Type[1];
            rectTypes[0] = typeof(RectTransform);
            var go = new GameObject(name, rectTypes);
            go.transform.SetParent(parent.transform, false);

            var text = go.AddComponent<TextMeshProUGUI>();
            text.font = _font;
            text.fontSize = fontSize;
            text.color = color;
            text.alignment = TextAlignmentOptions.TopLeft;
            text.enableWordWrapping = false; // 关闭自动折行：行数由我们截断控制，高度可预算
            text.margin = Vector4.zero;
            text.raycastTarget = false;

            var rt = go.GetComponent<RectTransform>();
            rt.anchorMin = new Vector2(0f, 1f);
            rt.anchorMax = new Vector2(0f, 1f);
            rt.pivot = new Vector2(0f, 1f);
            rt.anchoredPosition = new Vector2(x, y);
            rt.sizeDelta = new Vector2(PanelWidth - TextPad * 2f, height);
            return text;
        }

        /// <summary>
        /// 解析中文字体：游戏 FontLoader 单例（简体中文专用字体）→ notoSans →
        /// 场景内任意 TMP_FontAsset。返回 null 表示尚未就绪。
        /// </summary>
        private static TMP_FontAsset ResolveFont()
        {
            try
            {
                var loader = FontLoader.Instance;
                if (loader != null)
                {
                    if (loader.simplifiedChineseFont != null)
                    {
                        return loader.simplifiedChineseFont;
                    }
                    if (loader.notoSans != null)
                    {
                        return loader.notoSans;
                    }
                }
            }
            catch (Exception e)
            {
                ModLog.Warn($"读取 FontLoader 字体失败：{e.Message}");
            }

            try
            {
                var all = Resources.FindObjectsOfTypeAll<TMP_FontAsset>();
                if (all != null && all.Length > 0)
                {
                    for (var i = 0; i < all.Length; i++)
                    {
                        var font = all[i].TryCast<TMP_FontAsset>();
                        if (font != null)
                        {
                            return font;
                        }
                    }
                }
            }
            catch (Exception e)
            {
                ModLog.Warn($"枚举 TMP 字体失败：{e.Message}");
            }

            return null;
        }

        // ------------------------------------------------------------------
        // 内容刷新
        // ------------------------------------------------------------------

        private static void ApplyVisible(bool visible)
        {
            try
            {
                if (_root != null)
                {
                    _root.SetActive(visible);
                }
            }
            catch (Exception e)
            {
                ModLog.Warn($"浮层显隐切换失败：{e.Message}");
            }
        }

        /// <summary>置信度对应颜色：≥0.8 绿 / ≥0.5 琥珀 / &lt;0.5 红。</summary>
        private static Color ConfidenceColor(double conf)
        {
            if (conf >= 0.8) return new Color(0.45f, 0.84f, 0.38f);
            if (conf >= 0.5) return AmberColor;
            return new Color(0.90f, 0.42f, 0.35f);
        }

        /// <summary>按快照刷新文本与面板高度（仅在 Version 变化时调用）。</summary>
        private static void Refresh(DecisionSnapshot snap)
        {
            try
            {
                var textWidth = PanelWidth - TextPad * 2f;

                if (!snap.HasDecision)
                {
                    _actionText.text = "等待 AI 决策 ...";
                    _actionText.color = MetaColor;
                    _paramsText.text = "";
                    _paramsText.gameObject.SetActive(false);
                    _reasonText.text = "";
                    _reasonText.gameObject.SetActive(false);
                    _historyText.text = "";
                    _historyText.gameObject.SetActive(false);
                    _metaText.text = "";
                    _confFillRt.sizeDelta = new Vector2(0f, 3f);
                    // 空闲态面板：标题栏 + 等待文案 + 页脚
                    _panelRt.sizeDelta = new Vector2(PanelWidth, HeaderH + ActionH + FooterH + 10f);
                    return;
                }

                // 1. 动作行（大号白字，按置信度着色）
                var actionVisual = (int)(textWidth / 16f);
                _actionText.text = "<b>" + Truncate("#" + snap.Step + "  " + snap.Action, actionVisual) + "</b>";
                var confColor = ConfidenceColor(snap.Confidence);
                _actionText.color = ActionColor;

                // 2. 参数行
                var y = HeaderH + 10f + ActionH;
                if (string.IsNullOrEmpty(snap.ParamsText))
                {
                    _paramsText.text = "";
                    _paramsText.gameObject.SetActive(false);
                }
                else
                {
                    _paramsText.gameObject.SetActive(true);
                    _paramsText.rectTransform.anchoredPosition = new Vector2(TextPad, -y);
                    _paramsText.text = Truncate(snap.ParamsText, (int)(textWidth / 12f));
                    y += ParamH;
                }

                // 3. 理由（最多 2 行）
                var reasonLines = 0;
                if (!string.IsNullOrEmpty(snap.Reason))
                {
                    var wrapped = TruncateLines("理由：" + snap.Reason, (int)(textWidth / 12f), 2);
                    reasonLines = CountLines(wrapped);
                    _reasonText.gameObject.SetActive(true);
                    _reasonText.rectTransform.anchoredPosition = new Vector2(TextPad, -y);
                    _reasonText.text = wrapped;
                    y += reasonLines * ReasonLineH;
                }
                else
                {
                    _reasonText.text = "";
                    _reasonText.gameObject.SetActive(false);
                }

                // 4. 最近决策（最多 6 条，含小节标题行）
                y += SectionGap;
                var history = snap.HistoryLines;
                var shown = 0;
                if (history != null && history.Count > 0)
                {
                    var histSb = new StringBuilder();
                    histSb.Append("── 最近决策 ──");
                    foreach (var line in history)
                    {
                        if (shown >= MaxHistoryShown) break;
                        histSb.Append('\n');
                        histSb.Append(Truncate(line, (int)(textWidth / 11f)));
                        shown++;
                    }
                    _historyText.gameObject.SetActive(true);
                    _historyText.rectTransform.anchoredPosition = new Vector2(TextPad, -y);
                    _historyText.text = histSb.ToString();
                    y += (shown + 1) * HistLineH;
                }
                else
                {
                    _historyText.text = "";
                    _historyText.gameObject.SetActive(false);
                }

                // 5. 页脚：元信息 + 置信度进度条
                _metaText.text = "置信度 " + snap.Confidence.ToString("P0", CultureInfo.InvariantCulture)
                                 + "  ·  " + (string.IsNullOrEmpty(snap.Ts) ? "" : snap.Ts);
                var fillW = Mathf.Clamp01((float)snap.Confidence) * textWidth;
                _confFillRt.sizeDelta = new Vector2(fillW, 3f);
                _confFillImg.color = confColor;

                // 6. 面板总高（页脚锚定底部，这里只保证内容不被遮住）
                var bodyH = (y + SectionGap) - (HeaderH + 10f);
                var panelH = Mathf.Max(HeaderH + bodyH + FooterH + PadBottom,
                                       HeaderH + ActionH + FooterH + 22f);
                _panelRt.sizeDelta = new Vector2(PanelWidth, panelH);
            }
            catch (Exception e)
            {
                ModLog.Warn($"浮层内容刷新失败：{e.Message}");
            }
        }

        /// <summary>统计折行后的行数。</summary>
        private static int CountLines(string s)
        {
            if (string.IsNullOrEmpty(s)) return 0;
            var n = 1;
            for (var i = 0; i < s.Length; i++)
            {
                if (s[i] == '\n') n++;
            }
            return n;
        }

        // ------------------------------------------------------------------
        // 文本截断工具（TMP 关折行后由我们保证行数与行宽）
        // ------------------------------------------------------------------

        /// <summary>按"视觉宽度"截断（全角算 1、拉丁算 0.55），超出补省略号。</summary>
        private static string Truncate(string s, int maxVisual)
        {
            if (string.IsNullOrEmpty(s)) return "";
            var sb = new StringBuilder(s.Length + 3);
            var w = 0f;
            for (var i = 0; i < s.Length; i++)
            {
                var cw = s[i] > 0x2E80 ? 1f : 0.55f;
                if (w + cw > maxVisual)
                {
                    sb.Append("...");
                    return sb.ToString();
                }
                sb.Append(s[i]);
                w += cw;
            }
            return sb.ToString();
        }

        /// <summary>按视觉宽度折行（最多 maxLines 行，超出补省略号）。</summary>
        private static string TruncateLines(string s, int maxVisual, int maxLines)
        {
            if (string.IsNullOrEmpty(s)) return "";
            var sb = new StringBuilder(s.Length + 3);
            var w = 0f;
            var lines = 1;
            for (var i = 0; i < s.Length; i++)
            {
                var cw = s[i] > 0x2E80 ? 1f : 0.55f;
                if (w + cw > maxVisual)
                {
                    if (lines >= maxLines)
                    {
                        sb.Append("...");
                        return sb.ToString();
                    }
                    sb.Append('\n');
                    lines++;
                    w = 0;
                }
                sb.Append(s[i]);
                w += cw;
            }
            return sb.ToString();
        }
    }
}
