"""/ws/chat 流式对话端点的单元测试。"""

from fastapi.testclient import TestClient

from symphony.agent.chat_events import ChatCompleted
from symphony.ai.schema import StreamDelta
from symphony.config import (
    ChatRuntimeConfig,
    ContextCompressionConfig,
    LLMConfig,
    RuntimeConfig,
    ServerConfig,
    StorageConfig,
    SymphonyConfig,
)
from symphony.server.api import chat as chat_api
from symphony.server import app as server_app
from symphony.storage import SessionManager


class StreamingFakeProvider:
    api_key = "sk-test"
    model = "fake"

    async def chat_stream(self, messages, tools=None, **kwargs):
        yield StreamDelta(content="Hello")
        yield StreamDelta(content=" world")


def _app_with_provider(provider):
    # 复用真实 create_app 需要完整 config，这里直接装配最小 app。
    from fastapi import FastAPI, WebSocket, WebSocketDisconnect
    from symphony.server.app import _register_chat_ws  # 见 Step 3

    app = FastAPI()
    app.state.llm_provider = provider
    from symphony.skills.registry import SkillRegistry

    app.state.skill_registry = SkillRegistry()
    app.include_router(chat_api.router)
    _register_chat_ws(app)
    return app


def _runtime_config(tmp_path):
    """构造带非默认 chat/context_compression 的配置。"""
    return SymphonyConfig(
        llm=LLMConfig(
            provider="doubao",
            api_key="sk-test",
            model="doubao-test",
            base_url="http://x",
        ),
        server=ServerConfig(),
        storage=StorageConfig(
            workspace_dir=str(tmp_path / "ws"),
            templates_dir=str(tmp_path / "tpl"),
            custom_skills_dir=str(tmp_path / "sk"),
        ),
        runtime=RuntimeConfig(
            chat=ChatRuntimeConfig(max_iterations=5, skill_reference_limit=6),
            context_compression=ContextCompressionConfig(
                enabled=False,
                max_prompt_chars=2222,
                keep_recent_messages=7,
                min_recent_messages=3,
                summary_max_chars=444,
                max_message_chars=555,
            ),
        ),
    )


def test_chat_ws_streams_answer_deltas_then_completed():
    app = _app_with_provider(StreamingFakeProvider())
    client = TestClient(app)
    with client.websocket_connect("/ws/chat") as ws:
        ws.send_json({"question": "hi", "history": []})
        events = []
        while True:
            ev = ws.receive_json()
            events.append(ev)
            if ev["type"] in {"chat_completed", "chat_failed"}:
                break
    types = [e["type"] for e in events]
    assert types == ["chat_answer_delta", "chat_answer_delta", "chat_completed"]
    assert events[-1]["answer"] == "Hello world"


def test_create_chat_session_endpoint_creates_meta(tmp_path):
    """POST /api/chat/sessions 应创建 Chat session。"""
    app = _app_with_provider(StreamingFakeProvider())
    app.state.session_manager = SessionManager(tmp_path / "sessions")
    client = TestClient(app)

    resp = client.post(
        "/api/chat/sessions",
        json={"title": "WS", "source": "test"},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["session_id"].startswith("chat-")
    assert body["type"] == "chat"
    assert body["title"] == "WS"
    meta = app.state.session_manager.require(body["session_id"]).load_meta()
    assert meta["source"] == "test"


def test_chat_ws_with_session_id_persists_logs(tmp_path):
    """带 session_id 的 /ws/chat 应持久化 transcript/events。"""
    app = _app_with_provider(StreamingFakeProvider())
    app.state.session_manager = SessionManager(tmp_path / "sessions")
    meta = app.state.session_manager.create_chat(title="WS", source="test")
    client = TestClient(app)

    with client.websocket_connect(f"/ws/chat?session_id={meta.session_id}") as ws:
        ws.send_json({"question": "hi", "history": []})
        while True:
            ev = ws.receive_json()
            if ev["type"] in {"chat_completed", "chat_failed"}:
                break

    log = app.state.session_manager.require(meta.session_id)
    assert log.read_transcript()[-1]["content"] == "Hello world"
    assert "chat_completed" in [event["type"] for event in log.read_events()]


def test_chat_ws_passes_runtime_config_to_chat_runtime(tmp_path, monkeypatch):
    """无 session_id 的 /ws/chat 应使用配置化 ChatRuntime 参数。"""
    captured = {}

    class CapturingChatRuntime:
        """截获 ChatRuntime 构造参数。"""

        def __init__(self, provider, registry, **kwargs):
            captured["kwargs"] = kwargs

        async def stream(self, question, history):
            captured["stream"] = (question, history)
            yield ChatCompleted(answer="ok")

    monkeypatch.setattr(server_app, "ChatRuntime", CapturingChatRuntime)
    app = _app_with_provider(StreamingFakeProvider())
    app.state.config = _runtime_config(tmp_path)
    app.state.skill_reference_index = object()
    client = TestClient(app)

    with client.websocket_connect("/ws/chat") as ws:
        ws.send_json({"question": "hi", "history": []})
        event = ws.receive_json()

    assert event == {"type": "chat_completed", "answer": "ok"}
    assert captured["stream"] == ("hi", [])
    kwargs = captured["kwargs"]
    assert kwargs["max_iterations"] == 5
    assert kwargs["skill_reference_limit"] == 6
    assert kwargs["skill_reference_index"] is app.state.skill_reference_index
    compressor = kwargs["context_compressor"]
    assert compressor.enabled is False
    assert compressor.max_prompt_chars == 2222
    assert compressor.keep_recent_messages == 7
    assert compressor.min_recent_messages == 3
    assert compressor.summary_max_chars == 444
    assert compressor.max_message_chars == 555


def test_chat_ws_session_passes_runtime_config_to_session_runner(tmp_path, monkeypatch):
    """带 session_id 的 /ws/chat 应把配置化运行参数传给 ChatSessionRunner。"""
    captured = {}

    class CapturingChatSessionRunner:
        """截获 ChatSessionRunner 构造参数。"""

        def __init__(self, provider, registry, session_manager, **runtime_kwargs):
            captured["runtime_kwargs"] = runtime_kwargs
            captured["session_manager"] = session_manager

        async def stream(self, session_id, question, history):
            captured["stream"] = (session_id, question, history)
            yield ChatCompleted(answer="ok")

    monkeypatch.setattr(server_app, "ChatSessionRunner", CapturingChatSessionRunner)
    app = _app_with_provider(StreamingFakeProvider())
    app.state.config = _runtime_config(tmp_path)
    app.state.session_manager = SessionManager(tmp_path / "sessions")
    meta = app.state.session_manager.create_chat(title="WS", source="test")
    app.state.skill_reference_index = object()
    client = TestClient(app)

    with client.websocket_connect(f"/ws/chat?session_id={meta.session_id}") as ws:
        ws.send_json({"question": "hi", "history": []})
        event = ws.receive_json()

    assert event == {"type": "chat_completed", "answer": "ok"}
    assert captured["stream"] == (meta.session_id, "hi", [])
    assert captured["session_manager"] is app.state.session_manager
    kwargs = captured["runtime_kwargs"]
    assert kwargs["max_iterations"] == 5
    assert kwargs["skill_reference_limit"] == 6
    assert kwargs["skill_reference_index"] is app.state.skill_reference_index
    compressor = kwargs["context_compressor"]
    assert compressor.enabled is False
    assert compressor.max_prompt_chars == 2222
