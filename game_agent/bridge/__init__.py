"""BepInEx 桥接模块（bridge）：与游戏内 DeepSpaceBridge 插件通信（HTTP）。

这是「操控游戏」的第二通道（第一通道为鼠标键盘模拟 control 模块），
以及「游戏内决策展示浮层」的推送通道。

    Python（本模块）  ──HTTP 127.0.0.1:8965──▶  游戏进程（BepInEx 插件）
      GET  /state        读内存实时状态（现金/顾客/对话/门状态…）
      POST /action       直调游戏 API（12 种动作，见 client.ACTIONS）
      POST /decision     推送 AI 决策 → 游戏内右上角浮层展示
      GET  /decision     读回当前浮层状态（调试/测试用）

对外入口：

    BridgeClient    —— 桥接客户端（ping / get_state / post_action /
                        post_decision / get_decision）
    BridgeError     —— 桥接通信失败统一异常（见 game_agent.errors）

典型用法：

    client = BridgeClient()
    client.ping()                                  # 确认游戏与插件就绪
    state = client.get_state()                     # 读状态
    client.post_action("open_shutter")             # 直调开门
    client.post_decision(decision, step=1)         # 推送决策到浮层

注意：桥接服务随游戏进程启动而就绪；游戏未运行时会话以 BridgeError 失败，
调用方应按「best-effort」处理（如决策浮层推送失败不影响主循环）。
"""
from game_agent.bridge.client import ACTIONS, BridgeClient, BridgeError

__all__ = ["BridgeClient", "BridgeError", "ACTIONS"]
