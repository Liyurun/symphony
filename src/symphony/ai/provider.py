"""LLM Provider 抽象基类定义。

约定所有大模型服务提供方需实现的统一异步接口，
上层业务只依赖该抽象接口，从而与具体厂商实现解耦。
"""

from abc import ABC, abstractmethod
from typing import AsyncIterator, Optional

from symphony.ai.schema import LLMResponse, Message, StreamDelta, ToolDef


class LLMProvider(ABC):
    """大模型服务提供方抽象基类。"""

    @abstractmethod
    async def chat(
        self,
        messages: list[Message],
        tools: Optional[list[ToolDef]] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        **kwargs,
    ) -> LLMResponse:
        """发起一次对话请求并返回模型响应。

        :param messages: 对话消息列表。
        :param tools: 可供模型调用的工具列表，可选。
        :param temperature: 采样温度，None 时使用实现的默认值。
        :param max_tokens: 最大生成 token 数，None 时使用实现的默认值。
        :param kwargs: 其他透传给底层接口的扩展参数。
        :return: 解析后的 :class:`LLMResponse` 实例。
        """
        ...

    @abstractmethod
    def chat_stream(
        self,
        messages: list[Message],
        tools: Optional[list[ToolDef]] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        **kwargs,
    ) -> AsyncIterator[StreamDelta]:
        """以流式方式发起一次对话，逐段产出 StreamDelta。"""
        ...
