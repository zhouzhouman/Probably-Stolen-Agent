using System;
using System.Collections;
using System.Collections.Generic;

namespace DeepSpaceBridge
{
    /// <summary>
    /// 正式版（Mono）下访问游戏私有成员的统一出口。
    ///
    /// 为什么需要它：试玩版跑在 IL2CPP 上，字段/方法经 Il2CppInterop 暴露，访问修饰符不构成障碍；
    /// 换成 Mono 后编译期拿不到 private 成员，而其中几个（格子库存、卷帘门状态、日结行）
    /// 官方没有提供公开替代。
    ///
    /// 为什么走 ModReflect 而不是 System.Reflection（2026-11-14，R0 反编译 ModScanner 后改）：
    /// 游戏的 mod 扫描器只把「接收者类型是 System.Type 的 GetField/GetMethod/... 调用」判为
    /// reflection-external；而 2026-12-10 的游戏更新会拒载仍声明该能力的 mod。
    /// 旧实现是 `FindField(target.GetType(), name)` + `current.GetField(name, ...)`：
    /// 接收者是 System.Type、成员名来自形参、类型来自 computed 的 GetType()，
    /// 扫描器沿 Pass.FindProducer 追溯后判定为 " on a computed type"，属真违规。
    /// ModReflect 是官方为 mod 提供的等价出口（游戏 DLL 内明文写着
    /// "Replace computed reflection with typeof(...) or ModReflect"），
    /// 其接收者是 ModReflect 类本身，不进反射分类分支，因此不需要任何能力。
    /// 注意：不要为了「探测 ModReflect 是否可用」去反射 ModReflect 自己的成员——
    /// ModScanner.GuardTypes 里明确列了 ModReflect，那样做会被判 loader-target 硬拒。
    ///
    /// 语义承诺不变：**类型不符返回 default(T)、永不抛异常**。
    /// ModReflect.Get 在成员缺失时抛 ModReflectAccessException、target 为 null 时抛
    /// ArgumentNullException，所以这里必须 try/catch 兜住。
    /// </summary>
    public static class GameAccess
    {
        /// <summary>读取私有字段或属性。字段不存在、越权或类型不符时返回 default(T)，不抛异常。</summary>
        public static T Field<T>(object target, string name)
        {
            if (target == null)
            {
                return default;
            }
            try
            {
                // 用非泛型重载拿 object 再自行 is T：
                // 泛型重载 ModReflect.Get<T> 内部走 Convert，类型不符会抛 ModReflectAccessException，
                // 与旧实现「类型不符返回 default」的语义不一致。
                object value = ModReflect.Get(target, name);
                return value is T typed ? typed : default;
            }
            catch
            {
                return default;
            }
        }

        /// <summary>把 System.Collections.IEnumerable 私有字段读成强类型列表。</summary>
        public static List<object> ListField(object target, string name)
        {
            var result = new List<object>();
            if (!(Field<object>(target, name) is IEnumerable seq))
            {
                return result;
            }
            foreach (var item in seq)
            {
                result.Add(item);
            }
            return result;
        }
    }
}
