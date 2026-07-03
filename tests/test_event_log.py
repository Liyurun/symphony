"""Tests for the EventLog."""

import pytest
import time


@pytest.mark.asyncio
async def test_connect_and_schema(event_log):
    """Test that the event log connects and creates the storage directories."""
    assert event_log._connected is True

    # Verify the storage directories exist
    assert event_log.logs_dir.is_dir()
    assert event_log.tasks_dir.is_dir()
    assert event_log.templates_dir.is_dir()


@pytest.mark.asyncio
async def test_create_and_get_task(event_log):
    await event_log.create_task("task-1", "test-sop", "1.0", {"key": "val"})
    task = await event_log.get_task("task-1")
    assert task is not None
    assert task["task_id"] == "task-1"
    assert task["sop_name"] == "test-sop"
    assert task["status"] == "pending"


@pytest.mark.asyncio
async def test_list_tasks(event_log):
    await event_log.create_task("task-1", "sop-a")
    await event_log.create_task("task-2", "sop-b")
    await event_log.update_task_status("task-2", "running")

    tasks = await event_log.list_tasks()
    assert len(tasks) == 2

    running_tasks = await event_log.list_tasks(status="running")
    assert len(running_tasks) == 1
    assert running_tasks[0]["task_id"] == "task-2"


@pytest.mark.asyncio
async def test_append_and_get_events(event_log):
    await event_log.create_task("task-1", "test-sop")
    seq1 = await event_log.append({
        "task_id": "task-1",
        "event_type": "node_started",
        "node_id": "analyze",
        "data": {"msg": "hello"},
        "timestamp": time.time(),
    })
    seq2 = await event_log.append({
        "task_id": "task-1",
        "event_type": "node_completed",
        "node_id": "analyze",
        "data": {},
        "timestamp": time.time(),
    })

    events = await event_log.get_events("task-1")
    assert len(events) == 2
    assert events[0]["seq"] == 1
    assert events[1]["seq"] == 2

    events_after = await event_log.get_events("task-1", after_seq=1)
    assert len(events_after) == 1
    assert events_after[0]["seq"] == 2


@pytest.mark.asyncio
async def test_search_events(event_log):
    await event_log.create_task("task-1", "test-sop")
    await event_log.append({
        "task_id": "task-1",
        "event_type": "node_started",
        "node_id": "a",
        "data": {},
        "timestamp": time.time(),
    })
    await event_log.append({
        "task_id": "task-1",
        "event_type": "error",
        "node_id": "a",
        "data": {"error": "test"},
        "timestamp": time.time(),
    })

    results = await event_log.search_events(event_type="error")
    assert len(results) == 1
    assert results[0]["event_type"] == "error"

    results = await event_log.search_events(task_id="task-1", event_type="node_started")
    assert len(results) == 1


@pytest.mark.asyncio
async def test_task_claim_and_release(event_log):
    await event_log.create_task("task-1", "test-sop")

    success = await event_log.claim_task("task-1", "client-a")
    assert success

    # Can't claim by another client
    success = await event_log.claim_task("task-1", "client-b")
    assert not success

    task = await event_log.get_task("task-1")
    assert task["claimed_by"] == "client-a"

    await event_log.release_task("task-1")
    task = await event_log.get_task("task-1")
    assert task["claimed_by"] is None


@pytest.mark.asyncio
async def test_delete_task(event_log):
    await event_log.create_task("task-1", "test-sop")
    await event_log.append({
        "task_id": "task-1",
        "event_type": "test",
        "data": {},
        "timestamp": time.time(),
    })
    await event_log.delete_task("task-1")
    task = await event_log.get_task("task-1")
    assert task is None
    events = await event_log.get_events("task-1")
    assert len(events) == 0


@pytest.mark.asyncio
async def test_sop_template_crud(event_log):
    await event_log.save_sop_template("test", "1.0", {"name": "test", "nodes": []})
    tpl = await event_log.get_sop_template("test")
    assert tpl is not None
    assert tpl["name"] == "test"

    templates = await event_log.list_sop_templates()
    assert len(templates) == 1

    await event_log.delete_sop_template("test")
    tpl = await event_log.get_sop_template("test")
    assert tpl is None


@pytest.mark.asyncio
async def test_event_stats(event_log):
    await event_log.create_task("task-1", "test")
    await event_log.append({"task_id": "task-1", "event_type": "node_started", "data": {}, "timestamp": time.time()})
    await event_log.append({"task_id": "task-1", "event_type": "node_completed", "data": {}, "timestamp": time.time()})

    stats = await event_log.get_event_stats()
    assert stats["total_tasks"] == 1
    assert stats["total_events"] == 2
    assert "node_started" in stats["events_by_type"]
