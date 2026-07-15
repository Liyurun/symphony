"""EventLog（事件日志）单元测试。

验证追加写入、全量读取、按偏移读取以及行数统计等行为，
覆盖文件不存在与空文件等边界场景。
"""

from pathlib import Path

from symphony.storage import EventLog


def test_eventlog_append_and_read(tmp_path: Path):
    """追加两条事件后，read_all 应返回长度 2 且值正确。"""
    # 在临时目录构造事件日志文件路径
    log = EventLog(tmp_path / "events.jsonl")
    # 追加两条事件
    log.append({"seq": 0, "type": "a"})
    log.append({"seq": 1, "type": "b"})

    # 读取全部事件
    events = log.read_all()

    # 断言：读到两条
    assert len(events) == 2
    # 断言：第一条内容正确
    assert events[0] == {"seq": 0, "type": "a"}
    # 断言：第二条内容正确
    assert events[1] == {"seq": 1, "type": "b"}


def test_eventlog_read_since(tmp_path: Path):
    """追加 5 条事件后，read_since(2) 应返回长度 3 且首个 seq==2。"""
    # 构造事件日志
    log = EventLog(tmp_path / "events.jsonl")
    # 依次追加 seq 0..4 共 5 条事件
    for i in range(5):
        log.append({"seq": i})

    # 从偏移 2 开始读取
    events = log.read_since(2)

    # 断言：剩余 3 条
    assert len(events) == 3
    # 断言：首个事件 seq 为 2
    assert events[0]["seq"] == 2


def test_eventlog_count(tmp_path: Path):
    """追加 3 条后 count()==3；文件不存在时 count()==0。"""
    # 构造事件日志
    log = EventLog(tmp_path / "events.jsonl")
    # 追加 3 条事件
    for i in range(3):
        log.append({"seq": i})

    # 断言：统计到 3 条
    assert log.count() == 3

    # 针对一个不存在的文件构造事件日志
    empty = EventLog(tmp_path / "missing.jsonl")
    # 断言：文件不存在时统计为 0
    assert empty.count() == 0
