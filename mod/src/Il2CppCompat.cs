namespace DeepSpaceBridge
{
    /// <summary>
    /// IL2CPP → Mono 的兼容垫片。
    ///
    /// 旧桥接跑在 Il2CppInterop 上，所有对象都是 Il2CppObjectBase 包装类型，
    /// 换类型要靠 <c>obj.TryCast&lt;T&gt;()</c>；Mono 下对象的真实类型已经就位，
    /// 等价语义就是 <c>as</c>（失败返回 null，不抛异常）。
    ///
    /// 这里提供签名一致的扩展方法，让 BridgeApi / DecisionOverlay 的调用点**零改动**。
    /// 比用正则改写调用点更安全：正则切"接收者表达式"很容易把 <c>all[i]</c> 切成 <c>i]</c>。
    /// </summary>
    public static class Il2CppCompat
    {
        /// <summary>等价于 Il2CppObjectBase.TryCast&lt;T&gt;()：转型失败返回 null。</summary>
        public static T TryCast<T>(this object obj) where T : class => obj as T;
    }
}
