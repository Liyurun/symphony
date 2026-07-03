"""Human intervention manager — pause SOP execution for human approval.

When a node has human_intervention=True, the executor:
1. Publishes a human_intervention_required event
2. Waits for a human_intervention_response event
3. Resumes (approved) or retries (rejected) based on the response

Both TUI and Web UI can respond to intervention requests.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from symphony.core.event_bus import EventBus, SymphonyEvent

logger = logging.getLogger(__name__)


class HumanInterventionManager:
    """Manages human-in-the-loop interactions for SOP nodes."""

    def __init__(self, event_bus: EventBus):
        self.event_bus = event_bus
        self._pending: dict[str, asyncio.Future[tuple[bool, str]]] = {}
        # Separate registry for question-answer interactions (returns the user's
        # free-form answer text, not an approve/reject tuple).
        self._pending_questions: dict[str, asyncio.Future[str]] = {}

    async def request_approval(
        self,
        task_id: str,
        node_id: str,
        node_name: str,
        result: dict[str, Any],
        timeout: int = 300,
    ) -> tuple[bool, str]:
        """Request human approval for a node's result.

        Returns:
            (approved: bool, feedback: str)
        """
        request_id = f"{task_id}:{node_id}"

        # Publish intervention event
        await self.event_bus.publish(
            SymphonyEvent(
                task_id=task_id,
                node_id=node_id,
                event_type="human_intervention_required",
                data={
                    "request_id": request_id,
                    "node_name": node_name,
                    "result_preview": self._truncate_result(result),
                    "timeout": timeout,
                },
            )
        )

        # Wait for response
        future: asyncio.Future[tuple[bool, str]] = asyncio.get_event_loop().create_future()
        self._pending[request_id] = future

        try:
            approved, feedback = await asyncio.wait_for(future, timeout=timeout)
            return approved, feedback
        except asyncio.TimeoutError:
            logger.warning(f"Human intervention timed out for {request_id}")
            return False, "timeout"
        finally:
            self._pending.pop(request_id, None)

    async def respond(
        self, task_id: str, node_id: str, approved: bool, feedback: str = ""
    ) -> None:
        """Respond to a pending intervention request."""
        request_id = f"{task_id}:{node_id}"

        await self.event_bus.publish(
            SymphonyEvent(
                task_id=task_id,
                node_id=node_id,
                event_type="human_intervention_response",
                data={"approved": approved, "feedback": feedback},
            )
        )

        future = self._pending.get(request_id)
        if future and not future.done():
            future.set_result((approved, feedback))

    async def request_answer(
        self,
        task_id: str,
        node_id: str,
        node_name: str,
        questions: list[dict],
        reason: str = "",
        timeout: int = 3600,
    ) -> str:
        """Request free-form answer(s) from the user for a node's questions.

        Publishes a ``user_question_required`` event carrying the structured
        questions, then blocks until the user answers (via :meth:`answer`).
        Returns the combined answer text to feed back into the node re-run.
        Returns "" on timeout (caller decides how to proceed).
        """
        request_id = f"{task_id}:{node_id}"

        await self.event_bus.publish(
            SymphonyEvent(
                task_id=task_id,
                node_id=node_id,
                event_type="user_question_required",
                data={
                    "request_id": request_id,
                    "node_name": node_name,
                    "questions": questions,
                    "reason": reason,
                    "timeout": timeout,
                },
            )
        )

        future: asyncio.Future[str] = asyncio.get_event_loop().create_future()
        self._pending_questions[request_id] = future
        try:
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            logger.warning(f"User question timed out for {request_id}")
            return ""
        finally:
            self._pending_questions.pop(request_id, None)

    async def answer(self, task_id: str, node_id: str, answer_text: str) -> None:
        """Answer a pending ``request_answer`` with the user's text."""
        request_id = f"{task_id}:{node_id}"

        await self.event_bus.publish(
            SymphonyEvent(
                task_id=task_id,
                node_id=node_id,
                event_type="user_question_answered",
                data={"answer": answer_text},
            )
        )

        future = self._pending_questions.get(request_id)
        if future and not future.done():
            future.set_result(answer_text)

    def _truncate_result(self, result: dict, max_length: int = 500) -> dict:
        """Truncate large results for display in the intervention prompt."""
        truncated = {}
        for key, value in result.items():
            if isinstance(value, str) and len(value) > max_length:
                truncated[key] = value[:max_length] + "..."
            else:
                truncated[key] = value
        return truncated
