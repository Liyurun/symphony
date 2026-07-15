"""Workspace 与 WorkspaceManager：任务级文件工作区管理。

每个任务对应磁盘上的一个目录，内含 events.jsonl（事件真相来源）、
traces.jsonl（LLM 调试轨迹）、state.json（状态快照）、meta.json（任务元信息），
以及 inputs/、outputs/ 两个子目录。WorkspaceManager 负责工作区的创建、
列举、获取与删除。
"""

import json
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field

from symphony.storage.eventlog import EventLog
from symphony.storage.trace import TraceLog


class TaskMeta(BaseModel):
    """任务元信息，持久化到 meta.json。"""

    # 任务唯一 id
    task_id: str
    # 关联的 SOP 定义 id
    sop_id: str
    # SOP 名称（冗余保存，便于列表展示）
    sop_name: str = ""
    # 任务状态，默认为运行中
    status: str = "running"
    # 任务创建时间的 ISO 字符串
    created_at: str
    # 任务启动时的初始变量
    variables: dict = Field(default_factory=dict)
    # 当前执行到的节点 id
    current_node: Optional[str] = None
    # 失败时的错误信息
    error: Optional[str] = None


class Workspace:
    """单个任务的文件工作区。"""

    def __init__(self, root, task_id):
        """初始化工作区目录结构。

        root 为工作区根目录，实际任务目录为 root/task_id。
        """
        # 任务目录路径
        self.root = Path(root) / task_id
        # 任务 id
        self.task_id = task_id
        # 创建任务目录，已存在则忽略
        self.root.mkdir(parents=True, exist_ok=True)
        # 创建输入子目录
        (self.root / "inputs").mkdir(exist_ok=True)
        # 创建输出子目录
        (self.root / "outputs").mkdir(exist_ok=True)
        # 事件日志（真相来源）
        self.event_log = EventLog(self.events_path)
        # LLM 调试轨迹日志
        self.trace_log = TraceLog(self.traces_path)

    @property
    def events_path(self) -> Path:
        """事件日志文件路径。"""
        return self.root / "events.jsonl"

    @property
    def traces_path(self) -> Path:
        """LLM 轨迹日志文件路径。"""
        return self.root / "traces.jsonl"

    @property
    def state_path(self) -> Path:
        """状态快照文件路径。"""
        return self.root / "state.json"

    @property
    def meta_path(self) -> Path:
        """任务元信息文件路径。"""
        return self.root / "meta.json"

    def subflow_dir(self, parent_node_id: str) -> Path:
        """返回指定 composite 父节点的子流程目录，并确保基础目录存在。"""
        # 子流程数据隔离在任务目录下的 subflows/<parent_node_id> 中
        path = self.root / "subflows" / parent_node_id
        # 创建子流程目录
        path.mkdir(parents=True, exist_ok=True)
        # 创建子节点输入快照目录
        (path / "subnode_inputs").mkdir(exist_ok=True)
        # 创建子节点输出快照目录
        (path / "subnode_outputs").mkdir(exist_ok=True)
        return path

    def save_meta(self, meta: TaskMeta):
        """将任务元信息写入 meta.json。"""
        # 序列化为带缩进的 JSON 文本
        text = json.dumps(meta.model_dump(), indent=2, ensure_ascii=False, default=str)
        # 写入文件
        self.meta_path.write_text(text, encoding="utf-8")

    def load_meta(self) -> Optional[TaskMeta]:
        """读取 meta.json 并构造 TaskMeta；不存在返回 None。"""
        # 文件不存在则返回 None
        if not self.meta_path.exists():
            return None
        # 读取并解析为 TaskMeta
        data = json.loads(self.meta_path.read_text(encoding="utf-8"))
        return TaskMeta(**data)

    def save_state(self, state: dict):
        """将状态快照写入 state.json。"""
        # 序列化为带缩进的 JSON 文本
        text = json.dumps(state, indent=2, ensure_ascii=False, default=str)
        # 写入文件
        self.state_path.write_text(text, encoding="utf-8")

    def load_state(self) -> Optional[dict]:
        """读取 state.json；不存在返回 None。"""
        # 文件不存在则返回 None
        if not self.state_path.exists():
            return None
        # 读取并解析为字典
        return json.loads(self.state_path.read_text(encoding="utf-8"))

    def save_subflow_draft(self, parent_node_id: str, draft: dict) -> None:
        """保存指定 composite 父节点的子流程草案。"""
        # 子流程草案文件路径
        path = self.subflow_dir(parent_node_id) / "draft.json"
        # 写入格式化 JSON，保留中文内容
        path.write_text(json.dumps(draft, indent=2, ensure_ascii=False, default=str), encoding="utf-8")

    def load_subflow_draft(self, parent_node_id: str) -> Optional[dict]:
        """读取指定 composite 父节点的子流程草案；不存在返回 None。"""
        # 子流程草案文件路径
        path = self.subflow_dir(parent_node_id) / "draft.json"
        # 文件不存在则返回 None
        if not path.exists():
            return None
        # 读取并解析为字典
        return json.loads(path.read_text(encoding="utf-8"))

    def save_subflow_state(self, parent_node_id: str, state: dict) -> None:
        """保存指定 composite 父节点的子流程运行状态快照。"""
        # 子流程状态文件路径
        path = self.subflow_dir(parent_node_id) / "state.json"
        # 写入格式化 JSON，保留中文内容
        path.write_text(json.dumps(state, indent=2, ensure_ascii=False, default=str), encoding="utf-8")

    def load_subflow_state(self, parent_node_id: str) -> Optional[dict]:
        """读取指定 composite 父节点的子流程运行状态；不存在返回 None。"""
        # 子流程状态文件路径
        path = self.subflow_dir(parent_node_id) / "state.json"
        # 文件不存在则返回 None
        if not path.exists():
            return None
        # 读取并解析为字典
        return json.loads(path.read_text(encoding="utf-8"))

    def append_subflow_event(self, parent_node_id: str, event: dict) -> None:
        """追加写入指定 composite 父节点的子流程事件日志。"""
        # 子流程事件日志路径
        path = self.subflow_dir(parent_node_id) / "events.jsonl"
        # 以 JSONL 形式追加一行事件
        with path.open("a", encoding="utf-8") as file:
            file.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")

    def append_subflow_trace(self, parent_node_id: str, trace: dict) -> None:
        """追加写入指定 composite 父节点的子流程调试轨迹。"""
        # 子流程轨迹日志路径
        path = self.subflow_dir(parent_node_id) / "traces.jsonl"
        # 以 JSONL 形式追加一行轨迹
        with path.open("a", encoding="utf-8") as file:
            file.write(json.dumps(trace, ensure_ascii=False, default=str) + "\n")


class WorkspaceManager:
    """管理所有任务工作区的根目录。"""

    def __init__(self, root):
        """初始化根目录并确保其存在。"""
        # 展开用户目录（如 ~）后作为根目录
        self.root = Path(root).expanduser()
        # 创建根目录，已存在则忽略
        self.root.mkdir(parents=True, exist_ok=True)

    def _gen_task_id(self, sop_id) -> str:
        """生成任务 id：sop_id-日期-随机串。"""
        # 组合 sop_id、当天日期与 8 位随机十六进制
        return f"{sop_id}-{datetime.now().strftime('%Y%m%d')}-{uuid.uuid4().hex[:8]}"

    def create(self, sop_id, variables, task_id=None, sop_name="") -> Workspace:
        """创建一个新任务工作区并写入初始元信息。"""
        # task_id 缺失时自动生成
        if task_id is None:
            task_id = self._gen_task_id(sop_id)
        # 构造工作区目录结构
        workspace = Workspace(self.root, task_id)
        # 组装初始任务元信息
        meta = TaskMeta(
            task_id=task_id,
            sop_id=sop_id,
            sop_name=sop_name,
            status="running",
            created_at=datetime.now(timezone.utc).isoformat(),
            variables=variables,
        )
        # 写入 meta.json
        workspace.save_meta(meta)
        return workspace

    def get(self, task_id) -> Optional[Workspace]:
        """按 task_id 获取工作区；目录不存在返回 None。"""
        # 任务目录不存在则返回 None
        if not (self.root / task_id).is_dir():
            return None
        # 目录存在则重建 Workspace 视图
        return Workspace(self.root, task_id)

    def list_tasks(self) -> list[dict]:
        """列举所有任务的元信息字典，按创建时间倒序排列。"""
        # 收集所有含 meta.json 的子目录的元信息
        tasks = []
        for child in self.root.iterdir():
            # 跳过非目录项
            if not child.is_dir():
                continue
            # 定位该任务的 meta.json
            meta_file = child / "meta.json"
            # 无元信息则跳过
            if not meta_file.exists():
                continue
            # 读取并解析元信息
            data = json.loads(meta_file.read_text(encoding="utf-8"))
            tasks.append(data)
        # 按 created_at 倒序排序（新任务在前）
        tasks.sort(key=lambda item: item.get("created_at", ""), reverse=True)
        return tasks

    def delete(self, task_id) -> bool:
        """删除指定任务目录；成功返回 True，目录不存在返回 False。"""
        # 目标任务目录
        target = self.root / task_id
        # 目录不存在则返回 False
        if not target.is_dir():
            return False
        # 递归删除整个任务目录
        shutil.rmtree(target)
        return True
