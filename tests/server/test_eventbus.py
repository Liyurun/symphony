"""EventBus（进程内发布/订阅）的单元测试。

覆盖 EventBus 的核心行为：
1. 订阅某个 task 后能收到该 task 的发布事件；
2. 取消订阅后不再收到事件；
3. subscribe_all 通配订阅能收到任意 task 的发布事件。
"""

from symphony.server.eventbus import EventBus


def test_subscribe_publish():
    """订阅 task "t1"，publish 后回调应收到 event dict。"""
    # 事件总线实例
    bus = EventBus()
    # 收集回调收到的事件
    received: list[dict] = []
    # 订阅 t1
    bus.subscribe("t1", received.append)
    # 发布一条事件
    bus.publish("t1", {"type": "task_started", "task_id": "t1"})
    # 回调应恰好收到这条事件
    assert received == [{"type": "task_started", "task_id": "t1"}]


def test_unsubscribe():
    """取消订阅后回调不应再收到事件。"""
    # 事件总线实例
    bus = EventBus()
    # 收集回调收到的事件
    received: list[dict] = []
    # 订阅后立即取消
    bus.subscribe("t1", received.append)
    bus.unsubscribe("t1", received.append)
    # 发布事件
    bus.publish("t1", {"type": "task_started"})
    # 已取消订阅，不应收到任何事件
    assert received == []


def test_subscribe_all():
    """subscribe_all 后任意 task 的 publish 都应被通知。"""
    # 事件总线实例
    bus = EventBus()
    # 收集通配订阅者收到的事件
    received: list[dict] = []
    # 注册通配订阅
    bus.subscribe_all(received.append)
    # 发布两个不同 task 的事件
    bus.publish("t1", {"type": "a"})
    bus.publish("t2", {"type": "b"})
    # 通配订阅者应收到全部事件
    assert received == [{"type": "a"}, {"type": "b"}]


def test_publish_isolates_callback_exceptions():
    """单个订阅者抛异常不应影响其余订阅者接收事件。"""
    # 事件总线实例
    bus = EventBus()
    # 正常订阅者的收集列表
    received: list[dict] = []

    # 一个会抛异常的坏订阅者
    def bad_callback(event: dict) -> None:
        raise RuntimeError("boom")

    # 先注册坏订阅者，再注册正常订阅者
    bus.subscribe("t1", bad_callback)
    bus.subscribe("t1", received.append)
    # 发布事件不应向外抛异常
    bus.publish("t1", {"type": "ok"})
    # 正常订阅者仍应收到事件
    assert received == [{"type": "ok"}]
