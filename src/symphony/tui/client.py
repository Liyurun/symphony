"""TUI 客户端的纯逻辑层（不依赖 Textual）。

本模块封装两类能力：

- ``ServerClient``：对后端 REST 端点的异步封装，内部持有一个
  ``httpx.AsyncClient``。网络是边界，允许调用方感知异常（raise_for_status）。
- 若干**纯函数**（``apply_event`` / ``event_summary`` / ``render_dag`` /
  ``compact_tool_call`` / ``compact_tool_result``），
  它们不做任何 IO，仅根据输入计算输出，是本模块最核心、最易测的部分，
  其节点联动逻辑与 Web 前端 store.appendEvent 保持一致。
"""

from dataclasses import dataclass
import json
from typing import Any, TypeVar
from urllib.parse import quote

import httpx
import websockets


BRAND_TAGLINE = "Stand a little taller."
_T = TypeVar("_T")


def trim_history(history: list[_T], limit: int) -> list[_T]:
    """按保留数量返回最近的历史消息副本；limit 为 0 时不保留历史。"""
    safe_limit = max(0, int(limit))
    if safe_limit == 0:
        return []
    return list(history[-safe_limit:])


@dataclass(frozen=True)
class TuiCommand:
    """解析后的 TUI 输入命令。"""

    name: str
    args: list[str]
    raw: str


def parse_tui_command(raw: str) -> TuiCommand:
    """把 TUI 输入解析成命令；普通文本进入 ask。"""
    text = raw.strip()
    if not text:
        return TuiCommand(name="empty", args=[], raw=raw)
    if not text.startswith("/"):
        return TuiCommand(name="ask", args=[text], raw=raw)

    parts = text.split()
    head = parts[0].lower()
    tail = parts[1:]

    if head == "/home":
        return TuiCommand(name="home", args=tail, raw=raw)
    if head == "/web":
        return TuiCommand(name="web", args=tail, raw=raw)
    if head == "/help":
        return TuiCommand(name="help", args=tail, raw=raw)
    if head == "/copy":
        return TuiCommand(name="copy", args=tail, raw=raw)
    if head == "/exit":
        return TuiCommand(name="exit", args=tail, raw=raw)
    if head == "/retry" and len(tail) >= 2:
        return TuiCommand(name="retry", args=[tail[0], " ".join(tail[1:])], raw=raw)
    if head == "/answer" and len(tail) >= 2:
        return TuiCommand(name="answer", args=[tail[0], " ".join(tail[1:])], raw=raw)
    if head == "/logs" and tail:
        return TuiCommand(name="logs", args=[tail[0]], raw=raw)
    if head == "/sop":
        if not tail:
            return TuiCommand(name="sop", args=[], raw=raw)
        sub = tail[0].lower()
        if sub == "start" and len(tail) >= 2:
            return TuiCommand(name="sop_start", args=[tail[1]], raw=raw)
        if sub == "running":
            return TuiCommand(name="sop_running", args=tail[1:], raw=raw)
        if sub == "list":
            return TuiCommand(name="sop_list", args=tail[1:], raw=raw)
        return TuiCommand(name="sop", args=tail, raw=raw)

    return TuiCommand(name="unknown", args=parts, raw=raw)


def command_help() -> str:
    """返回 TUI 内置命令帮助。"""
    return "\n".join(
        [
            "Commands:",
            "  /home              return to chat",
            "  /sop               enter SOP console",
            "  /sop list          list SOP templates",
            "  /sop start <id>    start a SOP",
            "  /sop running       show running SOP tasks",
            "  /retry <node> <text>  rerun node with instruction",
            "  /answer <id> <json>   answer pending interaction",
            "  /logs <task_id>       show Web DAG log hint",
            "  /copy              copy last answer",
            "  /web               print Web UI URL",
            "  /help              show help",
            "  /exit              quit TUI",
        ]
    )


def render_home(base_url: str) -> str:
    """渲染固定宽度双栏 ASCII 欢迎卡片。"""
    left_width = 43
    right_width = 38
    border = "+" + "-" * (left_width + right_width + 4) + "+"

    def row(left: str = "", right: str = "") -> str:
        """渲染一行左右双栏内容，超长内容按列宽截断。"""
        return (
            "| "
            + left[:left_width].ljust(left_width)
            + "| "
            + right[:right_width].ljust(right_width)
            + " |"
        )

    def right_rule() -> str:
        """渲染右侧信息栏分隔线。"""
        return row("", "-" * right_width)

    lines = [
        border,
        row("Symphony", "Connection"),
        row("", f"Server: {base_url}"),
        row("             |\\", f"Web:    {base_url}"),
        right_rule(),
        row("             | \\", "Start"),
        row("             |  \\", "Ask anything, then press Enter."),
        row("          ___|___\\_", "Welcome scrolls away as you chat."),
        right_rule(),
        row("         \\        /", "Commands"),
        row("      ~~~~\\______/~~~~", "/sop   SOP console   /copy copy last"),
        row("", "/web   Web URL       /help commands"),
        row("       S Y M P H O N Y", ""),
        row(f"    {BRAND_TAGLINE}", ""),
        border,
    ]
    return "\n".join(lines)


def _json_schema_is_complex(schema: dict[str, Any] | None) -> bool:
    """判断 JSON schema 是否超出 TUI 轻量表单能力。"""
    if not schema:
        return False
    if schema.get("type") != "object":
        return schema.get("type") in {"array"}
    properties = schema.get("properties", {})
    if not isinstance(properties, dict):
        return True
    for prop in properties.values():
        if not isinstance(prop, dict):
            return True
        if prop.get("type") in {"array", "object"}:
            return True
    return False


def is_complex_variables_def(fields: list[dict[str, Any]]) -> bool:
    """判断 variables_def 是否需要转到 Web UI。"""
    for field in fields:
        field_type = field.get("type")
        if field_type not in {"text", "document", "json"}:
            return True
        if field_type == "json" and _json_schema_is_complex(field.get("json_schema")):
            return True
    return False


def coerce_tui_field_value(field: dict[str, Any], raw: str) -> Any:
    """把 TUI 表单字符串转换成字段值。"""
    field_type = field.get("type")
    if field_type in {"text", "document"}:
        return raw
    schema = field.get("json_schema") or {}
    schema_type = schema.get("type")
    if schema_type == "number":
        return float(raw)
    if schema_type == "integer":
        return int(raw)
    if schema_type == "boolean":
        return raw.strip().lower() in {"true", "1", "yes", "y"}
    return raw


def render_sop_detail(sop: dict[str, Any] | None, base_url: str) -> str:
    """渲染 SOP 详情和操作提示。"""
    if sop is None:
        return "Select a SOP to view details."

    variables = sop.get("variables_def") or []
    lines = [
        f"{sop.get('name', sop.get('id', 'SOP'))}",
        f"ID: {sop.get('id', '')}",
        sop.get("description", ""),
        "",
        "Inputs:",
    ]
    if variables:
        for field in variables:
            required = "required" if field.get("required") else "optional"
            label = field.get("label") or field.get("name")
            lines.append(
                f"  - {field.get('name')} ({field.get('type')}, {required}) {label}"
            )
    else:
        lines.append("  - no variables")

    sop_id = sop.get("id", "")
    lines.extend(
        [
            "",
            f"Start: /sop start {sop_id}",
            f"Web:   {base_url}",
        ]
    )
    if is_complex_variables_def(variables):
        lines.append("This SOP has complex inputs. Use Web UI for startup.")
    return "\n".join(line for line in lines if line is not None)


class ChatSocket:
    """默认流式对话的 WebSocket 客户端。

    连接 /ws/chat，发送一帧 {question, history}，异步迭代返回的 chat 事件字典，
    直到收到 chat_completed / chat_failed。
    """

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:8899",
        history_limit: int = 24,
    ) -> None:
        """由 HTTP base_url 推导出 ws 地址。"""
        base = base_url.rstrip("/")
        self.ws_url = (
            base.replace("https://", "wss://").replace("http://", "ws://")
            + "/ws/chat"
        )
        self.history_limit = max(0, int(history_limit))

    def url_for(self, session_id: str | None = None) -> str:
        """返回 WebSocket URL；传入 session_id 时进入持久化 Chat session。"""
        if not session_id:
            return self.ws_url
        return f"{self.ws_url}?session_id={quote(session_id)}"

    async def stream(
        self, question: str, history: list[dict], session_id: str | None = None
    ):
        """连接并流式产出 chat 事件字典。"""
        async with websockets.connect(self.url_for(session_id)) as ws:
            await ws.send(
                json.dumps(
                    {
                        "question": question,
                        "history": trim_history(history, self.history_limit),
                    }
                )
            )
            async for message in ws:
                try:
                    event = json.loads(message)
                except (json.JSONDecodeError, TypeError):
                    continue
                yield event
                if event.get("type") in {"chat_completed", "chat_failed"}:
                    return


class ServerClient:
    """对 Symphony 后端 REST 端点的异步封装。"""

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:8899",
        timeout: float = 30.0,
    ) -> None:
        """保存 base_url 并创建内部异步 HTTP 客户端。

        :param base_url: 后端服务地址，默认本地 8899 端口。
        :param timeout: HTTP 请求超时时间（秒）。
        """
        # 去掉尾部斜杠，避免拼接出双斜杠
        self.base_url = base_url.rstrip("/")
        # 内部复用的异步 HTTP 客户端；base_url 交给 httpx 统一拼接
        self._client = httpx.AsyncClient(base_url=self.base_url, timeout=timeout)

    @classmethod
    def from_config(
        cls,
        config: Any,
        base_url: str = "http://127.0.0.1:8899",
    ) -> "ServerClient":
        """从 SymphonyConfig 或 ClientConfig 构造客户端。"""
        # 允许直接传顶层 config，也允许只传 config.client
        client_config = getattr(config, "client", config)
        return cls(base_url, timeout=client_config.http_timeout_seconds)

    async def list_tasks(self) -> list[dict]:
        """GET /api/tasks，返回全部任务的元信息数组。"""
        # 网络边界：请求失败时由 raise_for_status 抛出
        resp = await self._client.get("/api/tasks")
        resp.raise_for_status()
        return resp.json()

    async def get_task(self, task_id: str) -> dict:
        """GET /api/tasks/{id}，返回任务状态快照。"""
        resp = await self._client.get(f"/api/tasks/{task_id}")
        resp.raise_for_status()
        return resp.json()

    async def get_events(self, task_id: str, since: int = 0) -> list[dict]:
        """GET /api/tasks/{id}/events?since=N，返回事件字典数组。"""
        resp = await self._client.get(
            f"/api/tasks/{task_id}/events", params={"since": since}
        )
        resp.raise_for_status()
        return resp.json()

    async def get_traces(self, task_id: str) -> list[dict]:
        """GET /api/tasks/{id}/traces，返回 LLM 轨迹数组。"""
        resp = await self._client.get(f"/api/tasks/{task_id}/traces")
        resp.raise_for_status()
        return resp.json()

    async def list_sops(self) -> list[dict]:
        """GET /api/sops，返回全部 SOP 模板数组。"""
        resp = await self._client.get("/api/sops")
        resp.raise_for_status()
        return resp.json()

    async def start_task(self, sop_id: str, variables: dict) -> dict:
        """POST /api/tasks，按 SOP 启动一个新任务，返回 {task_id}。"""
        resp = await self._client.post(
            "/api/tasks", json={"sop_id": sop_id, "variables": variables}
        )
        resp.raise_for_status()
        return resp.json()

    async def create_chat_session(self, title: str) -> dict:
        """POST /api/chat/sessions，创建持久化 Chat session。"""
        resp = await self._client.post(
            "/api/chat/sessions",
            json={"title": title, "source": "tui"},
        )
        resp.raise_for_status()
        return resp.json()

    async def chat(self, question: str, history: list[dict]) -> str:
        """POST /api/chat，返回通用问答 answer。"""
        resp = await self._client.post(
            "/api/chat", json={"question": question, "history": history}
        )
        try:
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            try:
                detail = resp.json().get("detail")
            except ValueError:
                detail = None
            if detail:
                raise RuntimeError(detail) from exc
            raise
        payload = resp.json()
        answer = payload.get("answer", "")
        data = payload.get("data")
        if data is None:
            return answer
        formatted = json.dumps(data, ensure_ascii=False, indent=2)
        if answer:
            return f"{answer}\n\n{formatted}"
        return formatted

    async def intervene(
        self, task_id: str, node_id: str, action: str, data: dict
    ) -> dict:
        """POST /api/tasks/{id}/intervene，对任务施加一次人工干预。"""
        resp = await self._client.post(
            f"/api/tasks/{task_id}/intervene",
            json={"node_id": node_id, "action": action, "data": data},
        )
        resp.raise_for_status()
        return resp.json()

    async def confirm_subflow(
        self, task_id: str, node_id: str, nodes: list[dict], edges: list[dict]
    ) -> dict:
        """POST /api/tasks/{id}/nodes/{node}/subflow/confirm，确认子流程草案。"""
        resp = await self._client.post(
            f"/api/tasks/{task_id}/nodes/{node_id}/subflow/confirm",
            json={"nodes": nodes, "edges": edges},
        )
        resp.raise_for_status()
        return resp.json()

    async def retry_subnode(
        self, task_id: str, node_id: str, sub_node_id: str, retry_prompt: str
    ) -> dict:
        """POST 子节点重跑端点，使用本次输入的提示词覆盖重跑。"""
        resp = await self._client.post(
            f"/api/tasks/{task_id}/nodes/{node_id}/subnodes/{sub_node_id}/retry",
            json={"retry_prompt": retry_prompt},
        )
        resp.raise_for_status()
        return resp.json()

    async def rerun_node(self, task_id: str, node_id: str, instruction: str) -> dict:
        """POST /api/tasks/{task}/nodes/{node}/rerun。"""
        resp = await self._client.post(
            f"/api/tasks/{task_id}/nodes/{node_id}/rerun",
            json={
                "supplemental_instruction": instruction,
                "invalidate_downstream": True,
            },
        )
        resp.raise_for_status()
        return resp.json()

    async def answer_interaction(
        self, task_id: str, interaction_id: str, answer: dict
    ) -> dict:
        """POST /api/tasks/{task}/interactions/{id}/answer。"""
        resp = await self._client.post(
            f"/api/tasks/{task_id}/interactions/{interaction_id}/answer",
            json={"answer": answer},
        )
        resp.raise_for_status()
        return resp.json()

    async def close(self) -> None:
        """关闭内部 HTTP 客户端，释放连接资源。"""
        await self._client.aclose()

    def ws_url(self, task_id: str) -> str:
        """由 base_url 推导出订阅指定任务事件的 WebSocket 地址。"""
        # 把 http/https 前缀替换为 ws/wss
        ws_base = self.base_url.replace("https://", "wss://").replace(
            "http://", "ws://"
        )
        return f"{ws_base}/ws?task_id={task_id}"


# 节点状态改变类事件与其目标状态的映射（纯数据，供 apply_event 使用）
_NODE_STATUS_BY_EVENT = {
    "node_started": "running",
    "node_completed": "completed",
    "node_failed": "failed",
    "node_waiting_input": "waiting_input",
}


def apply_event(nodes: dict, event: dict) -> dict:
    """根据一条事件计算并返回节点状态字典的**不可变更新**副本。

    与 Web 前端 store.appendEvent 的节点联动逻辑保持一致：

    - node_status_changed → 目标节点 status = event["status"]
    - node_started/completed/failed/waiting_input → 设为对应状态，
      completed 额外写 output、failed 额外写 error
    - 其它事件 → 原样返回（不改 nodes）

    node_id 为空时安全跳过；node_id 存在但不在 nodes 中时，
    新建一个最小 entry。返回值总是新 dict（浅拷贝 nodes 与被改节点）。

    :param nodes: 现有节点状态字典（不会被修改）。
    :param event: 单条事件字典。
    :return: 更新后的新节点状态字典。
    """
    event_type = event.get("type", "")
    # node_status_changed 与四类节点事件才会改动 nodes
    is_status_changed = event_type == "node_status_changed"
    is_node_event = event_type in _NODE_STATUS_BY_EVENT
    # 非节点相关事件：原样返回（保持不可变语义，返回浅拷贝）
    if not is_status_changed and not is_node_event:
        return dict(nodes)

    node_id = event.get("node_id")
    # node_id 为空：安全跳过，返回浅拷贝
    if not node_id:
        return dict(nodes)

    # 浅拷贝顶层字典，再对目标节点做浅拷贝，保证不修改入参
    new_nodes = dict(nodes)
    # 取出旧节点副本；不存在则新建最小 entry
    node = dict(new_nodes.get(node_id, {"node_id": node_id}))

    # 计算目标状态：status_changed 用事件里的 status，其余用映射表
    if is_status_changed:
        node["status"] = event.get("status")
    else:
        node["status"] = _NODE_STATUS_BY_EVENT[event_type]
        # completed/failed 额外携带 output/error
        if event_type == "node_completed":
            node["output"] = event.get("output")
        elif event_type == "node_failed":
            node["error"] = event.get("error")

    new_nodes[node_id] = node
    return new_nodes


def _truncate_one_line(text: Any, width: int = 120) -> str:
    """压缩为单行 ASCII 文本，并按宽度截断。"""
    clean = " ".join(str(text).split())
    clean = clean.encode("ascii", errors="backslashreplace").decode("ascii")
    if width <= 0:
        return ""
    if len(clean) <= width:
        return clean
    if width <= 3:
        return "." * width
    return clean[: width - 3] + "..."


def _json_preview(value: Any) -> str:
    """把任意值转成紧凑 JSON 预览，无法序列化时回退到字符串。"""
    try:
        return json.dumps(
            value,
            ensure_ascii=True,
            default=str,
            separators=(",", ":"),
            sort_keys=True,
        )
    except TypeError:
        return str(value).encode("ascii", errors="backslashreplace").decode("ascii")


def _preview_tool_args(args: Any) -> str:
    """从工具参数中提取最有辨识度的一段。"""
    if not isinstance(args, dict):
        return _json_preview(args)
    for key in ("command", "cmd", "url", "path", "query", "operation"):
        value = args.get(key)
        if value is not None:
            return f"{key}={_json_preview(value)}"
    return _json_preview(args)


def compact_tool_call(
    skill_name: str, args: dict[str, Any] | None, width: int = 120
) -> str:
    """把工具调用压缩成 TUI 单行摘要。"""
    preview = _preview_tool_args({} if args is None else args)
    return _truncate_one_line(f"-> {skill_name} {preview}", width)


def compact_tool_result(
    skill_name: str,
    ok: bool,
    detail: str = "",
    width: int = 120,
) -> str:
    """把工具结果压缩成 TUI 单行摘要。"""
    if ok:
        return _truncate_one_line(f"<- {skill_name} ok", width)
    suffix = detail or "failed"
    return _truncate_one_line(f"[!] {skill_name} {suffix}", width)


def _compact_value(value: Any, max_length: int = 96) -> str:
    """把任意值压缩为单行 ASCII 文本，适合事件日志展示。"""
    try:
        text = json.dumps(value, ensure_ascii=True, default=str, sort_keys=True)
    except TypeError:
        text = str(value).encode("ascii", errors="backslashreplace").decode("ascii")
    text = " ".join(text.split())
    if len(text) <= max_length:
        return text
    return text[: max_length - 3] + "..."


def event_summary(event: dict) -> str:
    """把一条事件转成一行纯 ASCII 文本，用于日志显示。"""
    event_type = event.get("type", "")
    node_id = event.get("node_id", "")

    if event_type == "task_started":
        return "> task started"
    if event_type == "task_completed":
        return "[ok] task completed"
    if event_type == "task_failed":
        return f"[x] task failed: {event.get('error', '')}"

    if event_type == "node_started":
        return f"> node {node_id} started"
    if event_type == "node_completed":
        return f"[ok] node {node_id} completed"
    if event_type == "node_failed":
        return f"[x] node {node_id} failed: {event.get('error', '')}"
    if event_type == "node_waiting_input":
        return f"[?] node {node_id} waiting: {event.get('reason', '')}"
    if event_type == "node_status_changed":
        return f"* {node_id} -> {event.get('status', '')}"

    if event_type == "agent_thought":
        content = str(event.get("content", ""))
        return f"~ {content[:80]}"
    if event_type == "skill_called":
        args = event.get("args")
        suffix = f" {_compact_value(args)}" if args else ""
        return f"tool: {event.get('skill_name', '')}{suffix}"
    if event_type == "skill_returned":
        result = event.get("result")
        suffix = f" {_compact_value(result)}" if result is not None else ""
        return f"<- {event.get('skill_name', '')} returned{suffix}"
    if event_type == "skill_failed":
        return f"[!] {event.get('skill_name', '')} failed: {event.get('error', '')}"
    if event_type == "log":
        return f". {event.get('message', '')}"
    if event_type == "user_intervened":
        return f"[user] {event.get('action', '')} @ {node_id}"

    return event_type


# 节点状态 -> (符号, Rich 颜色标记) 的映射；render_dag 依此上色
_STATUS_SYMBOL = {
    "pending": (".", "grey50"),
    "running": ("*", "blue"),
    "completed": ("*", "green"),
    "failed": ("x", "red"),
    "waiting_input": ("?", "yellow"),
    "skipped": ("-", "grey30"),
}


def render_dag(sop: dict | None, nodes: dict) -> str:
    """把 SOP 的线性 DAG 渲染成一行带 Rich 标记的文本图。

    按 entry_node 沿 edges 线性排列节点，节点间用 " -> " 连接。
    每个节点前缀一个状态符号并按状态上色，节点显示 name（缺失用 id）。

    :param sop: SOP 模板字典，None 时返回占位文本。
    :param nodes: 节点状态字典（nodeId -> {status,...}）。
    :return: 带 Rich 标记的多段单行文本，如 "[green]*[/] 审核 -> ..."。
    """
    # 无 SOP 时给出纯 ASCII 占位，避免终端编码问题
    if sop is None:
        return "(no DAG)"

    # 建立 id -> Node 映射，便于取显示名
    node_defs = {n["id"]: n for n in sop.get("nodes", [])}
    # 建立邻接表：from -> to（线性 DAG，每个节点至多一个后继）
    next_map = {edge["from"]: edge["to"] for edge in sop.get("edges", [])}

    # 从入口节点开始沿边线性遍历，收集有序节点 id 列表
    order: list[str] = []
    visited: set[str] = set()
    current = sop.get("entry_node")
    while current is not None and current not in visited:
        order.append(current)
        visited.add(current)
        current = next_map.get(current)

    # 遍历不到任何节点（缺 entry 或空 SOP）时的兜底
    if not order:
        return "(no DAG)"

    # 逐节点渲染 "符号 + 名称"，并按状态上色
    segments: list[str] = []
    for node_id in order:
        # 节点当前状态：默认 pending
        status = nodes.get(node_id, {}).get("status", "pending")
        symbol, color = _STATUS_SYMBOL.get(status, (".", "grey50"))
        # 显示名优先取 SOP 定义里的 name，缺失回退到 id
        label = node_defs.get(node_id, {}).get("name") or node_id
        segments.append(f"[{color}]{symbol}[/] {label}")

    # 用箭头连接各节点段
    return " -> ".join(segments)
