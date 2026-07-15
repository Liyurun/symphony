"""FastAPI REST API / WebSocket / AI SOP 生成器的集成测试。

用 fastapi.testclient.TestClient 驱动 create_app 构建的应用；用临时目录承载存储，
用 AsyncMock 替换 app.state 中的 llm_provider / sop_generator，避免真实网络调用。
覆盖 SOP 列表/创建/查询/生成、技能列表、配置掩码、任务启动等核心链路。
"""

import json
import time
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from symphony.ai.schema import LLMResponse, Message, Role, Usage
from symphony.config import (
    AgentRuntimeConfig,
    ContextCompressionConfig,
    HttpRequestSkillConfig,
    LLMConfig,
    PythonExecuteSkillConfig,
    RuntimeConfig,
    ServerConfig,
    SkillsConfig,
    StorageConfig,
    SymphonyConfig,
    WorkspaceSkillConfig,
)
from symphony.server import app as server_app
from symphony.server.app import create_app
from symphony.skills.references import SkillReferenceIndex


def _make_config(tmp_path):
    """构造一个使用临时目录作存储、api_key=test 的 SymphonyConfig。"""
    # 大模型配置：api_key 用占位值，避免真实调用
    llm = LLMConfig(
        provider="doubao",
        api_key="test",
        model="doubao-test",
        base_url="https://example.com/api/v3",
        temperature=0.5,
        max_tokens=1024,
    )
    # 服务配置：默认地址端口即可
    server = ServerConfig()
    # 存储配置：三个目录均指向临时目录
    storage = StorageConfig(
        workspace_dir=str(tmp_path / "workspaces"),
        templates_dir=str(tmp_path / "templates"),
        custom_skills_dir=str(tmp_path / "skills"),
    )
    return SymphonyConfig(llm=llm, server=server, storage=storage)


def test_create_app_passes_llm_timeout_and_agent_runtime_config(tmp_path, monkeypatch):
    """create_app 应把 LLM timeout 与 Agent runtime 配置接到后端依赖。"""
    captured = {}

    class CapturingProvider:
        """记录 DoubaoProvider 构造参数。"""

        def __init__(self, **kwargs):
            captured["provider_kwargs"] = kwargs
            self.api_key = kwargs["api_key"]
            self.model = kwargs["model"]

    config = _make_config(tmp_path)
    config.llm.timeout_seconds = 17.5
    config.skills = SkillsConfig(
        http_request=HttpRequestSkillConfig(timeout_seconds=6.5),
        workspace=WorkspaceSkillConfig(
            bash_timeout_seconds=4,
            max_output_chars=1800,
            list_files_max_results=11,
            search_max_results=12,
        ),
        python_execute=PythonExecuteSkillConfig(timeout_seconds=8),
    )
    config.runtime = RuntimeConfig(
        agent=AgentRuntimeConfig(max_iterations=8, max_retries=2),
        context_compression=ContextCompressionConfig(
            enabled=False,
            max_prompt_chars=2222,
            keep_recent_messages=7,
            min_recent_messages=3,
            summary_max_chars=444,
            max_message_chars=555,
        ),
    )
    monkeypatch.setattr(server_app, "DoubaoProvider", CapturingProvider)

    app = server_app.create_app(config)

    assert app.state.llm_provider is app.state.task_manager.llm_provider
    assert captured["provider_kwargs"]["timeout"] == 17.5
    assert app.state.task_manager.agent_max_iterations == 8
    assert app.state.task_manager.agent_max_retries == 2
    compression = app.state.task_manager.context_compression_config
    assert compression.enabled is False
    assert compression.max_prompt_chars == 2222
    assert app.state.skill_registry.get("http_request").timeout == 6.5
    assert app.state.skill_registry.get("bash_execute").default_timeout == 4
    assert app.state.skill_registry.get("bash_execute").default_max_output_chars == 1800
    assert app.state.skill_registry.get("workspace_list_files").default_max_results == 11
    assert app.state.skill_registry.get("workspace_search").default_max_results == 12
    assert app.state.skill_registry.get("python_execute").default_timeout == 8


def _sample_sop_dict(sop_id="demo"):
    """构造一个可被 SOPTemplate 校验通过的最小 SOP 字典（含单节点）。"""
    return {
        "id": sop_id,
        "name": "示例 SOP",
        "version": "1.0.0",
        "description": "演示用",
        "variables": {"type": "object", "properties": {}},
        "nodes": [
            {
                "id": "step1",
                "name": "第一步",
                "type": "agent",
                "prompt": "处理任务",
                "skills": [],
                "input_schema": {"type": "object"},
                "output_schema": {"type": "object"},
            }
        ],
        "edges": [],
        "entry_node": "step1",
    }


def _make_response(content):
    """构造一条带单个 assistant choice 的 LLMResponse。"""
    msg = Message(role=Role.ASSISTANT, content=content)
    return LLMResponse(id="r1", choices=[msg], usage=Usage(), model="doubao-test")


def _human_sop_dict(sop_id="human-sop"):
    """构造一个单 human 节点 SOP。"""
    payload = _sample_sop_dict(sop_id)
    payload["nodes"][0].update(
        {
            "id": "review",
            "name": "人工复核",
            "type": "human",
            "description": "请确认是否继续",
        }
    )
    payload["entry_node"] = "review"
    return payload


def _wait_for_pending_interactions(client, task_id):
    """等待后台任务写入 pending interaction，降低异步调度带来的抖动。"""
    last_resp = None
    for _ in range(20):
        last_resp = client.get(f"/api/tasks/{task_id}/interactions/pending")
        if last_resp.status_code != 200 or last_resp.json():
            return last_resp
        time.sleep(0.01)
    return last_resp


@pytest.fixture
def client(tmp_path):
    """构造应用并把 llm_provider 换成返回合法 SOP JSON 的 AsyncMock。"""
    # 用临时配置构建应用
    app = create_app(_make_config(tmp_path))
    # 把 provider 替换为 AsyncMock，chat 默认返回合法 SOP JSON
    provider = AsyncMock()
    provider.model = "doubao-test"
    provider.chat.return_value = _make_response(json.dumps(_sample_sop_dict("gen-sop")))
    # 同步替换 app.state 中依赖 provider 的对象
    app.state.llm_provider = provider
    app.state.task_manager.llm_provider = provider
    app.state.sop_generator.llm_provider = provider
    # TestClient 能自动驱动 async 端点
    with TestClient(app) as test_client:
        yield test_client


def test_list_sops_empty(client):
    """初始状态下 SOP 列表应为空数组。"""
    resp = client.get("/api/sops")
    assert resp.status_code == 200
    assert resp.json() == []


def test_create_and_get_sop(client):
    """创建 SOP 后应能按 id 查询，并出现在列表中。"""
    # 创建
    payload = _sample_sop_dict("demo")
    resp = client.post("/api/sops", json=payload)
    assert resp.status_code == 200
    assert resp.json()["id"] == "demo"
    # 按 id 查询
    resp = client.get("/api/sops/demo")
    assert resp.status_code == 200
    assert resp.json()["name"] == "示例 SOP"
    # 列表包含该 SOP
    resp = client.get("/api/sops")
    ids = [item["id"] for item in resp.json()]
    assert "demo" in ids


def test_get_sop_404(client):
    """查询不存在的 SOP 应返回 404。"""
    resp = client.get("/api/sops/nope")
    assert resp.status_code == 404


def test_delete_sop(client):
    """删除 SOP 应返回 deleted 标记。"""
    client.post("/api/sops", json=_sample_sop_dict("del-me"))
    resp = client.delete("/api/sops/del-me")
    assert resp.status_code == 200
    assert resp.json()["deleted"] is True


def test_generate_sop(client):
    """生成端点应调用 LLM 并返回解析出的 SOP（含 nodes）。"""
    resp = client.post("/api/sops/generate", json={"description": "帮我写个流程"})
    assert resp.status_code == 200
    data = resp.json()
    assert "nodes" in data
    assert len(data["nodes"]) >= 1
    # 生成的 SOP 应已被保存
    assert client.get(f"/api/sops/{data['id']}").status_code == 200


def test_generate_sop_draft_does_not_save(client):
    """生成草案端点应返回 SOP，但不写入模板目录。"""
    resp = client.post("/api/sops/generate-draft", json={"description": "帮我写个流程"})

    assert resp.status_code == 200
    data = resp.json()
    assert data["id"]
    assert "nodes" in data
    assert client.get(f"/api/sops/{data['id']}").status_code == 404


def test_skills_list(client):
    """技能列表应包含内置技能（如 http_request）。"""
    resp = client.get("/api/skills")
    assert resp.status_code == 200
    names = [item["name"] for item in resp.json()]
    assert "http_request" in names
    assert "workspace_search" in names
    assert "bash_execute" in names
    assert "file_patch" in names
    assert "skill_reference_search" in names
    assert "skill_inventory" in names
    # 每个技能应含描述与 schema 字段
    assert all("input_schema" in item and "description" in item for item in resp.json())
    assert any(item["name"] == "http_request" and item["source"] == "builtin" for item in resp.json())


def test_custom_skill_auto_loaded(tmp_path):
    """服务启动时应自动加载 custom_skills_dir 下的自定义 Skill。"""
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    (skills_dir / "time_skill.py").write_text(
        '''
from symphony.skills.base import Skill


class TimeSkill(Skill):
    name = "time_now"
    description = "Return a fake time"
    input_schema = {"type": "object", "properties": {}}
    output_schema = {"type": "object"}

    async def execute(self, args, context):
        return {"time": "now"}
''',
        encoding="utf-8",
    )
    app = create_app(_make_config(tmp_path))

    with TestClient(app) as test_client:
        resp = test_client.get("/api/skills")
        assert resp.status_code == 200
        skills = resp.json()
        assert any(item["name"] == "time_now" and item["source"].startswith("custom:") for item in skills)


def test_custom_skill_load_errors_visible(tmp_path):
    """自定义 Skill 加载失败时，应能通过 API 查到错误。"""
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    (skills_dir / "bad_skill.py").write_text("raise RuntimeError('bad custom skill')", encoding="utf-8")
    app = create_app(_make_config(tmp_path))

    with TestClient(app) as test_client:
        resp = test_client.get("/api/skills/load-errors")
        assert resp.status_code == 200
        data = resp.json()
        assert data["loaded"] == []
        assert "bad custom skill" in data["errors"][0]["error"]


def test_skill_references_search_api(client, tmp_path):
    """外部 SKILL.md 检索端点应返回命中的 Skill 参考资料。"""
    skill_dir = tmp_path / "external-skills" / "bytedance-log"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        """---
name: bytedance-log
description: 查询服务日志、LogID 和 pod 日志。
---

Use this skill when users need log search.
""",
        encoding="utf-8",
    )
    client.app.state.skill_reference_index = SkillReferenceIndex.from_roots([tmp_path / "external-skills"])

    resp = client.get("/api/skills/references", params={"query": "查日志", "limit": 5})

    assert resp.status_code == 200
    data = resp.json()
    assert data["query"] == "查日志"
    assert data["items"][0]["name"] == "bytedance-log"
    assert "日志" in data["items"][0]["snippet"]


def test_skill_references_inventory_api(client, tmp_path):
    """query=skill 应按清单模式列出外部 Skill，而不是普通关键词搜索。"""
    root = tmp_path / "external-skills"
    for name in ["alpha-tool", "beta-tool"]:
        skill_dir = root / name
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            f"""---
name: {name}
description: {name} description
---
""",
            encoding="utf-8",
        )
    client.app.state.skill_reference_index = SkillReferenceIndex.from_roots([root])

    resp = client.get("/api/skills/references", params={"query": "skill", "limit": 10})

    assert resp.status_code == 200
    data = resp.json()
    assert data["mode"] == "list"
    assert data["total"] == 2
    assert [item["name"] for item in data["items"]] == ["alpha-tool", "beta-tool"]


def test_skill_references_api_uses_configured_limit_defaults_and_clamp(client, tmp_path):
    """外部 Skill 查询 API 应用配置默认 limit，并允许请求覆盖且按最大值夹限。"""
    root = tmp_path / "external-skills"
    for name in ["alpha-tool", "beta-tool", "gamma-tool"]:
        skill_dir = root / name
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            f"""---
name: {name}
description: {name} description
---
""",
            encoding="utf-8",
        )
    client.app.state.skill_reference_index = SkillReferenceIndex.from_roots([root])
    client.app.state.config.skills.skill_references.default_limit = 1
    client.app.state.config.skills.skill_references.max_limit = 2

    default_resp = client.get("/api/skills/references", params={"query": "skill"})
    override_resp = client.get(
        "/api/skills/references", params={"query": "skill", "limit": 2}
    )
    clamped_resp = client.get(
        "/api/skills/references", params={"query": "skill", "limit": 99}
    )

    assert default_resp.status_code == 200
    assert len(default_resp.json()["items"]) == 1
    assert override_resp.status_code == 200
    assert len(override_resp.json()["items"]) == 2
    assert clamped_resp.status_code == 200
    assert len(clamped_resp.json()["items"]) == 2


def test_config_masked(client):
    """配置端点应对 api_key 掩码，并返回新增非敏感配置段。"""
    resp = client.get("/api/config")
    assert resp.status_code == 200
    data = resp.json()
    assert data["llm"]["api_key"] == "***"
    assert data["llm"]["model"] == "doubao-test"
    assert data["llm"]["timeout_seconds"] == 120.0
    assert data["runtime"]["chat"]["max_iterations"] == 999
    assert data["runtime"]["agent"]["max_retries"] == 3
    assert data["runtime"]["context_compression"]["max_prompt_chars"] == 120_000
    assert data["skills"]["http_request"]["timeout_seconds"] == 30.0
    assert data["skills"]["workspace"]["max_output_chars"] == 12_000
    assert data["skills"]["workspace"]["list_files_max_results"] == 200
    assert data["skills"]["workspace"]["search_max_results"] == 80
    assert data["skills"]["skill_references"]["default_limit"] == 20
    assert data["skills"]["skill_references"]["max_limit"] == 50
    assert data["client"]["http_timeout_seconds"] == 30.0
    assert data["client"]["tui_input_history_limit"] == 100
    assert data["client"]["chat_context_history_limit"] == 24


def test_config_update(client):
    """配置更新端点应能改动内存中的 llm 参数并回显掩码视图。"""
    resp = client.put("/api/config", json={"llm": {"model": "new-model", "temperature": 0.9}})
    assert resp.status_code == 200
    data = resp.json()
    assert data["llm"]["model"] == "new-model"
    assert data["llm"]["temperature"] == 0.9
    assert data["llm"]["api_key"] == "***"


def test_tasks_list_empty(client):
    """初始状态下任务列表应为空。"""
    resp = client.get("/api/tasks")
    assert resp.status_code == 200
    assert resp.json() == []


def test_start_task_and_list(client):
    """创建 SOP 后启动任务应返回 task_id，且任务出现在列表中。"""
    # 先存一个单节点 agent SOP，并让 provider 返回该节点期望的输出
    client.post("/api/sops", json=_sample_sop_dict("run-sop"))
    app = client.app
    app.state.llm_provider.chat.return_value = _make_response("{}")
    # 启动任务
    resp = client.post("/api/tasks", json={"sop_id": "run-sop", "variables": {}})
    assert resp.status_code == 200
    task_id = resp.json()["task_id"]
    assert task_id
    # 任务列表应包含该任务
    resp = client.get("/api/tasks")
    task_ids = [item["task_id"] for item in resp.json()]
    assert task_id in task_ids
    # 事件端点应能读取（至少含 task_started）
    resp = client.get(f"/api/tasks/{task_id}/events")
    assert resp.status_code == 200
    types = [event.get("type") for event in resp.json()]
    assert "task_started" in types


def test_task_events_404(client):
    """未知任务的事件端点应返回 404。"""
    resp = client.get("/api/tasks/unknown/events")
    assert resp.status_code == 404


def test_task_dag_log_api(client):
    """DAG log API 应返回节点、边和事件数量。"""
    client.app.state.llm_provider.chat.return_value = _make_response("{}")
    payload = _sample_sop_dict("dag-log-sop")
    payload["nodes"].append(
        {
            "id": "step2",
            "name": "第二步",
            "type": "agent",
            "prompt": "继续处理",
            "skills": [],
            "input_schema": {"type": "object"},
            "output_schema": {"type": "object"},
        }
    )
    client.post("/api/sops", json=payload)
    task_id = client.post("/api/tasks", json={"sop_id": "dag-log-sop", "variables": {}}).json()["task_id"]

    resp = client.get(f"/api/tasks/{task_id}/dag-log")

    assert resp.status_code == 200
    data = resp.json()
    assert data["task_id"] == task_id
    assert [node["node_id"] for node in data["nodes"]] == ["step1", "step2"]
    assert data["edges"] == [{"from": "step1", "to": "step2", "reason": None}]
    assert "raw_event_count" in data
    assert set(data["nodes"][0]) >= {"events", "traces", "interactions", "attempt_history"}


def test_task_dag_log_api_404(client):
    """未知任务的 DAG log API 应返回 404。"""
    resp = client.get("/api/tasks/unknown/dag-log")

    assert resp.status_code == 404


def test_rerun_node_endpoint_requires_instruction(client):
    """主节点 rerun API 应校验补充指令不能为空。"""
    client.app.state.llm_provider.chat.return_value = _make_response("{}")
    client.post("/api/sops", json=_sample_sop_dict("rerun-sop"))
    resp = client.post("/api/tasks", json={"sop_id": "rerun-sop", "variables": {}})
    task_id = resp.json()["task_id"]

    bad = client.post(
        f"/api/tasks/{task_id}/nodes/step1/rerun",
        json={"supplemental_instruction": "", "invalidate_downstream": True},
    )

    assert bad.status_code == 400


def test_rerun_node_endpoint_returns_invalidated_nodes(client):
    """主节点 rerun API 应返回 attempt 和被失效节点列表。"""
    client.app.state.llm_provider.chat.return_value = _make_response("{}")
    client.post("/api/sops", json=_sample_sop_dict("rerun-sop-2"))
    resp = client.post("/api/tasks", json={"sop_id": "rerun-sop-2", "variables": {}})
    task_id = resp.json()["task_id"]

    ok = client.post(
        f"/api/tasks/{task_id}/nodes/step1/rerun",
        json={"supplemental_instruction": "补充关注价格", "invalidate_downstream": True},
    )

    assert ok.status_code == 200
    data = ok.json()
    assert data["ok"] is True
    assert data["attempt_no"] >= 1
    assert "step1" in data["invalidated_node_ids"]


def test_pending_interactions_api_for_human_node(client):
    """human 节点应能通过 pending interactions API 查看并回答。"""
    client.post("/api/sops", json=_human_sop_dict("human-sop"))
    resp = client.post("/api/tasks", json={"sop_id": "human-sop", "variables": {}})
    task_id = resp.json()["task_id"]

    resp = _wait_for_pending_interactions(client, task_id)

    assert resp.status_code == 200
    pending = resp.json()
    assert len(pending) == 1
    assert pending[0]["type"] == "interaction_requested"
    assert pending[0]["node_id"] == "review"
    assert pending[0]["interaction_id"] == f"int-{task_id}-review-1"

    answer = client.post(
        f"/api/tasks/{task_id}/interactions/{pending[0]['interaction_id']}/answer",
        json={"answer": {"approved": True}},
    )

    assert answer.status_code == 200
    assert answer.json() == {"ok": True}
    time.sleep(0.05)
    assert client.get(f"/api/tasks/{task_id}/interactions/pending").json() == []
    snapshot = client.get(f"/api/tasks/{task_id}").json()
    assert snapshot["nodes"]["review"]["pending_interaction_id"] is None
    assert snapshot["nodes"]["review"]["status"] == "completed"
    assert snapshot["variables"]["review"] == {"approved": True}
    events = client.get(f"/api/tasks/{task_id}/events").json()
    answered = [event for event in events if event["type"] == "interaction_answered"]
    assert answered[-1]["answer"] == {"approved": True}


def test_pending_interactions_api_404(client):
    """未知任务的 pending interaction API 应返回 404。"""
    resp = client.get("/api/tasks/unknown/interactions/pending")

    assert resp.status_code == 404


def test_root_serves_html(client):
    """根路径应返回 200 的 HTML：dist 已构建则返回其 index.html，否则返回兜底提示。

    该断言不依赖 web/dist 是否存在（dist 被 gitignore 且可能过时），
    只要求根路径能稳定返回一段 HTML 首页即可。
    """
    resp = client.get("/")
    # 无论走静态托管还是兜底分支都应 200
    assert resp.status_code == 200
    # 兜底提示（含 npm/vite）或已构建的前端首页（含 <html>）二者其一
    text = resp.text
    assert ("npm" in text or "vite" in text) or ("<html" in text.lower())
