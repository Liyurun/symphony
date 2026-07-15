"""EventBus：进程内发布/订阅事件总线。

以 task_id 为频道维护订阅者回调列表，供任务执行过程中把事件推送给
关注该任务的所有订阅者（如 WebSocket 推送、看板总览等）。
额外支持通配订阅（key="*"），用于订阅所有任务的事件流。
回调以同步方式调用，单个订阅者异常不会影响其余订阅者。
"""

from typing import Callable

# 通配订阅使用的频道键，收到任意 task 的发布都会通知
ALL_KEY = "*"


class EventBus:
    """进程内的发布/订阅事件总线。"""

    def __init__(self) -> None:
        """初始化空的订阅表。"""
        # task_id -> 回调列表；key="*" 存储通配订阅者
        self._subscribers: dict[str, list[Callable[[dict], None]]] = {}

    def subscribe(self, task_id: str, callback: Callable[[dict], None]) -> None:
        """为指定 task 追加一个订阅回调。"""
        # 频道不存在时先建空列表，再追加回调
        self._subscribers.setdefault(task_id, []).append(callback)

    def subscribe_all(self, callback: Callable[[dict], None]) -> None:
        """注册一个通配订阅，接收所有 task 的发布事件。"""
        # 复用 subscribe，频道键固定为 "*"
        self.subscribe(ALL_KEY, callback)

    def unsubscribe(self, task_id: str, callback: Callable[[dict], None]) -> None:
        """移除指定 task 下的某个订阅回调，不存在则静默忽略。"""
        # 取出该频道的回调列表
        callbacks = self._subscribers.get(task_id)
        # 频道存在且回调在列表中才移除
        if callbacks is not None and callback in callbacks:
            callbacks.remove(callback)

    def unsubscribe_all(self, callback: Callable[[dict], None]) -> None:
        """移除一个通配订阅回调，不存在则静默忽略。"""
        # 复用 unsubscribe，频道键固定为 "*"
        self.unsubscribe(ALL_KEY, callback)

    def publish(self, task_id: str, event: dict) -> None:
        """向指定 task 的订阅者与所有通配订阅者发布一条事件。"""
        # 汇总目标回调：该 task 的订阅者 + 通配订阅者
        targets = list(self._subscribers.get(task_id, []))
        targets.extend(self._subscribers.get(ALL_KEY, []))
        # 逐个调用回调
        for callback in targets:
            # 回调边界：单个订阅者异常不应影响其余订阅者
            try:
                callback(event)
            except Exception:
                # 静默吞掉订阅者异常，保证发布过程健壮
                continue
