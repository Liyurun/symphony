from symphony.agent.events import NodeStatus, SubFlowDraftCreated, SubNodeRetried
from symphony.workflow.subflow import SubFlowExecutor
from symphony.workflow.models import (
    Edge,
    Node,
    NodeType,
    RetryPrompt,
    SubFlowDraft,
    SubNodeState,
)


def test_composite_node_defaults_require_confirmation():
    node = Node(id="develop", name="开发", type=NodeType.COMPOSITE)

    assert node.subflow_policy is not None
    assert node.subflow_policy.generation == "dynamic"
    assert node.subflow_policy.require_confirm is True
    assert node.subflow_policy.max_parallelism == 3
    assert node.subflow_policy.retry_scope == "node_and_downstream"


def test_subflow_draft_and_subnode_state_round_trip():
    draft = SubFlowDraft(
        parent_node_id="develop",
        draft_nodes=[Node(id="table_a", name="表A", type=NodeType.AGENT)],
        draft_edges=[Edge(from_node="table_a", to="merge")],
        generated_by="agent",
        created_at="2026-07-06T00:00:00Z",
    )
    state = SubNodeState(
        node_id="table_a",
        parent_node_id="develop",
        status=NodeStatus.PENDING,
        retry_prompts=[
            RetryPrompt(
                attempt_no=2,
                prompt="重新识别 status 字段",
                created_at="2026-07-06T00:01:00Z",
                created_by="user",
            )
        ],
        stale=True,
    )

    assert draft.status == "draft"
    assert state.retry_prompts[0].prompt == "重新识别 status 字段"
    assert state.stale is True


def test_subflow_events_serialize():
    draft_event = SubFlowDraftCreated(
        task_id="t1",
        node_id="develop",
        draft={"nodes": ["table_a"], "edges": []},
    )
    retry_event = SubNodeRetried(
        task_id="t1",
        node_id="develop",
        sub_node_id="table_b",
        retry_prompt="修复字段解释",
        invalidate_downstream=["merge"],
    )

    assert draft_event.to_dict()["type"] == "subflow_draft_created"
    assert retry_event.to_dict()["type"] == "subnode_retried"
    assert retry_event.to_dict()["sub_node_id"] == "table_b"


async def test_subflow_runs_fanout_then_merge():
    nodes = [
        Node(id="table_a", name="表A", type=NodeType.AGENT, outputs=[]),
        Node(id="table_b", name="表B", type=NodeType.AGENT, outputs=[]),
        Node(id="merge", name="合并", type=NodeType.AGENT, outputs=[]),
    ]
    edges = [Edge(from_node="table_a", to="merge"), Edge(from_node="table_b", to="merge")]
    calls = []

    async def runner(node, input_payload, retry_prompt):
        calls.append((node.id, retry_prompt))
        return {"value": node.id}

    executor = SubFlowExecutor(
        task_id="t1",
        parent_node_id="develop",
        nodes=nodes,
        edges=edges,
        variables={},
        run_node=runner,
        emit=lambda event: None,
        max_parallelism=2,
    )

    output = await executor.run()

    assert output == {"value": "merge"}
    assert calls[0][0] in {"table_a", "table_b"}
    assert calls[1][0] in {"table_a", "table_b"}
    assert calls[2][0] == "merge"
    assert executor.node_states["merge"].status == NodeStatus.COMPLETED


async def test_retry_subnode_invalidates_downstream_only():
    nodes = [
        Node(id="table_a", name="表A", type=NodeType.AGENT),
        Node(id="table_b", name="表B", type=NodeType.AGENT),
        Node(id="merge", name="合并", type=NodeType.AGENT),
    ]
    edges = [Edge(from_node="table_a", to="merge"), Edge(from_node="table_b", to="merge")]
    calls = []

    async def runner(node, input_payload, retry_prompt):
        calls.append((node.id, retry_prompt))
        return {"value": f"{node.id}:{len(calls)}"}

    executor = SubFlowExecutor(
        task_id="t1",
        parent_node_id="develop",
        nodes=nodes,
        edges=edges,
        variables={},
        run_node=runner,
        emit=lambda event: None,
        max_parallelism=2,
    )
    await executor.run()
    calls.clear()

    executor.retry_subnode("table_b", "status 是用户生命周期状态")
    output = await executor.run()

    assert [c[0] for c in calls] == ["table_b", "merge"]
    assert calls[0][1] == "status 是用户生命周期状态"
    assert executor.node_states["table_a"].status == NodeStatus.COMPLETED
    assert output["value"].startswith("merge:")


async def test_skipped_dependency_allows_downstream_to_run():
    """跳过一个上游子节点后，下游 merge 仍应可以继续运行。"""
    nodes = [
        Node(id="table_a", name="表A", type=NodeType.AGENT),
        Node(id="table_b", name="表B", type=NodeType.AGENT),
        Node(id="merge", name="合并", type=NodeType.AGENT),
    ]
    edges = [Edge(from_node="table_a", to="merge"), Edge(from_node="table_b", to="merge")]
    calls = []

    async def runner(node, input_payload, retry_prompt):
        calls.append(node.id)
        return {"value": node.id}

    executor = SubFlowExecutor(
        task_id="t1",
        parent_node_id="develop",
        nodes=nodes,
        edges=edges,
        variables={},
        run_node=runner,
        emit=lambda event: None,
        max_parallelism=2,
    )
    executor.node_states["table_b"].status = NodeStatus.SKIPPED

    output = await executor.run()

    assert calls == ["table_a", "merge"]
    assert output == {"value": "merge"}
