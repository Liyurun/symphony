"""统一 Session 日志 API。"""

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from symphony.agent.interactions import InteractionAnswer
from symphony.storage import SessionStatus

router = APIRouter(prefix="/api/sessions", tags=["sessions"])


class InteractionAnswerRequest(BaseModel):
    """用户回答反问的请求体。"""

    answer: dict


def _session_or_404(request: Request, session_id: str):
    """获取 session log，不存在则返回 404。"""
    log = request.app.state.session_manager.get(session_id)
    if log is None:
        raise HTTPException(status_code=404, detail=f"Session not found: {session_id}")
    return log


def _read_sop_events(request: Request, meta: dict) -> list[dict]:
    """读取 SOP session 关联 task 的事件日志。"""
    task_id = meta.get("task_id")
    if not task_id:
        return []
    workspace = request.app.state.workspace_manager.get(task_id)
    if workspace is None:
        return [{"type": "task_missing", "task_id": task_id}]
    return workspace.event_log.read_all()


def _read_sop_traces(request: Request, meta: dict) -> list[dict]:
    """读取 SOP session 关联 task 的 LLM trace。"""
    task_id = meta.get("task_id")
    if not task_id:
        return []
    workspace = request.app.state.workspace_manager.get(task_id)
    if workspace is None:
        return []
    return workspace.trace_log.read_all()


@router.get("")
def list_sessions(request: Request) -> list[dict]:
    """列举全部 session 元信息。"""
    return request.app.state.session_manager.list_sessions()


@router.get("/{session_id}")
def get_session(request: Request, session_id: str) -> dict:
    """读取指定 session 的元信息。"""
    return _session_or_404(request, session_id).load_meta()


@router.get("/{session_id}/transcript")
def get_transcript(request: Request, session_id: str) -> list[dict]:
    """读取指定 session 的 transcript。"""
    return _session_or_404(request, session_id).read_transcript()


@router.get("/{session_id}/events")
def get_events(request: Request, session_id: str) -> list[dict]:
    """读取 Chat session events 或 SOP task events。"""
    log = _session_or_404(request, session_id)
    meta = log.load_meta()
    if meta.get("type") == "sop":
        return _read_sop_events(request, meta)
    return log.read_events()


@router.get("/{session_id}/traces")
def get_traces(request: Request, session_id: str) -> list[dict]:
    """读取 Chat session traces 或 SOP task traces。"""
    log = _session_or_404(request, session_id)
    meta = log.load_meta()
    if meta.get("type") == "sop":
        return _read_sop_traces(request, meta)
    return log.read_traces()


@router.get("/{session_id}/interactions")
def get_interactions(request: Request, session_id: str) -> list[dict]:
    """读取指定 session 的反问与回答记录。"""
    return _session_or_404(request, session_id).read_interactions()


@router.post("/{session_id}/interactions/{interaction_id}/answer")
def answer_interaction(
    request: Request,
    session_id: str,
    interaction_id: str,
    body: InteractionAnswerRequest,
) -> dict:
    """持久化用户对反问的回答。"""
    log = _session_or_404(request, session_id)
    answer = InteractionAnswer(
        interaction_id=interaction_id,
        session_id=session_id,
        answer=body.answer,
    )
    log.append_interaction(answer.model_dump(mode="json"))
    request.app.state.session_manager.update_status(session_id, SessionStatus.RUNNING)
    return {"ok": True}
