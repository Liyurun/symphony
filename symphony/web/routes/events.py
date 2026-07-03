"""Events API — SSE streaming, human intervention response, and event search."""

from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from symphony.core.event_bus import EventBus
from symphony.core.event_log import EventLog


def create_events_router(event_log: EventLog, event_bus: EventBus, human_manager=None) -> APIRouter:
    router = APIRouter(tags=["events"])

    @router.get("/events/{task_id}/stream")
    async def stream_task_events(task_id: str, request: Request):
        """SSE endpoint for real-time task event streaming."""
        queue = event_bus.get_task_queue(task_id)

        async def event_generator():
            # First, send all historical events
            events = await event_log.get_events(task_id)
            for evt in events:
                yield f"data: {json.dumps(_serialize_event(evt))}\n\n"

            # Then, stream live events
            try:
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        event = await asyncio.wait_for(queue.get(), timeout=15)
                        yield f"data: {json.dumps(_serialize_live_event(event))}\n\n"
                    except asyncio.TimeoutError:
                        yield ": keepalive\n\n"
            finally:
                event_bus.remove_task_queue(task_id)

        return StreamingResponse(
            event_generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    @router.post("/human/respond")
    async def human_respond(request: Request):
        """Respond to a human intervention request."""
        data = await request.json()
        task_id = data["task_id"]
        node_id = data["node_id"]
        approved = data["approved"]
        feedback = data.get("feedback", "")

        # Must use the SHARED human manager (owned by TaskManager) so the
        # executor's awaiting future resolves. A fresh instance would no-op.
        if human_manager is None:
            return {"status": "error", "message": "human manager not wired"}
        await human_manager.respond(task_id, node_id, approved, feedback)

        return {"status": "responded"}

    @router.post("/human/answer")
    async def human_answer(request: Request):
        """Answer a node's pending ``needs_user_input`` question."""
        data = await request.json()
        task_id = data["task_id"]
        node_id = data["node_id"]
        answer = data.get("answer", "")

        if human_manager is None:
            return {"status": "error", "message": "human manager not wired"}
        await human_manager.answer(task_id, node_id, answer)

        return {"status": "answered"}

    @router.get("/logs")
    async def search_events(
        task_id: str | None = None,
        event_type: str | None = None,
        node_id: str | None = None,
        limit: int = 200,
        offset: int = 0,
    ):
        """Search events with optional filters."""
        events = await event_log.search_events(
            task_id=task_id,
            event_type=event_type,
            node_id=node_id,
            limit=limit,
            offset=offset,
        )
        return [_serialize_event(e) for e in events]

    @router.get("/logs/stats")
    async def get_event_stats():
        """Get event statistics."""
        return await event_log.get_event_stats()

    @router.get("/tasks/{task_id}/export")
    async def export_task_events(task_id: str):
        """Export all events for a task as JSON."""
        events = await event_log.get_events(task_id, limit=10000)
        task = await event_log.get_task(task_id)
        return {
            "task": task,
            "events": [_serialize_event(e) for e in events],
        }

    return router


def _serialize_event(evt: dict) -> dict:
    """Serialize a DB event row to a JSON-safe dict."""
    data = evt.get("data", {})
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except (json.JSONDecodeError, TypeError):
            pass

    return {
        "seq": evt["seq"],
        "node_id": evt.get("node_id"),
        "event_type": evt["event_type"],
        "data": data,
        "timestamp": evt["timestamp"],
    }


def _serialize_live_event(event) -> dict:
    """Serialize a live SymphonyEvent to a JSON-safe dict."""
    return {
        "seq": event.seq,
        "node_id": event.node_id,
        "event_type": event.event_type,
        "data": event.data,
        "timestamp": event.timestamp,
    }
