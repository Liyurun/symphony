"""ai.schema 模块的单元测试。

验证 Message、ToolCall、Usage、LLMResponse 等模型的基本创建，
以及 Message.to_api_dict 转换为 OpenAI/Ark 兼容字典的正确性。
"""

from symphony.ai.schema import Message, ToolCall, LLMResponse, Usage, Role


def test_message_creation():
    """普通用户消息创建：role 与 content 应正确赋值，工具字段为默认 None。"""
    msg = Message(role=Role.USER, content="你好")

    assert msg.role == Role.USER
    assert msg.content == "你好"
    assert msg.tool_calls is None
    assert msg.tool_call_id is None


def test_message_with_tool_calls():
    """带 tool_calls 的助手消息：应正确保存工具调用列表。"""
    tool_call = ToolCall(id="call_1", name="http_request", arguments={"url": "https://example.com"})
    msg = Message(role=Role.ASSISTANT, tool_calls=[tool_call])

    assert msg.role == Role.ASSISTANT
    assert msg.tool_calls is not None
    assert len(msg.tool_calls) == 1
    assert msg.tool_calls[0].name == "http_request"
    assert msg.tool_calls[0].arguments == {"url": "https://example.com"}


def test_llm_response_usage():
    """LLMResponse 中的 Usage 统计字段应正确保存。"""
    usage = Usage(prompt_tokens=10, completion_tokens=5, total_tokens=15)
    resp = LLMResponse(
        id="resp-1",
        choices=[Message(role=Role.ASSISTANT, content="回答")],
        usage=usage,
        model="doubao-pro-32k",
    )

    assert resp.usage.prompt_tokens == 10
    assert resp.usage.completion_tokens == 5
    assert resp.usage.total_tokens == 15
    assert resp.choices[0].content == "回答"


def test_message_to_dict():
    """to_api_dict：system 消息应转出 role="system" 与正确 content。"""
    msg = Message(role=Role.SYSTEM, content="你是一个助手")
    api_dict = msg.to_api_dict()

    assert api_dict["role"] == "system"
    assert api_dict["content"] == "你是一个助手"
    # 未设置工具相关字段时不应出现在字典中
    assert "tool_calls" not in api_dict
    assert "tool_call_id" not in api_dict


def test_message_to_dict_with_tool_calls():
    """to_api_dict：带 tool_calls 的消息应转成 OpenAI/Ark 兼容结构。"""
    tool_call = ToolCall(id="call_1", name="http_request", arguments={"url": "https://example.com"})
    msg = Message(role=Role.ASSISTANT, tool_calls=[tool_call])
    api_dict = msg.to_api_dict()

    assert api_dict["role"] == "assistant"
    assert api_dict["tool_calls"][0]["id"] == "call_1"
    assert api_dict["tool_calls"][0]["type"] == "function"
    assert api_dict["tool_calls"][0]["function"]["name"] == "http_request"
    assert api_dict["tool_calls"][0]["function"]["arguments"] == '{"url": "https://example.com"}'
