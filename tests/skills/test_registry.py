"""skills.registry 与 skills.base 的单元测试。

通过定义一个最小的 EchoSkill 验证注册中心的 register/get/list/unregister，
以及 Skill.execute 在 SkillContext 下的调用行为。
"""

from typing import Any

import pytest

from symphony.skills import Skill, SkillContext, SkillRegistry


class EchoSkill(Skill):
    """测试用最小技能：原样回显 args 中的 message 字段。"""

    # 技能名称
    name = "echo"
    # 技能描述
    description = "Echo back the given message"
    # 输入参数 schema
    input_schema = {"type": "object", "properties": {"message": {"type": "string"}}}

    async def execute(self, args: dict[str, Any], context: SkillContext) -> Any:
        """返回包含回显消息的字典。"""
        return {"echo": args["message"]}


def test_register_and_get():
    """注册后 get 应返回同一对象实例。"""
    registry = SkillRegistry()
    skill = EchoSkill()
    registry.register(skill)
    assert registry.get("echo") is skill


def test_list_skills():
    """list_skills 应包含已注册的技能。"""
    registry = SkillRegistry()
    skill = EchoSkill()
    registry.register(skill)
    skills = registry.list_skills()
    assert skill in skills
    assert len(skills) == 1


@pytest.mark.asyncio
async def test_execute():
    """execute 应在给定 SkillContext 下返回回显结果。"""
    registry = SkillRegistry()
    registry.register(EchoSkill())
    skill = registry.get("echo")
    context = SkillContext(task_id="t1", node_id="n1")
    result = await skill.execute({"message": "hi"}, context)
    assert result == {"echo": "hi"}


def test_get_missing_returns_none():
    """get 不存在的技能名应返回 None。"""
    registry = SkillRegistry()
    assert registry.get("nope") is None


def test_unregister():
    """unregister 后 get 应返回 None。"""
    registry = SkillRegistry()
    registry.register(EchoSkill())
    registry.unregister("echo")
    assert registry.get("echo") is None
