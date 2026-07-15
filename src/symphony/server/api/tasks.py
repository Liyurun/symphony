"""任务（工作流实例）管理的 REST API 路由。

通过 request.app.state 访问共享的 task_manager 与 workspace_manager，
提供任务列举、启动、快照查询、事件/轨迹读取、人工干预与删除能力。
"""

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

# 任务相关路由，统一前缀 /api/tasks
router = APIRouter(prefix="/api/tasks", tags=["tasks"])


class StartTaskRequest(BaseModel):
    """启动任务的请求体。"""

    # 目标 SOP id
    sop_id: str
    # 启动变量
    variables: dict = Field(default_factory=dict)


class InterveneRequest(BaseModel):
    """人工干预的请求体。"""

    # 目标节点 id
    node_id: str
    # 干预动作标识
    action: str
    # 干预附带数据
    data: dict = Field(default_factory=dict)


class RerunNodeRequest(BaseModel):
    """补充指令重跑主节点请求体。"""

    # 用户追加给主节点的补充指令
    supplemental_instruction: str
    # 是否联动失效下游节点
    invalidate_downstream: bool = True


class AnswerInteractionRequest(BaseModel):
    """回答运行中 interaction 的请求体。"""

    # 用户提交的回答内容
    answer: dict = Field(default_factory=dict)


class ConfirmSubFlowRequest(BaseModel):
    """确认子流程草案的请求体。"""

    # 确认后的子节点定义列表
    nodes: list[dict]
    # 确认后的子节点边列表
    edges: list[dict] = Field(default_factory=list)


class RejectSubFlowRequest(BaseModel):
    """拒绝子流程草案的请求体。"""

    # 拒绝原因
    reason: str = ""


class RetrySubNodeRequest(BaseModel):
    """重跑子节点的请求体。"""

    # 用户追加给子节点的重跑提示词
    retry_prompt: str = ""
    # 保留给前端表达语义，当前后端固定按节点及下游失效处理
    invalidate_downstream: bool = True


class ProvideSubNodeOutputRequest(BaseModel):
    """人工提供子节点输出的请求体。"""

    # 人工确认的子节点输出
    output: dict


class RetryUpstreamsRequest(BaseModel):
    """批量重跑上游子节点的请求体。"""

    # 需要重跑的子节点 id 列表
    sub_node_ids: list[str]
    # 子节点 id -> 重跑提示词
    retry_prompts: dict[str, str] = Field(default_factory=dict)


@router.get("")
def list_tasks(request: Request) -> list[dict]:
    """列举所有任务的元信息。"""
    return request.app.state.task_manager.list_tasks()


@router.post("")
async def start_task(request: Request, body: StartTaskRequest) -> dict:
    """按 SOP 启动一个新任务，返回其 task_id。"""
    # 取任务管理器
    manager = request.app.state.task_manager
    # 启动边界：SOP 不存在等错误转为 404
    try:
        task_id = await manager.start_task(body.sop_id, body.variables)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return {"task_id": task_id}


@router.get("/{task_id}")
def get_task(request: Request, task_id: str) -> dict:
    """获取任务状态快照，不存在返回 404。"""
    # 读取快照（内存优先，其次落盘）
    snapshot = request.app.state.task_manager.get_snapshot(task_id)
    # 缺失则报 404
    if snapshot is None:
        raise HTTPException(status_code=404, detail=f"Task not found: {task_id}")
    return snapshot


@router.get("/{task_id}/events")
def get_task_events(request: Request, task_id: str, since: int = 0) -> list[dict]:
    """读取任务事件日志，可用 since 做偏移切片；工作区不存在返回 404。"""
    # 定位工作区
    workspace = request.app.state.workspace_manager.get(task_id)
    if workspace is None:
        raise HTTPException(status_code=404, detail=f"Task not found: {task_id}")
    # 读取全部事件后按 since 偏移切片
    return workspace.event_log.read_all()[since:]


@router.get("/{task_id}/traces")
def get_task_traces(request: Request, task_id: str) -> list[dict]:
    """读取任务的 LLM 调试轨迹；工作区不存在返回 404。"""
    # 定位工作区
    workspace = request.app.state.workspace_manager.get(task_id)
    if workspace is None:
        raise HTTPException(status_code=404, detail=f"Task not found: {task_id}")
    return workspace.trace_log.read_all()


@router.get("/{task_id}/dag-log")
def get_task_dag_log(request: Request, task_id: str) -> dict:
    """读取任务 DAG 化运行日志。"""
    try:
        return request.app.state.task_manager.get_dag_log(task_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/{task_id}/intervene")
async def intervene_task(request: Request, task_id: str, body: InterveneRequest) -> dict:
    """对指定任务施加一次人工干预。"""
    # 取任务管理器
    manager = request.app.state.task_manager
    # 干预边界：任务不存在等错误转为 404
    try:
        await manager.intervene(task_id, body.node_id, body.action, body.data)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return {"ok": True}


@router.get("/{task_id}/interactions/pending")
def get_pending_interactions(request: Request, task_id: str) -> list[dict]:
    """读取任务待回答的运行中交互。"""
    try:
        return request.app.state.task_manager.pending_interactions(task_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/{task_id}/interactions/{interaction_id}/answer")
async def answer_task_interaction(
    request: Request,
    task_id: str,
    interaction_id: str,
    body: AnswerInteractionRequest,
) -> dict:
    """回答任务运行中的确认请求。"""
    try:
        await request.app.state.task_manager.answer_interaction(
            task_id,
            interaction_id,
            body.answer,
        )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return {"ok": True}


@router.post("/{task_id}/nodes/{node_id}/rerun")
async def rerun_node(
    request: Request,
    task_id: str,
    node_id: str,
    body: RerunNodeRequest,
) -> dict:
    """追加补充指令重跑指定主节点，并让下游失效重跑。"""
    # 补充指令必须有实际内容，避免生成空 attempt。
    if not body.supplemental_instruction.strip():
        raise HTTPException(status_code=400, detail="supplemental_instruction is required")
    manager = request.app.state.task_manager
    try:
        return await manager.rerun_node(
            task_id,
            node_id,
            body.supplemental_instruction,
            body.invalidate_downstream,
        )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/{task_id}/nodes/{node_id}/subflow/confirm")
async def confirm_subflow(request: Request, task_id: str, node_id: str, body: ConfirmSubFlowRequest) -> dict:
    """确认指定 composite 节点的子流程草案。"""
    manager = request.app.state.task_manager
    try:
        await manager.confirm_subflow(task_id, node_id, body.nodes, body.edges)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return {"ok": True}


@router.post("/{task_id}/nodes/{node_id}/subflow/reject")
async def reject_subflow(request: Request, task_id: str, node_id: str, body: RejectSubFlowRequest) -> dict:
    """拒绝指定 composite 节点的子流程草案。"""
    manager = request.app.state.task_manager
    try:
        await manager.reject_subflow(task_id, node_id, body.reason)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return {"ok": True}


@router.post("/{task_id}/nodes/{node_id}/subnodes/{sub_node_id}/retry")
async def retry_subnode(
    request: Request,
    task_id: str,
    node_id: str,
    sub_node_id: str,
    body: RetrySubNodeRequest,
) -> dict:
    """带提示词重跑指定子节点，并让父 composite 节点恢复调度。"""
    manager = request.app.state.task_manager
    try:
        await manager.retry_subnode(task_id, node_id, sub_node_id, body.retry_prompt)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return {"ok": True}


@router.post("/{task_id}/nodes/{node_id}/subnodes/{sub_node_id}/provide-output")
async def provide_subnode_output(
    request: Request,
    task_id: str,
    node_id: str,
    sub_node_id: str,
    body: ProvideSubNodeOutputRequest,
) -> dict:
    """人工提供子节点输出，并恢复父 composite 节点调度。"""
    manager = request.app.state.task_manager
    try:
        await manager.provide_subnode_output(task_id, node_id, sub_node_id, body.output)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return {"ok": True}


@router.post("/{task_id}/nodes/{node_id}/subnodes/{sub_node_id}/skip")
async def skip_subnode(request: Request, task_id: str, node_id: str, sub_node_id: str) -> dict:
    """跳过指定子节点，并恢复父 composite 节点调度。"""
    manager = request.app.state.task_manager
    try:
        await manager.skip_subnode(task_id, node_id, sub_node_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return {"ok": True}


@router.post("/{task_id}/nodes/{node_id}/subflow/retry-upstreams")
async def retry_upstreams(request: Request, task_id: str, node_id: str, body: RetryUpstreamsRequest) -> dict:
    """批量重跑上游子节点。"""
    manager = request.app.state.task_manager
    try:
        await manager.retry_upstreams(task_id, node_id, body.sub_node_ids, body.retry_prompts)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return {"ok": True}


@router.delete("/{task_id}")
def delete_task(request: Request, task_id: str) -> dict:
    """删除指定任务工作区，返回是否删除成功。"""
    deleted = request.app.state.workspace_manager.delete(task_id)
    return {"deleted": deleted}
