"""技能列表查询的 REST API 路由。

通过 request.app.state 访问 task_manager 的 skill_registry，
把已注册技能转为含 name/description/input_schema/output_schema 的字典列表。
"""

from fastapi import APIRouter, Request

from symphony.skills.references import is_skill_inventory_query

# 技能相关路由，统一前缀 /api/skills
router = APIRouter(prefix="/api/skills", tags=["skills"])


@router.get("")
def list_skills(request: Request) -> list[dict]:
    """列出全部已注册技能的元信息。"""
    # 从任务管理器取技能注册中心
    registry = request.app.state.task_manager.skill_registry
    # 逐个技能提取元信息字段
    return [
        {
            "name": skill.name,
            "description": skill.description,
            "input_schema": skill.input_schema,
            "output_schema": skill.output_schema,
            "source": registry.source_of(skill.name) or "",
        }
        for skill in registry.list_skills()
    ]


@router.get("/load-errors")
def list_skill_load_errors(request: Request) -> dict:
    """返回自定义 Skill 加载结果，便于定位坏脚本或构造错误。"""
    result = getattr(request.app.state, "custom_skill_load_result", None)
    if result is None:
        return {"loaded": [], "errors": []}
    return result.to_dict()


@router.get("/references")
def search_skill_references(
    request: Request, query: str = "", limit: int | None = None
) -> dict:
    """检索外部 SKILL.md 参考资料。"""
    index = getattr(request.app.state, "skill_reference_index", None)
    if index is None:
        return {"query": query, "items": []}
    config = getattr(request.app.state, "config", None)
    reference_config = getattr(
        getattr(config, "skills", None),
        "skill_references",
        None,
    )
    default_limit = getattr(reference_config, "default_limit", 20)
    max_limit = getattr(reference_config, "max_limit", 50)
    requested_limit = default_limit if limit is None else limit
    safe_limit = max(1, min(requested_limit, max_limit))
    matches = (
        index.list_all(limit=safe_limit)
        if is_skill_inventory_query(query)
        else index.search(query, limit=safe_limit)
    )
    return {
        "query": query,
        "mode": "list" if is_skill_inventory_query(query) else "search",
        "total": len(index.references),
        "items": [match.to_dict() for match in matches],
    }
