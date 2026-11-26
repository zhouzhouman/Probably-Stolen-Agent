using System;
using UnityEngine;
using UnityEngine.EventSystems;

namespace DeepSpaceBridge
{
    /// <summary>
    /// 物品 / 控件的屏幕坐标校准（移植自创意工坊 mod「游戏 MCP」v1.2.7 的
    /// Observation.Bounds / HitPoint / ItemAnchor / ItemPoint，设计已核实）。
    ///
    /// 为什么需要它：物品是 PixelElement（UI 层 rectTransform），不是世界层 Interactable，
    /// 直接用 transform.position 投影会偏。真值获取方式：
    ///   1. Bounds(rectTransform) → 四角 WorldToScreenPoint → 裁剪到屏幕内的矩形；
    ///   2. ItemAnchor(item) → rectTransform 矩形中心；
    ///   3. ItemPoint(item) → 中心若射线没命中该物品，就在 5×5 网格里找一个能命中的点（防遮挡）。
    /// 这样 player_drag 拿到的才是"玩家真正能点到的物品中心"。
    ///
    /// 合规：只用字面游戏类型（GameItem / PixelElement / GameItemElement / RenderHandler /
    /// TreeNodeRender）+ 公开成员，补丁游戏自身代码免费，不引入能力。
    /// </summary>
    internal static class ItemGeometry
    {
        private static readonly Vector3[] Corners = new Vector3[4];

        /// <summary>RectTransform 四角投影到屏幕，取交集矩形；不可见/越界返回 null。</summary>
        internal static Rect? Bounds(RectTransform rect)
        {
            if (rect == null || !rect.gameObject.activeInHierarchy || Screen.width < 1 || Screen.height < 1)
                return null;
            var canvas = rect.GetComponentInParent<Canvas>();
            if (canvas == null || !canvas.isActiveAndEnabled) return null;
            var root = canvas.rootCanvas;
            var cam = root.renderMode == RenderMode.ScreenSpaceOverlay
                ? null
                : (root.worldCamera ?? Camera.main);

            rect.GetWorldCorners(Corners);
            var min = new Vector2(Screen.width, Screen.height);
            var max = Vector2.zero;
            foreach (var corner in Corners)
            {
                var sp = RectTransformUtility.WorldToScreenPoint(cam, corner);
                min = Vector2.Min(min, sp);
                max = Vector2.Max(max, sp);
            }
            min = Vector2.Max(min, Vector2.zero);
            max = Vector2.Min(max, new Vector2(Screen.width, Screen.height));
            if (max.x - min.x < 1f || max.y - min.y < 1f) return null;
            return Rect.MinMaxRect(min.x, min.y, max.x, max.y);
        }

        /// <summary>3×3 采样点做射线检测，找第一个真正命中目标（或其子节点）的点。</summary>
        private static Vector2? HitPoint(GameObject target, Rect rect)
        {
            var es = EventSystem.current;
            if (es == null) return null;
            var ped = new PointerEventData(es);
            var results = new System.Collections.Generic.List<RaycastResult>();
            float[] samples = { 0.5f, 0.25f, 0.75f };
            foreach (float fy in samples)
            {
                foreach (float fx in samples)
                {
                    var point = new Vector2(
                        Mathf.Lerp(rect.xMin, rect.xMax, fx),
                        Mathf.Lerp(rect.yMin, rect.yMax, fy));
                    ped.position = point;
                    results.Clear();
                    try { es.RaycastAll(ped, results); } catch { continue; }
                    if (results.Count == 0) continue;
                    var go = results[0].gameObject;
                    if (go == target || go.transform.IsChildOf(target.transform)) return point;
                }
            }
            return null;
        }

        /// <summary>物品交互锚点：rectTransform 矩形中心（GameItemElement 有 background 兜底）。</summary>
        internal static Vector2? ItemAnchor(GameItem item)
        {
            var pixel = item as PixelElement;
            if (pixel == null || pixel.handler == null || !pixel.handler.gameObject.activeInHierarchy)
                return null;

            var bounds = Bounds(pixel.rectTransform);
            if (!bounds.HasValue)
            {
                var element = pixel as GameItemElement;
                var background = element != null ? element.background : null;
                bounds = Bounds(background != null ? background.rectTransform : null);
            }
            return bounds.HasValue ? bounds.Value.center : (Vector2?)null;
        }

        /// <summary>
        /// 物品可点击的屏幕点：优先锚点（射线确认命中），否则在 5×5 网格里找可见点。
        /// 返回 null 表示物品被遮挡（需要打开容器或关闭重叠面板）。
        /// </summary>
        internal static Vector2? ItemPoint(GameItem item)
        {
            var anchor = ItemAnchor(item);
            if (!anchor.HasValue) return null;
            try
            {
                if (RenderHandler.RaycastElement<GameItem>(anchor.Value, null) == item)
                    return anchor.Value;
            }
            catch { }

            var element = item as GameItemElement;
            var background = element != null ? element.background : null;
            var bounds = Bounds(background != null ? background.rectTransform : null);
            if (!bounds.HasValue) return null;

            float[] samples = { 0.5f, 0.25f, 0.75f, 0.125f, 0.875f };
            foreach (float fy in samples)
            {
                foreach (float fx in samples)
                {
                    var point = new Vector2(
                        Mathf.Lerp(bounds.Value.xMin, bounds.Value.xMax, fx),
                        Mathf.Lerp(bounds.Value.yMin, bounds.Value.yMax, fy));
                    try
                    {
                        if (RenderHandler.RaycastElement<GameItem>(point, null) == item)
                            return point;
                    }
                    catch { }
                }
            }
            return null;
        }

        /// <summary>控件的可点击屏幕点（按钮等有 RectTransform 的对象），带射线确认。</summary>
        internal static Vector2? ControlPoint(Component component)
        {
            if (component == null) return null;
            var rt = component.transform as RectTransform;
            var bounds = Bounds(rt);
            if (!bounds.HasValue) return null;
            return HitPoint(component.gameObject, bounds.Value) ?? bounds.Value.center;
        }

        /// <summary>
        /// 库存网格的（列, 行）→ 屏幕点（移植自 mod 的 Observation.GridPoint）。
        ///
        /// 网格坐标系：列向右、行向下，格子边长 = UISettings.current.gridSize。
        /// 取格子中心 (col+0.5, row+0.5)，再按「行向下为负 Y」的 UI 约定取负。
        /// 这是 player_drag 能把物品精确拖到目标格的最后一块拼图。
        /// </summary>
        internal static Vector2? GridPoint(GameGridInventory grid, int column, int row)
        {
            if (grid == null) return null;
            RectTransform rt;
            try { rt = grid.rectTransform; } catch { return null; }
            if (rt == null || !rt.gameObject.activeInHierarchy) return null;

            var canvas = rt.GetComponentInParent<Canvas>();
            if (canvas == null) return null;
            var root = canvas.rootCanvas;
            var cam = root.renderMode == RenderMode.ScreenSpaceOverlay
                ? null
                : (root.worldCamera ?? Camera.main);

            float cell = 16f;
            try
            {
                var settings = UISettings.current;
                if (settings != null && settings.gridSize > 0) cell = settings.gridSize;
            }
            catch { }

            var local = new Vector3((column + 0.5f) * cell, -(row + 0.5f) * cell, 0f);
            var world = rt.TransformPoint(local);
            var sp = RectTransformUtility.WorldToScreenPoint(cam, world);
            if (sp.x < 0 || sp.y < 0 || sp.x > Screen.width || sp.y > Screen.height) return null;
            return sp;
        }

        /// <summary>按区域名取该库存的网格（front/back/counter/ground/...）。</summary>
        internal static GameGridInventory GridByArea(string area)
        {
            var e = EmporiumEntry.Instance;
            if (e == null) return null;
            GameInventory inv = null;
            switch (area)
            {
                case "front": inv = e.frontInvinvElement; break;
                case "back": inv = e.backInvinvElement; break;
                case "counter": inv = e.backInvinvElementCounter; break;
                case "ground": inv = e.afterhourInventory; break;
                case "pocketLeft": inv = e.afterhourPocketSlotInvLeft; break;
                case "pocketRight": inv = e.afterhourPocketSlotInvRight; break;
                case "backpackSlot": inv = e.afterhourPocketSlotInvBackpack; break;
            }
            return inv as GameGridInventory;
        }
    }
}
