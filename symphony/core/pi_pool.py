"""Pi bridge process pool — one dedicated pi subprocess per concurrent task.

The single shared ``PiBridge`` cannot run two SOP tasks at once: pi's RPC
events are broadcast to all callbacks and a global ``abort`` would interrupt
every in-flight turn. To run multiple SOPs concurrently and independently we
give each task its own pi subprocess, so their event streams and aborts are
physically isolated.

A single "control" bridge is kept for interactive, non-task consumers
(skills listing, model queries, TUI chat, Q&A follow-up fallback) and is used
as a safe fallback whenever a task-scoped bridge is unavailable.
"""

from __future__ import annotations

import dataclasses
import logging

from symphony.core.pi_bridge import PiBridge, PiBridgeConfig

logger = logging.getLogger(__name__)


class PiBridgePool:
    """Allocates and recycles one dedicated ``PiBridge`` per task.

    Lifecycle:
      - ``acquire(task_id)``  — spawn + start a fresh pi subprocess for a task.
      - ``get(task_id)``      — return the task's bridge (or control fallback).
      - ``release(task_id)``  — stop and drop the task's bridge when it finishes.

    The ``control`` bridge is owned externally (created/started/stopped by the
    CLI) and is never stopped by the pool.
    """

    def __init__(self, base_config: PiBridgeConfig, control_bridge: PiBridge):
        self._base_config = base_config
        self._control = control_bridge
        self._bridges: dict[str, PiBridge] = {}

    @property
    def control(self) -> PiBridge:
        """The shared control bridge (skills/models/TUI/fallback)."""
        return self._control

    async def acquire(self, task_id: str) -> PiBridge:
        """Start a dedicated pi subprocess for ``task_id``.

        Returns the new bridge on success. If the subprocess fails to start,
        falls back to the shared control bridge so the task can still run
        (degraded to shared mode) rather than failing outright.
        """
        # If a bridge already exists for this task (e.g. re-start), reuse it.
        existing = self._bridges.get(task_id)
        if existing is not None:
            return existing

        cfg = dataclasses.replace(self._base_config)
        bridge = PiBridge(cfg)
        try:
            await bridge.start()
        except Exception as e:
            logger.warning(
                "Could not start dedicated pi bridge for task %s: %s. "
                "Falling back to the shared control bridge.",
                task_id,
                e,
            )
            return self._control

        self._bridges[task_id] = bridge
        return bridge

    def get(self, task_id: str) -> PiBridge:
        """Return the task's dedicated bridge, or the control bridge fallback."""
        return self._bridges.get(task_id) or self._control

    async def release(self, task_id: str) -> None:
        """Stop and drop the dedicated bridge for a finished task."""
        bridge = self._bridges.pop(task_id, None)
        if bridge is not None and bridge is not self._control:
            try:
                await bridge.stop()
            except Exception as e:
                logger.debug("Error stopping pi bridge for task %s: %s", task_id, e)

    async def shutdown(self) -> None:
        """Stop all dedicated bridges (control bridge is NOT stopped here)."""
        for task_id in list(self._bridges.keys()):
            await self.release(task_id)
