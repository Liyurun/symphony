"""Tests for the TaskManager."""

from types import SimpleNamespace

import pytest

from symphony.sop.sop_definition import NodeDefinition, SOPDefinition


@pytest.mark.asyncio
async def test_create_task(task_manager, event_bus, sample_sop):
    task = await task_manager.create_task(sample_sop)
    assert task.task_id is not None
    assert task.sop_name == "test-sop"
    assert task.status.value == "pending"

    # Should be persisted
    retrieved = await task_manager.get_task(task.task_id)
    assert retrieved is not None
    assert retrieved.sop_name == "test-sop"


@pytest.mark.asyncio
async def test_list_tasks(task_manager, event_bus, sample_sop):
    await task_manager.create_task(sample_sop)
    tasks = await task_manager.list_tasks()
    assert len(tasks) == 1
    assert tasks[0].sop_name == "test-sop"


@pytest.mark.asyncio
async def test_list_tasks_by_status(task_manager, event_bus, sample_sop):
    t1 = await task_manager.create_task(sample_sop)
    t2 = await task_manager.create_task(sample_sop)

    # Start one task, but since pi bridge isn't running,
    # it will fail immediately
    try:
        await task_manager.start_task(t1.task_id, sample_sop)
    except Exception:
        pass

    await task_manager.cancel_task(t1.task_id)

    # After cancellation, check status on the individual task
    t1_updated = await task_manager.get_task(t1.task_id)
    assert t1_updated.status.value in ("cancelled", "failed")

    # Both tasks should be listed (one cancelled/failed, one pending)
    all_tasks = await task_manager.list_tasks()
    assert len(all_tasks) == 2


@pytest.mark.asyncio
async def test_cancel_task(task_manager, event_bus, sample_sop):
    task = await task_manager.create_task(sample_sop)
    await task_manager.start_task(task.task_id, sample_sop)

    # Small delay to let the task start
    import asyncio
    await asyncio.sleep(0.1)

    await task_manager.cancel_task(task.task_id)

    retrieved = await task_manager.get_task(task.task_id)
    assert retrieved.status.value == "cancelled"


@pytest.mark.asyncio
async def test_claim_task(task_manager, event_bus, sample_sop):
    task = await task_manager.create_task(sample_sop)
    success = await task_manager.claim_task(task.task_id, "client-a")
    assert success

    # Can't claim by another
    success = await task_manager.claim_task(task.task_id, "client-b")
    assert not success

    # Release
    await task_manager.release_task(task.task_id)
    task = await task_manager.get_task(task.task_id)
    assert task.claimed_by is None


@pytest.mark.asyncio
async def test_delete_task(task_manager, event_bus, sample_sop):
    task = await task_manager.create_task(sample_sop)
    await task_manager.delete_task(task.task_id)
    retrieved = await task_manager.get_task(task.task_id)
    assert retrieved is None


@pytest.mark.asyncio
async def test_pause_resume_task(task_manager, event_bus, sample_sop):
    task = await task_manager.create_task(sample_sop)

    # Pause before start should work (no-op on internal state)
    await task_manager.pause_task(task.task_id)
    retrieved = await task_manager.get_task(task.task_id)
    assert retrieved.status.value == "paused"

    # Resume
    await task_manager.resume_task(task.task_id, sample_sop)
    retrieved = await task_manager.get_task(task.task_id)
    assert retrieved.status.value == "running"

    # Cancel to clean up
    await task_manager.cancel_task(task.task_id)


@pytest.mark.asyncio
async def test_start_nonexistent_task(task_manager):
    sop = SOPDefinition(
        name="test",
        nodes=[NodeDefinition(id="a", name="A", skill="s")],
    )
    with pytest.raises(ValueError, match="not found"):
        await task_manager.start_task("nonexistent", sop)


@pytest.mark.asyncio
async def test_plain_qa_uses_direct_llm_for_mira(task_manager, monkeypatch):
    from symphony.sop.sop_definition import NodeExecutor

    task_manager.llm_provider = SimpleNamespace(
        is_available=True,
        config=SimpleNamespace(type="mira", model="re-o-48"),
    )

    captured = {}

    async def fake_start_task(task_id, sop):
        captured["task_id"] = task_id
        captured["sop"] = sop

    monkeypatch.setattr(task_manager, "start_task", fake_start_task)

    task = await task_manager.create_and_start_qa("你好")
    assert task is not None
    assert captured["sop"].nodes[0].executor == NodeExecutor.LLM
    assert task.metadata["executor"] == NodeExecutor.LLM


@pytest.mark.asyncio
async def test_skill_qa_keeps_pi_even_for_mira(task_manager, monkeypatch):
    from symphony.sop.sop_definition import NodeExecutor

    task_manager.llm_provider = SimpleNamespace(
        is_available=True,
        config=SimpleNamespace(type="mira", model="re-o-48"),
    )

    captured = {}

    async def fake_start_task(task_id, sop):
        captured["task_id"] = task_id
        captured["sop"] = sop

    monkeypatch.setattr(task_manager, "start_task", fake_start_task)

    await task_manager.create_and_start_qa("分析一下", skill="reviewer")
    assert captured["sop"].nodes[0].executor == NodeExecutor.PI
