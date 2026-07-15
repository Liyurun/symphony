"""统一会话日志存储。

Chat 与 SOP 都以 session 作为产品层身份。Chat 会话直接持久化
transcript/events/traces/interactions；SOP 会话保存 task 引用，执行细节继续读取
既有 task workspace。
"""

import json
import uuid
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Optional

from pydantic import BaseModel


def now_iso() -> str:
    """返回当前 UTC 时间。"""
    return datetime.now(timezone.utc).isoformat()


class SessionType(str, Enum):
    """会话类型。"""

    CHAT = "chat"
    SOP = "sop"


class SessionStatus(str, Enum):
    """会话状态。"""

    RUNNING = "running"
    WAITING_INPUT = "waiting_input"
    COMPLETED = "completed"
    FAILED = "failed"


class SessionMeta(BaseModel):
    """会话元信息。"""

    session_id: str
    type: SessionType
    title: str
    status: SessionStatus
    created_at: str
    updated_at: str
    task_id: Optional[str] = None
    sop_id: Optional[str] = None
    source: str = "unknown"
    error: Optional[str] = None


class SessionLog:
    """单个 session 目录下的 JSON/JSONL 文件读写。"""

    def __init__(self, root):
        """初始化 session 目录。"""
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    @property
    def meta_path(self) -> Path:
        """元信息路径。"""
        return self.root / "meta.json"

    @property
    def transcript_path(self) -> Path:
        """用户可见对话记录路径。"""
        return self.root / "transcript.jsonl"

    @property
    def events_path(self) -> Path:
        """事件日志路径。"""
        return self.root / "events.jsonl"

    @property
    def traces_path(self) -> Path:
        """LLM Trace 路径。"""
        return self.root / "traces.jsonl"

    @property
    def interactions_path(self) -> Path:
        """反问与回答路径。"""
        return self.root / "interactions.jsonl"

    @property
    def task_ref_path(self) -> Path:
        """SOP session 指向 task workspace 的引用。"""
        return self.root / "task_ref.json"

    def save_meta(
        self,
        session_id: str,
        type: SessionType,
        title: str,
        status: SessionStatus,
        source: str,
        task_id: str | None = None,
        sop_id: str | None = None,
        error: str | None = None,
        created_at: str | None = None,
        updated_at: str | None = None,
    ) -> SessionMeta:
        """写入 meta.json 并返回模型。"""
        timestamp = now_iso()
        meta = SessionMeta(
            session_id=session_id,
            type=type,
            title=title,
            status=status,
            created_at=created_at or timestamp,
            updated_at=updated_at or timestamp,
            task_id=task_id,
            sop_id=sop_id,
            source=source,
            error=error,
        )
        self.meta_path.write_text(
            json.dumps(meta.model_dump(mode="json"), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        if task_id:
            self.task_ref_path.write_text(
                json.dumps({"task_id": task_id, "sop_id": sop_id}, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        return meta

    def load_meta(self) -> dict:
        """读取 meta.json；不存在时返回空字典。"""
        if not self.meta_path.exists():
            return {}
        return json.loads(self.meta_path.read_text(encoding="utf-8"))

    def update_meta(self, **patch) -> dict:
        """局部更新 meta.json。"""
        data = self.load_meta()
        data.update(patch)
        data["updated_at"] = now_iso()
        self.meta_path.write_text(
            json.dumps(data, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        return data

    def _append_jsonl(self, path: Path, record: dict) -> None:
        """追加一行 JSONL。"""
        with path.open("a", encoding="utf-8") as file:
            file.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")

    def _read_jsonl(self, path: Path) -> list[dict]:
        """读取 JSONL；跳过空行。"""
        if not path.exists():
            return []
        rows: list[dict] = []
        with path.open("r", encoding="utf-8") as file:
            for raw in file:
                line = raw.strip()
                if not line:
                    continue
                rows.append(json.loads(line))
        return rows

    def append_transcript(self, record: dict) -> None:
        """追加用户可见对话记录。"""
        self._append_jsonl(self.transcript_path, record)

    def read_transcript(self) -> list[dict]:
        """读取 transcript。"""
        return self._read_jsonl(self.transcript_path)

    def append_event(self, record: dict) -> None:
        """追加会话事件。"""
        self._append_jsonl(self.events_path, record)

    def read_events(self) -> list[dict]:
        """读取会话事件。"""
        return self._read_jsonl(self.events_path)

    def append_trace(self, record: dict) -> None:
        """追加 LLM Trace。"""
        self._append_jsonl(self.traces_path, record)

    def read_traces(self) -> list[dict]:
        """读取 LLM Trace。"""
        return self._read_jsonl(self.traces_path)

    def append_interaction(self, record: dict) -> None:
        """追加反问或回答。"""
        self._append_jsonl(self.interactions_path, record)

    def read_interactions(self) -> list[dict]:
        """读取反问记录。"""
        return self._read_jsonl(self.interactions_path)


class SessionManager:
    """管理全部 session 目录。"""

    def __init__(self, root):
        """初始化 session 根目录。"""
        self.root = Path(root).expanduser()
        self.root.mkdir(parents=True, exist_ok=True)

    def _gen_id(self, type: SessionType) -> str:
        """生成 session id。"""
        return f"{type.value}-{datetime.now().strftime('%Y%m%d')}-{uuid.uuid4().hex[:8]}"

    def _create(
        self,
        type: SessionType,
        title: str,
        source: str,
        task_id: str | None = None,
        sop_id: str | None = None,
    ) -> SessionMeta:
        """创建通用 session。"""
        session_id = self._gen_id(type)
        log = SessionLog(self.root / session_id)
        return log.save_meta(
            session_id=session_id,
            type=type,
            title=title,
            status=SessionStatus.RUNNING,
            source=source,
            task_id=task_id,
            sop_id=sop_id,
        )

    def create_chat(self, title: str, source: str = "unknown") -> SessionMeta:
        """创建 Chat session。"""
        return self._create(SessionType.CHAT, title=title, source=source)

    def create_sop(
        self,
        title: str,
        sop_id: str,
        task_id: str,
        source: str = "unknown",
    ) -> SessionMeta:
        """创建 SOP session。"""
        return self._create(
            SessionType.SOP,
            title=title,
            source=source,
            task_id=task_id,
            sop_id=sop_id,
        )

    def get(self, session_id: str) -> Optional[SessionLog]:
        """按 id 获取 session；不存在返回 None。"""
        path = self.root / session_id
        if not path.is_dir():
            return None
        return SessionLog(path)

    def require(self, session_id: str) -> SessionLog:
        """获取 session；不存在抛 ValueError。"""
        log = self.get(session_id)
        if log is None:
            raise ValueError(f"Session not found: {session_id}")
        return log

    def list_sessions(self) -> list[dict]:
        """列举 session meta，按 updated_at 倒序。"""
        items: list[dict] = []
        for child in self.root.iterdir():
            if not child.is_dir():
                continue
            meta_file = child / "meta.json"
            if not meta_file.exists():
                continue
            items.append(json.loads(meta_file.read_text(encoding="utf-8")))
        items.sort(key=lambda item: item.get("updated_at", ""), reverse=True)
        return items

    def update_status(
        self,
        session_id: str,
        status: SessionStatus,
        error: str | None = None,
    ) -> dict:
        """更新 session 状态。"""
        patch: dict = {"status": status.value}
        if error is not None:
            patch["error"] = error
        return self.require(session_id).update_meta(**patch)
