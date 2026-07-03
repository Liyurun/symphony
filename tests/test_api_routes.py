"""Tests for the REST API routes."""

import json

import pytest
from fastapi.testclient import TestClient

from symphony.core.event_bus import EventBus
from symphony.core.event_log import EventLog
from symphony.core.task_manager import TaskManager
from symphony.sop.sop_registry import SOPRegistry
from symphony.web.server import WebServer


class _RoutePiBridgeStub:
    def __init__(self, config):
        self.config = config
        self.calls = []

    async def list_skills(self):
        return [{"name": "review", "description": "Review code", "source": "skill"}]

    async def get_state(self):
        return {"model": {"provider": "stub", "id": "m1"}, "thinkingLevel": "low"}

    async def get_available_models(self):
        return [{"provider": "stub", "id": "m1", "contextWindow": 1000}]

    async def set_model(self, provider, model_id):
        self.calls.append(("set_model", provider, model_id))
        return {"provider": provider, "id": model_id}

    async def cycle_model(self):
        self.calls.append(("cycle_model",))
        return {"model": {"provider": "stub", "id": "m2"}}

    async def set_thinking_level(self, level):
        self.calls.append(("set_thinking_level", level))
        return {"level": level}

    async def cycle_thinking_level(self):
        self.calls.append(("cycle_thinking_level",))
        return {"level": "medium"}

    async def compact(self, custom_instructions=None):
        self.calls.append(("compact", custom_instructions))
        return {"status": "compacted"}

    async def set_auto_compaction(self, enabled):
        self.calls.append(("set_auto_compaction", enabled))
        return {"enabled": enabled}

    async def new_session(self):
        self.calls.append(("new_session",))
        return {"cancelled": False}

    async def get_commands(self):
        return [{"name": "compact", "description": "Compact", "source": "prompt"}]

    async def bash(self, command, exclude_from_context=False):
        self.calls.append(("bash", command, exclude_from_context))
        return {"stdout": "ok"}

    async def get_session_stats(self):
        return {"messageCount": 2}

    async def export_html(self, output_path=None):
        self.calls.append(("export_html", output_path))
        return {"path": output_path or "/tmp/session.html"}

    async def get_last_assistant_text(self):
        return "last answer"


def _make_test_app(event_log, event_bus, task_manager, sop_registry, pi_bridge=None):
    server = WebServer(
        event_bus=event_bus,
        event_log=event_log,
        task_manager=task_manager,
        sop_registry=sop_registry,
        pi_bridge=pi_bridge,
    )
    return server.app


@pytest.mark.asyncio
async def test_list_tasks_empty(event_bus, event_log, task_manager):
    sop_registry = SOPRegistry(event_log, event_log.templates_dir)
    app = _make_test_app(event_log, event_bus, task_manager, sop_registry)
    client = TestClient(app)

    response = client.get("/api/tasks")
    assert response.status_code == 200
    assert response.json() == []


@pytest.mark.asyncio
async def test_create_task(event_bus, event_log, task_manager, sample_sop):
    sop_registry = SOPRegistry(event_log, event_log.templates_dir)
    await sop_registry.register(sample_sop)
    app = _make_test_app(event_log, event_bus, task_manager, sop_registry)
    client = TestClient(app)

    response = client.post(
        "/api/tasks",
        json={"sop_name": "test-sop", "sop_version": "1.0"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["sop_name"] == "test-sop"
    assert "task_id" in data


@pytest.mark.asyncio
async def test_get_task_not_found(event_bus, event_log, task_manager):
    sop_registry = SOPRegistry(event_log)
    app = _make_test_app(event_log, event_bus, task_manager, sop_registry)
    client = TestClient(app)

    response = client.get("/api/tasks/nonexistent")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_list_sops(event_bus, event_log, task_manager, sample_sop):
    sop_registry = SOPRegistry(event_log)
    await sop_registry.register(sample_sop)
    app = _make_test_app(event_log, event_bus, task_manager, sop_registry)
    client = TestClient(app)

    response = client.get("/api/sop")
    assert response.status_code == 200
    data = response.json()
    assert len(data) == 1
    assert data[0]["name"] == "test-sop"


@pytest.mark.asyncio
async def test_create_sop(event_bus, event_log, task_manager):
    sop_registry = SOPRegistry(event_log)
    app = _make_test_app(event_log, event_bus, task_manager, sop_registry)
    client = TestClient(app)

    response = client.post(
        "/api/sop",
        json={
            "name": "new-sop",
            "version": "1.0",
            "description": "Test",
            "input_requirements": "Input must include a concrete task description.",
            "output_requirements": "Output must include the completed result.",
            "nodes": [{"id": "step1", "name": "Step 1", "skill": "test"}],
        },
    )
    assert response.status_code == 200
    assert response.json()["status"] == "saved"


@pytest.mark.asyncio
async def test_create_sop_defaults_single_node(event_bus, event_log, task_manager):
    sop_registry = SOPRegistry(event_log)
    app = _make_test_app(event_log, event_bus, task_manager, sop_registry)
    client = TestClient(app)

    response = client.post(
        "/api/sop",
        json={
            "name": "simple-sop",
            "version": "1.0",
            "description": "Produce a technical plan",
            "input_requirements": "Input must include background, goals, and constraints.",
            "output_requirements": "Output must include design, risks, tests, and rollout.",
        },
    )
    assert response.status_code == 200

    get_response = client.get("/api/sop/simple-sop")
    assert get_response.status_code == 200
    data = get_response.json()
    assert len(data["nodes"]) == 1
    assert data["nodes"][0]["id"] == "step-1"
    assert data["nodes"][0]["input_requirements"] == data["input_requirements"]


@pytest.mark.asyncio
async def test_create_sop_rejects_invalid_node_dependency(event_bus, event_log, task_manager):
    sop_registry = SOPRegistry(event_log)
    app = _make_test_app(event_log, event_bus, task_manager, sop_registry)
    client = TestClient(app)

    response = client.post(
        "/api/sop",
        json={
            "name": "bad-sop",
            "description": "Invalid dependency",
            "input_requirements": "Input must include a task.",
            "output_requirements": "Output must include a result.",
            "nodes": [{"id": "step-1", "name": "Step 1", "depends_on": ["missing"]}],
        },
    )
    assert response.status_code == 400
    assert "depends on unknown node" in response.json()["detail"]


@pytest.mark.asyncio
async def test_create_sop_rejects_node_dependency_cycle(event_bus, event_log, task_manager):
    sop_registry = SOPRegistry(event_log)
    app = _make_test_app(event_log, event_bus, task_manager, sop_registry)
    client = TestClient(app)

    response = client.post(
        "/api/sop",
        json={
            "name": "cycle-sop",
            "description": "Cycle dependency",
            "input_requirements": "Input must include a task.",
            "output_requirements": "Output must include a result.",
            "nodes": [
                {"id": "a", "name": "A", "depends_on": ["b"]},
                {"id": "b", "name": "B", "depends_on": ["a"]},
            ],
        },
    )
    assert response.status_code == 400
    assert "dependency cycle" in response.json()["detail"]


@pytest.mark.asyncio
async def test_load_legacy_sop_definition_dict_backfills_contract(event_bus, event_log, task_manager):
    await event_log.save_sop_template(
        "legacy-sop",
        "1.0",
        {
            "name": "legacy-sop",
            "version": "1.0",
            "description": "Old SOP without four-part contract",
            "nodes": [{"id": "step1", "name": "Step 1", "skill": ""}],
        },
    )
    sop_registry = SOPRegistry(event_log, event_log.templates_dir)

    loaded = await sop_registry.load_all()

    assert len(loaded) == 1
    sop = loaded[0]
    assert sop.input_requirements
    assert sop.output_requirements
    assert sop.nodes[0].input_requirements == sop.input_requirements


@pytest.mark.asyncio
async def test_static_assets_disable_cache(event_bus, event_log, task_manager):
    sop_registry = SOPRegistry(event_log)
    app = _make_test_app(event_log, event_bus, task_manager, sop_registry)
    client = TestClient(app)

    response = client.get("/static/js/views/sop-editor.js")

    assert response.status_code == 200
    assert response.headers["cache-control"].startswith("no-store")


@pytest.mark.asyncio
async def test_get_config(event_bus, event_log, task_manager):
    sop_registry = SOPRegistry(event_log)
    app = _make_test_app(event_log, event_bus, task_manager, sop_registry)
    client = TestClient(app)

    response = client.get("/api/config")
    assert response.status_code == 200
    data = response.json()
    assert "pi_agent" in data
    assert "web_ui" in data
    assert "tui" in data


@pytest.mark.asyncio
async def test_logs_stats(event_bus, event_log, task_manager):
    sop_registry = SOPRegistry(event_log)
    app = _make_test_app(event_log, event_bus, task_manager, sop_registry)
    client = TestClient(app)

    response = client.get("/api/logs/stats")
    assert response.status_code == 200
    data = response.json()
    assert "total_tasks" in data
    assert "total_events" in data


@pytest.mark.asyncio
async def test_pi_control_routes_for_ts_tui(event_bus, event_log, task_manager):
    sop_registry = SOPRegistry(event_log)
    pi = _RoutePiBridgeStub(task_manager.pi_bridge.config)
    app = _make_test_app(event_log, event_bus, task_manager, sop_registry, pi_bridge=pi)
    client = TestClient(app)

    assert client.get("/api/pi/state").json()["thinkingLevel"] == "low"
    assert client.get("/api/pi/context").json()["context_files"] == []
    assert client.get("/api/pi/models").json()["models"][0]["id"] == "m1"
    assert client.get("/api/pi/commands").json()["commands"][0]["name"] == "compact"
    assert client.get("/api/skills").json()[0]["name"] == "review"

    assert client.post("/api/pi/model", json={"provider": "stub", "model_id": "m2"}).json()["id"] == "m2"
    assert client.post("/api/pi/model/cycle").json()["model"]["id"] == "m2"
    assert client.post("/api/pi/thinking", json={"level": "high"}).json()["level"] == "high"
    assert client.post("/api/pi/thinking/cycle").json()["level"] == "medium"
    assert client.post("/api/pi/compact", json={"instructions": "keep decisions"}).json()["status"] == "compacted"
    assert client.post("/api/pi/auto-compact", json={"enabled": False}).json()["enabled"] is False
    assert client.post("/api/pi/new-session").json()["cancelled"] is False
    assert client.post("/api/pi/bash", json={"command": "pwd"}).json()["stdout"] == "ok"
    assert client.get("/api/pi/session-stats").json()["messageCount"] == 2
    assert client.post("/api/pi/export-html", json={"output_path": "/tmp/a.html"}).json()["path"] == "/tmp/a.html"
    assert client.get("/api/pi/last-assistant-text").json()["text"] == "last answer"

    assert ("set_model", "stub", "m2") in pi.calls
    assert ("set_auto_compaction", False) in pi.calls
