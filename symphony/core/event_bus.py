"""Event bus — the synchronization hub between TUI, Web UI, and SOP executor.

All components publish and subscribe through the EventBus.
TUI and Web UI stay in sync because both subscribe to the same event stream.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from symphony.core.event_log import EventLog


@dataclass
class SymphonyEvent:
    """Unified event model for all symphony events.

    Covers:
    - task lifecycle: task_created, task_started, task_completed, task_failed, task_cancelled
    - task takeover: task_claimed, task_released
    - node lifecycle: node_started, node_completed, node_retry, node_failed
    - agent messages: agent_message_start, agent_message_delta, agent_message_end
    - tool calls: tool_call_start, tool_call_update, tool_call_end
    - human intervention: human_intervention_required, human_intervention_response
    - user input: user_input (from both TUI and Web)
    - errors: error
    """

    task_id: str
    event_type: str
    data: dict[str, Any] = field(default_factory=dict)
    node_id: str | None = None
    timestamp: float = field(default_factory=time.time)
    seq: int | None = None  # Populated after persistence


class EventSubscriber(Protocol):
    """Protocol for event subscribers (TUI, WebSocket clients, etc.)."""

    async def on_event(self, event: SymphonyEvent) -> None: ...


EventCallback = Callable[[SymphonyEvent], None]


class EventBus:
    """Central event bus with persistence and real-time distribution.

    Architecture:
    ┌─────────┐     ┌──────────┐     ┌───────────┐
    │   TUI   │     │ EventBus │     │  Web UI   │
    │ (sync)  │◄───►│          │◄───►│ (WS push) │
    └─────────┘     │  ┌─────┐ │     └───────────┘
                    │  │ SQL │ │
                    │  └─────┘ │
                    └──────────┘
    """

    def __init__(self, event_log: EventLog):
        self.event_log = event_log
        self._subscribers: list[EventSubscriber] = []
        self._sync_callbacks: list[EventCallback] = []
        self._task_queues: dict[str, asyncio.Queue[SymphonyEvent]] = {}

    def subscribe(self, subscriber: EventSubscriber) -> None:
        """Subscribe to all events. Called by TUI and WebSocket manager."""
        self._subscribers.append(subscriber)

    def unsubscribe(self, subscriber: EventSubscriber) -> None:
        """Unsubscribe from events."""
        try:
            self._subscribers.remove(subscriber)
        except ValueError:
            pass

    def on_event_sync(self, callback: EventCallback) -> None:
        """Register a synchronous callback (for TUI thread-safe updates)."""
        self._sync_callbacks.append(callback)

    def get_task_queue(self, task_id: str) -> asyncio.Queue[SymphonyEvent]:
        """Get or create a task-specific event queue (for SSE streaming)."""
        if task_id not in self._task_queues:
            self._task_queues[task_id] = asyncio.Queue()
        return self._task_queues[task_id]

    def remove_task_queue(self, task_id: str) -> None:
        self._task_queues.pop(task_id, None)

    async def publish(self, event: SymphonyEvent) -> int:
        """Publish an event: persist to DB + notify all subscribers.

        Returns the seq number from the event log.
        """
        # 1. Persist to SQLite (append-only)
        seq = await self.event_log.append(
            {
                "task_id": event.task_id,
                "node_id": event.node_id,
                "event_type": event.event_type,
                "data": event.data,
                "timestamp": event.timestamp,
            }
        )
        event.seq = seq

        # 2. Notify async subscribers (WebSocket, TUI)
        for sub in self._subscribers:
            try:
                await sub.on_event(event)
            except Exception:
                pass  # Don't let one subscriber break others

        # 3. Notify sync callbacks
        for cb in self._sync_callbacks:
            try:
                cb(event)
            except Exception:
                pass

        # 4. Push to task-specific queue (for SSE)
        if event.task_id in self._task_queues:
            await self._task_queues[event.task_id].put(event)

        return seq
