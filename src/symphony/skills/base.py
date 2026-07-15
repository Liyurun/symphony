"""Skill 系统基础类型定义。

定义技能执行时的上下文 SkillContext，以及所有技能的抽象基类 Skill。
SkillContext 携带任务/节点标识、运行时变量与事件回调；Skill 约定统一的
元信息（名称、描述、输入/输出 schema）与异步 execute 接口。
"""

from abc import ABC, abstractmethod
from typing import Any, Callable

from pydantic import BaseModel, Field


class SkillContext(BaseModel):
    """技能执行上下文，承载任务/节点标识、变量与事件回调。"""

    # 允许携带 Callable 等任意类型字段
    model_config = {"arbitrary_types_allowed": True}

    # 所属任务 id
    task_id: str
    # 所属节点 id
    node_id: str
    # 运行时变量表
    variables: dict[str, Any] = Field(default_factory=dict)
    # 事件发射回调，默认 no-op，接收一个事件字典
    emit_event: Callable[[dict], None] = lambda event: None


class Skill(ABC):
    """所有技能的抽象基类，约定元信息与异步执行接口。"""

    # 技能唯一名称
    name: str
    # 技能功能描述
    description: str
    # 输入参数的 JSON Schema
    input_schema: dict[str, Any]
    # 输出结果的 JSON Schema，默认为通用对象
    output_schema: dict[str, Any] = {"type": "object"}

    @abstractmethod
    async def execute(self, args: dict[str, Any], context: SkillContext) -> Any:
        """执行技能。

        :param args: 调用参数，结构应符合 input_schema。
        :param context: 执行上下文。
        :return: 技能执行结果。
        """
        ...
