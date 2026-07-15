"""通用 Agent 问答 API（非流式兜底）。

TUI 默认走 /ws/chat 流式端点；本 REST 端点保留给脚本与测试，
内部复用 ChatRuntime，把流式增量收敛为一次自然语言回答。
"""

from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from symphony.agent.context_compression import ContextCompressor
from symphony.agent.chat_runtime import ChatRuntime
from symphony.config import ContextCompressionConfig, SymphonyConfig
from symphony.skills.registry import SkillRegistry

router = APIRouter(prefix="/api/chat", tags=["chat"])


class ChatHistoryItem(BaseModel):
    """TUI chat 历史消息。"""

    role: Literal["user", "assistant"]
    content: str


class ChatRequest(BaseModel):
    """通用问答请求。"""

    question: str = Field(min_length=1)
    history: list[ChatHistoryItem] = Field(default_factory=list)


class ChatResponse(BaseModel):
    """通用问答响应。"""

    answer: str


class CreateChatSessionRequest(BaseModel):
    """创建 Chat session 的请求体。"""

    title: str = "New chat"
    source: str = "web"


def build_context_compressor(config: ContextCompressionConfig) -> ContextCompressor:
    """按配置构造上下文压缩器。"""
    return ContextCompressor(
        max_prompt_chars=config.max_prompt_chars,
        keep_recent_messages=config.keep_recent_messages,
        min_recent_messages=config.min_recent_messages,
        summary_max_chars=config.summary_max_chars,
        max_message_chars=config.max_message_chars,
        enabled=config.enabled,
    )


def build_chat_runtime_kwargs(
    config: SymphonyConfig | None,
    skill_reference_index=None,
) -> dict:
    """从应用配置生成 ChatRuntime 构造参数；缺配置时保持旧默认行为。"""
    kwargs = {"skill_reference_index": skill_reference_index}
    if config is None:
        return kwargs
    kwargs.update(
        {
            "max_iterations": config.runtime.chat.max_iterations,
            "skill_reference_limit": config.runtime.chat.skill_reference_limit,
            "context_compressor": build_context_compressor(
                config.runtime.context_compression
            ),
        }
    )
    return kwargs


@router.post("/sessions")
async def create_chat_session(request: Request, body: CreateChatSessionRequest) -> dict:
    """创建持久化 Chat session。"""
    meta = request.app.state.session_manager.create_chat(
        title=body.title,
        source=body.source,
    )
    return meta.model_dump(mode="json")


@router.post("")
async def chat(request: Request, body: ChatRequest) -> dict:
    """非流式问答：收敛 ChatRuntime 的回答增量为完整文本。"""
    provider = request.app.state.llm_provider
    api_key = getattr(provider, "api_key", None)
    if api_key is not None and not str(api_key).strip():
        raise HTTPException(
            status_code=400,
            detail="缺少 LLM API Key：请设置 ARK_API_KEY，或在 config.local.yaml 中填写 llm.api_key 后重启 Symphony。",
        )
    registry = getattr(request.app.state, "skill_registry", None) or SkillRegistry()
    skill_reference_index = getattr(request.app.state, "skill_reference_index", None)
    config = getattr(request.app.state, "config", None)
    runtime = ChatRuntime(
        provider,
        registry,
        **build_chat_runtime_kwargs(config, skill_reference_index),
    )
    history = [{"role": item.role, "content": item.content} for item in body.history]

    parts: list[str] = []
    try:
        async for event in runtime.stream(body.question, history):
            if event.type == "chat_answer_delta":
                parts.append(event.text)
            elif event.type == "chat_failed":
                raise RuntimeError(event.error)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"问答失败: {exc}") from exc

    return ChatResponse(answer="".join(parts)).model_dump()
