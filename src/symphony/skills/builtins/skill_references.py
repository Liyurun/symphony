"""外部 Skill 文档检索 Skill。

把 Trae/system 的 SKILL.md 索引暴露为 Symphony 可调用的内置工具，让模型不仅能
被动看到少量参考片段，也能在需要时主动检索更多外部 Skill 说明。
"""

from typing import Any

from symphony.skills.base import Skill, SkillContext
from symphony.skills.references import SkillReferenceIndex, is_skill_inventory_query


class SkillReferenceSearchSkill(Skill):
    """检索 Trae/system 的 SKILL.md 参考资料。"""

    name = "skill_reference_search"
    description = "Search or list external Trae/system SKILL.md reference documents by query"
    input_schema = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search query, e.g. logs, lark doc, 数据库"},
            "limit": {"type": "integer", "default": 20},
        },
        "required": ["query"],
    }
    output_schema = {"type": "object"}

    def __init__(self, index: SkillReferenceIndex | None = None) -> None:
        """保存可选索引；未传入时执行时按默认位置构建。"""
        self.index = index

    async def execute(self, args: dict[str, Any], context: SkillContext) -> Any:
        """按 query 检索外部 Skill 文档并返回结构化结果。"""
        index = self.index or SkillReferenceIndex.from_default_locations()
        limit = _safe_limit(args.get("limit"))
        query = str(args["query"])
        matches = index.search(query, limit=limit)
        return {
            "query": query,
            "mode": "list" if is_skill_inventory_query(query) else "search",
            "total": len(index.references),
            "items": [match.to_dict() for match in matches],
        }


class SkillInventorySkill(Skill):
    """列出当前 Symphony 可见的 Skill。"""

    name = "skill_inventory"
    description = "List executable Symphony skills and external SKILL.md references"
    input_schema = {
        "type": "object",
        "properties": {
            "include_external": {"type": "boolean", "default": True},
            "query": {"type": "string", "description": "Optional filter for external references"},
            "limit": {"type": "integer", "default": 100},
        },
    }
    output_schema = {"type": "object"}

    async def execute(self, args: dict[str, Any], context: SkillContext) -> Any:
        """返回可执行 Skill 与外部参考 Skill 清单。"""
        executable = list(context.variables.get("available_skills") or [])
        include_external = bool(args.get("include_external", True))
        query = str(args.get("query") or "")
        limit = _safe_limit(args.get("limit"), default=100, maximum=200)
        external_items: list[dict[str, Any]] = []
        total_external = 0

        if include_external:
            index = context.variables.get("skill_reference_index") or SkillReferenceIndex.from_default_locations()
            total_external = len(index.references)
            matches = (
                index.search(query, limit=limit)
                if query and not is_skill_inventory_query(query)
                else index.list_all(limit=limit)
            )
            external_items = [
                {
                    "name": match.reference.name,
                    "description": match.reference.description,
                    "source": match.reference.source,
                    "path": match.reference.path,
                }
                for match in matches
            ]

        return {
            "executable_count": len(executable),
            "executable_skills": executable,
            "external_reference_count": total_external,
            "external_references": external_items,
            "note": "external_references are SKILL.md instructions; executable_skills are callable Python tools.",
        }


def _safe_limit(value: Any, default: int = 20, maximum: int = 200) -> int:
    """限制返回数量，避免上下文膨胀。"""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, min(number, maximum))
