"""DoubaoProvider.chat_stream 与鉴权头守卫的单元测试。"""

import pytest
from pytest_httpx import IteratorStream

from symphony.ai.doubao import DoubaoProvider
from symphony.ai.schema import Message, Role

_ENDPOINT = "https://ark.cn-beijing.volces.com/api/v3/chat/completions"


def test_auth_headers_empty_key_raises():
    provider = DoubaoProvider(api_key="")
    with pytest.raises(ValueError, match="ARK_API_KEY"):
        provider._auth_headers()


def test_auth_headers_non_ascii_key_raises():
    provider = DoubaoProvider(api_key="密钥bad")
    with pytest.raises(ValueError, match="ASCII"):
        provider._auth_headers()


def test_auth_headers_valid_key():
    provider = DoubaoProvider(api_key="sk-abc123")
    headers = provider._auth_headers()
    assert headers["Authorization"] == "Bearer sk-abc123"


def test_model_name_empty_raises():
    provider = DoubaoProvider(api_key="sk-abc123", model="")
    with pytest.raises(ValueError, match="ARK_MODEL_ID"):
        provider._model_name()


def test_model_name_non_ascii_raises():
    provider = DoubaoProvider(api_key="sk-abc123", model="你的 model id")
    with pytest.raises(ValueError, match="ASCII"):
        provider._model_name()


@pytest.mark.asyncio
async def test_chat_stream_yields_content_deltas(httpx_mock):
    httpx_mock.add_response(
        url=_ENDPOINT,
        stream=IteratorStream(
            [
                b'data: {"choices":[{"delta":{"content":"Hello"}}]}\n\n',
                b'data: {"choices":[{"delta":{"content":" world"}}]}\n\n',
                b"data: [DONE]\n\n",
            ]
        ),
    )
    provider = DoubaoProvider(api_key="sk-abc123")
    texts = []
    async for delta in provider.chat_stream(
        messages=[Message(role=Role.USER, content="hi")]
    ):
        if delta.content:
            texts.append(delta.content)
    assert "".join(texts) == "Hello world"
    await provider.close()


@pytest.mark.asyncio
async def test_chat_stream_assembles_tool_calls(httpx_mock):
    httpx_mock.add_response(
        url=_ENDPOINT,
        stream=IteratorStream(
            [
                b'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_1",'
                b'"function":{"name":"echo","arguments":"{\\"x\\":"}}]}}]}\n\n',
                b'data: {"choices":[{"delta":{"tool_calls":[{"index":0,'
                b'"function":{"arguments":"1}"}}]}}]}\n\n',
                b"data: [DONE]\n\n",
            ]
        ),
    )
    provider = DoubaoProvider(api_key="sk-abc123")
    tool_calls = []
    async for delta in provider.chat_stream(
        messages=[Message(role=Role.USER, content="use tool")]
    ):
        if delta.tool_calls:
            tool_calls = delta.tool_calls
    assert tool_calls[0].name == "echo"
    assert tool_calls[0].arguments == {"x": 1}
    await provider.close()


@pytest.mark.asyncio
async def test_chat_stream_http_error_includes_response_body(httpx_mock):
    httpx_mock.add_response(
        url=_ENDPOINT,
        status_code=400,
        json={"error": {"message": "model not found"}},
    )
    provider = DoubaoProvider(api_key="sk-abc123")

    with pytest.raises(RuntimeError, match="model not found"):
        async for _ in provider.chat_stream(
            messages=[Message(role=Role.USER, content="hi")]
        ):
            pass

    await provider.close()
