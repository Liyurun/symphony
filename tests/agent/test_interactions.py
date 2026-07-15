"""Interaction 模型测试。"""

import pytest
from pydantic import ValidationError

from symphony.agent.interactions import InteractionAnswer, InteractionRequest


def test_text_interaction_accepts_prompt():
    """文本反问应包含 prompt 与 schema。"""
    req = InteractionRequest.text(
        session_id="chat-1",
        task_id="task-1",
        node_id="node-1",
        prompt="请输入产品线",
    )

    assert req.type == "interaction_requested"
    assert req.session_id == "chat-1"
    assert req.task_id == "task-1"
    assert req.node_id == "node-1"
    assert req.prompt == "请输入产品线"
    assert req.input_schema["type"] == "object"
    assert req.status == "pending"


def test_select_interaction_keeps_options():
    """单选反问应保留 options。"""
    req = InteractionRequest.select(
        session_id="chat-1",
        task_id="task-1",
        node_id="node-1",
        prompt="请选择",
        options=[{"label": "A", "value": "a"}],
    )

    assert req.task_id == "task-1"
    assert req.node_id == "node-1"
    assert req.options[0].value == "a"
    assert req.multi_select is False


def test_select_interaction_accepts_multi_select():
    """多选反问应保留 multi_select 标记。"""
    req = InteractionRequest.select(
        session_id="chat-1",
        prompt="请选择",
        options=[{"label": "A", "value": "a"}],
        multi_select=True,
    )

    assert req.multi_select is True


def test_answer_requires_dict():
    """answer 必须是 dict。"""
    with pytest.raises(ValidationError):
        InteractionAnswer(
            interaction_id="int-1",
            session_id="chat-1",
            answer="bad",
        )
