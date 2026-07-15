"""Agent 工具（技能）共享助手。

把「技能 -> 工具定义」的转换与「执行一次技能调用」的逻辑集中在此，
供 AgentRuntime（SOP 节点执行）与 ChatRuntime（默认流式对话）复用。
本模块不发射任何事件，也不改写对话历史；调用方负责各自的事件与消息处理，
从而让两套运行时共享执行逻辑而不耦合各自的事件类型。
"""

import json
from typing import Any, Optional

from symphony.ai.schema import FunctionDef, ToolCall, ToolDef
from symphony.skills.base import SkillContext
from symphony.skills.registry import SkillRegistry


def build_tool_defs(registry: SkillRegistry) -> list[ToolDef]:
    """把已注册技能转换为供 LLM 调用的工具定义列表。"""
    return [
        ToolDef(
            function=FunctionDef(
                name=skill.name,
                description=skill.description,
                parameters=skill.input_schema,
            )
        )
        for skill in registry.list_skills()
    ]


def build_tool_guidance(tools: list[ToolDef]) -> str:
    """生成注入给模型的工具使用规则，帮助模型更主动、准确地调用 Skill。"""
    if not tools:
        return ""
    lines = [
        "工具使用规则：",
        "- 当用户需求需要读取/写入文件、访问 HTTP、执行计算或处理数据时，优先调用合适工具。",
        "- 当用户询问你有哪些 skill、工具、能力、能做什么时，优先调用 skill_inventory。",
        "- 当用户要查找某类外部 Skill 说明时，调用 skill_reference_search。",
        "- 只有收到工具结果后，才能声称已经完成对应外部动作。",
        "- 工具参数必须严格符合 JSON Schema；缺少必要信息时先向用户询问。",
        "- 工具执行失败时，简要说明失败原因，并给出下一步建议。",
        "可用工具：",
    ]
    for tool in tools:
        fn = tool.function
        schema = fn.parameters or {}
        required = schema.get("required") or []
        props = schema.get("properties") or {}
        prop_names = ", ".join(props.keys()) if isinstance(props, dict) else ""
        required_text = f"; required: {', '.join(required)}" if required else ""
        props_text = f"; args: {prop_names}" if prop_names else ""
        lines.append(f"- {fn.name}: {fn.description}{required_text}{props_text}")
    return "\n".join(lines)


def summarize_value(value: Any, max_length: int = 160) -> str:
    """把工具参数或结果压缩成一行 ASCII 摘要，适合 TUI 展示。"""
    try:
        text = json.dumps(value, ensure_ascii=True, default=str, sort_keys=True)
    except TypeError:
        text = str(value).encode("ascii", errors="backslashreplace").decode("ascii")
    text = " ".join(text.split())
    if len(text) <= max_length:
        return text
    return text[: max_length - 3] + "..."


async def run_skill(
    registry: SkillRegistry,
    tool_call: ToolCall,
    *,
    task_id: str,
    node_id: str,
    variables: dict[str, Any],
) -> tuple[Any, Optional[str]]:
    """执行一次技能调用，返回 (结果, 错误)。

    成功时返回 (result, None)；技能不存在或抛异常时返回 (None, 错误字符串)。
    不发射事件、不写回消息历史，交由调用方处理。
    """
    skill = registry.get(tool_call.name)
    if skill is None:
        return None, f"Skill not found: {tool_call.name}"
    ctx = SkillContext(
        task_id=task_id,
        node_id=node_id,
        variables=variables,
        emit_event=lambda e: None,
    )
    try:
        result = await skill.execute(tool_call.arguments, ctx)
    except Exception as exc:  # 外部技能代码边界
        return None, f"{type(exc).__name__}: {exc}"
    return result, None
