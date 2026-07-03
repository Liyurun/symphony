"""Tests for the EventBus."""

import asyncio

import pytest

from symphony.core.event_bus import EventSubscriber, SymphonyEvent


class _TestSubscriber(EventSubscriber):
    """Simple subscriber for testing."""
    def __init__(self):
        self.events = []

    async def on_event(self, event: SymphonyEvent) -> None:
        self.events.append(event)


@pytest.mark.asyncio
async def test_publish_and_subscribe(event_bus, event_log):
    await event_log.create_task("task-1", "test-sop")
    sub = _TestSubscriber()
    event_bus.subscribe(sub)

    seq = await event_bus.publish(
        SymphonyEvent(task_id="task-1", event_type="test_event", data={"key": "val"})
    )
    assert seq == 1
    assert len(sub.events) == 1
    assert sub.events[0].task_id == "task-1"
    assert sub.events[0].event_type == "test_event"


@pytest.mark.asyncio
async def test_unsubscribe(event_bus, event_log):
    await event_log.create_task("task-1", "test-sop")
    sub = _TestSubscriber()
    event_bus.subscribe(sub)
    event_bus.unsubscribe(sub)

    await event_bus.publish(
        SymphonyEvent(task_id="task-1", event_type="test_event", data={})
    )
    assert len(sub.events) == 0


@pytest.mark.asyncio
async def test_task_queue(event_bus, event_log):
    await event_log.create_task("task-1", "test-sop")
    queue = event_bus.get_task_queue("task-1")

    await event_bus.publish(
        SymphonyEvent(task_id="task-1", event_type="test_event", data={})
    )

    event = await asyncio.wait_for(queue.get(), timeout=1)
    assert event.task_id == "task-1"

    event_bus.remove_task_queue("task-1")
    assert "task-1" not in event_bus._task_queues


@pytest.mark.asyncio
async def test_persistence(event_bus, event_log):
    """Events should be persisted to the event log."""
    await event_log.create_task("task-1", "test-sop")

    await event_bus.publish(
        SymphonyEvent(task_id="task-1", event_type="e1", data={})
    )
    await event_bus.publish(
        SymphonyEvent(task_id="task-1", event_type="e2", data={})
    )

    events = await event_log.get_events("task-1")
    assert len(events) == 2
    assert events[0]["seq"] == 1
    assert events[1]["seq"] == 2
