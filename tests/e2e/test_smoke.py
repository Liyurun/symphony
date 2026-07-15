"""端到端冒烟测试：用 FastAPI TestClient 驱动 create_app 装配的完整应用。

本测试全程 mock LLM（不发起真实网络调用），验证 Symphony 的核心闭环：
- 根路径静态托管（dist 或兜底首页均应 200）；
- SOP 列表初始为空；
- 保存 SOP → 启动任务 → 后台执行器推进 → 事件落盘 / 状态快照；
- AI 生成 SOP 端点保存并可查询。

关键替换策略（在 create_app 之后就地替换 app.state 上的依赖）：
- create_app 内部会 new 一个 DoubaoProvider 与 SOPGenerator，并把 provider 传给
  TaskManager。TaskManager.start_task 里用的是 ``self.llm_provider`` 构造执行器，
  因此把 ``app.state.task_manager.llm_provider`` 替换为 FakeProvider，
  即可让此后新建的执行器全部使用假 provider（不触网）。
- SOP 生成端点 handler 自身负责 ``loader.save(template)``，所以只需把
  ``app.state.sop_generator`` 换成一个带 async generate 方法、返回合法
  SOPTemplate 的 FakeGenerator，端点即会保存并回显。
"""

import time

from fastapi.testclient import TestClient

from symphony.ai.schema import LLMResponse, Message, Role, Usage
from symphony.config import LLMConfig, ServerConfig, StorageConfig, SymphonyConfig
from symphony.server.app import create_app
from symphony.workflow.models import Node, NodeType, SOPTemplate


class FakeProvider:
    """假的 LLMProvider：chat 恒定返回一段合法 JSON，供 agent 节点解析。

    返回的 content 是 ``{"result":"ok"}``，配合宽松的 output_schema
    ``{"type":"object"}`` 能顺利通过 JSON Schema 校验，使 agent 节点完成。
    """

    # 冗余保存 model 属性，兼容部分调用方对 provider.model 的读取
    model = "fake"

    async def chat(
        self,
        messages,
        tools=None,
        temperature=None,
        max_tokens=None,
        **kwargs,
    ) -> LLMResponse:
        """返回一条带合法 JSON 正文的 assistant 响应。"""
        # 构造单个 assistant 候选消息，正文为最小合法 JSON 对象
        msg = Message(role=Role.ASSISTANT, content='{"result":"ok"}')
        # 组装响应：给定 id/model 与 token 用量
        return LLMResponse(id="fake", choices=[msg], usage=Usage(total_tokens=10), model="fake")


class FakeGenerator:
    """假的 SOP 生成器：async generate 返回一个固定的合法 SOPTemplate。

    不做任何持久化——保存由 /api/sops/generate 端点 handler 负责（loader.save）。
    """

    async def generate(self, description: str, sop_id: str | None = None) -> SOPTemplate:
        """忽略描述，返回一个单节点 agent SOP 模板。"""
        # 允许传入 sop_id 覆盖默认 id，便于端点回显一致
        return SOPTemplate(
            id=sop_id or "generated-sop",
            name="AI 生成的测试 SOP",
            version="1.0.0",
            description=description,
            variables={"type": "object", "properties": {}},
            nodes=[
                Node(
                    id="gen-step",
                    name="生成步骤",
                    type=NodeType.AGENT,
                    prompt="执行生成的步骤",
                    skills=[],
                    input_schema={"type": "object"},
                    output_schema={"type": "object"},
                )
            ],
            edges=[],
            entry_node="gen-step",
        )


def _make_config(tmp_path) -> SymphonyConfig:
    """构造以临时目录承载存储、api_key=test 的 SymphonyConfig（不触网）。"""
    # LLM 配置：占位 api_key，避免真实调用
    llm = LLMConfig(
        provider="doubao",
        api_key="test",
        model="doubao-pro-32k",
        base_url="https://example.com/api/v3",
    )
    # 服务配置用默认值即可
    server = ServerConfig()
    # 三个存储目录均指向 pytest 提供的临时目录
    storage = StorageConfig(
        workspace_dir=str(tmp_path / "workspaces"),
        templates_dir=str(tmp_path / "templates"),
        custom_skills_dir=str(tmp_path / "skills"),
    )
    return SymphonyConfig(llm=llm, server=server, storage=storage)


def _single_agent_sop(sop_id: str = "smoke-sop") -> dict:
    """构造一个宽松 output_schema 的单节点 agent SOP 字典。

    output_schema 用最宽松的 ``{"type":"object"}``，任意 JSON 对象都能通过校验，
    配合 FakeProvider 的 ``{"result":"ok"}`` 输出，可让节点顺利完成。
    """
    return {
        "id": sop_id,
        "name": "冒烟 SOP",
        "version": "1.0.0",
        "description": "端到端冒烟用的单节点 SOP",
        "variables": {"type": "object", "properties": {}},
        "nodes": [
            {
                "id": "only",
                "name": "唯一节点",
                "type": "agent",
                "prompt": "处理任务",
                "skills": [],
                "input_schema": {"type": "object"},
                "output_schema": {"type": "object"},
            }
        ],
        "edges": [],
        "entry_node": "only",
    }


def _poll_event_types(client: TestClient, task_id: str, want: str,
                      max_iters: int = 60, interval: float = 0.05) -> list[str]:
    """轮询事件端点直到出现目标事件类型或超时，返回最后一次的事件类型列表。

    后台任务由 asyncio.create_task 调度，运行在 TestClient 的事件循环线程上。
    这里在主线程用极短 sleep 让出时间片、并反复请求事件端点，给事件循环推进机会。
    最坏耗时约 max_iters * interval（默认约 3 秒）。
    """
    types: list[str] = []
    for _ in range(max_iters):
        # 读取全部事件并取其类型
        resp = client.get(f"/api/tasks/{task_id}/events")
        assert resp.status_code == 200
        types = [event.get("type") for event in resp.json()]
        # 命中目标事件即可提前结束
        if want in types:
            break
        # 让出时间片，允许后台事件循环推进
        time.sleep(interval)
    return types


def test_root_serves_fallback_or_index(tmp_path):
    """GET / 应返回 200 的 HTML（dist 存在则为构建产物，否则为兜底提示）。"""
    app = create_app(_make_config(tmp_path))
    with TestClient(app) as client:
        resp = client.get("/")
        # 无论走静态托管还是兜底分支都应 200
        assert resp.status_code == 200
        # 返回内容应是一段 HTML（兜底含 npm/vite，或构建产物含 <html>）
        text = resp.text
        assert ("npm" in text or "vite" in text) or ("<html" in text.lower())


def test_api_sops_empty(tmp_path):
    """初始状态下 GET /api/sops 应返回 200 与空数组。"""
    app = create_app(_make_config(tmp_path))
    with TestClient(app) as client:
        resp = client.get("/api/sops")
        assert resp.status_code == 200
        assert resp.json() == []


def test_create_run_and_events(tmp_path):
    """核心闭环：保存 SOP → 启动任务 → 事件推进 → 快照可查。"""
    app = create_app(_make_config(tmp_path))
    with TestClient(app) as client:
        # 1) 保存一个单节点 agent SOP
        resp = client.post("/api/sops", json=_single_agent_sop("smoke-sop"))
        assert resp.status_code == 200
        assert resp.json()["id"] == "smoke-sop"

        # 2) 替换任务管理器持有的 provider，使新建执行器使用假 provider（不触网）
        app.state.task_manager.llm_provider = FakeProvider()

        # 3) 启动任务，拿到 task_id
        resp = client.post("/api/tasks", json={"sop_id": "smoke-sop", "variables": {}})
        assert resp.status_code == 200
        task_id = resp.json()["task_id"]
        assert task_id

        # 4) 轮询事件端点，等待任务完成（或超时后退而验证已启动）
        types = _poll_event_types(client, task_id, want="task_completed")
        # 后台任务至少应发出 task_started（此断言稳定不 flaky）
        assert "task_started" in types
        # 单节点 + 假 provider 一次即产出合法输出，任务应能跑到完成
        assert "task_completed" in types

        # 5) 快照端点应返回 200 且包含节点状态
        resp = client.get(f"/api/tasks/{task_id}")
        assert resp.status_code == 200
        snapshot = resp.json()
        assert "nodes" in snapshot
        assert "only" in snapshot["nodes"]


def test_generate_sop(tmp_path):
    """AI 生成 SOP 端点：替换为 FakeGenerator，生成结果应被保存并可查询。"""
    app = create_app(_make_config(tmp_path))
    with TestClient(app) as client:
        # 替换生成器为假实现（端点 handler 负责 loader.save）
        app.state.sop_generator = FakeGenerator()
        # 调用生成端点
        resp = client.post("/api/sops/generate", json={"description": "做个测试SOP"})
        assert resp.status_code == 200
        data = resp.json()
        # 返回体应包含节点
        assert "nodes" in data
        assert len(data["nodes"]) >= 1
        # 生成的 SOP 应已被保存，可在列表中查到
        resp = client.get("/api/sops")
        assert resp.status_code == 200
        ids = [item["id"] for item in resp.json()]
        assert data["id"] in ids


def test_composite_data_dev_contract():
    """Composite data-dev demo documents the required runtime contract.

    This test intentionally validates the JSON shape used by the real manual case,
    without calling the real LLM.
    """
    sop = {
        "id": "data-dev-composite-demo",
        "name": "数据开发 Composite Demo",
        "variables_def": [
            {"name": "requirement", "type": "document", "required": True},
        ],
        "nodes": [
            {
                "id": "develop",
                "name": "开发节点",
                "type": "composite",
                "subflow_policy": {
                    "generation": "dynamic",
                    "require_confirm": True,
                    "max_parallelism": 5,
                    "retry_scope": "node_and_downstream",
                },
                "subflow_prompt": "识别上游表并生成字段提取子流程",
            }
        ],
    }

    template = SOPTemplate.model_validate(sop)

    assert template.nodes[0].type == NodeType.COMPOSITE
    assert template.nodes[0].subflow_policy.require_confirm is True
    assert template.nodes[0].subflow_policy.max_parallelism == 5
