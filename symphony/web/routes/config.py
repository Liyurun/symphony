"""Configuration REST API — user settings editable from Web UI."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from symphony.config.user_config import UserConfig


def create_config_router(user_config: UserConfig) -> APIRouter:
    router = APIRouter(tags=["config"])

    @router.get("/config")
    async def get_config():
        """Get current user configuration."""
        return {
            "pi_agent": user_config.pi_agent.model_dump(),
            "web_ui": user_config.web_ui.model_dump(),
            "tui": user_config.tui.model_dump(),
        }

    @router.put("/config")
    async def update_config(partial: dict):
        """Update user configuration (partial update)."""
        try:
            user_config.update(partial)
            user_config.save()
            return {"status": "saved"}
        except Exception as e:
            raise HTTPException(status_code=400, detail=str(e))

    @router.post("/config/reset")
    async def reset_config():
        """Reset configuration to defaults."""
        default = UserConfig()
        user_config.pi_agent = default.pi_agent
        user_config.web_ui = default.web_ui
        user_config.tui = default.tui
        user_config.save()
        return {"status": "reset"}

    return router
