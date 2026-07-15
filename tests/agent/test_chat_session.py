"""ChatSessionRunner 测试。"""

from pathlib import Path
from typing import Any

from symphony.agent.chat_session import ChatSessionRunner
from symphony.ai.schema import StreamDelta, ToolCall
from symphony.skills.base import Skill, SkillContext
from symphony.skills.registry import SkillRegistry
from symphony.storage import SessionManager


class FakeProvider:
    """流式 fake provider。"""

    model = "fake-model"

    async def chat_stream(self, messages, tools=None, **kwargs):
        """分两段返回最终回答。"""
        yield StreamDelta(content="Hello")
        yield StreamDelta(content=" world")


async def test_chat_session_runner_persists_transcript_events_and_traces(
    tmp_path: Path,
):
    """runner 应保存用户输入、最终回答、事件与 trace。"""
    sessions = SessionManager(tmp_path)
    meta = sessions.create_chat(title="Hi", source="test")
    runner = ChatSessionRunner(FakeProvider(), SkillRegistry(), sessions)

    events = []
    async for event in runner.stream(meta.session_id, "hi", []):
        events.append(event.to_dict())

    log = sessions.require(meta.session_id)
    transcript = log.read_transcript()
    persisted_events = log.read_events()
    traces = log.read_traces()

    assert transcript[0]["role"] == "user"
    assert transcript[0]["content"] == "hi"
    assert transcript[-1]["role"] == "assistant"
    assert transcript[-1]["content"] == "Hello world"
    assert events[-1]["type"] == "chat_completed"
    assert [e["type"] for e in persisted_events] == [
        "chat_started",
        "chat_user_message",
        "chat_answer_completed",
        "chat_completed",
    ]
    assert traces[0]["session_id"] == meta.session_id
    assert traces[0]["model"] == "fake-model"
    assert traces[0]["response"]["content"] == "Hello world"
    assert log.load_meta()["status"] == "completed"


class ToolProvider:
    """先产生工具调用，再返回最终答案。"""

    model = "fake-model"

    def __init__(self, tool_name: str):
        """保存待调用工具名。"""
        self.tool_name = tool_name
        self.calls = 0

    async def chat_stream(self, messages, tools=None, **kwargs):
        """第一轮调用工具，第二轮返回最终答案。"""
        self.calls += 1
        if self.calls == 1:
            yield StreamDelta(
                tool_calls=[
                    ToolCall(
                        id="tc-1",
                        name=self.tool_name,
                        arguments={"value": "hi"},
                    )
                ]
            )
        else:
            yield StreamDelta(content="done")


class EchoSkill(Skill):
    """用于验证工具成功事件的测试技能。"""

    name = "echo"
    description = "Return value"
    input_schema = {
        "type": "object",
        "properties": {"value": {"type": "string"}},
        "required": ["value"],
    }

    async def execute(self, args: dict[str, Any], context: SkillContext) -> Any:
        """返回输入参数。"""
        return {"echo": args["value"]}


async def test_chat_session_runner_persists_tool_returned_events(tmp_path: Path):
    """工具调用和成功结果应进入事件日志。"""
    sessions = SessionManager(tmp_path)
    meta = sessions.create_chat(title="Tool", source="test")
    registry = SkillRegistry()
    registry.register(EchoSkill())
    runner = ChatSessionRunner(ToolProvider("echo"), registry, sessions)

    async for _ in runner.stream(meta.session_id, "run tool", []):
        pass

    events = sessions.require(meta.session_id).read_events()
    types = [event["type"] for event in events]

    assert "tool_called" in types
    assert "tool_returned" in types
    assert events[-2]["type"] == "chat_answer_completed"


async def test_chat_session_runner_persists_tool_failed_events(tmp_path: Path):
    """工具调用失败时应记录 tool_failed。"""
    sessions = SessionManager(tmp_path)
    meta = sessions.create_chat(title="Tool", source="test")
    runner = ChatSessionRunner(ToolProvider("missing_tool"), SkillRegistry(), sessions)

    async for _ in runner.stream(meta.session_id, "run tool", []):
        pass

    types = [event["type"] for event in sessions.require(meta.session_id).read_events()]
    assert "tool_called" in types
    assert "tool_failed" in types
