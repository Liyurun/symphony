"""File-backed append-only event log — the single source of truth.

Storage layout (human-readable, no SQLite):

    <data_dir>/
    ├── logs/
    │   └── <task_id>.jsonl      # append-only event stream, one JSON per line
    ├── tasks/
    │   └── <task_id>.json       # task metadata + latest status
    └── sop_templates/
        └── <name>.json          # persisted SOP templates

Design notes
------------
- Events are append-only; each line in ``<task_id>.jsonl`` is a full event
  record ``{seq, task_id, node_id, event_type, data, timestamp}``.
- Task metadata (mutable status, claim, etc.) lives in a small JSON file so we
  can update it in place without rewriting the event stream.
- An in-process ``asyncio.Lock`` per log serializes seq allocation + append so
  concurrent publishers (parallel SOP nodes, forwarded pi events) can't collide
  on a seq. This is intentionally simpler than the previous SQLite design — no
  cross-event-loop owner thread is needed because plain file appends never
  orphan a database future.
- All methods are ``async`` with the SAME signatures as the old SQLite
  ``EventLog`` so the rest of the codebase (EventBus, SOPExecutor, Web routes)
  needs ZERO changes.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Optional


class EventLog:
    """Append-only event log backed by local JSON/JSONL files.

    Drop-in replacement for the former SQLite-backed EventLog. Same async API,
    same return shapes.
    """

    def __init__(self, data_dir: str | Path = "data"):
        # Accept either a data directory or a legacy ".db" path (from which we
        # derive the parent directory) so old call sites keep working.
        p = Path(data_dir)
        if p.suffix == ".db":
            p = p.parent
        self.data_dir = p
        self.logs_dir = self.data_dir / "logs"
        self.tasks_dir = self.data_dir / "tasks"
        self.templates_dir = self.data_dir / "sop_templates"
        # Per-task append lock (lazily created on the running loop).
        self._locks: dict[str, "asyncio.Lock"] = {}
        # Marker so tests / callers can detect "connected" state.
        self._connected = False

    # ── lifecycle ──────────────────────────────────────────────

    async def connect(self) -> None:
        """Create the storage directories."""
        for d in (self.logs_dir, self.tasks_dir, self.templates_dir):
            d.mkdir(parents=True, exist_ok=True)
        self._connected = True

    async def close(self) -> None:
        self._connected = False
        self._locks.clear()

    # ── helpers ────────────────────────────────────────────────

    def _lock_for(self, task_id: str) -> "asyncio.Lock":
        lock = self._locks.get(task_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[task_id] = lock
        return lock

    def _task_file(self, task_id: str) -> Path:
        return self.tasks_dir / f"{task_id}.json"

    def _log_file(self, task_id: str) -> Path:
        return self.logs_dir / f"{task_id}.jsonl"

    def _template_file(self, name: str) -> Path:
        return self.templates_dir / f"{name}.json"

    @staticmethod
    def _read_json(path: Path) -> Optional[dict]:
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None

    @staticmethod
    def _write_json(path: Path, obj: dict) -> None:
        # Atomic-ish write: write to a temp file then replace.
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)

    # ── Task CRUD ──────────────────────────────────────────────

    async def create_task(
        self, task_id: str, sop_name: str, sop_version: str = "1.0",
        metadata: dict | None = None,
    ) -> None:
        now = time.time()
        self._write_json(self._task_file(task_id), {
            "task_id": task_id,
            "sop_name": sop_name,
            "sop_version": sop_version,
            "status": "pending",
            "claimed_by": None,
            "claimed_at": None,
            "created_at": now,
            "updated_at": now,
            "metadata": metadata or {},
        })
        # Ensure the log file exists (empty) so get_events works immediately.
        self._log_file(task_id).touch(exist_ok=True)

    async def update_task_status(self, task_id: str, status: str) -> None:
        task = self._read_json(self._task_file(task_id))
        if task is None:
            return
        task["status"] = status
        task["updated_at"] = time.time()
        self._write_json(self._task_file(task_id), task)

    async def claim_task(self, task_id: str, claimed_by: str) -> bool:
        """Claim a task. Returns False if already claimed by another client."""
        task = self._read_json(self._task_file(task_id))
        if task is None:
            return False
        current = task.get("claimed_by")
        if current is not None and current != claimed_by:
            return False
        now = time.time()
        task["claimed_by"] = claimed_by
        task["claimed_at"] = now
        task["updated_at"] = now
        self._write_json(self._task_file(task_id), task)
        return True

    async def release_task(self, task_id: str) -> None:
        task = self._read_json(self._task_file(task_id))
        if task is None:
            return
        task["claimed_by"] = None
        task["claimed_at"] = None
        task["updated_at"] = time.time()
        self._write_json(self._task_file(task_id), task)

    async def delete_task(self, task_id: str) -> None:
        self._task_file(task_id).unlink(missing_ok=True)
        self._log_file(task_id).unlink(missing_ok=True)
        self._locks.pop(task_id, None)

    async def get_task(self, task_id: str) -> Optional[dict]:
        return self._read_json(self._task_file(task_id))

    async def list_tasks(self, limit: int = 50, status: str | None = None) -> list[dict]:
        tasks = []
        for f in self.tasks_dir.glob("*.json"):
            t = self._read_json(f)
            if t is None:
                continue
            if status and t.get("status") != status:
                continue
            tasks.append(t)
        # Most-recently-updated first, matching the old ORDER BY updated_at DESC.
        tasks.sort(key=lambda t: t.get("updated_at", 0), reverse=True)
        return tasks[:limit]

    # ── Event append & query ───────────────────────────────────

    async def append(self, event: dict) -> int:
        """Append an event to the log. Returns the seq number.

        The per-task lock serializes seq allocation + append so concurrent
        callers cannot pick the same seq for a task.
        """
        task_id = event["task_id"]
        log_path = self._log_file(task_id)
        async with self._lock_for(task_id):
            seq = await self.get_last_seq(task_id) + 1
            record = {
                "seq": seq,
                "task_id": task_id,
                "node_id": event.get("node_id"),
                "event_type": event["event_type"],
                "data": event.get("data", {}),
                "timestamp": event.get("timestamp", time.time()),
            }
            with log_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        return seq

    def _iter_events(self, task_id: str) -> list[dict]:
        path = self._log_file(task_id)
        if not path.exists():
            return []
        out = []
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return out

    async def get_events(
        self, task_id: str, after_seq: int = 0, limit: int = 500,
        event_type: str | None = None,
    ) -> list[dict]:
        """Get events for a task, optionally filtered by type."""
        events = [e for e in self._iter_events(task_id) if e["seq"] > after_seq]
        if event_type:
            events = [e for e in events if e["event_type"] == event_type]
        events.sort(key=lambda e: e["seq"])
        return events[:limit]

    async def get_last_seq(self, task_id: str) -> int:
        events = self._iter_events(task_id)
        return max((e["seq"] for e in events), default=0)

    async def get_node_artifacts(self, task_id: str) -> dict[str, dict]:
        """Aggregate each node's latest artifact from the event stream.

        Reads ``node_completed`` events and keeps the last artifact seen per
        node (later writes win). Returns ``{}`` for tasks without artifacts.
        """
        out: dict[str, dict] = {}
        for e in self._iter_events(task_id):
            if e.get("event_type") != "node_completed":
                continue
            node_id = e.get("node_id")
            art = (e.get("data") or {}).get("artifact")
            if node_id and art:
                out[node_id] = art
        return out

    async def search_events(
        self, task_id: str | None = None, event_type: str | None = None,
        node_id: str | None = None, limit: int = 200, offset: int = 0,
    ) -> list[dict]:
        """Search events across tasks with optional filters."""
        if task_id:
            candidate_ids = [task_id]
        else:
            candidate_ids = [f.stem for f in self.logs_dir.glob("*.jsonl")]

        results = []
        for tid in candidate_ids:
            for e in self._iter_events(tid):
                if event_type and e["event_type"] != event_type:
                    continue
                if node_id and e.get("node_id") != node_id:
                    continue
                results.append(e)
        # Newest first, matching old ORDER BY timestamp DESC.
        results.sort(key=lambda e: e.get("timestamp", 0), reverse=True)
        return results[offset:offset + limit]

    async def get_event_stats(self) -> dict:
        """Get event statistics."""
        by_type: dict[str, int] = {}
        total_events = 0
        for f in self.logs_dir.glob("*.jsonl"):
            for e in self._iter_events(f.stem):
                total_events += 1
                by_type[e["event_type"]] = by_type.get(e["event_type"], 0) + 1
        by_type = dict(sorted(by_type.items(), key=lambda kv: kv[1], reverse=True))
        total_tasks = sum(1 for _ in self.tasks_dir.glob("*.json"))
        return {
            "total_tasks": total_tasks,
            "total_events": total_events,
            "events_by_type": by_type,
        }

    # ── SOP template CRUD ──────────────────────────────────────

    async def save_sop_template(self, name: str, version: str, definition: dict) -> None:
        now = time.time()
        existing = self._read_json(self._template_file(name))
        created_at = existing.get("created_at", now) if existing else now
        self._write_json(self._template_file(name), {
            "name": name,
            "version": version,
            "definition": definition,
            "created_at": created_at,
            "updated_at": now,
        })

    async def get_sop_template(self, name: str) -> Optional[dict]:
        return self._read_json(self._template_file(name))

    async def list_sop_templates(self) -> list[dict]:
        out = []
        for f in self.templates_dir.glob("*.json"):
            t = self._read_json(f)
            if t is not None:
                out.append(t)
        out.sort(key=lambda t: t.get("name", ""))
        return out

    async def delete_sop_template(self, name: str) -> None:
        self._template_file(name).unlink(missing_ok=True)
