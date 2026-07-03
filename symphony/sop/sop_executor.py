"""SOP executor — runs SOP nodes as a state machine with retry and human intervention.

Inspired by pi's agent-loop.ts:
- Event-driven execution
- Parallel node execution
- Node lifecycle events published to EventBus
"""

from __future__ import annotations

import asyncio
import json
import logging
from enum import Enum

from symphony.core.event_bus import EventBus, SymphonyEvent
from symphony.core.event_log import EventLog
from symphony.core.pi_bridge import PiBridge, _TurnAccumulator
from symphony.sop.human_loop import HumanInterventionManager
from symphony.sop.retry import RetryHandler
from symphony.sop.sop_definition import NodeDefinition, SOPDefinition
from symphony.sop import schema_validator

logger = logging.getLogger(__name__)


class NodeStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    WAITING_HUMAN = "waiting_human"
    RETRYING = "retrying"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


class NodeInterrupted(Exception):
    """Raised inside a node's execution when a human requests interrupt & rerun.

    This is NOT a failure: the retry loop catches it and re-runs the SAME node
    (without consuming a retry attempt), appending the operator's extra
    instruction. Downstream nodes are re-run automatically (auto-cascade).
    """

    def __init__(self, node_id: str):
        self.node_id = node_id
        super().__init__(f"Node '{node_id}' interrupted for rerun")


class SOPExecutor:
    """Executes a SOP definition as a state machine.

    Flow:
    1. Topological sort nodes into execution levels
    2. Execute each level in parallel (nodes within a level run concurrently)
    3. For each node: invoke pi skill -> validate output -> human check if needed
    4. Retry on failure according to retry policy
    5. Publish all lifecycle events to EventBus
    """

    def __init__(
        self,
        pi_bridge: PiBridge,
        event_log: EventLog,
        event_bus: EventBus,
        llm_provider=None,
        human_manager: "HumanInterventionManager | None" = None,
        bridge_pool=None,
    ):
        self.pi_bridge = pi_bridge
        # When a bridge pool is supplied, each task runs on its own dedicated pi
        # subprocess (looked up per task_id). ``self.pi_bridge`` remains the
        # shared control bridge for compatibility and as the pool's fallback.
        self.bridge_pool = bridge_pool
        self.event_log = event_log
        self.event_bus = event_bus
        self.llm_provider = llm_provider  # Direct LLM provider (Mira/OpenAI)
        self.retry_handler = RetryHandler()
        # Reuse the shared manager if provided (so approvals from Web/TUI resolve
        # the same future). Only fall back to a private one for standalone use.
        self.human_manager = human_manager or HumanInterventionManager(event_bus)
        # ── Node-level interrupt / redirect state (item 7) ──
        # Per (task_id, node_id) control: an interrupt Event that, when set,
        # asks a running pi turn to abort so the node can be re-run; and an
        # extra instruction to append on the re-run. A monotonically increasing
        # generation per node lets us discard stale late events from an aborted
        # run.
        self._interrupts: dict[tuple[str, str], asyncio.Event] = {}
        self._extra_instructions: dict[tuple[str, str], str] = {}
        self._generation: dict[tuple[str, str], int] = {}
        # Last full assistant snapshot forwarded per running node. pi emits many
        # message_update events that repeat the same full text (and reasoning-only
        # updates with empty visible text). Keep the persisted/UI stream compact
        # and monotonic so renderers do not append duplicate snapshots.
        self._last_stream_text: dict[tuple[str, str], str] = {}
        self._last_prompt_context: dict[tuple[str, str], dict] = {}
        # task_id -> {node_id -> result}. The live, authoritative node result
        # table, used by redirect/manual-complete/follow-up. Created here so it
        # exists even before ``execute`` runs (manual completion may precede it).
        self._task_node_results: dict[str, dict[str, dict]] = {}

    # ── Node-level interrupt / redirect (item 7) ───────────────

    def request_node_interrupt(
        self, task_id: str, node_id: str, extra_instruction: str = ""
    ) -> None:
        """Signal a running node to abort so it can be re-run.

        Called by ``TaskManager.redirect_node``. Sets the node's interrupt event
        (which the pi execution races against) and records the extra instruction
        to append when the node is re-run. Bumps the node generation so any late
        events from the aborted turn are ignored.
        """
        key = (task_id, node_id)
        self._extra_instructions[key] = extra_instruction or ""
        self._generation[key] = self._generation.get(key, 0) + 1
        ev = self._interrupts.get(key)
        if ev is None:
            ev = asyncio.Event()
            self._interrupts[key] = ev
        ev.set()

    def _current_generation(self, task_id: str, node_id: str) -> int:
        return self._generation.get((task_id, node_id), 0)

    def _bridge_for(self, task_id: str) -> "PiBridge":
        """Return the pi bridge that should execute ``task_id``.

        With a pool, this is the task's dedicated subprocess (isolated event
        stream + abort); without one, it is the shared bridge.
        """
        if self.bridge_pool is not None:
            return self.bridge_pool.get(task_id)
        return self.pi_bridge

    @staticmethod
    async def _cancellable_sleep(delay: float, cancel_event: asyncio.Event) -> None:
        """Sleep for ``delay`` seconds but wake early if ``cancel_event`` fires.

        Retry backoff must not keep a cancelled task alive: without this, a task
        that has been cancelled (e.g. at shutdown) would still sleep through its
        full exponential backoff before the next top-of-loop cancel check. That
        stranded ``asyncio.sleep`` is what kept autostarted background tasks —
        and the interpreter — alive at teardown. Racing the sleep against the
        cancel event lets the task exit promptly.
        """
        try:
            await asyncio.wait_for(cancel_event.wait(), timeout=delay)
        except asyncio.TimeoutError:
            pass

    @staticmethod
    def _downstream_closure(sop: SOPDefinition, node_id: str) -> list[str]:
        """Return node_id plus ALL nodes transitively depending on it.

        Used to auto-cascade a rerun: re-running B invalidates and re-runs every
        node downstream of B (C, D, ...).
        """
        closure: list[str] = []
        seen: set[str] = set()
        stack = [node_id]
        while stack:
            cur = stack.pop()
            if cur in seen:
                continue
            seen.add(cur)
            closure.append(cur)
            for dep in sop.get_dependents(cur):
                if dep not in seen:
                    stack.append(dep)
        return closure

    async def redirect_node(
        self,
        task_id: str,
        sop: SOPDefinition,
        node_id: str,
        extra_instruction: str,
        cancel_event: asyncio.Event,
        pause_event: asyncio.Event,
    ) -> None:
        """Interrupt & rerun a node, auto-cascading to its downstream closure.

        Two cases:
          1. The node is still running -> signal an interrupt so its in-flight
             pi turn aborts and the node re-runs in place with the new
             instruction. The executor's ongoing level loop then feeds the fresh
             result to downstream nodes.
          2. The node already completed -> the level loop has moved past it, so
             we re-drive the downstream closure ourselves: reset those results
             and re-run the affected nodes in topological order.
        """
        node_results = self._task_node_results.get(task_id)
        # Record the instruction / bump generation regardless of case.
        self.request_node_interrupt(task_id, node_id, extra_instruction)

        if node_results is None:
            return

        current = node_results.get(node_id, {})

        if current.get("status") != NodeStatus.COMPLETED:
            # Case 1: running (or pending) — the interrupt event will trigger an
            # in-place rerun; nothing more to do here.
            return

        # Case 2: already completed — re-drive the closure.
        closure = set(self._downstream_closure(sop, node_id))
        for nid in closure:
            node_results.pop(nid, None)

        await self.event_bus.publish(
            SymphonyEvent(
                task_id=task_id,
                node_id=node_id,
                event_type="node_redirected",
                data={"closure": sorted(closure), "extra_instruction": extra_instruction},
            )
        )

        # Re-run affected nodes in topological order.
        for level_nodes in sop.topological_order():
            affected = [n for n in level_nodes if n.id in closure]
            if not affected:
                continue
            if cancel_event.is_set():
                break
            await pause_event.wait()
            tasks = [
                asyncio.create_task(
                    self._execute_node(
                        task_id, n, sop, node_results, cancel_event, pause_event
                    )
                )
                for n in affected
            ]
            await asyncio.gather(*tasks, return_exceptions=True)

        # Recompute + publish terminal task status after the cascade.
        all_completed = all(
            r.get("status") == NodeStatus.COMPLETED for r in node_results.values()
        )
        final_status = "completed" if all_completed else "failed"
        await self.event_log.update_task_status(task_id, final_status)
        await self.event_bus.publish(
            SymphonyEvent(
                task_id=task_id,
                event_type="task_completed" if all_completed else "task_failed",
                data={
                    "node_results": {
                        k: v["status"].value if isinstance(v.get("status"), NodeStatus) else v.get("status")
                        for k, v in node_results.items()
                    },
                    "reran_from": node_id,
                },
            )
        )

    async def complete_node_manually(
        self,
        task_id: str,
        sop: SOPDefinition,
        node_id: str,
        artifact: dict,
        cancel_event: asyncio.Event,
        pause_event: asyncio.Event,
        rerun_downstream: bool = True,
    ) -> None:
        """Manually mark a node COMPLETED with an operator-supplied artifact.

        Used for human-in-the-loop steps: an operator fills in the node's output
        artifact (e.g. a Feishu doc link) and marks it done. The artifact is
        format-validated against the node's ``output_artifact_type``; on success
        it becomes the node result and the downstream closure is re-run so the
        flow continues automatically.
        """
        from symphony.sop.artifact import ArtifactType, validate_artifact_format

        node = sop.get_node(node_id)
        if node is None:
            raise ValueError(f"node '{node_id}' not found")

        # 1) Enforce artifact type + format.
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

        # 2) Interrupt any in-flight turn (bump generation) BEFORE writing the
        #    result, so a late-arriving real pi turn cannot overwrite it.
        self.request_node_interrupt(task_id, node_id, extra_instruction="")

        node_results = self._task_node_results.setdefault(task_id, {})

        # 3) Write the manual result.
        art = {"type": atype.value, "value": artifact.get("value", ""), "label": artifact.get("label")}
        node_results[node_id] = {
            "status": NodeStatus.COMPLETED,
            "result": {
                "status": "completed",
                "output": art["value"],
                "artifact": art,
                "manual": True,
                "provider": "manual",
            },
        }

        # 4) Publish node_completed (with artifact + manual marker).
        await self.event_bus.publish(
            SymphonyEvent(
                task_id=task_id,
                node_id=node_id,
                event_type="node_completed",
                data={"node_name": node.name, "artifact": art, "manual": True},
            )
        )

        if not rerun_downstream:
            return

        # 5) Re-run the downstream closure (excluding this node itself).
        closure = set(self._downstream_closure(sop, node_id)) - {node_id}
        for nid in closure:
            node_results.pop(nid, None)
        for level_nodes in sop.topological_order():
            affected = [n for n in level_nodes if n.id in closure]
            if not affected:
                continue
            if cancel_event.is_set():
                break
            await pause_event.wait()
            tasks = [
                asyncio.create_task(
                    self._execute_node(
                        task_id, n, sop, node_results, cancel_event, pause_event
                    )
                )
                for n in affected
            ]
            await asyncio.gather(*tasks, return_exceptions=True)

        # Recompute + publish terminal task status.
        all_completed = all(
            r.get("status") == NodeStatus.COMPLETED for r in node_results.values()
        )
        final_status = "completed" if all_completed else "failed"
        await self.event_log.update_task_status(task_id, final_status)
        await self.event_bus.publish(
            SymphonyEvent(
                task_id=task_id,
                event_type="task_completed" if all_completed else "task_failed",
                data={"reran_from": node_id, "manual": True},
            )
        )

    async def execute(
        self,
        task_id: str,
        sop: SOPDefinition,
        cancel_event: asyncio.Event,
        pause_event: asyncio.Event,
        initial_input: dict | None = None,
    ) -> dict[str, dict]:
        """Execute all nodes of a SOP for a given task.

        Args:
            initial_input: User-provided inputs for the task. These are fed to
                ROOT nodes (nodes with no ``depends_on``) so a task started from
                the Web/TUI with a prompt or structured inputs actually receives
                them (previously root nodes always got an empty input — the
                "my question never reaches the node" defect).

        Returns:
            Dict mapping node_id -> {"status": NodeStatus, "result": ...}
        """
        self._initial_input = initial_input or {}
        node_results: dict[str, dict] = {}
        # Expose the live results so node-level redirect (item 7) can inspect
        # whether a node already finished and invalidate its downstream closure.
        if not hasattr(self, "_task_node_results"):
            self._task_node_results: dict[str, dict[str, dict]] = {}
        self._task_node_results[task_id] = node_results
        levels = sop.topological_order()

        for level_idx, level_nodes in enumerate(levels):
            if cancel_event.is_set():
                for node in level_nodes:
                    if node.id not in node_results:
                        node_results[node.id] = {"status": NodeStatus.SKIPPED, "reason": "cancelled"}
                break

            # Wait if paused
            await pause_event.wait()

            # Execute all nodes in this level concurrently
            tasks = []
            for node in level_nodes:
                if cancel_event.is_set():
                    if node.id not in node_results:
                        node_results[node.id] = {"status": NodeStatus.SKIPPED, "reason": "cancelled"}
                    continue
                tasks.append(
                    asyncio.create_task(
                        self._execute_node(
                            task_id, node, sop, node_results, cancel_event, pause_event
                        )
                    )
                )

            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

        if cancel_event.is_set():
            return node_results

        # Final status
        all_completed = all(
            r.get("status") == NodeStatus.COMPLETED for r in node_results.values()
        )
        final_status = "completed" if all_completed else "failed"

        await self.event_log.update_task_status(task_id, final_status)
        await self.event_bus.publish(
            SymphonyEvent(
                task_id=task_id,
                event_type="task_completed" if all_completed else "task_failed",
                data={
                    "node_results": {
                        k: v["status"].value if isinstance(v.get("status"), NodeStatus) else v.get("status")
                        for k, v in node_results.items()
                    }
                },
            )
        )

        return node_results

    async def _execute_node(
        self,
        task_id: str,
        node: NodeDefinition,
        sop: SOPDefinition,
        node_results: dict[str, dict],
        cancel_event: asyncio.Event,
        pause_event: asyncio.Event,
    ) -> None:
        """Execute a single node with retry logic."""
        # Skip if any dependency did not complete successfully — running a node
        # whose inputs never materialized produces garbage and hides the real
        # (upstream) failure.
        unmet = [
            dep for dep in node.depends_on
            if node_results.get(dep, {}).get("status") != NodeStatus.COMPLETED
        ]
        if unmet:
            node_results[node.id] = {
                "status": NodeStatus.SKIPPED,
                "reason": f"unmet dependencies: {', '.join(unmet)}",
            }
            await self.event_bus.publish(
                SymphonyEvent(
                    task_id=task_id,
                    node_id=node.id,
                    event_type="node_skipped",
                    data={"reason": "unmet_dependencies", "unmet": unmet,
                          "node_name": node.name},
                )
            )
            return

        attempt = 0
        while attempt < node.retry.max_attempts:
            attempt += 1
            if cancel_event.is_set():
                node_results[node.id] = {"status": NodeStatus.SKIPPED, "reason": "cancelled"}
                return

            await pause_event.wait()

            status = NodeStatus.RUNNING if attempt == 1 else NodeStatus.RETRYING
            await self.event_bus.publish(
                SymphonyEvent(
                    task_id=task_id,
                    node_id=node.id,
                    event_type="node_started" if attempt == 1 else "node_retry",
                    data={
                        "attempt": attempt,
                        "max_attempts": node.retry.max_attempts,
                        "node_name": node.name,
                        "skill": node.skill,
                    },
                )
            )

            try:
                # Prepare input from dependency outputs
                node_input = self._prepare_input(sop, node, node_results)

                # Validate the aggregated upstream outputs against this node's
                # input contract BEFORE running it. A contract violation raises
                # SchemaValidationError, which the retry handler below catches —
                # so a bad upstream result triggers a retry / eventual failure
                # instead of feeding garbage into the node.
                self._validate_input(task_id, node, node_input)

                # Choose the execution engine. Default is pi (full agent loop:
                # loads the skill, loops the LLM, runs pi's tools) — "the
                # agent's capability comes from pi". 'llm' is a single-shot
                # fallback; 'auto' prefers pi when the bridge is running.
                result = await self._dispatch_execution(
                    task_id, node, sop, node_input, cancel_event
                )

                # Structured "needs user input" contract: if the model declared
                # it must obtain information from the user before continuing, we
                # pause the node (waiting_human), surface a structured question
                # card, then re-run the SAME node with the user's answer appended
                # (via _extra_instructions) — without consuming a retry attempt.
                from symphony.sop.artifact import extract_needs_user_input

                question = extract_needs_user_input(result)
                if question:
                    node_results[node.id] = {
                        "status": NodeStatus.WAITING_HUMAN,
                        "questions": question["questions"],
                    }
                    answer = await self.human_manager.request_answer(
                        task_id=task_id,
                        node_id=node.id,
                        node_name=node.name,
                        questions=question["questions"],
                        reason=question.get("reason", ""),
                        timeout=max(node.timeout, 3600),
                    )
                    if answer:
                        combined = "用户已回答你之前请求的信息，请据此继续完成本节点（不要再重复提问）：\n" + answer
                        self.request_node_interrupt(task_id, node.id, combined)
                    attempt -= 1  # a clarification round does not consume a retry
                    continue

                # Validate the node's OUTPUT against its output contract. On
                # failure this raises SchemaValidationError -> retry. This is
                # what makes A's result trustworthy before it flows into B.
                result = self._validate_output(task_id, node, result)

                # Human intervention check
                if node.human_intervention:
                    approved, feedback = await self.human_manager.request_approval(
                        task_id=task_id,
                        node_id=node.id,
                        node_name=node.name,
                        result=result,
                        timeout=node.timeout,
                    )
                    if not approved:
                        logger.info(
                            f"Node {node.id} not approved by human, "
                            f"retrying ({attempt}/{node.retry.max_attempts})"
                        )
                        # Feed the reviewer's feedback back into the re-run so the
                        # node actually revises per the reject reason. Without this
                        # the retry would send an identical prompt (feedback was
                        # previously dead data). Uses the same _extra_instructions
                        # channel that _execute_via_pi appends to the prompt.
                        if feedback:
                            self.request_node_interrupt(task_id, node.id, feedback)
                        node_results[node.id] = {
                            "status": NodeStatus.RETRYING,
                            "feedback": feedback,
                            "attempt": attempt,
                        }
                        if attempt < node.retry.max_attempts:
                            delay = self.retry_handler.calc_delay(node.retry, attempt)
                            await self._cancellable_sleep(delay, cancel_event)
                        continue

                # Success
                node_results[node.id] = {
                    "status": NodeStatus.COMPLETED,
                    "result": result,
                }
                await self.event_bus.publish(
                    SymphonyEvent(
                        task_id=task_id,
                        node_id=node.id,
                        event_type="node_completed",
                        data={
                            "attempt": attempt,
                            "node_name": node.name,
                            "artifact": result.get("artifact") if isinstance(result, dict) else None,
                        },
                    )
                )
                return

            except NodeInterrupted:
                # Human requested "打断并重来": re-run the SAME node WITHOUT
                # consuming a retry attempt, appending the operator's extra
                # instruction (already stored for the next _execute_via_pi run).
                logger.info(f"Node {node.id} interrupted -> re-running with new instruction")
                await self.event_bus.publish(
                    SymphonyEvent(
                        task_id=task_id,
                        node_id=node.id,
                        event_type="node_interrupted",
                        data={
                            "node_name": node.name,
                            "extra_instruction": self._extra_instructions.get(
                                (task_id, node.id), ""
                            ),
                        },
                    )
                )
                attempt -= 1  # do not count the interrupted run against retries
                continue

            except asyncio.TimeoutError:
                logger.warning(
                    f"Node {node.id} timed out (attempt {attempt}/{node.retry.max_attempts})"
                )
                if attempt < node.retry.max_attempts:
                    delay = self.retry_handler.calc_delay(node.retry, attempt)
                    await self._cancellable_sleep(delay, cancel_event)
                else:
                    node_results[node.id] = {"status": NodeStatus.FAILED, "error": "timeout"}

            except Exception as e:
                logger.error(f"Node {node.id} failed: {e}")
                if attempt < node.retry.max_attempts:
                    delay = self.retry_handler.calc_delay(node.retry, attempt)
                    await self._cancellable_sleep(delay, cancel_event)
                else:
                    node_results[node.id] = {"status": NodeStatus.FAILED, "error": str(e)}

        # All retries exhausted
        await self.event_bus.publish(
            SymphonyEvent(
                task_id=task_id,
                node_id=node.id,
                event_type="node_failed",
                data=node_results.get(node.id, {}),
            )
        )

    async def _dispatch_execution(
        self,
        task_id: str,
        node: NodeDefinition,
        sop: SOPDefinition,
        node_input: dict,
        cancel_event: asyncio.Event,
    ) -> dict:
        """Route a node to its execution engine based on ``node.executor``.

        pi is the primary engine (full agent loop). 'llm' is a single-shot
        fallback. 'auto' prefers pi when the bridge is running, else llm.
        """
        from symphony.sop.sop_definition import NodeExecutor

        bridge = self._bridge_for(task_id)
        pi_available = bool(bridge and getattr(bridge, "_started", False))
        llm_available = bool(self.llm_provider and self.llm_provider.is_available)
        llm_type = getattr(getattr(self.llm_provider, "config", None), "type", "")
        direct_llm_preferred = llm_type in {"mira", "custom_http", "http", "nonstandard"}

        engine = node.executor

        # For Mira/custom direct providers, the user's selected model lives on
        # Symphony's side, not inside pi. Therefore these providers should be
        # executed via the direct LLM path even when the pi bridge is running;
        # otherwise plain tasks silently fall back to pi's unrelated saved model.
        if direct_llm_preferred:
            if llm_available:
                return await self._execute_via_llm(task_id, node, sop, node_input)
            if pi_available:
                logger.warning(
                    f"Node '{node.id}' selected provider type '{llm_type}', but the "
                    "direct LLM provider is unavailable; falling back to pi."
                )
                return await self._execute_via_pi(task_id, node, sop, node_input, cancel_event)
            raise RuntimeError(
                f"Node '{node.id}' selected provider type '{llm_type}', but the "
                f"direct LLM provider is not configured/available."
            )

        if engine == NodeExecutor.LLM:
            if not llm_available:
                raise RuntimeError(
                    f"Node '{node.id}' requires the direct LLM provider but it is "
                    f"not configured/available."
                )
            return await self._execute_via_llm(task_id, node, sop, node_input)

        if engine == NodeExecutor.AUTO:
            if pi_available:
                return await self._execute_via_pi(task_id, node, sop, node_input, cancel_event)
            if llm_available:
                logger.info(
                    f"Node '{node.id}' executor=auto: pi bridge not running, "
                    f"falling back to direct LLM."
                )
                return await self._execute_via_llm(task_id, node, sop, node_input)
            raise RuntimeError(
                f"Node '{node.id}' executor=auto but neither pi bridge nor LLM "
                f"provider is available."
            )

        # Default / explicit pi: full agent capability comes from pi.
        if pi_available:
            return await self._execute_via_pi(task_id, node, sop, node_input, cancel_event)
        if llm_available:
            logger.warning(
                f"Node '{node.id}' wants pi (full agent loop) but the pi bridge "
                f"is not running; degrading to single-shot LLM. Skill '{node.skill}' "
                f"and pi tools will NOT be available. Start pi (build dist/cli.js and "
                f"pass --pi-binary) for full capability."
            )
            return await self._execute_via_llm(task_id, node, sop, node_input)
        raise RuntimeError(
            f"Node '{node.id}' requires pi but the pi bridge is not running and no "
            f"LLM fallback is configured. Build pi (npm run build) and start it with "
            f"--pi-binary <path to dist/cli.js>."
        )

    @staticmethod
    def _ancestor_ids_in_order(sop: SOPDefinition, node_id: str) -> list[str]:
        """Return all transitive dependency ancestors of ``node_id``.

        Ordered root-first via the SOP's topological order, de-duplicated. Used
        to accumulate the full upstream context (not just direct parents) for a
        downstream node.
        """
        ancestors: set[str] = set()
        stack = list(sop.get_dependencies(node_id))
        while stack:
            cur = stack.pop()
            if cur in ancestors:
                continue
            ancestors.add(cur)
            stack.extend(sop.get_dependencies(cur))
        ordered: list[str] = []
        for level in sop.topological_order():
            for n in level:
                if n.id in ancestors:
                    ordered.append(n.id)
        return ordered

    def _prepare_input(
        self, sop: SOPDefinition, node: NodeDefinition, node_results: dict[str, dict]
    ) -> dict:
        """Prepare input for a node by collecting outputs from its dependencies.

        Root nodes (no dependencies) receive the task's ``initial_input`` — the
        prompt / structured inputs the user supplied when starting the task. This
        is what lets a Web/TUI-created task actually deliver the user's question
        to the first node.

        In addition to the direct-parent results (kept under ``dep_id`` keys so
        ``input_schema`` validation is unchanged), the FULL upstream ancestor
        context is accumulated under the reserved ``_ancestor_context`` key so a
        downstream node can see every completed upstream node's artifact/output.
        """
        node_input: dict = {}
        if not node.depends_on:
            # Root node — seed with the user-supplied task input.
            initial = getattr(self, "_initial_input", None) or {}
            if initial:
                node_input = dict(initial)
        for dep_id in node.depends_on:
            dep_result = node_results.get(dep_id, {})
            if dep_result.get("status") == NodeStatus.COMPLETED:
                node_input[dep_id] = dep_result.get("result", {})
        # Accumulate all completed ancestors' artifacts/outputs (transitive).
        context: list[dict] = []
        for anc_id in self._ancestor_ids_in_order(sop, node.id):
            anc = node_results.get(anc_id, {})
            if anc.get("status") != NodeStatus.COMPLETED:
                continue
            res = anc.get("result", {}) or {}
            context.append({
                "node_id": anc_id,
                "artifact": res.get("artifact"),
                "output": res.get("output"),
            })
        if context:
            node_input["_ancestor_context"] = context
        return node_input

    # ── Schema validation ──────────────────────────────────────

    def _validate_input(
        self, task_id: str, node: NodeDefinition, node_input: dict
    ) -> None:
        """Validate aggregated upstream outputs against node.input_schema.

        Raises SchemaValidationError on failure (caught by the retry loop).
        """
        if not node.input_schema:
            return
        vr = schema_validator.validate_input(node.id, node_input, node.input_schema)
        if not vr.ok:
            self._publish_validation_failure(task_id, node, "input", vr.errors)
            raise schema_validator.SchemaValidationError("input", node.id, vr.errors)

    def _validate_output(
        self, task_id: str, node: NodeDefinition, result: dict
    ) -> dict:
        """Validate a node's output against node.output_schema and artifact type.

        On success, attaches the recovered structured payload as
        ``result["validated"]`` and the extracted artifact as
        ``result["artifact"]`` so downstream nodes can consume them directly.
        Raises SchemaValidationError on failure (caught by the retry loop).
        """
        if node.output_schema:
            vr = schema_validator.validate_output(node.id, result, node.output_schema)
            if not vr.ok:
                self._publish_validation_failure(task_id, node, "output", vr.errors)
                raise schema_validator.SchemaValidationError("output", node.id, vr.errors)
            if isinstance(result, dict):
                result = {**result, "validated": vr.payload}

        # Artifact validation. For a non-TEXT output type the node MUST produce a
        # well-formed artifact (e.g. a valid Feishu URL); failure triggers retry.
        # TEXT is best-effort: attach when recoverable, never fail.
        from symphony.sop.artifact import (
            ArtifactType,
            extract_artifact,
            validate_artifact_format,
        )

        out_type = getattr(node, "output_artifact_type", ArtifactType.TEXT)
        if out_type and out_type != ArtifactType.TEXT:
            art = extract_artifact(result, out_type)
            if art is None:
                self._publish_validation_failure(
                    task_id, node, "artifact", ["未能从输出中解析出结构化产物"]
                )
                raise schema_validator.SchemaValidationError(
                    "artifact", node.id, ["missing artifact"]
                )
            ok, err = validate_artifact_format(out_type, art.value)
            if not ok:
                self._publish_validation_failure(task_id, node, "artifact", [err])
                raise schema_validator.SchemaValidationError("artifact", node.id, [err])
            if isinstance(result, dict):
                result = {**result, "artifact": art.model_dump(mode="json")}
        elif isinstance(result, dict):
            art = extract_artifact(result, ArtifactType.TEXT)
            if art:
                result = {**result, "artifact": art.model_dump(mode="json")}
        return result

    def _publish_validation_failure(
        self, task_id: str, node: NodeDefinition, where: str, errors: list[str]
    ) -> None:
        # Fire-and-forget publish; we're inside a sync helper.
        try:
            loop = asyncio.get_event_loop()
            loop.create_task(
                self.event_bus.publish(
                    SymphonyEvent(
                        task_id=task_id,
                        node_id=node.id,
                        event_type="node_validation_failed",
                        data={"where": where, "errors": errors, "node_name": node.name},
                    )
                )
            )
        except Exception:
            pass


    async def _execute_via_llm(
        self, task_id: str, node: NodeDefinition, sop: SOPDefinition, node_input: dict
    ) -> dict:
        """Execute a node using the direct LLM provider (Mira/OpenAI)."""
        system_prompt = (
            f"You are executing SOP node '{node.name}'. "
            "Follow the SOP and node input/output requirements exactly."
        )
        user_prompt = self._build_node_prompt(sop, node, node_input)

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]

        # Stream LLM output to EventBus
        full_text = ""
        async for chunk in self.llm_provider.chat(messages):
            if chunk["type"] == "text":
                content = chunk.get("content", "")
                if not isinstance(content, str):
                    content = json.dumps(content, ensure_ascii=False) if isinstance(content, (dict, list)) else str(content)
                full_text += content
                await self.event_bus.publish(SymphonyEvent(
                    task_id=task_id,
                    node_id=node.id,
                    event_type="agent_message_delta",
                    data={"text": content},
                ))
            elif chunk["type"] in ("tool_call", "tool_call_start"):
                await self.event_bus.publish(SymphonyEvent(
                    task_id=task_id,
                    node_id=node.id,
                    event_type="tool_call_start",
                    data={"tool_name": chunk.get("name", ""), "arguments": chunk.get("arguments", {})},
                ))
            elif chunk["type"] == "error":
                raise Exception(chunk["message"])
            # 'done' and 'reasoning' are ignored for result collection

        return {
            "status": "completed",
            "output": full_text,
            "provider": self.llm_provider.config.model,
        }

    async def _execute_via_pi(
        self,
        task_id: str,
        node: NodeDefinition,
        sop: SOPDefinition,
        node_input: dict,
        cancel_event: asyncio.Event,
    ) -> dict:
        """Execute a node by driving pi to completion via its skill.

        Unlike the old fire-and-forget path (which returned a command_id and
        pretended the node succeeded), this awaits pi's turn end and returns
        pi's real output. While pi runs, its streamed events are translated
        into SymphonyEvents and published to the EventBus so the TUI and Web
        UI show the live execution and tool calls in real time.

        The turn is raced against the node's interrupt event (item 7). If a
        human requests "打断并重来" mid-run, pi is aborted and a
        :class:`NodeInterrupted` is raised so the retry loop re-runs the node
        with the extra instruction appended.
        """
        loop = asyncio.get_event_loop()
        key = (task_id, node.id)
        my_generation = self._current_generation(task_id, node.id)
        self._last_stream_text.pop(key, None)

        # Use THIS task's dedicated pi bridge (isolated subprocess) so a
        # concurrent task's turn / abort never interferes with this one.
        bridge = self._bridge_for(task_id)

        # Build a task description that includes the SOP/node contracts and the
        # actual inputs, so pi has the full prompt contract to execute.
        task_description = self._build_node_prompt(sop, node, node_input)
        # Append any human redirect instruction for this node (item 7).
        extra = self._extra_instructions.get(key, "")
        if extra:
            task_description += (
                "\n\nAdditional instruction from the operator (revise accordingly):\n"
                + extra
            )

        context_files = []
        if bridge and getattr(bridge, "config", None):
            try:
                context_files = bridge.config.context_file_infos()
            except Exception as e:
                logger.debug(f"Could not inspect pi context files: {e}")
        prompt_sha = self._sha256_text(task_description)
        prompt_context = {
            "engine": "pi",
            "pi_cwd": getattr(getattr(bridge, "config", None), "cwd", None),
            "context_files": context_files,
            "prompt_sha256": prompt_sha,
            "prompt_sha256_short": prompt_sha[:12],
            "prompt_preview": task_description[:1200],
            "prompt_bytes": len(task_description.encode("utf-8")),
        }
        self._last_prompt_context[key] = prompt_context
        await self.event_bus.publish(SymphonyEvent(
            task_id=task_id,
            node_id=node.id,
            event_type="node_prompt_prepared",
            data=prompt_context,
        ))

        def _forward(evt: dict) -> None:
            """Translate a raw pi AgentEvent into a SymphonyEvent (fire-and-forget)."""
            # Drop stale events from an aborted/superseded run.
            if self._current_generation(task_id, node.id) != my_generation:
                return
            sym = self._pi_event_to_symphony(task_id, node, evt)
            if sym is not None:
                # publish is async; schedule it without blocking pi's reader.
                loop.create_task(self.event_bus.publish(sym))

        # Ensure a fresh interrupt event for this run.
        interrupt_ev = asyncio.Event()
        self._interrupts[key] = interrupt_ev

        turn_task = asyncio.ensure_future(
            asyncio.wait_for(
                bridge.run_skill_to_completion(
                    skill_name=node.skill,
                    task_description=task_description,
                    on_event=_forward,
                    timeout=node.timeout,
                ),
                timeout=node.timeout,
            )
        )
        interrupt_task = asyncio.ensure_future(interrupt_ev.wait())
        cancel_task = asyncio.ensure_future(cancel_event.wait())

        done, pending = await asyncio.wait(
            {turn_task, interrupt_task, cancel_task},
            return_when=asyncio.FIRST_COMPLETED,
        )

        if cancel_task in done and turn_task not in done:
            turn_task.cancel()
            interrupt_task.cancel()
            try:
                await bridge.abort()
            except Exception as e:
                logger.debug(f"pi abort during cancel failed: {e}")
            try:
                await turn_task
            except (asyncio.CancelledError, Exception):
                pass
            raise asyncio.CancelledError()

        if interrupt_task in done and turn_task not in done:
            # Human asked to interrupt & redirect — abort pi and re-run.
            turn_task.cancel()
            cancel_task.cancel()
            try:
                await bridge.abort()
            except Exception as e:
                logger.debug(f"pi abort during interrupt failed: {e}")
            try:
                await turn_task
            except (asyncio.CancelledError, Exception):
                pass
            raise NodeInterrupted(node.id)

        # Turn finished first — clean up the interrupt waiter.
        interrupt_task.cancel()
        cancel_task.cancel()
        turn = turn_task.result()
        return turn.to_node_result(skill=node.skill)

    @staticmethod
    def _sha256_text(text: str) -> str:
        import hashlib
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    @staticmethod
    def _build_node_prompt(sop: SOPDefinition, node: NodeDefinition, node_input: dict) -> str:
        """Build the exact prompt sent to pi/LLM for one SOP node."""
        # Shallow-copy so popping the reserved context key does not mutate the
        # caller's dict (which input_schema validation already consumed).
        node_input = dict(node_input) if isinstance(node_input, dict) else node_input
        ancestor_ctx = (
            node_input.pop("_ancestor_context", None)
            if isinstance(node_input, dict) else None
        )
        sections = [
            "请执行下面的 SOP 节点。必须遵守输入要求与输出要求。",
            f"SOP 名称：{sop.name}",
        ]
        if sop.description:
            sections.append(f"SOP 描述：\n{sop.description}")
        if sop.input_requirements:
            sections.append(f"SOP 要求的输入：\n{sop.input_requirements}")
        if sop.output_requirements:
            sections.append(f"SOP 要求的输出：\n{sop.output_requirements}")

        sections.extend([
            f"节点 ID：{node.id}",
            f"节点名称：{node.name}",
        ])
        if node.description:
            sections.append(f"节点描述：\n{node.description}")
        if node.input_requirements:
            sections.append(f"节点要求的输入：\n{node.input_requirements}")
        if node.output_requirements:
            sections.append(f"节点要求的输出：\n{node.output_requirements}")

        # Accumulated upstream artifacts/outputs (full ancestor context).
        if ancestor_ctx:
            lines = []
            for c in ancestor_ctx:
                art = c.get("artifact") or {}
                lines.append(
                    f"- [{c.get('node_id')}] 产物类型={art.get('type', 'text')} "
                    f"值={art.get('value', '')}"
                    + (f" 说明={art.get('label')}" if art.get("label") else "")
                )
            sections.append("上游已完成节点的产物（累计上下文，可作为本节点输入）：\n" + "\n".join(lines))

        # This node's artifact type + natural-language conditions.
        artifact_lines = [f"本节点输出产物类型：{node.output_artifact_type.value}"]
        if node.input_conditions:
            artifact_lines.append(f"输入产物约束条件：\n{node.input_conditions}")
        if node.output_conditions:
            artifact_lines.append(f"输出产物约束条件：\n{node.output_conditions}")
        sections.append("\n".join(artifact_lines))

        sections.append(
            "实际输入：\n"
            + (json.dumps(node_input, ensure_ascii=False, indent=2) if node_input else "{}")
        )
        if node.input_schema:
            sections.append(
                "输入 JSON Schema：\n"
                + json.dumps(node.input_schema, ensure_ascii=False, indent=2)
            )
        if node.output_schema:
            sections.append(
                "输出 JSON Schema：\n"
                + json.dumps(node.output_schema, ensure_ascii=False, indent=2)
            )
        sections.append(
            "回答要求：\n"
            "1. 只输出该节点要求的结果，不要复述无关过程。\n"
            "2. 如果输出要求中限定了结构、章节、字段或格式，必须严格满足。\n"
            "3. 如果输入不足以完成任务，明确指出缺失内容和影响。\n"
            "4. 结构化产物：回答正文之后，必须以一个 ```json 代码块结尾，内容形如 "
            '{"artifact": {"type": "<产物类型>", "value": "<产物值>", "label": "<可选说明>"}}。\n'
            f"5. 其中 type 必须为 {node.output_artifact_type.value}。若为 feishu_doc/link，"
            "value 必须是完整可点击的 URL；若为 sql，value 是可执行 SQL 文本；"
            "若为 task_id，value 是发布任务 ID。\n"
            "6. 若上方有输出产物约束条件，请在生成前自查是否满足（如是否包含背景/SQL/DAG 等），"
            "不满足则先补全再输出。\n"
            "7. 若你必须先获得用户提供的关键信息才能继续（如目标库名、是否上线确认、缺失的参数），"
            "不要臆测：改为在回答最后输出一个 ```json 代码块，内容形如 "
            '{"needs_user_input": {"questions": [{"key": "字段名", "question": "问题文本", "type": "text"}], "reason": "为什么需要"}}。'
            "此时不要再输出 artifact。"
        )
        return "\n\n".join(sections)

    def _pi_event_to_symphony(
        self, task_id: str, node: NodeDefinition, evt: dict
    ) -> SymphonyEvent | None:
        """Map pi's AgentEvent vocabulary onto Symphony's event_type names.

        The TUI/Web already understand: agent_message_delta, tool_call_start,
        tool_call_update, tool_call_end. We translate pi events into those so pi
        execution renders identically to direct-LLM execution.
        """
        etype = evt.get("type")
        key = (task_id, node.id)

        if etype in ("message_update", "message_end"):
            message = evt.get("message")
            if isinstance(message, dict) and message.get("role") in (None, "assistant"):
                text = _TurnAccumulator._extract_text(message)
                if not text and isinstance(message.get("errorMessage"), str):
                    text = message["errorMessage"]
                if not text:
                    return None
                last = self._last_stream_text.get(key, "")
                if text == last:
                    return None
                self._last_stream_text[key] = text
                return SymphonyEvent(
                    task_id=task_id,
                    node_id=node.id,
                    event_type="agent_message_delta",
                    data={"text": text, "replace": True, "source": "pi"},
                )
            return None

        if etype == "tool_execution_start":
            return SymphonyEvent(
                task_id=task_id,
                node_id=node.id,
                event_type="tool_call_start",
                data={
                    "tool_name": evt.get("toolName", ""),
                    "arguments": evt.get("args", {}),
                    "tool_call_id": evt.get("toolCallId"),
                    "source": "pi",
                },
            )

        if etype == "tool_execution_update":
            return SymphonyEvent(
                task_id=task_id,
                node_id=node.id,
                event_type="tool_call_update",
                data={
                    "tool_name": evt.get("toolName", ""),
                    "partial_result": evt.get("partialResult"),
                    "tool_call_id": evt.get("toolCallId"),
                    "source": "pi",
                },
            )

        if etype == "tool_execution_end":
            return SymphonyEvent(
                task_id=task_id,
                node_id=node.id,
                event_type="tool_call_end",
                data={
                    "tool_name": evt.get("toolName", ""),
                    "result": evt.get("result"),
                    "is_error": bool(evt.get("isError")),
                    "tool_call_id": evt.get("toolCallId"),
                    "source": "pi",
                },
            )

        return None
