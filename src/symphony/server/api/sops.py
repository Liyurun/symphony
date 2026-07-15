"""SOP 模板管理与 AI 生成的 REST API 路由。

通过 request.app.state 访问共享的 template_loader 与 sop_generator，
提供 SOP 的增删改查与基于自然语言描述的 AI 生成能力。
所有模板以 model_dump(by_alias=True) 序列化（edges 输出 "from" 键）。
"""

from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from symphony.workflow.models import SOPTemplate

# SOP 相关路由，统一前缀 /api/sops
router = APIRouter(prefix="/api/sops", tags=["sops"])


class GenerateRequest(BaseModel):
    """AI 生成 SOP 的请求体。"""

    # 自然语言描述
    description: str
    # 可选的目标 SOP id
    sop_id: Optional[str] = None


@router.get("")
def list_sops(request: Request) -> list[dict]:
    """列出全部 SOP 模板（按别名序列化）。"""
    # 从应用状态取模板加载器
    loader = request.app.state.template_loader
    # 逐个转为字典返回
    return [template.model_dump(by_alias=True) for template in loader.list_all()]


@router.post("/generate")
async def generate_sop(request: Request, body: GenerateRequest) -> dict:
    """基于自然语言描述调用 LLM 生成 SOP，保存后返回其字典。"""
    # 取出 AI 生成器与模板加载器
    generator = request.app.state.sop_generator
    loader = request.app.state.template_loader
    # LLM 调用/解析边界：失败时返回 500 并带上原因
    try:
        template = await generator.generate(body.description, body.sop_id)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"SOP 生成失败: {e}")
    # 持久化生成结果
    loader.save(template)
    return template.model_dump(by_alias=True)


@router.post("/generate-draft")
async def generate_sop_draft(request: Request, body: GenerateRequest) -> dict:
    """基于自然语言描述生成 SOP 草案，但不持久化，等待用户确认。"""
    # 只调用 AI 生成器，不写入模板加载器
    generator = request.app.state.sop_generator
    try:
        template = await generator.generate(body.description, body.sop_id)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"SOP 草案生成失败: {e}")
    return template.model_dump(by_alias=True)


@router.get("/{sop_id}")
def get_sop(request: Request, sop_id: str) -> dict:
    """按 id 获取 SOP，不存在返回 404。"""
    # 加载指定模板
    template = request.app.state.template_loader.load(sop_id)
    # 缺失则报 404
    if template is None:
        raise HTTPException(status_code=404, detail=f"SOP not found: {sop_id}")
    return template.model_dump(by_alias=True)


@router.post("")
def create_sop(request: Request, template: SOPTemplate) -> dict:
    """创建（保存）一个 SOP 模板并返回其字典。"""
    # 直接保存请求体校验后的模板
    request.app.state.template_loader.save(template)
    return template.model_dump(by_alias=True)


@router.put("/{sop_id}")
def update_sop(request: Request, sop_id: str, template: SOPTemplate) -> dict:
    """更新指定 SOP 模板（以路径 id 为准）并返回其字典。"""
    # 以路径参数覆盖 body 中的 id，保证一致
    data = template.model_dump(by_alias=True)
    data["id"] = sop_id
    # 重新构造并保存
    updated = SOPTemplate(**data)
    request.app.state.template_loader.save(updated)
    return updated.model_dump(by_alias=True)


@router.delete("/{sop_id}")
def delete_sop(request: Request, sop_id: str) -> dict:
    """删除指定 SOP，返回是否删除成功。"""
    # 删除并回传布尔结果
    deleted = request.app.state.template_loader.delete(sop_id)
    return {"deleted": deleted}
