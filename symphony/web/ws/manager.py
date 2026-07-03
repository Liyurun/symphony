"""WebSocket connection manager — manages real-time event push to web clients.

Each connected browser gets events pushed via WebSocket.
This is how the Web UI stays in sync with the TUI in real-time.
Supports structured message protocol for bidirectional communication.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from fastapi import WebSocket, WebSocketDisconnect

from symphony.core.event_bus import EventBus, EventSubscriber, SymphonyEvent
from symphony.core.event_log import EventLog
from symphony.core.task_manager import TaskManager
from symphony.sop.sop_registry import SOPRegistry
from symphony.web.ws.protocol import (
    ClientMessage,
    ClientMessageType,
    ServerMessage,
)

logger = logging.getLogger(__name__)


class WebSocketManager:
    """Manages WebSocket connections and bridges EventBus events to browsers.

    Architecture:
    ┌──────────┐     ┌────────────────┐     ┌───────────┐
    │ EventBus │────>│ WS Manager     │────>│ Browser 1 │
    │          │     │ (subscriber)   │────>│ Browser 2 │
    └──────────┘     └────────────────┘     └───────────┘
    """

    def __init__(
        self,
        event_bus: EventBus,
        event_log: EventLog | None = None,
        task_manager: TaskManager | None = None,
        sop_registry: SOPRegistry | None = None,
    ):
        self.event_bus = event_bus
        self.event_log = event_log
        self.task_manager = task_manager
        self.sop_registry = sop_registry
        self._connections: dict[WebSocket, dict[str, Any]] = {}
        self._subscriber = _WSSubscriber(self)

    async def handle_connection(self, websocket: WebSocket) -> None:
        """Handle a new WebSocket connection."""
        await websocket.accept()
        client_id = f"web-{id(websocket)}"
        self._connections[websocket] = {
            "client_id": client_id,
            "subscribed_tasks": set(),
        }

        # Subscribe on first connection
        if len(self._connections) == 1:
            self.event_bus.subscribe(self._subscriber)

        logger.info(f"WebSocket connected: {client_id} ({len(self._connections)} total)")

        try:
            # Send initial state
            await self._send_initial_state(websocket)

            # Handle incoming messages
            while True:
                data = await websocket.receive_text()
                try:
                    msg_dict = json.loads(data)
                    await self._handle_client_message(websocket, msg_dict)
                except json.JSONDecodeError:
                    logger.debug(f"Invalid JSON from client: {data[:100]}")
        except (WebSocketDisconnect, asyncio.CancelledError):
            # Normal browser disconnects and server shutdown cancellation should
            # not be logged by uvicorn as application errors. Cleanup happens in
            # the finally block below.
            pass
        except Exception as e:
            logger.debug(f"WebSocket disconnected: {e}")
        finally:
            self._connections.pop(websocket, None)
            if not self._connections:
                self.event_bus.unsubscribe(self._subscriber)
            logger.info(f"WebSocket disconnected: {client_id} ({len(self._connections)} remaining)")

    async def broadcast(self, event: SymphonyEvent) -> None:
        """Push an event to all connected browsers that are subscribed to the task."""
        if not self._connections:
            return

        msg = ServerMessage.from_symphony_event(event)
        payload = msg.model_dump_json()

        disconnected = set()
        for ws, conn_info in self._connections.items():
            # Only send to clients subscribed to this task, or all if no subscriptions
            subscribed = conn_info.get("subscribed_tasks", set())
            if subscribed and event.task_id not in subscribed:
                continue

            try:
                await ws.send_text(payload)
            except Exception:
                disconnected.add(ws)

        for ws in disconnected:
            self._connections.pop(ws, None)

    async def _send_initial_state(self, websocket: WebSocket) -> None:
        """Send initial state to a newly connected client."""
        tasks = []
        sops = []

        if self.task_manager:
            task_list = await self.task_manager.list_tasks()
            tasks = [
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
                for t in task_list
            ]

        if self.sop_registry:
            sop_list = await self.sop_registry.list_all()
            sops = [
                {
                    "name": s.name,
                    "version": s.version,
                    "description": s.description,
                    "input_requirements": s.input_requirements,
                    "output_requirements": s.output_requirements,
                    "node_count": len(s.nodes),
                    "nodes": [n.model_dump() for n in s.nodes],
                }
                for s in sop_list
            ]

        msg = ServerMessage.initial_state(tasks, sops)
        await websocket.send_text(msg.model_dump_json())

    async def _handle_client_message(self, websocket: WebSocket, msg_dict: dict) -> None:
        """Handle a structured message from a web client."""
        try:
            msg = ClientMessage(**msg_dict)
        except Exception:
            logger.debug(f"Invalid client message format: {msg_dict}")
            return

        conn_info = self._connections.get(websocket, {})
        client_id = conn_info.get("client_id", "unknown")

        if msg.type == ClientMessageType.SUBSCRIBE_TASK and msg.task_id:
            conn_info.setdefault("subscribed_tasks", set()).add(msg.task_id)
            logger.debug(f"Client {client_id} subscribed to task {msg.task_id}")

        elif msg.type == ClientMessageType.UNSUBSCRIBE_TASK and msg.task_id:
            conn_info.get("subscribed_tasks", set()).discard(msg.task_id)

        elif msg.type == ClientMessageType.USER_INPUT and msg.task_id:
            await self.event_bus.publish(
                SymphonyEvent(
                    task_id=msg.task_id,
                    event_type="user_input",
                    data={"message": msg.message, "source": "web", "client_id": client_id},
                )
            )
            # 方案A multi-turn: route the message as a follow-up turn on the task
            # (appends a new node depending on the previous turn), so the input
            # bar drives the SOP/task flow rather than blasting the raw pi bridge.
            if self.task_manager and msg.message:
                try:
                    await self.task_manager.ask_follow_up(msg.task_id, msg.message)
                except ValueError:
                    # No active SOP for this task (e.g. never started) — fall back
                    # to sending the prompt straight to pi. Prefer this task's own
                    # bridge if it still exists, else the shared control bridge.
                    pool = getattr(self.task_manager, "pi_pool", None)
                    bridge = pool.get(msg.task_id) if pool else self.task_manager.pi_bridge
                    await bridge.send_prompt(msg.message)

        elif msg.type == ClientMessageType.HUMAN_RESPONSE and msg.task_id and msg.node_id:
            # Route through the task manager's SHARED human manager so the
            # executor's awaiting future actually resolves.
            if self.task_manager:
                await self.task_manager.respond_human(
                    msg.task_id, msg.node_id, msg.approved, msg.feedback
                )

        elif msg.type == ClientMessageType.CREATE_TASK and (msg.sop_name or msg.prompt):
            await self._handle_create_task(websocket, msg)

        elif msg.type == ClientMessageType.START_TASK and msg.task_id:
            await self._handle_start_task(websocket, msg)

        elif msg.type == ClientMessageType.CANCEL_TASK and msg.task_id:
            await self._handle_cancel_task(websocket, msg)

        elif msg.type == ClientMessageType.PAUSE_TASK and msg.task_id:
            await self._handle_pause_task(websocket, msg)

        elif msg.type == ClientMessageType.RESUME_TASK and msg.task_id:
            await self._handle_resume_task(websocket, msg)

        elif msg.type == ClientMessageType.CLAIM_TASK and msg.task_id:
            await self._handle_claim_task(websocket, msg, client_id)

        elif msg.type == ClientMessageType.RELEASE_TASK and msg.task_id:
            await self._handle_release_task(websocket, msg)

    async def _handle_create_task(self, websocket: WebSocket, msg: ClientMessage) -> None:
        if not self.task_manager:
            return

        # 方案A · ad-hoc: no SOP selected, just a free-form question. Create a
        # single-node Q&A task and auto-start it (Claude-style "type & go").
        if not msg.sop_name and msg.prompt:
            task = await self.task_manager.create_and_start_qa(msg.prompt)
            update = ServerMessage.task_status_update(
                task.task_id, "created",
                sop_name=task.sop_name, started=True, adhoc=True,
            )
            await websocket.send_text(update.model_dump_json())
            return

        # SOP-backed path: resolve the named template.
        if not self.sop_registry:
            return
        sop = await self.sop_registry.get(msg.sop_name)
        if not sop:
            err = ServerMessage.error(f"SOP '{msg.sop_name}' not found")
            await websocket.send_text(err.model_dump_json())
            return

        task = await self.task_manager.create_task(sop)
        update = ServerMessage.task_status_update(
            task.task_id, "created",
            sop_name=task.sop_name,
        )
        await websocket.send_text(update.model_dump_json())

    async def _resolve_task_sop(self, task):
        """Resolve a task's SOP: prefer the in-memory cache (ad-hoc SOPs are not
        files in the registry), then fall back to the named registry template."""
        sop = self.task_manager._task_sops.get(task.task_id) if self.task_manager else None
        if sop is not None:
            return sop
        if self.sop_registry:
            return await self.sop_registry.get(task.sop_name)
        return None

    async def _handle_start_task(self, websocket: WebSocket, msg: ClientMessage) -> None:
        if not self.task_manager or not msg.task_id:
            return
        task = await self.task_manager.get_task(msg.task_id)
        if not task:
            err = ServerMessage.error("Task not found", msg.task_id)
            await websocket.send_text(err.model_dump_json())
            return

        sop = await self._resolve_task_sop(task)
        if not sop:
            err = ServerMessage.error(f"SOP '{task.sop_name}' not found", msg.task_id)
            await websocket.send_text(err.model_dump_json())
            return

        await self.task_manager.start_task(msg.task_id, sop)
        update = ServerMessage.task_status_update(msg.task_id, "running")
        await websocket.send_text(update.model_dump_json())

    async def _handle_cancel_task(self, websocket: WebSocket, msg: ClientMessage) -> None:
        if not self.task_manager or not msg.task_id:
            return
        await self.task_manager.cancel_task(msg.task_id)
        update = ServerMessage.task_status_update(msg.task_id, "cancelled")
        await websocket.send_text(update.model_dump_json())

    async def _handle_pause_task(self, websocket: WebSocket, msg: ClientMessage) -> None:
        if not self.task_manager or not msg.task_id:
            return
        await self.task_manager.pause_task(msg.task_id)
        update = ServerMessage.task_status_update(msg.task_id, "paused")
        await websocket.send_text(update.model_dump_json())

    async def _handle_resume_task(self, websocket: WebSocket, msg: ClientMessage) -> None:
        if not self.task_manager or not msg.task_id:
            return
        task = await self.task_manager.get_task(msg.task_id)
        if not task:
            return
        sop = await self._resolve_task_sop(task)
        if not sop:
            return
        await self.task_manager.resume_task(msg.task_id, sop)
        update = ServerMessage.task_status_update(msg.task_id, "running")
        await websocket.send_text(update.model_dump_json())

    async def _handle_claim_task(
        self, websocket: WebSocket, msg: ClientMessage, client_id: str
    ) -> None:
        if not self.task_manager or not msg.task_id:
            return
        success = await self.task_manager.claim_task(msg.task_id, client_id)
        if success:
            update = ServerMessage.task_status_update(
                msg.task_id, "claimed", client_id=client_id
            )
        else:
            update = ServerMessage.error("Task already claimed", msg.task_id)
        await websocket.send_text(update.model_dump_json())

    async def _handle_release_task(self, websocket: WebSocket, msg: ClientMessage) -> None:
        if not self.task_manager or not msg.task_id:
            return
        await self.task_manager.release_task(msg.task_id)
        update = ServerMessage.task_status_update(msg.task_id, "released")
        await websocket.send_text(update.model_dump_json())


class _WSSubscriber(EventSubscriber):
    """Bridges EventBus events to WebSocket connections."""

    def __init__(self, manager: WebSocketManager):
        self.manager = manager

    async def on_event(self, event: SymphonyEvent) -> None:
        await self.manager.broadcast(event)
