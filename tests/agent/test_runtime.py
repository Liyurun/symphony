"""AgentRuntime（ReAct 循环）的单元测试。

用 unittest.mock.AsyncMock 模拟 LLMProvider，覆盖两类核心场景：
1. LLM 直接返回符合 schema 的 JSON（无工具调用）；
2. LLM 先发起工具调用，观察结果后再返回最终 JSON。
"""

import pytest
from unittest.mock import AsyncMock

from symphony.ai.schema import Message, Role, ToolCall, LLMResponse, Usage
from symphony.agent.context_compression import ContextCompressor
from symphony.agent.context import AgentContext
from symphony.agent.runtime import AgentRuntime
from symphony.skills.registry import SkillRegistry
from symphony.skills.base import Skill, SkillContext


class MockEchoSkill(Skill):
    """回显技能，把入参中的 text 原样放到 echo 字段返回。"""

    name = "echo"
    description = "Echo"
    input_schema = {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}
    output_schema = {"type": "object"}

    async def execute(self, args, context):
        """回显 text。"""
        return {"echo": args["text"]}


@pytest.fixture
def mock_provider():
    """构造一个带 model 属性的 AsyncMock provider。"""
    p = AsyncMock()
    p.model = "test-model"
    return p


def _make_response(content=None, tool_calls=None):
    """构造一条包含单个 assistant choice 的 LLMResponse。"""
    msg = Message(role=Role.ASSISTANT, content=content, tool_calls=tool_calls)
    return LLMResponse(id="resp-1", choices=[msg], usage=Usage(), model="test-model")


def test_agent_direct_response(mock_provider):
    """LLM 直接返回合法 JSON 时，run 应返回解析结果并发出思考/完成事件。"""
    mock_provider.chat.return_value = _make_response(content='{"result":"done"}')

    events = []
    registry = SkillRegistry()
    output_schema = {"type": "object", "properties": {"result": {"type": "string"}}, "required": ["result"]}
    runtime = AgentRuntime(
        llm_provider=mock_provider,
        skill_registry=registry,
        system_prompt="你是一个助手。",
        output_schema=output_schema,
        on_event=events.append,
    )
    ctx = AgentContext(task_id="t1", node_id="n1")

    import asyncio

    result = asyncio.run(runtime.run("请开始", ctx))

    assert result == {"result": "done"}
    event_types = [e.type for e in events]
    assert "agent_thought" in event_types
    assert "node_completed" in event_types


def test_agent_tool_call_loop(mock_provider):
    """LLM 先调用技能、观察结果后再返回 JSON，run 应正确完成整个循环。"""
    first = _make_response(
        tool_calls=[ToolCall(id="call-1", name="echo", arguments={"text": "hi"})]
    )
    second = _make_response(content='{"result":"hi"}')
    mock_provider.chat.side_effect = [first, second]

    events = []
    registry = SkillRegistry()
    registry.register(MockEchoSkill())
    output_schema = {"type": "object", "properties": {"result": {"type": "string"}}, "required": ["result"]}
    runtime = AgentRuntime(
        llm_provider=mock_provider,
        skill_registry=registry,
        system_prompt="你是一个助手。",
        output_schema=output_schema,
        on_event=events.append,
    )
    ctx = AgentContext(task_id="t1", node_id="n1")

    import asyncio

    result = asyncio.run(runtime.run("请回显 hi", ctx))

    assert result == {"result": "hi"}
    assert mock_provider.chat.call_count == 2
    event_types = [e.type for e in events]
    assert "skill_called" in event_types


def test_agent_records_trace(mock_provider):
    """每次 LLM 调用后应通过 on_trace 记录一条含请求/响应/用量的轨迹。"""
    mock_provider.chat.return_value = _make_response(content='{"result":"done"}')

    traces = []
    registry = SkillRegistry()
    output_schema = {"type": "object", "properties": {"result": {"type": "string"}}, "required": ["result"]}
    runtime = AgentRuntime(
        llm_provider=mock_provider,
        skill_registry=registry,
        system_prompt="你是一个助手。",
        output_schema=output_schema,
        on_event=lambda e: None,
        on_trace=traces.append,
    )
    ctx = AgentContext(task_id="t1", node_id="n1")

    import asyncio

    asyncio.run(runtime.run("请开始", ctx))

    # 至少记录了一条轨迹，含节点 id、请求消息、响应与用量
    assert len(traces) >= 1
    tr = traces[0]
    assert tr["node_id"] == "n1"
    assert isinstance(tr["request_messages"], list) and len(tr["request_messages"]) >= 1
    assert tr["response"]["content"] == '{"result":"done"}'
    assert "total_tokens" in tr["usage"]


def test_agent_runtime_compacts_request_without_mutating_context(mock_provider):
    """SOP 节点请求应可压缩，但 context.messages 保留完整历史。"""
    mock_provider.chat.return_value = _make_response(content='{"result":"done"}')

    traces = []
    runtime = AgentRuntime(
        llm_provider=mock_provider,
        skill_registry=SkillRegistry(),
        system_prompt="system",
        output_schema={
            "type": "object",
            "properties": {"result": {"type": "string"}},
            "required": ["result"],
        },
        on_event=lambda e: None,
        on_trace=traces.append,
        context_compressor=ContextCompressor(
            max_prompt_chars=700,
            keep_recent_messages=2,
            min_recent_messages=1,
        ),
    )
    ctx = AgentContext(task_id="t1", node_id="n1")
    ctx.messages = [Message(role=Role.SYSTEM, content="system")]
    for idx in range(8):
        ctx.add_message(Message(role=Role.USER, content=f"old-{idx} " + "x" * 200))
    before_len = len(ctx.messages)

    import asyncio

    result = asyncio.run(runtime.run("ignored", ctx, reset=False))

    assert result == {"result": "done"}
    sent_messages = mock_provider.chat.call_args.kwargs["messages"]
    assert any("较早上下文已压缩" in (message.content or "") for message in sent_messages)
    assert len(ctx.messages) == before_len + 1
    assert traces[0]["context_compaction"]["compacted"] is True
