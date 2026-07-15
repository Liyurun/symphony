"""AI 层的数据模型定义。

使用 Pydantic v2 定义与大模型交互所需的消息、工具、请求、响应等强类型模型，
并保持与 OpenAI / 火山方舟（Ark）兼容的字段结构，便于各 Provider 复用。
"""

import json
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel


class Role(str, Enum):
    """消息角色枚举，取值与 OpenAI/Ark 接口保持一致。"""

    # 系统消息，用于设定模型行为
    SYSTEM = "system"
    # 用户消息，来自终端用户的输入
    USER = "user"
    # 助手消息，模型生成的回复
    ASSISTANT = "assistant"
    # 工具消息，携带工具执行结果
    TOOL = "tool"


class ToolCall(BaseModel):
    """模型发起的一次工具调用。"""

    # 工具调用的唯一标识
    id: str
    # 被调用的函数名称
    name: str
    # 调用参数，已解析为字典结构
    arguments: dict[str, Any]


class FunctionDef(BaseModel):
    """可供模型调用的函数定义。"""

    # 函数名称
    name: str
    # 函数用途描述，供模型理解何时调用
    description: str
    # 参数的 JSON Schema 定义
    parameters: dict[str, Any]


class ToolDef(BaseModel):
    """工具定义，当前仅支持 function 类型。"""

    # 工具类型，固定为 "function"
    type: str = "function"
    # 具体的函数定义
    function: FunctionDef


class Message(BaseModel):
    """对话消息，覆盖 system/user/assistant/tool 四类角色。"""

    # 消息角色
    role: Role
    # 文本内容，工具调用型助手消息可能为 None
    content: Optional[str] = None
    # 助手发起的工具调用列表
    tool_calls: Optional[list[ToolCall]] = None
    # 工具消息对应的工具调用 id
    tool_call_id: Optional[str] = None

    def to_api_dict(self) -> dict:
        """转换为 OpenAI/Ark 兼容的请求字典。

        规则：始终包含 role；content 非 None 时加入；
        tool_calls 存在时转成标准结构；tool_call_id 存在时加入。
        """
        # role 使用枚举的字符串值，保证序列化为 "system"/"user" 等
        result: dict[str, Any] = {"role": self.role.value}
        # 仅当 content 非 None 时加入（工具调用消息可能没有正文）
        if self.content is not None:
            result["content"] = self.content
        # 工具调用列表转成 OpenAI/Ark 标准结构；function.arguments 必须是 JSON 字符串
        if self.tool_calls is not None:
            result["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {
                        "name": call.name,
                        "arguments": json.dumps(call.arguments, ensure_ascii=False, default=str),
                    },
                }
                for call in self.tool_calls
            ]
        # 工具消息需回传其对应的工具调用 id
        if self.tool_call_id is not None:
            result["tool_call_id"] = self.tool_call_id
        return result


class StreamDelta(BaseModel):
    """流式响应中的一个增量片段。

    content 为增量文本（可能为 None）；tool_calls 在流结束、
    工具调用组装完成时一次性给出。
    """

    content: Optional[str] = None
    tool_calls: Optional[list[ToolCall]] = None


class Usage(BaseModel):
    """一次请求的 token 用量统计。"""

    # 提示词消耗的 token 数
    prompt_tokens: int = 0
    # 补全内容消耗的 token 数
    completion_tokens: int = 0
    # 总消耗 token 数
    total_tokens: int = 0


class LLMRequest(BaseModel):
    """一次大模型请求的参数集合。"""

    # 对话消息列表
    messages: list[Message]
    # 使用的模型名称
    model: str
    # 采样温度
    temperature: float = 0.7
    # 单次生成的最大 token 数
    max_tokens: int = 4096
    # 可供模型调用的工具列表
    tools: Optional[list[ToolDef]] = None
    # 是否使用流式响应
    stream: bool = False


class LLMResponse(BaseModel):
    """一次大模型请求的响应结果。"""

    # 响应唯一标识
    id: str
    # 候选回复消息列表
    choices: list[Message]
    # token 用量统计
    usage: Usage
    # 实际使用的模型名称
    model: str
