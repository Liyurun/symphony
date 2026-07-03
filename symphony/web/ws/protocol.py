"""WebSocket message protocol — structured message types for client-server communication.

Client -> Server messages:
    subscribe_task, unsubscribe_task, human_response, user_input,
    create_task, start_task, cancel_task, pause_task, resume_task,
    claim_task, release_task

Server -> Client messages:
    event, task_update, error
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel


class ClientMessageType(str, Enum):
    SUBSCRIBE_TASK = "subscribe_task"
    UNSUBSCRIBE_TASK = "unsubscribe_task"
    HUMAN_RESPONSE = "human_response"
    USER_INPUT = "user_input"
    CREATE_TASK = "create_task"
    START_TASK = "start_task"
    CANCEL_TASK = "cancel_task"
    PAUSE_TASK = "pause_task"
    RESUME_TASK = "resume_task"
    CLAIM_TASK = "claim_task"
    RELEASE_TASK = "release_task"


class ServerMessageType(str, Enum):
    EVENT = "event"
    TASK_UPDATE = "task_update"
    ERROR = "error"
    INITIAL_STATE = "initial_state"


class ClientMessage(BaseModel):
    """Message from a web client to the server."""

    type: ClientMessageType
    task_id: str | None = None
    node_id: str | None = None
    message: str = ""
    sop_name: str = ""
    # Free-form question for an ad-hoc (方案A) Q&A task created without a SOP.
    prompt: str = ""
    approved: bool = False
    feedback: str = ""
    client_id: str = ""


class ServerMessage(BaseModel):
    """Message from the server to a web client."""

    type: ServerMessageType
    task_id: str | None = None
    node_id: str | None = None
    event_type: str | None = None
    data: dict[str, Any] = {}
    timestamp: float | None = None
    message: str = ""

    @classmethod
    def from_symphony_event(cls, event: Any) -> "ServerMessage":
        """Create a ServerMessage from a SymphonyEvent."""
        return cls(
            type=ServerMessageType.EVENT,
            task_id=event.task_id,
            node_id=event.node_id,
            event_type=event.event_type,
            data=event.data,
            timestamp=event.timestamp,
        )

    @classmethod
    def task_status_update(cls, task_id: str, status: str, **extra) -> "ServerMessage":
        """Create a task status update message."""
        return cls(
            type=ServerMessageType.TASK_UPDATE,
            task_id=task_id,
            event_type="task_status",
            data={"status": status, **extra},
        )

    @classmethod
    def error(cls, message: str, task_id: str | None = None) -> "ServerMessage":
        """Create an error message."""
        return cls(
            type=ServerMessageType.ERROR,
            task_id=task_id,
            message=message,
        )

    @classmethod
    def initial_state(cls, tasks: list, sops: list) -> "ServerMessage":
        """Create an initial state message."""
        return cls(
            type=ServerMessageType.INITIAL_STATE,
            data={"tasks": tasks, "sops": sops},
        )
