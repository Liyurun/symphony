"""User-editable configuration persisted as TOML.

Separate from Settings (env-var based). Editable from both TUI and Web UI.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

try:
    import tomllib as _tomllib
except ImportError:
    try:
        import tomli as _tomllib
    except ImportError:
        _tomllib = None  # type: ignore

logger = logging.getLogger(__name__)

try:
    import tomli_w
except ImportError:
    tomli_w = None  # type: ignore


def _clean_dict(d: dict) -> dict:
    """Replace None values with empty strings for TOML serialization."""
    result = {}
    for k, v in d.items():
        if v is None:
            result[k] = ""
        elif isinstance(v, dict):
            result[k] = _clean_dict(v)
        else:
            result[k] = v
    return result


from pydantic import BaseModel, Field


class PiAgentConfig(BaseModel):
    """Pi agent connection and behavior settings."""

    binary_path: str = Field(default="pi", description="Path to pi binary")
    default_model: Optional[str] = Field(default=None, description="Default model ID")
    startup_timeout: float = Field(default=30.0, description="Max seconds for pi startup")
    request_timeout: float = Field(default=120.0, description="Default RPC timeout")
    thinking_level: str = Field(default="medium", description="Thinking level: off/minimal/low/medium/high")
    auto_compaction: bool = Field(default=True, description="Auto-compact context")


class WebUIConfig(BaseModel):
    """Web UI display settings."""

    host: str = Field(default="0.0.0.0")
    port: int = Field(default=8080)
    theme: str = Field(default="dark", description="UI theme: dark or light")
    auto_scroll: bool = Field(default=True, description="Auto-scroll agent output")
    max_log_entries: int = Field(default=1000, ge=100, le=100000)
    refresh_interval_ms: int = Field(default=100, ge=50, le=5000)


class ProviderConfig(BaseModel):
    """Built-in LLM provider settings.

    Supports provider types:
    - "openai": OpenAI-compatible API (Doubao, DeepSeek, OpenAI, etc.)
    - "mira": Mira (ByteDance internal) API
    - "custom_http" / "http": configurable non-standard HTTP API adapter
    """

    type: str = Field(default="openai", description="Provider type: openai/mira/custom_http")
    base_url: str = Field(default="", description="API base URL / Mira host")
    api_key: str = Field(default="", description="API key / Mira session token")
    model: str = Field(default="doubao-1.5-pro-32k", description="Model ID")
    max_tokens: int = Field(default=4096)
    temperature: float = Field(default=0.7)
    extra_headers: dict[str, str] = Field(default_factory=dict)

    # Custom non-standard HTTP provider knobs. These are intentionally generic:
    # users can map Symphony's messages/prompt into any JSON body and tell us
    # where the response text lives.
    endpoint: str = Field(default="", description="Custom HTTP endpoint path or absolute URL")
    method: str = Field(default="POST", description="HTTP method for custom_http")
    auth_header: str = Field(default="Authorization", description="Header name used for api_key")
    auth_prefix: str = Field(default="Bearer ", description="Prefix before api_key; set empty for raw token")
    request_template: dict = Field(default_factory=dict, description="JSON request template with {{placeholders}}")
    response_path: str = Field(default="", description="Dot path to response text, e.g. data.answer")
    stream_response_path: str = Field(default="", description="Dot path to text inside each SSE/JSONL event")
    stream: bool = Field(default=False, description="Whether custom_http should request/parse streaming response")


class TUIConfig(BaseModel):
    """Terminal UI display settings."""

    theme: str = Field(default="textual-dark")
    compact_view: bool = Field(default=False)
    log_normal_chat: bool = Field(
        default=True,
        description="Record normal (non-SOP) pi chat turns as tasks so the Web "
        "UI can display and later analyze them.",
    )


class UserConfig(BaseModel):
    """User-editable configuration persisted to data/config.toml."""

    pi_agent: PiAgentConfig = Field(default_factory=PiAgentConfig)
    web_ui: WebUIConfig = Field(default_factory=WebUIConfig)
    tui: TUIConfig = Field(default_factory=TUIConfig)
    provider: ProviderConfig = Field(default_factory=ProviderConfig)
    providers: dict[str, ProviderConfig] = Field(default_factory=dict)
    """Named providers, e.g. providers.mira, providers.doubao"""

    # Which provider to use: "default" (uses [provider] section) or a name from [providers]
    active_provider: str = Field(default="default")

    def get_active_provider_config(self) -> ProviderConfig:
        """Get the currently active provider config (default or named)."""
        if self.active_provider != "default" and self.active_provider in self.providers:
            return self.providers[self.active_provider]
        return self.provider

    @classmethod
    def load(cls, path: str | Path = "data/config.toml") -> "UserConfig":
        """Load from TOML file, creating with defaults if missing."""
        path = Path(path)
        if not path.exists():
            return cls()

        if _tomllib is None:
            logger.warning("tomllib/tomli not available, cannot load TOML config")
            return cls()

        try:
            with open(path, "rb") as f:
                data = _tomllib.load(f)

            pi_data = data.get("pi_agent", {})
            web_data = data.get("web_ui", {})
            tui_data = data.get("tui", {})
            provider_data = data.get("provider", {})
            active_provider = data.get("active_provider", "default")

            # Parse named providers
            named_providers = {}
            raw_providers = data.get("providers", {})
            if isinstance(raw_providers, dict):
                for name, pdata in raw_providers.items():
                    if isinstance(pdata, dict):
                        named_providers[name] = ProviderConfig(**pdata)

            return cls(
                pi_agent=PiAgentConfig(**pi_data) if pi_data else PiAgentConfig(),
                web_ui=WebUIConfig(**web_data) if web_data else WebUIConfig(),
                tui=TUIConfig(**tui_data) if tui_data else TUIConfig(),
                provider=ProviderConfig(**provider_data) if provider_data else ProviderConfig(),
                providers=named_providers,
                active_provider=active_provider,
            )
        except Exception as e:
            logger.warning(f"Failed to load config from {path}: {e}, using defaults")
            return cls()

    def save(self, path: str | Path = "data/config.toml") -> None:
        """Persist to TOML file."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)

        data = {
            "active_provider": self.active_provider,
            "provider": _clean_dict(self.provider.model_dump()),
            "providers": {name: _clean_dict(p.model_dump()) for name, p in self.providers.items()},
            "pi_agent": _clean_dict(self.pi_agent.model_dump()),
            "web_ui": _clean_dict(self.web_ui.model_dump()),
            "tui": _clean_dict(self.tui.model_dump()),
        }

        if tomli_w is not None:
            with open(path, "wb") as f:
                tomli_w.dump(data, f)
        else:
            # Fallback: write as simple TOML without tomli_w. The payload mixes
            # scalar top-level keys (e.g. active_provider), flat sections
            # (pi_agent/web_ui/tui) and a nested dict-of-dicts (providers), so we
            # must dispatch on value type instead of assuming every value is a
            # section table.
            import json

            def _fmt(value) -> str:
                if value is None:
                    return '""'
                if isinstance(value, bool):
                    return str(value).lower()
                if isinstance(value, (int, float)):
                    return str(value)
                return json.dumps(value, ensure_ascii=False)

            def _fields(table: dict) -> list[str]:
                out = []
                for key, value in table.items():
                    if isinstance(value, dict):
                        continue  # nested tables are emitted separately
                    out.append(f"{key} = {_fmt(value)}")
                return out

            lines = ["# Symphony User Configuration"]

            # 1) Scalar top-level keys first (must precede any [section] header).
            for key, value in data.items():
                if not isinstance(value, dict):
                    lines.append(f"{key} = {_fmt(value)}")

            # 2) Section tables and nested dict-of-dicts.
            for section, value in data.items():
                if not isinstance(value, dict):
                    continue
                nested = {k: v for k, v in value.items() if isinstance(v, dict)}
                if nested and all(isinstance(v, dict) for v in value.values()):
                    # e.g. [providers.mira]
                    for sub_name, sub_table in nested.items():
                        lines.append(f"\n[{section}.{sub_name}]")
                        lines.extend(_fields(sub_table))
                    continue
                lines.append(f"\n[{section}]")
                lines.extend(_fields(value))
                for sub_name, sub_table in nested.items():
                    lines.append(f"\n[{section}.{sub_name}]")
                    lines.extend(_fields(sub_table))

            with open(path, "w", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")

    def update(self, partial: dict) -> "UserConfig":
        """Update fields from a partial dict, then persist."""
        for section in ("provider", "pi_agent", "web_ui", "tui"):
            if section in partial:
                section_data = partial[section]
                if section == "provider":
                    for key, value in section_data.items():
                        if hasattr(self.provider, key):
                            setattr(self.provider, key, value)
                elif section == "pi_agent":
                    for key, value in section_data.items():
                        if hasattr(self.pi_agent, key):
                            setattr(self.pi_agent, key, value)
                elif section == "web_ui":
                    for key, value in section_data.items():
                        if hasattr(self.web_ui, key):
                            setattr(self.web_ui, key, value)
                elif section == "tui":
                    for key, value in section_data.items():
                        if hasattr(self.tui, key):
                            setattr(self.tui, key, value)
        return self
