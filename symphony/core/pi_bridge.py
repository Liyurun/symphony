"""Pi agent bridge — communicates with pi coding-agent via JSON-line protocol.

This module manages a subprocess running pi in --mode rpc,
sending commands and receiving events over stdin/stdout using pi's
native JSON-line protocol (one JSON object per line).

Reference: pi's packages/coding-agent/src/modes/rpc/rpc-mode.ts
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)


@dataclass
class PiBridgeConfig:
    """Configuration for the pi agent bridge."""

    pi_binary: str = "pi"
    """Path to the pi binary."""

    cwd: str | None = None
    """Working directory for the pi subprocess."""

    model: str | None = None
    """Model to use (passed as --model flag)."""

    startup_timeout: float = 30.0
    """Max seconds to wait for pi to start."""

    request_timeout: float = 120.0
    """Default timeout for RPC requests."""

    approve_project_files: bool = True
    """Trust project-local .pi/resources for this pi run by default."""

    def context_file_infos(self) -> list[dict[str, Any]]:
        """Return context files that pi should discover from this cwd.

        Pi itself owns the actual prompt construction, but its documented
        context-file discovery walks upward from cwd and loads AGENTS.md /
        CLAUDE.md. This helper gives Symphony an auditable view of the same
        files so the Web UI can show evidence that the intended AGENTS.md is in
        scope for a turn.
        """
        if not self.cwd:
            return []
        current = Path(self.cwd).expanduser().resolve()
        candidates = ("AGENTS.md", "AGENTS.MD", "CLAUDE.md", "CLAUDE.MD")
        infos: list[dict[str, Any]] = []
        for directory in [current, *current.parents]:
            for name in candidates:
                path = directory / name
                if not path.is_file():
                    continue
                try:
                    content = path.read_text(encoding="utf-8")
                    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
                    infos.append({
                        "path": str(path),
                        "name": name,
                        "sha256": digest,
                        "sha256_short": digest[:12],
                        "bytes": len(content.encode("utf-8")),
                    })
                except OSError as e:
                    infos.append({
                        "path": str(path),
                        "name": name,
                        "error": str(e),
                    })
        return infos


@dataclass
class PiTurnResult:
    """The converged result of a single pi turn.

    Produced by :meth:`PiBridge.run_prompt_to_completion` once pi finishes.
    """

    text: str = ""
    """The full accumulated assistant text for the turn."""

    tool_calls: list[dict] = field(default_factory=list)
    """Tool calls pi made, each: {name, args, result, is_error}."""

    messages: list[dict] = field(default_factory=list)
    """The final AgentMessage list from pi's agent_end event (if any)."""

    command_id: str | None = None
    """The command id of the originating prompt, for event correlation."""

    error_message: str | None = None
    """Provider/RPC error message surfaced by pi, if any."""

    def to_node_result(self, skill: str | None = None) -> dict:
        """Shape this into the dict a SOP node stores as its result."""
        out = {
            "status": "completed",
            "output": self.text,
            "tool_calls": self.tool_calls,
            "provider": "pi",
        }
        if skill:
            out["skill"] = skill
        if self.command_id:
            out["command_id"] = self.command_id
        if self.error_message:
            out["status"] = "failed"
            out["error"] = self.error_message
        return out


class _TurnAccumulator:
    """Folds pi's streamed AgentEvents into a PiTurnResult.

    Handles pi's event vocabulary:
      - message_update: streaming assistant text (we extract the latest text)
      - message_end:    assistant message finalized
      - tool_execution_start / tool_execution_end: tool-call lifecycle
      - agent_end:      carries the final message list
    """

    def __init__(self) -> None:
        self.text = ""
        self.tool_calls: list[dict] = []
        self._tool_by_id: dict[str, dict] = {}
        self.messages: list[dict] = []
        self.command_id: str | None = None
        self.error_message: str | None = None

    @staticmethod
    def _extract_text(message: Any) -> str:
        """Pull plain text out of an AgentMessage's content."""
        if not isinstance(message, dict):
            return ""
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = []
            for block in content:
                if isinstance(block, dict):
                    if block.get("type") == "text" and isinstance(block.get("text"), str):
                        parts.append(block["text"])
                    elif isinstance(block.get("text"), str):
                        parts.append(block["text"])
                elif isinstance(block, str):
                    parts.append(block)
            return "".join(parts)
        return ""

    def ingest(self, evt: dict) -> None:
        etype = evt.get("type")

        if etype in ("message_update", "message_end"):
            message = evt.get("message")
            # Only assistant text is interesting for node output.
            if isinstance(message, dict) and message.get("role") in (None, "assistant"):
                if message.get("stopReason") == "error" and isinstance(message.get("errorMessage"), str):
                    self.error_message = message["errorMessage"]
                text = self._extract_text(message)
                if not text and isinstance(message.get("errorMessage"), str):
                    text = message["errorMessage"]
                if text:
                    # Streamed messages carry the full text-so-far, so replace
                    # rather than append to avoid duplication.
                    self.text = text

        elif etype == "tool_execution_start":
            tid = evt.get("toolCallId") or f"tool-{len(self.tool_calls)}"
            call = {
                "name": evt.get("toolName", ""),
                "args": evt.get("args", {}),
                "result": None,
                "is_error": False,
            }
            self._tool_by_id[tid] = call
            self.tool_calls.append(call)

        elif etype == "tool_execution_end":
            tid = evt.get("toolCallId")
            call = self._tool_by_id.get(tid)
            if call is None:
                call = {"name": evt.get("toolName", ""), "args": {},
                        "result": None, "is_error": False}
                self.tool_calls.append(call)
            call["result"] = evt.get("result")
            call["is_error"] = bool(evt.get("isError"))

        elif etype == "agent_end":
            msgs = evt.get("messages")
            if isinstance(msgs, list):
                self.messages = msgs
                # If we never captured streamed text, recover it from the last
                # assistant message.
                if not self.text:
                    for m in reversed(msgs):
                        if isinstance(m, dict) and m.get("role") == "assistant":
                            if m.get("stopReason") == "error" and isinstance(m.get("errorMessage"), str):
                                self.error_message = m["errorMessage"]
                            self.text = self._extract_text(m)
                            if not self.text and isinstance(m.get("errorMessage"), str):
                                self.text = m["errorMessage"]
                            if self.text:
                                break

    def result(self) -> "PiTurnResult":
        return PiTurnResult(
            text=self.text,
            tool_calls=self.tool_calls,
            messages=self.messages,
            command_id=self.command_id,
            error_message=self.error_message,
        )


class PiBridge:
    """Bridge to pi agent using its native JSON-line RPC protocol.

    Protocol (matches pi's rpc-mode.ts):
    - Commands: {"type": "<command>", ...params, "id": "<uuid>"}  — one JSON line on stdin
    - Responses: {"type": "response", "command": "<cmd>", "success": true, "id": "<uuid>"}
    - Events: AgentSessionEvent objects streamed as JSON lines (no "type":"response")
    - Extension UI: {"type": "extension_ui_request", ...}

    Usage:
        bridge = PiBridge(PiBridgeConfig(pi_binary="pi"))
        await bridge.start()

        # Send a prompt
        cmd_id = await bridge.send_prompt("Review this code...")

        # Listen for agent events
        bridge.on_agent_event(lambda evt: print(evt))

        # Get available skills
        skills = await bridge.list_skills()

        await bridge.stop()
    """

    def __init__(self, config: PiBridgeConfig | None = None):
        self.config = config or PiBridgeConfig()
        self._process: asyncio.subprocess.Process | None = None
        self._cmd_counter = 0
        self._pending: dict[str, asyncio.Future[dict]] = {}
        self._reader_task: asyncio.Task | None = None
        self._started = False
        self._event_callbacks: list[Callable[[dict], None]] = []
        self._stdin_lock = asyncio.Lock()

    # ── Lifecycle ──────────────────────────────────────────────

    @staticmethod
    def _inject_node_path(env: dict) -> None:
        """Add common Node.js install paths without overriding the user's Node.

        ``uv tool`` can run with a reduced PATH, so we still add common Node
        locations as fallbacks. They must be appended, not prepended: otherwise
        an old local Node (for example ``~/.local/node-v20...``) can shadow a
        newer Homebrew/system Node and make pi crash before RPC starts.
        """
        import shutil
        extra_paths: list[str] = []
        current_path = env.get("PATH", "")
        current_parts = current_path.split(os.pathsep) if current_path else []

        # Prefer the node already visible to the current process.
        for cmd in ["node", "npm", "npx"]:
            found = shutil.which(cmd)
            if found:
                d = os.path.dirname(found)
                if d not in current_parts and d not in extra_paths:
                    extra_paths.append(d)

        # npm global binaries (for example bytedcli installed via
        # `npm install -g`) may live outside the shell PATH that launched
        # Symphony. Add `npm prefix -g`/bin so pi's bash tool can find them.
        npm = shutil.which("npm")
        if npm:
            try:
                import subprocess
                proc = subprocess.run(
                    [npm, "prefix", "-g"],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    text=True,
                    timeout=5,
                    env=env,
                    check=False,
                )
                prefix = proc.stdout.strip()
                if prefix:
                    npm_bin = os.path.join(prefix, "bin")
                    if os.path.isdir(npm_bin) and npm_bin not in current_parts and npm_bin not in extra_paths:
                        extra_paths.append(npm_bin)
            except Exception:
                pass

        # Fallback locations only. Keep local versioned Node directories last so
        # they never shadow a newer PATH/Homebrew Node.
        for d in [
            os.path.expanduser("~/.local/bin"),
            "/opt/homebrew/bin",
            "/usr/local/bin",
            os.path.expanduser("~/.local/node-*/bin"),
        ]:
            import glob
            for resolved in sorted(glob.glob(d), reverse=True):
                if os.path.isdir(resolved) and resolved not in current_parts and resolved not in extra_paths:
                    extra_paths.append(resolved)
        if extra_paths:
            env["PATH"] = current_path + os.pathsep + os.pathsep.join(extra_paths) if current_path else os.pathsep.join(extra_paths)

    async def start(self) -> None:
        """Start the pi agent subprocess in RPC mode."""
        if self._started:
            return

        args = [self.config.pi_binary, "--mode", "rpc"]
        if self.config.approve_project_files:
            args.append("--approve")
        if self.config.model:
            args.extend(["--model", self.config.model])

        logger.info(f"Starting pi agent: {' '.join(args)}")

        env = os.environ.copy()
        env.setdefault("PI_NO_COLOR", "1")
        # Ensure common Node.js paths are available (uv run has clean PATH)
        self._inject_node_path(env)

        self._process = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self.config.cwd,
            env=env,
        )

        # Start stderr reader for diagnostics
        asyncio.create_task(self._read_stderr())

        self._reader_task = asyncio.create_task(self._read_responses())
        self._started = True
        logger.info("Pi agent started in RPC mode")

    async def stop(self) -> None:
        """Stop the pi agent subprocess."""
        if not self._started:
            return

        # Close stdin to signal EOF, pi handles graceful shutdown
        if self._process and self._process.stdin:
            try:
                self._process.stdin.close()
            except Exception:
                pass

        if self._reader_task:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except asyncio.CancelledError:
                pass

        if self._process:
            try:
                await asyncio.wait_for(self._process.wait(), timeout=5)
            except asyncio.TimeoutError:
                self._process.kill()
                await self._process.wait()

        self._started = False
        logger.info("Pi agent stopped")

    # ── Event callback ─────────────────────────────────────────

    def on_agent_event(self, callback: Callable[[dict], None]) -> None:
        """Register a callback for AgentSessionEvent objects from pi.

        The callback receives the raw dict from pi's stdout.
        Called for every JSON line that is NOT a response or extension_ui_request.
        """
        self._event_callbacks.append(callback)

    def remove_event_callback(self, callback: Callable[[dict], None]) -> None:
        try:
            self._event_callbacks.remove(callback)
        except ValueError:
            pass

    # ── RPC commands ───────────────────────────────────────────

    async def send_prompt(
        self,
        message: str,
        images: list[str] | None = None,
        streaming_behavior: str | None = None,
    ) -> str:
        """Send a prompt to pi. Returns the command ID.

        pi streams AgentSessionEvent objects during processing.
        Listen via on_agent_event().
        """
        params: dict[str, Any] = {"message": message}
        if images:
            params["images"] = images
        if streaming_behavior:
            params["streamingBehavior"] = streaming_behavior

        return await self._send_command("prompt", params)

    async def steer(self, message: str, images: list[str] | None = None) -> str:
        """Send a steering (interrupt + redirect) message."""
        params: dict[str, Any] = {"message": message}
        if images:
            params["images"] = images
        return await self._send_command("steer", params)

    async def follow_up(self, message: str, images: list[str] | None = None) -> str:
        """Queue a follow-up message (runs after current turn completes)."""
        params: dict[str, Any] = {"message": message}
        if images:
            params["images"] = images
        return await self._send_command("follow_up", params)

    async def abort(self) -> str:
        """Abort the current operation."""
        return await self._send_command("abort", {})

    async def new_session(self, parent_session: str | None = None) -> dict:
        """Create a new session. Returns {cancelled: bool}."""
        params = {}
        if parent_session:
            params["parentSession"] = parent_session
        return await self._send_command_and_wait("new_session", params)

    async def get_state(self) -> dict:
        """Get current session state (model, thinking level, etc.)."""
        return await self._send_command_and_wait("get_state", {})

    async def set_model(self, provider: str, model_id: str) -> dict:
        """Set the active model."""
        return await self._send_command_and_wait("set_model", {
            "provider": provider,
            "modelId": model_id,
        })

    async def cycle_model(self) -> dict:
        """Cycle to the next available/scoped model."""
        return await self._send_command_and_wait("cycle_model", {})

    async def get_available_models(self) -> list[dict]:
        """List all available models."""
        result = await self._send_command_and_wait("get_available_models", {})
        return result.get("models", [])

    async def compact(self, custom_instructions: str | None = None) -> dict:
        """Trigger context compaction."""
        params = {}
        if custom_instructions:
            params["customInstructions"] = custom_instructions
        return await self._send_command_and_wait("compact", params)

    async def set_auto_compaction(self, enabled: bool) -> dict:
        """Enable/disable pi's auto-compaction."""
        return await self._send_command_and_wait("set_auto_compaction", {"enabled": enabled})

    async def get_commands(self) -> list[dict]:
        """Get all available commands (including skills).

        Returns list of RpcSlashCommand with fields: name, description, source, sourceInfo.
        source can be: "skill", "extension", "prompt".
        """
        result = await self._send_command_and_wait("get_commands", {})
        return result.get("commands", [])

    async def get_messages(self) -> list[dict]:
        """Get all messages in the current session."""
        result = await self._send_command_and_wait("get_messages", {})
        return result.get("messages", [])

    async def set_thinking_level(self, level: str) -> dict:
        """Set thinking level (off, minimal, low, medium, high)."""
        return await self._send_command_and_wait("set_thinking_level", {"level": level})

    async def cycle_thinking_level(self) -> dict:
        """Cycle pi's active thinking level."""
        return await self._send_command_and_wait("cycle_thinking_level", {})

    async def bash(self, command: str, exclude_from_context: bool = False) -> dict:
        """Execute a bash command via pi."""
        return await self._send_command_and_wait("bash", {
            "command": command,
            "excludeFromContext": exclude_from_context,
        })

    async def get_session_stats(self) -> dict:
        """Return pi session statistics."""
        return await self._send_command_and_wait("get_session_stats", {})

    async def export_html(self, output_path: str | None = None) -> dict:
        """Export current pi session to HTML."""
        params = {}
        if output_path:
            params["outputPath"] = output_path
        return await self._send_command_and_wait("export_html", params)

    async def get_last_assistant_text(self) -> str | None:
        """Return the latest assistant message text in the pi session."""
        result = await self._send_command_and_wait("get_last_assistant_text", {})
        text = result.get("text")
        return text if isinstance(text, str) else None

    # ── Convenience methods ────────────────────────────────────

    async def list_skills(self) -> list[dict]:
        """List all available pi skills.

        Filters get_commands() results to only return skills.
        """
        commands = await self.get_commands()
        return [c for c in commands if c.get("source") == "skill"]

    async def invoke_skill(
        self,
        skill_name: str,
        task_description: str = "",
    ) -> str:
        """Invoke a pi skill by sending a prompt that references it.

        Pi loads skills from .agents/skills/ as markdown files via its
        resource loader. The agent uses skills when asked to do so.
        This sends a prompt formatted to ask pi to use the skill.

        Returns the command ID for event correlation.

        NOTE: This is fire-and-forget — it returns as soon as the prompt is
        queued, NOT when pi finishes. For SOP node execution you almost always
        want :meth:`run_prompt_to_completion`, which actually awaits the turn
        and returns pi's real output.
        """
        prompt = self._build_skill_prompt(skill_name, task_description)
        return await self.send_prompt(prompt)

    @staticmethod
    def _build_skill_prompt(skill_name: str, task_description: str = "") -> str:
        """Build a prompt that DETERMINISTICALLY invokes a pi skill.

        pi triggers a skill via the native ``/skill:<name> <args>`` command:
        its AgentSession._expandSkillCommand() (agent-session.ts) expands
        ``/skill:name args`` into the skill's full markdown body wrapped in a
        ``<skill>...</skill>`` block plus the trailing args. This loads the
        skill's system prompt / tool whitelist / resources — i.e. pi's *full*
        skill capability — rather than merely *asking* the model to use it.

        If ``skill_name`` doesn't match a loaded skill, pi passes the text
        through unchanged (it just becomes a normal prompt), so this is safe.

        When ``skill_name`` is empty/None, this is an ad-hoc plain-prompt node
        (方案A: a single-turn Q&A is a one-node task with no skill). In that
        case we send the raw prompt so pi runs its full agent loop on the user's
        question directly, WITHOUT prefixing a ``/skill:`` command.
        """
        args = task_description.strip()
        if not skill_name:
            # Ad-hoc / pure-prompt node — send the question as-is.
            return args
        base = f"/skill:{skill_name}"
        return f"{base} {args}" if args else base

    async def run_skill_to_completion(
        self,
        skill_name: str,
        task_description: str = "",
        *,
        on_event: Callable[[dict], None] | None = None,
        timeout: float | None = None,
    ) -> "PiTurnResult":
        """Invoke a skill via prompt and wait until pi finishes the turn.

        Returns a :class:`PiTurnResult` with the accumulated assistant text
        and the tool calls pi made. See :meth:`run_prompt_to_completion`.
        """
        prompt = self._build_skill_prompt(skill_name, task_description)
        return await self.run_prompt_to_completion(
            prompt, on_event=on_event, timeout=timeout
        )

    async def run_prompt_to_completion(
        self,
        message: str,
        *,
        images: list[str] | None = None,
        on_event: Callable[[dict], None] | None = None,
        timeout: float | None = None,
    ) -> "PiTurnResult":
        """Send a prompt and await pi's turn completion, returning the result.

        This is the convergence primitive that turns pi's *streaming* protocol
        into an awaitable call. It:

        1. Registers a temporary event callback on the bridge.
        2. Sends the prompt.
        3. Accumulates streamed assistant text and tool-call activity.
        4. Resolves when pi emits ``agent_end`` with ``willRetry == false``
           (a retrying turn is not really finished, so we keep waiting).

        pi's event vocabulary (from AgentEvent in packages/agent/src/types.ts):
          - ``agent_start``               — turn begins
          - ``message_update``            — streaming assistant message (deltas)
          - ``message_end``               — assistant message finalized
          - ``tool_execution_start/end``  — tool call lifecycle
          - ``agent_end`` (+ willRetry)   — turn-end signal

        Args:
            message: The prompt text to send.
            images: Optional images to attach.
            on_event: Optional passthrough for every raw pi event, so callers
                (e.g. the SOP executor) can forward them to the EventBus for
                the TUI / Web UI to render pi's live execution.
            timeout: Max seconds to wait for turn completion. Defaults to
                ``config.request_timeout``.

        Returns:
            PiTurnResult with ``text``, ``tool_calls``, ``messages`` and
            ``command_id``.

        Raises:
            asyncio.TimeoutError: if pi does not finish within ``timeout``.
            PiRpcError: if pi reports the prompt failed.
        """
        loop = asyncio.get_event_loop()
        done: asyncio.Future[PiTurnResult] = loop.create_future()

        acc = _TurnAccumulator()

        def _callback(evt: dict) -> None:
            # Passthrough first so live UI updates even mid-turn.
            if on_event is not None:
                try:
                    on_event(evt)
                except Exception as e:  # never let a UI callback break convergence
                    logger.error(f"on_event callback error: {e}")

            try:
                acc.ingest(evt)
            except Exception as e:
                logger.error(f"Error accumulating pi event: {e}")

            if done.done():
                return

            etype = evt.get("type")
            if etype == "agent_end":
                # A turn that will be retried is not actually finished.
                if evt.get("willRetry"):
                    return
                result = acc.result()
                if result.error_message:
                    done.set_exception(PiRpcError("prompt", result.error_message))
                else:
                    done.set_result(result)

        self.on_agent_event(_callback)
        try:
            command_id = await self.send_prompt(message, images=images)
            acc.command_id = command_id
            result = await asyncio.wait_for(
                done, timeout=timeout or self.config.request_timeout
            )
            return result
        finally:
            self.remove_event_callback(_callback)

    # ── Internal ───────────────────────────────────────────────

    def _next_id(self) -> str:
        """Generate a unique command ID."""
        self._cmd_counter += 1
        return f"symphony-{int(time.time() * 1000)}-{self._cmd_counter}"

    async def _send_command(self, cmd_type: str, params: dict) -> str:
        """Send a command to pi. Returns the command ID.

        Does NOT wait for the response — most commands trigger
        streaming agent events. Use _send_command_and_wait for
        synchronous commands.
        """
        if not self._started or not self._process or not self._process.stdin:
            raise RuntimeError("PiBridge not started. Call start() first.")

        cmd_id = self._next_id()
        command = {"type": cmd_type, "id": cmd_id, **params}

        future: asyncio.Future[dict] = asyncio.get_event_loop().create_future()
        self._pending[cmd_id] = future

        payload = json.dumps(command, ensure_ascii=False) + "\n"

        async with self._stdin_lock:
            self._process.stdin.write(payload.encode())
            await self._process.stdin.drain()

        return cmd_id

    async def _send_command_and_wait(self, cmd_type: str, params: dict) -> dict:
        """Send a command and wait for the response.

        For synchronous commands like get_state, set_model, etc.
        """
        cmd_id = await self._send_command(cmd_type, params)

        try:
            future = self._pending[cmd_id]
            result = await asyncio.wait_for(future, timeout=self.config.request_timeout)

            if isinstance(result, dict) and result.get("success") is False:
                raise PiRpcError(
                    cmd_type,
                    result.get("error", "Unknown error"),
                )

            return result.get("data", {}) if isinstance(result, dict) else {}

        except asyncio.TimeoutError:
            raise PiRpcError(cmd_type, f"Request timed out after {self.config.request_timeout}s")
        finally:
            self._pending.pop(cmd_id, None)

    async def _read_responses(self) -> None:
        """Read JSON lines from pi's stdout and dispatch."""
        assert self._process and self._process.stdout

        buffer = b""
        while True:
            try:
                chunk = await self._process.stdout.read(4096)
                if not chunk:
                    logger.info("Pi stdout closed")
                    break

                buffer += chunk
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    if not line.strip():
                        continue
                    try:
                        msg = json.loads(line.decode())
                        self._dispatch_message(msg)
                    except json.JSONDecodeError:
                        logger.debug(f"Non-JSON line from pi: {line[:200]!r}")

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error reading pi stdout: {e}")
                break

    def _dispatch_message(self, msg: dict) -> None:
        """Dispatch a JSON message from pi's stdout.

        Three message types:
        1. {"type": "response", ...} -> resolve pending future
        2. {"type": "extension_ui_request", ...} -> handle/ignore
        3. Everything else -> AgentSessionEvent -> forward to callbacks
        """
        msg_type = msg.get("type")

        if msg_type == "response":
            # Response to a command
            cmd_id = msg.get("id")
            if cmd_id and cmd_id in self._pending:
                self._pending[cmd_id].set_result(msg)

        elif msg_type == "extension_ui_request":
            # Extension UI request — log and ignore for now
            method = msg.get("method", "unknown")
            logger.debug(f"Extension UI request: {method}")

        else:
            # AgentSessionEvent — forward to all callbacks
            for cb in self._event_callbacks:
                try:
                    cb(msg)
                except Exception as e:
                    logger.error(f"Error in agent event callback: {e}")

    async def _read_stderr(self) -> None:
        """Read pi's stderr for diagnostics."""
        assert self._process and self._process.stderr

        while True:
            try:
                line = await self._process.stderr.readline()
                if not line:
                    break
                text = line.decode().rstrip()
                if text:
                    logger.debug(f"pi stderr: {text}")
            except asyncio.CancelledError:
                break
            except Exception:
                break


class PiRpcError(Exception):
    """Error from pi agent RPC."""

    def __init__(self, command: str, message: str):
        self.command = command
        self.message = message
        super().__init__(f"Pi RPC error [{command}]: {message}")
