"""SOP Session 启动 API。"""

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

router = APIRouter(prefix="/api/sop-sessions", tags=["sop-sessions"])


class StartSopSessionRequest(BaseModel):
    """启动 SOP session 的请求体。"""

    sop_id: str
    variables: dict = Field(default_factory=dict)
    title: str = ""


@router.post("")
async def start_sop_session(request: Request, body: StartSopSessionRequest) -> dict:
    """创建 SOP session 并启动关联任务。"""
    manager = request.app.state.task_manager
    try:
        task_id = await manager.start_task(body.sop_id, body.variables)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))

    meta = request.app.state.session_manager.create_sop(
        title=body.title or body.sop_id,
        sop_id=body.sop_id,
        task_id=task_id,
        source="web",
    )
    return {"session_id": meta.session_id, "task_id": task_id}
