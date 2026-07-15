"""symphony.tui.client 的单元测试。

重点覆盖纯函数（apply_event / event_summary / render_dag）与
ServerClient 的 REST 调用（用 pytest-httpx mock）。app.py 只做
可导入与可实例化的轻量校验，不启动 Textual 事件循环。
"""

import json

import pytest

import symphony.tui.client as tui_client
from symphony.config import (
    ClientConfig,
    LLMConfig,
    ServerConfig,
    StorageConfig,
    SymphonyConfig,
)
from symphony.tui.client import (
    ChatSocket,
    ServerClient,
    apply_event,
    command_help,
    compact_tool_call,
    compact_tool_result,
    coerce_tui_field_value,
    event_summary,
    is_complex_variables_def,
    parse_tui_command,
    render_dag,
    render_home,
    render_sop_detail,
)


# ---------------- apply_event 纯函数 ----------------


def test_apply_event_node_status():
    """node_completed 应把节点置为 completed 并写入 output，且不改动入参。"""
    nodes = {"n1": {"node_id": "n1", "status": "pending"}}
    event = {"type": "node_completed", "node_id": "n1", "output": {"x": 1}}
    result = apply_event(nodes, event)
    # 返回的字典里 n1 已完成且带 output
    assert result["n1"]["status"] == "completed"
    assert result["n1"]["output"] == {"x": 1}
    # 原 nodes 未被修改（不可变更新）
    assert nodes["n1"]["status"] == "pending"
    assert "output" not in nodes["n1"]
    # 返回的是新对象
    assert result is not nodes


def test_apply_event_status_changed():
    """node_status_changed 应把节点状态改为事件中的 status。"""
    nodes = {"n1": {"node_id": "n1", "status": "pending"}}
    event = {"type": "node_status_changed", "node_id": "n1", "status": "running"}
    result = apply_event(nodes, event)
    assert result["n1"]["status"] == "running"
    # 原对象保持不变
    assert nodes["n1"]["status"] == "pending"


def test_apply_event_unknown_node():
    """node_id 不在 nodes 中时新建最小 entry，且不影响既有节点。"""
    nodes = {"n1": {"node_id": "n1", "status": "pending"}}
    event = {"type": "node_started", "node_id": "n2"}
    result = apply_event(nodes, event)
    # 新建的 n2 处于 running
    assert result["n2"]["status"] == "running"
    assert result["n2"]["node_id"] == "n2"
    # 既有 n1 不受影响
    assert result["n1"]["status"] == "pending"
    # 原 nodes 未新增 n2
    assert "n2" not in nodes


def test_apply_event_irrelevant():
    """agent_thought 等非节点事件不改动 nodes（仍返回新副本）。"""
    nodes = {"n1": {"node_id": "n1", "status": "running"}}
    event = {"type": "agent_thought", "node_id": "n1", "content": "思考中"}
    result = apply_event(nodes, event)
    # 状态未变
    assert result["n1"]["status"] == "running"
    # 返回的是新 dict（不可变语义）
    assert result is not nodes


def test_apply_event_empty_node_id():
    """node_id 为空时安全跳过，返回未改动的副本。"""
    nodes = {"n1": {"node_id": "n1", "status": "running"}}
    event = {"type": "node_started", "node_id": ""}
    result = apply_event(nodes, event)
    assert result == nodes
    assert result is not nodes


# ---------------- event_summary 纯函数 ----------------


def test_event_summary_is_ascii_and_informative():
    """事件摘要应为纯 ASCII 且包含关键信息。"""
    cases = [
        {"type": "task_completed"},
        {"type": "task_failed", "error": "boom"},
        {"type": "node_started", "node_id": "n1"},
        {"type": "node_completed", "node_id": "n1"},
        {"type": "node_failed", "node_id": "n1", "error": "boom"},
        {"type": "node_waiting_input", "node_id": "n1", "reason": "缺资料"},
        {"type": "node_status_changed", "node_id": "n1", "status": "running"},
        {"type": "agent_thought", "content": "hello"},
        {"type": "skill_called", "skill_name": "search"},
        {"type": "skill_returned", "skill_name": "search"},
        {"type": "skill_failed", "skill_name": "search", "error": "x"},
        {"type": "user_intervened", "action": "retry", "node_id": "n1"},
    ]
    for event in cases:
        summary = event_summary(event)
        # 摘要主体（去掉可能的中文 reason/error 透传）应为 ASCII 记号，不含 emoji
        assert all(ord(ch) < 128 for ch in summary if ch not in "缺资料")
    assert "boom" in event_summary({"type": "task_failed", "error": "boom"})
    assert "n1" in event_summary({"type": "node_started", "node_id": "n1"})
    assert "search" in event_summary({"type": "skill_called", "skill_name": "search"})
    assert "query" in event_summary({"type": "skill_called", "skill_name": "search", "args": {"query": "abc"}})
    assert "items" in event_summary({"type": "skill_returned", "skill_name": "search", "result": {"items": [1]}})
    assert event_summary({"type": "weird"}) == "weird"


def test_event_summary_thought_truncation():
    """agent_thought 内容应被截断到 80 字。"""
    summary = event_summary({"type": "agent_thought", "content": "z" * 200})
    assert summary.count("z") == 80


# ---------------- compact tool summaries 纯函数 ----------------


def test_compact_tool_call_prefers_command():
    """工具调用摘要应优先展示 command。"""
    text = compact_tool_call(
        "python_execute",
        {"command": "python analyze.py --input data.json"},
        width=120,
    )

    assert text == '-> python_execute command="python analyze.py --input data.json"'


def test_compact_tool_call_uses_compact_json_fallback():
    """缺少优先字段时应展示紧凑 JSON。"""
    text = compact_tool_call("custom_tool", {"b": 2, "a": 1}, width=120)

    assert text == '-> custom_tool {"a":1,"b":2}'


def test_compact_tool_call_truncates_long_text():
    """长工具调用摘要应截断为单行。"""
    text = compact_tool_call("python_execute", {"command": "x" * 200}, width=80)

    assert "\n" not in text
    assert len(text) <= 80
    assert text.endswith("...")


def test_compact_tool_call_prefers_query_and_omits_extra_args():
    """检索类工具调用只展示 query 关键字段，避免参数 JSON 过长。"""
    text = compact_tool_call(
        "skill_reference_search",
        {"query": "所有skill", "limit": 20},
        width=120,
    )

    assert text.startswith("-> skill_reference_search query=")
    assert "limit" not in text


def test_compact_tool_result_success_hides_payload():
    """成功结果不展示完整 payload。"""
    text = compact_tool_result("python_execute", ok=True, detail="large payload")

    assert text == "<- python_execute ok"


def test_compact_tool_result_failure_truncates_error():
    """失败结果显示截断错误。"""
    text = compact_tool_result(
        "python_execute", ok=False, detail="error-" + "x" * 200, width=80
    )

    assert text.startswith("[!] python_execute ")
    assert len(text) <= 80
    assert text.endswith("...")


# ---------------- render_dag 纯函数 ----------------


def test_render_dag_linear():
    sop = {
        "entry_node": "n1",
        "nodes": [{"id": "n1", "name": "start"}, {"id": "n2", "name": "review"}],
        "edges": [{"from": "n1", "to": "n2"}],
    }
    nodes = {"n1": {"status": "completed"}, "n2": {"status": "running"}}
    result = render_dag(sop, nodes)
    assert "start" in result
    assert "review" in result
    assert "->" in result
    assert "[green]" in result
    assert "[blue]" in result
    assert "*" in result
    assert all(ord(ch) < 128 for ch in result)


def test_render_dag_none():
    """sop 为 None 时返回占位文本。"""
    assert render_dag(None, {}) == "(no DAG)"


def test_render_dag_default_pending():
    sop = {"entry_node": "n1", "nodes": [{"id": "n1", "name": "start"}], "edges": []}
    result = render_dag(sop, {})
    assert "start" in result
    assert "." in result


# ---------------- TUI 命令与首页 / SOP 详情渲染 ----------------


def test_parse_tui_command_plain_question():
    """普通文本应进入默认问答，而不是被当作命令。"""
    command = parse_tui_command("帮我解释一下这个 SOP")

    assert command.name == "ask"
    assert command.args == ["帮我解释一下这个 SOP"]
    assert command.raw == "帮我解释一下这个 SOP"


def test_parse_tui_command_sop_start():
    """/sop start <id> 应解析成 sop_start 命令。"""
    command = parse_tui_command("/sop start ad_copy")

    assert command.name == "sop_start"
    assert command.args == ["ad_copy"]
    assert command.raw == "/sop start ad_copy"


def test_parse_tui_command_sop_running():
    """/sop running 应解析成运行中任务列表命令。"""
    command = parse_tui_command("/sop running")

    assert command.name == "sop_running"
    assert command.args == []


def test_parse_retry_answer_logs_commands():
    """TUI 应解析 SOP 纠偏和确认相关命令。"""
    retry = parse_tui_command("/retry step1 focus on price")
    answer = parse_tui_command('/answer int-1 {"approved": true}')
    logs = parse_tui_command("/logs task-1")

    assert retry.name == "retry"
    assert retry.args == ["step1", "focus on price"]
    assert answer.name == "answer"
    assert answer.args == ["int-1", '{"approved": true}']
    assert logs.name == "logs"
    assert logs.args == ["task-1"]


def test_parse_tui_command_unknown_slash_command():
    """未知 slash 命令应可识别，方便 UI 展示帮助。"""
    command = parse_tui_command("/unknown value")

    assert command.name == "unknown"
    assert command.args == ["/unknown", "value"]


def test_render_home_is_ascii_start_page():
    """首页应展示固定宽度双栏 ASCII 欢迎卡片。"""
    text = render_home("http://127.0.0.1:8900")
    lines = text.splitlines()

    assert all(ord(ch) < 128 for ch in text)
    assert len(lines) >= 10
    assert lines[0].startswith("+")
    assert lines[0].endswith("+")
    assert set(lines[0]) <= {"+", "-"}
    assert lines[-1] == lines[0]
    assert all(len(line) == len(lines[0]) for line in lines)
    assert any(" | Connection" in line for line in lines)
    assert any("-" * 30 in line for line in lines)
    assert "S Y M P H O N Y" in text
    assert "Stand a little taller." in text
    assert "\\______/" in text
    assert "Connection" in text
    assert "Start" in text
    assert "Commands" in text
    assert "http://127.0.0.1:8900" in text
    assert "/sop" in text
    assert "/web" in text
    assert "/copy" in text


def test_command_help_lists_core_commands():
    """帮助文本应列出首页和 SOP 相关命令。"""
    text = command_help()

    assert "/sop" in text
    assert "/home" in text
    assert "/web" in text
    assert "/sop start <id>" in text
    assert "/sop running" in text
    assert "/retry <node> <text>" in text
    assert "/answer <id> <json>" in text
    assert "/logs <task_id>" in text


def test_is_complex_variables_def_for_simple_fields():
    """text/document/simple json 字段应允许 TUI 轻量表单启动。"""
    fields = [
        {"name": "product_name", "type": "text", "required": True},
        {"name": "product_doc", "type": "document", "required": True},
        {
            "name": "metadata",
            "type": "json",
            "json_schema": {
                "type": "object",
                "properties": {
                    "brand": {"type": "string"},
                    "score": {"type": "number"},
                },
            },
        },
    ]

    assert is_complex_variables_def(fields) is False


def test_is_complex_variables_def_for_nested_json():
    """嵌套 object/array 字段应引导到 Web，而不是要求用户手写复杂 JSON。"""
    fields = [
        {
            "name": "payload",
            "type": "json",
            "json_schema": {
                "type": "object",
                "properties": {
                    "items": {"type": "array", "items": {"type": "object"}}
                },
            },
        }
    ]

    assert is_complex_variables_def(fields) is True


def test_coerce_tui_field_value_text_document_and_number():
    """TUI 表单输入应按字段类型做轻量转换。"""
    assert coerce_tui_field_value({"type": "text"}, "hello") == "hello"
    assert coerce_tui_field_value({"type": "document"}, "/tmp/a.docx") == "/tmp/a.docx"
    assert (
        coerce_tui_field_value(
            {"type": "json", "json_schema": {"type": "number"}}, "1.5"
        )
        == 1.5
    )
    assert (
        coerce_tui_field_value(
            {"type": "json", "json_schema": {"type": "integer"}}, "2"
        )
        == 2
    )
    assert (
        coerce_tui_field_value(
            {"type": "json", "json_schema": {"type": "boolean"}}, "true"
        )
        is True
    )


def test_render_sop_detail_shows_start_and_web_guidance():
    """SOP 详情应展示变量、启动入口和 Web 引导。"""
    sop = {
        "id": "ad_copy",
        "name": "广告营销文案三步法",
        "description": "生成广告文案",
        "variables_def": [
            {
                "name": "product_name",
                "label": "产品名称",
                "type": "text",
                "required": True,
            }
        ],
    }

    text = render_sop_detail(sop, "http://127.0.0.1:8900")

    assert "广告营销文案三步法" in text
    assert "product_name" in text
    assert "/sop start ad_copy" in text
    assert "http://127.0.0.1:8900" in text


# ---------------- ServerClient（httpx_mock） ----------------


@pytest.mark.asyncio
async def test_server_client_list_tasks(httpx_mock):
    """list_tasks 应发起 GET /api/tasks 并返回其 JSON 数组。"""
    payload = [{"task_id": "t-1", "sop_id": "s-1", "status": "running"}]
    httpx_mock.add_response(
        url="http://127.0.0.1:8899/api/tasks", method="GET", json=payload
    )
    client = ServerClient("http://127.0.0.1:8899")
    result = await client.list_tasks()
    assert result == payload
    await client.close()


@pytest.mark.asyncio
async def test_server_client_direct_constructor_keeps_default_timeout():
    """ServerClient 直接构造时应保持原有 30 秒默认 timeout。"""
    client = ServerClient("http://127.0.0.1:8899")

    assert client._client.timeout.connect == 30.0
    await client.close()


@pytest.mark.asyncio
async def test_server_client_from_config_uses_client_timeout(tmp_path):
    """ServerClient 从配置构造时应使用 client.http_timeout_seconds。"""
    config = SymphonyConfig(
        llm=LLMConfig(
            provider="doubao",
            api_key="test",
            model="doubao-test",
            base_url="https://example.com/api/v3",
        ),
        server=ServerConfig(),
        storage=StorageConfig(
            workspace_dir=str(tmp_path / "workspaces"),
            templates_dir=str(tmp_path / "templates"),
            custom_skills_dir=str(tmp_path / "skills"),
        ),
        client=ClientConfig(http_timeout_seconds=4.5),
    )

    client = ServerClient.from_config(config, "http://127.0.0.1:8899")

    assert client._client.timeout.connect == 4.5
    await client.close()


@pytest.mark.asyncio
async def test_start_task(httpx_mock):
    """start_task 应 POST /api/tasks 并返回 {task_id}。"""
    httpx_mock.add_response(
        url="http://127.0.0.1:8899/api/tasks",
        method="POST",
        json={"task_id": "t-1"},
    )
    client = ServerClient("http://127.0.0.1:8899")
    result = await client.start_task("s-1", {"k": "v"})
    assert result == {"task_id": "t-1"}
    await client.close()


@pytest.mark.asyncio
async def test_intervene(httpx_mock):
    """intervene 应 POST /api/tasks/{id}/intervene 并返回 {ok:true}。"""
    httpx_mock.add_response(
        url="http://127.0.0.1:8899/api/tasks/t-1/intervene",
        method="POST",
        json={"ok": True},
    )
    client = ServerClient("http://127.0.0.1:8899")
    result = await client.intervene("t-1", "n1", "retry", {})
    assert result == {"ok": True}
    await client.close()


@pytest.mark.asyncio
async def test_retry_subnode(httpx_mock):
    """retry_subnode 应 POST 子节点重跑端点并携带 retry_prompt。"""
    httpx_mock.add_response(
        url="http://127.0.0.1:8899/api/tasks/t-1/nodes/develop/subnodes/table_b/retry",
        method="POST",
        json={"ok": True},
    )
    client = ServerClient("http://127.0.0.1:8899")
    result = await client.retry_subnode(
        "t-1", "develop", "table_b", "修复 status 字段"
    )
    assert result == {"ok": True}
    request = httpx_mock.get_request()
    assert request is not None
    assert json.loads(request.read()) == {"retry_prompt": "修复 status 字段"}
    await client.close()


@pytest.mark.asyncio
async def test_rerun_node(httpx_mock):
    """rerun_node 应 POST 主节点重跑端点并携带补充指令。"""
    httpx_mock.add_response(
        url="http://127.0.0.1:8899/api/tasks/t-1/nodes/step1/rerun",
        method="POST",
        json={"ok": True},
    )
    client = ServerClient("http://127.0.0.1:8899")
    result = await client.rerun_node("t-1", "step1", "focus on price")
    assert result == {"ok": True}
    request = httpx_mock.get_request()
    assert request is not None
    assert json.loads(request.read()) == {
        "supplemental_instruction": "focus on price",
        "invalidate_downstream": True,
    }
    await client.close()


@pytest.mark.asyncio
async def test_answer_interaction(httpx_mock):
    """answer_interaction 应 POST interaction answer 端点。"""
    httpx_mock.add_response(
        url="http://127.0.0.1:8899/api/tasks/t-1/interactions/int-1/answer",
        method="POST",
        json={"ok": True},
    )
    client = ServerClient("http://127.0.0.1:8899")
    result = await client.answer_interaction("t-1", "int-1", {"approved": True})
    assert result == {"ok": True}
    request = httpx_mock.get_request()
    assert request is not None
    assert json.loads(request.read()) == {"answer": {"approved": True}}
    await client.close()


@pytest.mark.asyncio
async def test_chat(httpx_mock):
    """ServerClient.chat 应 POST /api/chat 并返回 answer。"""
    httpx_mock.add_response(
        url="http://127.0.0.1:8899/api/chat",
        method="POST",
        json={"answer": "hello"},
    )
    client = ServerClient("http://127.0.0.1:8899")

    result = await client.chat("hi", [{"role": "user", "content": "before"}])

    assert result == "hello"
    request = httpx_mock.get_request()
    assert request is not None
    assert request.read() == (
        b'{"question":"hi","history":[{"role":"user","content":"before"}]}'
    )
    await client.close()


@pytest.mark.asyncio
async def test_chat_formats_json_data(httpx_mock):
    """ServerClient.chat 应把后端 data 字段格式化展示。"""
    httpx_mock.add_response(
        url="http://127.0.0.1:8899/api/chat",
        method="POST",
        json={"answer": "按你的要求返回 JSON：", "data": {"name": "Symphony", "ok": True}},
    )
    client = ServerClient("http://127.0.0.1:8899")

    result = await client.chat("返回 JSON", [])

    assert "按你的要求返回 JSON：" in result
    assert '"name": "Symphony"' in result
    assert '"ok": true' in result
    await client.close()


@pytest.mark.asyncio
async def test_chat_error_uses_server_detail(httpx_mock):
    """ServerClient.chat 应把后端 detail 转成可读异常。"""
    httpx_mock.add_response(
        url="http://127.0.0.1:8899/api/chat",
        method="POST",
        status_code=400,
        json={"detail": "缺少 LLM API Key：请设置 ARK_API_KEY"},
    )
    client = ServerClient("http://127.0.0.1:8899")

    with pytest.raises(RuntimeError, match="ARK_API_KEY"):
        await client.chat("hi", [])

    await client.close()


@pytest.mark.asyncio
async def test_server_client_create_chat_session(httpx_mock):
    """ServerClient.create_chat_session 应调用 /api/chat/sessions。"""
    httpx_mock.add_response(
        method="POST",
        url="http://127.0.0.1:8899/api/chat/sessions",
        json={
            "session_id": "chat-20260710-abcd1234",
            "type": "chat",
            "title": "hello",
            "status": "running",
            "created_at": "2026-07-10T00:00:00Z",
            "updated_at": "2026-07-10T00:00:00Z",
            "source": "tui",
        },
    )
    client = ServerClient("http://127.0.0.1:8899")

    result = await client.create_chat_session("hello")

    assert result["session_id"] == "chat-20260710-abcd1234"
    request = httpx_mock.get_request()
    assert request is not None
    assert json.loads(request.read()) == {"title": "hello", "source": "tui"}
    await client.close()


def test_ws_url():
    """ws_url 应把 http 前缀替换为 ws 并携带 task_id 查询参数。"""
    client = ServerClient("http://127.0.0.1:8899")
    url = client.ws_url("t-1")
    assert url == "ws://127.0.0.1:8899/ws?task_id=t-1"


def test_chat_socket_url():
    """ChatSocket 应把 http 前缀替换为 ws 并指向 /ws/chat。"""
    socket = ChatSocket("http://127.0.0.1:8900")
    assert socket.ws_url == "ws://127.0.0.1:8900/ws/chat"


def test_chat_socket_url_for_session_id():
    """ChatSocket 应能为 session-aware WebSocket 追加 session_id。"""
    socket = ChatSocket("http://127.0.0.1:8899")

    assert socket.url_for(None) == "ws://127.0.0.1:8899/ws/chat"
    assert socket.url_for("chat id") == (
        "ws://127.0.0.1:8899/ws/chat?session_id=chat%20id"
    )


@pytest.mark.asyncio
async def test_chat_socket_trims_history_before_websocket_send(monkeypatch):
    """ChatSocket 发送 /ws/chat 前应按配置裁剪历史上下文。"""
    sent_payloads = []

    class FakeWebSocket:
        """最小 WebSocket async context manager，记录 send payload。"""

        def __init__(self):
            self._events = iter(['{"type": "chat_completed", "answer": "ok"}'])

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def send(self, payload):
            sent_payloads.append(json.loads(payload))

        def __aiter__(self):
            return self

        async def __anext__(self):
            try:
                return next(self._events)
            except StopIteration as exc:
                raise StopAsyncIteration from exc

    monkeypatch.setattr(tui_client.websockets, "connect", lambda url: FakeWebSocket())
    history = [
        {"role": "user", "content": "u1"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "u2"},
        {"role": "assistant", "content": "a2"},
    ]
    socket = ChatSocket("http://127.0.0.1:8899", history_limit=2)

    events = [event async for event in socket.stream("now", history)]

    assert events == [{"type": "chat_completed", "answer": "ok"}]
    assert sent_payloads == [
        {
            "question": "now",
            "history": [
                {"role": "user", "content": "u2"},
                {"role": "assistant", "content": "a2"},
            ],
        }
    ]
    assert len(history) == 4


# ---------------- app 可导入 / 可实例化 ----------------


class FakeChatSocket:
    """测试用流式 chat socket。"""

    def __init__(self, events):
        self.events = events
        self.calls = []

    async def stream(self, question, history, session_id=None):
        self.calls.append(
            {"question": question, "history": history, "session_id": session_id}
        )
        for event in self.events:
            yield event


class FakeSessionClient:
    """测试用 Chat session 创建客户端。"""

    def __init__(self, session_id="chat-20260710-abcd1234", error=None):
        self.session_id = session_id
        self.error = error
        self.titles = []

    async def create_chat_session(self, title):
        self.titles.append(title)
        if self.error is not None:
            raise self.error
        return {"session_id": self.session_id}

    async def close(self):
        """兼容 SymphonyTUI.on_unmount 的客户端关闭流程。"""


class FakeCorrectionClient:
    """测试用 SOP 纠偏客户端。"""

    def __init__(self):
        self.reruns = []
        self.answers = []

    async def rerun_node(self, task_id, node_id, instruction):
        self.reruns.append(
            {"task_id": task_id, "node_id": node_id, "instruction": instruction}
        )
        return {"ok": True}

    async def answer_interaction(self, task_id, interaction_id, answer):
        self.answers.append(
            {"task_id": task_id, "interaction_id": interaction_id, "answer": answer}
        )
        return {"ok": True}

    async def close(self):
        """兼容 SymphonyTUI.on_unmount 的客户端关闭流程。"""


def test_app_importable():
    """SymphonyTUI 与 run_tui 可导入，且 App 可实例化（不启动事件循环）。"""
    from symphony.tui.app import SymphonyTUI, run_tui

    app = SymphonyTUI("http://127.0.0.1:8899")
    # 实例化后基础状态就位
    assert app.base_url == "http://127.0.0.1:8899"
    assert app.active_task_id is None
    assert callable(run_tui)


def test_tui_initializes_in_home_mode():
    """TUI 默认应进入 home 模式，而不是任务监控模式。"""
    from symphony.tui.app import SymphonyTUI

    app = SymphonyTUI("http://127.0.0.1:8899")

    assert app.mode == "home"
    assert app.chat_history == []
    assert app.base_url == "http://127.0.0.1:8899"


def test_input_history_navigation():
    """上下键应在用户提交过的问题间循环回填输入。"""
    from symphony.tui.app import SymphonyTUI

    app = SymphonyTUI("http://127.0.0.1:8899")
    app._record_history("first")
    app._record_history("second")
    # 从草稿态起，上键回到最近一条
    assert app._history_prev("draft") == "second"
    assert app._history_prev("second") == "first"
    # 到顶后保持在最早一条
    assert app._history_prev("first") == "first"
    # 下键前进，越过最新回到空草稿
    assert app._history_next() == "second"
    assert app._history_next() == ""


def test_input_history_limit_keeps_recent_entries():
    """输入历史应按 TUI 配置只保留最近 N 条。"""
    from symphony.tui.app import SymphonyTUI

    app = SymphonyTUI("http://127.0.0.1:8899", input_history_limit=2)
    app._record_history("first")
    app._record_history("second")
    app._record_history("third")

    assert app._input_history == ["second", "third"]
    assert app._history_prev("draft") == "third"
    assert app._history_prev("third") == "second"


def test_copy_text_uses_clipboard(monkeypatch):
    """/copy 应把最后一条回答写入剪贴板。"""
    from symphony.tui.app import SymphonyTUI

    app = SymphonyTUI("http://127.0.0.1:8899")
    app._last_answer = "final answer"
    captured = {}
    monkeypatch.setattr(app, "copy_to_clipboard", lambda text: captured.update(text=text))
    app._copy_last_answer()
    assert captured["text"] == "final answer"


@pytest.mark.asyncio
async def test_stream_view_hidden_until_streaming():
    """初始 home 模式下流式视图应存在且默认隐藏。"""
    from symphony.tui.app import SymphonyTUI

    app = SymphonyTUI("http://127.0.0.1:8899")
    async with app.run_test():
        assert app.query_one("#stream-view").display is False


@pytest.mark.asyncio
async def test_home_layout_hides_console_widgets():
    """home 模式应隐藏控制台部件，把主体空间留给聊天流。"""
    from textual.containers import VerticalScroll
    from symphony.tui.app import SymphonyTUI

    app = SymphonyTUI("http://127.0.0.1:8899")

    async with app.run_test():
        assert app.query_one("#primary-table").display is False
        assert app.query_one("#detail").display is False
        assert app.query_one("#log").display is False
        assert app.query_one("#chat-scroll", VerticalScroll).display is True


@pytest.mark.asyncio
async def test_home_uses_chat_scroll_for_welcome():
    """home 模式应把欢迎页放入聊天流，而不是固定在大标题区。"""
    from textual.containers import VerticalScroll
    from symphony.tui.app import SymphonyTUI

    app = SymphonyTUI("http://127.0.0.1:8899")

    async with app.run_test():
        chat_scroll = app.query_one("#chat-scroll", VerticalScroll)
        assert chat_scroll.display is True
        assert app.query_one("#primary-table").display is False
        assert app.query_one("#detail").display is False
        assert app.query_one("#log").display is False
        assert app.query_one("#stream-view").display is False
        assert "Symphony | Ask anything | /sop for SOP console" in str(
            app.query_one("#main-title").render()
        )
        assert any(
            "S Y M P H O N Y" in str(child.render())
            for child in chat_scroll.children
        )


@pytest.mark.asyncio
async def test_console_layout_hides_home_chat_scroll():
    """进入 SOP 控制台时应隐藏 home 聊天流并恢复控制台部件。"""
    from textual.containers import VerticalScroll
    from symphony.tui.app import SymphonyTUI

    app = SymphonyTUI("http://127.0.0.1:8899")

    async with app.run_test():
        app._set_console_layout()
        assert app.query_one("#chat-scroll", VerticalScroll).display is False
        assert app.query_one("#primary-table").display is True
        assert app.query_one("#detail").display is True
        assert app.query_one("#log").display is True


@pytest.mark.asyncio
async def test_home_command_feedback_uses_chat_stream():
    """home 模式下 /help 等反馈应写入聊天流，而不是隐藏的 RichLog。"""
    from textual.containers import VerticalScroll
    from symphony.tui.app import SymphonyTUI

    app = SymphonyTUI("http://127.0.0.1:8899")

    async with app.run_test():
        chat_scroll = app.query_one("#chat-scroll", VerticalScroll)
        before = len(chat_scroll.children)
        await app._handle_user_input("/help")
        assert len(chat_scroll.children) == before + 1
        assert "Commands:" in str(chat_scroll.children[-1].render())


@pytest.mark.asyncio
async def test_retry_command_requires_active_task():
    """/retry 没有 active_task_id 时应提示，不发请求。"""
    from textual.widgets import Static
    from symphony.tui.app import SymphonyTUI

    app = SymphonyTUI("http://127.0.0.1:8899")
    client = FakeCorrectionClient()
    app.client = client

    async with app.run_test():
        await app._handle_user_input("/retry step1 focus on price")
        texts = [
            str(widget.render())
            for widget in app.query_one("#chat-scroll").query(Static)
        ]

    assert client.reruns == []
    assert any("No active task." in text for text in texts)


@pytest.mark.asyncio
async def test_retry_command_calls_rerun_node_and_writes_feedback():
    """/retry 应对 active_task_id 发起主节点重跑并写简短反馈。"""
    from textual.widgets import Static
    from symphony.tui.app import SymphonyTUI

    app = SymphonyTUI("http://127.0.0.1:8899")
    client = FakeCorrectionClient()
    app.client = client
    app.active_task_id = "task-1"

    async with app.run_test():
        await app._handle_user_input("/retry step1 focus on price")
        texts = [
            str(widget.render())
            for widget in app.query_one("#chat-scroll").query(Static)
        ]

    assert client.reruns == [
        {
            "task_id": "task-1",
            "node_id": "step1",
            "instruction": "focus on price",
        }
    ]
    assert any("Rerun requested: step1" in text for text in texts)


@pytest.mark.asyncio
async def test_answer_command_parses_json_or_wraps_text():
    """/answer 应提交 JSON；解析失败时包装为 text 字段。"""
    from textual.widgets import Static
    from symphony.tui.app import SymphonyTUI

    app = SymphonyTUI("http://127.0.0.1:8899")
    client = FakeCorrectionClient()
    app.client = client
    app.active_task_id = "task-1"

    async with app.run_test():
        await app._handle_user_input('/answer int-1 {"approved": true}')
        await app._handle_user_input("/answer int-2 please continue")
        texts = [
            str(widget.render())
            for widget in app.query_one("#chat-scroll").query(Static)
        ]

    assert client.answers == [
        {
            "task_id": "task-1",
            "interaction_id": "int-1",
            "answer": {"approved": True},
        },
        {
            "task_id": "task-1",
            "interaction_id": "int-2",
            "answer": {"text": "please continue"},
        },
    ]
    assert any("Answer submitted: int-1" in text for text in texts)
    assert any("Answer submitted: int-2" in text for text in texts)


@pytest.mark.asyncio
async def test_logs_command_writes_web_log_hint():
    """/logs 应写出 Web DAG log 提示。"""
    from textual.widgets import Static
    from symphony.tui.app import SymphonyTUI

    app = SymphonyTUI("http://127.0.0.1:8899")

    async with app.run_test():
        await app._handle_user_input("/logs task-1")
        texts = [
            str(widget.render())
            for widget in app.query_one("#chat-scroll").query(Static)
        ]

    assert any("Open Web Logs for task: task-1" in text for text in texts)


@pytest.mark.asyncio
async def test_ask_agent_appends_markdown_answer_to_chat_stream():
    """助手回答应以 Markdown 部件追加到 home 聊天流。"""
    from textual.widgets import Markdown
    from symphony.tui.app import SymphonyTUI

    app = SymphonyTUI("http://127.0.0.1:8899")
    app.client = FakeSessionClient()
    app.chat_socket = FakeChatSocket(
        [
            {"type": "chat_answer_delta", "text": "# Title\n\n"},
            {"type": "chat_answer_delta", "text": "- item"},
            {"type": "chat_completed", "answer": "# Title\n\n- item"},
        ]
    )

    async with app.run_test():
        await app._ask_agent("show markdown")
        markdowns = list(app.query(Markdown))
        assert markdowns
        assert app._last_answer == "# Title\n\n- item"
        assert app.chat_history[-1] == {
            "role": "assistant",
            "content": "# Title\n\n- item",
        }


@pytest.mark.asyncio
async def test_chat_failed_appends_status_message():
    """流式失败时应把错误写入聊天流，而不是写到隐藏的 RichLog。"""
    from textual.widgets import Static
    from symphony.tui.app import SymphonyTUI

    app = SymphonyTUI("http://127.0.0.1:8899")
    app.client = FakeSessionClient()
    app.chat_socket = FakeChatSocket(
        [{"type": "chat_failed", "error": "provider down"}]
    )

    async with app.run_test():
        await app._ask_agent("fail please")
        texts = [
            str(widget.render())
            for widget in app.query_one("#chat-scroll").query(Static)
        ]
        assert any("Chat failed: provider down" in text for text in texts)


@pytest.mark.asyncio
async def test_ask_agent_shows_tool_call_and_result_summary():
    """工具调用和结果摘要应写入 home 聊天流，便于用户确认工具确实执行。"""
    from textual.widgets import Static
    from symphony.tui.app import SymphonyTUI

    app = SymphonyTUI("http://127.0.0.1:8899")
    app.client = FakeSessionClient()
    app.chat_socket = FakeChatSocket(
        [
            {"type": "chat_thinking", "content": "calling tools"},
            {
                "type": "chat_tool_call",
                "skill_name": "file_read",
                "args": {"path": "/tmp/a.txt"},
            },
            {
                "type": "chat_tool_result",
                "skill_name": "file_read",
                "ok": True,
                "detail": '{"size": 3}',
            },
            {"type": "chat_answer_delta", "text": "done"},
            {"type": "chat_completed", "answer": "done"},
        ]
    )

    async with app.run_test():
        await app._ask_agent("read file")
        texts = [
            str(widget.render())
            for widget in app.query_one("#chat-scroll").query(Static)
        ]
        assert any('-> file_read path="/tmp/a.txt"' in text for text in texts)
        assert any("<- file_read ok" in text for text in texts)


@pytest.mark.asyncio
async def test_ask_agent_places_answer_after_tool_messages():
    """工具状态应出现在最终回答之前，且冗余“调用 N 个工具”不单独展示。"""
    from textual.widgets import Markdown, Static
    from symphony.tui.app import SymphonyTUI

    app = SymphonyTUI("http://127.0.0.1:8899")
    app.client = FakeSessionClient()
    app.chat_socket = FakeChatSocket(
        [
            {"type": "chat_thinking", "content": "调用 1 个工具"},
            {
                "type": "chat_tool_call",
                "skill_name": "skill_reference_search",
                "args": {"query": "所有skill", "limit": 20},
            },
            {
                "type": "chat_tool_result",
                "skill_name": "skill_reference_search",
                "ok": True,
                "detail": '{"items": []}',
            },
            {"type": "chat_answer_delta", "text": "final answer"},
            {"type": "chat_completed", "answer": "final answer"},
        ]
    )

    async with app.run_test():
        await app._ask_agent("search skills")
        children = list(app.query_one("#chat-scroll").children)
        rendered = [str(child.render()) for child in children]

        assert not any("调用 1 个工具" in text for text in rendered)
        tool_index = next(i for i, text in enumerate(rendered) if "-> skill_reference_search" in text)
        result_index = next(i for i, text in enumerate(rendered) if "<- skill_reference_search ok" in text)
        answer_index = next(i for i, child in enumerate(children) if isinstance(child, Markdown))
        assert tool_index < result_index < answer_index
        assert any(isinstance(child, Static) for child in children)


@pytest.mark.asyncio
async def test_ask_agent_creates_chat_session_and_passes_id_to_stream():
    """_ask_agent 应先创建 Chat session，并把 session_id 传给 stream。"""
    from symphony.tui.app import SymphonyTUI

    question = "x" * 60
    app = SymphonyTUI("http://127.0.0.1:8899")
    app.client = FakeSessionClient("chat-20260710-session")
    app.chat_socket = FakeChatSocket(
        [
            {"type": "chat_answer_delta", "text": "done"},
            {"type": "chat_completed", "answer": "done"},
        ]
    )

    async with app.run_test():
        await app._ask_agent(question)

    assert app.client.titles == ["x" * 40]
    assert app.chat_socket.calls == [
        {"question": question, "history": [], "session_id": "chat-20260710-session"}
    ]


@pytest.mark.asyncio
async def test_ask_agent_keeps_configured_chat_context_history():
    """TUI Chat 上下文历史应按配置保留，并用于下一次 WebSocket 请求。"""
    from symphony.tui.app import SymphonyTUI

    app = SymphonyTUI("http://127.0.0.1:8899", chat_context_history_limit=2)
    app.client = FakeSessionClient("chat-session")
    app.chat_socket = FakeChatSocket(
        [
            {"type": "chat_answer_delta", "text": "answer one"},
            {"type": "chat_completed", "answer": "answer one"},
        ]
    )

    async with app.run_test():
        await app._ask_agent("first")
        first_socket = app.chat_socket
        app.chat_socket = FakeChatSocket(
            [
                {"type": "chat_answer_delta", "text": "answer two"},
                {"type": "chat_completed", "answer": "answer two"},
            ]
        )
        await app._ask_agent("second")
        second_socket = app.chat_socket

    assert first_socket.calls == [
        {"question": "first", "history": [], "session_id": "chat-session"}
    ]
    assert second_socket.calls == [
        {
            "question": "second",
            "history": [
                {"role": "user", "content": "first"},
                {"role": "assistant", "content": "answer one"},
            ],
            "session_id": "chat-session",
        }
    ]
    assert app.chat_history == [
        {"role": "user", "content": "second"},
        {"role": "assistant", "content": "answer two"},
    ]


@pytest.mark.asyncio
async def test_ask_agent_falls_back_when_chat_session_creation_fails():
    """创建 Chat session 失败时应提示并回退到无 session 流程。"""
    from textual.widgets import Static
    from symphony.tui.app import SymphonyTUI

    app = SymphonyTUI("http://127.0.0.1:8899")
    app.client = FakeSessionClient(error=RuntimeError("storage down"))
    app.chat_socket = FakeChatSocket(
        [
            {"type": "chat_answer_delta", "text": "fallback"},
            {"type": "chat_completed", "answer": "fallback"},
        ]
    )

    async with app.run_test():
        await app._ask_agent("hi")
        texts = [
            str(widget.render())
            for widget in app.query_one("#chat-scroll").query(Static)
        ]

    assert any("Chat log disabled: storage down" in text for text in texts)
    assert app.chat_socket.calls == [
        {"question": "hi", "history": [], "session_id": None}
    ]


def test_tui_keeps_task_monitoring_state():
    """重构后仍应保留任务监控所需状态。"""
    from symphony.tui.app import SymphonyTUI

    app = SymphonyTUI("http://127.0.0.1:8899")

    assert app.active_task_id is None
    assert app.snapshot is None
    assert app.sop is None
    assert app.selected_node_id is None


def test_tui_has_sop_mode_renderer():
    """TUI 应提供 SOP 控制台渲染入口。"""
    from symphony.tui.app import SymphonyTUI

    app = SymphonyTUI("http://127.0.0.1:8899")

    assert hasattr(app, "_render_sop_console")
    assert hasattr(app, "_select_sop")
