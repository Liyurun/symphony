"""自定义 Skill 加载器测试。"""

from pathlib import Path

from symphony.skills.loader import load_custom_skills
from symphony.skills.registry import SkillRegistry


def test_load_custom_skill_class(tmp_path: Path):
    """目录中的 Skill 子类应被实例化并注册。"""
    skill_file = tmp_path / "hello_skill.py"
    skill_file.write_text(
        '''
from typing import Any

from symphony.skills.base import Skill, SkillContext


class HelloSkill(Skill):
    name = "hello"
    description = "Say hello"
    input_schema = {"type": "object", "properties": {"name": {"type": "string"}}}
    output_schema = {"type": "object"}

    async def execute(self, args: dict[str, Any], context: SkillContext) -> Any:
        return {"message": "hello " + args.get("name", "world")}
''',
        encoding="utf-8",
    )
    registry = SkillRegistry()

    result = load_custom_skills(registry, tmp_path)

    assert result.loaded == ["hello"]
    assert result.errors == []
    assert registry.get("hello") is not None
    assert registry.source_of("hello").startswith("custom:")


def test_load_custom_skill_explicit_exports(tmp_path: Path):
    """SKILLS 显式导出中的 Skill 类也应被注册。"""
    skill_file = tmp_path / "explicit_skill.py"
    skill_file.write_text(
        '''
from symphony.skills.base import Skill


class ExplicitSkill(Skill):
    name = "explicit"
    description = "Explicit export"
    input_schema = {"type": "object", "properties": {}}
    output_schema = {"type": "object"}

    async def execute(self, args, context):
        return {"ok": True}


SKILLS = [ExplicitSkill]
''',
        encoding="utf-8",
    )
    registry = SkillRegistry()

    result = load_custom_skills(registry, tmp_path)

    assert result.loaded == ["explicit"]
    assert result.errors == []
    assert registry.get("explicit") is not None


def test_load_custom_skill_reports_errors(tmp_path: Path):
    """坏脚本不应阻塞加载流程，应被记录到 errors。"""
    (tmp_path / "bad_skill.py").write_text("raise RuntimeError('boom')", encoding="utf-8")
    registry = SkillRegistry()

    result = load_custom_skills(registry, tmp_path)

    assert result.loaded == []
    assert len(result.errors) == 1
    assert "boom" in result.errors[0].error
