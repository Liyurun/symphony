"""Symphony Textual TUI 应用（UI 层）。

以 htop/lazygit 风格的终端界面作为后端的 HTTP/WebSocket 客户端：

- 左侧任务列表（DataTable），右侧上方 DAG 状态图（Static）、下方事件日志（RichLog）。
- 选中任务后拉取快照与 SOP，并建立 WebSocket 连接实时同步事件。
- 一组快捷键对当前活跃节点施加人工干预（重试/跳过/改提示词/审批）。

本模块依赖 Textual，仅负责编排与呈现；所有网络与纯计算逻辑均委托
``symphony.tui.client``（ServerClient + 纯函数）。
"""

import asyncio
import json
from typing import Literal

import websockets
from rich.text import Text
from textual import events
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import DataTable, Footer, Header, Input, Markdown, RichLog, Static

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
    trim_history,
)


class PromptModal(ModalScreen[str]):
    """一个极简模态输入框：收集一行文本并作为屏幕结果返回。

    通过 ``push_screen_wait`` 使用；用户回车提交返回输入文本，
    按 Esc 取消返回空字符串。
    """

    # Esc 取消，回车由 Input 的 submitted 事件处理
    BINDINGS = [("escape", "cancel", "取消")]

    def __init__(self, prompt_label: str) -> None:
        """保存输入框上方的提示标签文本。"""
        super().__init__()
        # 展示给用户的说明文字（如“输入新提示词”）
        self._prompt_label = prompt_label

    def compose(self) -> ComposeResult:
        """渲染一个带说明文字的输入框。"""
        # 垂直容器承载说明与输入框
        with Vertical(id="modal-box"):
            yield Static(self._prompt_label, id="modal-label")
            yield Input(id="modal-input")

    def on_mount(self) -> None:
        """挂载后让输入框获得焦点，便于直接键入。"""
        self.query_one("#modal-input", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """回车提交：以输入内容关闭模态。"""
        self.dismiss(event.value)

    def action_cancel(self) -> None:
        """Esc 取消：以空字符串关闭模态。"""
        self.dismiss("")


class SymphonyTUI(App):
    """Symphony 终端 UI 主应用。"""

    # 应用标题（显示在 Header 上）
    TITLE = "Symphony TUI"

    # 简单的界面样式：上方品牌区，左侧列表，右侧详情与日志
    CSS = """
    #root {
        height: 1fr;
    }
    #main-title {
        height: auto;
        min-height: 1;
        border-bottom: solid $accent;
        padding: 0 1;
    }
    #main-area {
        height: 1fr;
    }
    #primary-table {
        width: 36;
        border-right: solid $accent;
    }
    #detail-area {
        width: 1fr;
    }
    #chat-scroll {
        height: 1fr;
        padding: 1 1;
    }
    .chat-message {
        margin-bottom: 1;
    }
    .chat-user {
        color: $text;
    }
    .chat-status {
        color: $text-muted;
    }
    #detail {
        height: auto;
        min-height: 8;
        border-bottom: solid $accent;
        padding: 1 1;
    }
    #stream-view {
        height: auto;
        color: $text;
    }
    #log {
        height: 1fr;
    }
    #command-input {
        dock: bottom;
    }
    #modal-box {
        width: 60%;
        height: auto;
        padding: 1 2;
        background: $panel;
        border: thick $accent;
    }
    """

    # 快捷键绑定：全局导航与人工干预动作
    BINDINGS = [
        ("q", "quit", "quit"),
        ("r", "retry_node", "retry"),
        ("e", "retry_subnode_prompt", "retry subnode"),
        ("p", "edit_prompt", "edit prompt"),
        ("s", "skip_node", "skip"),
        ("a", "approve", "approve"),
        ("t", "toggle_view", "log/trace"),
        ("j", "cursor_down", "down"),
        ("k", "cursor_up", "up"),
    ]

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:8899",
        http_timeout: float = 30.0,
        input_history_limit: int = 100,
        chat_context_history_limit: int = 24,
    ) -> None:
        """初始化应用状态并创建后端客户端。

        :param base_url: 后端服务地址。
        :param http_timeout: REST 客户端请求超时时间（秒）。
        :param input_history_limit: 输入框历史保留条数。
        :param chat_context_history_limit: Chat 上下文历史消息保留条数。
        """
        super().__init__()
        # 后端地址与 REST 客户端
        self.base_url = base_url
        self.client = ServerClient(base_url, timeout=http_timeout)
        self.input_history_limit = max(0, int(input_history_limit))
        self.chat_context_history_limit = max(0, int(chat_context_history_limit))
        self.chat_socket = ChatSocket(
            base_url,
            history_limit=self.chat_context_history_limit,
        )
        # TUI 默认进入通用问答首页，SOP/任务监控作为后续模式切换
        self.mode: Literal["home", "sop", "task"] = "home"
        self.chat_history: list[dict] = []
        self._last_answer: str = ""
        self._input_history: list[str] = []
        self._history_cursor: int | None = None
        self.sops: list[dict] = []
        self.selected_sop_id: str | None = None
        # 运行时状态
        self.tasks: list[dict] = []
        self.active_task_id: str | None = None
        self.snapshot: dict | None = None
        self.sop: dict | None = None
        self.selected_node_id: str | None = None
        self.selected_sub_node_id: str | None = None
        # 后台 WebSocket 订阅任务（asyncio.Task）
        self._ws_task: asyncio.Task | None = None
        # 日志区当前显示模式：True=事件日志，False=LLM Trace
        self._show_log = True

    def compose(self) -> ComposeResult:
        """构建界面：Header + 主内容 + 输入框 + Footer。"""
        yield Header()
        with Vertical(id="root"):
            yield Static("", id="main-title")
            with Horizontal(id="main-area"):
                yield DataTable(id="primary-table")
                with Vertical(id="detail-area"):
                    yield VerticalScroll(id="chat-scroll")
                    yield Static("", id="detail")
                    yield Static("", id="stream-view")
                    yield RichLog(id="log", highlight=False, markup=True, wrap=True)
            yield Input(placeholder="Ask anything...", id="command-input")
        yield Footer()

    async def on_mount(self) -> None:
        """挂载后进入默认 home 模式。"""
        table = self.query_one("#primary-table", DataTable)
        table.cursor_type = "row"
        self.query_one("#stream-view", Static).display = False
        await self._render_home()
        self.query_one("#command-input", Input).focus()

    def _set_home_layout(self) -> None:
        """切到默认问答布局：显示聊天流，隐藏 SOP/task 控制台。"""
        self.query_one("#primary-table", DataTable).display = False
        self.query_one("#chat-scroll", VerticalScroll).display = True
        self.query_one("#detail", Static).display = False
        self.query_one("#stream-view", Static).display = False
        self.query_one("#log", RichLog).display = False

    def _set_console_layout(self) -> None:
        """切到 SOP/任务控制台布局：隐藏聊天流，显示控制台。"""
        self.query_one("#primary-table", DataTable).display = True
        self.query_one("#chat-scroll", VerticalScroll).display = False
        self.query_one("#detail", Static).display = True
        self.query_one("#stream-view", Static).display = False
        self.query_one("#log", RichLog).display = True

    def _chat_scroll(self) -> VerticalScroll:
        """返回 home 模式的聊天消息容器。"""
        return self.query_one("#chat-scroll", VerticalScroll)

    async def _clear_chat_stream(self) -> None:
        """清空聊天流里的所有消息部件。"""
        await self._chat_scroll().remove_children()

    async def _append_chat_widget(self, widget: Static | Markdown) -> None:
        """追加一个聊天消息部件并滚动到底部。"""
        await self._chat_scroll().mount(widget)
        self._chat_scroll().scroll_end(animate=False, force=True)

    async def _append_welcome_message(self) -> None:
        """把欢迎页作为聊天流第一条消息追加。"""
        await self._append_chat_widget(
            Static(render_home(self.base_url), classes="chat-message")
        )

    async def _append_user_message(self, text: str) -> None:
        """追加一条用户消息，使用 Text 避免 Rich markup 注入。"""
        await self._append_chat_widget(
            Static(Text(f"You\n{text}"), classes="chat-message chat-user")
        )

    async def _append_status_message(self, text: str) -> None:
        """追加一条工具/错误状态消息。"""
        await self._append_chat_widget(
            Static(text, classes="chat-message chat-status")
        )

    async def _write_feedback(self, text: str) -> None:
        """按当前模式写入用户可见反馈。"""
        if self.mode == "home":
            await self._append_status_message(text)
            return
        self.query_one("#log", RichLog).write(text)

    def _assistant_markdown(self, text: str) -> str:
        """组装助手 Markdown 消息正文。"""
        body = text if text else "_Thinking..._"
        return f"**Pi Agent**\n\n{body}"

    async def _render_home(self) -> None:
        """渲染 chat-first 首页，把欢迎页放进聊天流。"""
        self.mode = "home"
        self._set_home_layout()
        self.query_one("#main-title", Static).update(
            "Symphony | Ask anything | /sop for SOP console"
        )
        table = self.query_one("#primary-table", DataTable)
        table.clear(columns=True)
        detail = self.query_one("#detail", Static)
        detail.update("")
        self.query_one("#stream-view", Static).update("")
        self.query_one("#log", RichLog).clear()
        await self._clear_chat_stream()
        await self._append_welcome_message()
        for item in self.chat_history:
            if item["role"] == "user":
                await self._append_user_message(item["content"])
            else:
                await self._append_chat_widget(
                    Markdown(
                        self._assistant_markdown(item["content"]),
                        classes="chat-message",
                    )
                )

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        """处理底部输入框提交。"""
        if event.input.id != "command-input":
            return
        raw = event.value.strip()
        event.input.value = ""
        if not raw:
            return
        self._record_history(raw)
        await self._handle_user_input(raw)

    async def on_key(self, event: events.Key) -> None:
        """home 模式下上下键在输入历史间切换，而不是移动表格。"""
        if self.mode != "home" or event.key not in {"up", "down"}:
            return
        command_input = self.query_one("#command-input", Input)
        if event.key == "up":
            command_input.value = self._history_prev(command_input.value)
        else:
            command_input.value = self._history_next()
        command_input.cursor_position = len(command_input.value)
        event.prevent_default()
        event.stop()

    def _record_history(self, text: str) -> None:
        """把一条已提交的问题追加进输入历史并重置游标。"""
        if text and self.input_history_limit > 0:
            self._input_history.append(text)
            self._input_history = trim_history(
                self._input_history,
                self.input_history_limit,
            )
        self._history_cursor = None

    def _trim_chat_history(self) -> None:
        """按配置裁剪本地 Chat 上下文历史。"""
        self.chat_history = trim_history(
            self.chat_history,
            self.chat_context_history_limit,
        )

    def _history_prev(self, current: str) -> str:
        """上键：回到更早的历史问题，到顶保持最早一条。"""
        if not self._input_history:
            return current
        if self._history_cursor is None:
            self._history_cursor = len(self._input_history) - 1
        else:
            self._history_cursor = max(0, self._history_cursor - 1)
        return self._input_history[self._history_cursor]

    def _history_next(self) -> str:
        """下键：前进到更新的历史；越过最新则回到空草稿。"""
        if self._history_cursor is None:
            return ""
        self._history_cursor += 1
        if self._history_cursor >= len(self._input_history):
            self._history_cursor = None
            return ""
        return self._input_history[self._history_cursor]

    async def _handle_user_input(self, raw: str) -> None:
        """分发普通问答和 slash 命令。"""
        command = parse_tui_command(raw)
        if command.name == "ask":
            await self._ask_agent(command.args[0])
            return
        if command.name == "help":
            await self._write_feedback(command_help())
            return
        if command.name == "home":
            await self._render_home()
            return
        if command.name == "web":
            await self._write_feedback(f"Web UI: {self.base_url}")
            return
        if command.name == "copy":
            had_answer = bool(self._last_answer)
            self._copy_last_answer()
            if self.mode == "home":
                await self._append_status_message(
                    "Copied last answer to clipboard."
                    if had_answer
                    else "No answer to copy yet."
                )
            return
        if command.name == "retry":
            if not self.active_task_id:
                await self._write_feedback("No active task.")
                return
            await self.client.rerun_node(
                self.active_task_id, command.args[0], command.args[1]
            )
            await self._write_feedback(f"Rerun requested: {command.args[0]}")
            return
        if command.name == "answer":
            if not self.active_task_id:
                await self._write_feedback("No active task.")
                return
            try:
                answer = json.loads(command.args[1])
            except json.JSONDecodeError:
                answer = {"text": command.args[1]}
            await self.client.answer_interaction(
                self.active_task_id, command.args[0], answer
            )
            await self._write_feedback(f"Answer submitted: {command.args[0]}")
            return
        if command.name == "logs":
            await self._write_feedback(f"Open Web Logs for task: {command.args[0]}")
            return
        if command.name == "exit":
            self.exit()
            return
        if command.name in {"sop", "sop_list"}:
            await self._render_sop_console()
            return
        if command.name == "sop_start":
            await self._start_sop(command.args[0])
            return
        if command.name == "sop_running":
            self.mode = "task"
            self._set_console_layout()
            await self.refresh_tasks()
            self.query_one("#main-title", Static).update("/sop running")
            self.query_one("#detail", Static).update(
                "Select a running task to view DAG and event log."
            )
            return
        await self._write_feedback(command_help())

    async def _ask_agent(self, question: str) -> None:
        """默认问答：走 /ws/chat 流式端点，逐段更新 Markdown 消息。"""
        self.chat_history.append({"role": "user", "content": question})
        await self._append_user_message(question)

        answer_parts: list[str] = []
        history = self.chat_history[:-1]
        session_id: str | None = None
        try:
            session = await self.client.create_chat_session(question[:40] or "Chat")
            session_id = session.get("session_id")
        except Exception as exc:
            await self._append_status_message(f"Chat log disabled: {exc}")

        assistant: Markdown | None = None
        try:
            async for event in self.chat_socket.stream(
                question, history, session_id=session_id
            ):
                kind = event.get("type")
                if kind == "chat_thinking":
                    content = event.get("content", "Thinking...")
                    # 工具调用本身已经有一行摘要，避免重复显示“调用 N 个工具”。
                    if not str(content).startswith("调用 "):
                        await self._append_status_message(content)
                elif kind == "chat_tool_call":
                    await self._append_status_message(
                        compact_tool_call(
                            event.get("skill_name", ""),
                            event.get("args", {}),
                        )
                    )
                elif kind == "chat_tool_result":
                    await self._append_status_message(
                        compact_tool_result(
                            event.get("skill_name", ""),
                            bool(event.get("ok")),
                            event.get("detail", ""),
                        )
                    )
                elif kind == "chat_answer_delta":
                    if assistant is None:
                        assistant = Markdown(
                            self._assistant_markdown(""),
                            classes="chat-message",
                        )
                        await self._append_chat_widget(assistant)
                    answer_parts.append(event.get("text", ""))
                    await assistant.update(
                        self._assistant_markdown("".join(answer_parts))
                    )
                    self._chat_scroll().scroll_end(animate=False, force=True)
                elif kind == "chat_failed":
                    await self._append_status_message(
                        f"Chat failed: {event.get('error', '')}"
                    )
                    self._trim_chat_history()
                    return
        except Exception as exc:
            await self._append_status_message(f"Chat failed: {exc}")
            self._trim_chat_history()
            return

        answer = "".join(answer_parts)
        self._last_answer = answer
        self.chat_history.append({"role": "assistant", "content": answer})
        self._trim_chat_history()
        if assistant is None:
            assistant = Markdown(
                self._assistant_markdown(answer),
                classes="chat-message",
            )
            await self._append_chat_widget(assistant)
        else:
            await assistant.update(self._assistant_markdown(answer))
        self._chat_scroll().scroll_end(animate=False, force=True)

    def _copy_last_answer(self) -> None:
        """把最后一条回答写入系统剪贴板（OSC52 + macOS pbcopy 兜底）。"""
        if not self._last_answer:
            self._write_log_if_ready("[yellow]No answer to copy yet.[/]")
            return
        self.copy_to_clipboard(self._last_answer)
        try:
            import shutil
            import subprocess

            if shutil.which("pbcopy"):
                subprocess.run(
                    ["pbcopy"], input=self._last_answer.encode("utf-8"), check=False
                )
        except Exception:
            pass
        self._write_log_if_ready("[green]Copied last answer to clipboard.[/]")

    def _write_log_if_ready(self, text: str) -> None:
        """App 未挂载时跳过日志写入，便于纯单元测试直接调用方法。"""
        try:
            self.query_one("#log", RichLog).write(text)
        except Exception:
            return

    async def _render_sop_console(self) -> None:
        """渲染 /sop 控制台：左列表，右详情。"""
        self.mode = "sop"
        self._set_console_layout()
        self.query_one("#main-title", Static).update("/sop - SOP Console")
        log = self.query_one("#log", RichLog)
        log.clear()
        try:
            self.sops = await self.client.list_sops()
        except Exception as exc:
            log.write(f"[red]Load SOP list failed: {exc}[/]")
            return

        table = self.query_one("#primary-table", DataTable)
        table.clear(columns=True)
        table.add_columns("SOP", "ID")
        for sop in self.sops:
            table.add_row(
                sop.get("name", sop.get("id", ""))[:24],
                sop.get("id", ""),
                key=sop.get("id", ""),
            )

        if self.sops:
            self._select_sop(self.sops[0].get("id"))
        else:
            self.selected_sop_id = None
            self.query_one("#detail", Static).update("No SOP templates.")
        log.write("Use /sop start <id> to start, or /web for advanced editing.")

    def _select_sop(self, sop_id: str | None) -> None:
        """选择一个 SOP 并刷新详情面板。"""
        self.selected_sop_id = sop_id
        selected = next((s for s in self.sops if s.get("id") == sop_id), None)
        self.query_one("#detail", Static).update(
            render_sop_detail(selected, self.base_url)
        )

    async def _start_sop(self, sop_id: str) -> None:
        """用 TUI 轻量表单启动一个 SOP。"""
        log = self.query_one("#log", RichLog)
        sop = next((s for s in self.sops if s.get("id") == sop_id), None)
        if sop is None:
            try:
                self.sops = await self.client.list_sops()
            except Exception as exc:
                log.write(f"[red]Load SOP failed: {exc}[/]")
                return
            sop = next((s for s in self.sops if s.get("id") == sop_id), None)
        if sop is None:
            log.write(f"[red]SOP not found: {sop_id}[/]")
            return

        fields = sop.get("variables_def") or []
        if is_complex_variables_def(fields):
            log.write(f"[yellow]This SOP has complex inputs. Use Web UI: {self.base_url}[/]")
            return

        variables: dict = {}
        for field in fields:
            label = field.get("label") or field.get("name")
            suffix = " *" if field.get("required") else ""
            raw = await self.push_screen_wait(PromptModal(f"{label}{suffix}:"))
            if not raw and field.get("required"):
                log.write(f"[red]Missing required field: {field.get('name')}[/]")
                return
            if raw:
                try:
                    variables[field["name"]] = coerce_tui_field_value(field, raw)
                except (TypeError, ValueError) as exc:
                    log.write(f"[red]Field {field.get('name')} parse failed: {exc}[/]")
                    return

        try:
            result = await self.client.start_task(sop_id, variables)
        except Exception as exc:
            log.write(f"[red]Start SOP failed: {exc}[/]")
            return
        task_id = result.get("task_id")
        log.write(f"[green]Started SOP: {sop_id} -> {task_id}[/]")
        if task_id:
            await self.select_task(task_id)
            self.mode = "task"

    async def refresh_tasks(self) -> None:
        """拉取任务列表并重建任务表内容。"""
        # 网络边界：拉取失败时记一条日志，不让定时器崩溃
        try:
            self.tasks = await self.client.list_tasks()
        except Exception as exc:  # 网络/解析边界
            self._write_log(f"[red]Refresh tasks failed: {exc}[/]")
            return

        table = self.query_one("#primary-table", DataTable)
        # 记录当前高亮行对应的 task_id，重建后尽量保持选中
        table.clear(columns=True)
        table.add_columns("Task", "SOP", "Status")
        for task in self.tasks:
            # 行 key 用 task_id，便于选中时反查
            table.add_row(
                task.get("task_id", "")[:8],
                task.get("sop_name", task.get("sop_id", "")),
                task.get("status", ""),
                key=task.get("task_id", ""),
            )

    async def on_data_table_row_highlighted(
        self, event: DataTable.RowHighlighted
    ) -> None:
        """表格高亮行变化时按当前模式切换选择。"""
        row_id = event.row_key.value if event.row_key else None
        if not row_id:
            return
        if self.mode == "sop":
            self._select_sop(row_id)
            return
        if self.mode == "task" and row_id != self.active_task_id:
            await self.select_task(row_id)

    async def select_task(self, task_id: str) -> None:
        """选中某任务：拉快照与 SOP、重建 WS 订阅、刷新 DAG 与日志。"""
        self.mode = "task"
        self._set_console_layout()
        self.query_one("#main-title", Static).update("Task Monitor")
        self.active_task_id = task_id

        # 拉取任务快照（网络边界）
        try:
            self.snapshot = await self.client.get_task(task_id)
        except Exception as exc:  # 网络/解析边界
            self._write_log(f"[red]Load task snapshot failed: {exc}[/]")
            return

        # 当前活跃节点作为默认干预目标，并清掉旧任务遗留的子节点选择
        self._select_node(self.snapshot.get("current_node"))

        # 依据快照的 sop_id 从 SOP 列表匹配模板定义
        self.sop = await self._find_sop(self.snapshot.get("sop_id"))

        # 清空日志区，重建 DAG，并重连 WebSocket 回放历史 + 实时事件
        self.query_one("#log", RichLog).clear()
        self._refresh_dag()
        self._restart_ws(task_id)

    async def _find_sop(self, sop_id: str | None) -> dict | None:
        """在 SOP 列表中按 id 匹配模板定义，找不到返回 None。"""
        # 没有 sop_id 无从匹配
        if not sop_id:
            return None
        # 网络边界：拉取 SOP 列表失败时安静返回 None
        try:
            sops = await self.client.list_sops()
        except Exception:  # 网络/解析边界
            return None
        # 线性查找匹配 id 的模板
        return next((s for s in sops if s.get("id") == sop_id), None)

    def _refresh_dag(self) -> None:
        """用当前 sop 与 snapshot.nodes 重绘 DAG 状态图。"""
        # 快照缺失时节点字典为空
        nodes = (self.snapshot or {}).get("nodes", {})
        self.query_one("#detail", Static).update(render_dag(self.sop, nodes))

    def _write_log(self, text: str) -> None:
        """向日志区写入一行文本（带 Rich 标记）。"""
        self.query_one("#log", RichLog).write(text)

    def _select_node(self, node_id: str | None) -> None:
        """选择父节点作为干预目标，并清空已过期的子节点选择。"""
        self.selected_node_id = node_id
        self.selected_sub_node_id = None

    def _select_sub_node(self, sub_node_id: str | None) -> None:
        """选择子终端中的子节点；当前 TUI 尚未提供真实列表 UI 调用。"""
        self.selected_sub_node_id = sub_node_id

    def _restart_ws(self, task_id: str) -> None:
        """取消旧的 WS 订阅任务并为新任务启动后台订阅协程。"""
        # 先取消上一个订阅任务
        if self._ws_task is not None and not self._ws_task.done():
            self._ws_task.cancel()
        # 启动新的后台订阅协程
        self._ws_task = asyncio.create_task(self._ws_loop(task_id))

    async def _ws_loop(self, task_id: str) -> None:
        """连接 WebSocket，消费历史回放与实时事件并更新 UI。

        网络边界允许 try/except：连接断开时安静退出，不影响主界面。
        """
        ws_url = self.client.ws_url(task_id)
        try:
            async with websockets.connect(ws_url) as websocket:
                # 服务端先逐条回放历史事件，随后推送实时事件
                async for message in websocket:
                    # 切换到别的任务后，忽略旧连接的残留消息
                    if task_id != self.active_task_id:
                        break
                    self._handle_ws_message(message)
        except asyncio.CancelledError:
            # 主动取消（切任务/退出）：向上传播以正常结束任务
            raise
        except Exception as exc:  # 网络边界：连接断开等
            self._write_log(f"[grey50]. WS closed: {exc}[/]")

    def _handle_ws_message(self, message: str) -> None:
        """解析一条 WS 文本消息为事件，写日志并联动更新节点状态。"""
        # JSON 解析边界：非法消息安静忽略
        try:
            event = json.loads(message)
        except (json.JSONDecodeError, TypeError):
            return
        # 写一行事件摘要到日志
        self._write_log(event_summary(event))
        # 用纯函数更新快照里的节点状态并刷新 DAG
        if self.snapshot is not None:
            self.snapshot["nodes"] = apply_event(
                self.snapshot.get("nodes", {}), event
            )
            self._refresh_dag()

    # -------- 导航快捷键（委托给 DataTable 的光标移动） --------

    def action_cursor_down(self) -> None:
        """j / 下键：任务表光标下移一行。"""
        self.query_one("#primary-table", DataTable).action_cursor_down()

    def action_cursor_up(self) -> None:
        """k / 上键：任务表光标上移一行。"""
        self.query_one("#primary-table", DataTable).action_cursor_up()

    # -------- 人工干预快捷键 --------

    async def _intervene(self, action: str, data: dict) -> None:
        """对当前活跃任务与选中节点施加一次干预的公共逻辑。"""
        # 缺少活跃任务或目标节点时给出提示
        if not self.active_task_id or not self.selected_node_id:
            self._write_log("[yellow]. No active task or node; ignored[/]")
            return
        # 网络边界：干预请求失败时记日志
        try:
            await self.client.intervene(
                self.active_task_id, self.selected_node_id, action, data
            )
            self._write_log(
                f"[cyan][user] intervened {action} @ {self.selected_node_id}[/]"
            )
        except Exception as exc:  # 网络边界
            self._write_log(f"[red]Intervention failed: {exc}[/]")

    async def action_retry_node(self) -> None:
        """r：重试当前活跃节点。"""
        await self._intervene("retry", {})

    async def action_retry_subnode_prompt(self) -> None:
        """e：带提示词重跑当前选中的子节点。"""
        if not self.active_task_id or not self.selected_node_id:
            self._write_log("[yellow]. No active task or parent node; ignored[/]")
            return
        sub_node_id = self.selected_sub_node_id
        if not sub_node_id:
            self._write_log("[yellow]. Select a subnode first[/]")
            return
        prompt = await self.push_screen_wait(PromptModal("Retry prompt:"))
        if not prompt:
            return
        try:
            await self.client.retry_subnode(
                self.active_task_id, self.selected_node_id, sub_node_id, prompt
            )
            self._write_log(f"[cyan][user] retry subnode {sub_node_id}[/]")
            self.snapshot = await self.client.get_task(self.active_task_id)
            self._refresh_dag()
        except Exception as exc:  # 网络边界
            self._write_log(f"[red]Retry subnode failed: {exc}[/]")

    async def action_skip_node(self) -> None:
        """s：跳过当前活跃节点。"""
        await self._intervene("skip", {})

    async def action_edit_prompt(self) -> None:
        """p：弹出输入框收集新提示词并发送 edit_prompt 干预。"""
        # 弹出模态输入框，等待用户输入
        prompt = await self.push_screen_wait(PromptModal("New prompt:"))
        # 空输入视为取消
        if prompt:
            await self._intervene("edit_prompt", {"prompt": prompt})

    async def action_approve(self) -> None:
        """a：弹输入框收集 JSON 作为输出，发送 provide_output 审批。"""
        # 收集一段 JSON 文本作为节点输出
        raw = await self.push_screen_wait(PromptModal("Output JSON:"))
        # 空输入视为取消
        if not raw:
            return
        # JSON 解析边界：非法 JSON 时提示并放弃
        try:
            output = json.loads(raw)
        except json.JSONDecodeError:
            self._write_log("[red]. Invalid JSON; approval canceled[/]")
            return
        await self._intervene("provide_output", {"output": output})

    async def action_toggle_view(self) -> None:
        """t：在事件日志与 LLM Trace 之间切换日志区内容。"""
        # 翻转显示模式
        self._show_log = not self._show_log
        log = self.query_one("#log", RichLog)
        log.clear()

        # 无活跃任务时无内容可显示
        if not self.active_task_id:
            return

        if self._show_log:
            # 回到事件日志：回放已有事件
            self._write_log("[grey50]-- Event log --[/]")
            try:
                events = await self.client.get_events(self.active_task_id)
            except Exception as exc:  # 网络边界
                self._write_log(f"[red]Load events failed: {exc}[/]")
                return
            for event in events:
                self._write_log(event_summary(event))
        else:
            # 切到 LLM Trace：拉取并逐条打印
            self._write_log("[grey50]-- LLM Trace --[/]")
            try:
                traces = await self.client.get_traces(self.active_task_id)
            except Exception as exc:  # 网络边界
                self._write_log(f"[red]Load Trace failed: {exc}[/]")
                return
            for trace in traces:
                # Trace 结构不固定，用节点 id + 简短摘要呈现
                node_id = trace.get("node_id", "")
                self._write_log(f"[magenta]* {node_id}[/] {str(trace)[:120]}")

    async def on_unmount(self) -> None:
        """退出时取消 WS 订阅并关闭 HTTP 客户端。"""
        # 取消后台订阅任务
        if self._ws_task is not None and not self._ws_task.done():
            self._ws_task.cancel()
        # 关闭 REST 客户端连接
        await self.client.close()


def run_tui(
    base_url: str = "http://127.0.0.1:8899",
    http_timeout: float = 30.0,
    input_history_limit: int = 100,
    chat_context_history_limit: int = 24,
) -> None:
    """构造并运行 Symphony TUI（cli 的调用入口）。

    :param base_url: 后端服务地址。
    :param http_timeout: REST 客户端请求超时时间（秒）。
    :param input_history_limit: 输入框历史保留条数。
    :param chat_context_history_limit: Chat 上下文历史消息保留条数。
    """
    SymphonyTUI(
        base_url,
        http_timeout=http_timeout,
        input_history_limit=input_history_limit,
        chat_context_history_limit=chat_context_history_limit,
    ).run()
