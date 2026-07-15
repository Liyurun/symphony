"""Session-aware Chat runner。

该模块把默认 ChatRuntime 包装成持久化执行器：WebSocket 仍然收到原始 chat
事件流，同时 session 日志保存 transcript、产品层事件与 LLM trace。
"""

from typing import Any, AsyncIterator

from symphony.agent.chat_events import ChatEvent, ChatFailed
from symphony.agent.chat_runtime import ChatRuntime
from symphony.storage import SessionManager, SessionStatus


class ChatSessionRunner:
    """包装 ChatRuntime 并把会话过程持久化。"""

    def __init__(
        self,
        llm_provider: Any,
        skill_registry: Any,
        session_manager: SessionManager,
        **runtime_kwargs: Any,
    ) -> None:
        """保存依赖与传给 ChatRuntime 的可选参数。"""
        self.llm_provider = llm_provider
        self.skill_registry = skill_registry
        self.session_manager = session_manager
        self.runtime_kwargs = runtime_kwargs

    def _append_event(self, session_id: str, event: dict) -> None:
        """追加 session 事件。"""
        self.session_manager.require(session_id).append_event(
            {"session_id": session_id, **event}
        )

    def _append_trace(self, session_id: str, trace: dict) -> None:
        """追加 session trace。"""
        self.session_manager.require(session_id).append_trace(
            {"session_id": session_id, **trace}
        )

    async def stream(
        self,
        session_id: str,
        question: str,
        history: list[dict],
    ) -> AsyncIterator[ChatEvent]:
        """运行一轮 Chat，边流式返回边落盘。"""
        log = self.session_manager.require(session_id)
        log.append_transcript({"role": "user", "content": question})
        self._append_event(session_id, {"type": "chat_started"})
        self._append_event(
            session_id,
            {"type": "chat_user_message", "content": question},
        )
        parts: list[str] = []

        runtime = ChatRuntime(
            self.llm_provider,
            self.skill_registry,
            on_trace=lambda trace: self._append_trace(session_id, trace),
            **self.runtime_kwargs,
        )
        try:
            async for event in runtime.stream(question, history):
                data = event.to_dict()
                kind = data.get("type")
                if kind == "chat_answer_delta":
                    parts.append(data.get("text", ""))
                elif kind == "chat_tool_call":
                    self._append_event(
                        session_id,
                        {
                            "type": "tool_called",
                            "skill_name": data.get("skill_name"),
                            "args": data.get("args", {}),
                            "summary": data.get("summary"),
                        },
                    )
                elif kind == "chat_tool_result":
                    self._append_event(
                        session_id,
                        {
                            "type": "tool_returned"
                            if data.get("ok")
                            else "tool_failed",
                            "skill_name": data.get("skill_name"),
                            "ok": data.get("ok"),
                            "detail": data.get("detail", ""),
                        },
                    )
                elif kind == "chat_failed":
                    error = data.get("error", "")
                    self._append_event(
                        session_id,
                        {"type": "chat_failed", "error": error},
                    )
                    self.session_manager.update_status(
                        session_id,
                        SessionStatus.FAILED,
                        error=error,
                    )
                elif kind == "chat_completed":
                    answer = data.get("answer", "".join(parts))
                    log.append_transcript({"role": "assistant", "content": answer})
                    self._append_event(
                        session_id,
                        {"type": "chat_answer_completed", "answer": answer},
                    )
                    self._append_event(session_id, {"type": "chat_completed"})
                    self.session_manager.update_status(
                        session_id,
                        SessionStatus.COMPLETED,
                    )
                yield event
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            self._append_event(session_id, {"type": "chat_failed", "error": error})
            self.session_manager.update_status(
                session_id,
                SessionStatus.FAILED,
                error=error,
            )
            yield ChatFailed(error=error)
