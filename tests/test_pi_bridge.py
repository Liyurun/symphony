"""Tests for the PiBridge protocol handling."""

import asyncio
import json

import pytest

from symphony.core.pi_bridge import PiBridge, PiBridgeConfig, PiRpcError


def test_default_pi_cwd_points_to_bundled_agents_file():
    from symphony.cli import _default_pi_cwd

    cwd = _default_pi_cwd()
    assert cwd is not None
    assert cwd.endswith("pi-agent")
    from pathlib import Path
    assert (Path(cwd) / "AGENTS.md").is_file()


def test_context_file_infos_reports_agents_md(tmp_path):
    agents = tmp_path / "AGENTS.md"
    agents.write_text("# Rules\n", encoding="utf-8")

    cfg = PiBridgeConfig(pi_binary="echo", cwd=str(tmp_path))
    infos = cfg.context_file_infos()

    info = next(i for i in infos if i["path"] == str(agents))
    assert info["sha256_short"] == info["sha256"][:12]
    assert info["bytes"] == len("# Rules\n".encode("utf-8"))


@pytest.fixture
def bridge():
    """Create an unstarted bridge for protocol testing."""
    return PiBridge(PiBridgeConfig(pi_binary="echo"))


class _FakeStream:
    async def read(self, n=-1):
        return b""

    async def readline(self):
        return b""


class _FakeStdin:
    def __init__(self):
        self.closed = False

    def write(self, data):
        return None

    async def drain(self):
        return None

    def close(self):
        self.closed = True


class _FakeProcess:
    def __init__(self):
        self.stdin = _FakeStdin()
        self.stdout = _FakeStream()
        self.stderr = _FakeStream()

    async def wait(self):
        return 0

    def kill(self):
        return None


class TestPiBridgeProtocol:
    """Test the JSON-line protocol message dispatch."""

    @pytest.fixture(autouse=True)
    def _setup_loop(self):
        """Ensure an event loop exists for tests that need it."""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            asyncio.set_event_loop(asyncio.new_event_loop())

    def test_next_id_is_unique(self, bridge):
        id1 = bridge._next_id()
        id2 = bridge._next_id()
        assert id1 != id2
        assert "symphony-" in id1

    @pytest.mark.asyncio
    async def test_dispatch_response(self, bridge):
        """Test that response messages resolve pending futures."""
        future = asyncio.get_running_loop().create_future()
        bridge._pending["test-id"] = future

        msg = {
            "type": "response",
            "id": "test-id",
            "command": "prompt",
            "success": True,
            "data": {"result": "ok"},
        }
        bridge._dispatch_message(msg)

        assert future.done()
        result = future.result()
        assert result["success"] is True
        assert result["data"]["result"] == "ok"

    @pytest.mark.asyncio
    async def test_dispatch_response_error(self, bridge):
        future = asyncio.get_running_loop().create_future()
        bridge._pending["test-id"] = future

        msg = {
            "type": "response",
            "id": "test-id",
            "command": "prompt",
            "success": False,
            "error": "Something went wrong",
        }
        bridge._dispatch_message(msg)

        assert future.done()
        result = future.result()
        assert result["success"] is False
        assert result["error"] == "Something went wrong"

    def test_dispatch_agent_event(self, bridge):
        """Test that agent events are forwarded to callbacks."""
        received = []

        def callback(evt):
            received.append(evt)

        bridge.on_agent_event(callback)

        msg = {"type": "message_start", "text": "Hello"}
        bridge._dispatch_message(msg)

        assert len(received) == 1
        assert received[0]["type"] == "message_start"

    def test_dispatch_multiple_callbacks(self, bridge):
        results = {"a": 0, "b": 0}

        def cb_a(evt):
            results["a"] += 1

        def cb_b(evt):
            results["b"] += 1

        bridge.on_agent_event(cb_a)
        bridge.on_agent_event(cb_b)
        bridge._dispatch_message({"type": "test"})

        assert results["a"] == 1
        assert results["b"] == 1

    def test_dispatch_extension_ui_ignored(self, bridge):
        """Extension UI requests should not crash."""
        bridge._dispatch_message({
            "type": "extension_ui_request",
            "method": "notify",
            "message": "test",
        })
        # Should not raise

    def test_remove_event_callback(self, bridge):
        received = []

        def cb(evt):
            received.append(evt)

        bridge.on_agent_event(cb)
        bridge._dispatch_message({"type": "test1"})
        bridge.remove_event_callback(cb)
        bridge._dispatch_message({"type": "test2"})

        assert len(received) == 1

    @pytest.mark.asyncio
    async def test_start_uses_approve_by_default(self, monkeypatch):
        captured = {}

        async def fake_exec(*args, **kwargs):
            captured["args"] = args
            captured["kwargs"] = kwargs
            return _FakeProcess()

        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

        bridge = PiBridge(PiBridgeConfig(pi_binary="pi", cwd="/tmp/project"))
        await bridge.start()
        await bridge.stop()

        assert captured["args"][:3] == ("pi", "--mode", "rpc")
        assert "--approve" in captured["args"]
        assert captured["kwargs"]["cwd"] == "/tmp/project"

    @pytest.mark.asyncio
    async def test_start_can_disable_approve(self, monkeypatch):
        captured = {}

        async def fake_exec(*args, **kwargs):
            captured["args"] = args
            captured["kwargs"] = kwargs
            return _FakeProcess()

        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

        bridge = PiBridge(PiBridgeConfig(
            pi_binary="pi",
            cwd="/tmp/project",
            approve_project_files=False,
        ))
        await bridge.start()
        await bridge.stop()

        assert captured["args"][:3] == ("pi", "--mode", "rpc")
        assert "--approve" not in captured["args"]

    def test_pi_rpc_error(self):
        err = PiRpcError("prompt", "test error")
        assert err.command == "prompt"
        assert "test error" in str(err)

    @pytest.mark.asyncio
    async def test_extended_rpc_command_helpers(self, bridge):
        calls = []

        async def fake_send_command_and_wait(cmd_type, params):
            calls.append((cmd_type, params))
            if cmd_type == "get_available_models":
                return {"models": [{"id": "m1"}]}
            if cmd_type == "get_last_assistant_text":
                return {"text": "answer"}
            return {"ok": True}

        bridge._send_command_and_wait = fake_send_command_and_wait  # type: ignore

        assert await bridge.cycle_model() == {"ok": True}
        assert await bridge.get_available_models() == [{"id": "m1"}]
        assert await bridge.set_auto_compaction(False) == {"ok": True}
        assert await bridge.cycle_thinking_level() == {"ok": True}
        assert await bridge.get_session_stats() == {"ok": True}
        assert await bridge.export_html("/tmp/a.html") == {"ok": True}
        assert await bridge.get_last_assistant_text() == "answer"

        assert ("cycle_model", {}) in calls
        assert ("set_auto_compaction", {"enabled": False}) in calls
        assert ("cycle_thinking_level", {}) in calls
        assert ("get_session_stats", {}) in calls
        assert ("export_html", {"outputPath": "/tmp/a.html"}) in calls
        assert ("get_last_assistant_text", {}) in calls


class TestSkillPrompt:
    """The skill prompt must use pi's native /skill:<name> trigger so pi
    loads the skill's full definition (system prompt + tools + resources),
    not merely ASK the model to use it."""

    def test_uses_native_skill_command(self):
        p = PiBridge._build_skill_prompt("reviewer", "check auth.py")
        assert p.startswith("/skill:reviewer")
        assert "check auth.py" in p

    def test_no_args_still_triggers_skill(self):
        p = PiBridge._build_skill_prompt("reviewer")
        assert p == "/skill:reviewer"

    def test_not_a_natural_language_request(self):
        # Regression: the old impl said "Please use the 'X' skill" which pi
        # would NOT deterministically expand into the skill definition.
        p = PiBridge._build_skill_prompt("reviewer", "do it")
        assert "Please use" not in p


class TestTurnAccumulator:
    """Test folding pi's streamed AgentEvents into a PiTurnResult."""

    def _acc(self):
        from symphony.core.pi_bridge import _TurnAccumulator
        return _TurnAccumulator()

    def test_streamed_text_replaces_not_appends(self):
        acc = self._acc()
        acc.ingest({"type": "message_update",
                    "message": {"role": "assistant", "content": "Hello"}})
        acc.ingest({"type": "message_update",
                    "message": {"role": "assistant", "content": "Hello world"}})
        assert acc.result().text == "Hello world"

    def test_text_from_content_blocks(self):
        acc = self._acc()
        acc.ingest({"type": "message_end", "message": {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "part one "},
                {"type": "text", "text": "part two"},
            ],
        }})
        assert acc.result().text == "part one part two"

    def test_tool_call_lifecycle(self):
        acc = self._acc()
        acc.ingest({"type": "tool_execution_start", "toolCallId": "t1",
                    "toolName": "bash", "args": {"command": "ls"}})
        acc.ingest({"type": "tool_execution_end", "toolCallId": "t1",
                    "toolName": "bash", "result": "file.txt", "isError": False})
        calls = acc.result().tool_calls
        assert len(calls) == 1
        assert calls[0]["name"] == "bash"
        assert calls[0]["args"] == {"command": "ls"}
        assert calls[0]["result"] == "file.txt"
        assert calls[0]["is_error"] is False

    def test_agent_end_recovers_text_when_no_stream(self):
        acc = self._acc()
        acc.ingest({"type": "agent_end", "willRetry": False, "messages": [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "final answer"},
        ]})
        assert acc.result().text == "final answer"

    def test_to_node_result_shape(self):
        acc = self._acc()
        acc.command_id = "cmd-1"
        acc.ingest({"type": "message_end",
                    "message": {"role": "assistant", "content": "done"}})
        res = acc.result().to_node_result(skill="reviewer")
        assert res["status"] == "completed"
        assert res["output"] == "done"
        assert res["skill"] == "reviewer"
        assert res["provider"] == "pi"
        assert res["command_id"] == "cmd-1"


class TestRunPromptToCompletion:
    """Test that streaming prompts converge into an awaitable result."""

    @pytest.mark.asyncio
    async def test_resolves_on_agent_end(self, bridge):
        # Stub send_prompt so no real subprocess is needed.
        async def fake_send_prompt(message, images=None, streaming_behavior=None):
            return "cmd-42"
        bridge.send_prompt = fake_send_prompt  # type: ignore

        forwarded = []

        async def driver():
            # Wait a tick so run_prompt_to_completion registers its callback.
            await asyncio.sleep(0.01)
            bridge._dispatch_message({"type": "agent_start"})
            bridge._dispatch_message({"type": "message_update",
                "message": {"role": "assistant", "content": "Analyzing"}})
            bridge._dispatch_message({"type": "tool_execution_start",
                "toolCallId": "t1", "toolName": "bash", "args": {"command": "ls"}})
            bridge._dispatch_message({"type": "tool_execution_end",
                "toolCallId": "t1", "toolName": "bash", "result": "ok", "isError": False})
            bridge._dispatch_message({"type": "message_end",
                "message": {"role": "assistant", "content": "Analyzing done"}})
            bridge._dispatch_message({"type": "agent_end",
                "willRetry": False, "messages": []})

        _, result = await asyncio.gather(
            driver(),
            bridge.run_prompt_to_completion(
                "do it", on_event=lambda e: forwarded.append(e), timeout=2.0
            ),
        )
        assert result.text == "Analyzing done"
        assert result.command_id == "cmd-42"
        assert len(result.tool_calls) == 1
        assert result.tool_calls[0]["result"] == "ok"
        # on_event saw every raw pi event.
        assert any(e["type"] == "agent_start" for e in forwarded)
        assert any(e["type"] == "agent_end" for e in forwarded)

    @pytest.mark.asyncio
    async def test_ignores_retrying_agent_end(self, bridge):
        async def fake_send_prompt(message, images=None, streaming_behavior=None):
            return "cmd-1"
        bridge.send_prompt = fake_send_prompt  # type: ignore

        async def driver():
            await asyncio.sleep(0.01)
            # A retrying turn end must NOT resolve.
            bridge._dispatch_message({"type": "agent_end", "willRetry": True, "messages": []})
            await asyncio.sleep(0.02)
            bridge._dispatch_message({"type": "message_end",
                "message": {"role": "assistant", "content": "second try ok"}})
            bridge._dispatch_message({"type": "agent_end", "willRetry": False, "messages": []})

        _, result = await asyncio.gather(
            driver(),
            bridge.run_prompt_to_completion("do it", timeout=2.0),
        )
        assert result.text == "second try ok"

    @pytest.mark.asyncio
    async def test_callback_removed_after_completion(self, bridge):
        async def fake_send_prompt(message, images=None, streaming_behavior=None):
            return "cmd-1"
        bridge.send_prompt = fake_send_prompt  # type: ignore

        before = len(bridge._event_callbacks)

        async def driver():
            await asyncio.sleep(0.01)
            bridge._dispatch_message({"type": "agent_end", "willRetry": False, "messages": []})

        await asyncio.gather(driver(), bridge.run_prompt_to_completion("x", timeout=2.0))
        # No leaked callbacks.
        assert len(bridge._event_callbacks) == before

    @pytest.mark.asyncio
    async def test_timeout_when_no_agent_end(self, bridge):
        async def fake_send_prompt(message, images=None, streaming_behavior=None):
            return "cmd-1"
        bridge.send_prompt = fake_send_prompt  # type: ignore

        with pytest.raises(asyncio.TimeoutError):
            await bridge.run_prompt_to_completion("x", timeout=0.05)
        # Callback still cleaned up even on timeout.
        assert bridge._event_callbacks == []
