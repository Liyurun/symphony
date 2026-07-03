"""Tests for the HumanInterventionManager."""

import asyncio

import pytest

from symphony.core.event_bus import SymphonyEvent
from symphony.sop.human_loop import HumanInterventionManager


@pytest.mark.asyncio
async def test_request_and_respond_approval(event_bus):
    hm = HumanInterventionManager(event_bus)

    # Start an approval request in background
    async def request():
        return await hm.request_approval("task-1", "node-1", "Test Node", {"data": "ok"}, timeout=5)

    task = asyncio.create_task(request())

    # Small delay to ensure the request is waiting
    await asyncio.sleep(0.1)

    # Respond with approval
    await hm.respond("task-1", "node-1", True, "Looks good")

    approved, feedback = await task
    assert approved is True
    assert feedback == "Looks good"


@pytest.mark.asyncio
async def test_request_and_reject(event_bus):
    hm = HumanInterventionManager(event_bus)

    async def request():
        return await hm.request_approval("task-1", "node-1", "Test", {}, timeout=5)

    task = asyncio.create_task(request())
    await asyncio.sleep(0.1)

    await hm.respond("task-1", "node-1", False, "Needs improvement")

    approved, feedback = await task
    assert approved is False
    assert feedback == "Needs improvement"


@pytest.mark.asyncio
async def test_timeout(event_bus):
    hm = HumanInterventionManager(event_bus)
    approved, feedback = await hm.request_approval(
        "task-1", "node-1", "Test", {}, timeout=0.1
    )
    assert approved is False
    assert feedback == "timeout"


@pytest.mark.asyncio
async def test_publishes_events(event_bus, event_log):
    await event_log.create_task("task-1", "test-sop")
    hm = HumanInterventionManager(event_bus)

    async def request():
        return await hm.request_approval("task-1", "node-1", "Test Node", {"result": "ok"}, timeout=2)

    task = asyncio.create_task(request())
    await asyncio.sleep(0.1)
    await hm.respond("task-1", "node-1", True, "")

    await task

    # Verify events were persisted
    events = await event_log.get_events("task-1")
    event_types = [e["event_type"] for e in events]
    assert "human_intervention_required" in event_types
    assert "human_intervention_response" in event_types
