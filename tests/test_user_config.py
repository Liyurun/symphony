"""Tests for the UserConfig."""

import tempfile
from pathlib import Path

from symphony.config.user_config import (
    PiAgentConfig,
    TUIConfig,
    UserConfig,
    WebUIConfig,
)


class TestUserConfig:
    def test_defaults(self):
        config = UserConfig()
        assert config.pi_agent.binary_path == "pi"
        assert config.web_ui.port == 8080
        assert config.tui.theme == "textual-dark"

    def test_save_and_load(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "config.toml"

            config = UserConfig()
            config.pi_agent.binary_path = "/custom/pi"
            config.web_ui.port = 9090
            config.save(path)

            loaded = UserConfig.load(path)
            assert loaded.pi_agent.binary_path == "/custom/pi"
            assert loaded.web_ui.port == 9090

    def test_update_partial(self):
        config = UserConfig()
        config.update({
            "pi_agent": {"binary_path": "/updated/pi"},
            "web_ui": {"theme": "light"},
        })
        assert config.pi_agent.binary_path == "/updated/pi"
        assert config.web_ui.theme == "light"
        # Untouched fields remain
        assert config.web_ui.port == 8080

    def test_load_missing_returns_defaults(self):
        config = UserConfig.load("/nonexistent/path/config.toml")
        assert config.pi_agent.binary_path == "pi"

    def test_pi_agent_config(self):
        cfg = PiAgentConfig(thinking_level="high", default_model="claude-sonnet")
        assert cfg.thinking_level == "high"
        assert cfg.default_model == "claude-sonnet"

    def test_web_ui_config(self):
        cfg = WebUIConfig(theme="light", auto_scroll=False)
        assert cfg.theme == "light"
        assert cfg.auto_scroll is False
