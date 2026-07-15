"""agent.tools 共享助手的单元测试。"""

import pytest

from symphony.agent.tools import build_tool_defs, build_tool_guidance, run_skill, summarize_value
from symphony.skills.base import Skill, SkillContext
from symphony.skills.registry import SkillRegistry


class EchoSkill(Skill):
    name = "echo"
    description = "回显输入"
    input_schema = {"type": "object", "properties": {"x": {"type": "integer"}}}

    async def execute(self, args, ctx: SkillContext):
        return {"echo": args.get("x")}


class BoomSkill(Skill):
    name = "boom"
    description = "总是失败"
    input_schema = {"type": "object", "properties": {}}

    async def execute(self, args, ctx: SkillContext):
        raise ValueError("kaboom")


def _registry(*skills) -> SkillRegistry:
    reg = SkillRegistry()
    for s in skills:
        reg.register(s)
    return reg


def test_build_tool_defs_maps_skills():
    defs = build_tool_defs(_registry(EchoSkill()))
    assert defs[0].function.name == "echo"
    assert defs[0].function.description == "回显输入"


def test_build_tool_guidance_mentions_required_args():
    defs = build_tool_defs(_registry(EchoSkill()))
    guidance = build_tool_guidance(defs)
    assert "工具使用规则" in guidance
    assert "echo" in guidance
    assert "args: x" in guidance


def test_summarize_value_is_ascii_and_truncated():
    summary = summarize_value({"text": "中文" * 100}, max_length=40)
    assert len(summary) <= 40
    summary.encode("ascii")


@pytest.mark.asyncio
async def test_run_skill_success():
    from symphony.ai.schema import ToolCall

    tc = ToolCall(id="c1", name="echo", arguments={"x": 7})
    result, error = await run_skill(
        _registry(EchoSkill()), tc, task_id="chat", node_id="pi", variables={}
    )
    assert error is None
    assert result == {"echo": 7}


@pytest.mark.asyncio
async def test_run_skill_missing():
    from symphony.ai.schema import ToolCall

    tc = ToolCall(id="c1", name="nope", arguments={})
    result, error = await run_skill(
        _registry(), tc, task_id="chat", node_id="pi", variables={}
    )
    assert result is None
    assert "Skill not found" in error


@pytest.mark.asyncio
async def test_run_skill_raises_returns_error():
    from symphony.ai.schema import ToolCall

    tc = ToolCall(id="c1", name="boom", arguments={})
    result, error = await run_skill(
        _registry(BoomSkill()), tc, task_id="chat", node_id="pi", variables={}
    )
    assert result is None
    assert "kaboom" in error
