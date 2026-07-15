"""WorkflowExecutor（线性 DAG 执行器）的单元测试。

用 unittest.mock.AsyncMock 模拟 LLMProvider，覆盖执行器的核心行为：
1. 线性 agent SOP 的顺利执行；
2. 拓扑排序拒绝分支结构；
3. human 节点触发暂停等待人工；
4. skill 节点直接调用技能并校验输出；
5. 类型化 I/O 校验——缺输入/输出不符时标记失败；
6. 类型化 I/O 跨节点字段传递。
"""

import pytest
from unittest.mock import AsyncMock

from symphony.agent.events import NodeStatus
from symphony.ai.schema import LLMResponse, Message, Role, Usage
from symphony.config import ContextCompressionConfig
from symphony.skills.base import Skill
from symphony.skills.registry import SkillRegistry
from symphony.workflow import executor as executor_module
from symphony.workflow.executor import NodeState, WorkflowExecutor
from symphony.workflow.models import Edge, IOField, IOType, Node, NodeType, SOPTemplate


class EchoSkill(Skill):
    """回显技能，把入参原样放到 echo 字段返回一个 dict。"""

    name = "echo"
    description = "Echo"
    input_schema = {"type": "object"}
    output_schema = {"type": "object"}

    async def execute(self, args, context):
        """回显传入的参数。"""
        return {"echo": args}


class UpperSkill(Skill):
    """把入参中的 text 转成大写，用于区分多个已注册技能。"""

    name = "upper"
    description = "Uppercase"
    input_schema = {"type": "object"}
    output_schema = {"type": "object"}

    async def execute(self, args, context):
        """返回大写后的 text 字段。"""
        return {"upper": str(args.get("text", "")).upper()}


@pytest.fixture
def mock_provider():
    """构造一个带 model 属性的 AsyncMock provider。"""
    p = AsyncMock()
    p.model = "test-model"
    return p


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
        output_schema={"type": "object", "properties": {field: {"type": "string"}}, "required": [field]},
    )


def _linear_template():
    """构造 step1 -> step2 的两节点线性 agent SOP。"""
    return SOPTemplate(
        id="sop-1",
        name="linear",
        nodes=[_agent_node("step1", "a"), _agent_node("step2", "b")],
        edges=[Edge(from_node="step1", to="step2")],
        entry_node="step1",
    )


def test_node_state_tracks_attempt_history_and_stale_state():
    """NodeState 应保存 attempt 历史、stale 状态和 pending interaction。"""
    state = NodeState(node_id="n1")

    assert state.attempt_history == []
    assert state.stale is False
    assert state.stale_reason is None
    assert state.pending_interaction_id is None


def test_rerun_node_with_instruction_invalidates_downstream(mock_provider):
    """主节点补充指令重跑应重置当前节点并标记下游 stale。"""
    events = []
    executor = WorkflowExecutor(
        task_id="t1",
        template=_linear_template(),
        variables={"topic": "hello"},
        llm_provider=mock_provider,
        skill_registry=SkillRegistry(),
        on_event=events.append,
    )
    executor.variables["step1"] = {"a": "old"}
    executor.variables["step2"] = {"b": "old"}
    executor.node_states["step1"].status = NodeStatus.COMPLETED
    executor.node_states["step1"].output = {"a": "old"}
    executor.node_states["step1"].attempts = 1
    executor.node_states["step2"].status = NodeStatus.COMPLETED
    executor.node_states["step2"].output = {"b": "old"}

    invalidated = executor.rerun_node_with_instruction(
        "step1",
        supplemental_instruction="请更关注价格",
    )

    assert invalidated == ["step1", "step2"]
    assert executor.node_states["step1"].status == NodeStatus.PENDING
    assert executor.node_states["step1"].prompt_override == "请更关注价格"
    assert executor.node_states["step1"].attempt_history[-1] == {
        "attempt_no": 2,
        "trigger": "user_correction",
        "supplemental_instruction": "请更关注价格",
        "status": "pending",
    }
    assert executor.node_states["step1"].stale is False
    assert executor.node_states["step2"].status == NodeStatus.PENDING
    assert executor.node_states["step2"].stale is True
    assert executor.node_states["step2"].stale_reason == "upstream_rerun"
    assert "step1" not in executor.variables
    assert "step2" not in executor.variables

    event_types = [event.to_dict()["type"] for event in events]
    assert event_types == [
        "node_retry_requested",
        "node_supplemental_instruction_added",
        "downstream_invalidated",
        "node_marked_stale",
    ]
    assert events[0].to_dict()["invalidated_node_ids"] == ["step1", "step2"]
    assert events[2].to_dict()["invalidated_node_ids"] == ["step2"]


async def test_executor_records_successful_attempt_lifecycle(mock_provider):
    """节点成功执行应记录 attempt started/completed 事件和完整历史。"""
    mock_provider.chat.side_effect = [
        _make_response(content='{"a":"1"}'),
        _make_response(content='{"b":"2"}'),
    ]
    events = []
    executor = WorkflowExecutor(
        task_id="t1",
        template=_linear_template(),
        variables={"topic": "hello"},
        llm_provider=mock_provider,
        skill_registry=SkillRegistry(),
        on_event=events.append,
    )

    snapshot = await executor.run()

    step1_history = snapshot["nodes"]["step1"]["attempt_history"]
    assert step1_history == [
        {
            "attempt_no": 1,
            "trigger": "normal",
            "supplemental_instruction": None,
            "status": "completed",
            "input": {"topic": "hello"},
            "output": {"a": "1"},
            "error": None,
        }
    ]
    assert snapshot["nodes"]["step1"]["stale"] is False
    attempt_events = [
        event.to_dict()
        for event in events
        if event.to_dict().get("node_id") == "step1" and event.to_dict().get("type", "").startswith("node_attempt_")
    ]
    assert [event["type"] for event in attempt_events] == ["node_attempt_started", "node_attempt_completed"]
    assert attempt_events[0]["attempt_no"] == 1
    assert attempt_events[0]["trigger"] == "normal"
    assert attempt_events[1]["output"] == {"a": "1"}


async def test_executor_passes_agent_runtime_config(mock_provider, monkeypatch):
    """WorkflowExecutor 构造 AgentRuntime 时应透传配置化运行参数。"""
    captured = {}

    class CapturingAgentRuntime:
        """截获 AgentRuntime 构造参数并返回合法输出。"""

        def __init__(self, **kwargs):
            captured["runtime_kwargs"] = kwargs

        async def run(self, initial_input, context, prompt_override=None, reset=True):
            captured["run"] = (initial_input, context.node_id, prompt_override, reset)
            return {"a": "1"}

    monkeypatch.setattr(executor_module, "AgentRuntime", CapturingAgentRuntime)
    compression_config = ContextCompressionConfig(
        enabled=False,
        max_prompt_chars=1234,
        keep_recent_messages=5,
        min_recent_messages=2,
        summary_max_chars=321,
        max_message_chars=654,
    )
    events = []
    executor = WorkflowExecutor(
        task_id="t1",
        template=SOPTemplate(
            id="sop-1",
            name="linear",
            nodes=[_agent_node("step1", "a")],
            edges=[],
            entry_node="step1",
        ),
        variables={"topic": "hello"},
        llm_provider=mock_provider,
        skill_registry=SkillRegistry(),
        on_event=events.append,
        max_iterations=9,
        max_retries=4,
        context_compression_config=compression_config,
    )

    snapshot = await executor.run()

    assert snapshot["nodes"]["step1"]["output"] == {"a": "1"}
    kwargs = captured["runtime_kwargs"]
    assert kwargs["max_iterations"] == 9
    assert kwargs["max_retries"] == 4
    compressor = kwargs["context_compressor"]
    assert compressor.enabled is False
    assert compressor.max_prompt_chars == 1234
    assert compressor.keep_recent_messages == 5
    assert compressor.min_recent_messages == 2
    assert compressor.summary_max_chars == 321
    assert compressor.max_message_chars == 654


async def test_empty_agent_skills_exposes_all_registered_skills(mock_provider, monkeypatch):
    """agent 节点 skills 为空列表时，应把全部注册技能暴露给 AgentRuntime。"""
    captured = {}

    class CapturingAgentRuntime:
        """截获传入 AgentRuntime 的技能注册中心。"""

        def __init__(self, **kwargs):
            captured["skill_names"] = sorted(
                skill.name for skill in kwargs["skill_registry"].list_skills()
            )

        async def run(self, initial_input, context, prompt_override=None, reset=True):
            return {"a": "1"}

    monkeypatch.setattr(executor_module, "AgentRuntime", CapturingAgentRuntime)
    registry = SkillRegistry()
    registry.register(EchoSkill())
    registry.register(UpperSkill())
    template = SOPTemplate(
        id="sop-1",
        name="all-skills",
        nodes=[
            Node(
                id="step1",
                name="step1",
                type=NodeType.AGENT,
                prompt="处理任务",
                skills=[],
                output_schema={
                    "type": "object",
                    "properties": {"a": {"type": "string"}},
                    "required": ["a"],
                },
            )
        ],
        edges=[],
        entry_node="step1",
    )
    executor = WorkflowExecutor(
        task_id="t1",
        template=template,
        variables={"topic": "hello"},
        llm_provider=mock_provider,
        skill_registry=registry,
        on_event=lambda e: None,
    )

    await executor.run()

    assert captured["skill_names"] == ["echo", "upper"]


async def test_explicit_agent_skills_still_restrict_registered_skills(mock_provider, monkeypatch):
    """agent 节点显式声明 skills 时，仍只暴露对应白名单技能。"""
    captured = {}

    class CapturingAgentRuntime:
        """截获传入 AgentRuntime 的技能注册中心。"""

        def __init__(self, **kwargs):
            captured["skill_names"] = sorted(
                skill.name for skill in kwargs["skill_registry"].list_skills()
            )

        async def run(self, initial_input, context, prompt_override=None, reset=True):
            return {"a": "1"}

    monkeypatch.setattr(executor_module, "AgentRuntime", CapturingAgentRuntime)
    registry = SkillRegistry()
    registry.register(EchoSkill())
    registry.register(UpperSkill())
    template = SOPTemplate(
        id="sop-1",
        name="explicit-skills",
        nodes=[
            Node(
                id="step1",
                name="step1",
                type=NodeType.AGENT,
                prompt="处理任务",
                skills=["echo"],
                output_schema={
                    "type": "object",
                    "properties": {"a": {"type": "string"}},
                    "required": ["a"],
                },
            )
        ],
        edges=[],
        entry_node="step1",
    )
    executor = WorkflowExecutor(
        task_id="t1",
        template=template,
        variables={"topic": "hello"},
        llm_provider=mock_provider,
        skill_registry=registry,
        on_event=lambda e: None,
    )

    await executor.run()

    assert captured["skill_names"] == ["echo"]


async def test_executor_records_failed_attempt_lifecycle(mock_provider):
    """节点失败时应把当前 attempt 标记为 failed 并发射失败事件。"""
    template = SOPTemplate(
        id="sop-missing",
        name="missing-input",
        nodes=[
            Node(
                id="n1",
                name="n1",
                type=NodeType.AGENT,
                prompt="p",
                inputs=[IOField(name="title", type=IOType.TEXT, label="标题")],
                outputs=[IOField(name="summary", type=IOType.TEXT, label="摘要")],
            )
        ],
    )
    events = []
    executor = WorkflowExecutor(
        task_id="t1",
        template=template,
        variables={},
        llm_provider=mock_provider,
        skill_registry=SkillRegistry(),
        on_event=events.append,
    )

    snapshot = await executor.run()

    attempt = snapshot["nodes"]["n1"]["attempt_history"][0]
    assert attempt["attempt_no"] == 1
    assert attempt["trigger"] == "normal"
    assert attempt["status"] == "failed"
    assert attempt["input"] is None
    assert "缺少必填输入" in attempt["error"]
    attempt_failed = [
        event.to_dict()
        for event in events
        if event.to_dict().get("node_id") == "n1" and event.to_dict().get("type") == "node_attempt_failed"
    ]
    assert len(attempt_failed) == 1
    assert "缺少必填输入" in attempt_failed[0]["error"]


async def test_executor_records_user_correction_attempt_lifecycle(mock_provider):
    """补充指令重跑应把实际 attempt 记录为 user_correction 并清除 stale。"""
    mock_provider.chat.side_effect = [
        _make_response(content='{"a":"new"}'),
        _make_response(content='{"b":"updated"}'),
    ]
    events = []
    executor = WorkflowExecutor(
        task_id="t1",
        template=_linear_template(),
        variables={"topic": "hello", "step1": {"a": "old"}, "step2": {"b": "old"}},
        llm_provider=mock_provider,
        skill_registry=SkillRegistry(),
        on_event=events.append,
    )
    executor.node_states["step1"].status = NodeStatus.COMPLETED
    executor.node_states["step1"].output = {"a": "old"}
    executor.node_states["step1"].attempts = 1
    executor.node_states["step1"].attempt_history = [
        {
            "attempt_no": 1,
            "trigger": "normal",
            "supplemental_instruction": None,
            "status": "completed",
            "input": {"topic": "hello"},
            "output": {"a": "old"},
            "error": None,
        }
    ]
    executor.node_states["step2"].status = NodeStatus.COMPLETED
    executor.node_states["step2"].output = {"b": "old"}
    executor.node_states["step2"].attempts = 1

    executor.rerun_node_with_instruction("step1", supplemental_instruction="请更关注价格")
    snapshot = await executor.run()

    step1_history = snapshot["nodes"]["step1"]["attempt_history"]
    assert len(step1_history) == 2
    assert step1_history[-1] == {
        "attempt_no": 2,
        "trigger": "user_correction",
        "supplemental_instruction": "请更关注价格",
        "status": "completed",
        "input": {"topic": "hello"},
        "output": {"a": "new"},
        "error": None,
    }
    assert snapshot["nodes"]["step1"]["stale"] is False
    assert snapshot["nodes"]["step1"]["stale_reason"] is None
    started = [
        event.to_dict()
        for event in events
        if event.to_dict().get("node_id") == "step1" and event.to_dict().get("type") == "node_attempt_started"
    ]
    assert started[-1]["attempt_no"] == 2
    assert started[-1]["trigger"] == "user_correction"


async def test_linear_execution_success(mock_provider):
    """两节点线性 agent SOP 应全部完成，并发出 task_started/task_completed。"""
    mock_provider.chat.side_effect = [
        _make_response(content='{"a":"1"}'),
        _make_response(content='{"b":"2"}'),
    ]
    events = []
    executor = WorkflowExecutor(
        task_id="t1",
        template=_linear_template(),
        variables={"topic": "hello"},
        llm_provider=mock_provider,
        skill_registry=SkillRegistry(),
        on_event=events.append,
    )

    snapshot = await executor.run()

    assert executor.node_states["step1"].status.value == "completed"
    assert executor.node_states["step2"].status.value == "completed"
    assert snapshot["nodes"]["step1"]["status"] == "completed"
    event_types = [e.type for e in events]
    assert "task_started" in event_types
    assert "task_completed" in event_types


async def test_topo_order_rejects_branching(mock_provider):
    """step1 有两条出边时，template.linear_order() 应抛 ValueError（已迁到模板层）。"""
    template = SOPTemplate(
        id="sop-branch",
        name="branch",
        nodes=[_agent_node("step1", "a"), _agent_node("step2", "b"), _agent_node("step3", "c")],
        edges=[Edge(from_node="step1", to="step2"), Edge(from_node="step1", to="step3")],
        entry_node="step1",
    )
    executor = WorkflowExecutor(
        task_id="t1",
        template=template,
        variables={},
        llm_provider=mock_provider,
        skill_registry=SkillRegistry(),
        on_event=lambda e: None,
    )

    with pytest.raises(ValueError):
        # 执行器 run() 内部调用 template.linear_order() 时会抛错
        await executor.run()


async def test_human_node_pauses(mock_provider):
    """单个 human 节点的 SOP 执行后应暂停并等待人工输入。"""
    template = SOPTemplate(
        id="sop-human",
        name="human",
        nodes=[Node(id="h1", name="h1", type=NodeType.HUMAN)],
        edges=[],
        entry_node="h1",
    )
    events = []
    executor = WorkflowExecutor(
        task_id="t1",
        template=template,
        variables={},
        llm_provider=mock_provider,
        skill_registry=SkillRegistry(),
        on_event=events.append,
    )

    await executor.run()

    assert executor.node_states["h1"].status.value == "waiting_input"
    assert executor._paused is True
    assert "node_waiting_input" in [e.type for e in events]


async def test_human_node_creates_pending_interaction(mock_provider):
    """human 节点应创建 pending interaction 并进入 waiting_input。"""
    template = SOPTemplate(
        id="sop-human-interaction",
        name="human interaction",
        nodes=[
            Node(
                id="review",
                name="人工复核",
                type=NodeType.HUMAN,
                description="请确认评审结论",
            )
        ],
        edges=[],
        entry_node="review",
    )
    events = []
    executor = WorkflowExecutor(
        task_id="t1",
        template=template,
        variables={},
        llm_provider=mock_provider,
        skill_registry=SkillRegistry(),
        on_event=events.append,
    )

    snapshot = await executor.run()

    interaction_events = [event.to_dict() for event in events if event.to_dict()["type"] == "interaction_requested"]
    assert interaction_events
    interaction = interaction_events[0]
    assert interaction["interaction_id"] == "int-t1-review-1"
    assert interaction["attempt_no"] == 1
    assert interaction["prompt"] == "请确认评审结论"
    assert snapshot["nodes"]["review"]["pending_interaction_id"] == interaction["interaction_id"]
    assert snapshot["nodes"]["review"]["status"] == "waiting_input"


async def test_skill_node_execution(mock_provider):
    """skill 节点应直接调用技能，输出通过校验后标记完成。"""
    registry = SkillRegistry()
    registry.register(EchoSkill())
    template = SOPTemplate(
        id="sop-skill",
        name="skill",
        nodes=[
            Node(
                id="s1",
                name="s1",
                type=NodeType.SKILL,
                skill_name="echo",
                output_schema={"type": "object"},
            )
        ],
        edges=[],
        entry_node="s1",
    )
    events = []
    executor = WorkflowExecutor(
        task_id="t1",
        template=template,
        variables={"k": "v"},
        llm_provider=mock_provider,
        skill_registry=registry,
        on_event=events.append,
    )

    snapshot = await executor.run()

    assert executor.node_states["s1"].status.value == "completed"
    assert executor.node_states["s1"].output == {"echo": {"k": "v"}}
    assert snapshot["nodes"]["s1"]["status"] == "completed"
    event_types = [e.type for e in events]
    assert "skill_called" in event_types
    assert "skill_returned" in event_types


async def test_typed_io_missing_input_fails(mock_provider):
    """声明了必填 typed inputs 但变量里缺失时，节点应立即失败并暂停。"""
    template = SOPTemplate(
        id="sop-missing",
        name="missing-input",
        nodes=[
            Node(
                id="n1",
                name="n1",
                type=NodeType.AGENT,
                prompt="p",
                inputs=[IOField(name="title", type=IOType.TEXT, label="标题")],
                outputs=[IOField(name="summary", type=IOType.TEXT, label="摘要")],
            )
        ],
    )
    events = []
    executor = WorkflowExecutor(
        task_id="t1",
        template=template,
        variables={},  # 注意：未提供 title
        llm_provider=mock_provider,
        skill_registry=SkillRegistry(),
        on_event=events.append,
    )

    snapshot = await executor.run()

    assert executor.node_states["n1"].status.value == "failed"
    assert "缺少必填输入" in (executor.node_states["n1"].error or "")
    assert snapshot["paused"] is True


async def test_typed_io_output_type_mismatch_fails(mock_provider):
    """agent 输出字段类型不符（声明 text 却返回 number）时，ReAct 循环重试多次后
    转为 waiting_input（已有行为），不会继续推进到下一个节点。"""
    template = SOPTemplate(
        id="sop-bad-out",
        name="bad-output",
        nodes=[
            Node(
                id="n1",
                name="n1",
                type=NodeType.AGENT,
                prompt="p",
                inputs=[IOField(name="title", type=IOType.TEXT)],
                outputs=[IOField(name="summary", type=IOType.TEXT, label="摘要")],
            )
        ],
    )
    # LLM 始终返回 number 而不是 string：AgentRuntime 会重试 max_retries 次
    mock_provider.chat.side_effect = [_make_response(content='{"summary": 123}')] * 5
    executor = WorkflowExecutor(
        task_id="t1",
        template=template,
        variables={"title": "hello"},
        llm_provider=mock_provider,
        skill_registry=SkillRegistry(),
        on_event=lambda e: None,
    )

    await executor.run()

    # 反复产出不合规输出 → 转为 waiting_input 暂停，不会标记 completed
    assert executor.node_states["n1"].status.value == "waiting_input"


async def test_typed_io_empty_text_output_fails_post_check(mock_provider):
    """AgentRuntime 通过 schema 校验但输出为空字符串时，外层字段校验直接标失败。

    补充覆盖：text 类型字段必填但返回空串的情况（JSON Schema 的 type=string
    不会拦截空串，字段级校验负责兜住）。
    """
    template = SOPTemplate(
        id="sop-empty",
        name="empty-text",
        nodes=[
            Node(
                id="n1",
                name="n1",
                type=NodeType.AGENT,
                prompt="p",
                inputs=[IOField(name="title", type=IOType.TEXT)],
                outputs=[IOField(name="summary", type=IOType.TEXT, label="摘要")],
            )
        ],
    )
    # LLM 首次就返回空串——能通过 {"type":"string"} 校验，但会被外层字段检查拦下
    mock_provider.chat.side_effect = [_make_response(content='{"summary": ""}')]
    executor = WorkflowExecutor(
        task_id="t1",
        template=template,
        variables={"title": "hello"},
        llm_provider=mock_provider,
        skill_registry=SkillRegistry(),
        on_event=lambda e: None,
    )

    await executor.run()

    assert executor.node_states["n1"].status.value == "failed"
    assert "summary" in (executor.node_states["n1"].error or "")


async def test_typed_io_passes_between_nodes(mock_provider):
    """节点输出自动作为下游同名字段输入，全链通过。"""
    template = SOPTemplate(
        id="sop-pass",
        name="pass-through",
        nodes=[
            Node(
                id="n1",
                name="n1",
                type=NodeType.AGENT,
                prompt="生成 slogan",
                inputs=[IOField(name="product", type=IOType.TEXT, label="产品")],
                outputs=[IOField(name="slogan", type=IOType.TEXT, label="口号")],
            ),
            Node(
                id="n2",
                name="n2",
                type=NodeType.AGENT,
                prompt="基于 slogan 写点评",
                inputs=[IOField(name="slogan", type=IOType.TEXT, label="口号")],
                outputs=[IOField(name="comment", type=IOType.TEXT, label="点评")],
            ),
        ],
    )
    mock_provider.chat.side_effect = [
        _make_response(content='{"slogan":"Just Do It"}'),
        _make_response(content='{"comment":"很燃"}'),
    ]
    executor = WorkflowExecutor(
        task_id="t1",
        template=template,
        variables={"product": "Shoes"},
        llm_provider=mock_provider,
        skill_registry=SkillRegistry(),
        on_event=lambda e: None,
    )

    snapshot = await executor.run()

    assert executor.node_states["n1"].status.value == "completed"
    assert executor.node_states["n2"].status.value == "completed"
    assert executor.variables["n1"] == {"slogan": "Just Do It"}
    # n2 的 input 应该能拿到 slogan（从 n1 输出里解析到）
    assert snapshot["nodes"]["n2"]["input"] == {"slogan": "Just Do It"}


async def test_auto_linear_edges_from_nodes_order(mock_provider):
    """不写 edges/entry_node 时，模板自动按 nodes 顺序串接为线性链。"""
    template = SOPTemplate(
        id="sop-auto",
        name="auto-linear",
        nodes=[
            Node(
                id="a",
                name="a",
                type=NodeType.AGENT,
                prompt="p",
                outputs=[IOField(name="x", type=IOType.TEXT)],
            ),
            Node(
                id="b",
                name="b",
                type=NodeType.AGENT,
                prompt="p",
                inputs=[IOField(name="x", type=IOType.TEXT)],
                outputs=[IOField(name="y", type=IOType.TEXT)],
            ),
        ],
    )
    assert template.entry_node == "a"
    assert [(e.from_node, e.to) for e in template.edges] == [("a", "b")]
    assert template.linear_order() == ["a", "b"]


async def test_composite_node_generates_draft_and_pauses(mock_provider):
    """composite 节点首次运行应生成子流程草案，并暂停等待用户确认。"""
    template = SOPTemplate(
        id="sop-composite",
        name="composite",
        nodes=[
            Node(
                id="develop",
                name="开发",
                type=NodeType.COMPOSITE,
                subflow_prompt="生成两个表分析子节点和一个合并节点",
            )
        ],
    )
    mock_provider.chat.return_value = _make_response(
        content=(
            '{"nodes":[{"id":"table_a","name":"表A","type":"agent"},'
            '{"id":"merge","name":"合并","type":"agent"}],'
            '"edges":[{"from":"table_a","to":"merge"}]}'
        )
    )
    events = []
    executor = WorkflowExecutor(
        task_id="t1",
        template=template,
        variables={},
        llm_provider=mock_provider,
        skill_registry=SkillRegistry(),
        on_event=events.append,
    )

    snapshot = await executor.run()

    assert snapshot["paused"] is True
    assert snapshot["nodes"]["develop"]["status"] == "waiting_input"
    assert snapshot["nodes"]["develop"]["subflow_status"] == "waiting_subflow_confirm"
    assert snapshot["subflows"]["develop"]["status"] == "draft"
    assert snapshot["subflows"]["develop"]["nodes"] == {}
    assert executor.subflow_drafts["develop"].status == "draft"
    assert [node.id for node in executor.subflow_drafts["develop"].draft_nodes] == ["table_a", "merge"]
    assert any(e.to_dict()["type"] == "subflow_draft_created" for e in events)


async def test_confirmed_composite_runs_subflow(mock_provider):
    """确认后的 composite 节点应运行子流程，并把子流程输出作为父节点输出。"""
    template = SOPTemplate(
        id="sop-composite",
        name="composite",
        nodes=[Node(id="develop", name="开发", type=NodeType.COMPOSITE)],
    )
    executor = WorkflowExecutor(
        task_id="t1",
        template=template,
        variables={},
        llm_provider=mock_provider,
        skill_registry=SkillRegistry(),
        on_event=lambda e: None,
    )
    mock_provider.chat.return_value = _make_response(content='{"result":"ok"}')
    executor.confirm_subflow(
        "develop",
        nodes=[Node(id="table_a", name="表A", type=NodeType.AGENT)],
        edges=[],
    )

    snapshot = await executor.run()

    assert snapshot["paused"] is False
    assert snapshot["nodes"]["develop"]["status"] == "completed"
    assert snapshot["nodes"]["develop"]["output"] == {"result": "ok"}
    assert snapshot["variables"]["develop"] == {"result": "ok"}
    assert snapshot["subflows"]["develop"]["status"] == "completed"
    assert snapshot["subflows"]["develop"]["nodes"]["table_a"]["status"] == "completed"
    assert "table_a" not in snapshot["nodes"]


async def test_composite_subnode_failure_pauses_for_intervention(mock_provider):
    """子流程内部子节点失败时，父 composite 节点应暂停等待用户干预。"""
    template = SOPTemplate(
        id="sop-composite-fail",
        name="composite-fail",
        nodes=[Node(id="develop", name="开发", type=NodeType.COMPOSITE)],
    )
    events = []
    executor = WorkflowExecutor(
        task_id="t1",
        template=template,
        variables={},
        llm_provider=mock_provider,
        skill_registry=SkillRegistry(),
        on_event=events.append,
    )
    mock_provider.chat.return_value = _make_response(content=None)
    executor.confirm_subflow(
        "develop",
        nodes=[Node(id="table_b", name="表B", type=NodeType.AGENT)],
        edges=[],
    )

    snapshot = await executor.run()

    assert snapshot["paused"] is True
    assert snapshot["nodes"]["develop"]["status"] == "waiting_input"
    assert snapshot["nodes"]["develop"]["subflow_status"] == "waiting_input"
    assert snapshot["subflows"]["develop"]["status"] == "confirmed"
    assert snapshot["subflows"]["develop"]["nodes"]["table_b"]["status"] == "failed"
    assert "Subnode table_b did not complete" in snapshot["nodes"]["develop"]["error"]
    assert any(e.to_dict()["type"] == "node_waiting_input" for e in events)
