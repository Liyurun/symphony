"""火山方舟（Doubao/Ark）大模型 Provider 实现。

基于 httpx.AsyncClient 调用 Ark 的 /chat/completions 接口，
将内部 Message 模型转成 API 请求体，并把响应解析回 LLMResponse。
"""

import json
import uuid
from typing import AsyncIterator, Optional

import httpx

from symphony.ai.provider import LLMProvider
from symphony.ai.schema import (
    LLMResponse,
    Message,
    Role,
    StreamDelta,
    ToolCall,
    ToolDef,
    Usage,
)


class DoubaoProvider(LLMProvider):
    """调用火山方舟接口的 LLM Provider。"""

    def __init__(
        self,
        api_key: str,
        model: str = "doubao-pro-32k",
        base_url: str = "https://ark.cn-beijing.volces.com/api/v3",
        temperature: float = 0.7,
        max_tokens: int = 4096,
        timeout: float = 120.0,
    ):
        """初始化 Provider 并创建异步 HTTP 客户端。

        :param api_key: 访问 Ark 服务的 API Key。
        :param model: 默认使用的模型名称。
        :param base_url: Ark 服务基础 URL。
        :param temperature: 默认采样温度。
        :param max_tokens: 默认最大生成 token 数。
        :param timeout: HTTP 请求超时时间（秒）。
        """
        # 保存鉴权与默认参数
        self.api_key = api_key
        self.model = model
        # 去掉尾部斜杠，避免拼接出双斜杠
        self.base_url = base_url.rstrip("/")
        self.temperature = temperature
        self.max_tokens = max_tokens
        # 创建可复用的异步 HTTP 客户端
        self.client = httpx.AsyncClient(timeout=timeout)

    def _auth_headers(self) -> dict[str, str]:
        """构造鉴权头，并在 key 缺失或含非 ASCII 字符时给出可操作错误。"""
        key = (self.api_key or "").strip()
        if not key:
            raise ValueError(
                "缺少 ARK_API_KEY：请设置环境变量或在 config.local.yaml 填写 llm.api_key。"
            )
        try:
            key.encode("ascii")
        except UnicodeEncodeError:
            raise ValueError(
                "ARK_API_KEY 含非 ASCII 字符，可能复制了中文引号或多余空白，请重新粘贴纯英文数字 key。"
            )
        return {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}

    def _model_name(self) -> str:
        """返回可发送给 Ark 的模型/endpoint id，并校验常见配置错误。"""
        model = (self.model or "").strip()
        if not model:
            raise ValueError(
                "缺少 LLM 模型：请设置 ARK_MODEL_ID，或在 config.local.yaml 中填写 llm.model。"
            )
        try:
            model.encode("ascii")
        except UnicodeEncodeError:
            raise ValueError(
                "ARK_MODEL_ID 含非 ASCII 字符，可能仍是占位文字，请填写火山方舟真实 endpoint/model id。"
            )
        return model

    @staticmethod
    def _response_error_detail(resp: httpx.Response, body: str) -> str:
        """把 Ark 非 2xx 响应转成便于 TUI 展示的错误详情。"""
        detail = body.strip()
        if not detail:
            detail = resp.reason_phrase
        return f"Ark request failed ({resp.status_code}): {detail}"

    async def chat(
        self,
        messages: list[Message],
        tools: Optional[list[ToolDef]] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        **kwargs,
    ) -> LLMResponse:
        """调用 Ark /chat/completions 接口并解析响应。

        temperature / max_tokens 传入非 None 时覆盖实例默认值。
        """
        # 构造请求体：消息转成 API 兼容字典
        payload = {
            "model": self._model_name(),
            "messages": [message.to_api_dict() for message in messages],
            "temperature": temperature if temperature is not None else self.temperature,
            "max_tokens": max_tokens if max_tokens is not None else self.max_tokens,
        }
        # 存在工具定义时转换并加入请求体
        if tools is not None:
            payload["tools"] = [tool.model_dump() for tool in tools]
        # 透传其余扩展参数
        payload.update(kwargs)

        # 组装鉴权与内容类型请求头
        headers = self._auth_headers()

        # 发起 POST 请求并在非 2xx 时抛出异常（网络边界，允许抛出）
        resp = await self.client.post(
            f"{self.base_url}/chat/completions", json=payload, headers=headers
        )
        if not resp.is_success:
            raise RuntimeError(self._response_error_detail(resp, resp.text))
        data = resp.json()

        # 解析每个候选回复为 Message
        choices: list[Message] = []
        for choice in data.get("choices", []):
            message_data = choice.get("message", {})
            # 解析工具调用（若存在）
            tool_calls = self._parse_tool_calls(message_data.get("tool_calls"))
            choices.append(
                Message(
                    role=Role(message_data.get("role", "assistant")),
                    content=message_data.get("content"),
                    tool_calls=tool_calls,
                )
            )

        # 解析 token 用量统计
        usage_data = data.get("usage", {})
        usage = Usage(
            prompt_tokens=usage_data.get("prompt_tokens", 0),
            completion_tokens=usage_data.get("completion_tokens", 0),
            total_tokens=usage_data.get("total_tokens", 0),
        )

        return LLMResponse(
            id=data.get("id", ""),
            choices=choices,
            usage=usage,
            model=data.get("model", self.model),
        )

    async def chat_stream(
        self,
        messages: list[Message],
        tools: Optional[list[ToolDef]] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        **kwargs,
    ) -> AsyncIterator[StreamDelta]:
        """调用 Ark 流式接口，逐段产出内容增量；工具调用在末尾组装后一次性给出。"""
        payload = {
            "model": self._model_name(),
            "messages": [message.to_api_dict() for message in messages],
            "temperature": temperature if temperature is not None else self.temperature,
            "max_tokens": max_tokens if max_tokens is not None else self.max_tokens,
            "stream": True,
        }
        if tools is not None:
            payload["tools"] = [tool.model_dump() for tool in tools]
        payload.update(kwargs)

        headers = self._auth_headers()
        # 按 index 累积工具调用分片：index -> {id, name, arguments(str)}
        tool_fragments: dict[int, dict] = {}

        async with self.client.stream(
            "POST",
            f"{self.base_url}/chat/completions",
            json=payload,
            headers=headers,
        ) as resp:
            if not resp.is_success:
                body = (await resp.aread()).decode("utf-8", errors="replace")
                raise RuntimeError(self._response_error_detail(resp, body))
            async for line in resp.aiter_lines():
                line = line.strip()
                if not line or not line.startswith("data:"):
                    continue
                data = line[len("data:") :].strip()
                if data == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                except json.JSONDecodeError:
                    continue
                choices = chunk.get("choices") or []
                if not choices:
                    continue
                delta = choices[0].get("delta") or {}
                content = delta.get("content")
                if content:
                    yield StreamDelta(content=content)
                for frag in delta.get("tool_calls") or []:
                    self._accumulate_fragment(tool_fragments, frag)

        assembled = self._assemble_tool_calls(tool_fragments)
        if assembled:
            yield StreamDelta(tool_calls=assembled)

    @staticmethod
    def _accumulate_fragment(store: dict[int, dict], frag: dict) -> None:
        """把一个流式 tool_call 分片按 index 累积到 store。"""
        index = frag.get("index", 0)
        slot = store.setdefault(index, {"id": None, "name": None, "arguments": ""})
        if frag.get("id"):
            slot["id"] = frag["id"]
        function = frag.get("function") or {}
        if function.get("name"):
            slot["name"] = function["name"]
        if function.get("arguments"):
            slot["arguments"] += function["arguments"]

    @staticmethod
    def _assemble_tool_calls(store: dict[int, dict]) -> list[ToolCall]:
        """把累积的分片组装为 ToolCall 列表，arguments 解析为 dict。"""
        import uuid

        calls: list[ToolCall] = []
        for _, slot in sorted(store.items()):
            raw_args = slot["arguments"]
            try:
                arguments = json.loads(raw_args) if raw_args else {}
            except json.JSONDecodeError:
                arguments = {"_raw": raw_args}
            calls.append(
                ToolCall(
                    id=slot["id"] or str(uuid.uuid4()),
                    name=slot["name"] or "",
                    arguments=arguments,
                )
            )
        return calls

    @staticmethod
    def _parse_tool_calls(raw_tool_calls) -> Optional[list[ToolCall]]:
        """将 API 返回的 tool_calls 原始结构解析为 ToolCall 列表。

        function.arguments 可能是 JSON 字符串，需要解析成 dict；
        缺失 id 时用 uuid4 兜底生成。
        """
        # 没有工具调用时直接返回 None
        if not raw_tool_calls:
            return None
        parsed: list[ToolCall] = []
        for raw in raw_tool_calls:
            function = raw.get("function", {})
            arguments = function.get("arguments", {})
            # arguments 为字符串时按 JSON 解析（JSON 解析边界，允许 try）
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments) if arguments else {}
                except json.JSONDecodeError:
                    # 解析失败则退化为原始字符串包装，避免整体失败
                    arguments = {"_raw": arguments}
            parsed.append(
                ToolCall(
                    id=raw.get("id") or str(uuid.uuid4()),
                    name=function.get("name", ""),
                    arguments=arguments,
                )
            )
        return parsed

    async def close(self):
        """关闭底层 HTTP 客户端，释放连接资源。"""
        await self.client.aclose()
