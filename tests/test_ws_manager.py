"""Tests for WebSocket protocol messages."""

import json

import pytest

from symphony.web.ws.manager import WebSocketManager
from symphony.web.ws.protocol import (
    ClientMessage,
    ClientMessageType,
    ServerMessage,
    ServerMessageType,
)


class TestClientMessage:
    def test_subscribe_task(self):
        msg = ClientMessage(type=ClientMessageType.SUBSCRIBE_TASK, task_id="task-1")
        assert msg.type == ClientMessageType.SUBSCRIBE_TASK
        assert msg.task_id == "task-1"

    def test_human_response(self):
        msg = ClientMessage(
            type=ClientMessageType.HUMAN_RESPONSE,
            task_id="task-1",
            node_id="node-1",
            approved=True,
            feedback="Looks good",
        )
        assert msg.type == ClientMessageType.HUMAN_RESPONSE
        assert msg.approved is True

    def test_create_task(self):
        msg = ClientMessage(
            type=ClientMessageType.CREATE_TASK,
            sop_name="code-review",
        )
        assert msg.sop_name == "code-review"

    def test_create_task_with_prompt(self):
        # 方案A ad-hoc: create a task from a free-form question, no SOP.
        msg = ClientMessage(
            type=ClientMessageType.CREATE_TASK,
            prompt="What is 2+2?",
        )
        assert msg.prompt == "What is 2+2?"
        assert msg.sop_name == ""


class TestServerMessage:
    def test_from_symphony_event(self):
        from symphony.core.event_bus import SymphonyEvent

        evt = SymphonyEvent(
            task_id="task-1",
            event_type="node_started",
            node_id="node-a",
            data={"key": "val"},
        )
        msg = ServerMessage.from_symphony_event(evt)
        assert msg.type == ServerMessageType.EVENT
        assert msg.task_id == "task-1"
        assert msg.event_type == "node_started"

    def test_task_status_update(self):
        msg = ServerMessage.task_status_update("task-1", "running")
        assert msg.type == ServerMessageType.TASK_UPDATE
        assert msg.data["status"] == "running"

    def test_error(self):
        msg = ServerMessage.error("Something went wrong", "task-1")
        assert msg.type == ServerMessageType.ERROR
        assert msg.message == "Something went wrong"

    def test_initial_state(self):
        tasks = [{"task_id": "t1", "status": "running"}]
        sops = [{"name": "review", "nodes": 3}]
        msg = ServerMessage.initial_state(tasks, sops)
        assert msg.type == ServerMessageType.INITIAL_STATE
        assert len(msg.data["tasks"]) == 1
        assert len(msg.data["sops"]) == 1


class _FakeWebSocket:
    """Captures every text frame the manager sends."""

    def __init__(self):
        self.sent: list[dict] = []

    async def send_text(self, text: str) -> None:
        self.sent.append(json.loads(text))


class TestWSManagerCreateAndResolve:
    """Exercise the manager's Claude-style ad-hoc create + SOP resolution paths."""

    @pytest.mark.asyncio
    async def test_ws_create_adhoc_from_prompt(
        self, event_bus, event_log, task_manager
    ):
        """CREATE_TASK with only a prompt (no sop_name) synthesizes a one-node
        Q&A task and auto-starts it (Claude-style type & go)."""
        from symphony.sop.sop_registry import SOPRegistry

        registry = SOPRegistry(event_log)
        mgr = WebSocketManager(event_bus, event_log, task_manager, registry)
        ws = _FakeWebSocket()

        msg = ClientMessage(type=ClientMessageType.CREATE_TASK, prompt="hello there")
        await mgr._handle_create_task(ws, msg)

        assert len(ws.sent) == 1
        payload = ws.sent[0]
        assert payload["type"] == "task_update"
        assert payload["data"]["status"] == "created"
        assert payload["data"]["started"] is True
        assert payload["data"]["adhoc"] is True

        # The ad-hoc SOP is cached so start/resume can re-resolve it.
        task_id = payload["task_id"]
        assert task_id in task_manager._task_sops

    @pytest.mark.asyncio
    async def test_ws_start_resolves_adhoc_sop_from_cache(
        self, event_bus, event_log, task_manager
    ):
        """_handle_start_task resolves an ad-hoc SOP from the task_manager cache
        (it is not a file in the registry)."""
        from symphony.sop.sop_registry import SOPRegistry

        registry = SOPRegistry(event_log)  # empty registry — no templates
        mgr = WebSocketManager(event_bus, event_log, task_manager, registry)

        # Create (but don't start) an ad-hoc task directly via the task manager.
        from symphony.sop.sop_definition import make_adhoc_sop

        sop = make_adhoc_sop("resolve me")
        task = await task_manager.create_task(
            sop, metadata={"adhoc": True, "prompt": "resolve me"}
        )

        resolved = await mgr._resolve_task_sop(task)
        assert resolved is not None
        assert resolved.name == sop.name

    @pytest.mark.asyncio
    async def test_ws_resolve_falls_back_to_registry(
        self, event_bus, event_log, task_manager, sample_sop
    ):
        """When a task's SOP is not cached, resolution falls back to the named
        registry template."""
        from symphony.sop.sop_registry import SOPRegistry

        registry = SOPRegistry(event_log)
        await registry.register(sample_sop)
        mgr = WebSocketManager(event_bus, event_log, task_manager, registry)

        # A task object whose SOP is only in the registry (not cached).
        from symphony.core.task_manager import Task

        task = Task(task_id="nope-not-cached", sop_name=sample_sop.name)
        resolved = await mgr._resolve_task_sop(task)
        assert resolved is not None
        assert resolved.name == sample_sop.name
