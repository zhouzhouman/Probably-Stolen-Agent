"""决策模块：把结构化观察 + 操作历史交给语言模型，产出可执行的下一步动作。

输出契约（对下游的稳定接口）：
    {
      "action": "click | move | press_key | type_text | wait | stop",
      "params": {...},          # 与动作对应的参数
      "reason": "...",          # 决策理由（简短中文）
      "confidence": 0.0 ~ 1.0   # 置信度
    }

动作白名单与参数校验见 schema.py；提示词见 prompt.py；客户端见 client.py。
"""

from game_agent.brain.brain import decide
from game_agent.brain.client import MockBrainClient, OpenAITextClient, make_brain_client
from game_agent.brain.prompt import build_decision_prompt
from game_agent.brain.schema import ALLOWED_ACTIONS, validate_decision

__all__ = [
    "decide",
    "validate_decision",
    "build_decision_prompt",
    "make_brain_client",
    "MockBrainClient",
    "OpenAITextClient",
    "ALLOWED_ACTIONS",
]
