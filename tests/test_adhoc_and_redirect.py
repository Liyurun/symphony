"""Tests for 方案A (ad-hoc Q&A tasks), root-node input seeding, node-level
interrupt/redirect (打断并重来) with auto-cascade, and the enhanced create-task
API (prompt/inputs + auto_start)."""

import asyncio

import pytest
from fastapi.testclient import TestClient

from symphony.core.event_bus import EventBus
from symphony.core.event_log import EventLog
from symphony.core.pi_bridge import PiBridge
from symphony.sop.sop_definition import (
    NodeDefinition,
    SOPDefinition,
    append_turn_node,
    make_adhoc_node,
    make_adhoc_sop,
)
from symphony.sop.sop_executor import SOPExecutor, NodeStatus
from symphony.sop.sop_registry import SOPRegistry
from symphony.web.server import WebServer


# ── Ad-hoc SOP builders (item 6) ──────────────────────────────────

def test_make_adhoc_sop_single_node():
    sop = make_adhoc_sop("What is 2+2?")
    assert len(sop.nodes) == 1
    node = sop.nodes[0]
    assert node.id == "turn-1"
    assert node.skill == ""  # pure-prompt node
    assert node.description == "What is 2+2?"
    assert node.depends_on == []
    assert sop.metadata.get("adhoc") is True


def test_append_turn_node_chains_on_previous():
    sop = make_adhoc_sop("First question")
    node2 = append_turn_node(sop, "Follow-up question")
    assert len(sop.nodes) == 2
    assert node2.id == "turn-2"
    assert node2.depends_on == ["turn-1"]  # conversation continuity


def test_make_adhoc_node_name_truncation():
    long_prompt = "x" * 100
    node = make_adhoc_node(long_prompt)
    assert node.name.endswith("…")
    assert len(node.name) <= 41


# ── pi_bridge pure-prompt branch (item 6) ─────────────────────────

def test_build_skill_prompt_with_skill():
    p = PiBridge._build_skill_prompt("code-review", "check this")
    assert p == "/skill:code-review check this"


def test_build_skill_prompt_empty_skill_is_raw_prompt():
    # Ad-hoc node: no /skill: prefix, raw question sent to pi.
    p = PiBridge._build_skill_prompt("", "What is the capital of France?")
    assert p == "What is the capital of France?"


# ── downstream closure (item 7) ───────────────────────────────────

def test_downstream_closure_linear(sample_sop):
    closure = SOPExecutor._downstream_closure(sample_sop, "analyze")
    assert set(closure) == {"analyze", "review", "report"}


def test_downstream_closure_from_middle(sample_sop):
    closure = SOPExecutor._downstream_closure(sample_sop, "review")
    assert set(closure) == {"review", "report"}
    assert "analyze" not in closure


def test_downstream_closure_leaf(sample_sop):
    closure = SOPExecutor._downstream_closure(sample_sop, "report")
    assert set(closure) == {"report"}


# ── root-node input seeding (item 4) ──────────────────────────────

@pytest.mark.asyncio
async def test_prepare_input_seeds_root_node(event_bus, event_log, pi_bridge):
    executor = SOPExecutor(pi_bridge, event_log, event_bus)
    executor._initial_input = {"prompt": "hello"}
    root = NodeDefinition(id="root", name="Root", skill="s")
    sop = SOPDefinition(name="s", nodes=[root])
    node_input = executor._prepare_input(sop, root, {})
    assert node_input == {"prompt": "hello"}


@pytest.mark.asyncio
async def test_prepare_input_dependency_over_root(event_bus, event_log, pi_bridge):
    executor = SOPExecutor(pi_bridge, event_log, event_bus)
    executor._initial_input = {"prompt": "hello"}
    child = NodeDefinition(id="child", name="Child", skill="s", depends_on=["root"])
    sop = SOPDefinition(name="s", nodes=[
        NodeDefinition(id="root", name="Root", skill="s"),
        child,
    ])
    results = {"root": {"status": NodeStatus.COMPLETED, "result": {"x": 1}}}
    node_input = executor._prepare_input(sop, child, results)
    # Non-root node gets dependency output, NOT the initial input. The reserved
    # accumulated-ancestor context is added separately.
    assert node_input["root"] == {"x": 1}
    assert "prompt" not in node_input
    assert [c["node_id"] for c in node_input["_ancestor_context"]] == ["root"]


# ── redirect_node interrupt state (item 7) ────────────────────────

@pytest.mark.asyncio
async def test_request_node_interrupt_bumps_generation(event_bus, event_log, pi_bridge):
    executor = SOPExecutor(pi_bridge, event_log, event_bus)
    assert executor._current_generation("t1", "n1") == 0
    executor.request_node_interrupt("t1", "n1", "please redo")
    assert executor._current_generation("t1", "n1") == 1
    assert executor._extra_instructions[("t1", "n1")] == "please redo"
    assert executor._interrupts[("t1", "n1")].is_set()


# ── enhanced create-task API (item 4) ─────────────────────────────

def _make_app(event_log, event_bus, task_manager, sop_registry):
    server = WebServer(
        event_bus=event_bus,
        event_log=event_log,
        task_manager=task_manager,
        sop_registry=sop_registry,
    )
    return server.app


async def _drain_task(task_manager, task_id: str) -> None:
    """Best-effort stop of a just-autostarted task's background asyncio task.

    Autostart fires ``asyncio.create_task(_run_task(...))`` on whichever loop
    handled the request (under ``TestClient`` that is the Starlette portal loop).
    Cross-loop hang safety no longer depends on this helper: ``EventLog`` runs
    all DB work on its own private owner loop, so a leaked task can never orphan
    an aiosqlite future on a closing portal loop. We still request cooperative
    cancellation to keep leaked tasks from lingering, and await only when the
    task lives on *this* loop.
    """
    task = task_manager._tasks.get(task_id)
    if not task:
        return
    task._cancel_event.set()
    bg = getattr(task, "_asyncio_task", None)
    if bg is None or bg.done():
        return
    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        running = None
    if running is not None and bg.get_loop() is running:
        bg.cancel()
        try:
            await bg
        except (asyncio.CancelledError, Exception):
            pass
    else:
        bg.cancel()


@pytest.mark.asyncio
async def test_create_task_with_prompt_autostart(
    event_bus, event_log, task_manager, sample_sop
):
    registry = SOPRegistry(event_log)
    await registry.register(sample_sop)
    app = _make_app(event_log, event_bus, task_manager, registry)
    with TestClient(app) as client:
        resp = client.post(
            "/api/tasks",
            json={"sop_name": "test-sop", "prompt": "analyze my repo"},
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["started"] is True
    # The prompt is stored in metadata so the root node receives it.
    task = await task_manager.get_task(data["task_id"])
    assert task.metadata.get("prompt") == "analyze my repo"
    await _drain_task(task_manager, data["task_id"])


@pytest.mark.asyncio
async def test_create_adhoc_task_without_sop(event_bus, event_log, task_manager):
    registry = SOPRegistry(event_log)
    app = _make_app(event_log, event_bus, task_manager, registry)
    with TestClient(app) as client:
        resp = client.post("/api/tasks", json={"prompt": "hi there"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["sop_name"].startswith("ask-")
    assert data["started"] is True
    await _drain_task(task_manager, data["task_id"])


@pytest.mark.asyncio
async def test_create_task_requires_sop_or_prompt(event_bus, event_log, task_manager):
    registry = SOPRegistry(event_log)
    app = _make_app(event_log, event_bus, task_manager, registry)
    with TestClient(app) as client:
        resp = client.post("/api/tasks", json={})
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_ask_endpoint(event_bus, event_log, task_manager):
    registry = SOPRegistry(event_log)
    app = _make_app(event_log, event_bus, task_manager, registry)
    with TestClient(app) as client:
        resp = client.post("/api/ask", json={"prompt": "quick question"})
    assert resp.status_code == 200
    data = resp.json()
    assert "task_id" in data
    await _drain_task(task_manager, data["task_id"])


@pytest.mark.asyncio
async def test_redirect_unknown_task_400(event_bus, event_log, task_manager):
    registry = SOPRegistry(event_log)
    app = _make_app(event_log, event_bus, task_manager, registry)
    client = TestClient(app)

    resp = client.post(
        "/api/tasks/does-not-exist/redirect",
        json={"node_id": "n1", "instruction": "redo"},
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_get_task_includes_node_graph(
    event_bus, event_log, task_manager, sample_sop
):
    """GET /tasks/{id} exposes the resolved node graph so the UI can render it
    (works for ad-hoc tasks too, whose SOP is not in the registry)."""
    registry = SOPRegistry(event_log)
    await registry.register(sample_sop)
    app = _make_app(event_log, event_bus, task_manager, registry)
    client = TestClient(app)

    created = client.post(
        "/api/tasks",
        json={"sop_name": "test-sop", "prompt": "go", "auto_start": False},
    ).json()
    resp = client.get(f"/api/tasks/{created['task_id']}")
    assert resp.status_code == 200
    nodes = resp.json()["nodes"]
    assert {n["id"] for n in nodes} == {"analyze", "review", "report"}
    # Each node summary carries the fields the UI needs.
    for n in nodes:
        assert "name" in n and "skill" in n and "depends_on" in n


# ── redirect_node auto-cascade (item 7, integration) ──────────────

class _CountingPi:
    """Pi stub that records how many times each node's skill runs."""

    def __init__(self):
        from symphony.core.pi_bridge import PiTurnResult
        self._PiTurnResult = PiTurnResult
        self._started = True
        self.runs = []

    async def run_skill_to_completion(self, skill_name, task_description="",
                                      *, on_event=None, timeout=None):
        self.runs.append(skill_name)
        if on_event:
            on_event({"type": "agent_end", "willRetry": False, "messages": []})
        return self._PiTurnResult(text=f"{skill_name}-out", command_id="c")

    async def abort(self):
        return "abort"


@pytest.mark.asyncio
async def test_redirect_completed_node_reruns_downstream(
    event_bus, event_log, sample_sop
):
    """Redirecting an already-completed node re-runs it plus its downstream
    closure (auto-cascade) with the extra instruction applied."""
    await event_log.create_task("task-1", "test-sop")
    pi = _CountingPi()
    executor = SOPExecutor(pi, event_log, event_bus)

    cancel = asyncio.Event()
    pause = asyncio.Event()
    pause.set()

    # First full run: analyze -> review -> report (report is a leaf).
    results = await executor.execute("task-1", sample_sop, cancel, pause)
    assert all(r["status"] == NodeStatus.COMPLETED for r in results.values())
    first_run_count = len(pi.runs)
    assert first_run_count == 3

    # Redirect "review" (already completed): review + report must re-run,
    # analyze must NOT.
    await executor.redirect_node(
        "task-1", sample_sop, "review", "focus on security",
        cancel, pause,
    )

    # review + report ran again -> 2 more skill invocations.
    assert len(pi.runs) == first_run_count + 2
    # analyze count unchanged (only ran once overall).
    assert pi.runs.count("test-analyze") == 1
    assert pi.runs.count("test-review") == 2
    assert pi.runs.count("test-report") == 2

    # The extra instruction was recorded for the redirected node.
    assert executor._extra_instructions[("task-1", "review")] == "focus on security"


@pytest.mark.asyncio
async def test_ask_follow_up_appends_and_runs_node(event_bus, event_log):
    """方案A multi-turn: a follow-up appends a node that depends on the prior
    turn and runs it."""
    from symphony.core.task_manager import TaskManager

    pi = _CountingPi()
    tm = TaskManager(event_bus, event_log, pi)

    task = await tm.create_and_start_qa("first question")
    # Let the first turn's background task complete.
    await asyncio.sleep(0.05)

    node_id = await tm.ask_follow_up(task.task_id, "second question")
    await asyncio.sleep(0.05)

    assert node_id == "turn-2"
    sop = tm._task_sops[task.task_id]
    assert len(sop.nodes) == 2
    assert sop.nodes[1].depends_on == ["turn-1"]
    await _drain_task(tm, task.task_id)

