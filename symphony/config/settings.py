"""Application settings — loaded from environment and config files.

Uses pydantic-settings for type-safe configuration.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Symphony application settings.

    Can be configured via:
    1. Environment variables (SYMPHONY_ prefix)
    2. .env file in the project root
    3. Default values
    """

    model_config = SettingsConfigDict(
        env_prefix="SYMPHONY_",
        env_file=".env",
        env_file_encoding="utf-8",
    )

    # ── Paths ──────────────────────────────

    data_dir: Path = Path("data")
    """Directory for runtime data (event logs, SOP templates, config).

    The file-backed EventLog stores per-task event streams under
    ``<data_dir>/logs/*.jsonl`` and task metadata under
    ``<data_dir>/tasks/*.json``. There is no SQLite database anymore.
    """

    sop_templates_dir: Path = Path("data/sop_templates")
    """Directory for SOP YAML template files."""

    config_path: Path = Path("data/config.toml")
    """TOML config file path."""

    # ── Pi Agent ───────────────────────────

    pi_binary: str = "pi"
    """Path to the pi agent binary."""

    pi_cwd: Optional[str] = None
    """Working directory for pi agent subprocess."""

    pi_model: Optional[str] = None
    """Model override for pi agent."""

    pi_startup_timeout: float = 30.0
    """Max seconds to wait for pi agent to start."""

    pi_request_timeout: float = 120.0
    """Default timeout for pi RPC requests."""

    # ── Server ─────────────────────────────

    web_host: str = "0.0.0.0"
    """Host for the web server."""

    web_port: int = 8080
    """Port for the web server."""

    # ── Logging ────────────────────────────

    log_level: str = "INFO"
    """Logging level (DEBUG, INFO, WARNING, ERROR)."""
