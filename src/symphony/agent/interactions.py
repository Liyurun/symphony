"""用户反问与回答模型。

该模块定义产品层 interaction 协议，供 Chat、SOP 和统一 Session 日志复用。
"""

import uuid
from typing import Any

from pydantic import BaseModel, Field


class InteractionOption(BaseModel):
    """反问选项。"""

    label: str
    value: Any


class InteractionRequest(BaseModel):
    """用户反问请求。"""

    type: str = "interaction_requested"
    interaction_id: str = Field(default_factory=lambda: f"int-{uuid.uuid4().hex[:8]}")
    session_id: str
    task_id: str | None = None
    node_id: str | None = None
    prompt: str
    input_schema: dict[str, Any]
    options: list[InteractionOption] = Field(default_factory=list)
    multi_select: bool = False
    status: str = "pending"

    @classmethod
    def text(
        cls,
        session_id: str,
        prompt: str,
        task_id: str | None = None,
        node_id: str | None = None,
    ) -> "InteractionRequest":
        """构造文本输入反问。"""
        return cls(
            session_id=session_id,
            task_id=task_id,
            node_id=node_id,
            prompt=prompt,
            input_schema={
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
        )

    @classmethod
    def select(
        cls,
        session_id: str,
        prompt: str,
        options: list[dict[str, Any]],
        multi_select: bool = False,
        task_id: str | None = None,
        node_id: str | None = None,
    ) -> "InteractionRequest":
        """构造选择题反问。"""
        return cls(
            session_id=session_id,
            task_id=task_id,
            node_id=node_id,
            prompt=prompt,
            input_schema={"type": "object"},
            options=[InteractionOption(**item) for item in options],
            multi_select=multi_select,
        )


class InteractionAnswer(BaseModel):
    """用户反问回答。"""

    type: str = "interaction_answered"
    interaction_id: str
    session_id: str
    answer: dict[str, Any]
