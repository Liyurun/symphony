"""Symphony FastAPI 应用装配。

create_app 依据 SymphonyConfig 组装全部运行时依赖（LLM Provider、模板加载器、
工作区管理器、事件总线、连接管理器、技能注册中心、任务管理器、SOP 生成器），
挂载 REST 路由与一个 WebSocket 端点，并把 EventBus 的发布事件转发到对应
task 的 WebSocket 连接。未构建前端时提供一个兜底首页提示。
"""

import asyncio
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from symphony.agent.chat_session import ChatSessionRunner
from symphony.agent.chat_runtime import ChatRuntime
from symphony.ai.doubao import DoubaoProvider
from symphony.config import SymphonyConfig
from symphony.server.api import chat as chat_api
from symphony.server.api import config as config_api
from symphony.server.api import sessions as sessions_api
from symphony.server.api import skills as skills_api
from symphony.server.api import sop_sessions as sop_sessions_api
from symphony.server.api import sops as sops_api
from symphony.server.api import tasks as tasks_api
from symphony.server.eventbus import EventBus
from symphony.server.manager import TaskManager
from symphony.server.ws import ConnectionManager
from symphony.skills.builtins import register_builtins
from symphony.skills.loader import load_custom_skills
from symphony.skills.references import SkillReferenceIndex
from symphony.skills.registry import SkillRegistry
from symphony.storage import SessionManager
from symphony.storage.workspace import WorkspaceManager
from symphony.workflow.generator import SOPGenerator
from symphony.workflow.template import TemplateLoader

# 项目根目录：app.py 位于 src/symphony/server/app.py，
# parents[0]=server, [1]=symphony, [2]=src, [3]=项目根。
# 解析为绝对路径，确保无论从哪个工作目录启动服务都能正确定位前端产物。
_PROJECT_ROOT = Path(__file__).resolve().parents[3]

# 前端构建产物目录（相对项目根解析为绝对路径，不依赖启动时的 cwd）
_WEB_DIST = _PROJECT_ROOT / "web" / "dist"

# 未构建前端时的兜底首页
_FALLBACK_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head><meta charset="utf-8"><title>Symphony</title></head>
<body style="font-family: system-ui; max-width: 640px; margin: 4rem auto; line-height: 1.6;">
  <h1>Symphony</h1>
  <p>前端尚未构建。请在 <code>web/</code> 目录下执行：</p>
  <pre>npm install &amp;&amp; npm run build</pre>
  <p>或使用开发模式：<code>npm run dev</code>（vite dev server）。</p>
  <p>REST API 已可用，详见 <a href="/docs">/docs</a>。</p>
</body>
</html>"""


def _register_chat_ws(app: FastAPI) -> None:
    """在应用上注册 /ws/chat 流式对话端点。

    客户端连接后发送一帧 {"question", "history"}，服务端把 ChatRuntime 的
    事件逐条 send_json，完成后保持连接以接收下一问。
    """

    @app.websocket("/ws/chat")
    async def chat_ws(websocket: WebSocket) -> None:
        await websocket.accept()
        provider = websocket.app.state.llm_provider
        registry = getattr(websocket.app.state, "skill_registry", None) or SkillRegistry()
        skill_reference_index = getattr(websocket.app.state, "skill_reference_index", None)
        session_id = websocket.query_params.get("session_id")
        try:
            while True:
                payload = await websocket.receive_json()
                question = (payload or {}).get("question", "")
                history = (payload or {}).get("history", [])
                if not question:
                    continue
                try:
                    runtime_kwargs = chat_api.build_chat_runtime_kwargs(
                        getattr(websocket.app.state, "config", None),
                        skill_reference_index,
                    )
                    if session_id:
                        runner = ChatSessionRunner(
                            provider,
                            registry,
                            websocket.app.state.session_manager,
                            **runtime_kwargs,
                        )
                        async for event in runner.stream(session_id, question, history):
                            await websocket.send_json(event.to_dict())
                    else:
                        runtime = ChatRuntime(
                            provider,
                            registry,
                            **runtime_kwargs,
                        )
                        async for event in runtime.stream(question, history):
                            await websocket.send_json(event.to_dict())
                except Exception as exc:  # 运行时/网络边界
                    await websocket.send_json(
                        {"type": "chat_failed", "error": f"问答失败: {exc}"}
                    )
        except WebSocketDisconnect:
            return


def create_app(config: SymphonyConfig) -> FastAPI:
    """依据配置构建并返回一个装配完成的 FastAPI 应用。"""
    # 创建应用实例
    app = FastAPI(title="Symphony", version="0.1.0")
    # 本地开发放开跨域限制
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # 构造 LLM Provider（火山方舟）
    provider = DoubaoProvider(
        api_key=config.llm.api_key,
        model=config.llm.model,
        base_url=config.llm.base_url,
        temperature=config.llm.temperature,
        max_tokens=config.llm.max_tokens,
        timeout=config.llm.timeout_seconds,
    )
    # 模板加载器与工作区管理器
    template_loader = TemplateLoader(config.storage.templates_dir)
    workspace_manager = WorkspaceManager(config.storage.workspace_dir)
    session_manager = SessionManager(config.storage.resolved_sessions_dir())
    # 事件总线与 WebSocket 连接管理器
    event_bus = EventBus()
    conn_manager = ConnectionManager()
    # 技能注册中心并注册全部内置技能
    skill_registry = SkillRegistry()
    register_builtins(skill_registry, skills=config.skills)
    # 加载用户自定义技能；错误保存在 app.state，供 API 展示，不阻塞服务启动
    custom_skill_load_result = load_custom_skills(
        skill_registry, config.storage.custom_skills_dir
    )
    # 外部 Skill 文档索引：只读引用 Trae/system 的 SKILL.md，用于 Chat 检索注入。
    skill_reference_index = SkillReferenceIndex.from_default_locations()
    # 任务管理器串联执行器/存储/事件总线
    task_manager = TaskManager(
        template_loader=template_loader,
        workspace_manager=workspace_manager,
        llm_provider=provider,
        event_bus=event_bus,
        skill_registry=skill_registry,
        agent_max_iterations=config.runtime.agent.max_iterations,
        agent_max_retries=config.runtime.agent.max_retries,
        context_compression_config=config.runtime.context_compression,
    )
    # AI SOP 生成器
    sop_generator = SOPGenerator(provider)

    # 把共享依赖存入 app.state，供各 router 通过 request.app.state 访问
    app.state.config = config
    app.state.llm_provider = provider
    app.state.template_loader = template_loader
    app.state.workspace_manager = workspace_manager
    app.state.session_manager = session_manager
    app.state.event_bus = event_bus
    app.state.conn_manager = conn_manager
    app.state.skill_registry = skill_registry
    app.state.custom_skill_load_result = custom_skill_load_result
    app.state.skill_reference_index = skill_reference_index
    app.state.task_manager = task_manager
    app.state.sop_generator = sop_generator

    # 打通 EventBus -> WebSocket：通配订阅一个回调，把事件转发给对应 task 的连接。
    # 注意 subscribe_all 的回调仅接收 event 字典（不含独立 task_id），
    # 而事件字典本身携带 task_id 字段，据此定向转发。
    def _relay(event: dict) -> None:
        """把总线事件调度到对应 task 的 WebSocket 连接（同步转异步）。"""
        # 从事件中取出目标 task_id，缺失则不转发
        task_id = event.get("task_id")
        if not task_id:
            return
        # send_to_task 是协程，需在运行中的事件循环里调度为后台任务
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # 无运行中的事件循环（极少见）时静默跳过，不影响事件落盘
            return
        loop.create_task(conn_manager.send_to_task(task_id, event))

    # 注册通配订阅
    event_bus.subscribe_all(_relay)

    # 挂载 REST 路由（须在静态资源之前）
    app.include_router(chat_api.router)
    app.include_router(sessions_api.router)
    app.include_router(sop_sessions_api.router)
    app.include_router(sops_api.router)
    app.include_router(tasks_api.router)
    app.include_router(skills_api.router)
    app.include_router(config_api.router)

    @app.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket) -> None:
        """WebSocket 端点：按 task_id 订阅事件，先回放历史事件再保持连接。"""
        # 读取 query 参数 task_id（缺省为空字符串频道）
        task_id = websocket.query_params.get("task_id", "")
        # 接受连接并纳入连接管理器
        await conn_manager.connect(websocket, task_id)
        # 网络边界：客户端断开时抛 WebSocketDisconnect
        try:
            # 连接建立后回放已有事件，便于后连上的客户端补齐进度
            workspace = workspace_manager.get(task_id)
            if workspace is not None:
                for event in workspace.event_log.read_all():
                    await websocket.send_json(event)
            # 保持连接：持续接收并忽略客户端消息
            while True:
                await websocket.receive_text()
        except WebSocketDisconnect:
            # 断开时从连接管理器移除
            conn_manager.disconnect(websocket, task_id)

    # 注册默认流式对话端点（点对点流式，复用同一 WebSocket 栈）
    _register_chat_ws(app)

    # 静态托管兜底：前端已构建则挂载 dist，否则提供提示首页
    if (_WEB_DIST / "index.html").exists():
        app.mount("/", StaticFiles(directory=str(_WEB_DIST), html=True), name="web")
    else:

        @app.get("/", response_class=HTMLResponse)
        def index() -> str:
            """前端未构建时的兜底首页。"""
            return _FALLBACK_HTML

    return app
