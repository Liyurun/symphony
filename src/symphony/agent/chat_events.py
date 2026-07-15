"""默认流式对话（ChatRuntime）的事件模型。

与 SOP 执行事件（agent/events.py）分开定义，专用于 Pi Agent 默认对话的
流式推送：思考、工具调用、工具结果、逐段回答、完成与失败。
所有事件通过 to_dict() 序列化为可经 WebSocket send_json 的纯 ASCII 键字典。
"""

from typing import Any

from pydantic import BaseModel, Field


class ChatEvent(BaseModel):
    """所有 chat 事件的基类。"""

    type: str

    def to_dict(self) -> dict:
        """序列化为字典，剔除值为 None 的字段。"""
        return self.model_dump(exclude_none=True)


class ChatThinking(ChatEvent):
    """一轮工具调用前的思考提示。"""

    type: str = "chat_thinking"
    content: str


class ChatToolCall(ChatEvent):
    """模型发起一次工具调用。"""

    type: str = "chat_tool_call"
    skill_name: str
    args: dict[str, Any] = Field(default_factory=dict)
    summary: str | None = None


class ChatToolResult(ChatEvent):
    """一次工具调用的结果（成功或失败）。"""

    type: str = "chat_tool_result"
    skill_name: str
    ok: bool
    detail: str = ""


class ChatAnswerDelta(ChatEvent):
    """一段增量回答文本。"""

    type: str = "chat_answer_delta"
    text: str


class ChatCompleted(ChatEvent):
    """本轮对话完成，携带完整回答。"""

    type: str = "chat_completed"
    answer: str


class ChatFailed(ChatEvent):
    """本轮对话失败。"""

    type: str = "chat_failed"
    error: str
