"""Tasks REST API — create, list, start, cancel, claim, release tasks."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from symphony.core.event_bus import EventBus
from symphony.core.event_log import EventLog
from symphony.core.task_manager import TaskManager
from symphony.sop.sop_registry import SOPRegistry


class CreateTaskRequest(BaseModel):
    # For a SOP-template task, set sop_name. For an ad-hoc single-turn question
    # (方案A), leave sop_name empty and provide `prompt`.
    sop_name: str = ""
    sop_version: str = "1.0"
    metadata: dict = {}
    # User input: a free-form question OR structured inputs for the first node.
    prompt: str = ""
    inputs: dict = {}
    # Auto-start the task right after creation (default True — the old "create
    # then separately start" two-step was a confusing dead end in the UI).
    auto_start: bool = True


class AskRequest(BaseModel):
    """Ad-hoc single-turn question (方案A: everything is a task)."""
    prompt: str
    skill: str = ""


class FollowUpRequest(BaseModel):
    """Append a new turn to an existing ad-hoc task (multi-turn chat)."""
    prompt: str
    skill: str = ""


class RedirectRequest(BaseModel):
    """Interrupt & rerun a node ("打断并重来"), auto-cascading downstream."""
    node_id: str
    instruction: str = ""


class CompleteNodeRequest(BaseModel):
    """Manually mark a node completed with an operator-supplied artifact."""
    node_id: str
    artifact_type: str = "text"
    artifact_value: str
    label: str | None = None
    rerun_downstream: bool = True


class ClaimRequest(BaseModel):
    client_id: str


class HumanResponseRequest(BaseModel):
    task_id: str
    node_id: str
    approved: bool
    feedback: str = ""


def create_tasks_router(
    task_manager: TaskManager,
    event_log: EventLog,
    event_bus: EventBus,
    sop_registry: SOPRegistry,
) -> APIRouter:
    router = APIRouter(tags=["tasks"])

    @router.get("/tasks")
    async def list_tasks(status: str | None = None):
        """List all tasks, optionally filtered by status."""
        tasks = await task_manager.list_tasks(status=status)
        return [
            {
                "task_id": t.task_id,
                "sop_name": t.sop_name,
                "sop_version": t.sop_version,
                "status": t.status.value,
                "claimed_by": t.claimed_by,
                "claimed_at": t.claimed_at,
                "created_at": t.created_at,
                "updated_at": t.updated_at,
                "metadata": t.metadata,
            }
            for t in tasks
        ]

    @router.post("/tasks")
    async def create_task(req: CreateTaskRequest):
        """Create a task, then (by default) start it.

        Two shapes:
          - SOP-template task: provide ``sop_name`` (optionally ``prompt`` /
            ``inputs`` that seed the first node).
          - Ad-hoc single-turn question (方案A): omit ``sop_name`` and provide
            ``prompt``; a one-node SOP is synthesised on the fly.
        """
        # Merge prompt/inputs into metadata so the executor can seed root nodes.
        metadata = dict(req.metadata or {})
        metadata.setdefault("pi_cwd", task_manager.pi_bridge.config.cwd)
        metadata.setdefault(
            "pi_context_files", task_manager.pi_bridge.config.context_file_infos()
        )
        if req.prompt:
            metadata.setdefault("prompt", req.prompt)
        if req.inputs:
            metadata.setdefault("inputs", req.inputs)

        if req.sop_name:
            sop = await sop_registry.get(req.sop_name)
            if not sop:
                raise HTTPException(
                    status_code=404, detail=f"SOP '{req.sop_name}' not found"
                )
            task = await task_manager.create_task(
                sop=sop, sop_version=req.sop_version, metadata=metadata,
            )
            if req.auto_start:
                await task_manager.start_task(task.task_id, sop)
        else:
            # Ad-hoc Q&A task.
            if not req.prompt:
                raise HTTPException(
                    status_code=400,
                    detail="Provide either 'sop_name' or a 'prompt' for an ad-hoc task.",
                )
            from symphony.sop.sop_definition import make_adhoc_sop

            sop = make_adhoc_sop(req.prompt)
            metadata.update({"adhoc": True, "kind": "qa", "prompt": req.prompt})
            task = await task_manager.create_task(sop=sop, metadata=metadata)
            if req.auto_start:
                await task_manager.start_task(task.task_id, sop)

        return {
            "task_id": task.task_id,
            "sop_name": task.sop_name,
            "status": task.status.value,
            "started": req.auto_start,
        }

    @router.post("/ask")
    async def ask(req: AskRequest):
        """方案A entry point: ask a single-turn question as a one-node task."""
        if not req.prompt.strip():
            raise HTTPException(status_code=400, detail="prompt is required")
        task = await task_manager.create_and_start_qa(req.prompt, skill=req.skill)
        return {"task_id": task.task_id, "status": task.status.value}

    @router.post("/tasks/{task_id}/follow-up")
    async def follow_up(task_id: str, req: FollowUpRequest):
        """Append a new turn to an existing ad-hoc task (multi-turn chat)."""
        if not req.prompt.strip():
            raise HTTPException(status_code=400, detail="prompt is required")
        try:
            node_id = await task_manager.ask_follow_up(
                task_id, req.prompt, skill=req.skill
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        return {"task_id": task_id, "node_id": node_id, "status": "running"}

    @router.post("/tasks/{task_id}/redirect")
    async def redirect_node(task_id: str, req: RedirectRequest):
        """Interrupt & rerun a node ("打断并重来"), auto-cascading downstream."""
        try:
            await task_manager.redirect_node(task_id, req.node_id, req.instruction)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        return {"status": "redirected", "node_id": req.node_id}

    @router.post("/tasks/{task_id}/complete-node")
    async def complete_node(task_id: str, req: CompleteNodeRequest):
        """Manually mark a node completed with an operator-supplied artifact."""
        artifact = {
            "type": req.artifact_type,
            "value": req.artifact_value,
            "label": req.label,
        }
        try:
            await task_manager.complete_node(
                task_id, req.node_id, artifact, req.rerun_downstream
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        return {"status": "completed", "node_id": req.node_id}

    @router.get("/tasks/{task_id}/artifacts")
    async def get_task_artifacts(task_id: str):
        """Get each node's latest artifact for a task."""
        return await event_log.get_node_artifacts(task_id)

    @router.get("/tasks/{task_id}")
    async def get_task(task_id: str):
        """Get task details."""
        task = await task_manager.get_task(task_id)
        if not task:
            raise HTTPException(status_code=404, detail="Task not found")

        # Resolve the live SOP (prefer the in-memory one so ad-hoc tasks, which
        # are not registry files, still expose their node graph to the UI).
        sop = await _resolve_sop(task)
        nodes = []
        if sop:
            nodes = [
                {
                    "id": n.id,
                    "name": n.name,
                    "skill": n.skill,
                    "depends_on": list(n.depends_on or []),
                    "human_intervention": bool(getattr(n, "human_intervention", False)),
                    "input_artifact_type": getattr(
                        getattr(n, "input_artifact_type", None), "value", "text"
                    ),
                    "output_artifact_type": getattr(
                        getattr(n, "output_artifact_type", None), "value", "text"
                    ),
                    "input_conditions": getattr(n, "input_conditions", ""),
                    "output_conditions": getattr(n, "output_conditions", ""),
                }
                for n in sop.nodes
            ]

        return {
            "task_id": task.task_id,
            "sop_name": task.sop_name,
            "sop_version": task.sop_version,
            "status": task.status.value,
            "claimed_by": task.claimed_by,
            "claimed_at": task.claimed_at,
            "created_at": task.created_at,
            "updated_at": task.updated_at,
            "metadata": task.metadata,
            "nodes": nodes,
        }

    async def _resolve_sop(task):
        """Get the SOP for a task: prefer the live in-memory SOP (ad-hoc tasks
        are not YAML files), fall back to the registry by name."""
        sop = task_manager._task_sops.get(task.task_id)
        if sop is not None:
            return sop
        return await sop_registry.get(task.sop_name)

    @router.post("/tasks/{task_id}/start")
    async def start_task(task_id: str):
        """Start executing a task."""
        task = await task_manager.get_task(task_id)
        if not task:
            raise HTTPException(status_code=404, detail="Task not found")

        sop = await _resolve_sop(task)
        if not sop:
            raise HTTPException(status_code=404, detail=f"SOP '{task.sop_name}' not found")

        await task_manager.start_task(task_id, sop)
        return {"status": "started"}

    @router.post("/tasks/{task_id}/cancel")
    async def cancel_task(task_id: str):
        """Cancel a running task."""
        await task_manager.cancel_task(task_id)
        return {"status": "cancelled"}

    @router.post("/tasks/{task_id}/pause")
    async def pause_task(task_id: str):
        """Pause a running task."""
        await task_manager.pause_task(task_id)
        return {"status": "paused"}

    @router.post("/tasks/{task_id}/resume")
    async def resume_task(task_id: str):
        """Resume a paused task."""
        task = await task_manager.get_task(task_id)
        if not task:
            raise HTTPException(status_code=404, detail="Task not found")
        sop = await _resolve_sop(task)
        if not sop:
            raise HTTPException(status_code=404, detail=f"SOP '{task.sop_name}' not found")
        await task_manager.resume_task(task_id, sop)
        return {"status": "resumed"}

    @router.post("/tasks/{task_id}/claim")
    async def claim_task(task_id: str, req: ClaimRequest):
        """Claim a task for a specific client."""
        success = await task_manager.claim_task(task_id, req.client_id)
        if not success:
            raise HTTPException(status_code=409, detail="Task already claimed")
        return {"status": "claimed"}

    @router.post("/tasks/{task_id}/release")
    async def release_task(task_id: str):
        """Release a claimed task."""
        await task_manager.release_task(task_id)
        return {"status": "released"}

    @router.delete("/tasks/{task_id}")
    async def delete_task(task_id: str):
        """Delete a task and its events."""
        await task_manager.delete_task(task_id)
        return {"status": "deleted"}

    @router.get("/tasks/{task_id}/events")
    async def get_task_events(
        task_id: str,
        after_seq: int = 0,
        event_type: str | None = None,
        limit: int = 500,
    ):
        """Get events for a task."""
        events = await event_log.get_events(
            task_id, after_seq=after_seq, event_type=event_type, limit=limit
        )
        return [
            {
                "seq": e["seq"],
                "node_id": e["node_id"],
                "event_type": e["event_type"],
                "data": e["data"],
                "timestamp": e["timestamp"],
            }
            for e in events
        ]

    return router
