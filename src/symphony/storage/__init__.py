"""Symphony 存储层。

采用纯文件存储 + 事件溯源：events.jsonl 为真相来源，state.json 为状态快照，
traces.jsonl 记录完整的 LLM 调试日志。此处导出对外使用的核心类型。
"""

from symphony.storage.eventlog import EventLog
from symphony.storage.session import (
    SessionLog,
    SessionManager,
    SessionMeta,
    SessionStatus,
    SessionType,
)
from symphony.storage.trace import TraceLog
from symphony.storage.workspace import TaskMeta, Workspace, WorkspaceManager

__all__ = [
    "EventLog",
    "Workspace",
    "WorkspaceManager",
    "TaskMeta",
    "TraceLog",
    "SessionLog",
    "SessionManager",
    "SessionMeta",
    "SessionStatus",
    "SessionType",
]
