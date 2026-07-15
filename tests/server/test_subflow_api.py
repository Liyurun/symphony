from fastapi import FastAPI
from fastapi.testclient import TestClient

from symphony.server.api.tasks import router


class FakeTaskManager:
    def __init__(self):
        self.confirm_args = None
        self.reject_args = None
        self.retry_args = None
        self.provide_args = None
        self.skip_args = None
        self.retry_upstreams_args = None

    async def confirm_subflow(self, task_id, node_id, nodes, edges):
        self.confirm_args = (task_id, node_id, nodes, edges)

    async def reject_subflow(self, task_id, node_id, reason):
        self.reject_args = (task_id, node_id, reason)

    async def retry_subnode(self, task_id, node_id, sub_node_id, retry_prompt):
        self.retry_args = (task_id, node_id, sub_node_id, retry_prompt)

    async def provide_subnode_output(self, task_id, node_id, sub_node_id, output):
        self.provide_args = (task_id, node_id, sub_node_id, output)

    async def skip_subnode(self, task_id, node_id, sub_node_id):
        self.skip_args = (task_id, node_id, sub_node_id)

    async def retry_upstreams(self, task_id, node_id, sub_node_ids, retry_prompts):
        self.retry_upstreams_args = (task_id, node_id, sub_node_ids, retry_prompts)


def make_client():
    app = FastAPI()
    app.state.task_manager = FakeTaskManager()
    app.include_router(router)
    return TestClient(app), app.state.task_manager


def test_confirm_subflow_endpoint_returns_ok():
    client, manager = make_client()

    response = client.post(
        "/api/tasks/task-1/nodes/develop/subflow/confirm",
        json={
            "nodes": [{"id": "table_a", "name": "表A", "type": "agent"}],
            "edges": [],
        },
    )

    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert manager.confirm_args == (
        "task-1",
        "develop",
        [{"id": "table_a", "name": "表A", "type": "agent"}],
        [],
    )


def test_retry_subnode_endpoint_calls_manager():
    client, manager = make_client()

    response = client.post(
        "/api/tasks/task-1/nodes/develop/subnodes/table_b/retry",
        json={"retry_prompt": "重新解释 status 字段", "invalidate_downstream": True},
    )

    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert manager.retry_args == ("task-1", "develop", "table_b", "重新解释 status 字段")


def test_reject_subflow_endpoint_calls_manager():
    client, manager = make_client()

    response = client.post(
        "/api/tasks/task-1/nodes/develop/subflow/reject",
        json={"reason": "上游表识别错误"},
    )

    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert manager.reject_args == ("task-1", "develop", "上游表识别错误")


def test_provide_subnode_output_endpoint_calls_manager():
    client, manager = make_client()

    response = client.post(
        "/api/tasks/task-1/nodes/develop/subnodes/table_b/provide-output",
        json={"output": {"fields": ["user_id", "status"]}},
    )

    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert manager.provide_args == (
        "task-1",
        "develop",
        "table_b",
        {"fields": ["user_id", "status"]},
    )


def test_skip_subnode_endpoint_calls_manager():
    client, manager = make_client()

    response = client.post("/api/tasks/task-1/nodes/develop/subnodes/table_b/skip")

    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert manager.skip_args == ("task-1", "develop", "table_b")


def test_retry_upstreams_endpoint_calls_manager():
    client, manager = make_client()

    response = client.post(
        "/api/tasks/task-1/nodes/develop/subflow/retry-upstreams",
        json={
            "sub_node_ids": ["table_b", "table_d"],
            "retry_prompts": {"table_b": "修复 B", "table_d": "修复 D"},
        },
    )

    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert manager.retry_upstreams_args == (
        "task-1",
        "develop",
        ["table_b", "table_d"],
        {"table_b": "修复 B", "table_d": "修复 D"},
    )
