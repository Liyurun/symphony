"""Skill 注册中心。

维护技能名称到实例的映射，提供注册、查询、列举与注销能力，
供上层 Agent 运行时按名称查找并调用对应技能。
"""

from typing import Optional

from symphony.skills.base import Skill


class SkillRegistry:
    """技能注册中心，按名称管理已注册的 Skill 实例。"""

    def __init__(self):
        """初始化空的技能映射表。"""
        # 名称到技能实例的映射
        self._skills: dict[str, Skill] = {}
        # 名称到来源的映射，用于 API 展示和排查自定义技能加载情况
        self._sources: dict[str, str] = {}

    def register(self, skill: Skill, source: str = "manual") -> None:
        """注册一个技能实例，若同名则覆盖。"""
        self._skills[skill.name] = skill
        self._sources[skill.name] = source

    def get(self, name: str) -> Optional[Skill]:
        """按名称获取技能，不存在时返回 None。"""
        return self._skills.get(name)

    def source_of(self, name: str) -> Optional[str]:
        """返回指定技能的来源，不存在时返回 None。"""
        return self._sources.get(name)

    def list_skills(self) -> list[Skill]:
        """返回所有已注册技能实例的列表。"""
        return list(self._skills.values())

    def unregister(self, name: str) -> None:
        """按名称注销技能，不存在时静默忽略。"""
        self._skills.pop(name, None)
        self._sources.pop(name, None)
