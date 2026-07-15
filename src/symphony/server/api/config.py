"""运行时配置查询与更新的 REST API 路由。

通过 request.app.state.config 访问内存中的 SymphonyConfig，
GET 返回对 api_key 掩码后的安全视图；PUT 支持更新 llm 的常用参数（仅内存态，
MVP 不强制写回文件），返回更新后的安全视图。
"""

from typing import Optional

from fastapi import APIRouter, Request
from pydantic import BaseModel

# 配置相关路由，统一前缀 /api/config
router = APIRouter(prefix="/api/config", tags=["config"])


class LLMUpdate(BaseModel):
    """llm 配置的可更新字段（均可选）。"""

    # 覆盖模型名称
    model: Optional[str] = None
    # 覆盖采样温度
    temperature: Optional[float] = None
    # 覆盖最大生成 token 数
    max_tokens: Optional[int] = None
    # 覆盖服务基础 URL
    base_url: Optional[str] = None


class ConfigUpdate(BaseModel):
    """配置更新请求体，目前仅支持更新 llm。"""

    # llm 子配置的部分更新
    llm: Optional[LLMUpdate] = None


def _safe_view(config) -> dict:
    """构造对 api_key 掩码后的配置安全视图。"""
    return {
        # llm 视图：api_key 固定掩码为 ***
        "llm": {
            "provider": config.llm.provider,
            "api_key": "***",
            "model": config.llm.model,
            "base_url": config.llm.base_url,
            "temperature": config.llm.temperature,
            "max_tokens": config.llm.max_tokens,
            "timeout_seconds": config.llm.timeout_seconds,
        },
        # server / storage / runtime / skills / client 原样导出（无敏感字段）
        "server": config.server.model_dump(),
        "storage": config.storage.model_dump(),
        "runtime": config.runtime.model_dump(),
        "skills": config.skills.model_dump(),
        "client": config.client.model_dump(),
    }


@router.get("")
def get_config(request: Request) -> dict:
    """返回当前配置的安全视图（api_key 掩码）。"""
    return _safe_view(request.app.state.config)


@router.put("")
def update_config(request: Request, body: ConfigUpdate) -> dict:
    """更新内存中的 llm 配置字段，返回更新后的安全视图。"""
    # 取出内存配置
    config = request.app.state.config
    # 仅处理提供了 llm 更新的情况
    if body.llm is not None:
        # 仅设置非 None 字段，逐项写入内存 llm 配置
        for field, value in body.llm.model_dump(exclude_none=True).items():
            setattr(config.llm, field, value)
        # 同步更新 provider 的对应属性，使后续调用生效
        provider = getattr(request.app.state, "llm_provider", None)
        if provider is not None:
            for field, value in body.llm.model_dump(exclude_none=True).items():
                setattr(provider, field, value)
    return _safe_view(config)
