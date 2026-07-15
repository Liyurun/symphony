"""agent.chat_events 模型的单元测试。"""

from symphony.agent.chat_events import (
    ChatAnswerDelta,
    ChatCompleted,
    ChatFailed,
    ChatThinking,
    ChatToolCall,
    ChatToolResult,
)


def test_answer_delta_to_dict():
    ev = ChatAnswerDelta(text="hi")
    assert ev.to_dict() == {"type": "chat_answer_delta", "text": "hi"}


def test_tool_call_and_result():
    call = ChatToolCall(skill_name="echo", args={"x": 1})
    assert call.to_dict() == {
        "type": "chat_tool_call",
        "skill_name": "echo",
        "args": {"x": 1},
    }
    result = ChatToolResult(skill_name="echo", ok=True, detail="done")
    assert result.to_dict()["type"] == "chat_tool_result"
    assert result.to_dict()["ok"] is True


def test_completed_and_failed_and_thinking():
    assert ChatCompleted(answer="final").to_dict() == {
        "type": "chat_completed",
        "answer": "final",
    }
    assert ChatFailed(error="boom").to_dict()["error"] == "boom"
    assert ChatThinking(content="using tools").to_dict()["type"] == "chat_thinking"
