"""Built-in LLM providers — OpenAI-compatible + Mira + custom HTTP.

Supports two provider types:
    provider_type = "openai"  (default) — any OpenAI-compatible API
    provider_type = "mira"    — Mira (ByteDance internal) API
    provider_type = "custom_http" — configurable non-standard HTTP API

Configuration via data/config.toml:
    [provider]
    type = "mira"                                      # or "openai"
    api_key = "eyJhbG..."                              # mira_session token
    model = "re-o-48"                                  # model ID
    base_url = "https://mira.byteintl.net"             # Mira host
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Optional

logger = logging.getLogger(__name__)

# ── Mira control tag filter ──────────────────────────────

class _ControlTagFilter:
    """Filters out control tags like <cis-ctrl>/<cis-meta> from Mira stream output."""

    TAGS = ["cis-ctrl", "cis-meta"]

    def __init__(self):
        self._pending = ""
        self._hidden_tag: str | None = None
        self._max_open_len = max(len(f"<{t}>") for t in self.TAGS)

    def push(self, chunk: str) -> str:
        input_str = self._pending + chunk
        self._pending = ""
        output = ""

        while input_str:
            if self._hidden_tag:
                close = input_str.find(f"</{self._hidden_tag}>")
                if close < 0:
                    return output
                input_str = input_str[close + len(f"</{self._hidden_tag}>"):]
                self._hidden_tag = None
                continue

            # Find earliest open tag
            earliest: tuple[int, str] | None = None
            for tag in self.TAGS:
                idx = input_str.find(f"<{tag}>")
                if idx >= 0 and (earliest is None or idx < earliest[0]):
                    earliest = (idx, tag)

            if earliest is None:
                # Check for trailing partial open tag
                split = self._split_trailing(input_str)
                self._pending = split[1]
                output += split[0]
                break

            idx, tag = earliest
            output += input_str[:idx]
            input_str = input_str[idx + len(f"<{tag}>"):]
            self._hidden_tag = tag

        return output

    def _split_trailing(self, s: str) -> tuple[str, str]:
        max_len = min(self._max_open_len - 1, len(s))
        for length in range(max_len, 0, -1):
            suffix = s[-length:]
            if any(f"<{t}>".startswith(suffix) for t in self.TAGS):
                return s[:-length], suffix
        return s, ""

    def flush(self) -> str:
        output = self._pending
        self._pending = ""
        self._hidden_tag = None
        return output


# ── Config ────────────────────────────────────────────

@dataclass
class ProviderConfig:
    """Configuration for LLM providers (OpenAI-compatible or Mira)."""

    type: str = "openai"
    """Provider type: "openai" or "mira" """

    base_url: str = ""
    """For openai: API base URL. For mira: Mira host."""

    api_key: str = ""
    """For openai: API key. For mira: mira_session JWT token."""

    model: str = "doubao-1.5-pro-32k"
    """Model ID."""

    max_tokens: int = 4096

    temperature: float = 0.7

    extra_headers: dict[str, str] = field(default_factory=dict)

    # Generic non-standard HTTP adapter settings.
    endpoint: str = ""
    method: str = "POST"
    auth_header: str = "Authorization"
    auth_prefix: str = "Bearer "
    request_template: dict[str, Any] = field(default_factory=dict)
    response_path: str = ""
    stream_response_path: str = ""
    stream: bool = False

    @classmethod
    def from_config(cls, config_dict: dict) -> "ProviderConfig":
        return cls(
            type=config_dict.get("type", "openai"),
            base_url=config_dict.get("base_url", ""),
            api_key=config_dict.get("api_key", ""),
            model=config_dict.get("model", "doubao-1.5-pro-32k"),
            max_tokens=config_dict.get("max_tokens", 4096),
            temperature=config_dict.get("temperature", 0.7),
            extra_headers=config_dict.get("extra_headers", {}),
            endpoint=config_dict.get("endpoint", ""),
            method=config_dict.get("method", "POST"),
            auth_header=config_dict.get("auth_header", "Authorization"),
            auth_prefix=config_dict.get("auth_prefix", "Bearer "),
            request_template=config_dict.get("request_template", {}),
            response_path=config_dict.get("response_path", ""),
            stream_response_path=config_dict.get("stream_response_path", ""),
            stream=config_dict.get("stream", False),
        )


# ── Mira Provider ─────────────────────────────────────

class MiraProvider:
    """Mira API provider — session-based auth, double SSE parsing, control tag filter.

    Protocol:
        1. POST /mira/api/v1/chat/create  → sessionId
        2. POST /mira/api/v1/chat/completion → SSE stream
        3. Each SSE "data:" line is JSON. Inside, "Message" is a JSON string
           with the actual event (content/reason/tool_use etc.)
    """

    MIRA_TOOLS = [
        {"name": "Web", "id": 54604802835, "scope": "GLOBAL"},
        {"name": "ByteDanceContext", "id": 117073920019, "scope": "GLOBAL"},
        {"name": "ImageRich", "id": 54604820243, "scope": "GLOBAL"},
    ]

    def __init__(self, config: ProviderConfig):
        self.host = (config.base_url or "https://mira.byteintl.net").rstrip("/")
        self.session = config.api_key
        self.model = config.model or "re-o-48"
        self._available = bool(self.session)

    @property
    def is_available(self) -> bool:
        return self._available

    def _headers(self) -> dict[str, str]:
        return {
            "Cookie": f"mira_session={self.session}",
            "jwt-token": self.session,
            "content-type": "application/json",
            "x-mira-timezone": "Asia/Shanghai",
        }

    async def _create_session(self, topic: str = "Symphony task") -> str:
        """Create a Mira chat session. Returns sessionId."""
        import aiohttp
        async with aiohttp.ClientSession() as http:
            async with http.post(
                f"{self.host}/mira/api/v1/chat/create",
                headers=self._headers(),
                json={
                    "sessionProperties": {
                        "topic": topic,
                        "dataSource": "manus",
                        "dataSources": ["manus"],
                        "model": self.model,
                    },
                },
            ) as resp:
                text = await resp.text()
                try:
                    data = json.loads(text)
                except json.JSONDecodeError:
                    raise MiraError(f"create session: HTTP {resp.status}, body={text[:300]}")

                if not resp.ok:
                    raise MiraError(f"create session: HTTP {resp.status}, body={text[:300]}")

                # Check for error indicators: code != 0, or success = false
                code = data.get("code")
                success = data.get("success")
                if (code is not None and code != 0) or (success is not None and not success):
                    raise MiraError(f"create session: {json.dumps(data)}")

                item = data.get("sessionItem") or data.get("data", {}).get("sessionItem") or data.get("data", {})
                sid = item.get("sessionId") or item.get("session_id")
                if not sid:
                    raise MiraError(f"no sessionId in response: {json.dumps(data)}")
                return str(sid)

    async def chat(
        self,
        messages: list[dict[str, str]],
        tools: list[dict] | None = None,
        stream: bool = True,
    ) -> AsyncIterator[dict]:
        """Mira streaming chat. Converts messages to a single prompt."""
        if not self._available:
            yield {"type": "error", "message": "Mira provider: api_key (mira_session) not configured"}
            return

        # Convert messages to a single prompt (Mira doesn't support multi-turn via API)
        prompt = ""
        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            if role == "system":
                prompt = f"{content}\n\n{prompt}"
            elif role == "user":
                if prompt:
                    prompt += f"\n\nUser: {content}"
                else:
                    prompt = content

        try:
            import aiohttp

            # Step 1: Create session
            session_id = await self._create_session()

            # Step 2: Stream completion
            async with aiohttp.ClientSession() as http:
                tool_list = self.MIRA_TOOLS.copy()
                # Add any user-defined tools
                if tools:
                    for t in tools:
                        fn = t.get("function", {})
                        tool_list.append({"name": fn.get("name", "unknown"), "id": 0, "scope": "GLOBAL"})

                body = {
                    "sessionId": session_id,
                    "content": prompt,
                    "messageType": 1,
                    "summaryAgent": self.model,
                    "dataSources": ["manus"],
                    "comprehensive": 0,
                    "config": {
                        "online": True,
                        "mode": "quick",
                        "model": self.model,
                        "tool_list": tool_list,
                    },
                }

                async with http.post(
                    f"{self.host}/mira/api/v1/chat/completion",
                    headers=self._headers(),
                    json=body,
                ) as resp:
                    if not resp.ok or not resp.content:
                        text = await resp.text() if resp.content else ""
                        yield {"type": "error", "message": f"Mira completion: HTTP {resp.status} {text[:300]}"}
                        return

                    content_filter = _ControlTagFilter()
                    reasoning_filter = _ControlTagFilter()
                    buffer = ""

                    async for chunk in resp.content.iter_any():
                        buffer += chunk.decode(errors="replace")

                        # Parse SSE frames
                        events, buffer = self._parse_sse(buffer)

                        for event_data in events:
                            try:
                                outer = json.loads(event_data)
                            except json.JSONDecodeError:
                                continue

                            if outer.get("done"):
                                break
                            if outer.get("error"):
                                yield {"type": "error", "message": json.dumps(outer["error"])}
                                continue

                            # Inner message is JSON-stringified
                            inner_raw = outer.get("Message")
                            if not inner_raw:
                                continue
                            try:
                                inner = json.loads(inner_raw) if isinstance(inner_raw, str) else inner_raw
                            except json.JSONDecodeError:
                                continue

                            if inner.get("event") == "echo":
                                continue

                            # Extract tool events
                            tool_evt = self._extract_tool(inner)
                            if tool_evt:
                                yield tool_evt

                            # Extract text content
                            content_text = self._extract_content(inner)
                            filtered = content_filter.push(content_text)
                            if filtered:
                                yield {"type": "text", "content": filtered}

                            # Extract reasoning (debug)
                            reasoning_text = self._extract_reasoning(inner)
                            filtered_r = reasoning_filter.push(reasoning_text)
                            if filtered_r:
                                yield {"type": "reasoning", "content": filtered_r}

                    # Flush remaining
                    trailing = content_filter.flush()
                    if trailing:
                        yield {"type": "text", "content": trailing}

        except ImportError:
            yield {"type": "error", "message": "aiohttp not installed"}
        except MiraError as e:
            yield {"type": "error", "message": str(e)}
        except Exception as e:
            logger.error(f"Mira provider error: {e}")
            yield {"type": "error", "message": str(e)}

        yield {"type": "done"}

    async def complete(self, messages: list[dict[str, str]], tools: list[dict] | None = None) -> dict:
        """Non-streaming completion — collects all text from stream."""
        result = {"content": "", "tool_calls": []}
        async for chunk in self.chat(messages, tools=tools):
            if chunk["type"] == "text":
                c = chunk.get("content", "")
                if not isinstance(c, str):
                    c = str(c)
                result["content"] += c
            elif chunk["type"] == "done":
                pass
            elif chunk["type"] == "error":
                result["error"] = chunk["message"]
        return result

    # ── Internal helpers ────────────────────────────

    @staticmethod
    def _parse_sse(buffer: str) -> tuple[list[str], str]:
        """Parse SSE frames. Returns (events, remaining_buffer)"""
        events = []
        frames = buffer.split("\n\n")
        rest = frames.pop() if frames else ""

        for frame in frames:
            data_lines = []
            for line in frame.split("\n"):
                line = line.strip()
                if line.startswith("data:"):
                    data_lines.append(line[5:].lstrip())
            if data_lines:
                events.append("\n".join(data_lines))

        return events, rest

    @staticmethod
    def _extract_content(inner: dict) -> str:
        if inner.get("event") != "content":
            return ""
        data = inner.get("data", {})
        if isinstance(data, str):
            return MiraProvider._extract_result_text(data)
        if isinstance(data, dict):
            content = data.get("content", "")
            if isinstance(content, str):
                return MiraProvider._extract_result_text(content)
            if isinstance(content, (dict, list)):
                return MiraProvider._extract_result_text(content)
            return str(content)
        return ""

    @staticmethod
    def _extract_result_text(value) -> str:
        """Extract user-visible answer text from Mira content payloads.

        Some Mira variants emit a normal text delta, while others emit a JSON
        result envelope as the content payload, for example:

            {"type":"result", "result":"你好", "usage": {...}}

        Showing that whole envelope in the TUI is noisy and makes the answer
        look wrong. Prefer the nested ``result``/``answer``/``content`` fields
        when present, and fall back to the original text otherwise.
        """
        if isinstance(value, str):
            stripped = value.strip()
            if stripped.startswith("{") or stripped.startswith("["):
                try:
                    parsed = json.loads(stripped)
                except json.JSONDecodeError:
                    return value
                extracted = MiraProvider._extract_result_text(parsed)
                return extracted if extracted else value
            return value
        if isinstance(value, dict):
            for key in ("result", "answer", "content", "text"):
                item = value.get(key)
                if isinstance(item, str):
                    return item
                if isinstance(item, (dict, list)):
                    nested = MiraProvider._extract_result_text(item)
                    if nested:
                        return nested
            return json.dumps(value, ensure_ascii=False)
        if isinstance(value, list):
            parts = [MiraProvider._extract_result_text(v) for v in value]
            return "".join(p for p in parts if p)
        return "" if value is None else str(value)

    @staticmethod
    def _extract_reasoning(inner: dict) -> str:
        if inner.get("event") != "reason":
            return ""
        data = inner.get("data", {})
        if not isinstance(data, dict):
            return ""
        stream_event = data.get("event", {})
        if not isinstance(stream_event, dict):
            return ""
        delta = stream_event.get("delta", {})
        if isinstance(delta, dict) and delta.get("type") == "text_delta":
            return delta.get("text", "")
        return ""

    @staticmethod
    def _extract_tool(inner: dict) -> dict | None:
        data = inner.get("data", {})

        # Tool use snapshot (from message.content)
        content = data.get("message", {}).get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    return {
                        "type": "tool_call",
                        "name": block.get("name", ""),
                        "id": block.get("id", ""),
                        "arguments": block.get("input", {}),
                    }

        # Streaming tool events
        stream_event = data.get("event", {})
        if not isinstance(stream_event, dict):
            return None

        if stream_event.get("type") == "content_block_start":
            cb = stream_event.get("content_block", {})
            if isinstance(cb, dict) and cb.get("type") == "tool_use":
                return {
                    "type": "tool_call_start",
                    "name": cb.get("name", ""),
                    "id": cb.get("id", ""),
                    "arguments": cb.get("input", {}),
                }

        if stream_event.get("type") == "content_block_delta":
            delta = stream_event.get("delta", {})
            if isinstance(delta, dict) and delta.get("type") == "input_json_delta":
                return {
                    "type": "tool_input_delta",
                    "partial_json": delta.get("partial_json", ""),
                }

        return None


class MiraError(Exception):
    """Error from Mira API."""
    pass


# ── OpenAI-compatible Provider ────────────────────────

class OpenAIProvider:
    """OpenAI-compatible API provider (Doubao, DeepSeek, OpenAI, etc.)."""

    def __init__(self, config: ProviderConfig):
        self.config = config
        self._available = bool(config.base_url and config.api_key)

    @property
    def is_available(self) -> bool:
        return self._available

    async def chat(
        self,
        messages: list[dict[str, str]],
        tools: list[dict] | None = None,
        stream: bool = True,
    ) -> AsyncIterator[dict]:
        if not self._available:
            yield {"type": "error", "message": "Provider not configured. Set base_url and api_key."}
            return

        url = f"{self.config.base_url.rstrip('/')}/chat/completions"
        body: dict[str, Any] = {
            "model": self.config.model,
            "messages": messages,
            "max_tokens": self.config.max_tokens,
            "temperature": self.config.temperature,
            "stream": stream,
        }
        if tools:
            body["tools"] = tools

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.config.api_key}",
            **self.config.extra_headers,
        }

        try:
            import aiohttp
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=body, headers=headers) as resp:
                    if resp.status != 200:
                        text = await resp.text()
                        yield {"type": "error", "message": f"API error {resp.status}: {text[:500]}"}
                        return
                    if stream:
                        async for chunk in self._parse_stream(resp):
                            yield chunk
                    else:
                        data = await resp.json()
                        choice = data.get("choices", [{}])[0]
                        content = choice.get("message", {}).get("content", "")
                        yield {"type": "done", "content": content}
        except ImportError:
            yield {"type": "error", "message": "aiohttp not installed"}
        except Exception as e:
            logger.error(f"Provider error: {e}")
            yield {"type": "error", "message": str(e)}

    async def _parse_stream(self, resp) -> AsyncIterator[dict]:
        buffer = b""
        async for chunk in resp.content.iter_any():
            buffer += chunk
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                line = line.strip()
                if not line or line == b"data: [DONE]":
                    continue
                if line.startswith(b"data: "):
                    try:
                        data = json.loads(line[6:])
                        choices = data.get("choices", [])
                        if not choices:
                            continue
                        delta = choices[0].get("delta", {})
                        if "content" in delta and delta["content"]:
                            yield {"type": "text", "content": delta["content"]}
                        if "tool_calls" in delta:
                            for tc in delta["tool_calls"]:
                                fn = tc.get("function", {})
                                yield {
                                    "type": "tool_call",
                                    "id": tc.get("id", ""),
                                    "name": fn.get("name", ""),
                                    "arguments": fn.get("arguments", ""),
                                }
                    except json.JSONDecodeError:
                        pass
        yield {"type": "done"}

    async def complete(self, messages: list[dict[str, str]], tools: list[dict] | None = None) -> dict:
        result = {"content": "", "tool_calls": []}
        async for chunk in self.chat(messages, tools=tools, stream=False):
            if chunk["type"] == "done":
                result["content"] = chunk.get("content", "")
            elif chunk["type"] == "error":
                result["error"] = chunk["message"]
        return result


def _messages_to_prompt(messages: list[dict[str, str]]) -> str:
    parts: list[str] = []
    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        if content:
            parts.append(f"{role}: {content}")
    return "\n\n".join(parts)


def _get_path(obj: Any, path: str) -> Any:
    """Read a dotted path from dict/list objects, e.g. choices.0.text."""
    if not path:
        return None
    cur = obj
    for part in path.split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, list):
            try:
                cur = cur[int(part)]
            except Exception:
                return None
        else:
            return None
    return cur


def _render_template(value: Any, context: dict[str, Any]) -> Any:
    """Render {{placeholders}} inside a JSON-serialisable template."""
    if isinstance(value, dict):
        return {k: _render_template(v, context) for k, v in value.items()}
    if isinstance(value, list):
        return [_render_template(v, context) for v in value]
    if not isinstance(value, str):
        return value

    # If the whole string is exactly one placeholder, preserve original type
    # (list/dict/int) instead of stringifying JSON fields such as messages.
    m = re_full_placeholder(value)
    if m:
        return context.get(m, "")

    out = value
    for key, raw in context.items():
        replacement = raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False)
        out = out.replace("{{" + key + "}}", replacement)
    return out


def re_full_placeholder(value: str) -> str | None:
    import re
    m = re.fullmatch(r"\{\{\s*([a-zA-Z0-9_]+)\s*\}\}", value.strip())
    return m.group(1) if m else None


class CustomHTTPProvider:
    """Configurable adapter for non-OpenAI HTTP LLM APIs.

    It maps Symphony's messages into a user-provided JSON template and extracts
    response text using a configurable dotted path. This covers many internal
    "one POST returns answer" APIs without writing a new Python class each time.
    """

    DEFAULT_RESPONSE_PATHS = [
        "choices.0.message.content",
        "choices.0.delta.content",
        "choices.0.text",
        "data.answer",
        "data.content",
        "data.text",
        "answer",
        "content",
        "text",
        "message",
    ]

    def __init__(self, config: ProviderConfig):
        self.config = config
        self.base_url = config.base_url.rstrip("/")
        self.endpoint = config.endpoint or ""
        self._available = bool((self.base_url or self.endpoint) and config.model)

    @property
    def is_available(self) -> bool:
        return self._available

    def _url(self) -> str:
        if self.endpoint.startswith("http://") or self.endpoint.startswith("https://"):
            return self.endpoint
        if not self.endpoint:
            return self.base_url
        return f"{self.base_url}/{self.endpoint.lstrip('/')}"

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json", **self.config.extra_headers}
        if self.config.api_key:
            headers[self.config.auth_header or "Authorization"] = f"{self.config.auth_prefix}{self.config.api_key}"
        return headers

    def _body(self, messages: list[dict[str, str]], stream: bool) -> dict:
        prompt = _messages_to_prompt(messages)
        context = {
            "model": self.config.model,
            "prompt": prompt,
            "messages": messages,
            "messages_json": json.dumps(messages, ensure_ascii=False),
            "max_tokens": self.config.max_tokens,
            "temperature": self.config.temperature,
            "stream": stream,
        }
        if self.config.request_template:
            rendered = _render_template(self.config.request_template, context)
            return rendered if isinstance(rendered, dict) else {"input": rendered}
        return {
            "model": self.config.model,
            "prompt": prompt,
            "messages": messages,
            "max_tokens": self.config.max_tokens,
            "temperature": self.config.temperature,
            "stream": stream,
        }

    def _extract_text(self, data: Any, *, stream: bool = False) -> str:
        paths = []
        if stream and self.config.stream_response_path:
            paths.append(self.config.stream_response_path)
        if self.config.response_path:
            paths.append(self.config.response_path)
        paths.extend(self.DEFAULT_RESPONSE_PATHS)
        for path in paths:
            val = _get_path(data, path)
            if val is None:
                continue
            if isinstance(val, str):
                return val
            if isinstance(val, (dict, list)):
                return json.dumps(val, ensure_ascii=False)
            return str(val)
        return ""

    async def chat(
        self,
        messages: list[dict[str, str]],
        tools: list[dict] | None = None,
        stream: bool = True,
    ) -> AsyncIterator[dict]:
        if not self._available:
            yield {"type": "error", "message": "custom_http provider not configured. Set base_url/endpoint and model."}
            return
        if tools:
            logger.info("custom_http provider ignores tool schema; use pi/OpenAI-compatible provider for tool calling")

        wants_stream = bool(stream and self.config.stream)
        try:
            import aiohttp
            async with aiohttp.ClientSession() as session:
                async with session.request(
                    self.config.method.upper() or "POST",
                    self._url(),
                    json=self._body(messages, wants_stream),
                    headers=self._headers(),
                ) as resp:
                    if resp.status < 200 or resp.status >= 300:
                        text = await resp.text()
                        yield {"type": "error", "message": f"custom_http API error {resp.status}: {text[:500]}"}
                        return

                    if wants_stream:
                        async for chunk in self._parse_stream(resp):
                            yield chunk
                    else:
                        data = await resp.json(content_type=None)
                        text = self._extract_text(data)
                        if text:
                            yield {"type": "text", "content": text}
                        else:
                            yield {"type": "error", "message": f"custom_http response_path not found in response: {json.dumps(data, ensure_ascii=False)[:500]}"}
                            return
        except ImportError:
            yield {"type": "error", "message": "aiohttp not installed"}
            return
        except Exception as e:
            logger.error("custom_http provider error: %s", e)
            yield {"type": "error", "message": str(e)}
            return

        yield {"type": "done"}

    async def _parse_stream(self, resp) -> AsyncIterator[dict]:
        buffer = ""
        async for raw in resp.content.iter_any():
            buffer += raw.decode(errors="replace")
            while "\n" in buffer:
                line, buffer = buffer.split("\n", 1)
                line = line.strip()
                if not line:
                    continue
                if line.startswith("data:"):
                    line = line[5:].strip()
                if line == "[DONE]":
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    yield {"type": "text", "content": line}
                    continue
                text = self._extract_text(data, stream=True)
                if text:
                    yield {"type": "text", "content": text}

    async def complete(self, messages: list[dict[str, str]], tools: list[dict] | None = None) -> dict:
        result = {"content": "", "tool_calls": []}
        async for chunk in self.chat(messages, tools=tools, stream=False):
            if chunk["type"] == "text":
                result["content"] += chunk.get("content", "")
            elif chunk["type"] == "error":
                result["error"] = chunk["message"]
        return result


# ── Provider factory ──────────────────────────────────

def create_provider(config: ProviderConfig):
    """Create the right provider based on config.type."""
    if config.type == "mira":
        return MiraProvider(config)
    if config.type in {"custom_http", "http", "nonstandard"}:
        return CustomHTTPProvider(config)
    return OpenAIProvider(config)
