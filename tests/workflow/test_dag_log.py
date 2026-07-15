"""DAG Log 投影测试。"""

from symphony.workflow.dag_log import build_dag_log


def test_build_dag_log_groups_runtime_records_by_node():
    """DAG Log 应按 SOP 节点顺序归组事件、trace 与 interaction。"""
    sop = {
        "nodes": [{"id": "a", "name": "A"}, {"id": "b", "name": "B"}],
        "edges": [],
    }
    snapshot = {
        "task_id": "task-1",
        "nodes": {
            "a": {
                "status": "completed",
                "attempts": 2,
                "attempt_history": [{"attempt_no": 2, "status": "completed"}],
            },
            "b": {
                "status": "waiting_input",
                "attempts": 1,
                "attempt_history": [],
                "stale": True,
                "stale_reason": "upstream_rerun",
                "pending_interaction_id": "int-1",
            },
        },
    }
    events = [
        {"type": "task_started", "task_id": "task-1"},
        {"type": "node_attempt_started", "task_id": "task-1", "node_id": "a", "attempt_no": 2},
        {
            "type": "node_marked_stale",
            "task_id": "task-1",
            "node_id": "b",
            "reason": "upstream_rerun",
            "upstream_node_id": "a",
        },
        {
            "type": "interaction_requested",
            "task_id": "task-1",
            "node_id": "b",
            "interaction_id": "int-1",
            "attempt_no": 1,
        },
    ]
    traces = [
        {"task_id": "task-1", "node_id": "a", "model": "doubao-test"},
        {"task_id": "task-1", "node_id": "other", "model": "doubao-test"},
    ]
    interactions = [event for event in events if event["type"].startswith("interaction_")]

    dag_log = build_dag_log("task-1", sop, snapshot, events, traces, interactions)

    assert dag_log["task_id"] == "task-1"
    assert dag_log["raw_event_count"] == 4
    assert [node["node_id"] for node in dag_log["nodes"]] == ["a", "b"]
    assert set(dag_log["nodes"][0]) >= {
        "node_id",
        "name",
        "status",
        "attempts",
        "attempt_history",
        "stale",
        "stale_reason",
        "pending_interaction_id",
        "events",
        "traces",
        "interactions",
    }
    assert dag_log["nodes"][0]["status"] == "completed"
    assert dag_log["nodes"][0]["attempts"] == 2
    assert dag_log["nodes"][0]["attempt_history"] == [{"attempt_no": 2, "status": "completed"}]
    assert dag_log["nodes"][0]["events"] == [events[1]]
    assert dag_log["nodes"][0]["traces"] == [traces[0]]
    assert dag_log["nodes"][1]["stale"] is True
    assert dag_log["nodes"][1]["stale_reason"] == "upstream_rerun"
    assert dag_log["nodes"][1]["pending_interaction_id"] == "int-1"
    assert dag_log["nodes"][1]["interactions"] == [events[3]]
    assert dag_log["edges"] == [{"from": "a", "to": "b", "reason": "upstream_rerun"}]


def test_build_dag_log_uses_explicit_sop_edges():
    """SOP 显式 edges 存在时应直接使用，并兼容 from_node 键。"""
    sop = {
        "nodes": [{"id": "start", "name": "开始"}, {"id": "finish", "name": "结束"}],
        "edges": [{"from_node": "start", "to": "finish"}],
    }
    snapshot = {
        "nodes": {
            "start": {"status": "completed"},
            "finish": {"status": "pending"},
        }
    }

    dag_log = build_dag_log("task-2", sop, snapshot, events=[], traces=[], interactions=[])

    assert dag_log["edges"] == [{"from": "start", "to": "finish", "reason": None}]
