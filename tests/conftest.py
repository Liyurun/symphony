"""Shared test fixtures."""

import asyncio
import tempfile
from pathlib import Path

import pytest
import pytest_asyncio

from symphony.core.event_bus import EventBus
from symphony.core.event_log import EventLog
from symphony.core.pi_bridge import PiBridge, PiBridgeConfig
from symphony.core.task_manager import TaskManager
from symphony.sop.sop_definition import NodeDefinition, SOPDefinition


@pytest.fixture
def temp_data_dir():
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest_asyncio.fixture
async def event_log(temp_data_dir):
    log = EventLog(temp_data_dir)
    await log.connect()
    yield log
    await log.close()


@pytest_asyncio.fixture
async def event_bus(event_log):
    return EventBus(event_log)


@pytest.fixture
def pi_bridge_config():
    return PiBridgeConfig(pi_binary="echo")


@pytest.fixture
def pi_bridge(pi_bridge_config):
    """Create a PiBridge without starting it (for unit tests)."""
    return PiBridge(pi_bridge_config)


@pytest_asyncio.fixture
async def task_manager(event_bus, event_log, pi_bridge):
    tm = TaskManager(event_bus, event_log, pi_bridge)
    yield tm
    # Teardown: cancel any background asyncio tasks the manager spawned (e.g.
    # auto-started ad-hoc tasks). Without this, a leaked running task can keep
    # the interpreter alive at exit and intermittently hang a full-suite run,
    # even though each file passes in isolation.
    #
    # We only *request* cancellation here (no await): some of these tasks were
    # created on a different event loop (e.g. Starlette TestClient's portal),
    # and awaiting a task that belongs to an already-closed loop would itself
    # hang. Cancellation is enough to let the loop tear down cleanly.
    for task in list(tm._tasks.values()):
        t = getattr(task, "_asyncio_task", None)
        if t is not None and not t.done():
            t.cancel()


@pytest.fixture
def sample_sop():
    """A simple SOP with 3 nodes in a linear chain."""
    return SOPDefinition(
        name="test-sop",
        version="1.0",
        description="Test SOP",
        nodes=[
            NodeDefinition(
                id="analyze",
                name="Analyze",
                skill="test-analyze",
                description="Analyze step",
            ),
            NodeDefinition(
                id="review",
                name="Review",
                skill="test-review",
                depends_on=["analyze"],
                description="Review step",
            ),
            NodeDefinition(
                id="report",
                name="Report",
                skill="test-report",
                depends_on=["review"],
                description="Report step",
            ),
        ],
    )


@pytest.fixture
def parallel_sop():
    """A SOP with parallelizable nodes."""
    return SOPDefinition(
        name="parallel-test",
        nodes=[
            NodeDefinition(id="a", name="Node A", skill="skill-a"),
            NodeDefinition(id="b", name="Node B", skill="skill-b"),
            NodeDefinition(id="c", name="Node C", skill="skill-c", depends_on=["a", "b"]),
        ],
    )
