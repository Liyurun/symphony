"""Multi-task orchestrator — manages concurrent SOP task executions.

Each task runs in its own asyncio Task, driven by the SOPExecutor.
The TaskManager tracks all active tasks and their lifecycle.
Supports task claiming for the takeover pattern.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Optional

from symphony.core.event_bus import EventBus, SymphonyEvent
from symphony.core.event_log import EventLog
from symphony.core.pi_bridge import PiBridge
from symphony.sop.sop_definition import SOPDefinition

if TYPE_CHECKING:
    from symphony.sop.sop_executor import SOPExecutor

logger = logging.getLogger(__name__)


class TaskStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    WAITING_HUMAN = "waiting_human"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class Task:
    """A single SOP task instance."""

    task_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    sop_name: str = ""
    sop_version: str = "1.0"
    status: TaskStatus = TaskStatus.PENDING
    claimed_by: str | None = None
    claimed_at: float | None = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    metadata: dict = field(default_factory=dict)

    # Internal
    _asyncio_task: asyncio.Task | None = field(default=None, repr=False)
    _cancel_event: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    _pause_event: asyncio.Event | None = field(default=None, repr=False)


class TaskManager:
    """Manages multiple concurrent SOP task executions.

    Each task:
    1. Gets a unique task_id (UUID)
    2. Is persisted in the EventLog
    3. Runs via SOPExecutor in its own asyncio.Task
    4. Publishes lifecycle events to the EventBus
    5. Can be claimed by a specific client (takeover pattern)
    """

    def __init__(
        self,
        event_bus: EventBus,
        event_log: EventLog,
        pi_bridge: PiBridge,
        llm_provider=None,
        pi_pool=None,
    ):
        self.event_bus = event_bus
        self.event_log = event_log
        self.pi_bridge = pi_bridge
        self.llm_provider = llm_provider
        # Optional per-task pi process pool. When supplied (by the CLI), each
        # task runs in its own dedicated pi subprocess so multiple SOPs execute
        # in parallel with isolated event streams / aborts. When omitted (tests,
        # embedded use) all task execution degrades to the shared ``pi_bridge``
        # and no extra subprocesses are spawned.
        self.pi_pool = pi_pool
        self._executor = None  # Lazy init
        self._tasks: dict[str, Task] = {}
        # Remember the SOP object per task so node-level redirect/rerun can
        # re-drive it (ad-hoc SOPs are not persisted as YAML files).
        self._task_sops: dict[str, SOPDefinition] = {}
        # Single shared human-intervention manager so that approvals coming from
        # ANY surface (Web WS, TUI, REST) resolve the SAME pending future the
        # executor is awaiting. Creating a fresh manager elsewhere would leave the
        # executor blocked forever (the classic "approve does nothing" bug).
        from symphony.sop.human_loop import HumanInterventionManager
        self.human_manager = HumanInterventionManager(event_bus)

    def _get_executor(self) -> "SOPExecutor":
        if self._executor is None:
            from symphony.sop.sop_executor import SOPExecutor
            self._executor = SOPExecutor(
                self.pi_bridge, self.event_log, self.event_bus, self.llm_provider,
                human_manager=self.human_manager, bridge_pool=self.pi_pool,
            )
        return self._executor

    def _prefer_direct_llm_for_plain_qa(self, skill: str = "") -> bool:
        """Use direct LLM for plain chat when the active provider is non-pi.

        Mira/custom_http providers are implemented as Symphony-side direct LLM
        adapters, not pi model backends. A plain chat turn (no skill requested)
        should therefore run via the direct LLM path so the user's selected
        provider is honored instead of falling back to pi's unrelated saved
        default model.
        """
        if skill:
            return False
        if not self.llm_provider or not getattr(self.llm_provider, "is_available", False):
            return False
        llm_type = getattr(getattr(self.llm_provider, "config", None), "type", "")
        return llm_type in {"mira", "custom_http", "http", "nonstandard"}

    def _pi_context_metadata(self) -> dict:
        """Return best-effort pi context evidence for task metadata.

        Tests and some lightweight integrations use pi stubs that do not expose
        the full PiBridge ``config`` object, so this must be optional.
        """
        config = getattr(self.pi_bridge, "config", None)
        if config is None:
            return {}
        context_files = []
        context_file_infos = getattr(config, "context_file_infos", None)
        if callable(context_file_infos):
            context_files = context_file_infos()
        return {
            "pi_cwd": getattr(config, "cwd", None),
            "pi_context_files": context_files,
        }

    async def respond_human(
        self, task_id: str, node_id: str, approved: bool, feedback: str = ""
    ) -> None:
        """Route a human-intervention response to the shared manager."""
        await self.human_manager.respond(task_id, node_id, approved, feedback)

    async def answer_question(
        self, task_id: str, node_id: str, answer: str
    ) -> None:
        """Route a user's answer to a node's pending ``needs_user_input`` request."""
        await self.human_manager.answer(task_id, node_id, answer)

    async def redirect_node(
        self, task_id: str, node_id: str, extra_instruction: str = ""
    ) -> None:
        """Interrupt & rerun a node ("打断并重来"), auto-cascading downstream.

        Node-level control: the whole node re-runs with the operator's extra
        instruction appended, and every node downstream of it re-runs too.
        """
        task = self._tasks.get(task_id)
        if not task:
            raise ValueError(f"Task {task_id} not found")

        sop = self._task_sops.get(task_id)
        if sop is None:
            raise ValueError(
                f"Task {task_id} has no active SOP to redirect (not running?)"
            )

        # Ensure a pause_event exists so the executor's re-drive can gate on it.
        if task._pause_event is None:
            task._pause_event = asyncio.Event()
            task._pause_event.set()

        task.status = TaskStatus.RUNNING
        await self.event_log.update_task_status(task_id, TaskStatus.RUNNING)

        executor = self._get_executor()
        # Re-drive in the background so the API call returns promptly.
        asyncio.create_task(
            executor.redirect_node(
                task_id, sop, node_id, extra_instruction,
                task._cancel_event, task._pause_event,
            )
        )

    async def complete_node(
        self,
        task_id: str,
        node_id: str,
        artifact: dict,
        rerun_downstream: bool = True,
    ) -> None:
        """Manually mark a node completed with an operator-supplied artifact.

        Format-validates the artifact against the node's output type (raising
        ValueError on mismatch) and, by default, re-runs the downstream closure
        so the flow continues automatically.
        """
        task = self._tasks.get(task_id)
        if not task:
            raise ValueError(f"Task {task_id} not found")

        sop = self._task_sops.get(task_id)
        if sop is None:
            raise ValueError(
                f"Task {task_id} has no active SOP (not started?)"
            )

        if task._pause_event is None:
            task._pause_event = asyncio.Event()
            task._pause_event.set()

        # Validate synchronously (before flipping status) so the API can surface
        # a 400 without leaving the task stuck in RUNNING; the (possibly long)
        # downstream re-run happens in the background.
        from symphony.sop.artifact import ArtifactType, validate_artifact_format

        node = sop.get_node(node_id)
        if node is None:
            raise ValueError(f"node '{node_id}' not found")
        raw_type = artifact.get("type") or node.output_artifact_type
        try:
            atype = ArtifactType(raw_type)
        except ValueError:
            raise ValueError(f"未知的产物类型：{raw_type}")
        if atype != node.output_artifact_type:
            raise ValueError(f"产物类型必须为 {node.output_artifact_type.value}")
        ok, err = validate_artifact_format(atype, artifact.get("value", ""))
        if not ok:
            raise ValueError(err)

        task.status = TaskStatus.RUNNING
        await self.event_log.update_task_status(task_id, TaskStatus.RUNNING)

        executor = self._get_executor()
        asyncio.create_task(
            executor.complete_node_manually(
                task_id, sop, node_id, artifact,
                task._cancel_event, task._pause_event, rerun_downstream,
            )
        )

    async def create_task(
        self,
        sop: SOPDefinition,
        sop_version: str = "1.0",
        metadata: dict | None = None,
    ) -> Task:
        """Create a new task and persist it."""
        task = Task(
            sop_name=sop.name,
            sop_version=sop_version,
            metadata=metadata or {},
        )
        self._tasks[task.task_id] = task
        # Keep the SOP object so start/redirect can re-drive it (esp. ad-hoc).
        self._task_sops[task.task_id] = sop

        await self.event_log.create_task(
            task.task_id, sop.name, sop_version, metadata
        )

        await self.event_bus.publish(
            SymphonyEvent(
                task_id=task.task_id,
                event_type="task_created",
                data={"sop_name": sop.name, "sop_version": sop_version},
            )
        )

        return task

    async def create_and_start_qa(
        self, prompt: str, *, skill: str = "", timeout: int = 300
    ) -> Task:
        """方案A: create a one-node ad-hoc task for a single question and start it.

        This is the "everything is a task" entry point — even a plain single-turn
        Q&A becomes an observable, interruptible, persisted task.
        """
        from symphony.sop.sop_definition import make_adhoc_sop
        from symphony.sop.sop_definition import NodeExecutor

        sop = make_adhoc_sop(prompt, skill=skill, timeout=timeout)
        if self._prefer_direct_llm_for_plain_qa(skill):
            sop.nodes[0].executor = NodeExecutor.LLM
            sop.metadata["executor"] = "llm"
        task = await self.create_task(
            sop,
            metadata={
                "adhoc": True,
                "kind": "qa",
                "prompt": prompt,
                "executor": sop.nodes[0].executor,
                **self._pi_context_metadata(),
            },
        )
        await self.start_task(task.task_id, sop)
        return task

    async def ask_follow_up(
        self, task_id: str, prompt: str, *, skill: str = "", timeout: int = 300
    ) -> str:
        """方案A multi-turn: append a new turn node to an existing ad-hoc task.

        The new node depends on the previous turn (conversation continuity) and
        is executed via the downstream re-drive path. Returns the new node id.
        """
        from symphony.sop.sop_definition import append_turn_node
        from symphony.sop.sop_definition import NodeExecutor

        sop = self._task_sops.get(task_id)
        if sop is None:
            raise ValueError(f"Task {task_id} has no active SOP (not running?)")
        task = self._tasks.get(task_id)
        if task is None:
            raise ValueError(f"Task {task_id} not found")

        node = append_turn_node(sop, prompt, skill=skill, timeout=timeout)
        if self._prefer_direct_llm_for_plain_qa(skill):
            node.executor = NodeExecutor.LLM

        if task._pause_event is None:
            task._pause_event = asyncio.Event()
            task._pause_event.set()
        task.status = TaskStatus.RUNNING
        await self.event_log.update_task_status(task_id, TaskStatus.RUNNING)

        node_results = getattr(self._get_executor(), "_task_node_results", {}).get(task_id)

        async def _run_new_turn() -> None:
            executor = self._get_executor()
            results = executor._task_node_results.setdefault(task_id, node_results or {})
            await executor._execute_node(
                task_id, node, sop, results, task._cancel_event, task._pause_event
            )
            done = results.get(node.id, {}).get("status")
            from symphony.sop.sop_executor import NodeStatus
            status = "completed" if done == NodeStatus.COMPLETED else "failed"
            await self.event_log.update_task_status(task_id, status)
            await self.event_bus.publish(
                SymphonyEvent(
                    task_id=task_id,
                    event_type="task_completed" if status == "completed" else "task_failed",
                    data={"appended_node": node.id},
                )
            )

        asyncio.create_task(_run_new_turn())
        return node.id

    async def start_task(self, task_id: str, sop: SOPDefinition) -> None:
        """Start executing a task's SOP."""
        task = self._tasks.get(task_id)
        if not task:
            raise ValueError(f"Task {task_id} not found")

        task.status = TaskStatus.RUNNING
        task._pause_event = asyncio.Event()
        task._pause_event.set()  # Not paused initially
        await self.event_log.update_task_status(task_id, TaskStatus.RUNNING)

        # Give this task its own dedicated pi subprocess so it runs fully
        # in parallel with other tasks (isolated event stream + abort).
        if self.pi_pool is not None:
            await self.pi_pool.acquire(task_id)

        # Remember the SOP so node-level redirect/rerun can re-drive it without
        # re-fetching (ad-hoc SOPs are not files in the registry).
        self._task_sops[task_id] = sop

        await self.event_bus.publish(
            SymphonyEvent(
                task_id=task_id,
                event_type="task_started",
                data={"sop_name": sop.name},
            )
        )

        # Extract the user-supplied inputs (prompt / structured fields) so root
        # nodes actually receive them.
        initial_input = self._extract_initial_input(task)

        # Run SOP executor in a background asyncio task
        task._asyncio_task = asyncio.create_task(
            self._run_task(
                task_id, sop, task._cancel_event, task._pause_event, initial_input
            )
        )

    @staticmethod
    def _extract_initial_input(task: "Task") -> dict:
        """Derive a root-node input dict from a task's metadata.

        Accepts either a structured ``inputs`` dict or a free-form ``prompt``
        string in the task metadata. A bare prompt is wrapped as
        ``{"prompt": <text>}`` so schema-less ad-hoc nodes and schema'd SOP
        nodes can both consume it.
        """
        md = task.metadata or {}
        inputs = md.get("inputs")
        if isinstance(inputs, dict) and inputs:
            return dict(inputs)
        prompt = md.get("prompt")
        if isinstance(prompt, str) and prompt.strip():
            return {"prompt": prompt}
        return {}

    async def pause_task(self, task_id: str) -> None:
        """Pause a running task."""
        task = self._tasks.get(task_id)
        if not task:
            return

        if task._pause_event:
            task._pause_event.clear()

        task.status = TaskStatus.PAUSED
        await self.event_log.update_task_status(task_id, TaskStatus.PAUSED)

        await self.event_bus.publish(
            SymphonyEvent(
                task_id=task_id,
                event_type="task_paused",
            )
        )

    async def resume_task(self, task_id: str, sop: SOPDefinition) -> None:
        """Resume a paused task."""
        task = self._tasks.get(task_id)
        if not task:
            return

        if task._pause_event:
            task._pause_event.set()

        task.status = TaskStatus.RUNNING
        await self.event_log.update_task_status(task_id, TaskStatus.RUNNING)

        await self.event_bus.publish(
            SymphonyEvent(
                task_id=task_id,
                event_type="task_resumed",
            )
        )

    async def cancel_task(self, task_id: str) -> None:
        """Cancel a running task."""
        task = self._tasks.get(task_id)
        if not task:
            return

        task._cancel_event.set()
        task.status = TaskStatus.CANCELLED
        await self.event_log.update_task_status(task_id, TaskStatus.CANCELLED)

        # If the task is currently inside pi RPC/tool execution, setting the
        # local cancel_event is not enough: the RPC call may be blocked waiting
        # for a long-running shell/login command. Ask THIS TASK's own pi bridge
        # to abort as well (not the shared one) so we do not interrupt other
        # concurrently-running tasks.
        try:
            bridge = self.pi_pool.get(task_id) if self.pi_pool else self.pi_bridge
            if bridge and getattr(bridge, "_started", False):
                await bridge.abort()
        except Exception as e:
            logger.debug("pi abort during task cancel failed: %s", e)

        await self.event_bus.publish(
            SymphonyEvent(
                task_id=task_id,
                event_type="task_cancelled",
            )
        )

    async def claim_task(self, task_id: str, client_id: str) -> bool:
        """Claim a task for a specific client. Returns False if already claimed."""
        success = await self.event_log.claim_task(task_id, client_id)
        if success:
            task = self._tasks.get(task_id)
            if task:
                task.claimed_by = client_id
                task.claimed_at = time.time()

            await self.event_bus.publish(
                SymphonyEvent(
                    task_id=task_id,
                    event_type="task_claimed",
                    data={"client_id": client_id},
                )
            )
        return success

    async def release_task(self, task_id: str) -> None:
        """Release a claimed task."""
        await self.event_log.release_task(task_id)
        task = self._tasks.get(task_id)
        if task:
            task.claimed_by = None
            task.claimed_at = None

        await self.event_bus.publish(
            SymphonyEvent(
                task_id=task_id,
                event_type="task_released",
            )
        )

    async def delete_task(self, task_id: str) -> None:
        """Delete a task and its events."""
        self._tasks.pop(task_id, None)
        self.event_bus.remove_task_queue(task_id)
        await self.event_log.delete_task(task_id)

    async def get_task(self, task_id: str) -> Optional[Task]:
        """Get task by ID. Falls back to DB if not in memory."""
        if task_id in self._tasks:
            return self._tasks[task_id]

        db_task = await self.event_log.get_task(task_id)
        if db_task:
            task = Task(
                task_id=db_task["task_id"],
                sop_name=db_task["sop_name"],
                sop_version=db_task["sop_version"],
                status=TaskStatus(db_task["status"]),
                claimed_by=db_task.get("claimed_by"),
                claimed_at=db_task.get("claimed_at"),
                created_at=db_task["created_at"],
                updated_at=db_task["updated_at"],
            )
            self._tasks[task_id] = task
            return task

        return None

    async def list_tasks(self, status: str | None = None) -> list[Task]:
        """List all tasks, optionally filtered by status."""
        db_tasks = await self.event_log.list_tasks(status=status)
        result = []
        for t in db_tasks:
            task = self._tasks.get(t["task_id"])
            if not task:
                task = Task(
                    task_id=t["task_id"],
                    sop_name=t["sop_name"],
                    sop_version=t["sop_version"],
                    status=TaskStatus(t["status"]),
                    claimed_by=t.get("claimed_by"),
                    claimed_at=t.get("claimed_at"),
                    created_at=t["created_at"],
                    updated_at=t["updated_at"],
                )
                self._tasks[t["task_id"]] = task
            result.append(task)
        return result

    async def _run_task(
        self,
        task_id: str,
        sop: SOPDefinition,
        cancel_event: asyncio.Event,
        pause_event: asyncio.Event,
        initial_input: dict | None = None,
    ) -> None:
        """Internal: run the SOP executor for a task.

        The executor is the single authority for terminal state: it updates the
        DB status AND publishes exactly one of task_completed / task_failed.
        Here we only mirror that terminal status onto the in-memory Task object
        (we must NOT re-publish, or the UI would receive contradictory events).
        """
        try:
            node_results = await self._get_executor().execute(
                task_id, sop, cancel_event, pause_event, initial_input
            )

            task = self._tasks.get(task_id)
            if task and task.status not in (
                TaskStatus.CANCELLED, TaskStatus.FAILED, TaskStatus.COMPLETED
            ):
                all_ok = bool(node_results) and all(
                    (r.get("status").value if hasattr(r.get("status"), "value") else r.get("status"))
                    == "completed"
                    for r in node_results.values()
                )
                task.status = TaskStatus.COMPLETED if all_ok else TaskStatus.FAILED

        except asyncio.CancelledError:
            pass
        except Exception as e:
            task = self._tasks.get(task_id)
            if task:
                task.status = TaskStatus.FAILED
            # The event_log / bus may already be shutting down (e.g. process
            # teardown), in which case these writes raise on a closed DB. That
            # is not a task failure, so swallow it rather than surface a scary
            # traceback.
            try:
                if task:
                    await self.event_log.update_task_status(task_id, TaskStatus.FAILED)
                await self.event_bus.publish(
                    SymphonyEvent(
                        task_id=task_id,
                        event_type="task_failed",
                        data={"error": str(e)},
                    )
                )
            except Exception:
                logger.debug(
                    "Could not persist task_failed for %s (event log closed?)",
                    task_id,
                )
        finally:
            # Recycle this task's dedicated pi subprocess once the SOP run has
            # settled (completed / failed / cancelled). Follow-up turns after
            # this point fall back to the shared control bridge via pool.get().
            if self.pi_pool is not None:
                try:
                    await self.pi_pool.release(task_id)
                except Exception as e:
                    logger.debug("pi bridge release for %s failed: %s", task_id, e)
