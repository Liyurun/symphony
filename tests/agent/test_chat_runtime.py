"""ChatRuntime.stream 的单元测试。"""

import pytest

from symphony.agent.context_compression import ContextCompressor
from symphony.agent.chat_runtime import ChatRuntime
from symphony.ai.schema import StreamDelta, ToolCall
from symphony.skills.base import Skill, SkillContext
from symphony.skills.references import SkillReferenceIndex
from symphony.skills.registry import SkillRegistry


class EchoSkill(Skill):
    name = "echo"
    description = "回显"
    input_schema = {"type": "object", "properties": {"x": {"type": "integer"}}}

    async def execute(self, args, ctx: SkillContext):
        return {"echo": args.get("x")}


class ScriptedProvider:
    """按预置脚本产出流式增量的假 provider。"""

    def __init__(self, scripts: list[list[StreamDelta]]):
        # 每次调用 chat_stream 消费一段脚本
        self._scripts = scripts
        self._calls = 0

    async def chat_stream(self, messages, tools=None, **kwargs):
        script = self._scripts[self._calls]
        self._calls += 1
        for delta in script:
            yield delta


@pytest.mark.asyncio
async def test_stream_plain_answer():
    provider = ScriptedProvider(
        [[StreamDelta(content="Hello"), StreamDelta(content=" there")]]
    )
    runtime = ChatRuntime(provider, SkillRegistry())
    types = []
    answer = None
    async for ev in runtime.stream("hi", []):
        types.append(ev.type)
        if ev.type == "chat_completed":
            answer = ev.answer
    assert types == ["chat_answer_delta", "chat_answer_delta", "chat_completed"]
    assert answer == "Hello there"


@pytest.mark.asyncio
async def test_stream_runs_tool_then_answers():
    reg = SkillRegistry()
    reg.register(EchoSkill())
    provider = ScriptedProvider(
        [
            [StreamDelta(tool_calls=[ToolCall(id="c1", name="echo", arguments={"x": 5})])],
            [StreamDelta(content="done")],
        ]
    )
    runtime = ChatRuntime(provider, reg)
    events = [ev async for ev in runtime.stream("use tool", [])]
    types = [ev.type for ev in events]
    assert types == [
        "chat_thinking",
        "chat_tool_call",
        "chat_tool_result",
        "chat_answer_delta",
        "chat_completed",
    ]
    assert events[1].summary == '{"x": 5}'
    assert events[2].detail == '{"echo": 5}'


@pytest.mark.asyncio
async def test_stream_falls_back_to_final_answer_after_tool_loop_limit():
    """工具循环达到上限后，应无工具兜底生成最终回答。"""
    reg = SkillRegistry()
    reg.register(EchoSkill())

    class LoopingThenFinalProvider:
        def __init__(self) -> None:
            self.calls: list[list[str]] = []

        async def chat_stream(self, messages, tools=None, **kwargs):
            self.calls.append([tool.function.name for tool in tools or []])
            if tools:
                yield StreamDelta(
                    tool_calls=[ToolCall(id="c1", name="echo", arguments={"x": 1})]
                )
                return
            yield StreamDelta(content="final answer")

    provider = LoopingThenFinalProvider()
    runtime = ChatRuntime(provider, reg, max_iterations=2)

    events = [ev async for ev in runtime.stream("please answer", [])]

    assert events[-1].type == "chat_completed"
    assert events[-1].answer == "final answer"
    assert provider.calls == [["echo"], ["echo"], []]
    assert "chat_failed" not in [ev.type for ev in events]


@pytest.mark.asyncio
async def test_stream_includes_history():
    captured = {}

    class CapturingProvider(ScriptedProvider):
        async def chat_stream(self, messages, tools=None, **kwargs):
            captured["contents"] = [m.content for m in messages]
            async for delta in super().chat_stream(messages, tools, **kwargs):
                yield delta

    provider = CapturingProvider([[StreamDelta(content="ok")]])
    runtime = ChatRuntime(provider, SkillRegistry())
    _ = [ev async for ev in runtime.stream("now", [{"role": "user", "content": "past"}])]
    # system + past + now
    assert captured["contents"][-2:] == ["past", "now"]
    assert "Pi Agent" in (captured["contents"][0] or "")


@pytest.mark.asyncio
async def test_stream_compacts_long_history_before_llm_call():
    """长历史发给模型前应压缩为摘要 + 最近消息。"""
    captured = {}

    class CapturingProvider(ScriptedProvider):
        async def chat_stream(self, messages, tools=None, **kwargs):
            captured["messages"] = messages
            async for delta in super().chat_stream(messages, tools, **kwargs):
                yield delta

    history = [
        {"role": "user", "content": f"old user {idx} " + "x" * 300}
        for idx in range(8)
    ]
    provider = CapturingProvider([[StreamDelta(content="ok")]])
    runtime = ChatRuntime(
        provider,
        SkillRegistry(),
        context_compressor=ContextCompressor(
            max_prompt_chars=900,
            keep_recent_messages=3,
            min_recent_messages=2,
        ),
    )

    _ = [ev async for ev in runtime.stream("now", history)]

    sent = captured["messages"]
    assert any("较早上下文已压缩" in (message.content or "") for message in sent)
    assert sent[-1].content == "now"
    assert len(sent) < len(history) + 2


@pytest.mark.asyncio
async def test_stream_injects_tool_guidance():
    """存在 Skill 时，系统提示应包含工具使用规则和工具名。"""
    captured = {}

    class CapturingProvider(ScriptedProvider):
        async def chat_stream(self, messages, tools=None, **kwargs):
            captured["system"] = messages[0].content
            captured["tool_names"] = [tool.function.name for tool in tools or []]
            async for delta in super().chat_stream(messages, tools, **kwargs):
                yield delta

    reg = SkillRegistry()
    reg.register(EchoSkill())
    provider = CapturingProvider([[StreamDelta(content="ok")]])
    runtime = ChatRuntime(provider, reg)

    _ = [ev async for ev in runtime.stream("use available tools", [])]

    assert "工具使用规则" in captured["system"]
    assert "echo" in captured["system"]
    assert captured["tool_names"] == ["echo"]


@pytest.mark.asyncio
async def test_stream_injects_matching_external_skill_reference(tmp_path):
    """用户问题命中外部 SKILL.md 时，应把参考资料注入系统提示。"""
    skill_dir = tmp_path / "bytedance-log"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        """---
name: bytedance-log
description: 查询服务日志、LogID 和 pod 日志。
---

Use this skill when users need to search logs.
""",
        encoding="utf-8",
    )
    index = SkillReferenceIndex.from_roots([tmp_path])
    captured = {}

    class CapturingProvider(ScriptedProvider):
        async def chat_stream(self, messages, tools=None, **kwargs):
            captured["system"] = messages[0].content
            async for delta in super().chat_stream(messages, tools, **kwargs):
                yield delta

    provider = CapturingProvider([[StreamDelta(content="ok")]])
    runtime = ChatRuntime(provider, SkillRegistry(), skill_reference_index=index)

    _ = [ev async for ev in runtime.stream("帮我查日志", [])]

    assert "外部 Skill 参考资料" in captured["system"]
    assert "bytedance-log" in captured["system"]
    assert "不代表 Symphony 已具备对应执行能力" in captured["system"]


@pytest.mark.asyncio
async def test_stream_injects_skill_inventory_for_capability_questions(tmp_path):
    """能力清单类问题应自动注入可执行工具和外部参考摘要。"""
    skill_dir = tmp_path / "external-one"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        """---
name: external-one
description: external reference
---
""",
        encoding="utf-8",
    )
    skill_dir = tmp_path / "external-two"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        """---
name: external-two
description: second external reference
---
""",
        encoding="utf-8",
    )
    index = SkillReferenceIndex.from_roots([tmp_path])
    captured = {}

    class CapturingProvider(ScriptedProvider):
        async def chat_stream(self, messages, tools=None, **kwargs):
            captured["system"] = messages[0].content
            async for delta in super().chat_stream(messages, tools, **kwargs):
                yield delta

    reg = SkillRegistry()
    reg.register(EchoSkill())
    provider = CapturingProvider([[StreamDelta(content="ok")]])
    runtime = ChatRuntime(
        provider,
        reg,
        skill_reference_index=index,
        skill_reference_limit=1,
    )

    _ = [ev async for ev in runtime.stream("你有哪些能力", [])]

    assert "当前 Symphony Skill 清单" in captured["system"]
    assert "executable_skills" in captured["system"]
    assert "echo" in captured["system"]
    assert "external-one" in captured["system"]
    assert "external-two" not in captured["system"]
