"""OmniAgent Event System — 发布-订阅事件总线。

基于 Pydantic 类型化事件模型，提供:
- EventBus: 核心发布-订阅总线
- 标准事件类型: run/tool/llm/step/review 等
- 与现有 EngineCallback 的桥接层

设计原则（借鉴 KamaClaude）:
- 所有事件都是强类型的 Pydantic BaseModel
- 订阅者通过 async handler 函数注册
- 事件发布按注册顺序依次通知
- 任何组件（TUI/CLI/trace writer）都可以独立订阅
"""

from omniagent.events.bus import EventBus
from omniagent.events.models import (
    BaseEvent,
    RunStartedEvent,
    RunFinishedEvent,
    StepStartedEvent,
    StepFinishedEvent,
    ToolCallStartedEvent,
    ToolCallFinishedEvent,
    LlmModelSelectedEvent,
    LlmTokenEvent,
    LlmUsageEvent,
    AgentThoughtEvent,
    AgentFinalAnswerEvent,
    ContextCompactedEvent,
    PermissionRequestEvent,
    ReviewFinishedEvent,
    RunErrorEvent,
    RunWarningEvent,
)

__all__ = [
    "EventBus",
    "BaseEvent",
    "RunStartedEvent",
    "RunFinishedEvent",
    "StepStartedEvent",
    "StepFinishedEvent",
    "ToolCallStartedEvent",
    "ToolCallFinishedEvent",
    "LlmModelSelectedEvent",
    "LlmTokenEvent",
    "LlmUsageEvent",
    "AgentThoughtEvent",
    "AgentFinalAnswerEvent",
    "ContextCompactedEvent",
    "PermissionRequestEvent",
    "ReviewFinishedEvent",
    "RunErrorEvent",
    "RunWarningEvent",
]
