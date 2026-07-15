"""ConnectionManager：WebSocket 连接管理器（异步）。

按 task_id 维护活跃 WebSocket 连接集合，提供接受连接、按 task 定向发送、
断开与全局广播能力。为便于测试，不直接依赖 FastAPI 的 WebSocket 类型，
而是采用 duck typing：任何具备异步 accept/send_json 方法的对象都可作为连接。
发送边界会忽略已断开连接抛出的异常。
"""

from typing import Any


class ConnectionManager:
    """管理按 task 分组的 WebSocket 连接。"""

    def __init__(self) -> None:
        """初始化空的连接表。"""
        # task_id -> 该 task 下的活跃连接集合
        self._connections: dict[str, set[Any]] = {}

    async def connect(self, websocket: Any, task_id: str) -> None:
        """接受一个 WebSocket 连接并纳入指定 task 的连接集合。"""
        # 先接受连接（握手）
        await websocket.accept()
        # 频道不存在时先建空集合，再加入连接
        self._connections.setdefault(task_id, set()).add(websocket)

    def disconnect(self, websocket: Any, task_id: str) -> None:
        """从指定 task 的连接集合中移除一个连接，不存在则静默忽略。"""
        # 取出该 task 的连接集合
        conns = self._connections.get(task_id)
        # 集合存在且包含该连接才移除
        if conns is not None and websocket in conns:
            conns.discard(websocket)

    async def send_to_task(self, task_id: str, message: dict) -> None:
        """向指定 task 的所有连接发送 JSON 消息，忽略已断开连接。"""
        # 拷贝一份集合，避免发送期间集合被并发修改
        for websocket in list(self._connections.get(task_id, set())):
            # 发送边界：连接可能已断开，忽略其异常
            try:
                await websocket.send_json(message)
            except Exception:
                continue

    async def broadcast(self, message: dict) -> None:
        """向所有 task 下的所有连接广播 JSON 消息，忽略已断开连接。"""
        # 遍历所有频道的连接集合
        for conns in list(self._connections.values()):
            for websocket in list(conns):
                # 发送边界：连接可能已断开，忽略其异常
                try:
                    await websocket.send_json(message)
                except Exception:
                    continue
