"""Workspace 与 WorkspaceManager 单元测试。

验证任务工作区的创建、列举、状态读写往返，以及获取与删除等行为。
"""

from pathlib import Path

from symphony.storage import WorkspaceManager
from symphony.workflow.models import Node, NodeType, SubFlowDraft


def test_workspace_create_and_list(tmp_path: Path):
    """创建工作区后应生成 meta.json，且 list_tasks 能列出该任务。"""
    # 以临时目录作为工作区根目录
    manager = WorkspaceManager(tmp_path)
    # 创建一个新任务工作区
    ws = manager.create(sop_id="sop1", variables={"a": 1})

    # 断言：生成了 task_id
    assert ws.task_id
    # 断言：meta.json 已写入
    assert ws.meta_path.exists()

    # 列举所有任务
    tasks = manager.list_tasks()

    # 断言：仅有一个任务
    assert len(tasks) == 1
    # 断言：列表中含刚创建的 task_id
    assert tasks[0]["task_id"] == ws.task_id


def test_workspace_state_roundtrip(tmp_path: Path):
    """保存状态后再读取应得到相同内容。"""
    # 创建工作区
    manager = WorkspaceManager(tmp_path)
    ws = manager.create(sop_id="sop1", variables={})

    # 保存状态快照
    ws.save_state({"x": 1})

    # 断言：读取到的状态与保存一致
    assert ws.load_state() == {"x": 1}


def test_workspace_get_and_delete(tmp_path: Path):
    """create 后 get 应非 None；delete 返回 True 后再 get 应为 None。"""
    # 创建工作区
    manager = WorkspaceManager(tmp_path)
    ws = manager.create(sop_id="sop1", variables={})

    # 断言：可按 task_id 获取到工作区
    assert manager.get(ws.task_id) is not None
    # 断言：删除成功返回 True
    assert manager.delete(ws.task_id) is True
    # 断言：删除后再获取返回 None
    assert manager.get(ws.task_id) is None


def test_subflow_draft_and_state_persistence(tmp_path: Path):
    """子流程草案和运行状态应持久化到父任务工作区的 subflows 目录。"""
    # 创建固定 task_id 的工作区，便于断言目录结构
    manager = WorkspaceManager(tmp_path)
    workspace = manager.create("sop-1", {}, task_id="task-1", sop_name="SOP")
    # 构造一个包含子节点的子流程草案
    draft = SubFlowDraft(
        parent_node_id="develop",
        draft_nodes=[Node(id="table_a", name="表A", type=NodeType.AGENT)],
        draft_edges=[],
        created_at="2026-07-06T00:00:00Z",
    )
    # 构造子流程运行状态快照
    state = {
        "parent_node_id": "develop",
        "nodes": {"table_a": {"status": "pending"}},
    }

    # 保存草案与状态
    workspace.save_subflow_draft("develop", draft.model_dump(mode="json"))
    workspace.save_subflow_state("develop", state)

    # 断言：读取内容与保存内容一致
    assert workspace.load_subflow_draft("develop")["parent_node_id"] == "develop"
    assert workspace.load_subflow_state("develop")["nodes"]["table_a"]["status"] == "pending"
    # 断言：文件落在当前 Workspace 既有 root 字段对应的任务目录下
    assert (workspace.root / "subflows" / "develop" / "draft.json").exists()
    assert (workspace.root / "subflows" / "develop" / "state.json").exists()
