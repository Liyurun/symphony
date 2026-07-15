"""TraceLog（LLM 调试轨迹日志）单元测试。

验证轨迹的追加/读取，以及便捷方法 record_llm_call 组装并落盘轨迹记录。
"""

from pathlib import Path

from symphony.storage import TraceLog


def test_trace_append_read(tmp_path: Path):
    """追加两条轨迹后，read_all 应返回长度 2。"""
    # 构造轨迹日志
    log = TraceLog(tmp_path / "traces.jsonl")
    # 追加两条轨迹
    log.append({"node_id": "n1"})
    log.append({"node_id": "n2"})

    # 断言：读取到两条
    assert len(log.read_all()) == 2


def test_record_llm_call(tmp_path: Path):
    """record_llm_call 应写入一条含 node_id/model/timestamp 的轨迹。"""
    # 构造轨迹日志
    log = TraceLog(tmp_path / "traces.jsonl")
    # 记录一次 LLM 调用
    log.record_llm_call(
        node_id="n1",
        request_messages=[{"role": "user", "content": "hi"}],
        response={"content": "ok"},
        usage={"total_tokens": 10},
        model="doubao",
    )

    # 读取全部轨迹
    traces = log.read_all()

    # 断言：仅有一条轨迹
    assert len(traces) == 1
    # 断言：轨迹含 node_id 字段
    assert traces[0]["node_id"] == "n1"
    # 断言：轨迹含 model 字段
    assert traces[0]["model"] == "doubao"
    # 断言：轨迹含 timestamp 字段
    assert traces[0]["timestamp"]
