"""SessionLog / SessionManager 单元测试。"""

from pathlib import Path

from symphony.storage import SessionLog, SessionManager, SessionStatus, SessionType


def test_session_log_creates_files_and_appends_records(tmp_path: Path):
    """SessionLog 应创建会话目录并读写 transcript/events/traces/interactions。"""
    log = SessionLog(tmp_path / "chat-20260710-abcd1234")
    log.save_meta(
        session_id="chat-20260710-abcd1234",
        type=SessionType.CHAT,
        title="Demo chat",
        status=SessionStatus.RUNNING,
        source="test",
    )
    log.append_transcript({"role": "user", "content": "hi"})
    log.append_event({"type": "chat_started"})
    log.append_trace({"model": "fake"})
    log.append_interaction({"type": "interaction_requested", "interaction_id": "int-1"})

    assert log.load_meta()["session_id"] == "chat-20260710-abcd1234"
    assert log.read_transcript() == [{"role": "user", "content": "hi"}]
    assert log.read_events() == [{"type": "chat_started"}]
    assert log.read_traces() == [{"model": "fake"}]
    assert log.read_interactions()[0]["interaction_id"] == "int-1"


def test_session_manager_creates_chat_and_sop_sessions(tmp_path: Path):
    """SessionManager 应生成可排序的 Chat/SOP session 元信息。"""
    manager = SessionManager(tmp_path)

    chat = manager.create_chat(title="Ask anything", source="web")
    sop = manager.create_sop(
        title="Run SOP",
        sop_id="demo-sop",
        task_id="demo-task-1",
        source="web",
    )

    assert chat.session_id.startswith("chat-")
    assert sop.session_id.startswith("sop-")
    assert manager.get(chat.session_id).load_meta()["type"] == "chat"
    assert manager.get(sop.session_id).load_meta()["task_id"] == "demo-task-1"
    assert [item["session_id"] for item in manager.list_sessions()] == [
        sop.session_id,
        chat.session_id,
    ]


def test_session_manager_updates_status(tmp_path: Path):
    """SessionManager.update_status 应更新时间与状态。"""
    manager = SessionManager(tmp_path)
    session = manager.create_chat(title="Status", source="test")

    before = manager.get(session.session_id).load_meta()["updated_at"]
    manager.update_status(session.session_id, SessionStatus.COMPLETED)
    meta = manager.get(session.session_id).load_meta()

    assert meta["status"] == "completed"
    assert meta["updated_at"] >= before
