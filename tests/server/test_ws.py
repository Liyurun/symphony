"""ConnectionManager（WebSocket 连接管理）的单元测试。

用 FakeWebSocket 通过 duck typing 模拟 WebSocket（异步 accept/send_json），
覆盖连接、按 task 发送、断开与广播等行为，并验证对已断开连接的容错。
"""

from symphony.server.ws import ConnectionManager


class FakeWebSocket:
    """最小化的 WebSocket 替身，用于测试。

    仅实现 ConnectionManager 依赖的异步 accept/send_json 方法；
    通过 closed 标志模拟连接断开，断开后 send_json 会抛异常。
    """

    def __init__(self):
        """初始化替身：记录收到的消息，初始未被接受、未关闭。"""
        # 已通过 send_json 收到的消息
        self.sent: list[dict] = []
        # 是否已 accept
        self.accepted = False
        # 是否已断开
        self.closed = False

    async def accept(self) -> None:
        """模拟接受连接。"""
        self.accepted = True

    async def send_json(self, message: dict) -> None:
        """模拟发送 JSON；连接已关闭时抛异常。"""
        # 已断开的连接发送应抛异常，供 ConnectionManager 容错
        if self.closed:
            raise RuntimeError("connection closed")
        self.sent.append(message)


async def test_connect_and_send():
    """connect 后应 accept 连接，send_to_task 时收到消息。"""
    # 连接管理器与替身连接
    manager = ConnectionManager()
    ws = FakeWebSocket()
    # 建立连接
    await manager.connect(ws, "t1")
    # 连接应已被接受
    assert ws.accepted is True
    # 向该 task 发送消息
    await manager.send_to_task("t1", {"type": "hello"})
    # 替身应收到消息
    assert ws.sent == [{"type": "hello"}]


async def test_disconnect():
    """disconnect 后 send_to_task 不应再发给该连接。"""
    # 连接管理器与替身连接
    manager = ConnectionManager()
    ws = FakeWebSocket()
    # 建立连接后断开
    await manager.connect(ws, "t1")
    manager.disconnect(ws, "t1")
    # 断开后再发送
    await manager.send_to_task("t1", {"type": "hello"})
    # 替身不应收到任何消息
    assert ws.sent == []


async def test_broadcast():
    """broadcast 应向所有 task 下的所有连接发送。"""
    # 连接管理器与三个替身连接（分属两个 task）
    manager = ConnectionManager()
    ws1 = FakeWebSocket()
    ws2 = FakeWebSocket()
    ws3 = FakeWebSocket()
    # 建立连接
    await manager.connect(ws1, "t1")
    await manager.connect(ws2, "t1")
    await manager.connect(ws3, "t2")
    # 广播一条消息
    await manager.broadcast({"type": "ping"})
    # 三个连接都应收到
    assert ws1.sent == [{"type": "ping"}]
    assert ws2.sent == [{"type": "ping"}]
    assert ws3.sent == [{"type": "ping"}]


async def test_send_tolerates_closed_connection():
    """已断开连接发送异常应被忽略，不影响其余连接。"""
    # 连接管理器与两个替身连接
    manager = ConnectionManager()
    ws_open = FakeWebSocket()
    ws_closed = FakeWebSocket()
    # 建立连接
    await manager.connect(ws_open, "t1")
    await manager.connect(ws_closed, "t1")
    # 模拟其中一个断开
    ws_closed.closed = True
    # 发送不应向外抛异常
    await manager.send_to_task("t1", {"type": "x"})
    # 正常连接仍应收到消息
    assert ws_open.sent == [{"type": "x"}]
