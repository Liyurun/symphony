"""统一 session API 测试。"""

import json
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from symphony.ai.schema import LLMResponse, Message, Role, Usage
from symphony.config import LLMConfig, ServerConfig, StorageConfig, SymphonyConfig
from symphony.server.app import create_app


def _config(tmp_path):
    """构造临时配置。"""
    return SymphonyConfig(
        llm=LLMConfig(
            provider="doubao",
            api_key="test",
            model="fake",
            base_url="https://example.com/api/v3",
        ),
        server=ServerConfig(),
        storage=StorageConfig(
            workspace_dir=str(tmp_path / "workspaces"),
            sessions_dir=str(tmp_path / "sessions"),
            templates_dir=str(tmp_path / "templates"),
            custom_skills_dir=str(tmp_path / "skills"),
        ),
    )


def _sop(sop_id="demo-sop"):
    """构造单节点 SOP。"""
    return {
        "id": sop_id,
        "name": "Demo SOP",
        "version": "1.0.0",
        "description": "Demo",
        "variables": {"type": "object", "properties": {}},
        "nodes": [
            {
                "id": "step1",
                "name": "Step 1",
                "type": "agent",
                "prompt": "Return JSON",
                "skills": [],
                "input_schema": {"type": "object"},
                "output_schema": {"type": "object"},
            }
        ],
        "edges": [],
        "entry_node": "step1",
    }


def _response(content="{}"):
    """构造 LLM 响应。"""
    return LLMResponse(
        id="r1",
        choices=[Message(role=Role.ASSISTANT, content=content)],
        usage=Usage(),
        model="fake",
    )


@pytest.fixture
def client(tmp_path):
    """构造 TestClient 并替换 provider。"""
    app = create_app(_config(tmp_path))
    provider = AsyncMock()
    provider.model = "fake"
    provider.api_key = "test"
    provider.chat.return_value = _response(json.dumps({"ok": True}))
    app.state.llm_provider = provider
    app.state.task_manager.llm_provider = provider
    with TestClient(app) as test_client:
        yield test_client


def test_session_list_empty(client):
    """初始 session 列表为空。"""
    resp = client.get("/api/sessions")

    assert resp.status_code == 200
    assert resp.json() == []


def test_chat_session_meta_and_logs(client):
    """Chat session 应能读取 meta 与四类日志。"""
    meta = client.app.state.session_manager.create_chat(title="Ask", source="test")
    log = client.app.state.session_manager.require(meta.session_id)
    log.append_transcript({"role": "user", "content": "hi"})
    log.append_event({"type": "chat_started"})
    log.append_trace({"model": "fake"})
    log.append_interaction({"type": "interaction_requested", "interaction_id": "int-1"})

    meta_resp = client.get(f"/api/sessions/{meta.session_id}")
    transcript_resp = client.get(f"/api/sessions/{meta.session_id}/transcript")
    events_resp = client.get(f"/api/sessions/{meta.session_id}/events")
    traces_resp = client.get(f"/api/sessions/{meta.session_id}/traces")
    interactions_resp = client.get(f"/api/sessions/{meta.session_id}/interactions")

    assert meta_resp.status_code == 200
    assert meta_resp.json()["session_id"] == meta.session_id
    assert transcript_resp.json() == [{"role": "user", "content": "hi"}]
    assert events_resp.json() == [{"type": "chat_started"}]
    assert traces_resp.json() == [{"model": "fake"}]
    assert interactions_resp.json()[0]["interaction_id"] == "int-1"


def test_answer_interaction_persists_answer(client):
    """POST answer 应把用户回答追加到 interactions。"""
    meta = client.app.state.session_manager.create_chat(title="Ask", source="test")

    resp = client.post(
        f"/api/sessions/{meta.session_id}/interactions/int-1/answer",
        json={"answer": {"value": "yes"}},
    )
    interactions = client.get(f"/api/sessions/{meta.session_id}/interactions").json()

    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    assert interactions[-1]["type"] == "interaction_answered"
    assert interactions[-1]["interaction_id"] == "int-1"
    assert interactions[-1]["session_id"] == meta.session_id
    assert interactions[-1]["answer"] == {"value": "yes"}


def test_answer_interaction_rejects_non_dict_answer(client):
    """POST answer 应拒绝非 dict 回答。"""
    meta = client.app.state.session_manager.create_chat(title="Ask", source="test")

    resp = client.post(
        f"/api/sessions/{meta.session_id}/interactions/int-1/answer",
        json={"answer": "bad"},
    )

    assert resp.status_code == 422


def test_create_sop_session_starts_task_and_lists_session(client):
    """POST /api/sop-sessions 应创建 session 并启动任务。"""
    assert client.post("/api/sops", json=_sop()).status_code == 200

    resp = client.post(
        "/api/sop-sessions",
        json={"sop_id": "demo-sop", "variables": {}, "title": "Run demo"},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["session_id"].startswith("sop-")
    assert body["task_id"]
    sessions = client.get("/api/sessions").json()
    assert sessions[0]["session_id"] == body["session_id"]
    assert sessions[0]["task_id"] == body["task_id"]
    tasks = client.get("/api/tasks").json()
    assert body["task_id"] in [task["task_id"] for task in tasks]


def test_sop_session_events_and_traces_resolve_task_workspace(client):
    """SOP session 的 events/traces 应读取关联 task workspace。"""
    client.post("/api/sops", json=_sop())
    body = client.post(
        "/api/sop-sessions",
        json={"sop_id": "demo-sop", "variables": {}, "title": "Run demo"},
    ).json()
    workspace = client.app.state.workspace_manager.get(body["task_id"])
    workspace.event_log.append({"type": "task_workspace_event", "task_id": body["task_id"]})
    workspace.trace_log.append({"type": "task_workspace_trace", "task_id": body["task_id"]})

    events_resp = client.get(f"/api/sessions/{body['session_id']}/events")
    traces_resp = client.get(f"/api/sessions/{body['session_id']}/traces")

    assert events_resp.status_code == 200
    assert traces_resp.status_code == 200
    assert "task_workspace_event" in [item.get("type") for item in events_resp.json()]
    assert "task_workspace_trace" in [item.get("type") for item in traces_resp.json()]


def test_create_sop_session_unknown_sop_404(client):
    """未知 SOP 创建 session 应返回 404。"""
    resp = client.post(
        "/api/sop-sessions",
        json={"sop_id": "missing", "variables": {}, "title": "Missing"},
    )

    assert resp.status_code == 404


def test_session_404(client):
    """未知 session 的 meta 与日志端点均返回 404。"""
    paths = [
        "/api/sessions/missing",
        "/api/sessions/missing/transcript",
        "/api/sessions/missing/events",
        "/api/sessions/missing/traces",
        "/api/sessions/missing/interactions",
    ]

    for path in paths:
        resp = client.get(path)
        assert resp.status_code == 404
