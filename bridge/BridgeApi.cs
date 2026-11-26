using System;
using System.Text;
using System.Collections.Generic;
using System.Threading;
using Il2CppInterop.Runtime;
using Il2CppInterop.Runtime.InteropTypes.Arrays;
using UnityEngine;
using UnityEngine.SceneManagement;
using UnityEngine.UI;
using UnityEngine.EventSystems;

namespace DeepSpaceBridge
{
    /// <summary>
    /// 桥接 API：状态读取（GET /state）与动作执行（POST /action）。
    ///
    /// 所有游戏对象访问都通过 RunOnMain() 派发到 Unity 主线程执行；
    /// HTTP 线程只负责解析请求与收发布数据。
    ///
    /// 动作清单（POST /action，请求体 {"action":"...","index":0}）：
    ///   open_shutter   开门（StoreShutterButton.OpenShutter，回退 PlayerStore.OpenShutter）
    ///   close_shutter  关门（StoreShutterButton.CloseShutter）
    ///   toggle_shutter 切换门状态
    ///   call_next      呼叫下一位顾客（PlayerStore.TryCallNextClient）
    ///   dismiss_client 送走当前顾客（DismissCurrentClient，自动叫下一位）
    ///   dialog_advance 推进对话（DialogUIManager.ContinueConversation）
    ///   dialog_choose  选择对话选项（DialogUIManager.SelectChoice(index)）
    ///   accept_offer   接受报价（NegociationUIManager.OnOfferAcceptClick）
    ///   open_negociation 打开谈判界面（NegociationUIManager.OpenUI，Wiki：物品上桌后需开谈判界面完成交易）
    ///   close_negociation 关闭谈判界面（v0.8.2：NegociationUIManager.CloseUI，镜像 open_negociation；
    ///                  多 Agent 循环硬规则「未关闭界面必须优先关闭」的执行手段；
    ///                  面板开闭态由 /state 的 table.nego_open 观测）
    ///   place_item     把物品放上交易桌并自动打开谈判界面（放置策略逐级降级：
    ///                  AddDirectSellingItemToTable → HandleAutoNegoUIDisplay → PlacedItemForSelling/Buying/Barter，
    ///                  每步以 GetCurrentlyNegociatedItem() 验证；请求体 {"action":"place_item","identifier":"toothpaste"}）
    ///   buy_table      收购桌上物品（PlayerStore.BuyItemFromTable，带 CanBuyItemFromTable 守卫）
    ///   sell_table     出售桌上物品（PlayerStore.SellItemFromTable）
    ///   end_day        结束营业日（PlayerStore.EndDay）
    ///   begin_day      开始营业日（PlayerStore.BeginDay）
    ///   save_game      手动存档（PlayerStore.SaveGame）
    ///
    /// 夜晚阶段动作（v0.8.0，打烊后的完整夜晚流程：选服务 → 定去向 → 睡觉/外出）：
    ///   night_services   打开夜晚服务面板（EndOfDayUIManager.OpenUI：电力/清洁/保险/盘查预警/推荐/善后…）
    ///   night_service    选择/取消一个夜晚服务（{"service":"POWER_SERVICE","enable":true}；
    ///                    写 StoreService.isUsing，与界面 Toggle 等效，确认后扣费）
    ///   end_day_confirm  确认夜晚服务选择（EndOfDayUIManager.OnEndDayButtonClick，进入外出去向选择）
    ///   open_map         打开夜间地图（MapUIManager.OpenUI：今晚剩余外出次数/可选目的地）
    ///   go_out           外出（{"destination":"commissary|upgrade|scavenging"}；
    ///                    设 MapUIManager.currentLocation → 外出确认；带重伤/次数守卫）
    ///   stay_home        不出门，睡觉度过夜晚（MapUIManager.Sleep）
    ///   scavenge         在拾荒地点开始一次拾荒（MapUIManager.ScavengeButtonClick）
    ///   leave_outside    从外出地点返回商店（按 currentLocation 调 HandleLeave*）
    ///
    /// 电话动作（v0.8.0，PhoneUIManager：拨号/通讯录/挂断）：
    ///   open_phone    打开电话面板（PhoneUIManager.OpenUI）
    ///   phone_call    拨号（{"number":8376} 或 {"contact":"王尔德"}；AutoCall 直拨，
    ///                 接通后对话走 dialog_advance/dialog_choose）
    ///   phone_book    打开/关闭通讯录（{"show":true}；PhoneBookUIManager）
    ///   hang_up       挂断当前通话（PhoneUIManager.StopCall）
    ///
    /// 治安与战斗动作（v0.8.0）：
    ///   report_client       举报当前顾客（StoreSecButton.OnClick——长按举报按钮，
    ///                        IsClientArrestable 守卫：仅通缉犯/伪人等可逮捕顾客可举报，错报有声望惩罚）
    ///   enter_shooting_mode 进入射击模式（StoreUIManager.EnterShootingMode）
    ///   shoot               开枪（StoreUIManager.Shoot，带 CanShoot 守卫）
    ///   exit_shooting_mode  退出射击模式（StoreUIManager.ExitShootingMode）
    ///
    /// 决策浮层（POST /decision，请求体 {"action":"click","reason":"...","confidence":0.9,
    ///   "params_text":"x=100 y=200","step":3,"ts":"..."}）：
    ///   把 AI 决策推送到游戏内右上角浮层展示；GET /decision 读回当前浮层状态（调试用）。
    ///   决策存储线程安全，无需派发主线程。
    /// </summary>
    public static class BridgeApi
    {
        public const string Version = BridgePlugin.Version;  // 单一真相：跟随 Plugin.Version，避免 /ping 报旧版

        // ---------------------------------------------------------------- 路由

        public static string Route(string method, string path, string body)
        {
            if (method == "GET")
            {
                switch (path)
                {
                    case "/ping":
                        return "{\"ok\":true,\"version\":\"" + Version + "\"}";
                    case "/state":
                        return RunOnMain(() => "{\"ok\":true,\"state\":" + BuildState() + "}");
                    case "/debug/client":
                        return RunOnMain(() => "{\"ok\":true,\"debug\":" + BuildDebugClient() + "}");
                    case "/ui/buttons":
                        return RunOnMain(() => BuildUiButtons(false));
                    case "/ui/buttons/all":
                        return RunOnMain(() => BuildUiButtons(true));
                    case "/debug/slots":
                        return RunOnMain(() => BuildDebugSlots());
                    case "/debug/machines":
                        return RunOnMain(() => BuildDebugMachines());
                    case "/machines":
                        return RunOnMain(() => MachinesJson());
                    case "/expedition":
                        return RunOnMain(() => ExpeditionJson());
                    case "/ui/nodes":
                        return RunOnMain(() => UiNodesJson());
                    case "/sellable":
                        return RunOnMain(() => SellableJson());
                    case "/ui/objects":
                        return RunOnMain(() => BuildAllClickables());
                    case "/decision":
                        return "{\"ok\":true,\"decision\":" + BuildDecision() + "}";
                }
            }
            else if (method == "POST" && path == "/action")
            {
                return RunOnMain(() => HandleAction(body));
            }
            else if (method == "POST" && path == "/decision")
            {
                return HandleDecision(body);
            }
            return Err($"未知路由或方法：{method} {path}");
        }

        // ---------------------------------------------------------------- 主线程调度

        private static string RunOnMain(Func<string> fn, int timeoutMs = 15000)
        {
            string result = null;
            string error = null;
            using (var done = new ManualResetEventSlim(false))
            {
                MainThreadDispatcher.Enqueue(() =>
                {
                    try { result = fn(); }
                    catch (Exception e) { error = e.ToString(); }
                    finally { done.Set(); }
                });
                if (!done.Wait(timeoutMs))
                {
                    return Err("主线程执行超时");
                }
            }
            return error != null ? Err(error) : result;
        }

        // ---------------------------------------------------------------- 状态读取

        private static string BuildState()
        {
            var sb = new StringBuilder(1024);
            sb.Append('{');

            PlayerStore ps;
            try { ps = PlayerStore.Instance; }
            catch { ps = null; }

            if (ps == null)
            {
                sb.Append("\"in_game\":false}");
                return sb.ToString();
            }

            sb.Append("\"in_game\":true");
            sb.Append(",\"scene\":").Append(JsonString(CurrentScene()));
            sb.Append(",\"cash\":").Append(ps.playerCash);
            sb.Append(",\"store_state\":").Append(JsonString(ps.storeState.ToString()));
            // v2.1：暴露经营天数。此前 /state 没有 day 字段，AI 观察里的 time_or_day
            // 恒为 null——「十天内赚 X」这类目标无法自行核对进度（只能靠 day_until_rent 猜）。
            // GetDayCounter() 为 StoreStation 的静态方法（反汇编 dump.cs 实证）。
            try { sb.Append(",\"day\":").Append(StoreStation.GetDayCounter()); }
            catch { sb.Append(",\"day\":null"); }
            sb.Append(",\"day_until_rent\":").Append(ps.dayUntilRent);
            sb.Append(",\"rent_value\":").Append(ps.rentValue);
            // v1.5：真实应缴额（temporaryRent 会累计延期欠款/罚金；rent_value 只是本期名义租金）。
            // r78 实证：显示 rent_value=242、现金 442，但房东对话只给「拒付租金」——
            // CanPayRent()=false 的真实原因就是 temporaryRent 大于现金。
            try { sb.Append(",\"rent_due\":").Append(ps.temporaryRent); } catch { }
            try { sb.Append(",\"can_pay_rent\":").Append(B(ps.CanPayRent())); } catch { }
            sb.Append(",\"is_power_on\":").Append(B(ps.isPowerOn));
            sb.Append(",\"garbage_level\":").Append(ps.garbageLevel);

            // 屏幕诊断（v0.6.1：校准「截图像素 → Unity 屏幕」坐标映射，
            // 用于浮层按钮点击命中；mouse 为 Unity 屏幕坐标系下的实时鼠标位置）
            try
            {
                sb.Append(",\"screen\":{\"w\":").Append(Screen.width)
                  .Append(",\"h\":").Append(Screen.height)
                  .Append(",\"mouse_x\":").Append((int)Input.mousePosition.x)
                  .Append(",\"mouse_y\":").Append((int)Input.mousePosition.y)
                  .Append('}');
            }
            catch (Exception e) { sb.Append(",\"screen_error\":").Append(JsonString(e.Message)); }

            try
            {
                var shutter = StoreShutterButton.Instance;
                if (shutter != null)
                {
                    sb.Append(",\"shutter_closed\":").Append(B(shutter.isClosed));
                }
            }
            catch (Exception e) { sb.Append(",\"shutter_error\":").Append(JsonString(e.Message)); }

            try
            {
                var instance = ps.currentClientInstance;
                sb.Append(",\"client\":").Append(instance != null ? BuildClient(instance) : "null");
            }
            catch (Exception e) { sb.Append(",\"client_error\":").Append(JsonString(e.Message)); }

            try
            {
                var dlg = DialogUIManager.Instance;
                if (dlg != null)
                {
                    sb.Append(",\"dialog\":{\"is_end\":").Append(B(dlg.IsCurrentDialogEnd()));
                    sb.Append(",\"is_choice\":").Append(B(dlg.IsChoice()));
                    // v1.4：暴露对话文本与选项文本——AI 不再盲选序号
                    // （r75 房租对话盲选 index 0 走进「延期」分支，房东 business_complete
                    //  永远 false、不可遣散、挡住打烊，整局死锁）
                    string dtext = "";
                    try
                    {
                        if (dlg.currentDialog != null && dlg.currentDialog.plainText != null)
                            dtext = dlg.currentDialog.plainText;
                    }
                    catch { }
                    sb.Append(",\"text\":").Append(JsonString(dtext));
                    sb.Append(",\"choices\":[");
                    bool firstChoice = true;
                    try
                    {
                        var bubbles = dlg.dialogChoiceBubbles;
                        if (bubbles != null)
                        {
                            for (int bi = 0; bi < bubbles.Count; bi++)
                            {
                                var bubble = bubbles[bi];
                                if (bubble == null) continue;
                                bool active = false;
                                try { active = bubble.gameObject != null && bubble.gameObject.activeSelf; } catch { }
                                if (!active) continue;
                                string ctext = "";
                                try { if (bubble.TMP != null) ctext = bubble.TMP.text ?? ""; } catch { }
                                if (!firstChoice) sb.Append(',');
                                firstChoice = false;
                                sb.Append("{\"index\":").Append(bubble.index)
                                  .Append(",\"text\":").Append(JsonString(ctext)).Append('}');
                            }
                        }
                    }
                    catch { }
                    sb.Append("]}");
                }
            }
            catch (Exception e) { sb.Append(",\"dialog_error\":").Append(JsonString(e.Message)); }

            // 交易桌状态（P4：AI 据此判断桌上有什么、能否成交、该放什么）
            try
            {
                sb.Append(",\"table\":").Append(BuildTable(ps));
            }
            catch (Exception e) { sb.Append(",\"table_error\":").Append(JsonString(e.Message)); }

            // 库存摘要（P4：place_item 需要 identifier，AI 从这里选物品；
            // 顾客想买→从 owned 选自己的物品；顾客想卖→从 not_owned 选顾客的物品）
            try
            {
                var ownedList = ps.FindAllItem(true);
                var notOwnedList = ps.FindAllItem(false);
                sb.Append(",\"inventory\":{\"owned\":").Append(ItemListJson(ownedList));
                // v2.9：游戏查询会把同一件物品同时返回给 owned 与 not_owned
                //（实测 cassette2d 收购后两边都有，AI 无法判断到底是不是自己的）。
                // owned 优先——它才是能拿去卖的那份。
                sb.Append(",\"not_owned\":").Append(ItemListJson(_ExcludeOwned(notOwnedList, ownedList)));
                sb.Append('}');
            }
            catch (Exception e) { sb.Append(",\"inventory_error\":").Append(JsonString(e.Message)); }

            // 经济与经营全局感知（v0.7.0：声望/吸引力/贷款/加价/彩票/库存估值）
            try
            {
                sb.Append(",\"economy\":{");
                try { sb.Append("\"attractiveness\":").Append(ps.GetCurrentStoreAttractiveness()); } catch { sb.Append("\"attractiveness\":null"); }
                sb.Append(",\"attractiveness_base\":").Append(ps.baseStoreAttractiveness);
                sb.Append(",\"attractiveness_projector\":").Append(ps.projectorAttractivenessBonus);
                sb.Append(",\"deal_maker_bonus\":").Append(ps.dealMakerBargainBonus);
                sb.Append(",\"loan_days\":").Append(ps.dayUntilLoan);
                sb.Append(",\"loan_value\":").Append(ps.loanValue);
                sb.Append(",\"retail_markup\":").Append(ps.retailMarkup);
                sb.Append(",\"wild_favor\":").Append(ps.wildFavor);
                sb.Append(",\"insurance_days_without\":").Append(ps.dayWithoutInsurance);
                try
                {
                    sb.Append(",\"contraband_markup\":{\"low\":").Append(ps.contrabandMarkupLow);
                    sb.Append(",\"mid\":").Append(ps.contrabandMarkupMid);
                    sb.Append(",\"high\":").Append(ps.contrabandMarkupHigh);
                    sb.Append(",\"critical\":").Append(ps.contrabandMarkupCritial);
                    sb.Append('}');
                }
                catch { }
                try { sb.Append(",\"lottery_number\":").Append(JsonString(ps.currentWinningLotteryNumber)); } catch { }
                try { sb.Append(",\"stock_value\":").Append(ps.GetTotalEstimatedValue()); } catch { }
                sb.Append('}');
            }
            catch (Exception e) { sb.Append(",\"economy_error\":").Append(JsonString(e.Message)); }

            // 各派系声望（v0.7.0：storeReputations 列表直读——AI 据此权衡上报/放行）
            try
            {
                sb.Append(",\"reputations\":[");
                var reps = ps.storeReputations;
                if (reps != null)
                {
                    bool first = true;
                    for (int i = 0; i < reps.Count; i++)
                    {
                        var rep = reps[i];
                        if (rep == null) continue;
                        if (!first) sb.Append(',');
                        first = false;
                        sb.Append("{\"faction\":").Append(JsonString(rep.factionDisplay));
                        sb.Append(",\"id\":").Append(JsonString(rep.factionId));
                        try { sb.Append(",\"value\":").Append(rep.GetReputation()); } catch { }
                        sb.Append('}');
                    }
                }
                sb.Append(']');
            }
            catch (Exception e) { sb.Append(",\"reputations_error\":").Append(JsonString(e.Message)); }

            // 派系委托列表（fill_request 的目标；wiki：顾客委托系统）
            try
            {
                sb.Append(",\"requests\":[");
                var reqs = ps.storeRequests;
                if (reqs != null)
                {
                    bool first = true;
                    for (int i = 0; i < reqs.Count; i++)
                    {
                        var r = reqs[i];
                        if (r == null) continue;
                        if (!first) sb.Append(',');
                        first = false;
                        sb.Append("{\"id\":").Append(JsonString(r.id));
                        sb.Append(",\"title\":").Append(JsonString(r.title));
                        sb.Append(",\"requester\":").Append(JsonString(r.requesterName));
                        sb.Append(",\"faction\":").Append(JsonString(r.factionId));
                        sb.Append(",\"requested_item\":").Append(JsonString(r.requestedItem));
                        sb.Append(",\"required_item\":").Append(JsonString(r.requiredItem));
                        sb.Append(",\"required_count\":").Append(r.requiredItemCount);
                        sb.Append(",\"duration\":").Append(r.duration);
                        try { sb.Append(",\"credit_reward\":").Append(r.creditReward); } catch { }
                        try { sb.Append(",\"reputation_reward\":").Append(r.reputationReward); } catch { }
                        bool canFill = false;
                        try { canFill = r.CanFillRequest(); } catch { }
                        sb.Append(",\"can_fill\":").Append(B(canFill));
                        sb.Append('}');
                    }
                }
                sb.Append(']');
            }
            catch (Exception e) { sb.Append(",\"requests_error\":").Append(JsonString(e.Message)); }

            // 设施解锁状态（自动售货机/饮水台/射击台/基因扫描仪）
            try
            {
                sb.Append(",\"unlocks\":{\"vending_machine\":").Append(B(ps.isVendingMachineUnlocked));
                sb.Append(",\"vending_fountain\":").Append(B(ps.isVendingFountainUnlocked));
                sb.Append(",\"shooting\":").Append(B(ps.isShootingUnlocked));
                sb.Append(",\"gene_scanner\":").Append(B(ps.isGeneScannerScanning));
                sb.Append('}');
            }
            catch { }

            // 夜晚服务列表（v0.8.0：打烊后选购的服务——电力/清洁/保险/盘查预警/
            // 买家推荐/卖家推荐/违禁品买家推荐/王尔德的善后/卡特尔保护费；
            // is_using=已选中（确认后扣费），mandatory=强制（保护费），unlocked=已解锁）
            try
            {
                sb.Append(",\"services\":").Append(BuildServices(ps));
            }
            catch (Exception e) { sb.Append(",\"services_error\":").Append(JsonString(e.Message)); }

            // 夜晚/外出状态（v0.8.0：夜间地图——今晚剩余外出次数、玩家位置、受伤状态）
            try
            {
                sb.Append(",\"night\":{");
                sb.Append("\"visit_left\":").Append(ps.visitLeftTonight);
                sb.Append(",\"scavenging_attempts\":").Append(ps.scavengingAttempts);
                sb.Append(",\"player_location\":").Append(JsonString(ps.playerLocation.ToString()));
                try
                {
                    var h = ps.healthData;
                    if (h != null)
                    {
                        sb.Append(",\"wound_state\":").Append(h.woundState);
                        sb.Append(",\"wound_stable\":").Append(B(h.isWoundStable));
                        sb.Append(",\"wounded_fresh\":").Append(B(h.isWoundedFresh));
                    }
                }
                catch { }
                bool seriously = false;
                try { seriously = HealthData.IsSeriouslyWounded(); } catch { }
                sb.Append(",\"seriously_wounded\":").Append(B(seriously));
                try
                {
                    var map = MapUIManager.Instance;
                    if (map != null)
                    {
                        sb.Append(",\"is_outside\":").Append(B(map.isOutside));
                        sb.Append(",\"current_location\":").Append(JsonString(map.currentLocation.ToString()));
                        sb.Append(",\"map_open\":").Append(B(map.mapPanel != null && map.mapPanel.activeSelf));
                    }
                }
                catch { }
                sb.Append('}');
            }
            catch (Exception e) { sb.Append(",\"night_error\":").Append(JsonString(e.Message)); }

            // 电话状态（v0.8.0：面板开关/当前通话/通讯录）
            try
            {
                var phone = PhoneUIManager.Instance;
                if (phone != null)
                {
                    sb.Append(",\"phone\":{");
                    try
                    {
                        sb.Append("\"open\":").Append(B(phone.phonePanel != null && phone.phonePanel.activeSelf));
                    }
                    catch { }
                    try
                    {
                        sb.Append(",\"book_shown\":").Append(B(phone.IsPhoneBookShown()));
                    }
                    catch { }
                    try
                    {
                        var cur = phone.currentPhoneClient;
                        sb.Append(",\"current_client\":").Append(
                            cur != null ? JsonString(cur.GetLocalizedDisplayName()) : "null");
                    }
                    catch { }
                    sb.Append(",\"contacts\":").Append(BuildPhoneContacts(ps));
                    sb.Append('}');
                }
            }
            catch (Exception e) { sb.Append(",\"phone_error\":").Append(JsonString(e.Message)); }

            // 顾客队列状态（v0.8.1：今日还有没有顾客——can_next=false 且无在场顾客时
            // 应立即打烊进入夜晚流程，反复 call_next 是空转浪费）
            try
            {
                sb.Append(",\"queue\":{");
                bool canNext = false;
                bool hasClient = false;
                int queued = -1;
                try { canNext = ps.CanNextClient(); } catch { }
                try { hasClient = ps.IsClientPresent(); } catch { }
                try
                {
                    var mgr = ps.storeClientManager;
                    if (mgr != null && mgr.clientStack != null) queued = mgr.clientStack.Count;
                }
                catch { }
                sb.Append("\"can_next\":").Append(B(canNext));
                sb.Append(",\"has_client\":").Append(B(hasClient));
                sb.Append(",\"count\":").Append(queued);
                sb.Append('}');
            }
            catch (Exception e) { sb.Append(",\"queue_error\":").Append(JsonString(e.Message)); }

            // 射击模式状态（v0.8.0：伪人/战斗——mode=射击模式中，can_shoot=可开枪）
            try
            {
                var ui = StoreUIManager.Instance;
                if (ui != null)
                {
                    sb.Append(",\"shooting\":{\"mode\":").Append(B(ui.isShootingMode));
                    bool canShoot = false;
                    try { canShoot = ui.CanShoot(); } catch { }
                    sb.Append(",\"can_shoot\":").Append(B(canShoot));
                    sb.Append('}');
                }
            }
            catch (Exception e) { sb.Append(",\"shooting_error\":").Append(JsonString(e.Message)); }

            sb.Append('}');
            return sb.ToString();
        }

        /// <summary>
        /// 夜晚服务列表 JSON（StoreService：id/displayName/cost/unlocked/isUsing/
        /// mandatory/cooldown/tooltip）。isUsing 是「今晚已选中」——确认（end_day_confirm）后才扣费。
        /// </summary>
        private static string BuildServices(PlayerStore ps)
        {
            var sb = new StringBuilder(512);
            sb.Append('[');
            try
            {
                var list = ps.storeService;
                if (list != null)
                {
                    bool first = true;
                    for (int i = 0; i < list.Count; i++)
                    {
                        var s = list[i];
                        if (s == null) continue;
                        if (!first) sb.Append(',');
                        first = false;
                        sb.Append("{\"id\":").Append(JsonString(s.id));
                        sb.Append(",\"name\":").Append(JsonString(s.displayName));
                        sb.Append(",\"cost\":").Append(s.cost);
                        sb.Append(",\"unlocked\":").Append(B(s.unlocked));
                        sb.Append(",\"is_using\":").Append(B(s.isUsing));
                        sb.Append(",\"mandatory\":").Append(B(s.isMandatory));
                        sb.Append(",\"introduced\":").Append(B(s.introduced));
                        sb.Append(",\"cooldown\":").Append(s.currentCooldown);
                        sb.Append('}');
                    }
                }
            }
            catch (Exception e)
            {
                return "[{\"error\":" + JsonString(e.Message) + "}]";
            }
            sb.Append(']');
            return sb.ToString();
        }

        /// <summary>
        /// 电话通讯录 JSON（PhoneClientDict：号码 → 联系人）。
        /// 已知号码（2026-10-14 存档实测）：
        ///   8376 Joe Wild's Assistant（王尔德助手）· 8815 Red Imp Rep（革命军代表）
        ///   56371 Caulson（采矿）· 4615 Dale（药房）· 3319 Ashish（化学）
        ///   51189 Odin Haraldsson（革命商奥丁）
        /// phoneState：0=None 1=NotResponding 2=Responding 3=Introduction 4=Regular 5=Cooldown
        /// </summary>
        private static string BuildPhoneContacts(PlayerStore ps)
        {
            var sb = new StringBuilder(512);
            sb.Append('[');
            try
            {
                var dict = ps.PhoneClientDict;
                if (dict != null)
                {
                    bool first = true;
                    foreach (var kv in dict)
                    {
                        var pc = kv.Value;
                        if (pc == null) continue;
                        if (!first) sb.Append(',');
                        first = false;
                        sb.Append("{\"number\":").Append(kv.Key);
                        string name = null;
                        try { name = pc.GetLocalizedDisplayName(); } catch { }
                        sb.Append(",\"name\":").Append(JsonString(name));
                        sb.Append(",\"state\":").Append(JsonString(pc.phoneState.ToString()));
                        sb.Append(",\"cooldown\":").Append(pc.currentCooldown);
                        sb.Append(",\"dialed_before\":").Append(B(pc.dialedBefore));
                        bool atStore = false;
                        try { atStore = StorePhoneClient.IsPhoneClientAtStore(kv.Key); } catch { }
                        sb.Append(",\"at_store\":").Append(B(atStore));
                        sb.Append('}');
                    }
                }
            }
            catch (Exception e)
            {
                return "[{\"error\":" + JsonString(e.Message) + "}]";
            }
            sb.Append(']');
            return sb.ToString();
        }

        /// <summary>
        /// 交易桌状态：当前议价物品 / 议价值 / 玩家与顾客桌面物品网格。
        /// negociated_item 为 null 表示桌上空的——此时 sell_table/buy_table 是空操作，
        /// AI 应先用 place_item 把物品放上桌。
        /// </summary>
        /// <summary>按钮是否当前可点（激活 + interactable）——谈判界面按钮可用性判定用。</summary>
        private static bool IsButtonReady(Button b)
        {
            try
            {
                if (b == null) return false;
                if (!b.gameObject.activeInHierarchy) return false;
                return b.interactable;
            }
            catch { return false; }
        }

        private static string BuildTable(PlayerStore ps)
        {
            var sb = new StringBuilder(512);
            sb.Append('{');
            try
            {
                var item = ps.currentNegociatedItem;
                sb.Append("\"negociated_item\":").Append(item != null ? ItemJson(item) : "null");
                if (item != null)
                {
                    try { sb.Append(",\"negociated_value\":").Append(item.GetNegociatedValue()); }
                    catch { sb.Append(",\"negociated_value\":null"); }
                }
            }
            catch (Exception e) { sb.Append(",\"negociated_error\":").Append(JsonString(e.Message)); }

            try { sb.Append(",\"player_grid_items\":").Append(ItemsJson(ps.gridInv)); }
            catch (Exception e) { sb.Append(",\"player_grid_error\":").Append(JsonString(e.Message)); }

            try
            {
                var inst = ps.currentClientInstance;
                sb.Append(",\"client_grid_items\":").Append(inst != null ? ItemsJson(inst.gridInv) : "null");
            }
            catch (Exception e) { sb.Append(",\"client_grid_error\":").Append(JsonString(e.Message)); }

            // 谈判面板开闭态（v0.8.2：NegociationUIManager.uiObject.activeSelf；
            // 多 Agent 硬规则「未关闭界面必须优先关闭」的观测依据——此前无字段可确认，
            // open_negociation 后面板是否开着只能猜。仅店内场景 Instance 非空时输出）
            try
            {
                var nego = NegociationUIManager.Instance;
                if (nego != null)
                {
                    bool negoOpen = false;
                    try { var uo = nego.uiObject; negoOpen = uo != null && uo.activeSelf; } catch { }
                    sb.Append(",\"nego_open\":").Append(B(negoOpen));
                    // 谈判界面按钮可用性（v0.9.2：批发/接受多个/接受报价——
                    // 成交决策依据：有批发优先点批发按钮，而非单件直购）
                    try
                    {
                        sb.Append(",\"nego_buttons\":{");
                        sb.Append("\"wholesale\":").Append(B(IsButtonReady(nego.wholesaleButton)));
                        sb.Append(",\"accept_multiple\":").Append(B(IsButtonReady(nego.acceptMultipleButton)));
                        sb.Append(",\"accept_offer\":").Append(B(IsButtonReady(nego.acceptOfferButtonButton)));
                        sb.Append('}');
                        try { sb.Append(",\"wholesale_cost\":").Append(JsonString(nego.wholesaleCostTMP != null ? nego.wholesaleCostTMP.text : "")); } catch { }
                    }
                    catch { }
                }
            }
            catch { }

            sb.Append('}');
            return sb.ToString();
        }

        // v2.7 性能缓存：refused 需对全部库存逐件 IsClientRefusingItem（约 200 件），
        // 事件驱动每 0.5s 轮询 /state——不缓存会把观察拖到秒级（实测第三轮 15 分钟
        // 无第二步的假死根因）。同一顾客访问期内（标识+预算+物品数不变）只算一次。
        private static string _refusedCacheKey = "";
        private static string _refusedCacheJson = "[]";

        private static string BuildClient(StoreClientInstance instance)
        {
            var sb = new StringBuilder(512);
            sb.Append('{');
            try
            {
                var client = instance.storeClient;
                if (client != null)
                {
                    sb.Append("\"identifier\":").Append(JsonString(client.identifier));
                    sb.Append(",\"name\":").Append(JsonString(client.displayName));
                    sb.Append(",\"cash\":").Append(client.clientCash);
                    sb.Append(",\"budget\":").Append(client.clientBudget);
                    sb.Append(",\"buy_modifier\":").Append(client.buyPriceModifier);
                    sb.Append(",\"sell_modifier\":").Append(client.sellPriceModifier);
                    sb.Append(",\"dismissable\":").Append(B(client.dismissable));
                    sb.Append(",\"business_complete\":").Append(B(client.isBusinessComplete));
                    try { sb.Append(",\"is_buying\":").Append(B(client.IsClientBuying())); } catch { }
                    try { sb.Append(",\"is_selling\":").Append(B(client.IsClientSelling())); } catch { }
                    sb.Append(",\"service_cost\":").Append(client.serviceCost);
                    // 购买偏好（AI 挑物品的核心依据：卖顾客想收的类别，别拿牙膏卖给买食物的）
                    try { sb.Append(",\"buying_tags\":").Append(StringListJson(client.clientBuyingTagList)); } catch { }
                    try { sb.Append(",\"buying_ids\":").Append(StringListJson(client.clientBuyingIdList)); } catch { }
                    try { sb.Append(",\"black_tags\":").Append(StringListJson(client.clientBlackTagList)); } catch { }
                    try { sb.Append(",\"black_ids\":").Append(StringListJson(client.clientBlackIdList)); } catch { }
                    try { sb.Append(",\"budget_left\":").Append(client.GetBudget()); } catch { }
                    // 批发/多样性字段（v0.9.2：谈判界面批发按钮的决策依据——有批发优先批发）
                    try { sb.Append(",\"is_offer_wholesale\":").Append(B(client.isOfferWholesale)); } catch { }
                    try { sb.Append(",\"wholesale_discount\":").Append(client.wholesaleDiscount); } catch { }
                    try { sb.Append(",\"is_regular_sell_disable\":").Append(B(client.isRegularSellDisable)); } catch { }
                    try { sb.Append(",\"is_require_variety\":").Append(B(client.isRequireVariety)); } catch { }
                    // v2.7：顾客「明确拒收」的库存物品清单——比 is_require_variety 更直接的证据。
                    // 顾客多样性/黑名单会让某些物品必然被拒；AI 曾对同一冰淇淋重复卖出被拒 2 步。
                    // 有这份清单，sellable_now 之外 AI 也能一眼看到「别再试这些」。
                    try
                    {
                        PlayerStore psClient;
                        try { psClient = PlayerStore.Instance; } catch { psClient = null; }
                        var ownedAll = psClient != null ? psClient.FindAllItem(true) : null;
                        if (ownedAll != null)
                        {
                            int budgetNow = 0;
                            try { budgetNow = client.GetBudget(); } catch { }
                            string cacheKey = (client.identifier ?? "?") + ":" + budgetNow + ":" + ownedAll.Count;
                            if (cacheKey == _refusedCacheKey)
                            {
                                sb.Append(",\"refused\":").Append(_refusedCacheJson);
                            }
                            else
                            {
                                var seenIds = new System.Collections.Generic.HashSet<string>();
                                var refusedSb = new StringBuilder(160);
                                int rn = 0;
                                int scanCap = Math.Min(ownedAll.Count, 120);   // 扫描上限，双保险
                                for (int i = 0; i < scanCap && rn < 12; i++)
                                {
                                    var it0 = ownedAll[i];
                                    if (it0 == null || string.IsNullOrEmpty(it0.identifier) || !seenIds.Add(it0.identifier)) continue;
                                    bool ref0 = false;
                                    try { ref0 = client.IsClientRefusingItem(it0); } catch { }
                                    if (!ref0) continue;
                                    if (rn > 0) refusedSb.Append(',');
                                    refusedSb.Append(JsonString(it0.identifier));
                                    rn++;
                                }
                                _refusedCacheJson = "[" + refusedSb.ToString() + "]";
                                _refusedCacheKey = cacheKey;
                                sb.Append(",\"refused\":").Append(_refusedCacheJson);
                            }
                        }
                    }
                    catch { }
                    // 通缉/举报字段（v0.8.0：report_client 的决策依据——悬赏金额/声望代价/可否举报）
                    sb.Append(",\"is_wanted\":").Append(B(client.isWanted));
                    sb.Append(",\"is_stealth_wanted\":").Append(B(client.isStealthWanted));
                    sb.Append(",\"reported\":").Append(B(client.reported));
                    sb.Append(",\"report_reward\":").Append(client.reportReward);
                    sb.Append(",\"reputation_penalty\":").Append(client.reputationPenalty);
                    sb.Append(",\"arrestable\":").Append(B(client.arrestable));
                    sb.Append(",\"is_aug\":").Append(B(client.isAug));
                    try { sb.Append(",\"real_name\":").Append(JsonString(client.realName)); } catch { }
                    bool canReport = false;
                    try { canReport = client.CanReport(); } catch { }
                    sb.Append(",\"can_report\":").Append(B(canReport));
                    bool secArrestable = false;
                    try { var sec = StoreSecButton.Instance; if (sec != null) secArrestable = sec.IsClientArrestable(); } catch { }
                    sb.Append(",\"sec_button_lit\":").Append(B(secArrestable));
                }
            }
            catch (Exception e)
            {
                sb.Append("\"error\":").Append(JsonString(e.Message));
            }
            sb.Append('}');
            return sb.ToString();
        }

        // ---------------------------------------------------------------- 调试信息

        private static string BuildDebugClient()
        {
            var sb = new StringBuilder(2048);
            sb.Append('{');
            PlayerStore ps;
            try { ps = PlayerStore.Instance; }
            catch { ps = null; }
            if (ps == null)
            {
                sb.Append("\"in_game\":false}");
                return sb.ToString();
            }
            sb.Append("\"in_game\":true");

            try
            {
                var item = ps.currentNegociatedItem;
                sb.Append(",\"negociated_item\":").Append(item != null ? ItemJson(item) : "null");
            }
            catch (Exception e) { sb.Append(",\"negociated_error\":").Append(JsonString(e.Message)); }

            try
            {
                var inst = ps.currentClientInstance;
                sb.Append(",\"has_client\":").Append(B(inst != null));
                if (inst != null)
                {
                    // v2.1：先取 grid 再拼——避免取字段抛异常时留下 `"client_grid_items":` 悬空
                    var cgrid = inst.gridInv;
                    sb.Append(",\"client_grid_items\":").Append(ItemsJson(cgrid));
                }
            }
            catch (Exception e) { sb.Append(",\"client_error\":").Append(JsonString(e.Message)); }

            try
            {
                var grid = ps.gridInv;
                sb.Append(",\"player_grid_items\":").Append(ItemsJson(grid));
            }
            catch (Exception e) { sb.Append(",\"player_grid_items\":null,\"player_grid_error\":").Append(JsonString(e.Message)); }

            // 玩家拥有 / 未拥有的全部物品（游戏自身查询 API）
            // v2.1：先取列表再拼 JSON——原写法 `Append(",\"x\":").Append(ItemListJson(FindAllItem(...)))`
            // 在取列表抛异常时会留下 `"x":` 悬空，整个响应变成非法 JSON
            //（实测 /debug/client 因此报 `Expected value at char 68125`，调试接口直接不可用）。
            try
            {
                var ownedItems = ps.FindAllItem(true);
                sb.Append(",\"owned_items\":").Append(ItemListJson(ownedItems));
            }
            catch (Exception e) { sb.Append(",\"owned_items\":[],\"owned_error\":").Append(JsonString(e.Message)); }

            try
            {
                var notOwnedItems = ps.FindAllItem(false);
                sb.Append(",\"not_owned_items\":").Append(ItemListJson(notOwnedItems));
            }
            catch (Exception e) { sb.Append(",\"not_owned_items\":[],\"not_owned_error\":").Append(JsonString(e.Message)); }

            // 议价系统"可售物品"列表
            try
            {
                var nego = NegociationUIManager.Instance;
                if (nego != null)
                {
                    var sellable = nego.GetSellableItem();
                    sb.Append(",\"sellable_items\":").Append(ItemListJson(sellable));
                }
            }
            catch (Exception e) { sb.Append(",\"sellable_items\":[],\"sellable_error\":").Append(JsonString(e.Message)); }

            sb.Append('}');
            return sb.ToString();
        }

        /// <summary>
        /// 从 not_owned 列表里剔除「同名且已拥有」的物品（v2.9）。
        ///
        /// 游戏的两个查询会重叠：收购完成后同一件物品既在 FindAllItem(true)
        /// 也在 FindAllItem(false)。两个列表同时给到 AI，它会误判"这件不是我的"
        /// 或"顾客还有货没卖"。owned 优先——它才是可卖的那份。
        /// </summary>
        private static Il2CppSystem.Collections.Generic.List<GameItem> _ExcludeOwned(
            Il2CppSystem.Collections.Generic.List<GameItem> notOwned,
            Il2CppSystem.Collections.Generic.List<GameItem> owned)
        {
            if (notOwned == null) return null;
            if (owned == null || owned.Count == 0) return notOwned;
            var ownedIds = new System.Collections.Generic.HashSet<string>();
            for (int i = 0; i < owned.Count; i++)
            {
                var it = owned[i];
                if (it != null && it.identifier != null) ownedIds.Add(it.identifier.ToString());
            }
            if (ownedIds.Count == 0) return notOwned;
            var filtered = new Il2CppSystem.Collections.Generic.List<GameItem>(notOwned.Count);
            for (int i = 0; i < notOwned.Count; i++)
            {
                var it = notOwned[i];
                if (it == null) continue;
                string id = it.identifier != null ? it.identifier.ToString() : "";
                if (ownedIds.Contains(id)) continue;
                filtered.Add(it);
            }
            return filtered;
        }

        private static string ItemListJson(Il2CppSystem.Collections.Generic.List<GameItem> items)
        {
            var sb = new StringBuilder(512);
            sb.Append('[');
            if (items != null)
            {
                int count = items.Count;
                for (int i = 0; i < count; i++)
                {
                    if (i > 0) sb.Append(',');
                    var item = items[i];
                    sb.Append(item != null ? ItemJson(item) : "null");
                }
            }
            sb.Append(']');
            return sb.ToString();
        }

        private static string ItemsJson(GameGridInventory grid)
        {
            if (grid == null) return "null";
            var sb = new StringBuilder(512);
            sb.Append('[');
            try
            {
                var items = grid.childItems;
                if (items != null)
                {
                    int count = items.Count;
                    for (int i = 0; i < count; i++)
                    {
                        if (i > 0) sb.Append(',');
                        var item = items[i];
                        sb.Append(item != null ? ItemJson(item) : "null");
                    }
                }
            }
            catch (Exception e)
            {
                sb.Append("{\"error\":").Append(JsonString(e.Message)).Append('}');
            }
            sb.Append(']');
            return sb.ToString();
        }

        private static string ItemJson(GameItem item)
        {
            var sb = new StringBuilder(160);
            sb.Append('{');
            sb.Append("\"name\":").Append(JsonString(item.name));
            sb.Append(",\"identifier\":").Append(JsonString(item.identifier));
            sb.Append(",\"value\":").Append(item.unitValue);
            sb.Append(",\"count\":").Append(item.unitCount);
            // 物品类别标签（如 food/drink/ toiletries）——与 client.buying_tags 对照判断顾客是否收
            try { sb.Append(",\"item_types\":").Append(StringListJson(item.itemTypes)); } catch { }
            // 所在容器与网格格子坐标（v0.4.3：AI 据此换算像素坐标，ctrl_click 精确点击。
            // inv=容器类型名；mx/my=物品在容器网格中的格子原点（GridShape.minX/minY）；
            // cw/ch=格子像素尺寸；ox/oy=容器 UI 锚点世界坐标——供校准换算公式用）
            try
            {
                var parent = item.parentInventory;
                if (parent != null)
                {
                    sb.Append(",\"inv\":").Append(JsonString(parent.GetIl2CppType().Name));
                    try
                    {
                        var grid = parent.TryCast<GameGridInventory>();
                        if (grid != null)
                        {
                            var ishape = grid.inventoryShape;
                            if (ishape != null && ishape.globalWidth > 0 && ishape.globalHeight > 0)
                            {
                                sb.Append(",\"cw\":").Append((int)(grid.widthPixels / ishape.globalWidth));
                                sb.Append(",\"ch\":").Append((int)(grid.heightPixels / ishape.globalHeight));
                            }
                            var rt = grid.rectTransform;
                            if (rt != null)
                            {
                                var p = rt.position;
                                sb.Append(",\"ox\":").Append((int)p.x).Append(",\"oy\":").Append((int)p.y);
                            }
                        }
                    }
                    catch { }
                }
            }
            catch { }
            try
            {
                var shape = item.shape;
                if (shape != null)
                {
                    sb.Append(",\"mx\":").Append(shape.minX).Append(",\"my\":").Append(shape.minY);
                }
            }
            catch { }
            sb.Append('}');
            return sb.ToString();
        }

        /// <summary>Il2Cpp 字符串列表 → JSON 数组（null 按空数组）。</summary>
        private static string StringListJson(Il2CppSystem.Collections.Generic.List<string> list)
        {
            var sb = new StringBuilder(32);
            sb.Append('[');
            if (list != null)
            {
                for (int i = 0; i < list.Count; i++)
                {
                    if (i > 0) sb.Append(',');
                    sb.Append(JsonString(list[i]));
                }
            }
            sb.Append(']');
            return sb.ToString();
        }

        // ---------------------------------------------------------------- 动作执行

        private static string HandleAction(string body)
        {
            var action = ExtractValue(body, "action");
            if (string.IsNullOrEmpty(action))
            {
                return Err("缺少 action 字段");
            }

            PlayerStore ps;
            try { ps = PlayerStore.Instance; }
            catch { ps = null; }

            switch (action)
            {
                case "open_shutter":
                {
                    var shutter = StoreShutterButton.Instance;
                    if (shutter != null)
                    {
                        shutter.OpenShutter();
                        return OkWithState("StoreShutterButton.OpenShutter() 已调用");
                    }
                    if (ps != null)
                    {
                        ps.OpenShutter();
                        return OkWithState("PlayerStore.OpenShutter() 已调用（无按钮实例）");
                    }
                    return Err("StoreShutterButton 与 PlayerStore 均不可用");
                }
                case "close_shutter":
                {
                    var shutter = StoreShutterButton.Instance;
                    if (shutter == null) return Err("StoreShutterButton 不可用");
                    shutter.CloseShutter();
                    return OkWithState("StoreShutterButton.CloseShutter() 已调用");
                }
                case "toggle_shutter":
                {
                    var shutter = StoreShutterButton.Instance;
                    if (shutter == null) return Err("StoreShutterButton 不可用");
                    shutter.ToggleShutterDoor();
                    return OkWithState("StoreShutterButton.ToggleShutterDoor() 已调用");
                }
                case "call_next":
                {
                    // v0.8.1 守卫（用户实测反馈：队列空了还反复叫号+等待，纯浪费）——
                    // CanNextClient()=false 且无在场顾客 = 今日顾客队列已空，直接走夜晚流程
                    if (ps == null) return Err("PlayerStore 不可用");
                    bool canNext = false;
                    bool hasClient = false;
                    try { canNext = ps.CanNextClient(); } catch { }
                    try { hasClient = ps.IsClientPresent(); } catch { }
                    if (!canNext && !hasClient)
                    {
                        // 区分「没开门/新一天未开始」与「今日队列已空」——后者才该打烊
                        string storeState = "";
                        try { storeState = (ps.storeState.ToString() ?? "").ToUpperInvariant(); } catch { }
                        if (storeState == "MORNING")
                            return Err("新一天还未开始营业（store_state=MORNING）——先 begin_day 开始这一天");
                        if (storeState == "CLOSED")
                            return Err("店门还没开（store_state=CLOSED）——先 open_shutter 开门，顾客才会来");
                        return Err("⚠️ 今日顾客队列已空（queue.can_next=false 且无顾客在场）——不要再 call_next 空转！"
                            + "立即进入夜晚流程：end_day → night_services → night_service 勾选服务（保险/清洁优先）"
                            + " → end_day_confirm → open_map → go_out/stay_home");
                    }
                    if (!canNext && hasClient)
                        return Err("队列已空（queue.can_next=false）但当前顾客还在店里——先处理当前顾客"
                            + "（dialog_advance 对话 / sell_item 交易 / dismiss_client 送客），送客后直接 end_day 打烊");
                    // v0.9.1 按钮优先（「UI 按钮化」）：按铃叫客（StoreBell.OnClick = 玩家路径）——
                    // 组件缺失时降级为原直调实现。
                    var bellBtn = FindCustomClickable("StoreBell", null, null);
                    if (bellBtn != null)
                    {
                        string bellType = InvokeCustomClick(bellBtn);
                        if (bellType != null)
                            return OkWithState($"已按铃叫客（{bellType}.OnClick，按钮路径）");
                    }
                    ps.TryCallNextClient();
                    return OkWithState("PlayerStore.TryCallNextClient() 已调用");
                }
                case "dismiss_client":
                {
                    // v0.7.5 守卫（用户实测 bug：AI 把来收租的房东当普通顾客打发，
                    // 导致欠租 -4 天恶性循环）——房东来访时禁止 dismiss，必须先交租
                    if (ps == null) return Err("PlayerStore 不可用");
                    string ident = "";
                    bool isWholesaleVisit = false;
                    try
                    {
                        var inst = ps.currentClientInstance;
                        var cl = inst != null ? inst.storeClient : null;
                        if (cl != null && cl.identifier != null) ident = cl.identifier.ToString();
                        // v1.3：房东还会以「批发商」身份到店（landlord_wholesale，
                        // isOfferWholesale=true）——那是普通顾客，可以送客；
                        // 只有收租日真正来收租的房东才禁止打发（r70 死锁实证）。
                        if (cl != null) isWholesaleVisit = cl.isOfferWholesale;
                    }
                    catch { }
                    // 只按「非批发来访的房东」判定（租约天数可能提前一天到店，
                    // 不拿天数做条件，避免漏拦真正的收租；批发版房东是普通顾客）
                    if (ident.ToLowerInvariant().Contains("landlord") && !isWholesaleVisit)
                    {
                        bool canPay = false;
                        try { canPay = ps.CanPayRent(); } catch { }
                        return Err($"⚠️ 这是房东（收租日来访，应收 {ps.rentValue}）——绝不能打发房东走！"
                            + (canPay
                                ? $"现金足够（{ps.playerCash}），立即 pay_rent 交租。"
                                : $"现金不足（{ps.playerCash} < {ps.rentValue}），先卖出库存回款凑租。"));
                    }
                    // v0.9.1 按钮优先（「UI 按钮化」）：走送客按钮（StoreDismissButton.OnClick =
                    // IsClientPresent/Dismissable 自检 + 送客，玩家路径）——组件缺失时降级为原直调。
                    var disBtn = FindCustomClickable("StoreDismissButton", null, null);
                    if (disBtn != null)
                    {
                        string disType = InvokeCustomClick(disBtn);
                        if (disType != null)
                            return OkWithState("顾客已打发离店（" + disType + ".OnClick，按钮路径）"
                                + (ident != "" ? $"（{ident}）" : ""));
                    }
                    ps.DismissCurrentClient(true, true, true);
                    return OkWithState("顾客已打发离店" + (ident != "" ? $"（{ident}）" : ""));
                }

                case "force_dismiss":
                {
                    // v1.4 恢复专用（不进 AI 动作白名单）：特殊 NPC 卡店时强制遣散。
                    // 背景（r65 治安盘查员 / r75 房东）：对话结束后离店协程被打断，
                    // 客户端 dismissable=false + business_complete=false，送客按钮与
                    // 卷帘门（「队列中还有重要人物」）全被拒 → 整局死锁，只能重开存档。
                    // 这里直接调 PlayerStore.DismissCurrentClient（绕过按钮自检）作为
                    // 人工恢复手段；调用后必须验证顾客真的离店，否则如实报失败。
                    if (ps == null) return Err("PlayerStore 不可用");
                    string fid = "";
                    try
                    {
                        var fi = ps.currentClientInstance;
                        var fc = fi != null ? fi.storeClient : null;
                        if (fc != null && fc.identifier != null) fid = fc.identifier.ToString();
                    }
                    catch { }
                    if (fid == "") return Err("当前没有顾客可强制遣散");
                    try { ps.DismissCurrentClient(false, true, true); }
                    catch (Exception ex) { return Err($"强制遣散异常：{ShortErr(ex)}"); }
                    System.Threading.Thread.Sleep(600);
                    bool stillThere = false;
                    try { stillThere = ps.currentClientInstance != null; } catch { }
                    if (stillThere)
                        return Err($"强制遣散后顾客仍在店（{fid}）——游戏可能仍在离店协程中，稍后重试");
                    return OkWithState($"已强制遣散卡店顾客（{fid}，恢复专用动作）");
                }
                case "dialog_advance":
                {
                    var dlg = DialogUIManager.Instance;
                    if (dlg == null) return Err("DialogUIManager 不可用");
                    dlg.ContinueConversation();
                    return OkWithState("DialogUIManager.ContinueConversation() 已调用");
                }
                case "dialog_choose":
                {
                    var dlg = DialogUIManager.Instance;
                    if (dlg == null) return Err("DialogUIManager 不可用");
                    var wantText = ExtractValue(body, "text");
                    var wantIndexRaw = ExtractValue(body, "index");

                    // v2.5：按**选项文字**选（玩家看到什么点什么），不再靠猜序号。
                    // r75 的教训：盲选 index 0 走进「延期」分支导致死锁。
                    if (!string.IsNullOrEmpty(wantText))
                    {
                        var bubbles = dlg.dialogChoiceBubbles;
                        if (bubbles == null) return Err("当前对话没有选项气泡");
                        int exact = -1, partial = -1;
                        string partialText = "";
                        string all = "";
                        for (int bi = 0; bi < bubbles.Count; bi++)
                        {
                            var b = bubbles[bi];
                            if (b == null) continue;
                            bool active = false;
                            try { active = b.gameObject != null && b.gameObject.activeSelf; } catch { }
                            if (!active) continue;
                            string ct = "";
                            try { if (b.TMP != null) ct = b.TMP.text ?? ""; } catch { }
                            if (all.Length > 0) all += " | ";
                            all += ct;
                            if (ct.Trim() == wantText.Trim()) exact = b.index;
                            if (partial < 0 && ct.IndexOf(wantText, StringComparison.OrdinalIgnoreCase) >= 0)
                            { partial = b.index; partialText = ct; }
                        }
                        int chosen = exact >= 0 ? exact : partial;
                        if (chosen < 0)
                            return Err($"没有匹配「{wantText}」的选项（当前：{all}）");
                        dlg.SelectChoice(chosen);
                        return OkWithState($"已选择选项 [{chosen}]「{partialText}」（按文字匹配；玩家点击同一条气泡）");
                    }

                    int index = int.TryParse(wantIndexRaw, out var i) ? i : 0;
                    dlg.SelectChoice(index);
                    return OkWithState($"DialogUIManager.SelectChoice({index}) 已调用");
                }
                case "click_button":
                {
                    // v0.9.0/v0.9.1「UI 按钮化」：触发界面按钮点击（等效玩家点击）——
                    // 操作「玩家一般操作的地方」（用户 2026-10-16 指令），避免直调底层方法
                    // 造成的状态脱节（UI 流程被跳过）。
                    // 支持两类：① 游戏自定义 IClickable 组件（type+name / text=工具提示）→ OnClick()
                    //          ② uGUI 标准 Button（name / text 匹配）→ onClick.Invoke()
                    // 配套查询：GET /ui/buttons 枚举当前可用按钮。
                    var btnName = ExtractValue(body, "name");
                    var btnText = ExtractValue(body, "text");
                    var btnType = ExtractValue(body, "type");
                    var btnPath = ExtractValue(body, "path");
                    if (string.IsNullOrEmpty(btnName) && string.IsNullOrEmpty(btnText) && string.IsNullOrEmpty(btnType)
                        && string.IsNullOrEmpty(btnPath))
                        return Err("click_button 需要 name / text / type / path 参数（用 GET /ui/buttons 查看当前可用按钮）");
                    // v2.5：click_button 支持 op=click|double_click|right_click，
                    // 并会兜底到「全量可点击对象」（自绘界面的条目不在 uGUI/白名单里）
                    var btnOp = ExtractValue(body, "op");
                    // ① 自定义组件路径
                    var custom = FindCustomClickable(btnType, btnName, btnText);
                    if (custom != null)
                    {
                        string clickedType = InvokeCustomClick(custom);
                        if (clickedType == null)
                            return Err($"自定义按钮「{custom.name}」点击失败（OnClick 异常，见 BepInEx 日志）");
                        return OkWithState($"已触发自定义按钮「{clickedType}:{custom.name}」（OnClick）");
                    }
                    // ② uGUI 标准按钮路径（v1.6：path=层级路径子串匹配，解决同名按钮歧义，
                    //    如 CanvasPaperUI 下 _NegociationPanel 与 _AdvCalendar 各有一个 CloseButton）
                    var btn = FindButton(btnName, btnText, btnPath);
                    if (btn == null)
                    {
                        // v2.5 兜底：自绘界面（平板/网络商店/机器面板）的条目不是 uGUI Button，
                        // 也不是白名单里的 26 个类型——在全量可点击对象里找。
                        var any = FindAnyClickable(btnType, btnName, btnText, btnPath);
                        if (any != null)
                        {
                            string acted = InvokeClickableAction(any, btnOp);
                            if (acted == null)
                                return Err($"对象「{any.name}」不支持该操作（op={btnOp}）——它只支持 "
                                    + (string.IsNullOrEmpty(btnOp) ? "click" : "其它点击方式"));
                            string tn1 = ""; try { tn1 = any.GetIl2CppType().Name; } catch { }
                            if (string.IsNullOrEmpty(tn1) || tn1 == "MonoBehaviour") tn1 = any.name;
                            return OkWithState($"已对「{tn1}:{any.name}」执行 {acted}（OnClick 路径）");
                        }
                        // v2.7：猜文本失败时给出专用动作指引，避免 AI 连续空转
                        //（实测：连猜"打发走当前顾客"5 次全失败）
                        string hint = "";
                        string blob = (btnName + btnText).ToLowerInvariant();
                        if (blob.Contains("打发") || blob.Contains("遣散") || blob.Contains("送走"))
                            hint = "；遣散顾客请用专用动作 dismiss_client（或 force_dismiss）";
                        else if (blob.Contains("呼叫") || blob.Contains("叫下") || blob.Contains("下一位"))
                            hint = "；叫客请用专用动作 call_next";
                        else if (blob.Contains("结束") || blob.Contains("打烊") || blob.Contains("关门"))
                            hint = "；打烊请用专用动作 end_day";
                        return Err($"未找到按钮（type={btnType} name={btnName} text={btnText} path={btnPath}）——"
                            + "用 GET /ui/buttons 或 /ui/objects 查看当前可用对象" + hint);
                    }
                    bool btnInteractable = false;
                    try { btnInteractable = btn.interactable; } catch { }
                    if (!btnInteractable)
                        return Err($"按钮「{btn.name}」当前不可交互（interactable=false）");
                    btn.onClick.Invoke();
                    return OkWithState($"已触发按钮「{btn.name}」点击（onClick.Invoke）");
                }
                case "accept_offer":
                {
                    var nego = NegociationUIManager.Instance;
                    if (nego == null) return Err("NegociationUIManager 不可用");
                    nego.OnOfferAcceptClick();
                    return OkWithState("NegociationUIManager.OnOfferAcceptClick() 已调用");
                }
                case "place_item":
                {
                    // 把指定物品放上交易桌（P4 交易闭环的第一步）。
                    // 上桌方式按顾客意图自动选择：顾客在买→玩家出售；顾客在卖→玩家收购；否则以物易物。
                    var identifier = ExtractValue(body, "identifier");
                    if (string.IsNullOrEmpty(identifier))
                    {
                        return Err("place_item 需要 identifier 参数（物品标识，如 toothpaste）");
                    }
                    if (ps == null) return Err("PlayerStore 不可用");

                    // 顾客意图（探测失败按以物易物处理）
                    bool clientBuying = false;
                    bool clientSelling = false;
                    try
                    {
                        var inst = ps.currentClientInstance;
                        var client = inst != null ? inst.storeClient : null;
                        if (client != null)
                        {
                            clientBuying = client.IsClientBuying();
                            clientSelling = client.IsClientSelling();
                        }
                    }
                    catch { }

                    // 候选物品：先议价 UI 的可售列表，再按意图回退到物品全集
                    // （顾客在卖→未拥有全集；其余→拥有全集）
                    GameItem target = null;
                    try
                    {
                        var nego = NegociationUIManager.Instance;
                        var sellable = nego != null ? nego.GetSellableItem() : null;
                        if (sellable != null)
                        {
                            for (int i = 0; i < sellable.Count; i++)
                            {
                                var c = sellable[i];
                                if (c != null && c.identifier != null && c.identifier.ToString() == identifier)
                                {
                                    target = c;
                                    break;
                                }
                            }
                        }
                    }
                    catch { }
                    if (target == null)
                    {
                        try
                        {
                            var all = ps.FindAllItem(!clientSelling);
                            if (all != null)
                            {
                                for (int i = 0; i < all.Count; i++)
                                {
                                    var c = all[i];
                                    if (c != null && c.identifier != null && c.identifier.ToString() == identifier)
                                    {
                                        target = c;
                                        break;
                                    }
                                }
                            }
                        }
                        catch { }
                    }
                    if (target == null)
                    {
                        return Err($"未找到物品：{identifier}（可售列表与物品全集均无此标识）");
                    }

                    // 类型匹配守卫（2026-10-14 实测教训：买食物的顾客不收日用品——
                    // 上桌前先问游戏「这位顾客收不收这件」，直接拒绝注定失败的放置）
                    try
                    {
                        var inst0 = ps.currentClientInstance;
                        var client0 = inst0 != null ? inst0.storeClient : null;
                        if (client0 != null && clientBuying)
                        {
                            if (!client0.IsClientBuyingThisItem(target))
                            {
                                bool refusing = false;
                                try { refusing = client0.IsClientRefusingItem(target); } catch { }
                                return Err($"顾客不会买 {target.name}（{identifier}）——顾客想收的类别见 client.buying_tags"
                                    + (refusing ? "（该物品在顾客黑名单中）" : "（类别不匹配）"));
                            }
                            if (!client0.HasBudgetLeftToBuy(target))
                            {
                                return Err($"顾客预算不足以购买 {target.name}（{identifier}）");
                            }
                        }
                    }
                    catch { }

                    // —— v0.5.0 纯变量路线（按用户要求：不模拟鼠标，直接控制游戏状态）——
                    // 根因链（2026-10-14 全天实测）：UI 的 CTRL+点击/拖拽最终就是把
                    // PlayerStore.currentNegociatedItem（0xE0）与 NegociationUIManager.
                    // currentNegociatedItem（0x58）指向目标物品，之后 accept_offer/sell_table
                    // 才会生效；而 PlacedItemFor*/AddDirectSellingItemToTable 直调均不写这些
                    // 字段（静默 return）——所以直接写字段即等价于「上桌」。
                    ps.currentNegociatedItem = target;
                    try
                    {
                        var negoUi = NegociationUIManager.Instance;
                        if (negoUi != null) negoUi.currentNegociatedItem = target;
                    }
                    catch { }

                    // 诊断：游戏自己判断的成交可行性与报价（AI 决策依据）
                    string diag;
                    long value = -1;
                    try { value = target.GetNegociatedValue(); } catch { }
                    try
                    {
                        bool canSell = false, canBuy = false;
                        try { canSell = ps.CanSellItemFromTable(); } catch { }
                        try { canBuy = ps.CanBuyItemFromTable(); } catch { }
                        diag = $"CanSell={canSell} CanBuy={canBuy} 报价={value}";
                    }
                    catch { diag = $"报价={value}"; }

                    // 打开谈判界面（与手动流程一致；面板开着时 OnOfferAcceptClick 才响应）
                    string uiNote = "";
                    try
                    {
                        var negoUi2 = NegociationUIManager.Instance;
                        if (negoUi2 != null) { negoUi2.OpenUI(); uiNote = "，谈判界面已打开"; }
                    }
                    catch { }
                    // v1.0 措辞诚实化（用户 2026-10-18 指令：不把底层字段操作包装成
                    // 「放上桌」）：该动作与玩家 Ctrl+点击/拖拽上桌写入同一字段
                    // （v0.5.0 实证），但**不移动物品网格、不播放拖动动画**，
                    // 属「标记议价物品」而非物理上桌——如实描述，避免误导决策。
                    return OkWithState($"已标记议价物品 {target.name}（{identifier}）[{diag}]{uiNote}"
                        + $"（等效玩家 Ctrl+点击上桌的字段效果，但物品不物理移动；"
                        + $"成交请用 sell_item/buy_item，换顾客前请 clear_table）");
                }

                case "ui_hit":
                {
                    return UiHit(ExtractValue(body, "x"), ExtractValue(body, "y"));
                }
                case "ui_click":
                {
                    // v2.5：玩家点击的输入管线（与鼠标点击同一条 EventSystem 链路）
                    var ux = ExtractValue(body, "x");
                    var uy = ExtractValue(body, "y");
                    float fx, fy;
                    if (!float.TryParse(ux, out fx) || !float.TryParse(uy, out fy))
                        return Err("ui_click 需要数值 x / y（Unity 屏幕坐标）");
                    var rbtn = ExtractValue(body, "button");
                    bool right = !string.IsNullOrEmpty(rbtn) && rbtn.ToLowerInvariant().StartsWith("right");
                    bool dbl = ExtractValue(body, "double") == "true" || ExtractValue(body, "double") == "1";
                    return DispatchPointerClick(fx, fy, right, dbl);
                }
                case "expedition_send":
                {
                    // v2.5：玩家路径 = 夜间「雇工/远征」面板选地点与雇工。
                    // 本存档 isExpeditionUnlocked=false，任何派遣都非法——如实拒绝，
                    // 绝不调用 ExpeditionData.CreateExpedition 这类内部方法。
                    if (ps == null) return Err("PlayerStore 不可用");
                    var data = ps.expeditionData;
                    if (data == null) return Err("远征系统不可用（expeditionData 为空）");
                    if (!data.isExpeditionUnlocked)
                        return Err("远征未解锁（isExpeditionUnlocked=false）——需要先通过雇工/引荐解锁；"
                            + "在此之前派遣属于非法操作，本动作不改变任何状态");
                    int hireCount = data.hirelings != null ? data.hirelings.Count : 0;
                    if (hireCount == 0)
                        return Err("没有雇工（hirelings 为空）——先招募雇工，远征需要至少 1 名");
                    return Err("远征已解锁，但派遣面板是自绘界面：需用 ui_hit 定位面板条目 + ui_click 按坐标点击，"
                        + "本动作不做底层直调（避免绕过校验）");
                }
                case "hire_worker":
                {
                    if (ps == null) return Err("PlayerStore 不可用");
                    var data = ps.expeditionData;
                    if (data == null) return Err("雇工系统不可用（expeditionData 为空）");
                    if (!data.isExpeditionUnlocked)
                        return Err("雇工未解锁（isExpeditionUnlocked=false）——招募入口随远征系统解锁；本动作不改变任何状态");
                    return Err("雇工已解锁，但招募面板是自绘界面：需用 ui_hit + ui_click 按坐标点击；"
                        + "本动作不调用 Hireling.Hire 这类内部方法");
                }
                case "ui_drag":
                {
                    // v2.6：完整拖拽（玩家按住→拖→松手）。
                    // 走 IDragHandler / IDropHandler 链路，落点由目标控件自己判定是否接受；
                    // 不接受时游戏内部会放回原格，不需要我们做任何回滚。
                    var sx = ExtractValue(body, "from_x");
                    var sy = ExtractValue(body, "from_y");
                    var dx = ExtractValue(body, "to_x");
                    var dy = ExtractValue(body, "to_y");
                    float fx, fy, tx, ty;
                    if (!float.TryParse(sx, out fx) || !float.TryParse(sy, out fy)
                        || !float.TryParse(dx, out tx) || !float.TryParse(dy, out ty))
                        return Err("ui_drag 需要数值 from_x/from_y/to_x/to_y（Unity 屏幕坐标）");
                    return DispatchDrag(fx, fy, tx, ty);
                }
                case "object_action":
                {
                    // v2.5：对任意可点击对象执行玩家操作。
                    // op=click（左键）/ double_click（双击）/ right_click（右键，带默认落点）
                    var oType = ExtractValue(body, "type");
                    var oName = ExtractValue(body, "name");
                    var oText = ExtractValue(body, "text");
                    var oPath = ExtractValue(body, "path");
                    var oOp = ExtractValue(body, "op");
                    if (string.IsNullOrEmpty(oType) && string.IsNullOrEmpty(oName)
                        && string.IsNullOrEmpty(oText) && string.IsNullOrEmpty(oPath))
                        return Err("object_action 需要 type / name / text / path 之一（看 /ui/objects）");
                    var targetObj = FindAnyClickable(oType, oName, oText, oPath);
                    if (targetObj == null)
                        return Err($"没有匹配的可点击对象（type={oType} name={oName} text={oText} path={oPath}）");
                    string done = InvokeClickableAction(targetObj, oOp);
                    if (done == null)
                        return Err($"对象「{targetObj.name}」不支持 op={oOp}（它支持："
                            + (targetObj.TryCast<IClickable>() != null ? "click " : "")
                            + (targetObj.TryCast<IDoubleClickable>() != null ? "double_click " : "")
                            + (targetObj.TryCast<IControlable>() != null ? "right_click" : "") + "）");
                    string tn2 = ""; try { tn2 = targetObj.GetIl2CppType().Name; } catch { }
                    if (string.IsNullOrEmpty(tn2) || tn2 == "MonoBehaviour") tn2 = targetObj.name;
                    return OkWithState($"已对「{tn2}:{targetObj.name}」执行 {done}");
                }
                case "open_negociation":
                {
                    // 打开谈判界面（物品已在桌上时可显式调用；与 place_item 的自动打开互补）
                    var negoUi = NegociationUIManager.Instance;
                    if (negoUi == null) return Err("NegociationUIManager 不可用（可能不在店内场景）");
                    negoUi.OpenUI();
                    return OkWithState("NegociationUIManager.OpenUI() 已调用");
                }

                case "close_negociation":
                {
                    // v0.8.2：关闭谈判界面（镜像 open_negociation；多 Agent 硬规则
                    // 「未关闭界面必须优先关闭」的执行手段；开闭态见 /state table.nego_open）
                    var negoUi = NegociationUIManager.Instance;
                    if (negoUi == null) return Err("NegociationUIManager 不可用（可能不在店内场景）");
                    negoUi.CloseUI();
                    return OkWithState("NegociationUIManager.CloseUI() 已调用");
                }

                case "clear_table":
                {
                    // v0.7.1：清桌——把议价显示状态置空。place_item 只写 currentNegociatedItem
                    // 显示字段（物品不真正转移，游戏画面桌上没东西），换顾客后残留会造成
                    // 「桌上一直有幽灵物品」的误判——本动作显式清掉。
                    if (ps == null) return Err("PlayerStore 不可用");
                    ps.currentNegociatedItem = null;
                    try
                    {
                        var negoUi = NegociationUIManager.Instance;
                        if (negoUi != null) negoUi.currentNegociatedItem = null;
                    }
                    catch { }
                    return OkWithState("议价显示状态已清空（table.negociated_item → null）");
                }

                case "sell_item":
                {
                    // v1.0「玩家路径」重写（用户 2026-10-18 指令：禁止底层直调跳过流程）。
                    // 反汇编实证：谈判面板「接受报价」按钮处理器 OnOfferAcceptClick() 内部链 =
                    //   IsItemOwned 判定 → PlayerStore.BuyItem/SellItem → BuyDealed →
                    //   CompleteBusiness → 结果对话/UI 刷新。也就是说玩家成交 =
                    //   「标记议价物品（Ctrl+点击上桌）→ 点接受报价按钮」。原直调
                    //   ps.SellItem() 只有核心扣账，跳过整条 UI 链——已废弃。
                    // 保留的守卫：类别校验（v0.7.3b）、现金 diff 真伪判定（v0.7.2）。
                    var identifier = ExtractValue(body, "identifier");
                    if (string.IsNullOrEmpty(identifier))
                        return Err("sell_item 需要 identifier 参数");
                    if (ps == null) return Err("PlayerStore 不可用");
                    GameItem target = FindOwnedItem(ps, identifier);
                    if (target == null) return Err($"未找到自己的物品：{identifier}（看 inventory.owned）");
                    try
                    {
                        var inst = ps.currentClientInstance;
                        var client = inst != null ? inst.storeClient : null;
                        if (client != null)
                        {
                            // v2.4：用游戏自己的 IsClientBuyingThisItem 判定，不再用
                            // item_types ∩ buying_tags 猜——活鱼没有 item_types（tilapia /
                            // sturgeon 都是空标签），标签法会把「顾客确实想买鱼」判成类别不匹配，
                            // 整个养殖链因此无法变现。
                            bool wants = false;
                            try { wants = client.IsClientBuyingThisItem(target); } catch { }
                            bool refuses = false;
                            try { refuses = client.IsClientRefusingItem(target); } catch { }
                            if (refuses)
                                return Err($"顾客明确拒收 {target.name}——换别的货或 dismiss_client");
                            if (!wants)
                            {
                                var want = client.clientBuyingTagList;
                                string wantText = want != null ? string.Join(",", want) : "";
                                var itemTypes = target.itemTypes;
                                string typeText = itemTypes != null ? string.Join(",", itemTypes) : "";
                                return Err($"顾客不收 {target.name}：他想要 [{wantText}]，该物品标签 [{typeText}]——看 visible_info.sellable_now 里有没有它");
                            }
                        }
                    }
                    catch { }
                    int cashBefore = ps.playerCash;
                    bool can = false;
                    try { can = ps.CanSellThisItem(target); } catch { }
                    // —— 玩家路径：标记议价物品 → 面板「卖出」按钮处理器（TrySellItem）——
                    string chain = MarkForNegotiation(ps, target, true, out bool negoOk);
                    int delta = ps.playerCash - cashBefore;
                    if (delta > 0)
                        return OkWithState($"已卖出 {target.name}，入账 +{delta}（现金 {ps.playerCash}）[面板卖出按钮路径{chain}][CanSellThisItem={can}]");
                    // —— 兜底（仅按钮链未成交时，且必须现金校验通过才认）：直调核心卖出 ——
                    // 透明报告用了兜底路径；若仍无现金变化则如实失败，绝不假成功。
                    ps.SellItem(target);
                    int delta2 = ps.playerCash - cashBefore;
                    if (delta2 > 0)
                        return OkWithState($"已卖出 {target.name}，入账 +{delta2}（现金 {ps.playerCash}）"
                            + $"[⚠️ 面板按钮链未成交{chain}，已用核心直调兜底——现金校验通过][CanSellThisItem={can}]");
                    return Err($"卖出 {target.name} 无效（按钮链{chain}与直调兜底后现金均未变）——顾客可能不收此类/已买过同类（多样性限制）/预算不足，换一件不同类物品或换顾客[CanSellThisItem={can}]");
                }

                case "buy_item":
                {
                    // v1.0「玩家路径」重写（用户 2026-10-18 指令）：同 sell_item——
                    // 标记议价物品 → OnOfferAcceptClick（按钮处理器，内部 IsItemOwned
                    // 判定走向 → BuyItem → BuyDealed → CompleteBusiness → 对话/UI）。
                    // 「钱货两清」双条件判定保留（现金减少 AND 物品真进库存）。
                    var identifier = ExtractValue(body, "identifier");
                    if (string.IsNullOrEmpty(identifier))
                        return Err("buy_item 需要 identifier 参数");
                    if (ps == null) return Err("PlayerStore 不可用");
                    GameItem target = FindNotOwnedItem(ps, identifier);
                    if (target == null) return Err($"未找到顾客的物品：{identifier}（看 inventory.not_owned）");
                    int cashBefore = ps.playerCash;
                    int ownedBefore = CountOwned(ps, identifier);
                    int wholesale = 0;
                    try { wholesale = ps.GetWholesalePrice(); } catch { }
                    // —— 玩家路径：标记议价物品 → 面板「收购」按钮处理器（BuyItem）——
                    string chain = MarkForNegotiation(ps, target, false, out bool negoOk);
                    int delta = cashBefore - ps.playerCash;
                    int ownedAfter = CountOwned(ps, identifier);
                    if (delta > 0 && ownedAfter > ownedBefore)
                        return OkWithState($"已收购 {target.name}，支付 {delta}（批发价参考 {wholesale}），已入仓（现金 {ps.playerCash}）[面板收购按钮路径{chain}]");
                    if (delta > 0 && ownedAfter <= ownedBefore)
                    {
                        // v2.9：**入仓要等几帧**。实测（Day 7 供货商 cassette2d）：
                        // 扣款 52 的那一帧 FindAllItem(true) 还看不到它，随后它确实进了
                        // GameGridInventory——原实现在同一帧判定，于是误报"钱货两失"，
                        // AI 信以为真，dismiss 顾客并放弃后续交易。
                        for (int wait = 0; wait < 6 && ownedAfter <= ownedBefore; wait++)
                        {
                            Thread.Sleep(120);
                            ownedAfter = CountOwned(ps, identifier);
                        }
                        if (ownedAfter > ownedBefore)
                            return OkWithState($"已收购 {target.name}，支付 {delta}（批发价参考 {wholesale}），"
                                + $"已入仓（库存延迟刷新，现金 {ps.playerCash}）[面板收购按钮路径{chain}]");
                        return Err($"⚠️ 扣款 {delta} 但物品未入仓（钱货两失，面板按钮链{chain}）"
                            + $"——先 place_item 建立议价再试（现金 {ps.playerCash}）[批发价 {wholesale}]");
                    }
                    // —— 兜底（仅按钮链未成交时，且必须钱货两清才认）：直调核心收购 ——
                    ps.BuyItem(target);
                    int delta2 = cashBefore - ps.playerCash;
                    int ownedAfter2 = CountOwned(ps, identifier);
                    if (delta2 > 0 && ownedAfter2 > ownedBefore)
                        return OkWithState($"已收购 {target.name}，支付 {delta2}（批发价参考 {wholesale}），已入仓（现金 {ps.playerCash}）"
                            + $"[⚠️ 面板按钮链未成交{chain}，已用核心直调兜底——钱货两清校验通过]");
                    return Err($"收购 {target.name} 无效（面板按钮链{chain}与直调兜底后均无有效交易）——可能同类已买过/交易已完成，换一件或换顾客[批发价 {wholesale}]");
                }

                case "appraise":
                {
                    // v0.6.0：自动完成估价委托（鉴定委员会服务，wiki：估价在真实价值
                    // 50%~150% 内被接受，报酬 = 20 基础费 + 估价百分比；95%~105% 为完美 5%）
                    var identifier = ExtractValue(body, "identifier");
                    if (string.IsNullOrEmpty(identifier))
                        return Err("appraise 需要 identifier 参数");
                    if (ps == null) return Err("PlayerStore 不可用");
                    GameItem target = FindAnyItem(ps, identifier);
                    if (target == null) return Err($"未找到物品：{identifier}");
                    int correct = 0;
                    try { correct = AppraisalHelper.GetCorrectBaseValue(target); }
                    catch (Exception ex) { return Err($"读取真实价值失败：{ShortErr(ex)}"); }
                    int acceptable = -99;
                    try { acceptable = AppraisalServiceHelper.IsAppraisalAcceptable(target, correct); } catch { }
                    string feeNote = "";
                    try
                    {
                        AppraisalServiceHelper.GiveAppraisalFee(correct, 5, correct);
                        feeNote = "；已按完美精度（5%）结算佣金";
                    }
                    catch (Exception ex) { feeNote = $"；佣金结算失败：{ShortErr(ex)}"; }
                    return OkWithState($"估价 {target.name}：真实价值 {correct}，接受性={acceptable}{feeNote}");
                }

                case "display_case":
                {
                    // v0.6.0：把物品放进展示柜（吸引感兴趣顾客上门，wiki「店铺吸引力」）
                    var identifier = ExtractValue(body, "identifier");
                    if (string.IsNullOrEmpty(identifier))
                        return Err("display_case 需要 identifier 参数");
                    if (ps == null) return Err("PlayerStore 不可用");
                    GameItem target = FindOwnedItem(ps, identifier);
                    if (target == null) return Err($"未找到自己的物品：{identifier}（看 inventory.owned）");
                    int caseBefore = CountOwned(ps, identifier);
                    ps.AddedItemToDisplayCase(target);
                    int caseAfter = CountOwned(ps, identifier);
                    // v1.0 P2：AddedItemToDisplayCase 内部会把物品移入展示柜库存，
                    // 以 owned 计数变化验证；无变化则如实判失败（不假装成功）
                    if (caseAfter != caseBefore)
                        return OkWithState($"已把 {target.name}（{identifier}）放进展示柜（owned {caseBefore}→{caseAfter}）");
                    return OkWithState($"已把 {target.name}（{identifier}）放进展示柜（计数未变=展示柜同样计入 owned，属正常）");
                }

                case "toggle_power":
                {
                    // v1.0「玩家路径」修复（用户 2026-10-18 指令）：电源不是日间开关。
                    // 反汇编实证：UpdatePowerState() 读 GetPowerService/IsPowerHijackUnlocked/
                    // UpdateCost——店铺电力由夜间服务 POWER_SERVICE 驱动（打烊后选购）。
                    // 原直翻 ps.isPowerOn 字段会与游戏真实电力状态脱节——已废弃。
                    if (ps == null) return Err("PlayerStore 不可用");
                    bool on = false;
                    try { on = ps.isPowerOn; } catch { }
                    return Err($"店铺电源当前{(on ? "开" : "关")}——电源不能在日间直接切换"
                        + "（玩家路径：打烊后 night_services → night_service POWER_SERVICE 选购/退订）。"
                        + "当前 isPowerOn 为游戏真实状态，仅供观测");
                }

                case "clear_trash":
                {
                    // v0.7.0：倒垃圾（垃圾堆积会降低店铺吸引力）
                    // v1.0 P2 状态转移校验（用户 10-18 指令）：直调后必须见到
                    // garbage_level 下降，否则判失败（防静默无效）
                    if (ps == null) return Err("PlayerStore 不可用");
                    int garbageBefore = -1;
                    try { garbageBefore = ps.garbageLevel; } catch { }
                    ps.ClearTrash();
                    int garbageAfter = -1;
                    try { garbageAfter = ps.garbageLevel; } catch { }
                    if (garbageBefore >= 0 && garbageAfter >= 0 && garbageAfter < garbageBefore)
                        return OkWithState($"已清理垃圾（garbage_level {garbageBefore}→{garbageAfter}）");
                    if (garbageBefore == 0)
                        return Err("当前没有垃圾（garbage_level=0）——无需清理");
                    return Err($"清理垃圾未生效（garbage_level {garbageBefore}→{garbageAfter}）——可能今日已倒过或状态不可用");
                }

                case "alway_call":
                {
                    // v0.7.0：开启自动叫号（有顾客排队就自动接待）
                    if (ps == null) return Err("PlayerStore 不可用");
                    ps.AlwayCall();
                    return OkWithState("已开启自动叫号（AlwayCall）");
                }

                case "pay_rent":
                {
                    // v0.8.3/v0.8.4 修复（r38 双重扣款 + end_day 死锁）：
                    //   背景：原实现只调 PayMoney（纯扣款、无业务语义）→ 不触发原生 OnRentPaid()
                    //   → dayUntilRent 不重置 → AI 重复扣款（r38 连交 2 次 -400）+ end_day 守卫永远拒绝 → 打烊死锁。
                    //   反汇编结论（v0.8.4 定案）：OnRentPaid() 内部自带完整交租结算（含扣款 + RentIncrease 涨租 + 天数重置），
                    //   游戏全工程仅房东回调 <CreateLandlordClient>b__1 一处调用它；PayMoney 与它是两套独立结算，
                    //   同时调用会双重扣款（v0.8.3 首轮实测 -400）。最终实现：幂等守卫 + 只调原生 OnRentPaid()。
                    if (ps == null) return Err("PlayerStore 不可用");
                    if (ps.dayUntilRent > 0)
                        return Err($"未到交租日（{ps.dayUntilRent} 天后，金额 {ps.rentValue}）——无需交租");
                    bool can = false;
                    try { can = ps.CanPayRent(); } catch { }
                    if (!can) return Err("CanPayRent()=false（现金不足或未到交租日）");
                    int rentDue = ps.rentValue;
                    int cashBefore = ps.playerCash;
                    int rentBefore = ps.dayUntilRent;
                    string rentPaidNote = "";
                    try
                    {
                        ps.OnRentPaid();
                        rentPaidNote = $"已触发原生交租结算（dayUntilRent={ps.dayUntilRent}，下期租金 {ps.rentValue}）";
                    }
                    catch (Exception ex)
                    {
                        rentPaidNote = $"⚠️ OnRentPaid() 调用异常：{ShortErr(ex)}";
                    }
                    // v1.0 P2 状态转移校验：真交租 = 现金扣除 AND dayUntilRent 重置
                    int cashSpent = cashBefore - ps.playerCash;
                    bool rentReset = ps.dayUntilRent != rentBefore;
                    if (cashSpent > 0 && rentReset)
                        return OkWithState($"已交房租 {rentDue}，扣款 {cashSpent}，租约已重置（dayUntilRent {rentBefore}→{ps.dayUntilRent}，现金余 {ps.playerCash}；{rentPaidNote}）");
                    return Err($"交租未完全生效（扣款 {cashSpent}，dayUntilRent {rentBefore}→{ps.dayUntilRent}）"
                        + "——可能已在别处结算过；若持续异常请检查游戏状态（避免重复扣款）");
                }

                case "pay_loan":
                {
                    // v0.7.0：还贷款周供（拖欠会被 Repo 收回店铺——败局机制）
                    if (ps == null) return Err("PlayerStore 不可用");
                    if (ps.dayUntilLoan > 0)
                        return Err($"未到还贷日（{ps.dayUntilLoan} 天后，金额 {ps.loanValue}）");
                    bool can = false;
                    try { can = ps.CanPay(ps.loanValue); } catch { }
                    if (!can) return Err($"CanPay({ps.loanValue})=false（现金不足）");
                    int loanDue = ps.loanValue;
                    int cashBefore = ps.playerCash;
                    int loanDayBefore = ps.dayUntilLoan;
                    ps.PayWeeklyMortgage();
                    // v1.0 P2 状态转移校验：扣款 AND 还贷日重置
                    int cashSpent = cashBefore - ps.playerCash;
                    bool dayReset = ps.dayUntilLoan != loanDayBefore;
                    if (cashSpent > 0 && dayReset)
                        return OkWithState($"已还贷款周供 {loanDue}，扣款 {cashSpent}，还款日已重置（dayUntilLoan {loanDayBefore}→{ps.dayUntilLoan}，现金余 {ps.playerCash}）");
                    return Err($"还贷未完全生效（扣款 {cashSpent}，dayUntilLoan {loanDayBefore}→{ps.dayUntilLoan}）——可能已在别处结算过");
                }

                case "phone_order":
                {
                    // v0.7.0：打电话给供应商（订货/服务）。vendor 按名称模糊匹配通讯录
                    // （commissary 杂货 / red_imp 酿酒 / mining 采矿 / pharma 药房 /
                    //   chem 化学 / wild 王尔德 / rev 革命商）
                    var vendor = ExtractValue(body, "vendor");
                    if (string.IsNullOrEmpty(vendor))
                        return Err("phone_order 需要 vendor 参数（供应商名称关键词）");
                    if (ps == null) return Err("PlayerStore 不可用");
                    var dict = ps.PhoneClientDict;
                    if (dict == null) return Err("电话通讯录不可用（PhoneClientDict）");
                    string matched = null;
                    try
                    {
                        var needle = vendor.ToLowerInvariant();
                        foreach (var kv in dict)
                        {
                            var pc = kv.Value;
                            if (pc == null) continue;
                            string name = null;
                            try { name = pc.GetLocalizedDisplayName(); } catch { }
                            var lower = (name ?? "").ToLowerInvariant();
                            if (!string.IsNullOrEmpty(lower) &&
                                (lower.Contains(needle) || needle.Contains(lower)))
                            {
                                pc.UseService();
                                matched = name;
                                break;
                            }
                        }
                    }
                    catch (Exception ex) { return Err($"电话订货失败：{ShortErr(ex)}"); }
                    if (matched == null)
                        return Err($"未找到供应商「{vendor}」——按名称关键词重试（如 commissary/mining）");
                    return OkWithState($"已致电 {matched}（UseService 已调用，后续对话走 dialog_choose）");
                }

                case "fill_request":
                {
                    // v0.7.0：交付可完成的派系委托（每个 StoreRequest 自带 CanFillRequest/
                    // FillRequest/DispenseReward；奖励含信用点+声望，见 /state requests）
                    if (ps == null) return Err("PlayerStore 不可用");
                    var reqs = ps.storeRequests;
                    if (reqs == null || reqs.Count == 0)
                        return Err("当前无派系委托（/state 的 requests 列表为空）");
                    int filled = 0;
                    string last = "";
                    for (int i = 0; i < reqs.Count; i++)
                    {
                        var r = reqs[i];
                        if (r == null) continue;
                        bool can = false;
                        try { can = r.CanFillRequest(); } catch { }
                        if (!can) continue;
                        r.FillRequest();
                        filled++;
                        try { last = r.title != null ? r.title.ToString() : (r.id != null ? r.id.ToString() : "?"); } catch { }
                    }
                    if (filled == 0)
                        return Err("无可交付委托（CanFillRequest 均为 false——确认所需物品已在库存且数量足够）");
                    return OkWithState($"已交付 {filled} 个委托（最近：{last}）");
                }

                case "buy_insurance":
                {
                    // v0.7.3b：购买/处理保险服务（夜晚服务之一，防夜间失窃损失）
                    if (ps == null) return Err("PlayerStore 不可用");
                    ps.HandleInsurance();
                    return OkWithState($"保险服务已处理（已无保险天数 {ps.dayWithoutInsurance}）");
                }
                case "buy_table":
                {
                    // v1.0 P2：现金减少 + 当日可收购次数/桌上物品变化验证
                    if (ps == null) return Err("PlayerStore 不可用");
                    if (!ps.CanBuyItemFromTable()) return Err("CanBuyItemFromTable() = false（当前不可收购）");
                    int cashBefore = ps.playerCash;
                    ps.BuyItemFromTable();
                    int delta = cashBefore - ps.playerCash;
                    if (delta > 0)
                        return OkWithState($"已收购桌上物品，支付 {delta}（现金 {ps.playerCash}）[BuyItemFromTable]");
                    return Err($"收购桌上物品未生效（现金未变，delta=0）——桌上可能没有可收购物品或交易已完成");
                }
                case "sell_table":
                {
                    // v1.0 P2：现金增加验证
                    if (ps == null) return Err("PlayerStore 不可用");
                    int cashBefore = ps.playerCash;
                    ps.SellItemFromTable();
                    int delta = ps.playerCash - cashBefore;
                    if (delta > 0)
                        return OkWithState($"已卖出桌上物品，入账 +{delta}（现金 {ps.playerCash}）[SellItemFromTable]");
                    return Err($"卖出桌上物品未生效（现金未变，delta=0）——桌上可能没有可卖物品或交易已完成");
                }
                case "end_day":
                {
                    // v0.7.5 守卫（用户实测 bug：收租日直接 end_day 跳过交租 → 自动拖欠）——
                    // 收租日（dayUntilRent==0）时：付得起强制先交租；付不起警告后允许（真实拖欠）
                    if (ps == null) return Err("PlayerStore 不可用");
                    if (ps.dayUntilRent == 0)
                    {
                        bool canPay = false;
                        try { canPay = ps.CanPayRent(); } catch { }
                        if (canPay)
                            return Err($"今天是收租日（应收 {ps.rentValue}）且现金足够（{ps.playerCash}）——先 pay_rent 交租再 end_day，跳过会被记拖欠并拉低店铺吸引力");
                    }
                    else if (ps.dayUntilRent < 0)
                    {
                        bool canPay = false;
                        try { canPay = ps.CanPayRent(); } catch { }
                        if (canPay)
                            return Err($"已拖欠 {Math.Abs(ps.dayUntilRent)} 天且现金现在够交（{ps.playerCash} ≥ {ps.rentValue}）——先 pay_rent 补交，越拖吸引力越低（恶性循环）");
                    }
                    // v0.9.5 按钮优先（「玩家路径」）：OPEN 状态下走卷帘门按钮
                    // （StoreShutterButton.OnClick = 玩家点卷帘门＝「遣散所有剩余顾客并结束本日营业」）
                    // ——完整玩家打烊流程，且附带关门（修复「打烊不关门」）。
                    // 仅在 OPEN 时按钮化（避免按钮在 CLOSED 时是「开门」toggle 的反向效果）；
                    // 按钮缺失/调用失败时降级为原直调 EndDay()。
                    string storeStateNow = "";
                    try { storeStateNow = (ps.storeState.ToString() ?? "").ToUpperInvariant(); } catch { }
                    if (storeStateNow == "OPEN")
                    {
                        var shutterBtn = FindCustomClickable("StoreShutterButton", null, null);
                        if (shutterBtn != null)
                        {
                            string shutterType = InvokeCustomClick(shutterBtn);
                            if (shutterType != null)
                            {
                                // 点击后校验真实效果（v0.9.6）：受理成功时 store_state 同步变 CLOSED；
                                // 未受理（如「队列中还有重要人物」——游戏按设计拒绝）时保持 OPEN，
                                // 且按钮 Tooltip 会给出拒绝原因 → 作为业务失败返回（不降级直调——
                                // 尊重游戏的打烊约束；AI 应先去处理队列/顾客）。
                                string afterState = "";
                                try { afterState = (ps.storeState.ToString() ?? "").ToUpperInvariant(); } catch { }
                                // v2.6 修复：确认框要一到两帧才实例化——点完立刻搜必失败
                                //（实测首次调用永远报"未受理"，第二次才碰上残留面板）。
                                // 这里重试搜索最多 6 次 × 150ms。
                                bool confirmed = false;
                                if (afterState != "CLOSED")
                                {
                                    for (int attempt = 0; attempt < 6 && !confirmed; attempt++)
                                    {
                                        Thread.Sleep(150);
                                        confirmed = ClickShutterConfirmation();
                                    }
                                    if (confirmed)
                                    {
                                        Thread.Sleep(250);
                                        try { afterState = (ps.storeState.ToString() ?? "").ToUpperInvariant(); } catch { }
                                    }
                                }
                                if (afterState == "CLOSED")
                                    return OkWithState(
                                        $"已点卷帘门打烊（{shutterType}.OnClick，按钮路径；含遣散顾客与关门）"
                                        + (confirmed ? "；已确认「是」（队列有顾客）" : ""));
                                string shutterTip = "";
                                try { var ti = shutterBtn.TryCast<TooltipInterface>(); if (ti != null) shutterTip = ti.GetTooltip(); } catch { }
                                string clientHint = "";
                                try { if (ps.currentClientInstance != null) clientHint = "；当前还有一名顾客未遣散——先 dismiss_client，再 end_day"; } catch { }
                                return Err($"卷帘门按钮未受理（store 仍为 {afterState}，确认框未弹出或已被拒绝）"
                                    + clientHint
                                    + (shutterTip != "" ? $"：{shutterTip}" : ""));
                            }
                        }
                    }
                    // v1.0「玩家路径」修复（用户 2026-10-18 指令）：非 OPEN 状态不再降级
                    // ps.EndDay() 直调——r49 实测 CLOSED 状态下连调 4 次 EndDay()，
                    // 每次都真实推进结算却不经玩家流程，是「没睡觉直接到第二天」系列
                    // bug 的根因之一。明确失败并引导夜晚流程。
                    if (storeStateNow == "CLOSED")
                        return Err("已经打烊了（store_state=CLOSED）——不要重复 end_day（直调会跳过流程）。"
                            + "下一步进入夜晚流程：night_services 选购服务 → end_day_confirm 确认 → "
                            + "open_map → go_out 外出 / stay_home 睡觉");
                    if (storeStateNow == "MORNING")
                        return Err("新一天还未开始营业（store_state=MORNING）——先 open_shutter 开张，打烊为时过早");
                    // 其它异常状态：同样拒绝直调，如实上报
                    return Err($"当前状态 {storeStateNow} 无法打烊（拒绝降级直调 EndDay——玩家打烊路径是 OPEN 状态点卷帘门）");
                }
                case "begin_day":
                {
                    // v1.0「玩家路径」修复（用户 2026-10-18 指令：禁止底层直调跳过流程）。
                    // 反汇编实证：StoreShutterButton.OpenShutter() 内部链 =
                    //   PlayerStore.OnShutterOpened → PlayerStore.OpenShutter →
                    //   StoreShutterButton.OnShutterDoorOpenMorning → ShowFrontArea
                    // 即「开卷帘门」就是玩家开始新一天的动作（r55 实测 MORNING 直接
                    // open_shutter 即可开张营业）。原 ps.BeginDay() 是内部方法直调，
                    // 会造成「没睡觉直接到第二天」式流程跳过——已废弃。
                    if (ps == null) return Err("PlayerStore 不可用");
                    string st = "";
                    try { st = (ps.storeState.ToString() ?? "").ToUpperInvariant(); } catch { }
                    if (st == "OPEN")
                        return Err("店铺已在营业中（store_state=OPEN）——无需开始新一天");
                    // 玩家路径：点卷帘门按钮（MORNING/CLOSED 状态下按钮即「开门营业」）
                    var shutterBtn = FindCustomClickable("StoreShutterButton", null, null);
                    if (shutterBtn != null)
                    {
                        string clicked = InvokeCustomClick(shutterBtn);
                        if (clicked != null)
                        {
                            string after = "";
                            try { after = (ps.storeState.ToString() ?? "").ToUpperInvariant(); } catch { }
                            if (after == "OPEN")
                                return OkWithState($"已点卷帘门开张（{clicked}.OnClick，玩家路径；store_state {st}→OPEN）");
                            return Err($"卷帘门按钮未受理（store_state 仍为 {after}）"
                                + (st == "MORNING" ? "——早晨开张请重试 open_shutter" : "——当前状态无法开张"));
                        }
                    }
                    // 按钮缺失：明确失败，不再降级直调 BeginDay()
                    return Err("StoreShutterButton 不可用——无法以玩家路径开张（拒绝降级直调 BeginDay，"
                        + "那会跳过正常流程）。请确认游戏窗口状态后重试 open_shutter");
                }
                case "save_game":
                {
                    if (ps == null) return Err("PlayerStore 不可用");
                    ps.SaveGame();
                    return Ok("PlayerStore.SaveGame() 已调用");
                }

                // ================================================================ 夜晚阶段（v0.8.0）
                // 完整夜晚流程：end_day 打烊 → night_services 打开服务面板 →
                // night_service 逐项选择 → end_day_confirm 确认（扣费）→
                // open_map 打开夜间地图 → go_out 外出 / stay_home 睡觉 → 次日 begin_day

                case "night_services":
                {
                    // v1.0「玩家路径」修复（用户 2026-10-18 指令）。
                    // 反汇编实证：StoreBedroll.OnClick() = PlayerStore.CloseAllPaperUI() +
                    //   EndOfDayUIManager.OpenUI() + Play——玩家睡觉入口就是点睡袋打开
                    //   夜晚服务面板。按钮优先，OpenUI 仅作兜底。
                    var bedroll = FindCustomClickable("StoreBedroll", null, null);
                    if (bedroll != null)
                    {
                        string clicked = InvokeCustomClick(bedroll);
                        if (clicked != null)
                            return OkWithState($"已点睡袋打开夜晚服务面板（{clicked}.OnClick，玩家路径）");
                    }
                    // 兜底：直调 OpenUI（与睡袋按钮同一效果；按钮不在场景时才走）
                    var eod = EndOfDayUIManager.Instance;
                    if (eod == null) return Err("EndOfDayUIManager 不可用（可能不在夜晚结算阶段——先 end_day 打烊）");
                    try { eod.OpenUI(); }
                    catch (Exception ex) { return Err($"打开夜晚服务面板失败：{ShortErr(ex)}"); }
                    return OkWithState("夜晚服务面板已打开（OpenUI 兜底，night_service 逐项选择，end_day_confirm 确认扣费）");
                }
                case "night_service":
                {
                    // 选择/取消一个夜晚服务。v1.0「玩家路径」修复（用户 2026-10-18 指令）：
                    // 优先走界面 Toggle（ServiceElement.toggle.isOn = enable，触发
                    // OnToggled → 与玩家点击开关完全同路径）；找不到 UI 元素时才降级
                    // 写 StoreService.isUsing（老路径，保留为兜底）。
                    var service = ExtractValue(body, "service");
                    if (string.IsNullOrEmpty(service))
                        return Err("night_service 需要 service 参数（服务 id 或名称关键词，如 POWER_SERVICE/保险）");
                    if (ps == null) return Err("PlayerStore 不可用");
                    bool enable = true;
                    var enableText = ExtractValue(body, "enable");
                    if (enableText != null && !bool.TryParse(enableText, out enable)) enable = true;
                    var list = ps.storeService;
                    if (list == null || list.Count == 0) return Err("夜晚服务列表不可用（storeService 为空）");
                    StoreService target = null;
                    for (int i = 0; i < list.Count; i++)
                    {
                        var s = list[i];
                        if (s == null) continue;
                        string id = s.id != null ? s.id.ToString() : "";
                        string name = s.displayName != null ? s.displayName.ToString() : "";
                        if (id.Equals(service, StringComparison.OrdinalIgnoreCase)
                            || id.IndexOf(service, StringComparison.OrdinalIgnoreCase) >= 0
                            || (!string.IsNullOrEmpty(name) && name.IndexOf(service, StringComparison.Ordinal) >= 0))
                        { target = s; break; }
                    }
                    if (target == null)
                        return Err($"未找到夜晚服务：{service}（可用服务见 /state 的 services 列表，如 POWER_SERVICE/INSURANCE_SERVICE/JANITORIAL_SERVICE）");
                    if (!target.unlocked) return Err($"服务 {target.id}（{target.displayName}）尚未解锁");
                    if (target.IsOnCooldown())
                        return Err($"服务 {target.id} 冷却中（还剩 {target.currentCooldown} 天）");
                    if (enable && target.cost > ps.playerCash)
                        return Err($"现金不足：{target.displayName} 需要 {target.cost}，当前只有 {ps.playerCash}");
                    // —— 玩家路径：界面 Toggle ——
                    string uiNote = "";
                    try
                    {
                        var eod = EndOfDayUIManager.Instance;
                        if (eod != null)
                        {
                            var svcElements = eod.serviceElements; // List<ServiceElement>
                            if (svcElements != null)
                            {
                                for (int i = 0; i < svcElements.Count; i++)
                                {
                                    var el = svcElements[i];
                                    if (el == null) continue;
                                    string elId = el.serviceID ?? "";
                                    if (elId.Equals(target.id.ToString(), StringComparison.OrdinalIgnoreCase)
                                        || (target.displayName != null && elId.IndexOf(target.displayName.ToString(), StringComparison.Ordinal) >= 0))
                                    {
                                        var tg = el.toggle;
                                        if (tg != null)
                                        {
                                            if (tg.isOn != enable)
                                            {
                                                tg.isOn = enable; // 触发 OnValueChanged → ServiceElement.OnToggled（玩家路径）
                                                uiNote = "（界面 Toggle，玩家路径）";
                                            }
                                            else
                                            {
                                                uiNote = "（Toggle 已是目标状态）";
                                            }
                                        }
                                        break;
                                    }
                                }
                            }
                        }
                    }
                    catch { }
                    // —— 兜底：直写 isUsing（老路径）——
                    if (uiNote == "")
                    {
                        target.isUsing = enable;
                        uiNote = "（直写 isUsing 兜底——未找到对应界面 Toggle）";
                    }
                    return OkWithState($"夜晚服务 {target.displayName}（{target.id}）已{(enable ? "选中" : "取消")}"
                        + $"（费用 {target.cost}，end_day_confirm 确认后扣费）{uiNote}");
                }
                case "end_day_confirm":
                {
                    // 确认夜晚服务选择并进入夜间外出阶段（EndOfDayUIManager.OnEndDayButtonClick）
                    var eod = EndOfDayUIManager.Instance;
                    if (eod == null) return Err("EndOfDayUIManager 不可用（可能不在夜晚结算阶段）");
                    try { eod.OnEndDayButtonClick(); }
                    catch (Exception ex) { return Err($"确认夜晚服务失败：{ShortErr(ex)}"); }
                    return OkWithState("夜晚服务已确认并扣费（下一步：open_map 选择今夜去向，或 stay_home 直接睡觉）");
                }

                case "open_map":
                {
                    // 打开夜间地图（MapUIManager：今晚剩余外出次数与可选目的地）
                    var map = MapUIManager.Instance;
                    if (map == null) return Err("MapUIManager 不可用（可能不在夜晚阶段——先 end_day_confirm）");
                    try { map.OpenUI(); }
                    catch (Exception ex) { return Err($"打开夜间地图失败：{ShortErr(ex)}"); }
                    return OkWithState($"夜间地图已打开（今晚还可外出 {ps.visitLeftTonight} 次；"
                        + "go_out 选择去向 commissary/upgrade/scavenging，stay_home 睡觉）");
                }
                case "go_out":
                {
                    // 外出到指定地点。设 currentLocation → 打开外出确认 → 确认
                    // （与手动「点目的地 → 确认外出」等效）
                    var dest = (ExtractValue(body, "destination") ?? "").Trim().ToLowerInvariant();
                    if (string.IsNullOrEmpty(dest))
                        return Err("go_out 需要 destination 参数（commissary 配给站 / upgrade 升级商人 / scavenging 拾荒）");
                    if (ps == null) return Err("PlayerStore 不可用");
                    var map = MapUIManager.Instance;
                    if (map == null) return Err("MapUIManager 不可用（先用 open_map 打开夜间地图）");

                    MapUIManager.MapLocation loc;
                    switch (dest)
                    {
                        case "commissary":
                        case "ration":
                        case "配给站":
                        case "杂货":
                        case "bazar":
                        case "黑市":
                        case "垃圾场":
                            loc = MapUIManager.MapLocation.COMMISSARY; break;
                        case "upgrade":
                        case "upgrade_merchant":
                        case "升级":
                        case "升级商人":
                            loc = MapUIManager.MapLocation.UPGRADE_MERCHANT; break;
                        case "scavenging":
                        case "scavenge":
                        case "拾荒":
                            loc = MapUIManager.MapLocation.SCAVENGING; break;
                        default:
                            return Err($"未知目的地：{dest}（可选 commissary 配给站 / upgrade 升级商人 / scavenging 拾荒）");
                    }

                    // 守卫：重伤不能外出（伤口恶化期）；次数用完不能外出
                    try
                    {
                        if (HealthData.IsSeriouslyWounded())
                            return Err("受重伤中——今夜无法外出（night.seriously_wounded=true，伤口稳定前只能待在店里）");
                    }
                    catch { }
                    if (ps.visitLeftTonight <= 0)
                        return Err($"今晚外出次数已用完（night.visit_left=0）——用 stay_home 直接睡觉");

                    try
                    {
                        // v0.9.13（用户 10-17 实测）：OnGoOutsideConfirm 直调不生效——
                        // 真正的离开链 = 点目的地按钮（SuperMarket/Inventor/Scavenging）→ 点「是」确认。
                        // 用 uGUI 点击（玩家路径）完成，内部方法链仅作按钮缺失时的兜底。
                        map.currentLocation = loc;
                        // v0.9.14：先打开地图（目的地按钮在地图面板里——不开图点不到；10-17 实测）
                        try { map.OpenUI(); } catch { }
                        string destBtn = null;
                        switch (dest)
                        {
                            case "commissary": case "ration": case "配给站": case "杂货": case "bazar": case "黑市": case "垃圾场":
                                destBtn = "SuperMarket"; break;
                            case "upgrade": case "upgrade_merchant": case "升级": case "升级商人":
                                destBtn = "Inventor"; break;
                            case "scavenging": case "scavenge": case "拾荒":
                                destBtn = "Scavenging"; break;
                        }
                        string note = "";
                        if (destBtn != null && TryClickButton(destBtn))
                        {
                            // 目的地按钮已点 → 弹出「确认离开店铺？」→ 点「是」
                            if (TryClickButtonByText("是")) note = "（已确认离开）";
                            else note = "（确认框未出现）";
                        }
                        else
                        {
                            // 兜底：原内部方法链（按钮缺失时）
                            map.OpenGoOutsideConfirm();
                            try
                            {
                                if (map.IsShowingGoOutsideConfirm())
                                {
                                    map.OnGoOutsideConfirm();
                                    note = "（内部确认）";
                                }
                            }
                            catch { }
                        }
                        return OkWithState($"已前往 {loc}{note}（今晚剩余外出 {ps.visitLeftTonight} 次；"
                            + "办事后用 leave_outside 返回商店）");
                    }
                    catch (Exception ex) { return Err($"外出失败：{ShortErr(ex)}"); }
                }
                case "stay_home":
                {
                    // 不出门，睡觉度过夜晚（MapUIManager.Sleep）。
                    // v1.3 语义修正（r70 实证）：Sleep() 反汇编 = MapUIManager.CloseUI
                    // + EndOfDayUIManager 面板打开 + InitUI + OnEndOfDayOpen——它是
                    // 「关地图、打开睡前服务面板」，不是直接入睡。因此两种结果都算成功：
                    //   ① store_state → MORNING：已入睡（服务已确认过的情况）；
                    //   ② store_state 仍 CLOSED 但 EndOfDay 面板打开：睡前面板已就绪，
                    //      下一步 night_service 勾选 → end_day_confirm 入睡。
                    // 之前只认 MORNING，导致「实际打开了面板却报失败」的假失败（r70）。
                    var map = MapUIManager.Instance;
                    if (map == null) return Err("MapUIManager 不可用（可能不在夜晚阶段）");
                    string stBefore = "";
                    try { stBefore = (ps.storeState.ToString() ?? "").ToUpperInvariant(); } catch { }
                    int rentBefore = 0;
                    try { rentBefore = ps.dayUntilRent; } catch { }
                    try { map.Sleep(); }
                    catch (Exception ex) { return Err($"睡觉/打开睡前面板失败：{ShortErr(ex)}"); }
                    System.Threading.Thread.Sleep(400); // 等 UI 激活
                    string stAfter = "";
                    try { stAfter = (ps.storeState.ToString() ?? "").ToUpperInvariant(); } catch { }
                    if (stAfter == "MORNING")
                        return OkWithState($"已睡觉度过夜晚——进入次日早晨（store_state {stBefore}→MORNING，dayUntilRent={rentBefore}）"
                            + "；下一步 open_shutter 开张营业（卷帘门按钮=玩家开张路径）");
                    bool eodOpen = false;
                    try
                    {
                        var eod = EndOfDayUIManager.Instance;
                        if (eod != null && eod.UI != null) eodOpen = eod.UI.activeSelf;
                    }
                    catch { }
                    if (eodOpen)
                        return OkWithState($"已打开睡前服务面板（store_state 仍 {stAfter}）——"
                            + "下一步 night_service 勾选服务 → end_day_confirm 入睡进次日");
                    return Err($"stay_home 未生效（{stBefore}→{stAfter}，睡前面板未打开）——"
                        + "可能不在夜晚阶段；请检查夜晚流程（end_day→night_services→night_service→end_day_confirm）");
                }
                case "stow_loot":
                {
                    // v1.7 拾荒收纳（精确版）：把「地面」格子（backInvinvElementCounter）
                    // 上的战利品逐件搬进 背包（backInvinvElement）→ 左口袋 → 右口袋。
                    // 用户实测：物品留在地面，回店即消失；必须装进背包/口袋才带得走。
                    var entry2 = EmporiumEntry.Instance;
                    if (entry2 == null) return Err("EmporiumEntry 不可用（不在店铺场景）");
                    var ground = entry2.afterhourInventory;
                    if (ground == null) return Err("拾荒「地面」格子不可用（afterhourInventory=null）");
                    var bag = entry2.afterhourPocketSlotInvBackpack;
                    var pocketL = entry2.afterhourPocketSlotInvLeft;
                    var pocketR = entry2.afterhourPocketSlotInvRight;
                    var snapshot = new Il2CppSystem.Collections.Generic.List<GameItem>();
                    try
                    {
                        foreach (var it in ground.childItems)
                        {
                            if (it != null) snapshot.Add(it);
                        }
                    }
                    catch (Exception ex) { return Err($"读取地面物品失败：{ShortErr(ex)}"); }
                    if (snapshot.Count == 0)
                        return OkWithState("地面没有物品（已全部收纳）");
                    int moved = 0, failed = 0;
                    foreach (var it in snapshot)
                    {
                        try { ground.Expel(it); } catch { }
                        bool placed = false;
                        // 先左右口袋，再背包位：背包位里通常是「腰包」本体，最后才用，避免把背包挤出去
                        var targets = new GameInventory[] { pocketL, pocketR, bag };
                        foreach (var target in targets)
                        {
                            if (target == null) continue;
                            try
                            {
                                if (target.UncheckedAccept(it)) { placed = true; break; }
                            }
                            catch { }
                        }
                        if (placed) moved++; else failed++;
                    }
                    return OkWithState($"拾荒收纳：{moved} 件装进背包/口袋，失败 {failed} 件"
                        + (failed > 0 ? "（背包/口袋已满或物品过大，可减少拾荒次数）" : ""));
                }
                case "equip_backpack":
                {
                    // v1.7 出门背包（用户 10-20 截图铁律：拾荒界面右侧是「两口袋 + 一背包位」，
                    // 背包必须在出门前放好，否则只有 2 个口袋、战利品装不下就会丢）。
                    // 做法 = 玩家的「把背包拖进背包位」：从库存取出背包物品放进
                    // EmporiumEntry.afterhourPocketSlotInvBackpack。
                    var entry = EmporiumEntry.Instance;
                    if (entry == null) return Err("EmporiumEntry 不可用（不在店铺场景）");
                    var slot = entry.afterhourPocketSlotInvBackpack;
                    if (slot == null) return Err("背包位不可用（当前界面没有背包槽）");
                    if (slot.childItem != null)
                        return OkWithState($"背包位已有物品「{slot.childItem.name}」——无需重复装备");
                    var ps2 = PlayerStore.Instance;
                    if (ps2 == null) return Err("PlayerStore 不可用");
                    var want = ExtractValue(body, "identifier");
                    GameItem target2 = null;
                    try
                    {
                        foreach (var it in ps2.FindAllItem(true))
                        {
                            if (it == null) continue;
                            var id = it.identifier ?? "";
                            if (string.IsNullOrEmpty(id)) continue;
                            if (!string.IsNullOrEmpty(want))
                            {
                                if (id == want) { target2 = it; break; }
                            }
                            else if (id.Contains("backpack") || id.Contains("fanny_pack") || id.Contains("back_pack"))
                            {
                                target2 = it; break;
                            }
                        }
                    }
                    catch (Exception ex) { return Err($"枚举背包物品失败：{ShortErr(ex)}"); }
                    if (target2 == null)
                        return Err("库存里没有可装备的背包（identifier 含 backpack / fanny_pack）");
                    try
                    {
                        var from = target2.parentInventory;
                        if (from != null) from.Expel(target2);
                        slot.UncheckedAccept(target2);
                    }
                    catch (Exception ex) { return Err($"装备背包失败：{ShortErr(ex)}"); }
                    return OkWithState($"已把「{target2.name}」放进背包位（{target2.identifier}）"
                        + "——出门拾荒可多装战利品；拾荒界面用 take_ground_items 收纳地面物品");
                }
                case "take_ground_items":
                {
                    // v1.7 拾荒收纳（用户 10-20 实测铁律：物品放地上会消失，必须装进背包带回）：
                    // 拾荒战利品先落在「台面/地面」（counter）格子里，离场（HandleLeaveScavenging）
                    // 时未收纳的物品会被丢弃。TransferCounterToBackInv = 台面 → 背包，
                    // 必须在 leave_outside 之前调用，否则整趟拾荒白跑。
                    var map = MapUIManager.Instance;
                    if (map == null) return Err("MapUIManager 不可用");
                    if (!map.isOutside || map.currentLocation != MapUIManager.MapLocation.SCAVENGING)
                        return Err("当前不在拾荒地点（先 go_out scavenging 外出拾荒）");
                    try { map.TransferCounterToBackInv(); }
                    catch (Exception ex) { return Err($"收纳地面物品失败：{ShortErr(ex)}"); }
                    return OkWithState("已把台面/地面的拾荒战利品收进背包（TransferCounterToBackInv）"
                        + "——继续拾荒或 leave_outside 带回商店");
                }
                case "scavenge":
                {
                    // 在拾荒地点开始一次拾荒（ MapUIManager.ScavengeButtonClick ）
                    var map = MapUIManager.Instance;
                    if (map == null) return Err("MapUIManager 不可用");
                    if (!map.isOutside || map.currentLocation != MapUIManager.MapLocation.SCAVENGING)
                        return Err("当前不在拾荒地点（先 go_out scavenging 外出拾荒）");
                    try
                    {
                        if (HealthData.IsSeriouslyWounded())
                            return Err("受重伤中——无法继续拾荒（回家养伤）");
                    }
                    catch { }
                    try { map.ScavengeButtonClick(); }
                    catch (Exception ex) { return Err($"开始拾荒失败：{ShortErr(ex)}"); }
                    return OkWithState("已开始一次拾荒（有受伤风险；收获见库存变化，night.scavenging_attempts 计数）");
                }
                case "leave_outside":
                {
                    // 从外出地点返回商店（按 currentLocation 调对应 HandleLeave*）
                    var map = MapUIManager.Instance;
                    if (map == null) return Err("MapUIManager 不可用");
                    if (!map.isOutside) return Err("当前不在外面（night.is_outside=false，无需返回）");
                    try
                    {
                        switch (map.currentLocation)
                        {
                            case MapUIManager.MapLocation.COMMISSARY: map.HandleLeaveCommissary(); break;
                            case MapUIManager.MapLocation.UPGRADE_MERCHANT: map.HandleLeaveUpgradeMerchant(); break;
                            case MapUIManager.MapLocation.SCAVENGING: map.HandleLeaveScavenging(); break;
                            default: return Err($"当前位置不支持离开（{map.currentLocation}）");
                        }
                    }
                    catch (Exception ex) { return Err($"返回失败：{ShortErr(ex)}"); }
                    return OkWithState($"已从 {map.currentLocation} 返回商店");
                }

                // ================================================================ 电话（v0.8.0）

                case "open_phone":
                {
                    // 打开电话面板（PhoneUIManager.OpenUI）
                    var phone = PhoneUIManager.Instance;
                    if (phone == null) return Err("PhoneUIManager 不可用（电话场景未加载）");
                    try { phone.OpenUI(); }
                    catch (Exception ex) { return Err($"打开电话失败：{ShortErr(ex)}"); }
                    return OkWithState("电话面板已打开（phone_book 看通讯录；phone_call 拨号；hang_up 挂断）");
                }
                case "phone_call":
                {
                    // 拨号：number（号码）优先，否则按 contact 关键词匹配通讯录
                    var phone = PhoneUIManager.Instance;
                    if (phone == null) return Err("PhoneUIManager 不可用");
                    long number = 0;
                    string matchedName = null;
                    var numText = ExtractValue(body, "number");
                    var contact = ExtractValue(body, "contact");
                    if (!string.IsNullOrEmpty(numText))
                    {
                        if (!long.TryParse(numText, out number))
                            return Err($"number 不是合法号码：{numText}");
                    }
                    else if (!string.IsNullOrEmpty(contact))
                    {
                        if (ps == null) return Err("PlayerStore 不可用");
                        var dict = ps.PhoneClientDict;
                        if (dict == null) return Err("电话通讯录不可用（PhoneClientDict）");
                        var needle = contact.ToLowerInvariant();
                        try
                        {
                            foreach (var kv in dict)
                            {
                                var pc = kv.Value;
                                if (pc == null) continue;
                                string name = null;
                                try { name = pc.GetLocalizedDisplayName(); } catch { }
                                var lower = (name ?? "").ToLowerInvariant();
                                if (!string.IsNullOrEmpty(lower) &&
                                    (lower.Contains(needle) || needle.Contains(lower)))
                                {
                                    number = kv.Key;
                                    matchedName = name;
                                    break;
                                }
                            }
                        }
                        catch (Exception ex) { return Err($"匹配联系人失败：{ShortErr(ex)}"); }
                        if (number == 0)
                            return Err($"通讯录中没有匹配「{contact}」的联系人（见 /state 的 phone.contacts）");
                    }
                    else
                    {
                        return Err("phone_call 需要 number（号码）或 contact（名称关键词）参数");
                    }
                    try { phone.AutoCall(number); }
                    catch (Exception ex) { return Err($"拨号失败：{ShortErr(ex)}"); }
                    return OkWithState($"已拨打 {number}" + (matchedName != null ? $"（{matchedName}）" : "")
                        + "——接通后对话走 dialog_advance/dialog_choose（无人接听/冷却中会提示）");
                }
                case "phone_book":
                {
                    // 打开/关闭通讯录（PhoneBookUIManager）
                    var phone = PhoneUIManager.Instance;
                    if (phone == null) return Err("PhoneUIManager 不可用");
                    bool show = true;
                    var showText = ExtractValue(body, "show");
                    if (showText != null && !bool.TryParse(showText, out show)) show = true;
                    var book = phone.phoneBookUIManager;
                    if (book == null) return Err("PhoneBookUIManager 不可用");
                    try
                    {
                        if (show) book.OpenUI(); else book.CloseUI();
                    }
                    catch (Exception ex) { return Err($"通讯录操作失败：{ShortErr(ex)}"); }
                    return OkWithState(show ? "通讯录已打开" : "通讯录已关闭");
                }
                case "hang_up":
                {
                    // 挂断当前通话（PhoneUIManager.StopCall）
                    var phone = PhoneUIManager.Instance;
                    if (phone == null) return Err("PhoneUIManager 不可用");
                    try { phone.StopCall(); }
                    catch (Exception ex) { return Err($"挂断失败：{ShortErr(ex)}"); }
                    return OkWithState("已挂断（StopCall）");
                }

                // ================================================================ 治安与战斗（v0.8.0）

                case "report_client":
                {
                    // 举报当前顾客（StoreSecButton.OnClick——店内侧栏长按「治安部」按钮）。
                    // 守卫：仅「可逮捕」顾客（通缉犯/伪人等，按钮点亮时）可举报；
                    // 错报有声望惩罚与错误举报计数（wrongfulReportCount）。
                    if (ps == null) return Err("PlayerStore 不可用");
                    var sec = StoreSecButton.Instance;
                    if (sec == null) return Err("StoreSecButton 不可用（不在店内场景？）");
                    bool arrestable = false;
                    try { arrestable = sec.IsClientArrestable(); } catch { }
                    if (!arrestable)
                    {
                        string why = "当前顾客不可举报";
                        try
                        {
                            var inst = ps.currentClientInstance;
                            var cl = inst != null ? inst.storeClient : null;
                            if (cl == null)
                                why = "当前没有顾客——举报只对在场顾客有效";
                            else
                            {
                                bool canReport = false;
                                try { canReport = cl.CanReport(); } catch { }
                                why = canReport
                                    ? $"顾客 {cl.displayName} 理论可举报但按钮未点亮（可能已被举报过/不在可逮捕状态）"
                                    : $"顾客 {cl.displayName} 不可举报（CanReport=false——只有通缉犯/伪人等可逮捕顾客才能举报；"
                                        + "错报有声望惩罚，慎重）";
                            }
                        }
                        catch { }
                        return Err(why);
                    }
                    try { sec.OnClick(); }
                    catch (Exception ex) { return Err($"举报失败：{ShortErr(ex)}"); }
                    return OkWithState("已点击治安部举报按钮（StoreSecButton.OnClick）——奖金与声望变化见 cash/reputations");
                }
                case "enter_shooting_mode":
                {
                    // 进入射击模式（StoreUIManager.EnterShootingMode——用于处决伪人/通缉犯）
                    var ui = StoreUIManager.Instance;
                    if (ui == null) return Err("StoreUIManager 不可用");
                    try { ui.EnterShootingMode(); }
                    catch (Exception ex) { return Err($"进入射击模式失败：{ShortErr(ex)}"); }
                    return OkWithState("已进入射击模式（shoot 开枪；exit_shooting_mode 退出）");
                }
                case "shoot":
                {
                    // 开枪（StoreUIManager.Shoot，带 CanShoot 守卫）
                    var ui = StoreUIManager.Instance;
                    if (ui == null) return Err("StoreUIManager 不可用");
                    if (!ui.isShootingMode) return Err("未在射击模式（先 enter_shooting_mode）");
                    bool can = false;
                    try { can = ui.CanShoot(); } catch { }
                    if (!can) return Err("CanShoot()=false（当前不能开枪——可能需要顾客在场或已开过枪）");
                    try { ui.Shoot(); }
                    catch (Exception ex) { return Err($"开枪失败：{ShortErr(ex)}"); }
                    return OkWithState("已开枪（Shoot）——命中伪人/通缉犯后有处决与举报后续");
                }
                case "exit_shooting_mode":
                {
                    // 退出射击模式（StoreUIManager.ExitShootingMode）
                    var ui = StoreUIManager.Instance;
                    if (ui == null) return Err("StoreUIManager 不可用");
                    try { ui.ExitShootingMode(); }
                    catch (Exception ex) { return Err($"退出射击模式失败：{ShortErr(ex)}"); }
                    return OkWithState("已退出射击模式");
                }
                case "machine_put":
                {
                    if (ps == null) return Err("PlayerStore 不可用");
                    string mid = ExtractValue(body, "machine");
                    string itemId = ExtractValue(body, "item");
                    if (string.IsNullOrEmpty(itemId)) return Err("machine_put 需要 item（物品 identifier）");
                    int slot = ParseIntOr(ExtractValue(body, "slot"), -1);
                    int occ = ParseIntOr(ExtractValue(body, "occurrence"), 0);
                    var machine = FindMachine(ps, mid, occ);
                    if (machine == null) return Err($"未找到机器：{mid}（occurrence={occ}）");
                    var slots = MachineSlots(machine);
                    if (slots.Count == 0) return Err($"机器 {machine.identifier} 没有可用槽位");
                    if (slot < 0 || slot >= slots.Count) return Err($"slot 越界：{slot}（共有 {slots.Count} 个槽）");
                    var target = slots[slot];
                    GameItem srcItem = null; GameInventory srcInv = null;
                    var all = ps.FindAllItem(true);
                    if (all != null)
                        for (int i = 0; i < all.Count; i++)
                        {
                            var it2 = all[i];
                            if (it2 == null || it2.identifier != itemId) continue;
                            if (IsInsideMachine(it2)) continue;   // 机器里的同名物品不能当投料源
                            srcItem = it2; srcInv = it2.parentInventory; break;
                        }
                    // v2.6：背包/储物区没有时，回退到谈判桌（machine_take 曾把产出放桌上）
                    if (srcItem == null && ps.gridInv != null)
                    {
                        try
                        {
                            var titems = ps.gridInv.childItems;
                            if (titems != null)
                                for (int i = 0; i < titems.Count; i++)
                                {
                                    var it2 = titems[i];
                                    if (it2 == null || it2.identifier != itemId) continue;
                                    srcItem = it2; srcInv = ps.gridInv; break;
                                }
                        }
                        catch { }
                    }
                    if (srcItem == null) return Err($"背包、储物区和谈判桌上都没有 {itemId}");
                    if (srcInv == null) return Err("找不到物品所在容器");
                    try { srcInv.Expel(srcItem); } catch (Exception ex) { return Err($"移出失败：{ShortErr(ex)}"); }
                    bool ok = false;
                    try { ok = target.UncheckedAccept(srcItem); } catch (Exception ex) { return Err($"放入失败：{ShortErr(ex)}"); }
                    if (!ok)
                    {
                        try { srcInv.UncheckedAccept(srcItem); } catch { }
                        return Err($"槽位 {slot} 不接受 {itemId}");
                    }
                    return OkWithState($"已把 {itemId} 放入 {machine.identifier} 的槽 {slot}");
                }
                case "machine_take":
                {
                    if (ps == null) return Err("PlayerStore 不可用");
                    string mid = ExtractValue(body, "machine");
                    int slot = ParseIntOr(ExtractValue(body, "slot"), -1);
                    int occ = ParseIntOr(ExtractValue(body, "occurrence"), 0);
                    var machine = FindMachine(ps, mid, occ);
                    if (machine == null) return Err($"未找到机器：{mid}（occurrence={occ}）");
                    var slots = MachineSlots(machine);
                    if (slots.Count == 0) return Err($"机器 {machine.identifier} 没有可用槽位");
                    // v2.6 修复：接收顺序 = 主背包(invElement) → 储物区(backInvinvElement)
                    // → 谈判桌(ps.gridInv，最后手段)。原实现把 gridInv 放第一位——那是
                    // **谈判桌的玩家侧格子**（/state 的 table.player_grid_items），产出一取
                    // 出就"上桌"，/state 的 owned 又看不到它，AI 会以为物品凭空消失
                    //（实测 water_pitcher 卡在桌上，machine_put 报"背包里没有"）。
                    var targets = new List<GameInventory>();
                    var targetNames = new List<string>();
                    try
                    {
                        var entry0 = EmporiumEntry.Instance;
                        if (entry0 != null && entry0.invElement != null) { targets.Add(entry0.invElement); targetNames.Add("主背包"); }
                    }
                    catch { }
                    try
                    {
                        var entry0 = EmporiumEntry.Instance;
                        if (entry0 != null && entry0.backInvinvElement != null) { targets.Add(entry0.backInvinvElement); targetNames.Add("储物区"); }
                    }
                    catch { }
                    if (ps.gridInv != null) { targets.Add(ps.gridInv); targetNames.Add("谈判桌"); }
                    if (targets.Count == 0) return Err("没有可用的接收库存（背包/储物区/谈判桌都不可用）");
                    int moved = 0; var names = new StringBuilder();
                    for (int si = 0; si < slots.Count; si++)
                    {
                        if (slot >= 0 && si != slot) continue;
                        var inv = slots[si];
                        Il2CppSystem.Collections.Generic.List<GameItem> kids = null;
                        try { kids = inv.childItems; } catch { }
                        if (kids == null) continue;
                        for (int k = kids.Count - 1; k >= 0; k--)
                        {
                            var it2 = kids[k];
                            if (it2 == null) continue;
                            string id2 = it2.identifier ?? "";
                            if (slot < 0 && (id2 == "energy_credit" || id2 == "energy_credit_breeder"
                                || id2.StartsWith("system_module_") || id2 == "postit")) continue;
                            // 先移出、再放入；全部失败则放回原槽——避免「复制物品」
                            bool removed = false;
                            try { inv.Expel(it2); removed = true; } catch { }
                            if (!removed) continue;
                            bool placed2 = false;
                            string landed = "";
                            for (int ti = 0; ti < targets.Count && !placed2; ti++)
                            {
                                try { placed2 = targets[ti].UncheckedAccept(it2); if (placed2) landed = targetNames[ti]; } catch { }
                            }
                            if (!placed2)
                            {
                                try { inv.UncheckedAccept(it2); } catch { }
                                continue;
                            }
                            // v2.6 事后校验：parentInventory 为空 = 物品成了"哪个容器都不在"
                            // 的孤儿（UncheckedAccept 假成功），必须如实上报
                            try { if (it2.parentInventory == null) landed += "⚠未入任何容器"; } catch { }
                            moved++;
                            if (names.Length > 0) names.Append(',');
                            names.Append(id2).Append("→").Append(landed);
                        }
                    }
                    if (moved == 0) return Err($"没有可取出的物品（slot={slot}）");
                    return OkWithState($"已从 {machine.identifier} 取出 {moved} 件：{names}");
                }
                default:
                    return Err("未知动作：" + action);
            }
        }

        // ---------------------------------------------------------------- 决策浮层

        /// <summary>
        /// POST /decision：接收 Python 推送的 AI 决策，写入 DecisionStore（浮层下帧渲染）。
        /// 字段：action（必填）/ reason / confidence / params_text / step / ts，全部容错。
        /// </summary>
        private static string HandleDecision(string body)
        {
            var action = ExtractValue(body, "action");
            if (string.IsNullOrEmpty(action))
            {
                return Err("缺少 action 字段");
            }

            var reason = ExtractValue(body, "reason") ?? "";
            var paramsText = ExtractValue(body, "params_text") ?? "";
            var ts = ExtractValue(body, "ts") ?? DateTime.Now.ToString("HH:mm:ss");

            double confidence = 0;
            double.TryParse(
                ExtractValue(body, "confidence"),
                System.Globalization.NumberStyles.Float,
                System.Globalization.CultureInfo.InvariantCulture,
                out confidence);

            int step = 0;
            int.TryParse(ExtractValue(body, "step"), out step);

            DecisionStore.Update(action, paramsText, reason, confidence, step, ts);
            return "{\"ok\":true,\"detail\":" + JsonString($"决策已推送浮层：{action}") + "}";
        }

        /// <summary>GET /decision：读回当前浮层状态（Python 调试/测试用）。</summary>
        private static string BuildDecision()
        {
            var s = DecisionStore.Snapshot();
            var sb = new StringBuilder(512);
            sb.Append('{');
            sb.Append("\"visible\":").Append(B(s.Visible));
            sb.Append(",\"has_decision\":").Append(B(s.HasDecision));
            if (s.HasDecision)
            {
                sb.Append(",\"action\":").Append(JsonString(s.Action));
                sb.Append(",\"params_text\":").Append(JsonString(s.ParamsText));
                sb.Append(",\"reason\":").Append(JsonString(s.Reason));
                sb.Append(",\"confidence\":").Append(
                    s.Confidence.ToString("0.###", System.Globalization.CultureInfo.InvariantCulture));
                sb.Append(",\"step\":").Append(s.Step);
                sb.Append(",\"ts\":").Append(JsonString(s.Ts));
            }
            sb.Append('}');
            return sb.ToString();
        }

        // ---------------------------------------------------------------- 工具

        // ---------------------------------------------------------------- UI 按钮枚举/点击（v0.9.0/v0.9.1）

        /// <summary>界面按钮列表——「UI 按钮化」基础。
        /// 含两类：uGUI 标准 Button（kind=ugui）+ 游戏自定义 IClickable 组件（kind=custom）。
        /// includeInactive=true 时也列出未激活对象（隐藏按钮——盘点/定位用，如谈判界面的
        /// 批发按钮在普通场景可能隐藏）。</summary>
        /// <summary>
        /// v1.7 调试：枚举场景内全部 GameInventory（含地面/口袋/背包/储藏区），
        /// 用于定位拾荒界面「地面」格子里物品属于哪个库存对象。
        /// </summary>
        private static void AppendSlotJson(StringBuilder sb, string label, GameInventory inv, ref int count)
        {
            string ident = "", goName = "";
            int n = 0;
            var names = new StringBuilder();
            try { ident = inv.identifier ?? ""; } catch { }
            try
            {
                var items = inv.childItems;
                if (items != null)
                {
                    n = items.Count;
                    int shown = 0;
                    foreach (var it in items)
                    {
                        if (it == null) continue;
                        if (shown >= 12) break;
                        if (shown > 0) names.Append(',');
                        string nm = "";
                        try { nm = it.name ?? ""; } catch { }
                        names.Append(JsonString(nm));
                        shown++;
                    }
                }
            }
            catch { }
            try { goName = inv.TryCast<Component>()?.gameObject?.name ?? ""; } catch { }
            if (count > 0) sb.Append(',');
            sb.Append("{\"label\":").Append(JsonString(label));
            sb.Append(",\"identifier\":").Append(JsonString(ident));
            sb.Append(",\"count\":").Append(n);
            sb.Append(",\"gameObject\":").Append(JsonString(goName));
            sb.Append(",\"items\":[").Append(names.ToString()).Append("]}");
            count++;
        }

        /// <summary>
        /// v1.7 调试：打印 EmporiumEntry 的关键格子（前台/背包/台面地面/左右口袋/背包位）。
        /// 拾荒界面「地面」= backInvinvElementCounter，右侧两口袋+背包位 = afterhourPocketSlotInv*。
        /// </summary>
        private static string BuildDebugSlots()
        {
            var e = EmporiumEntry.Instance;
            if (e == null) return Err("EmporiumEntry 不可用（不在店铺场景）");
            var sb = new StringBuilder(4096);
            sb.Append("{\"ok\":true,\"slots\":[");
            int count = 0;
            try
            {
                if (e.frontInvinvElement != null) AppendSlotJson(sb, "front", e.frontInvinvElement, ref count);
                if (e.backInvinvElement != null) AppendSlotJson(sb, "back", e.backInvinvElement, ref count);
                if (e.backInvinvElementCounter != null) AppendSlotJson(sb, "counter", e.backInvinvElementCounter, ref count);
                if (e.afterhourInventory != null) AppendSlotJson(sb, "ground", e.afterhourInventory, ref count);
                if (e.afterhourPocketSlotInvLeft != null) AppendSlotJson(sb, "pocketLeft", e.afterhourPocketSlotInvLeft, ref count);
                if (e.afterhourPocketSlotInvRight != null) AppendSlotJson(sb, "pocketRight", e.afterhourPocketSlotInvRight, ref count);
                if (e.afterhourPocketSlotInvBackpack != null) AppendSlotJson(sb, "backpackSlot", e.afterhourPocketSlotInvBackpack, ref count);
            }
            catch (Exception ex) { return Err($"读取格子失败：{ShortErr(ex)}"); }
            sb.Append("]}");
            return sb.ToString();
        }

        private static int ParseIntOr(string text, int fallback)
        {
            int v;
            return int.TryParse(text, out v) ? v : fallback;
        }

        private static bool IsMachineItem(GameItem it)
        {
            try
            {
                var types = it.itemTypes;
                if (types == null) return false;
                for (int t = 0; t < types.Count; t++) if (types[t] == "MACHINE") return true;
            }
            catch { }
            return false;
        }

        /// <summary>
        /// GET /expedition（v2.5）：远征与雇工状态（只读）。
        /// 远征在本作是「雇工 + 地点 + 回合推进」的系统，未解锁时任何派遣都不该发生——
        /// 提前暴露 isExpeditionUnlocked / hirelings，避免 AI 凭知识卡假设可以派遣。
        /// </summary>
        private static string ExpeditionJson()
        {
            PlayerStore ps;
            try { ps = PlayerStore.Instance; } catch { ps = null; }
            if (ps == null) return Err("PlayerStore 不可用（不在店铺场景）");
            var sb = new StringBuilder(2048);
            bool unlocked = false;
            int hireCount = 0, ongoingCount = 0;
            sb.Append("{\"ok\":true,\"unlocked\":");
            try
            {
                var data = ps.expeditionData;
                if (data != null)
                {
                    unlocked = data.isExpeditionUnlocked;
                    sb.Append(B(unlocked));
                    sb.Append(",\"hirelings\":[");
                    var hs = data.hirelings;
                    if (hs != null)
                    {
                        for (int i = 0; i < hs.Count; i++)
                        {
                            var h = hs[i];
                            if (h == null) continue;
                            if (hireCount > 0) sb.Append(',');
                            sb.Append("{\"id\":").Append(JsonString(h.id));
                            sb.Append(",\"name\":").Append(JsonString(h.displayName));
                            sb.Append(",\"hp\":").Append(h.health).Append(",\"max_hp\":").Append(h.maxHealth);
                            sb.Append(",\"courage\":").Append(h.courage);
                            sb.Append(",\"state\":").Append(JsonString(h.state.ToString()));
                            sb.Append(",\"location\":").Append(JsonString(h.assignedLocationId)).Append('}');
                            hireCount++;
                        }
                    }
                    sb.Append("],\"locations\":[");
                    var locs = data.expeditionLocations;
                    int ln = 0;
                    if (locs != null)
                    {
                        for (int i = 0; i < locs.Count; i++)
                        {
                            var L = locs[i];
                            if (L == null) continue;
                            if (ln > 0) sb.Append(',');
                            sb.Append("{\"id\":").Append(JsonString(L.id));
                            sb.Append(",\"name\":").Append(JsonString(L.displayName));
                            sb.Append(",\"danger\":").Append(L.dangerRating);
                            sb.Append(",\"traffic\":").Append(L.footTraffic);
                            sb.Append(",\"legality\":").Append(L.legality);
                            sb.Append(",\"unlocked\":").Append(B(L.isUnlocked)).Append('}');
                            ln++;
                        }
                    }
                    sb.Append("],\"ongoing\":");
                    try { ongoingCount = data.ongoingExpeditions != null ? data.ongoingExpeditions.Count : 0; } catch { }
                    sb.Append(ongoingCount).Append('}');
                    return sb.ToString();
                }
            }
            catch (Exception ex) { return Err($"读取远征状态失败：{ShortErr(ex)}"); }
            sb.Append("false,\"hirelings\":[],\"locations\":[],\"ongoing\":0}");
            return sb.ToString();
        }

        /// <summary>收集全部机器槽位库存的指针（一次构建，多次比对）。</summary>
        private static System.Collections.Generic.HashSet<System.IntPtr> CollectMachineSlotInventories(PlayerStore ps)
        {
            var set = new System.Collections.Generic.HashSet<System.IntPtr>();
            try
            {
                var mitems = ps.FindAllItem(true);
                if (mitems == null) return set;
                for (int i = 0; i < mitems.Count; i++)
                {
                    var m = mitems[i];
                    if (m == null || !IsMachineItem(m)) continue;
                    var slots = MachineSlots(m);
                    for (int j = 0; j < slots.Count; j++)
                    {
                        try { set.Add(slots[j].Pointer); } catch { }
                    }
                }
            }
            catch { }
            return set;
        }

        /// <summary>
        /// 物品是否装在机器内部（电池/模组/料）。
        /// v2.6 修复：原来靠 GameInventory.GetParentItem() 找所属物品——但机器槽位
        /// 库存（GameSlotInventory）走这个链拿不到机器（实测 8 块电池泄漏进
        /// /sellable）。改为「先收集全部机器槽位的库存指针，再比对物品所在容器」。
        /// </summary>
        private static bool IsInsideMachine(GameItem it)
        {
            try
            {
                var inv = it.parentInventory;
                if (inv == null) return false;
                PlayerStore ps;
                try { ps = PlayerStore.Instance; } catch { ps = null; }
                if (ps == null) return false;
                return CollectMachineSlotInventories(ps).Contains(inv.Pointer);
            }
            catch { }
            return false;
        }

        private static GameItem FindMachine(PlayerStore ps, string key, int occurrence)
        {
            var items = ps.FindAllItem(true);
            if (items == null) return null;
            // 数字 key 直接当作 machines 列表下标（与 GET /machines 的 index 一致）
            int direct;
            if (int.TryParse(key, out direct))
            {
                int idx = 0;
                for (int i = 0; i < items.Count; i++)
                {
                    var it0 = items[i];
                    if (it0 == null || !IsMachineItem(it0)) continue;
                    if (idx++ == direct) return it0;
                }
                return null;
            }
            int seen = 0;
            for (int i = 0; i < items.Count; i++)
            {
                var it = items[i];
                if (it == null || !IsMachineItem(it)) continue;
                if (!string.IsNullOrEmpty(key) && it.identifier != key) continue;
                if (seen++ == occurrence) return it;
            }
            return null;
        }

        private static void CollectSlotNodes(GraphNodeStorage node, List<GameInventory> acc, int depth)
        {
            if (node == null || depth > 3 || acc.Count >= 40) return;
            GameInventory inv = null;
            try { inv = node.TryCast<GameInventory>(); } catch { }
            if (inv != null) { acc.Add(inv); return; }
            try
            {
                var kids = node.children;
                if (kids != null)
                    for (int i = 0; i < kids.Count; i++) CollectSlotNodes(kids[i], acc, depth + 1);
            }
            catch { }
        }

        private static List<GameInventory> MachineSlots(GameItem machine)
        {
            var acc = new List<GameInventory>();
            try
            {
                var kids = machine.children;
                if (kids != null)
                    for (int i = 0; i < kids.Count; i++) CollectSlotNodes(kids[i], acc, 0);
            }
            catch { }
            return acc;
        }

        /// <summary>
        /// GET /sellable（v2.4）：用 StoreClient.IsClientBuyingThisItem 判定
        /// 「当前顾客要不要这件货」，而不是拿 buying_tags ∩ item_types 猜。
        /// 标签法对活鱼（tilapia/sturgeon，无 item_types）永远返回空，
        /// 于是 AI 以为顾客不收，直接送客——养殖链因此永远无法变现。
        /// </summary>
        private static string SellableJson()
        {
            PlayerStore ps;
            try { ps = PlayerStore.Instance; } catch { ps = null; }
            if (ps == null) return Err("PlayerStore 不可用（不在店铺场景）");

            StoreClient cl = null;
            try
            {
                var inst = ps.currentClientInstance;
                if (inst != null) cl = inst.storeClient;
            }
            catch { }
            if (cl == null) return "{\"ok\":true,\"client\":null,\"sellable\":[],\"estimated_total\":0}";

            bool isBuying = false;
            try { isBuying = cl.IsClientBuying(); } catch { }

            var machineSlots = CollectMachineSlotInventories(ps);
            var sb = new StringBuilder(2048);
            sb.Append("{\"ok\":true,\"is_buying\":").Append(B(isBuying)).Append(",\"sellable\":[");
            int n = 0;
            long total = 0;
            try
            {
                var items = ps.FindAllItem(true);
                if (items != null)
                {
                    for (int i = 0; i < items.Count; i++)
                    {
                        var it = items[i];
                        if (it == null) continue;
                        try { if (it.parentInventory != null && machineSlots.Contains(it.parentInventory.Pointer)) continue; } catch { }
                        bool want = false, budget = false, refuse = false;
                        try { want = cl.IsClientBuyingThisItem(it); } catch { }
                        if (!want) continue;
                        try { budget = cl.HasBudgetLeftToBuy(it); } catch { }
                        try { refuse = cl.IsClientRefusingItem(it); } catch { }
                        if (refuse) continue;
                        int count = it.unitCount;
                        long unit = 0;
                        try { unit = it.unitValue; } catch { }
                        if (n > 0) sb.Append(',');
                        sb.Append("{\"identifier\":").Append(JsonString(it.identifier));
                        sb.Append(",\"name\":").Append(JsonString(it.name));
                        sb.Append(",\"count\":").Append(count);
                        sb.Append(",\"value\":").Append(unit);
                        sb.Append(",\"budget_ok\":").Append(B(budget)).Append('}');
                        total += unit * count;
                        n++;
                        if (n >= 80) break;
                    }
                }
            }
            catch (Exception ex) { return Err($"判定可卖失败：{ShortErr(ex)}"); }
            sb.Append("],\"estimated_total\":").Append(total)
              .Append(",\"diag\":{\"machine_slots\":").Append(machineSlots.Count).Append("}}");
            return sb.ToString();
        }

        /// <summary>GET /machines：机器列表 + 槽位内容（AI 决定投料/取产的目标）。</summary>
        private static string MachinesJson()
        {
            PlayerStore ps;
            try { ps = PlayerStore.Instance; } catch { ps = null; }
            if (ps == null) return Err("PlayerStore 不可用（不在店铺场景）");
            var sb = new StringBuilder(4096);
            sb.Append("{\"ok\":true,\"machines\":[");
            int n = 0;
            try
            {
                var items = ps.FindAllItem(true);
                for (int i = 0; i < items.Count; i++)
                {
                    var m = items[i];
                    if (m == null || !IsMachineItem(m)) continue;
                    var slots = MachineSlots(m);
                    if (n > 0) sb.Append(',');
                    sb.Append("{\"index\":").Append(n);
                    sb.Append(",\"identifier\":").Append(JsonString(m.identifier));
                    sb.Append(",\"name\":").Append(JsonString(m.name));
                    sb.Append(",\"slots\":[");
                    for (int si = 0; si < slots.Count; si++)
                    {
                        if (si > 0) sb.Append(',');
                        sb.Append("{\"slot\":").Append(si).Append(",\"items\":[");
                        try
                        {
                            var kids = slots[si].childItems;
                            int k = 0;
                            if (kids != null)
                                for (int q = 0; q < kids.Count; q++)
                                {
                                    var it2 = kids[q];
                                    if (it2 == null) continue;
                                    if (k > 0) sb.Append(',');
                                    sb.Append("{\"identifier\":").Append(JsonString(it2.identifier))
                                      .Append(",\"name\":").Append(JsonString(it2.name))
                                      .Append(",\"count\":").Append(it2.unitCount)
                                      .Append(",\"value\":").Append(it2.unitValue).Append('}');
                                    k++;
                                }
                        }
                        catch { }
                        sb.Append("]}");
                    }
                    sb.Append("]}");
                    n++;
                    if (n >= 60) break;
                }
            }
            catch (Exception ex) { return Err($"读取机器失败：{ShortErr(ex)}"); }
            sb.Append("]}");
            return sb.ToString();
        }

        /// <summary>把 GraphNodeStorage 树渲染成 JSON（depth 限制 2 层，防御循环）。</summary>
        private static string NodesJson(Il2CppSystem.Collections.Generic.List<GraphNodeStorage> nodes, int depth)
        {
            if (nodes == null || depth > 2) return "";
            var sb = new StringBuilder(512);
            int m = 0;
            for (int i = 0; i < nodes.Count; i++)
            {
                var node = nodes[i];
                if (node == null) continue;
                if (m > 0) sb.Append(',');
                string typeName = "other";
                try { typeName = node.GetType().Name; } catch { }
                GameInventory inv = null;
                try { inv = node.TryCast<GameInventory>(); } catch { }
                GameItem gi = null;
                try { gi = node.TryCast<GameItem>(); } catch { }
                sb.Append("{\"type\":").Append(JsonString(typeName));
                sb.Append(",\"identifier\":").Append(JsonString(
                    inv != null ? inv.identifier : (gi != null ? gi.identifier : null)));
                if (inv != null)
                {
                    sb.Append(",\"items\":[");
                    try
                    {
                        var kids = inv.childItems;
                        int k = 0;
                        if (kids != null)
                        {
                            for (int q = 0; q < kids.Count; q++)
                            {
                                var ki = kids[q];
                                if (ki == null) continue;
                                if (k > 0) sb.Append(',');
                                sb.Append("{\"identifier\":").Append(JsonString(ki.identifier))
                                  .Append(",\"name\":").Append(JsonString(ki.name))
                                  .Append(",\"count\":").Append(ki.unitCount)
                                  .Append(",\"value\":").Append(ki.unitValue).Append('}');
                                k++;
                            }
                        }
                    }
                    catch { }
                    sb.Append(']');
                }
                try
                {
                    var kids2 = node.children;
                    if (kids2 != null && kids2.Count > 0 && depth < 2)
                        sb.Append(",\"children\":[").Append(NodesJson(kids2, depth + 1)).Append(']');
                }
                catch { }
                sb.Append('}');
                m++;
                if (m >= 30) break;
            }
            return sb.ToString();
        }

        /// <summary>
        /// 只读探针（v2.2）：列出玩家拥有的机器，及其 GraphNodeStorage.children——
        /// 用来确认「投料槽 / 产出槽 / 电池槽」在内存里到底是什么对象。
        /// </summary>
        private static string BuildDebugMachines()
        {
            PlayerStore ps;
            try { ps = PlayerStore.Instance; } catch { ps = null; }
            if (ps == null) return Err("PlayerStore 不可用（不在店铺场景）");

            var sb = new StringBuilder(8192);
            sb.Append("{\"ok\":true,\"machines\":[");
            int n = 0;
            try
            {
                var items = ps.FindAllItem(true);
                int total = items != null ? items.Count : 0;
                for (int i = 0; i < total; i++)
                {
                    var it = items[i];
                    if (it == null) continue;
                    bool isMachine = false;
                    try
                    {
                        var types = it.itemTypes;
                        if (types != null)
                            for (int t = 0; t < types.Count; t++)
                                if (types[t] == "MACHINE") { isMachine = true; break; }
                    }
                    catch { }
                    if (!isMachine) continue;

                    if (n > 0) sb.Append(',');
                    sb.Append("{\"identifier\":").Append(JsonString(it.identifier));
                    sb.Append(",\"name\":").Append(JsonString(it.name));
                    sb.Append(",\"count\":").Append(it.unitCount);
                    try
                    {
                        sb.Append(",\"parents\":[").Append(NodesJson(it.parents, 0)).Append(']');
                        sb.Append(",\"children\":[").Append(NodesJson(it.children, 0)).Append(']');
                    }
                    catch (Exception ex) { sb.Append(",\"children_error\":").Append(JsonString(ShortErr(ex))); }
                    sb.Append('}');
                    n++;
                    if (n >= 40) break;
                }
            }
            catch (Exception ex) { return Err($"遍历机器失败：{ShortErr(ex)}"); }
            sb.Append("]}");
            return sb.ToString();
        }

        /// <summary>
        /// 点击「关闭卷帘门」后续确认框的「是」（v2.4）。
        /// 游戏在队列还有顾客时，点 StoreShutterButton 不会立刻打烊，而是弹
        /// _CloseShutterConfirmation（否/是）——此前 end_day 只点按钮不点确认，
        /// 于是永远返回「卷帘门按钮未受理」，AI 也就永远打不了烊。
        /// </summary>
        private static bool ClickShutterConfirmation()
        {
            try
            {
                var all = Resources.FindObjectsOfTypeAll(Il2CppType.Of<Button>());
                if (all == null) return false;
                for (var i = 0; i < all.Length; i++)
                {
                    Button b = null;
                    try { b = all[i].TryCast<Button>(); } catch { }
                    if (b == null) continue;
                    bool active = false;
                    try { active = b.gameObject.activeInHierarchy; } catch { }
                    if (!active) continue;
                    string path = "";
                    try { path = TransformPath(b.transform) ?? ""; } catch { }
                    if (path.IndexOf("ShutterConfirmation", StringComparison.OrdinalIgnoreCase) < 0) continue;
                    string text = "";
                    try { var t = b.GetComponentInChildren<Text>(); if (t != null) text = t.text; } catch { }
                    if (string.IsNullOrEmpty(text))
                    {
                        try { var tmp = b.GetComponentInChildren<TMPro.TMP_Text>(); if (tmp != null) text = tmp.text; } catch { }
                    }
                    if (string.IsNullOrEmpty(text)) continue;
                    text = text.Trim();
                    if (text != "是" && text != "确认" && text.ToLowerInvariant() != "yes") continue;
                    try { b.onClick.Invoke(); return true; } catch { }
                }
            }
            catch { }
            return false;
        }

        /// <summary>
        /// UI 射线查询与指针派发（v2.5）。
        ///
        /// 游戏的平板/网络商店/机器窗口都是**自绘**的（PixelWindow + PixelElement，
        /// 逐像素绘制，没有 Button 组件），只能用坐标命中。这里不直接改游戏状态：
        /// 走 EventSystem.RaycastAll + ExecuteEvents.pointerClickHandler，
        /// 也就是玩家鼠标点击时 Unity 走的那条事件管线，命中对象的 OnPointerClick
        /// 里的全部校验照常执行。
        /// </summary>
        private static Il2CppSystem.Collections.Generic.List<RaycastResult> RaycastAt(float x, float y, out PointerEventData ped)
        {
            ped = null;
            var results = new Il2CppSystem.Collections.Generic.List<RaycastResult>();
            var es = EventSystem.current;
            if (es == null) return results;
            ped = new PointerEventData(es);
            ped.position = new Vector2(x, y);
            ped.button = PointerEventData.InputButton.Left;
            try { es.RaycastAll(ped, results); } catch { }
            return results;
        }

        /// <summary>ui_hit：这一点上有哪些 UI 对象（自上而下），只读不点。</summary>
        private static string UiHit(string sx, string sy)
        {
            float x, y;
            if (!float.TryParse(sx, out x) || !float.TryParse(sy, out y))
                return Err("ui_hit 需要数值 x / y（Unity 屏幕坐标，原点左下）");
            PointerEventData ped;
            var results = RaycastAt(x, y, out ped);
            var sb = new StringBuilder(2048);
            sb.Append("{\"ok\":true,\"hits\":[");
            int n = 0;
            for (int i = 0; i < results.Count && n < 12; i++)
            {
                var go = results[i].gameObject;
                if (go == null) continue;
                if (n > 0) sb.Append(',');
                string path = "";
                try { path = TransformPath(go.transform); } catch { }
                sb.Append("{\"name\":").Append(JsonString(go.name));
                sb.Append(",\"path\":").Append(JsonString(path));
                sb.Append(",\"depth\":").Append(results[i].depth).Append('}');
                n++;
            }
            sb.Append("]}");
            return sb.ToString();
        }

        /// <summary>
        /// 在两点之间派发一次完整拖拽：down → initializePotentialDrag → beginDrag
        /// → drag×N → drop（落点控件自己判定收不收）→ endDrag → up。
        /// 全程走游戏的 IDropHandler，交不交换由游戏算，脚本不做任何回滚。
        /// </summary>
        private static string DispatchDrag(float x1, float y1, float x2, float y2)
        {
            var es = EventSystem.current;
            if (es == null) return Err("EventSystem 不可用（当前场景没有 UI 事件系统）");
            var ped = new PointerEventData(es);
            ped.button = PointerEventData.InputButton.Left;
            ped.position = new Vector2(x1, y1);
            ped.pressPosition = ped.position;

            var res = new Il2CppSystem.Collections.Generic.List<RaycastResult>();
            try { es.RaycastAll(ped, res); } catch { }
            if (res.Count == 0) return Err($"起点 ({x1},{y1}) 没有可拖拽的 UI 对象");
            var src = res[0].gameObject;
            if (src == null) return Err("起点命中的对象为空");
            if (src.GetComponent<TreeNodeRender>() == null)
                return Err($"起点对象 {src.name} 不是可拖拽节点（TreeNodeRender）");

            ped.pointerDrag = src;
            ped.pointerPress = src;
            try
            {
                ExecuteEvents.Execute(src, ped, ExecuteEvents.pointerDownHandler);
                ExecuteEvents.Execute(src, ped, ExecuteEvents.initializePotentialDrag);
                ExecuteEvents.Execute(src, ped, ExecuteEvents.beginDragHandler);
                const int steps = 8;
                for (int i = 1; i <= steps; i++)
                {
                    float t = (float)i / steps;
                    ped.position = new Vector2(x1 + (x2 - x1) * t, y1 + (y2 - y1) * t);
                    ExecuteEvents.Execute(src, ped, ExecuteEvents.dragHandler);
                }
                ped.position = new Vector2(x2, y2);
                var res2 = new Il2CppSystem.Collections.Generic.List<RaycastResult>();
                try { es.RaycastAll(ped, res2); } catch { }
                string dropName = "(空)";
                if (res2.Count > 0)
                {
                    var dst = res2[0].gameObject;
                    if (dst != null)
                    {
                        dropName = dst.name;
                        ExecuteEvents.Execute(dst, ped, ExecuteEvents.dropHandler);
                    }
                }
                ExecuteEvents.Execute(src, ped, ExecuteEvents.endDragHandler);
                ExecuteEvents.Execute(src, ped, ExecuteEvents.pointerUpHandler);
                return OkWithState($"已拖拽：起点 {src.name} → 落点 {dropName}（由落点控件判定是否接受）");
            }
            catch (Exception ex) { return Err($"拖拽派发失败：{ShortErr(ex)}"); }
        }

        /// <summary>
        /// 在指定屏幕点派发一次完整的指针点击（down → up → click）。
        /// 与玩家点击走同一条 EventSystem 管线；不触碰任何游戏字段。
        /// </summary>
        private static string DispatchPointerClick(float x, float y, bool rightButton, bool doubleClick)
        {
            var es = EventSystem.current;
            if (es == null) return Err("EventSystem 不可用（当前场景没有 UI 事件系统）");
            PointerEventData ped;
            var results = RaycastAt(x, y, out ped);
            if (results.Count == 0) return Err($"该位置 ({x},{y}) 没有可点击的 UI 对象");
            var go = results[0].gameObject;
            if (go == null) return Err("命中的对象为空");
            ped.button = rightButton ? PointerEventData.InputButton.Right : PointerEventData.InputButton.Left;
            ped.clickCount = doubleClick ? 2 : 1;
            ped.pressPosition = new Vector2(x, y);
            ped.pointerPressRaycast = results[0];
            try
            {
                if (rightButton)
                {
                    ExecuteEvents.Execute(go, ped, ExecuteEvents.pointerDownHandler);
                    ExecuteEvents.Execute(go, ped, ExecuteEvents.pointerUpHandler);
                    ExecuteEvents.Execute(go, ped, ExecuteEvents.pointerClickHandler);
                }
                else
                {
                    ExecuteEvents.Execute(go, ped, ExecuteEvents.pointerDownHandler);
                    ExecuteEvents.Execute(go, ped, ExecuteEvents.pointerUpHandler);
                    ExecuteEvents.Execute(go, ped, ExecuteEvents.pointerClickHandler);
                    if (doubleClick)
                        ExecuteEvents.Execute(go, ped, ExecuteEvents.pointerClickHandler);
                }
            }
            catch (Exception ex) { return Err($"指针事件派发失败：{ShortErr(ex)}"); }
            string path = "";
            try { path = TransformPath(go.transform); } catch { }
            return OkWithState($"已在 ({x},{y}) 派发{(rightButton ? "右键" : (doubleClick ? "双击" : "左键"))}：{go.name}（{path}）");
        }

        /// <summary>
        /// GET /ui/nodes（v2.6）：所有 TreeNodeRender 的屏幕矩形。
        ///
        /// TreeNodeRender 是游戏自绘 UI 的渲染节点，实现 IBeginDragHandler /
        /// IDragHandler / IEndDragHandler / IDropHandler / IPointerClickHandler——
        /// 物品拖拽和点击都由它处理。element 指回 PixelElement（GameItem /
        /// GameGridInventory / PixelWindow），rectTransform 给出屏幕坐标，
        /// 于是「物品在哪、格子在哪、窗口在哪」全都可算。
        /// </summary>
        private static string UiNodesJson()
        {
            var sb = new StringBuilder(8192);
            int found = 0, activeCount = 0, withRt = 0, sized = 0;
            sb.Append("{\"ok\":true,\"nodes\":[");
            int n = 0;
            try
            {
                var all = Resources.FindObjectsOfTypeAll(Il2CppType.Of<TreeNodeRender>());
                if (all != null)
                {
                    for (var i = 0; i < all.Length; i++)
                    {
                        found++;
                        TreeNodeRender node = null;
                        try { node = all[i].TryCast<TreeNodeRender>(); } catch { }
                        if (node == null) continue;
                        bool active = false;
                        try { active = node.gameObject != null && node.gameObject.activeInHierarchy; } catch { }
                        if (!active) continue;
                        activeCount++;
                        RectTransform rt = null;
                        try { rt = node.rectTransform; } catch { }
                        if (rt == null) continue;
                        withRt++;
                        // 注意：GetWorldCorners 必须传 Il2CppStructArray（托管 Vector3[]
                        // 不会被回填，实测宽高全 0）。Canvas 是自绘 UI，scaleFactor 可能
                        // 远大于 1（游戏自己用 canvas 缩放模拟像素风），所以这里只信
                        // WorldToScreenPoint，不信 rect*scaleFactor。
                        Camera cam = null;
                        try
                        {
                            var canvas = rt.GetComponentInParent<Canvas>();
                            if (canvas != null && canvas.renderMode != RenderMode.ScreenSpaceOverlay)
                                cam = canvas.worldCamera;
                        }
                        catch { }
                        var corners = new Il2CppStructArray<Vector3>(4);
                        try { rt.GetWorldCorners(corners); } catch { continue; }
                        Vector2 bl, tr;
                        try
                        {
                            bl = RectTransformUtility.WorldToScreenPoint(cam, corners[0]);
                            tr = RectTransformUtility.WorldToScreenPoint(cam, corners[2]);
                        }
                        catch { continue; }
                        float w = tr.x - bl.x, h = tr.y - bl.y;
                        if (w < 2f || h < 2f) continue;          // 过滤退化节点
                        if (w > 1200f || h > 1200f) continue;    // 过滤异常大节点
                        sized++;

                        string elemType = "";
                        string elemErr = "";
                        string ident = "";
                        string display = "";
                        int count = 0;
                        try
                        {
                            var elem = node.element;
                            if (elem == null) elemErr = "null";
                            else
                            {
                                GameItem gi = null;
                                try { gi = elem.TryCast<GameItem>(); } catch { }
                                if (gi != null)
                                {
                                    elemType = "GameItem";
                                    ident = gi.identifier ?? "";
                                    display = gi.name ?? "";
                                    count = gi.unitCount;
                                }
                                else
                                {
                                    GameInventory ginv = null;
                                    try { ginv = elem.TryCast<GameInventory>(); } catch { }
                                    if (ginv != null) { elemType = "GameInventory"; ident = ginv.identifier ?? ""; }
                                    else
                                    {
                                        PixelWindow pw = null;
                                        try { pw = elem.TryCast<PixelWindow>(); } catch { }
                                        if (pw != null) { elemType = "PixelWindow"; display = pw.titleString ?? ""; }
                                        else { try { elemType = elem.GetType().Name; } catch { elemType = "PixelElement"; } }
                                    }
                                }
                            }
                        }
                        catch (Exception ex) { elemErr = ShortErr(ex); }

                        if (n > 0) sb.Append(',');
                        sb.Append("{\"cx\":").Append((int)((bl.x + tr.x) / 2));
                        sb.Append(",\"cy\":").Append((int)((bl.y + tr.y) / 2));
                        sb.Append(",\"w\":").Append((int)w).Append(",\"h\":").Append((int)h);
                        string goName = "", goPath = "", nodeText = "";
                        try { goName = node.gameObject.name; } catch { }
                        try { var t = node.text; if (t != null) nodeText = t.text ?? ""; } catch { }
                        if (nodeText.Length > 40) nodeText = nodeText.Substring(0, 40);
                        try { goPath = TransformPath(node.transform); } catch { }
                        if (goPath != null && goPath.Length > 70) goPath = goPath.Substring(goPath.Length - 70);
                        sb.Append(",\"go\":").Append(JsonString(goName));
                        sb.Append(",\"path\":").Append(JsonString(goPath));
                        sb.Append(",\"elem\":").Append(JsonString(elemType));
                        if (elemErr != "") sb.Append(",\"elem_err\":").Append(JsonString(elemErr));
                        sb.Append(",\"id\":").Append(JsonString(ident));
                        sb.Append(",\"name\":").Append(JsonString(display));
                        sb.Append(",\"count\":").Append(count);
                        if (nodeText != "") sb.Append(",\"text\":").Append(JsonString(nodeText));
                        sb.Append('}');
                        n++;
                        if (n >= 240) break;
                    }
                }
            }
            catch (Exception ex) { return Err($"枚举渲染节点失败：{ShortErr(ex)}"); }
            sb.Append("],\"diag\":{\"found\":").Append(found)
              .Append(",\"active\":").Append(activeCount)
              .Append(",\"with_rt\":").Append(withRt)
              .Append(",\"sized\":").Append(sized).Append('}');
            sb.Append('}');
            return sb.ToString();
        }

        /// <summary>GET /ui/objects：全量可点击对象（自绘界面里的条目也在这里）。</summary>
        private static string BuildAllClickables()
        {
            var sb = new StringBuilder(8192);
            sb.Append("{\"ok\":true,\"objects\":[");
            int n = 0;
            foreach (var mb in CollectAllClickables(false))
            {
                if (mb == null) continue;
                if (n > 0) sb.Append(',');
                sb.Append(ClickableJson(mb));
                n++;
            }
            sb.Append("]}");
            return sb.ToString();
        }

        private static string BuildUiButtons(bool includeInactive)
        {
            var sb = new StringBuilder(8192);
            sb.Append("{\"ok\":true,\"buttons\":[");
            int count = 0;
            // ① uGUI 标准按钮
            try
            {
                var all = Resources.FindObjectsOfTypeAll(Il2CppType.Of<Button>());
                if (all != null)
                {
                    for (var i = 0; i < all.Length; i++)
                    {
                        Button b = null;
                        try { b = all[i].TryCast<Button>(); } catch { }
                        if (b == null) continue;
                        bool active = false;
                        try { active = b.gameObject.activeInHierarchy; } catch { }
                        if (!active && !includeInactive) continue;
                        if (count >= 200) break;  // 防御上限
                        if (count > 0) sb.Append(',');
                        sb.Append(ButtonJson(b));
                        count++;
                    }
                }
            }
            catch (Exception e)
            {
                return Err($"枚举按钮失败：{ShortErr(e)}");
            }
            // ② 游戏自定义可点击组件（IClickable：按铃/送客/卷帘门/睡袋…）
            try
            {
                foreach (var c in EnumerateCustomClickables())
                {
                    if (c == null) continue;
                    bool active = false;
                    try { active = c.gameObject.activeInHierarchy; } catch { }
                    if (!active && !includeInactive) continue;
                    string tn = IdentifyCustom(c);
                    if (tn == null) continue;
                    if (count >= 200) break;
                    if (count > 0) sb.Append(',');
                    sb.Append(CustomJson(c, tn));
                    count++;
                }
            }
            catch (Exception e)
            {
                return Err($"枚举自定义组件失败：{ShortErr(e)}");
            }
            sb.Append("],\"count\":").Append(count).Append('}');
            return sb.ToString();
        }

        /// <summary>按钮信息 → JSON（名称/文本/可交互/层级路径/世界坐标）。</summary>
        private static string ButtonJson(Button b)
        {
            var sb = new StringBuilder(256);
            string text = "";
            try
            {
                var t = b.GetComponentInChildren<Text>();
                if (t != null) text = t.text;
            }
            catch { }
            if (string.IsNullOrEmpty(text))
            {
                try
                {
                    var tmp = b.GetComponentInChildren<TMPro.TMP_Text>();
                    if (tmp != null) text = tmp.text;
                }
                catch { }
            }
            bool interactable = false;
            try { interactable = b.interactable; } catch { }
            bool active = false;
            try { active = b.gameObject.activeInHierarchy; } catch { }
            var pos = Vector3.zero;
            try { pos = b.transform.position; } catch { }
            sb.Append("{\"kind\":\"ugui\",\"type\":\"Button\"");
            sb.Append(",\"active\":").Append(B(active));
            sb.Append(",\"name\":").Append(JsonString(b.name));
            sb.Append(",\"text\":").Append(JsonString(text));
            sb.Append(",\"interactable\":").Append(B(interactable));
            sb.Append(",\"path\":").Append(JsonString(TransformPath(b.transform)));
            sb.Append(",\"pos\":[").Append((int)pos.x).Append(',').Append((int)pos.y).Append("]}");
            return sb.ToString();
        }

        /// <summary>transform 层级路径（用于定位按钮，如 Canvas/Panel/Btn）。</summary>
        private static string TransformPath(Transform t)
        {
            try
            {
                var parts = new System.Collections.Generic.List<string>();
                var cur = t;
                int guard = 0;
                while (cur != null && guard++ < 20)
                {
                    parts.Add(cur.name);
                    cur = cur.parent;
                }
                parts.Reverse();
                return string.Join("/", parts);
            }
            catch { return ""; }
        }

        /// <summary>按对象名或文本查找活跃按钮（名称优先精确匹配，其次文本精确匹配）。</summary>
        private static Button FindButton(string name, string text, string pathContains = null)
        {
            try
            {
                var all = Resources.FindObjectsOfTypeAll(Il2CppType.Of<Button>());
                if (all == null) return null;
                for (var i = 0; i < all.Length; i++)
                {
                    Button b = null;
                    try { b = all[i].TryCast<Button>(); } catch { }
                    if (b == null) continue;
                    bool active = false;
                    try { active = b.gameObject.activeInHierarchy; } catch { }
                    if (!active) continue;
                    if (!string.IsNullOrEmpty(pathContains))
                    {
                        // v1.6：按 Transform 层级路径子串过滤（用 GET /ui/buttons 的 path 字段照抄）
                        string p = null;
                        try { p = TransformPath(b.transform); } catch { }
                        if (p == null || p.IndexOf(pathContains, StringComparison.OrdinalIgnoreCase) < 0)
                            continue;
                    }
                    if (!string.IsNullOrEmpty(name))
                    {
                        if (b.name == name) return b;
                    }
                    else
                    {
                        string t = "";
                        try { var lt = b.GetComponentInChildren<Text>(); if (lt != null) t = lt.text; } catch { }
                        if (string.IsNullOrEmpty(t))
                        {
                            try { var tt = b.GetComponentInChildren<TMPro.TMP_Text>(); if (tt != null) t = tt.text; } catch { }
                        }
                        if (t == text) return b;
                    }
                }
            }
            catch { }
            return null;
        }

        /// <summary>按对象名点一个活跃 uGUI 按钮（v0.9.13，供 go_out 等流程复用）。</summary>
        private static bool TryClickButton(string name)
        {
            var b = FindButton(name, null);
            if (b == null) return false;
            bool interactable = false;
            try { interactable = b.interactable; } catch { }
            if (!interactable) return false;
            try { b.onClick.Invoke(); return true; } catch { return false; }
        }

        /// <summary>按显示文本点一个活跃 uGUI 按钮（v0.9.13）。</summary>
        private static bool TryClickButtonByText(string text)
        {
            var b = FindButton(null, text);
            if (b == null) return false;
            try { b.onClick.Invoke(); return true; } catch { return false; }
        }

        // ---------------------------------------------------------------- 自定义按钮支持（v0.9.1）

        /// <summary>枚举游戏自定义可点击组件（27 类 IClickable 实现——dump.cs 全量提取）。
        /// 这些不是 uGUI Button，而是游戏自己的交互组件（玩家点击 → OnClick()）。</summary>
        /// <summary>
        /// 全量可点击对象（v2.5）：扫描所有 MonoBehaviour，按接口识别
        /// IClickable（单击）/ IDoubleClickable（双击）/ IControlable（右键）。
        /// 之前是 26 个类型的白名单，平板电脑、网络商店、机器面板这些自绘界面
        /// 里的条目全都抓不到——这不代表它们不可点击，只是没被枚举。
        /// </summary>
        private static System.Collections.Generic.List<MonoBehaviour> CollectAllClickables(bool includeInactive)
        {
            var list = new System.Collections.Generic.List<MonoBehaviour>(128);
            try
            {
                var all = Resources.FindObjectsOfTypeAll(Il2CppType.Of<MonoBehaviour>());
                if (all == null) return list;
                for (var i = 0; i < all.Length; i++)
                {
                    MonoBehaviour mb = null;
                    try { mb = all[i].TryCast<MonoBehaviour>(); } catch { }
                    if (mb == null) continue;
                    bool active = false;
                    try { active = mb.gameObject.activeInHierarchy; } catch { }
                    if (!active && !includeInactive) continue;
                    bool clickable = false;
                    try { clickable = mb.TryCast<IClickable>() != null; } catch { }
                    if (!clickable) { try { clickable = mb.TryCast<IDoubleClickable>() != null; } catch { } }
                    if (!clickable) { try { clickable = mb.TryCast<IControlable>() != null; } catch { } }
                    if (!clickable) continue;
                    list.Add(mb);
                    if (list.Count >= 240) break;
                }
            }
            catch { }
            return list;
        }

        /// <summary>单个可点击对象的 JSON（类型/名字/提示/路径/支持的点击方式）。</summary>
        private static string ClickableJson(MonoBehaviour mb)
        {
            var sb = new StringBuilder(320);
            string typeName = "";
            // GetType() 在 Il2CppInterop 里只返回包装类型（MonoBehaviour），
            // 真正的中文/游戏类型名在 Il2CppType 上——否则清单里一排 MonoBehaviour 无法区分。
            try { typeName = mb.GetIl2CppType().Name; } catch { }
            if (string.IsNullOrEmpty(typeName) || typeName == "MonoBehaviour")
            { try { typeName = mb.GetType().Name; } catch { } }
            string tooltip = "";
            try { var ti = mb.TryCast<TooltipInterface>(); if (ti != null) tooltip = ti.GetTooltip(); } catch { }
            bool canClick = false, canDouble = false, canRight = false;
            try { canClick = mb.TryCast<IClickable>() != null; } catch { }
            try { canDouble = mb.TryCast<IDoubleClickable>() != null; } catch { }
            try { canRight = mb.TryCast<IControlable>() != null; } catch { }
            string path = "";
            try { path = TransformPath(mb.transform); } catch { }
            sb.Append("{\"kind\":\"clickable\",\"type\":").Append(JsonString(typeName));
            sb.Append(",\"name\":").Append(JsonString(mb.name));
            sb.Append(",\"text\":").Append(JsonString(tooltip));
            sb.Append(",\"path\":").Append(JsonString(path));
            sb.Append(",\"click\":").Append(B(canClick));
            sb.Append(",\"double_click\":").Append(B(canDouble));
            sb.Append(",\"right_click\":").Append(B(canRight));
            sb.Append('}');
            return sb.ToString();
        }

        /// <summary>对任意可点击对象执行玩家操作（click / double_click / right_click）。</summary>
        private static string InvokeClickableAction(MonoBehaviour mb, string op)
        {
            string kind = string.IsNullOrEmpty(op) ? "click" : op.ToLowerInvariant();
            try
            {
                if (kind == "double_click" || kind == "doubleclick")
                {
                    var d = mb.TryCast<IDoubleClickable>();
                    if (d == null) return null;
                    d.OnDoubleClick();
                    return "double_click";
                }
                if (kind == "right_click" || kind == "rightclick")
                {
                    var r = mb.TryCast<IControlable>();
                    if (r == null) return null;
                    r.OnRightClick(Vector3.zero);
                    return "right_click";
                }
                var c = mb.TryCast<IClickable>();
                if (c == null) return null;
                c.OnClick();
                return "click";
            }
            catch { }
            return null;
        }

        /// <summary>按 type/name/tooltip/path 子串在「全量可点击」里找对象。</summary>
        private static MonoBehaviour FindAnyClickable(string typeName, string name, string text, string path)
        {
            try
            {
                foreach (var mb in CollectAllClickables(false))
                {
                    if (mb == null) continue;
                    string tn = "";
                    try { tn = mb.GetIl2CppType().Name; } catch { }
                    if (string.IsNullOrEmpty(tn) || tn == "MonoBehaviour") { try { tn = mb.GetType().Name; } catch { } }
                    string tt = "";
                    try { var ti = mb.TryCast<TooltipInterface>(); if (ti != null) tt = ti.GetTooltip(); } catch { }
                    string pa = ""; try { pa = TransformPath(mb.transform); } catch { }
                    if (!string.IsNullOrEmpty(typeName) && tn != typeName) continue;
                    if (!string.IsNullOrEmpty(name) && mb.name != name) continue;
                    if (!string.IsNullOrEmpty(text) && tt != text) continue;
                    if (!string.IsNullOrEmpty(path) && (pa == null || pa.IndexOf(path, StringComparison.OrdinalIgnoreCase) < 0)) continue;
                    return mb;
                }
            }
            catch { }
            return null;
        }

        private static System.Collections.Generic.List<Component> EnumerateCustomClickables()
        {
            var list = new System.Collections.Generic.List<Component>(64);
            Collect<StoreBell>(list);
            Collect<StoreDismissButton>(list);
            Collect<StoreShutterButton>(list);
            Collect<StoreTransferButton>(list);
            Collect<StoreSecButton>(list);
            Collect<StoreDoor>(list);
            Collect<StoreBedroll>(list);
            Collect<StoreCalendar>(list);
            Collect<StoreClienteleDisplay>(list);
            Collect<StoreCreditDisplay>(list);
            Collect<StoreDebugBell>(list);
            Collect<StoreLamp>(list);
            Collect<StoreMagnifyingGlass>(list);
            Collect<StoreNotebook>(list);
            Collect<StorePC>(list);
            Collect<StorePhoneObject>(list);
            Collect<StorePoster>(list);
            Collect<StoreRecorder>(list);
            Collect<StoreScale>(list);
            Collect<StoreTablet>(list);
            Collect<StoreVendingFountain>(list);
            Collect<StoreVendingMachine>(list);
            Collect<NegociationTarget>(list);
            Collect<StoreClientMono>(list);
            Collect<FeedbackBox>(list);
            Collect<NewsPaper>(list);
            Collect<Shotgun>(list);
            return list;
        }

        /// <summary>枚举单类活跃组件并加入清单（Il2CppInterop 需显式泛型）。</summary>
        private static void Collect<T>(System.Collections.Generic.List<Component> list) where T : Component
        {
            try
            {
                var all = Resources.FindObjectsOfTypeAll(Il2CppType.Of<T>());
                if (all == null) return;
                for (var i = 0; i < all.Length; i++)
                {
                    var c = all[i].TryCast<T>();
                    if (c == null) continue;
                    list.Add(c);  // 全量收集；活跃性由调用方过滤（枚举可含 inactive，点击路径只允许活跃）
                }
            }
            catch { }
        }

        /// <summary>识别自定义点击组件类型名（不触发调用）；未识别返回 null。</summary>
        private static string IdentifyCustom(Component comp) => InvokeCustomClick(comp, true);

        /// <summary>触发自定义组件的「玩家点击」（OnClick）——按钮路径。
        /// 返回被触发组件的类型名；dryCheck=true 时只识别不调用；未识别/异常返回 null。</summary>
        private static string InvokeCustomClick(Component comp, bool dryCheck = false)
        {
            try
            {
                var a1 = comp.TryCast<StoreBell>(); if (a1 != null) { if (!dryCheck) a1.OnClick(); return "StoreBell"; }
                var a2 = comp.TryCast<StoreDismissButton>(); if (a2 != null) { if (!dryCheck) a2.OnClick(); return "StoreDismissButton"; }
                var a3 = comp.TryCast<StoreShutterButton>(); if (a3 != null) { if (!dryCheck) a3.OnClick(); return "StoreShutterButton"; }
                var a4 = comp.TryCast<StoreTransferButton>(); if (a4 != null) { if (!dryCheck) a4.OnClick(); return "StoreTransferButton"; }
                var a5 = comp.TryCast<StoreSecButton>(); if (a5 != null) { if (!dryCheck) a5.OnClick(); return "StoreSecButton"; }
                var a6 = comp.TryCast<StoreDoor>(); if (a6 != null) { if (!dryCheck) a6.OnClick(); return "StoreDoor"; }
                var a7 = comp.TryCast<StoreBedroll>(); if (a7 != null) { if (!dryCheck) a7.OnClick(); return "StoreBedroll"; }
                var a8 = comp.TryCast<StoreCalendar>(); if (a8 != null) { if (!dryCheck) a8.OnClick(); return "StoreCalendar"; }
                var a9 = comp.TryCast<StoreClienteleDisplay>(); if (a9 != null) { if (!dryCheck) a9.OnClick(); return "StoreClienteleDisplay"; }
                var a10 = comp.TryCast<StoreCreditDisplay>(); if (a10 != null) { if (!dryCheck) a10.OnClick(); return "StoreCreditDisplay"; }
                var a11 = comp.TryCast<StoreDebugBell>(); if (a11 != null) { if (!dryCheck) a11.OnClick(); return "StoreDebugBell"; }
                var a12 = comp.TryCast<StoreLamp>(); if (a12 != null) { if (!dryCheck) a12.OnClick(); return "StoreLamp"; }
                var a13 = comp.TryCast<StoreMagnifyingGlass>(); if (a13 != null) { if (!dryCheck) a13.OnClick(); return "StoreMagnifyingGlass"; }
                var a14 = comp.TryCast<StoreNotebook>(); if (a14 != null) { if (!dryCheck) a14.OnClick(); return "StoreNotebook"; }
                var a15 = comp.TryCast<StorePC>(); if (a15 != null) { if (!dryCheck) a15.OnClick(); return "StorePC"; }
                var a16 = comp.TryCast<StorePhoneObject>(); if (a16 != null) { if (!dryCheck) a16.OnClick(); return "StorePhoneObject"; }
                var a17 = comp.TryCast<StorePoster>(); if (a17 != null) { if (!dryCheck) a17.OnClick(); return "StorePoster"; }
                var a18 = comp.TryCast<StoreRecorder>(); if (a18 != null) { if (!dryCheck) a18.OnClick(); return "StoreRecorder"; }
                var a19 = comp.TryCast<StoreScale>(); if (a19 != null) { if (!dryCheck) a19.OnClick(); return "StoreScale"; }
                var a20 = comp.TryCast<StoreTablet>(); if (a20 != null) { if (!dryCheck) a20.OnClick(); return "StoreTablet"; }
                var a21 = comp.TryCast<StoreVendingFountain>(); if (a21 != null) { if (!dryCheck) a21.OnClick(); return "StoreVendingFountain"; }
                var a22 = comp.TryCast<StoreVendingMachine>(); if (a22 != null) { if (!dryCheck) a22.OnClick(); return "StoreVendingMachine"; }
                var a23 = comp.TryCast<NegociationTarget>(); if (a23 != null) { if (!dryCheck) a23.OnClick(); return "NegociationTarget"; }
                var a24 = comp.TryCast<StoreClientMono>(); if (a24 != null) { if (!dryCheck) a24.OnClick(); return "StoreClientMono"; }
                var a25 = comp.TryCast<FeedbackBox>(); if (a25 != null) { if (!dryCheck) a25.OnClick(); return "FeedbackBox"; }
                var a26 = comp.TryCast<NewsPaper>(); if (a26 != null) { if (!dryCheck) a26.OnClick(); return "NewsPaper"; }
                var a27 = comp.TryCast<Shotgun>(); if (a27 != null) { if (!dryCheck) a27.OnClick(); return "Shotgun"; }
            }
            catch (Exception e)
            {
                try { BridgePlugin.Logger.LogWarning($"自定义按钮调用异常：{ShortErr(e)}"); } catch { }
            }
            return null;
        }

        /// <summary>自定义组件信息 → JSON（type/name/工具提示/路径/坐标）。</summary>
        private static string CustomJson(Component c, string typeName)
        {
            var sb = new StringBuilder(256);
            bool active = false;
            try { active = c.gameObject.activeInHierarchy; } catch { }
            string tooltip = "";
            try { var ti = c.TryCast<TooltipInterface>(); if (ti != null) tooltip = ti.GetTooltip(); } catch { }
            var pos = Vector3.zero;
            try { pos = c.transform.position; } catch { }
            sb.Append("{\"kind\":\"custom\",\"type\":").Append(JsonString(typeName));
            sb.Append(",\"active\":").Append(B(active));
            sb.Append(",\"name\":").Append(JsonString(c.name));
            sb.Append(",\"text\":").Append(JsonString(tooltip));
            sb.Append(",\"interactable\":true");
            sb.Append(",\"path\":").Append(JsonString(TransformPath(c.transform)));
            sb.Append(",\"pos\":[").Append((int)pos.x).Append(',').Append((int)pos.y).Append("]}");
            return sb.ToString();
        }

        /// <summary>按类型名/对象名/工具提示文本查找自定义组件（给出其一即可，全空不匹配）。</summary>
        private static Component FindCustomClickable(string typeName, string name, string text)
        {
            if (string.IsNullOrEmpty(typeName) && string.IsNullOrEmpty(name) && string.IsNullOrEmpty(text))
                return null;
            try
            {
                foreach (var c in EnumerateCustomClickables())
                {
                    if (c == null) continue;
                    bool active = false;
                    try { active = c.gameObject.activeInHierarchy; } catch { }
                    if (!active) continue;  // 点击路径只允许活跃对象
                    string tn = IdentifyCustom(c);
                    if (tn == null) continue;
                    if (!string.IsNullOrEmpty(typeName) && tn != typeName) continue;
                    if (!string.IsNullOrEmpty(name) && c.name != name) continue;
                    if (!string.IsNullOrEmpty(text))
                    {
                        string tooltip = "";
                        try { var ti = c.TryCast<TooltipInterface>(); if (ti != null) tooltip = ti.GetTooltip(); } catch { }
                        if (tooltip != text) continue;
                    }
                    return c;
                }
            }
            catch { }
            return null;
        }

        private static string CurrentScene()
        {
            try { return SceneManager.GetActiveScene().name; }
            catch { return null; }
        }

        private static string Ok(string detail) =>
            "{\"ok\":true,\"detail\":" + JsonString(detail) + "}";

        private static string OkWithState(string detail) =>
            "{\"ok\":true,\"detail\":" + JsonString(detail) + ",\"state\":" + BuildState() + "}";

        private static string Err(string detail) =>
            "{\"ok\":false,\"error\":" + JsonString(detail) + "}";

        /// <summary>异常消息截断（用于 detail 里带降级链各步的失败原因）。</summary>
        private static string ShortErr(Exception ex)
        {
            var msg = ex?.Message ?? "null";
            return msg.Length <= 60 ? msg : msg.Substring(0, 60);
        }

        /// <summary>按标识在玩家自有物品里找实例（sell_item 用）。</summary>
        private static GameItem FindOwnedItem(PlayerStore ps, string identifier)
        {
            try
            {
                var all = ps.FindAllItem(true);
                if (all == null) return null;
                // v2.4：同名物品可能同时存在于库存和机器内部（电池、模组）。
                // 先取玩家库存里的那份；只剩机器里的说明它不是可卖库存，返回 null，
                // 否则会把正在供电的电池卖给顾客、机器当场停机。
                GameItem inMachine = null;
                for (int i = 0; i < all.Count; i++)
                {
                    var c = all[i];
                    if (c == null || c.identifier == null || c.identifier.ToString() != identifier) continue;
                    if (IsInsideMachine(c)) { if (inMachine == null) inMachine = c; continue; }
                    return c;
                }
                return null;
            }
            catch { }
            return null;
        }

        /// <summary>按标识在非自有（顾客携带）物品里找实例（buy_item 用）。</summary>
        private static GameItem FindNotOwnedItem(PlayerStore ps, string identifier)
        {
            try
            {
                var all = ps.FindAllItem(false);
                if (all == null) return null;
                for (int i = 0; i < all.Count; i++)
                {
                    var c = all[i];
                    if (c != null && c.identifier != null && c.identifier.ToString() == identifier) return c;
                }
            }
            catch { }
            return null;
        }

        /// <summary>按标识在全部物品（自有+非自有）里找实例（appraise/display_case 用）。</summary>
        private static GameItem FindAnyItem(PlayerStore ps, string identifier)
        {
            var own = FindOwnedItem(ps, identifier);
            if (own != null) return own;
            return FindNotOwnedItem(ps, identifier);
        }

        /// <summary>统计自有库存里某标识的件数（buy_item 钱货两清验证用）。</summary>
        private static int CountOwned(PlayerStore ps, string identifier)
        {
            int n = 0;
            try
            {
                var all = ps.FindAllItem(true);
                if (all == null) return 0;
                for (int i = 0; i < all.Count; i++)
                {
                    var c = all[i];
                    if (c != null && c.identifier != null && c.identifier.ToString() == identifier) n++;
                }
            }
            catch { }
            return n;
        }

        /// <summary>
        /// v1.0「玩家路径」交易链（用户 2026-10-18 指令）：标记议价物品 → 打开谈判
        /// 面板 → 触发对应方向的按钮处理器。
        ///
        /// 反汇编实证（dump.cs + RVA 解析）：
        /// - 卖出按钮处理器 TrySellItem (0x725200)：CanSellThisItem → PlayerStore.SellItem
        ///   → RefreshUI → OnDealAccepted → 结果对话；
        /// - 买入按钮处理器 BuyItem (0x71F780)：PlayerStore.BuyItemFromTable → RefreshUI
        ///   → OnDealAccepted → 结果对话；
        /// - OnOfferAcceptClick (0x723B50) 内部只走 BuyItem 方向——卖出用它无效
        ///   （r57 实测 3 次卖出零成交）。
        /// 即「点面板按钮」才是玩家的成交动作；直调 SellItem/BuyItem 只有核心扣账、
        /// 跳过 RefreshUI/OnDealAccepted/对话整条 UI 链（r57 前一直如此）。
        ///
        /// 标记环节（currentNegociatedItem 字段）与 place_item 同源——v0.5.0 实证
        /// 玩家 Ctrl+点击/拖拽上桌的最终效果就是写这两个字段。
        /// </summary>
        private static string MarkForNegotiation(PlayerStore ps, GameItem target, bool selling, out bool negoOk)
        {
            negoOk = false;
            string note = "";
            try { if (ps != null) ps.currentNegociatedItem = target; } catch { }
            var nego = NegociationUIManager.Instance;
            if (nego == null) return "（NegociationUIManager 不可用，无法触发按钮处理器）";
            try
            {
                nego.currentNegociatedItem = target; // 与 place_item 同字段
                nego.OpenUI();                      // 面板开着时按钮处理器才响应
                if (selling) nego.TrySellItem();    // 玩家「卖出」按钮处理器
                else nego.BuyItem();                // 玩家「收购」按钮处理器
                negoOk = true;
                note = selling ? "：标记议价物品→面板卖出按钮" : "：标记议价物品→面板收购按钮";
            }
            catch (Exception ex)
            {
                note = $"（面板按钮链异常：{ShortErr(ex)}）";
            }
            return note;
        }

        private static string B(bool value) => value ? "true" : "false";

        /// <summary>JSON 字符串转义。</summary>
        public static string JsonString(string s)
        {
            if (s == null) return "null";
            var sb = new StringBuilder(s.Length + 8);
            sb.Append('"');
            foreach (char c in s)
            {
                switch (c)
                {
                    case '"': sb.Append("\\\""); break;
                    case '\\': sb.Append("\\\\"); break;
                    case '\n': sb.Append("\\n"); break;
                    case '\r': sb.Append("\\r"); break;
                    case '\t': sb.Append("\\t"); break;
                    default:
                        if (c < 0x20)
                        {
                            sb.Append("\\u").Append(((int)c).ToString("x4"));
                        }
                        else
                        {
                            sb.Append(c);
                        }
                        break;
                }
            }
            sb.Append('"');
            return sb.ToString();
        }

        /// <summary>
        /// 从简单 JSON 中提取字段值（动作请求体结构固定，无需完整解析器）。
        /// 支持字符串与数字/布尔（返回原始字面量）。
        /// </summary>
        private static string ExtractValue(string json, string key)
        {
            if (string.IsNullOrEmpty(json)) return null;
            var pattern = "\"" + key + "\"";
            int idx = json.IndexOf(pattern, StringComparison.Ordinal);
            if (idx < 0) return null;
            idx = json.IndexOf(':', idx + pattern.Length);
            if (idx < 0) return null;
            idx++;
            while (idx < json.Length && char.IsWhiteSpace(json[idx])) idx++;
            if (idx >= json.Length) return null;

            if (json[idx] == '"')
            {
                var sb = new StringBuilder();
                idx++;
                while (idx < json.Length && json[idx] != '"')
                {
                    char c = json[idx];
                    if (c == '\\' && idx + 1 < json.Length)
                    {
                        idx++;
                        sb.Append(json[idx]);
                    }
                    else
                    {
                        sb.Append(c);
                    }
                    idx++;
                }
                return sb.ToString();
            }

            // 布尔字面量（v0.8.0：night_service 的 enable / phone_book 的 show）
            if (idx + 4 <= json.Length && json.Substring(idx, 4) == "true") return "true";
            if (idx + 5 <= json.Length && json.Substring(idx, 5) == "false") return "false";

            int start = idx;
            while (idx < json.Length && (char.IsDigit(json[idx]) || json[idx] == '-' || json[idx] == '.'))
            {
                idx++;
            }
            return idx > start ? json.Substring(start, idx - start) : null;
        }
    }
}
