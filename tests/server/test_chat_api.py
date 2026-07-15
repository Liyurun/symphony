from fastapi import FastAPI
from fastapi.testclient import TestClient

from symphony.agent.chat_events import ChatAnswerDelta, ChatCompleted
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
from symphony.skills.registry import SkillRegistry


class FakeProvider:
    """流式假 provider：按脚本吐增量。"""

    def __init__(self, fail: bool = False, api_key: str = "sk-test", model: str = "fake",
                 parts: list[str] | None = None) -> None:
        self.fail = fail
        self.api_key = api_key
        self.model = model
        self.parts = parts if parts is not None else ["你好，", "我是 Symphony。"]
        self.messages = []

    async def chat_stream(self, messages, tools=None, **kwargs):
        if self.fail:
            raise RuntimeError("provider down")
        self.messages = list(messages)
        for part in self.parts:
            yield StreamDelta(content=part)


def _client(provider: FakeProvider) -> TestClient:
    app = FastAPI()
    app.state.llm_provider = provider
    app.state.skill_registry = SkillRegistry()
    app.include_router(chat_api.router)
    return TestClient(app)


def _config(tmp_path) -> SymphonyConfig:
    """构造带非默认 runtime.chat/context_compression 的配置。"""
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
            chat=ChatRuntimeConfig(max_iterations=3, skill_reference_limit=4),
            context_compression=ContextCompressionConfig(
                enabled=False,
                max_prompt_chars=1234,
                keep_recent_messages=5,
                min_recent_messages=2,
                summary_max_chars=321,
                max_message_chars=654,
            ),
        ),
    )


def test_chat_returns_joined_answer():
    provider = FakeProvider()
    client = _client(provider)
    resp = client.post("/api/chat", json={"question": "你是谁", "history": []})
    assert resp.status_code == 200
    assert resp.json() == {"answer": "你好，我是 Symphony。"}
    assert provider.messages[-1].content == "你是谁"
    assert "Pi Agent" in (provider.messages[0].content or "")


def test_chat_includes_history():
    provider = FakeProvider()
    client = _client(provider)
    resp = client.post(
        "/api/chat",
        json={
            "question": "继续",
            "history": [
                {"role": "user", "content": "上一问"},
                {"role": "assistant", "content": "上一答"},
            ],
        },
    )
    assert resp.status_code == 200
    assert [m.content for m in provider.messages][-3:] == ["上一问", "上一答", "继续"]


def test_chat_provider_failure_returns_500():
    client = _client(FakeProvider(fail=True))
    resp = client.post("/api/chat", json={"question": "hi", "history": []})
    assert resp.status_code == 500
    assert "问答失败" in resp.json()["detail"]


def test_chat_missing_api_key_returns_actionable_error():
    client = _client(FakeProvider(api_key=""))
    resp = client.post("/api/chat", json={"question": "hi", "history": []})
    assert resp.status_code == 400
    assert "ARK_API_KEY" in resp.json()["detail"]


def test_rest_chat_passes_runtime_config_to_chat_runtime(tmp_path, monkeypatch):
    """REST Chat 应使用 config.runtime.chat 与 context_compression。"""
    captured = {}

    class CapturingChatRuntime:
        """截获 ChatRuntime 构造参数并返回固定回答。"""

        def __init__(self, provider, registry, **kwargs):
            captured["provider"] = provider
            captured["registry"] = registry
            captured["kwargs"] = kwargs

        async def stream(self, question, history):
            captured["stream"] = (question, history)
            yield ChatAnswerDelta(text="ok")
            yield ChatCompleted(answer="ok")

    monkeypatch.setattr(chat_api, "ChatRuntime", CapturingChatRuntime)
    app = FastAPI()
    app.state.llm_provider = FakeProvider()
    app.state.skill_registry = SkillRegistry()
    app.state.config = _config(tmp_path)
    app.state.skill_reference_index = object()
    app.include_router(chat_api.router)
    client = TestClient(app)

    resp = client.post("/api/chat", json={"question": "hi", "history": []})

    assert resp.status_code == 200
    assert resp.json() == {"answer": "ok"}
    assert captured["stream"] == ("hi", [])
    kwargs = captured["kwargs"]
    assert kwargs["max_iterations"] == 3
    assert kwargs["skill_reference_limit"] == 4
    assert kwargs["skill_reference_index"] is app.state.skill_reference_index
    compressor = kwargs["context_compressor"]
    assert compressor.enabled is False
    assert compressor.max_prompt_chars == 1234
    assert compressor.keep_recent_messages == 5
    assert compressor.min_recent_messages == 2
    assert compressor.summary_max_chars == 321
    assert compressor.max_message_chars == 654
