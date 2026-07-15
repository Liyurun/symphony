"""EventLog：基于 JSONL 的追加式事件日志。

事件溯源模式下 events.jsonl 是任务状态的真相来源。本模块提供最小化的
追加写入与读取能力：每行一个 JSON 对象，追加写不覆盖历史，读取时逐行解析。
"""

import json
from pathlib import Path


class EventLog:
    """以 JSONL 文件形式持久化事件序列。"""

    def __init__(self, path):
        """保存日志文件路径，并确保其父目录存在。"""
        # 统一转为 Path 便于后续操作
        self.path = Path(path)
        # 创建父目录，已存在则忽略
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, event: dict):
        """以追加模式写入一条事件（一行 JSON）。"""
        # 以追加模式打开文件，写入序列化后的事件并换行
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")

    def read_all(self) -> list[dict]:
        """读取全部事件；文件不存在时返回空列表。"""
        # 文件不存在则无历史，返回空列表
        if not self.path.exists():
            return []
        # 逐行读取，跳过空行后解析为字典
        with open(self.path, "r", encoding="utf-8") as f:
            return [json.loads(line) for line in (raw.strip() for raw in f) if line]

    def read_since(self, offset: int) -> list[dict]:
        """返回从指定偏移开始的事件（用于增量拉取）。"""
        # 复用 read_all 后按偏移切片
        return self.read_all()[offset:]

    def count(self) -> int:
        """统计事件条数；文件不存在时返回 0。"""
        # 文件不存在则计数为 0
        if not self.path.exists():
            return 0
        # 统计非空行数量
        with open(self.path, "r", encoding="utf-8") as f:
            return sum(1 for raw in f if raw.strip())
