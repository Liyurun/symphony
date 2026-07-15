"""ai.doubao 模块的单元测试。

使用 pytest-httpx 的 httpx_mock fixture mock Ark /chat/completions 接口，
验证 DoubaoProvider.chat 对普通文本响应与 tool_calls 响应的解析能力。
"""

import pytest

from symphony.ai.doubao import DoubaoProvider
from symphony.ai.schema import Message, Role

# Ark chat/completions 完整接口地址，用于 mock 匹配
_ENDPOINT = "https://ark.cn-beijing.volces.com/api/v3/chat/completions"


@pytest.fixture
def provider():
    """构造一个使用测试 api_key 的 DoubaoProvider 实例。"""
    return DoubaoProvider(api_key="test-key")


@pytest.mark.asyncio
async def test_chat_completion(httpx_mock, provider):
    """普通文本响应：应正确解析 id、content 与 usage 统计。"""
    httpx_mock.add_response(
        url=_ENDPOINT,
        json={
            "id": "resp-123",
            "model": "doubao-pro-32k",
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "你好，我能帮你什么？",
                    }
                }
            ],
            "usage": {
                "prompt_tokens": 12,
                "completion_tokens": 8,
                "total_tokens": 20,
            },
        },
    )

    resp = await provider.chat(messages=[Message(role=Role.USER, content="你好")])

    assert resp.id == "resp-123"
    assert resp.choices[0].content == "你好，我能帮你什么？"
    assert resp.usage.total_tokens == 20

    await provider.close()


@pytest.mark.asyncio
async def test_tool_calls_parsing(httpx_mock, provider):
    """tool_calls 响应：function.arguments 为 JSON 字符串时应解析成 dict。"""
    httpx_mock.add_response(
        url=_ENDPOINT,
        json={
            "id": "resp-456",
            "model": "doubao-pro-32k",
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_abc",
                                "type": "function",
                                "function": {
                                    "name": "http_request",
                                    "arguments": '{"url": "https://example.com"}',
                                },
                            }
                        ],
                    }
                }
            ],
            "usage": {
                "prompt_tokens": 5,
                "completion_tokens": 3,
                "total_tokens": 8,
            },
        },
    )

    resp = await provider.chat(messages=[Message(role=Role.USER, content="请求一个网页")])

    assert resp.choices[0].tool_calls[0].name == "http_request"
    assert resp.choices[0].tool_calls[0].arguments == {"url": "https://example.com"}

    await provider.close()
