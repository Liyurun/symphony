"""上下文压缩器测试。"""

from symphony.agent.context_compression import ContextCompressor
from symphony.ai.schema import Message, Role, ToolCall


def test_context_compressor_keeps_small_context_unchanged():
    """短上下文不应被压缩。"""
    messages = [
        Message(role=Role.SYSTEM, content="system"),
        Message(role=Role.USER, content="hello"),
        Message(role=Role.ASSISTANT, content="hi"),
    ]
    compressor = ContextCompressor(max_prompt_chars=10_000)

    result = compressor.compress(messages)

    assert result.compacted is False
    assert [message.content for message in result.messages] == ["system", "hello", "hi"]


def test_context_compressor_summarizes_old_messages_and_keeps_recent_tail():
    """长上下文应把旧消息折叠为 system summary，并保留最近消息。"""
    messages = [Message(role=Role.SYSTEM, content="system prompt")]
    for idx in range(10):
        messages.append(Message(role=Role.USER, content=f"user-{idx} " + "x" * 200))
        messages.append(Message(role=Role.ASSISTANT, content=f"assistant-{idx}"))
    compressor = ContextCompressor(
        max_prompt_chars=900,
        keep_recent_messages=4,
        min_recent_messages=2,
        summary_max_chars=500,
    )

    result = compressor.compress(messages)

    assert result.compacted is True
    assert result.messages[0].content == "system prompt"
    assert "较早上下文已压缩" in (result.messages[1].content or "")
    assert result.messages[-1].content == "assistant-9"
    assert result.messages[-2].content.startswith("user-9")
    assert len(messages) == 21


def test_context_compressor_does_not_start_recent_tail_with_orphan_tool_message():
    """压缩后保留区不能从孤立 tool 消息开始。"""
    messages = [
        Message(role=Role.SYSTEM, content="system"),
        Message(role=Role.USER, content="old " + "x" * 1000),
        Message(
            role=Role.ASSISTANT,
            content=None,
            tool_calls=[ToolCall(id="c1", name="file_read", arguments={"path": "a"})],
        ),
        Message(role=Role.TOOL, tool_call_id="c1", content="tool result"),
        Message(role=Role.USER, content="latest"),
    ]
    compressor = ContextCompressor(
        max_prompt_chars=900,
        keep_recent_messages=2,
        min_recent_messages=2,
    )

    result = compressor.compress(messages)

    roles = [message.role for message in result.messages]
    assert roles[-3:] == [Role.ASSISTANT, Role.TOOL, Role.USER]
    assert result.messages[-3].tool_calls[0].id == "c1"


def test_context_compressor_truncates_large_recent_message_without_mutating_original():
    """最近的大工具结果应裁剪发送给模型，但不修改原始消息。"""
    long_content = "z" * 500
    messages = [
        Message(role=Role.SYSTEM, content="system"),
        Message(role=Role.USER, content="old " + "x" * 1000),
        Message(role=Role.TOOL, tool_call_id="c1", content=long_content),
        Message(role=Role.USER, content="next"),
    ]
    compressor = ContextCompressor(
        max_prompt_chars=500,
        keep_recent_messages=2,
        min_recent_messages=2,
        max_message_chars=80,
    )

    result = compressor.compress(messages)

    tool_message = next(message for message in result.messages if message.role == Role.TOOL)
    assert "truncated" in (tool_message.content or "")
    assert messages[2].content == long_content
