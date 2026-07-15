from symphony.agent.events import (
    AgentThought,
    DownstreamInvalidated,
    Event,
    InteractionAnswered,
    InteractionRequested,
    NodeAttemptCompleted,
    NodeAttemptStarted,
    NodeCompleted,
    NodeMarkedStale,
    NodeRetryRequested,
    NodeStarted,
    NodeStatus,
    NodeSupplementalInstructionAdded,
    NodeWaitingInput,
    SkillCalled,
    TaskStarted,
)


def test_event_creation():
    e = TaskStarted(task_id="t1", sop_id="sop1", variables={"a": 1})
    assert e.type == "task_started"
    assert e.task_id == "t1"


def test_event_serialization():
    e = AgentThought(node_id="n1", content="Thinking...")
    d = e.to_dict()
    assert d["type"] == "agent_thought"
    assert d["node_id"] == "n1"
    assert d["content"] == "Thinking..."
    assert "timestamp" in d


def test_node_status_enum():
    assert NodeStatus.PENDING.value == "pending"
    assert NodeStatus.RUNNING.value == "running"
    assert NodeStatus.COMPLETED.value == "completed"
    assert NodeStatus.WAITING_INPUT.value == "waiting_input"
    assert NodeStatus.FAILED.value == "failed"
    assert NodeStatus.SKIPPED.value == "skipped"


def test_skill_called_serialization():
    e = SkillCalled(node_id="n2", skill_name="search", args={"q": "hello"})
    d = e.to_dict()
    assert d["type"] == "skill_called"
    assert d["skill_name"] == "search"
    assert d["args"] == {"q": "hello"}


def test_node_waiting_input():
    e = NodeWaitingInput(node_id="n3", reason="need approval")
    d = e.to_dict()
    assert d["type"] == "node_waiting_input"
    assert d["reason"] == "need approval"


def test_runtime_correction_events_to_dict():
    """运行时纠偏事件应能序列化为前端可消费的 dict。"""
    retry = NodeRetryRequested(
        task_id="task-1",
        node_id="n2",
        attempt_no=2,
        supplemental_instruction="聚焦价格策略",
        invalidate_downstream=True,
        invalidated_node_ids=["n3", "n4"],
    )
    stale = NodeMarkedStale(
        task_id="task-1",
        node_id="n3",
        reason="upstream_rerun",
        upstream_node_id="n2",
    )
    invalidated = DownstreamInvalidated(
        task_id="task-1",
        node_id="n2",
        invalidated_node_ids=["n3", "n4"],
        reason="upstream_rerun",
    )

    assert retry.to_dict()["type"] == "node_retry_requested"
    assert retry.to_dict()["attempt_no"] == 2
    assert stale.to_dict()["type"] == "node_marked_stale"
    assert invalidated.to_dict()["invalidated_node_ids"] == ["n3", "n4"]


def test_attempt_and_interaction_events_to_dict():
    """Attempt 与 interaction 事件应包含 node/attempt 关键字段。"""
    started = NodeAttemptStarted(
        task_id="task-1",
        node_id="n1",
        attempt_no=1,
        trigger="normal",
    )
    completed = NodeAttemptCompleted(
        task_id="task-1",
        node_id="n1",
        attempt_no=1,
        output={"ok": True},
    )
    requested = InteractionRequested(
        task_id="task-1",
        node_id="review",
        interaction_id="int-1",
        attempt_no=1,
        prompt="是否继续？",
        input_schema={"type": "object"},
        options=[{"label": "继续", "value": True}],
        multi_select=False,
    )
    answered = InteractionAnswered(
        task_id="task-1",
        node_id="review",
        interaction_id="int-1",
        attempt_no=1,
        answer={"approved": True},
    )
    supplement = NodeSupplementalInstructionAdded(
        task_id="task-1",
        node_id="n1",
        attempt_no=2,
        supplemental_instruction="补充说明",
    )

    assert started.to_dict()["type"] == "node_attempt_started"
    assert completed.to_dict()["output"] == {"ok": True}
    assert requested.to_dict()["type"] == "interaction_requested"
    assert answered.to_dict()["answer"] == {"approved": True}
    assert supplement.to_dict()["supplemental_instruction"] == "补充说明"
