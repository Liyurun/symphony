"""守卫测试：所有 TUI 可显示字符串必须是纯 ASCII。"""

from symphony.tui.client import (
    _STATUS_SYMBOL,
    command_help,
    compact_tool_call,
    compact_tool_result,
    event_summary,
    render_home,
)

_EVENT_SAMPLES = [
    {"type": "task_started"},
    {"type": "task_completed"},
    {"type": "task_failed", "error": "e"},
    {"type": "node_started", "node_id": "n"},
    {"type": "node_completed", "node_id": "n"},
    {"type": "node_failed", "node_id": "n", "error": "e"},
    {"type": "node_waiting_input", "node_id": "n", "reason": "r"},
    {"type": "node_status_changed", "node_id": "n", "status": "running"},
    {"type": "agent_thought", "content": "c"},
    {"type": "skill_called", "skill_name": "s"},
    {"type": "skill_returned", "skill_name": "s"},
    {"type": "skill_failed", "skill_name": "s", "error": "e"},
    {"type": "log", "message": "m"},
    {"type": "user_intervened", "action": "retry", "node_id": "n"},
    {"type": "weird"},
]


def _is_ascii(text: str) -> bool:
    return all(ord(ch) < 128 for ch in text)


def test_render_home_ascii():
    assert _is_ascii(render_home("http://127.0.0.1:8899"))


def test_command_help_ascii():
    assert _is_ascii(command_help())


def test_command_help_correction_commands_ascii():
    text = command_help()

    for command in (
        "/retry <node> <text>",
        "/answer <id> <json>",
        "/logs <task_id>",
    ):
        assert command in text
        assert _is_ascii(command)


def test_event_summary_ascii():
    for event in _EVENT_SAMPLES:
        assert _is_ascii(event_summary(event)), event["type"]


def test_compact_tool_summaries_ascii():
    lines = [
        compact_tool_call("python_execute", {"command": "python a.py"}),
        compact_tool_result("python_execute", True, ""),
        compact_tool_result("python_execute", False, "boom"),
    ]

    assert all(_is_ascii(line) for line in lines)


def test_status_symbols_ascii():
    for symbol, _color in _STATUS_SYMBOL.values():
        assert _is_ascii(symbol)
