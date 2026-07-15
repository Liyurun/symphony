"""TaskManager（编排 executor↔storage↔eventbus）的集成测试。

用 AsyncMock 模拟 LLMProvider，临时目录承载 TemplateLoader/WorkspaceManager，
配合真实 EventBus，验证 start_task 能启动后台任务、把事件落盘并发布、
最终把 meta 状态流转为 completed。
"""

import pytest
from unittest.mock import AsyncMock

from symphony.ai.schema import LLMResponse, Message, Role, Usage
from symphony.config import ContextCompressionConfig
from symphony.server import manager as manager_module
from symphony.server.eventbus import EventBus
from symphony.server.manager import TaskManager
from symphony.storage.workspace import WorkspaceManager
from symphony.workflow.models import Edge, Node, NodeType, SOPTemplate
from symphony.workflow.template import TemplateLoader


def _make_response(content=None):
    """构造一条包含单个 assistant choice 的 LLMResponse。"""
    msg = Message(role=Role.ASSISTANT, content=content)
    return LLMResponse(id="resp-1", choices=[msg], usage=Usage(), model="test-model")


def _agent_node(node_id, field):
    """构造一个要求 output 含指定字段的 agent 节点。"""
    return Node(
        id=node_id,
        name=node_id,
        type=NodeType.AGENT,
        prompt="处理任务",
        output_schema={
            "type": "object",
            "properties": {field: {"type": "string"}},
            "required": [field],
        },
    )


def _linear_template():
    """构造 step1 -> step2 的两节点线性 agent SOP。"""
    return SOPTemplate(
        id="sop-1",
        name="线性 SOP",
        nodes=[_agent_node("step1", "a"), _agent_node("step2", "b")],
        edges=[Edge(from_node="step1", to="step2")],
        entry_node="step1",
    )


@pytest.fixture
def mock_provider():
    """构造带 model 属性、chat 返回合法 JSON 的 AsyncMock provider。"""
    p = AsyncMock()
    p.model = "test-model"
    # 给足够多次合法响应，避免 side_effect 耗尽
    p.chat.side_effect = [
        _make_response(content='{"a":"1"}'),
        _make_response(content='{"b":"2"}'),
        _make_response(content='{"a":"1"}'),
        _make_response(content='{"b":"2"}'),
    ]
    return p


@pytest.fixture
def task_manager(tmp_path, mock_provider):
    """组装依赖真实存储与事件总线的 TaskManager。"""
    # 模板加载器：预存一个线性 SOP
    loader = TemplateLoader(tmp_path / "templates")
    loader.save(_linear_template())
    # 工作区管理器与事件总线
    workspaces = WorkspaceManager(tmp_path / "workspaces")
    bus = EventBus()
    manager = TaskManager(
        template_loader=loader,
        workspace_manager=workspaces,
        llm_provider=mock_provider,
        event_bus=bus,
    )
    # 一并暴露 bus/workspaces 供断言使用
    return manager, bus, workspaces


async def test_start_task_completes(task_manager):
    """start_task 应启动任务并跑完，事件落盘、meta 变为 completed、总线收到关键事件。"""
    manager, bus, workspaces = task_manager
    # 通过通配订阅收集所有发布事件
    published: list[dict] = []
    bus.subscribe_all(published.append)
    # 启动任务
    task_id = await manager.start_task("sop-1", {})
    # 等待后台任务结束
    await manager._tasks[task_id]

    # 工作区应有落盘事件
    workspace = workspaces.get(task_id)
    events = workspace.event_log.read_all()
    assert len(events) > 0
    # meta 状态应流转为 completed
    meta = workspace.load_meta()
    assert meta.status == "completed"
    # 总线应收到过 task_started 与 task_completed
    types = [e.get("type") for e in published]
    assert "task_started" in types
    assert "task_completed" in types


async def test_start_task_unknown_sop_raises(task_manager):
    """未知 sop_id 应抛 ValueError。"""
    manager, _, _ = task_manager
    with pytest.raises(ValueError):
        await manager.start_task("not-exist", {})


async def test_task_manager_passes_agent_runtime_config_to_executor(
    tmp_path, mock_provider, monkeypatch
):
    """TaskManager 应把配置化 Agent 参数传给 WorkflowExecutor。"""
    captured = {}

    class CapturingExecutor:
        """截获 WorkflowExecutor 构造参数。"""

        def __init__(self, **kwargs):
            captured["executor_kwargs"] = kwargs

    loader = TemplateLoader(tmp_path / "templates")
    loader.save(_linear_template())
    workspaces = WorkspaceManager(tmp_path / "workspaces")
    compression_config = ContextCompressionConfig(
        enabled=False,
        max_prompt_chars=1234,
    )
    manager = TaskManager(
        template_loader=loader,
        workspace_manager=workspaces,
        llm_provider=mock_provider,
        event_bus=EventBus(),
        agent_max_iterations=7,
        agent_max_retries=2,
        context_compression_config=compression_config,
    )
    manager._spawn_run = lambda task_id, executor, workspace: None
    monkeypatch.setattr(manager_module, "WorkflowExecutor", CapturingExecutor)

    task_id = await manager.start_task("sop-1", {"topic": "hello"})

    assert task_id
    kwargs = captured["executor_kwargs"]
    assert kwargs["max_iterations"] == 7
    assert kwargs["max_retries"] == 2
    assert kwargs["context_compression_config"] is compression_config


async def test_get_snapshot_from_memory(task_manager):
    """任务完成后 get_snapshot 应返回执行器内存快照。"""
    manager, _, _ = task_manager
    # 启动并等待完成
    task_id = await manager.start_task("sop-1", {})
    await manager._tasks[task_id]
    # 快照应包含 task_id 与节点状态
    snapshot = manager.get_snapshot(task_id)
    assert snapshot is not None
    assert snapshot["task_id"] == task_id
    assert "nodes" in snapshot


async def test_list_tasks(task_manager):
    """list_tasks 应返回已创建任务的元信息列表。"""
    manager, _, _ = task_manager
    # 启动并等待完成
    task_id = await manager.start_task("sop-1", {})
    await manager._tasks[task_id]
    # 列表中应能找到该任务
    tasks = manager.list_tasks()
    assert any(item.get("task_id") == task_id for item in tasks)


async def test_rerun_node_saves_snapshot_and_respawns(task_manager):
    """TaskManager.rerun_node 应保存重跑快照并重新调度执行器。"""
    manager, _, workspaces = task_manager
    task_id = await manager.start_task("sop-1", {})
    await manager._tasks[task_id]

    spawned = []

    def fake_spawn(spawn_task_id, executor, workspace):
        """测试中截获重新调度，避免后台 run 改写待重跑快照。"""
        spawned.append((spawn_task_id, executor, workspace))

    manager._spawn_run = fake_spawn

    result = await manager.rerun_node(
        task_id,
        "step1",
        supplemental_instruction="请更关注价格",
    )

    assert result == {
        "ok": True,
        "attempt_no": 2,
        "invalidated_node_ids": ["step1", "step2"],
    }
    assert spawned and spawned[0][0] == task_id

    workspace = workspaces.get(task_id)
    snapshot = workspace.load_state()
    assert snapshot["nodes"]["step1"]["status"] == "pending"
    assert snapshot["nodes"]["step1"]["prompt_override"] == "请更关注价格"
    assert snapshot["nodes"]["step2"]["status"] == "pending"
    assert snapshot["nodes"]["step2"]["stale"] is True
    assert snapshot["nodes"]["step2"]["stale_reason"] == "upstream_rerun"

    event_types = [event["type"] for event in workspace.event_log.read_all()]
    assert "node_retry_requested" in event_types
    assert "node_supplemental_instruction_added" in event_types
    assert "downstream_invalidated" in event_types
    assert "node_marked_stale" in event_types
