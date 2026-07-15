"""TraceLog：LLM 调用调试轨迹日志。

与 EventLog 一样采用 JSONL 追加式存储，但用途是记录完整的 LLM 请求/响应，
便于事后调试与复盘。record_llm_call 提供便捷入口，调用方传入已序列化好的数据。
"""

import json
from datetime import datetime, timezone
from pathlib import Path


class TraceLog:
    """以 JSONL 文件形式持久化 LLM 调用轨迹。"""

    def __init__(self, path):
        """保存轨迹文件路径，并确保其父目录存在。"""
        # 统一转为 Path
        self.path = Path(path)
        # 创建父目录，已存在则忽略
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, trace: dict):
        """以追加模式写入一条轨迹（一行 JSON）。"""
        # 以追加模式打开文件，写入序列化后的轨迹并换行
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(trace, ensure_ascii=False, default=str) + "\n")

    def read_all(self) -> list[dict]:
        """读取全部轨迹；文件不存在时返回空列表。"""
        # 文件不存在则返回空列表
        if not self.path.exists():
            return []
        # 逐行读取，跳过空行后解析为字典
        with open(self.path, "r", encoding="utf-8") as f:
            return [json.loads(line) for line in (raw.strip() for raw in f) if line]

    def record_llm_call(self, node_id, request_messages, response, usage, tool_calls=None, model=None):
        """组装并落盘一条 LLM 调用轨迹。"""
        # 组装轨迹字典，timestamp 使用当前 UTC ISO 时间
        trace = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "node_id": node_id,
            "model": model,
            "request_messages": request_messages,
            "response": response,
            "usage": usage,
            "tool_calls": tool_calls,
        }
        # 追加写入
        self.append(trace)
