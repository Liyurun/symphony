"""Symphony REST API 路由包。

聚合 chat、SOP、任务、技能与配置 APIRouter，供 create_app 统一挂载。
各 router 通过 request.app.state 访问共享依赖（task_manager、template_loader、
sop_generator、config 等），保持与应用装配的解耦。
"""

from symphony.server.api import chat, config, sessions, skills, sop_sessions, sops, tasks

__all__ = ["chat", "config", "sessions", "skills", "sop_sessions", "sops", "tasks"]
