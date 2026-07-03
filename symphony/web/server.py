"""Web server — FastAPI application serving REST API, WebSocket, and static UI."""

from __future__ import annotations

import logging
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from symphony.config.user_config import UserConfig
from symphony.core.event_bus import EventBus
from symphony.core.event_log import EventLog
from symphony.core.pi_bridge import PiBridge
from symphony.core.task_manager import TaskManager
from symphony.sop.sop_registry import SOPRegistry
from symphony.web.routes.config import create_config_router
from symphony.web.routes.events import create_events_router
from symphony.web.routes.skills import create_skills_router
from symphony.web.routes.sop import create_sop_router
from symphony.web.routes.tasks import create_tasks_router
from symphony.web.ws.manager import WebSocketManager

logger = logging.getLogger(__name__)


class WebServer:
    """FastAPI web server for Symphony.

    Endpoints:
        GET  /                     -> Static SPA (index.html)
        GET  /api/tasks            -> List all tasks
        POST /api/tasks            -> Create a new task
        GET  /api/tasks/{id}       -> Get task details
        POST /api/tasks/{id}/start -> Start a task
        POST /api/tasks/{id}/cancel-> Cancel a task
        POST /api/tasks/{id}/pause -> Pause a task
        POST /api/tasks/{id}/resume-> Resume a task
        POST /api/tasks/{id}/claim -> Claim a task
        POST /api/tasks/{id}/release -> Release a claimed task
        DELETE /api/tasks/{id}     -> Delete a task
        GET  /api/tasks/{id}/events-> Get task events
        GET  /api/tasks/{id}/export-> Export task events as JSON
        GET  /api/sop              -> List SOP templates
        POST /api/sop              -> Create/update SOP template
        GET  /api/sop/{name}       -> Get SOP template
        POST /api/sop/validate     -> Validate SOP definition
        DELETE /api/sop/{name}     -> Delete SOP template
        POST /api/human/respond    -> Respond to human intervention
        GET  /api/skills           -> List available pi skills
        GET  /api/pi/state         -> Get pi agent state
        GET  /api/pi/models        -> Get available models
        GET  /api/config           -> Get user configuration
        PUT  /api/config           -> Update user configuration
        POST /api/config/reset     -> Reset configuration
        GET  /api/logs             -> Search events
        GET  /api/logs/stats       -> Event statistics
        GET  /api/events/{id}/stream -> SSE event stream
        WS   /ws                   -> WebSocket for real-time events
    """

    def __init__(
        self,
        event_bus: EventBus,
        event_log: EventLog,
        task_manager: TaskManager,
        sop_registry: SOPRegistry,
        pi_bridge: PiBridge | None = None,
        user_config: UserConfig | None = None,
        host: str = "0.0.0.0",
        port: int = 8080,
    ):
        self.event_bus = event_bus
        self.event_log = event_log
        self.task_manager = task_manager
        self.sop_registry = sop_registry
        self.pi_bridge = pi_bridge
        self.user_config = user_config or UserConfig()
        self.host = host
        self.port = port
        self._server: uvicorn.Server | None = None

        self.ws_manager = WebSocketManager(
            event_bus=event_bus,
            event_log=event_log,
            task_manager=task_manager,
            sop_registry=sop_registry,
        )
        self.app = self._create_app()

    def _create_app(self) -> FastAPI:
        app = FastAPI(
            title="Symphony",
            version="0.1.0",
            description="SOP-based multi-agent task orchestrator",
        )

        # CORS for development
        app.add_middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

        @app.middleware("http")
        async def no_cache_static_assets(request, call_next):
            """Prevent browsers from serving stale SPA JS/CSS after local edits.

            The Web UI uses ES modules and dynamic imports; Safari/Chrome may keep
            old modules around across server restarts. While the project is local
            and actively edited, no-store is the safest default.
            """
            response = await call_next(request)
            if request.url.path == "/" or request.url.path.startswith("/static/"):
                response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
                response.headers["Pragma"] = "no-cache"
                response.headers["Expires"] = "0"
            return response

        # WebSocket endpoint — handle API rename across FastAPI versions
        try:
            app.add_websocket_route("/ws", self.ws_manager.handle_connection)
        except AttributeError:
            app.add_api_websocket_route("/ws", self.ws_manager.handle_connection)

        # Register API routes
        app.include_router(
            create_tasks_router(
                self.task_manager, self.event_log, self.event_bus, self.sop_registry
            ),
            prefix="/api",
        )
        app.include_router(
            create_sop_router(self.sop_registry),
            prefix="/api",
        )
        app.include_router(
            create_events_router(
                self.event_log, self.event_bus,
                human_manager=getattr(self.task_manager, "human_manager", None),
            ),
            prefix="/api",
        )
        app.include_router(
            create_config_router(self.user_config),
            prefix="/api",
        )
        if self.pi_bridge:
            app.include_router(
                create_skills_router(self.pi_bridge),
                prefix="/api",
            )

        # Static files
        static_dir = Path(__file__).parent / "static"
        if static_dir.exists():
            from fastapi.responses import FileResponse

            @app.get("/")
            async def serve_index():
                return FileResponse(static_dir / "index.html")

            app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

        return app

    async def start(self, quiet: bool = False) -> None:
        """Start the web server.

        When ``quiet`` is set (used when the full-screen TUI owns the terminal),
        uvicorn's own logging (banner + access log) is fully suppressed so it
        cannot corrupt the TUI rendering. All Symphony logging still goes to the
        log file configured by the CLI.
        """
        config = uvicorn.Config(
            self.app,
            host=self.host,
            port=self.port,
            log_level="warning" if quiet else "info",
            log_config=None if quiet else uvicorn.config.LOGGING_CONFIG,
            access_log=not quiet,
        )
        server = uvicorn.Server(config)
        self._server = server
        try:
            await server.serve()
        finally:
            self._server = None

    async def stop(self) -> None:
        """Request a graceful shutdown when the server is running."""
        if self._server is not None:
            self._server.should_exit = True

    def run_sync(self) -> None:
        """Run the web server synchronously (blocking)."""
        uvicorn.run(self.app, host=self.host, port=self.port)
