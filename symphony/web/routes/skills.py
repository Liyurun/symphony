"""Skills REST API — list available pi agent skills."""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from symphony.core.pi_bridge import PiBridge


class SetModelRequest(BaseModel):
    provider: str
    model_id: str


class ThinkingRequest(BaseModel):
    level: str


class CompactRequest(BaseModel):
    instructions: str = ""


class BashRequest(BaseModel):
    command: str
    exclude_from_context: bool = False


class AutoCompactionRequest(BaseModel):
    enabled: bool


class ExportHtmlRequest(BaseModel):
    output_path: str = ""


def create_skills_router(pi_bridge: PiBridge) -> APIRouter:
    router = APIRouter(tags=["skills"])

    @router.get("/skills")
    async def list_skills():
        """List all available pi skills."""
        try:
            skills = await pi_bridge.list_skills()
            return [
                {
                    "name": s.get("name", ""),
                    "description": s.get("description", ""),
                    "source": s.get("source", "skill"),
                    "source_info": s.get("sourceInfo", ""),
                }
                for s in skills
            ]
        except Exception as e:
            return {"error": str(e), "skills": []}

    @router.post("/skills/refresh")
    async def refresh_skills():
        """Force-refresh the skill cache by re-querying pi."""
        try:
            skills = await pi_bridge.list_skills()
            return {"status": "refreshed", "count": len(skills)}
        except Exception as e:
            return {"error": str(e)}

    @router.get("/pi/state")
    async def get_pi_state():
        """Get current pi agent session state."""
        try:
            state = await pi_bridge.get_state()
            return state
        except Exception as e:
            return {"error": str(e)}

    @router.get("/pi/context")
    async def get_pi_context():
        """Return pi cwd and context-file discovery evidence."""
        try:
            cfg = pi_bridge.config
            return {
                "cwd": cfg.cwd,
                "context_files": cfg.context_file_infos(),
            }
        except Exception as e:
            return {"error": str(e), "context_files": []}

    @router.get("/pi/models")
    async def get_pi_models():
        """Get available models from pi."""
        try:
            models = await pi_bridge.get_available_models()
            return {"models": models}
        except Exception as e:
            return {"error": str(e), "models": []}

    @router.post("/pi/model")
    async def set_pi_model(req: SetModelRequest):
        """Switch pi's active model."""
        try:
            return await pi_bridge.set_model(req.provider, req.model_id)
        except Exception as e:
            return {"error": str(e)}

    @router.post("/pi/model/cycle")
    async def cycle_pi_model():
        """Cycle pi's active model."""
        try:
            return await pi_bridge.cycle_model()
        except Exception as e:
            return {"error": str(e)}

    @router.post("/pi/thinking")
    async def set_pi_thinking(req: ThinkingRequest):
        """Set pi thinking level."""
        try:
            return await pi_bridge.set_thinking_level(req.level)
        except Exception as e:
            return {"error": str(e)}

    @router.post("/pi/thinking/cycle")
    async def cycle_pi_thinking():
        """Cycle pi's active thinking level."""
        try:
            return await pi_bridge.cycle_thinking_level()
        except Exception as e:
            return {"error": str(e)}

    @router.post("/pi/compact")
    async def compact_pi_context(req: CompactRequest):
        """Trigger pi context compaction."""
        try:
            return await pi_bridge.compact(req.instructions.strip() or None)
        except Exception as e:
            return {"error": str(e)}

    @router.post("/pi/auto-compact")
    async def set_pi_auto_compaction(req: AutoCompactionRequest):
        """Enable/disable pi auto-compaction."""
        try:
            return await pi_bridge.set_auto_compaction(req.enabled)
        except Exception as e:
            return {"error": str(e)}

    @router.post("/pi/new-session")
    async def new_pi_session():
        """Create a new pi session."""
        try:
            return await pi_bridge.new_session()
        except Exception as e:
            return {"error": str(e)}

    @router.get("/pi/commands")
    async def get_pi_commands():
        """List pi slash/prompt/extension/skill commands."""
        try:
            return {"commands": await pi_bridge.get_commands()}
        except Exception as e:
            return {"error": str(e), "commands": []}

    @router.post("/pi/bash")
    async def run_pi_bash(req: BashRequest):
        """Execute a bash command via pi so it enters pi's session context."""
        try:
            return await pi_bridge.bash(
                req.command,
                exclude_from_context=req.exclude_from_context,
            )
        except Exception as e:
            return {"error": str(e)}

    @router.get("/pi/session-stats")
    async def get_pi_session_stats():
        """Return pi session statistics."""
        try:
            return await pi_bridge.get_session_stats()
        except Exception as e:
            return {"error": str(e)}

    @router.post("/pi/export-html")
    async def export_pi_html(req: ExportHtmlRequest):
        """Export current pi session to HTML."""
        try:
            return await pi_bridge.export_html(req.output_path.strip() or None)
        except Exception as e:
            return {"error": str(e)}

    @router.get("/pi/last-assistant-text")
    async def get_pi_last_assistant_text():
        """Return latest assistant text from pi session."""
        try:
            return {"text": await pi_bridge.get_last_assistant_text()}
        except Exception as e:
            return {"error": str(e), "text": None}

    return router
