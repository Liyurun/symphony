"""Agent 运行时上下文定义。

AgentContext 承载单个节点一次 Agent 执行所需的可变状态：任务/节点标识、
对话消息历史、运行时变量、重试计数与可选的提示词覆盖。它在 ReAct 循环中被
持续读写，并可在等待用户输入后被复用以恢复执行。
"""

from typing import Any, Optional

from pydantic import BaseModel, Field

from symphony.ai.schema import Message, Role


class AgentContext(BaseModel):
    """单次 Agent 执行的运行时上下文。"""

    # 所属任务 id
    task_id: str
    # 所属节点 id
    node_id: str
    # 对话消息历史
    messages: list[Message] = Field(default_factory=list)
    # 运行时变量表，供技能读写
    variables: dict[str, Any] = Field(default_factory=dict)
    # 当前重试次数
    retry_count: int = 0
    # 提示词覆盖，非空时优先于运行时默认 system_prompt
    prompt_override: Optional[str] = None

    def add_message(self, message: Message) -> None:
        """向消息历史追加一条消息。"""
        self.messages.append(message)

    def reset_messages(self, system_prompt: Optional[str] = None) -> None:
        """清空消息历史，若提供 system_prompt 则写入一条系统消息。"""
        # 重置为一个全新的空列表
        self.messages = []
        # 有系统提示词时作为首条系统消息注入
        if system_prompt is not None:
            self.messages.append(Message(role=Role.SYSTEM, content=system_prompt))
