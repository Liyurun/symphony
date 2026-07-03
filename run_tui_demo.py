"""Drive the REAL NativeTUI non-interactively by feeding it scripted stdin lines.

Proves the TUI slash-command path (/help, /sops, /tasks, /sop) works end to end,
including running a full SOP from the TUI (which goes through the backend and
writes the shared file log). Output is captured for a screenshot.
"""
import asyncio
import builtins
from pathlib import Path

from symphony.config.user_config import UserConfig
from symphony.core.event_bus import EventBus
from symphony.core.event_log import EventLog
from symphony.core.pi_bridge import PiBridge, PiBridgeConfig
from symphony.core.task_manager import TaskManager
from symphony.core.llm_provider import ProviderConfig as LLMProviderConfig, create_provider
from symphony.sop.sop_registry import SOPRegistry
from symphony.tui.native_tui import NativeTUI

DATA_DIR = "data"

SCRIPT = iter([
    "/help",
    "/sops",
    "/sop demo-haiku the sea at dawn",
    "/tasks",
    "/quit",
])


def scripted_input(prompt=""):
    try:
        line = next(SCRIPT)
    except StopIteration:
        raise EOFError
    print(f"{prompt}{line}")   # echo so the transcript shows what was typed
    return line


async def main():
    uc = UserConfig.load(f"{DATA_DIR}/config.toml")
    event_log = EventLog(DATA_DIR)
    await event_log.connect()
    event_bus = EventBus(event_log)
    pi_bridge = PiBridge(PiBridgeConfig(pi_binary="pi", model=None))
    active = uc.get_active_provider_config()
    llm = create_provider(LLMProviderConfig(
        type=active.type, base_url=active.base_url, api_key=active.api_key,
        model=active.model, max_tokens=active.max_tokens, temperature=active.temperature,
    ))
    tm = TaskManager(event_bus, event_log, pi_bridge, llm)
    registry = SOPRegistry(event_log, f"{DATA_DIR}/sop_templates")
    await registry.load_all()

    tui = NativeTUI(
        pi_bridge=pi_bridge, task_manager=tm, sop_registry=registry,
        event_bus=event_bus, event_log=event_log, web_port=8080,
        log_normal_chat=True, model=active.model,
    )

    # Patch the blocking input() used by the TUI with our scripted feeder.
    orig_input = builtins.input
    builtins.input = scripted_input
    try:
        await tui.run()
    finally:
        builtins.input = orig_input
        await event_log.close()


if __name__ == "__main__":
    asyncio.run(main())
