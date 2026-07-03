"""Headless end-to-end demo: run a full SOP task through the real Symphony backend.

This drives EXACTLY the same backend path a `/sop` command in the TUI uses:
  EventLog(file-backed) -> EventBus -> TaskManager.create_task/start_task
  -> SOPExecutor -> real LLM calls (Volcengine, from data/config.toml)
and prints the node-by-node transitions plus the final file-log contents.
"""
import asyncio
import json
from pathlib import Path

from symphony.config.user_config import UserConfig
from symphony.core.event_bus import EventBus
from symphony.core.event_log import EventLog
from symphony.core.pi_bridge import PiBridge, PiBridgeConfig
from symphony.core.task_manager import TaskManager
from symphony.core.llm_provider import ProviderConfig as LLMProviderConfig, create_provider
from symphony.sop.sop_registry import SOPRegistry

DATA_DIR = "data"


async def main():
    uc = UserConfig.load(f"{DATA_DIR}/config.toml")

    event_log = EventLog(DATA_DIR)
    await event_log.connect()
    event_bus = EventBus(event_log)

    # pi bridge exists but is NOT started -> nodes with executor=llm use the
    # real direct LLM provider (Volcengine). No pi build needed for this demo.
    pi_bridge = PiBridge(PiBridgeConfig(pi_binary="pi", model=None))

    active = uc.get_active_provider_config()
    llm = create_provider(LLMProviderConfig(
        type=active.type, base_url=active.base_url, api_key=active.api_key,
        model=active.model, max_tokens=active.max_tokens, temperature=active.temperature,
    ))
    print(f"LLM provider available: {llm.is_available}  ({active.type}/{active.model})")

    tm = TaskManager(event_bus, event_log, pi_bridge, llm)
    registry = SOPRegistry(event_log, f"{DATA_DIR}/sop_templates")
    await registry.load_all()
    print("Loaded SOPs:", await registry.list_names())

    sop = await registry.get("demo-haiku")
    assert sop is not None, "demo-haiku SOP not found"

    initial_input = {"prompt": "the sea at dawn"}
    task = await tm.create_task(sop, metadata={"source": "demo", "inputs": initial_input,
                                               "prompt": initial_input["prompt"]})
    print(f"\nCreated task {task.task_id}  (SOP: demo-haiku)")
    print("Nodes:", " -> ".join(n.id for n in sop.nodes))

    await tm.start_task(task.task_id, sop)

    # Tail events until terminal.
    seen = 0
    while True:
        events = await event_log.get_events(task.task_id, after_seq=seen)
        for e in events:
            seen = max(seen, e["seq"])
            et = e["event_type"]; nid = e.get("node_id") or ""
            if et in ("node_started", "node_completed", "node_failed",
                      "task_completed", "task_failed"):
                print(f"  [{et}] {nid}")
        t = await tm.get_task(task.task_id)
        if t and t.status in ("completed", "failed", "cancelled"):
            print(f"\nFinal task status: {t.status}")
            break
        await asyncio.sleep(0.3)

    # Dump the actual node outputs from the file log.
    print("\n===== NODE OUTPUTS (from real LLM) =====")
    all_events = await event_log.get_events(task.task_id, after_seq=0)
    deltas = {}
    for e in all_events:
        if e["event_type"] == "agent_message_delta":
            nid = e.get("node_id") or "?"
            deltas.setdefault(nid, "")
            deltas[nid] += e["data"].get("text", "")
    for nid in [n.id for n in sop.nodes]:
        print(f"\n--- {nid} ---")
        print(deltas.get(nid, "(no text captured)").strip())

    # Show the file-log layout on disk.
    print("\n===== FILE-LOG STORAGE (no SQLite) =====")
    for p in sorted(Path(DATA_DIR).glob("logs/*.jsonl")):
        print(" ", p, f"({p.stat().st_size} bytes)")
    for p in sorted(Path(DATA_DIR).glob("tasks/*.json")):
        print(" ", p)

    await event_log.close()
    # write the task_id out for the renderer
    Path("demo_task_id.txt").write_text(task.task_id)


if __name__ == "__main__":
    asyncio.run(main())
