"""项目统一异常定义。

所有自定义异常的基类是 GameAgentError，CLI 会按异常类型映射到不同的退出码，
便于脚本化调用与问题定位。
"""


class GameAgentError(Exception):
    """所有自定义异常的基类。"""


class WindowNotFoundError(GameAgentError):
    """未能找到目标游戏窗口（未运行 / 被最小化 / 标题关键词不匹配）。"""


class CaptureError(GameAgentError):
    """截图捕获失败（窗口失效、黑屏、纯色无效画面等）。"""


class AnalyzeError(GameAgentError):
    """视觉模型分析失败（未配置、网络错误、响应无法解析等）。"""


class ControlError(GameAgentError):
    """鼠标/键盘控制失败（窗口无法激活、坐标越界、按键不支持等）。

    设计原则：控制类操作宁可拒绝执行并报错，也不盲目把输入发给错误窗口。
    """


class BridgeError(GameAgentError):
    """BepInEx 桥接通信失败（游戏未运行 / 插件未加载 / 请求超时 / 响应非法）。

    涉及 DeepSpaceBridge 插件的 HTTP 通信（127.0.0.1:8965）：
    状态读取、动作直调、决策浮层推送。
    """
