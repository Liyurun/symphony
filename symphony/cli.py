"""CLI entry point — `symphony` launches TUI + Web server (like `pi`).

Usage:
    symphony --provider-url https://ark.cn-beijing.volces.com/api/v3 --provider-key sk-xxx
    symphony --tui-only
    symphony --web-only --port 9090
    symphony --help
    symphony --version
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import webbrowser
from pathlib import Path

import click

from symphony import __version__

_logger = logging.getLogger("symphony")


def _find_pi_binary() -> str:
    """Auto-detect pi binary.

    Priority:
      1. Built pi entry `pi-agent/packages/coding-agent/dist/cli.js` — the ONLY
         bundled option that supports `--mode rpc` (full pi agent capability).
      2. `pi` on the system PATH.
      3. Fall back to bundled `pi-test.sh` ONLY as a last resort, with a loud
         warning that it does NOT support rpc (nodes will degrade to single-shot
         LLM). Build pi (`npm run build`) or pass `--pi-binary` for full power.
    """
    import shutil

    pkg_dir = Path(__file__).resolve().parent.parent
    built_cli = pkg_dir / "pi-agent" / "packages" / "coding-agent" / "dist" / "cli.js"
    if built_cli.exists():
        return str(built_cli)

    if shutil.which("pi"):
        return "pi"

    test_sh = pkg_dir / "pi-agent" / "pi-test.sh"
    if test_sh.exists():
        click.echo(
            "  ⚠️  Using pi-test.sh, which does NOT support `--mode rpc`.\n"
            "      Pi's full agent capability (skills + tools) will be UNAVAILABLE\n"
            "      and nodes will degrade to single-shot LLM.\n"
            "      Fix: cd pi-agent/packages/coding-agent && npm install && npm run build\n"
            "      then start with: --pi-binary pi-agent/packages/coding-agent/dist/cli.js",
            err=True,
        )
        return str(test_sh)

    return "pi"


def _resolve_pi_binary(pi_binary: str) -> str:
    """Resolve a user-supplied --pi-binary path robustly.

    A relative path is confusing because it is interpreted against the current
    working directory, which is often NOT the project root (the user may be in
    ``pi-agent/`` or anywhere). We therefore try, in order:

      1. The path as given (absolute, or relative to CWD).
      2. The path relative to the Symphony package root (where the project and
         the bundled ``pi-agent/`` live).

    Returns the first existing candidate. If ``pi_binary`` is a bare command
    name (e.g. "pi") with no path separator, it is returned unchanged so PATH
    lookup still works. If nothing resolves, the ORIGINAL value is returned so
    the caller can fail fast with a clear message.
    """
    import os

    # Bare command name — let PATH handle it.
    if os.sep not in pi_binary and (os.altsep is None or os.altsep not in pi_binary):
        return pi_binary

    p = Path(pi_binary).expanduser()
    if p.exists():
        return str(p.resolve())

    if not p.is_absolute():
        pkg_dir = Path(__file__).resolve().parent.parent
        candidate = (pkg_dir / pi_binary).resolve()
        if candidate.exists():
            return str(candidate)

    return pi_binary


def _default_pi_cwd() -> str | None:
    """Return the bundled pi-agent directory so pi can load its AGENTS.md.

    Pi discovers context files such as AGENTS.md by walking upward from its
    process cwd. When Symphony is launched from the symphony project root, the
    pi-agent/AGENTS.md file is a child directory and would not be discovered.
    Prefer the bundled pi-agent directory when present; callers can still
    override this via SYMPHONY_PI_CWD / Settings.pi_cwd.
    """
    project_root = Path(__file__).resolve().parent.parent
    pi_agent_dir = project_root / "pi-agent"
    if (pi_agent_dir / "AGENTS.md").is_file():
        return str(pi_agent_dir)
    return None


def _pi_model_arg(pi_model: str | None, provider_type: str, provider_model: str) -> str | None:
    """Resolve the model argument passed to `pi --mode rpc`.

    For OpenAI-compatible providers Symphony writes a `custom` provider into
    pi's models.json, so pass `custom/<model>` to avoid pi falling back to an
    unrelated saved/default model.
    """
    if pi_model:
        return pi_model
    if not provider_model:
        return None
    if provider_type == "openai":
        return f"custom/{provider_model}"
    # Mira/custom_http are direct-LLM adapters here, not pi model providers.
    # Passing their model IDs to pi would make pi try to resolve an unknown
    # model and can prevent the bridge from starting. Users can still override
    # this explicitly with --pi-model when they really want pi to use something.
    return None


def _setup_pi_provider(base_url: str, api_key: str, model: str) -> None:
    """Write pi's ~/.pi/agent/models.json so pi can use a custom OpenAI-compatible provider.

    This is the only configuration a user needs — base URL + API key.
    After this, pi can call any OpenAI-compatible API (Doubao, DeepSeek, etc.).
    """
    agent_dir = Path.home() / ".pi" / "agent"
    models_path = agent_dir / "models.json"
    settings_path = agent_dir / "settings.json"
    agent_dir.mkdir(parents=True, exist_ok=True)

    # Load existing custom model registry if any.
    models_config = {}
    if models_path.exists():
        try:
            models_config = json.loads(models_path.read_text())
        except json.JSONDecodeError:
            pass
    if not isinstance(models_config, dict):
        models_config = {}

    # Use "custom" as the provider name — pi accepts any provider name
    # with baseUrl + apiKey for OpenAI-compatible APIs
    provider_config = {
        "baseUrl": base_url.rstrip("/"),
        "apiKey": api_key,
        "api": "openai-completions",
        "compat": {
            "supportsDeveloperRole": False,
            "supportsReasoningEffort": False,
            "supportsStore": False,
            "maxTokensField": "max_tokens",
        },
        "models": [
            {
                "id": model,
                "name": model,
                "api": "openai-completions",
                "reasoning": True,
                "compat": {
                    "supportsDeveloperRole": False,
                    "supportsReasoningEffort": False,
                    "supportsStore": False,
                    "maxTokensField": "max_tokens",
                },
                "input": ["text"],
                "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
            }
        ],
    }

    models_config["providers"] = models_config.get("providers", {})
    models_config["providers"]["custom"] = provider_config
    models_path.write_text(json.dumps(models_config, indent=2))

    # Also seed pi's saved default so launches without --model still work.
    settings = {}
    if settings_path.exists():
        try:
            settings = json.loads(settings_path.read_text())
        except json.JSONDecodeError:
            pass
    if not isinstance(settings, dict):
        settings = {}
    settings["defaultProvider"] = "custom"
    settings["defaultModel"] = model
    settings_path.write_text(json.dumps(settings, indent=2))

    click.echo(f"  Pi provider config written to {models_path}")
    click.echo(f"  Provider: custom/{model}")


@click.command()
@click.option("--tui-only", is_flag=True, help="Start only the TUI (no web server)")
@click.option("--web-only", is_flag=True, help="Start only the web server (no TUI)")
@click.option("--host", default=None, help="Web server host (defaults to [web_ui].host)")
@click.option("--port", default=None, type=int, help="Web server port (defaults to [web_ui].port)")
@click.option(
    "--open-browser/--no-open-browser",
    default=True,
    help="Open the Web UI in the browser when the web server starts",
)
@click.option("--pi-binary", default=None, help="Path to pi agent binary (auto-detected)")
@click.option("--pi-model", default=None, help="Model override for pi agent")
@click.option(
    "--provider-type",
    default=None,
    type=click.Choice(["openai", "mira", "custom_http", "http", "nonstandard"]),
    help="Provider type: openai (default), mira, or custom_http",
)
@click.option(
    "--provider-url",
    default=None,
    help="API base URL / Mira host",
)
@click.option(
    "--provider-key",
    default=None,
    help="API key / Mira session token",
)
@click.option(
    "--provider-model",
    default="doubao-1.5-pro-32k",
    help="Model ID to use (default: doubao-1.5-pro-32k)",
)
@click.option(
    "--active-provider",
    default=None,
    help="Which named provider to use (default, mira, doubao, etc.)",
)
@click.option("--data-dir", default="data", help="Data directory")
@click.option("--log-level", default="INFO", help="Log level")
@click.version_option(__version__, prog_name="symphony")
def main(
    tui_only: bool,
    web_only: bool,
    host: str | None,
    port: int | None,
    open_browser: bool,
    pi_binary: str | None,
    pi_model: str | None,
    provider_type: str | None,
    provider_url: str | None,
    provider_key: str | None,
    provider_model: str,
    active_provider: str | None,
    data_dir: str,
    log_level: str,
):
    """Symphony — SOP-based multi-agent task orchestrator.

    Just provide an API URL and key to get started:

    \b
    # Doubao (火山引擎)
    symphony --provider-url https://ark.cn-beijing.volces.com/api/v3 --provider-key YOUR_KEY

    \b
    # DeepSeek
    symphony --provider-url https://api.deepseek.com/v1 --provider-key YOUR_KEY --provider-model deepseek-chat

    \b
    # OpenAI
    symphony --provider-url https://api.openai.com/v1 --provider-key YOUR_KEY --provider-model gpt-4o

    Or set environment variables:
      SYMPHONY_PROVIDER_URL, SYMPHONY_PROVIDER_KEY, SYMPHONY_PROVIDER_MODEL
    """

    # Resolve provider config: CLI args > env vars
    ptype = provider_type or os.environ.get("SYMPHONY_PROVIDER_TYPE", "openai")
    url = provider_url or os.environ.get("SYMPHONY_PROVIDER_URL", "")
    key = provider_key or os.environ.get("SYMPHONY_PROVIDER_KEY", "")
    model = provider_model or os.environ.get("SYMPHONY_PROVIDER_MODEL", "doubao-1.5-pro-32k")

    # Resolve pi binary. Precedence:
    #   1. explicit --pi-binary flag
    #   2. [pi_agent].binary_path in data/config.toml (if it points at a real
    #      file, e.g. an absolute path to dist/cli.js — a bare "pi" is ignored
    #      here so auto-detection can find the bundled build first)
    #   3. auto-detection (bundled dist/cli.js -> PATH `pi` -> pi-test.sh)
    # This lets a bare `symphony` launch work entirely off the config file.
    if pi_binary is None:
        cfg_pi_binary = ""
        try:
            from symphony.config.user_config import UserConfig
            _uc = UserConfig.load(Path(data_dir) / "config.toml")
            cfg_pi_binary = (_uc.pi_agent.binary_path or "").strip()
        except Exception:
            cfg_pi_binary = ""

        # Only honor a config path that actually resolves to a file; a bare
        # command name like "pi" falls through to auto-detection so the bundled
        # rpc-capable build is preferred over a possibly-degraded PATH `pi`.
        is_bare = cfg_pi_binary and (os.sep not in cfg_pi_binary) and (
            os.altsep is None or os.altsep not in cfg_pi_binary
        )
        resolved_cfg = _resolve_pi_binary(cfg_pi_binary) if cfg_pi_binary else ""
        if cfg_pi_binary and not is_bare and Path(resolved_cfg).exists():
            pi_binary = resolved_cfg
        else:
            pi_binary = _find_pi_binary()
    else:
        # An explicit --pi-binary is resolved robustly (relative paths are tried
        # against the package root too) and MUST exist — otherwise fail fast so
        # the user doesn't silently run the degraded single-shot LLM path.
        resolved = _resolve_pi_binary(pi_binary)
        is_bare_cmd = (os.sep not in pi_binary) and (
            os.altsep is None or os.altsep not in pi_binary
        )
        if not is_bare_cmd and not Path(resolved).exists():
            raise click.ClickException(
                f"--pi-binary path not found: {pi_binary}\n"
                f"  Tried: {resolved}\n"
                f"  Build pi first:\n"
                f"    cd pi-agent/packages/coding-agent && npm install && npm run build\n"
                f"  Then pass the built entry (absolute path recommended):\n"
                f"    --pi-binary <project>/pi-agent/packages/coding-agent/dist/cli.js\n"
                f"  Or omit --pi-binary entirely to let Symphony auto-detect it."
            )
        pi_binary = resolved

    # Write pi settings if provider config provided (only for OpenAI-compatible type).
    # Non-standard/custom_http providers are direct-LLM adapters and cannot be
    # passed to pi as-is unless they expose an OpenAI-compatible endpoint.
    if url and key and ptype == "openai":
        _setup_pi_provider(url, key, model)

    # Also save to symphony's own config
    if url or key or active_provider:
        from symphony.config.user_config import UserConfig
        config_path = Path(data_dir) / "config.toml"
        uc = UserConfig.load(config_path)
        uc.provider.type = ptype
        if url:
            uc.provider.base_url = url
        if key:
            uc.provider.api_key = key
        if provider_model:
            uc.provider.model = provider_model
        if active_provider:
            uc.active_provider = active_provider
        uc.save(config_path)

    # Logging: when the TUI is active (i.e. NOT --web-only), the terminal is
    # owned by the full-screen TUI. Any stray log line or uvicorn banner would
    # corrupt its rendering, so we route ALL logging to a file
    # (<data-dir>/symphony.log) and keep the terminal clean. In --web-only mode
    # there is no TUI, so logs go to the console as usual.
    tui_active = not web_only
    if tui_active:
        Path(data_dir).mkdir(parents=True, exist_ok=True)
        log_file = str(Path(data_dir) / "symphony.log")
        logging.basicConfig(
            level=getattr(logging, log_level.upper()),
            format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            filename=log_file,
            filemode="a",
        )
    else:
        log_file = None
        logging.basicConfig(
            level=getattr(logging, log_level.upper()),
            format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        )

    logger = logging.getLogger("symphony")
    logger.info(f"Symphony v{__version__} starting...")
    logger.info(f"Pi binary: {pi_binary}")

    asyncio.run(
        _run(
            tui_only=tui_only,
            web_only=web_only,
            host=host,
            port=port,
            open_browser=open_browser,
            pi_binary=pi_binary,
            pi_model=pi_model,
            data_dir=data_dir,
            log_file=log_file,
        )
    )


def _web_url_for_browser(host: str, port: int) -> str:
    """Return the URL users should open in a local browser."""
    display_host = host or "localhost"
    if display_host in {"0.0.0.0", "::", "[::]"}:
        display_host = "localhost"
    return f"http://{display_host}:{port}/"


async def _open_browser_later(url: str, delay: float = 0.8) -> None:
    """Open the Web UI after uvicorn has had a moment to bind the port."""
    logger = logging.getLogger("symphony")
    try:
        await asyncio.sleep(delay)
        ok = webbrowser.open(url, new=2)
        if ok:
            logger.info(f"Opened browser: {url}")
        else:
            logger.info(f"Browser open returned false for: {url}")
    except Exception as e:
        logger.debug(f"Could not open browser for {url}: {e}")


async def _run(
    tui_only: bool,
    web_only: bool,
    host: str | None,
    port: int | None,
    open_browser: bool,
    pi_binary: str,
    pi_model: str | None,
    data_dir: str,
    log_file: str | None = None,
):
    """Async entry point — initializes all components and starts services."""

    logger = logging.getLogger("symphony")

    from symphony.config.settings import Settings
    from symphony.config.user_config import UserConfig
    from symphony.core.event_bus import EventBus
    from symphony.core.event_log import EventLog
    from symphony.core.pi_bridge import PiBridge, PiBridgeConfig
    from symphony.core.task_manager import TaskManager
    from symphony.sop.sop_registry import SOPRegistry

    settings = Settings(
        data_dir=data_dir,
        sop_templates_dir=f"{data_dir}/sop_templates",
        config_path=f"{data_dir}/config.toml",
        pi_binary=pi_binary,
        pi_model=pi_model,
        web_host=host or "0.0.0.0",
        web_port=port or 8080,
    )

    user_config = UserConfig.load(settings.config_path)
    effective_host = host or user_config.web_ui.host or settings.web_host
    effective_port = port if port is not None else (user_config.web_ui.port or settings.web_port)
    web_url = _web_url_for_browser(effective_host, effective_port)

    # Keep Settings aligned with the effective Web endpoint. Previously the web
    # server used the CLI default 8080 while the TUI showed [web_ui].port (e.g.
    # 8765), which made http://localhost:8765/ look broken.
    settings.web_host = effective_host
    settings.web_port = effective_port

    # The startup banner only makes sense when NO TUI will take over the
    # terminal. When the TUI is active it would just be scrolled away / corrupt
    # the first frame, so we suppress it and point the user at the log file.
    if web_only:
        click.echo(f"\U0001F3B5 Symphony v{__version__} - SOP Orchestrator")
        click.echo(f"   Web UI: {web_url}")
        click.echo("")
    elif not tui_only:
        # TUI + Web: print a single short line BEFORE the TUI starts, so the
        # user knows where the Web UI and logs are; the TUI then owns the screen.
        click.echo(
            f"\U0001F3B5 Symphony v{__version__} — Web UI: {web_url}"
            + (f"  |  logs: {log_file}" if log_file else "")
        )
        click.echo("   Starting TUI…")

    event_log = EventLog(settings.data_dir)
    await event_log.connect()

    event_bus = EventBus(event_log)

    # Create direct LLM provider from config
    from symphony.core.llm_provider import ProviderConfig as LLMProviderConfig, create_provider
    active = user_config.get_active_provider_config()
    # Explicitly pass the configured model into `pi --mode rpc`. Without this,
    # pi falls back to its own saved/default model selection; on a fresh machine
    # that can leave the RPC session with `model: unknown`, so prompts appear to
    # hang with no assistant output.
    model = pi_model or user_config.pi_agent.default_model or active.model or ""
    pi_config = PiBridgeConfig(
        pi_binary=pi_binary or user_config.pi_agent.binary_path or "pi",
        model=_pi_model_arg(pi_model or user_config.pi_agent.default_model, active.type, active.model),
        startup_timeout=user_config.pi_agent.startup_timeout,
        request_timeout=user_config.pi_agent.request_timeout,
        cwd=settings.pi_cwd or _default_pi_cwd(),
    )
    if pi_config.cwd:
        logger.info(f"Pi cwd: {pi_config.cwd}")
    for info in pi_config.context_file_infos():
        if info.get("error"):
            logger.warning(f"Pi context file unreadable: {info['path']}: {info['error']}")
        else:
            logger.info(
                "Pi context file in scope: "
                f"{info['path']} sha256={info['sha256_short']} bytes={info['bytes']}"
            )
    pi_bridge = PiBridge(pi_config)

    llm_config = LLMProviderConfig(
        type=active.type,
        base_url=active.base_url,
        api_key=active.api_key,
        model=active.model,
        max_tokens=active.max_tokens,
        temperature=active.temperature,
        extra_headers=active.extra_headers,
        endpoint=active.endpoint,
        method=active.method,
        auth_header=active.auth_header,
        auth_prefix=active.auth_prefix,
        request_template=active.request_template,
        response_path=active.response_path,
        stream_response_path=active.stream_response_path,
        stream=active.stream,
    )
    llm_provider = create_provider(llm_config)
    if llm_provider.is_available:
        logger.info(f"LLM provider: {llm_config.type}/{llm_config.model} ({user_config.active_provider})")
    else:
        logger.info("LLM provider not configured — will use pi bridge fallback")

    # The active model, resolved from the config file. Used to seed the TUI and,
    # for OpenAI-compatible providers, to write pi's provider settings so a bare
    # `symphony` launch (no CLI flags) works entirely off the config file.

    # Seed pi's own provider settings from the config file when the active
    # provider is OpenAI-compatible. This mirrors what the CLI flags do, so the
    # user only needs to fill in data/config.toml — no flags required.
    if active.type == "openai" and active.base_url and active.api_key:
        try:
            _setup_pi_provider(active.base_url, active.api_key, model)
        except Exception as e:
            logger.warning(f"Could not write pi provider settings: {e}")

    try:
        await pi_bridge.start()
        logger.info("Pi agent bridge connected")
    except Exception as e:
        logger.warning(f"Could not start pi agent: {e}. Running without agent integration.")

    # Per-task pi process pool: each SOP/Q&A task gets its own dedicated pi
    # subprocess so multiple tasks run truly in parallel without their event
    # streams or aborts colliding. ``pi_bridge`` stays as the shared control
    # bridge (skills/models/TUI/Q&A follow-up) and the pool's fallback.
    from symphony.core.pi_pool import PiBridgePool

    pi_pool = PiBridgePool(pi_config, pi_bridge)
    task_manager = TaskManager(
        event_bus, event_log, pi_bridge, llm_provider, pi_pool=pi_pool
    )

    sop_registry = SOPRegistry(event_log, settings.sop_templates_dir)
    await sop_registry.load_all()
    sop_count = len(await sop_registry.list_names())
    logger.info(f"Loaded {sop_count} SOP templates")

    def on_pi_event(raw_event: dict):
        try:
            from symphony.core.event_bus import SymphonyEvent
            from symphony.core.pi_bridge import _TurnAccumulator
            et = raw_event.get("type", "")
            tid = raw_event.get("task_id", "")
            # pi's native RPC events usually do not carry a Symphony task_id.
            # Task-scoped execution is already forwarded by SOPExecutor with the
            # correct task/node id. Persisting these raw global events as
            # task_id="unknown" creates huge logs and can surface empty/repeated
            # reasoning-only deltas in the UI, so only forward events that are
            # explicitly tagged with a real Symphony task id.
            if not tid:
                return

            if et == "message_start":
                asyncio.create_task(
                    event_bus.publish(SymphonyEvent(task_id=tid, event_type="agent_message_start", data=raw_event)))
            elif et == "message_update":
                msg = raw_event.get("message") if isinstance(raw_event, dict) else None
                text = _TurnAccumulator._extract_text(msg) if isinstance(msg, dict) else raw_event.get("text", "")
                if not text and isinstance(msg, dict) and isinstance(msg.get("errorMessage"), str):
                    text = msg["errorMessage"]
                if text:
                    asyncio.create_task(
                        event_bus.publish(SymphonyEvent(task_id=tid, event_type="agent_message_delta",
                                                         data={"text": text, "replace": True})))
            elif et == "message_end":
                asyncio.create_task(
                    event_bus.publish(SymphonyEvent(task_id=tid, event_type="agent_message_end", data=raw_event)))
            elif et == "tool_execution_start":
                args = raw_event.get("args", raw_event.get("arguments", raw_event.get("input", {})))
                asyncio.create_task(
                    event_bus.publish(SymphonyEvent(task_id=tid, event_type="tool_call_start",
                                                     data={"tool_name": raw_event.get("toolName", raw_event.get("tool_name", raw_event.get("name", ""))),
                                                           "arguments": args})))
            elif et == "tool_execution_update":
                asyncio.create_task(
                    event_bus.publish(SymphonyEvent(task_id=tid, event_type="tool_call_update",
                                                     data={"tool_name": raw_event.get("toolName", raw_event.get("tool_name", raw_event.get("name", ""))),
                                                           "partial_result": raw_event.get("partialResult"),
                                                           "tool_call_id": raw_event.get("toolCallId")})))
            elif et == "tool_execution_end":
                asyncio.create_task(
                    event_bus.publish(SymphonyEvent(task_id=tid, event_type="tool_call_end", data=raw_event)))
        except Exception as e:
            logger.debug(f"Error handling pi event: {e}")

    pi_bridge.on_agent_event(on_pi_event)

    tasks = []

    tui_impl = os.environ.get("SYMPHONY_TUI_IMPL", "ts").strip().lower()
    needs_web_server = web_only or not tui_only or tui_impl == "ts"

    web_server = None
    if needs_web_server:
        from symphony.web.server import WebServer
        web_server = WebServer(
            event_bus=event_bus, event_log=event_log, task_manager=task_manager,
            sop_registry=sop_registry, pi_bridge=pi_bridge, user_config=user_config,
            host=effective_host, port=effective_port,
        )
        logger.info(f"Web server: http://{effective_host}:{effective_port}")
        # When the TUI is active it owns the terminal, so uvicorn must stay quiet
        # to avoid corrupting the TUI. web_only=True means no TUI -> normal logs.
        tasks.append(asyncio.create_task(web_server.start(quiet=not web_only)))
        if open_browser and not tui_only:
            asyncio.create_task(_open_browser_later(web_url))

    if not web_only:
        try:
            await _start_tui(
                pi_bridge, event_bus, event_log, task_manager, sop_registry,
                web_port=effective_port, model=model, web_url=web_url,
                log_normal_chat=user_config.tui.log_normal_chat,
            )
        except Exception as e:
            logger.error(f"TUI error: {e}")

        # In TUI mode the web server is only a local API/control plane for the
        # terminal. Once the TUI exits, shut it down instead of waiting forever.
        if web_server is not None:
            await web_server.stop()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        tasks = []

    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    # Recycle any dedicated per-task pi subprocesses still alive, then stop the
    # shared control bridge.
    try:
        await pi_pool.shutdown()
    except Exception as e:
        logger.debug(f"pi pool shutdown error: {e}")
    await pi_bridge.stop()
    await event_log.close()


async def _start_tui(
    pi_bridge, event_bus, event_log, task_manager, sop_registry=None,
    web_port: int = 8080, model: str = "", web_url: str | None = None,
    log_normal_chat: bool = True,
):
    """Start the Symphony TUI.

    Default is the TypeScript TUI so the terminal UI can evolve on top of the
    same ecosystem as pi's native TUI. Set ``SYMPHONY_TUI_IMPL=python`` to use
    the legacy Python fallback.
    """
    impl = os.environ.get("SYMPHONY_TUI_IMPL", "ts").strip().lower()
    if impl in {"ts", "typescript", "node"}:
        try:
            await _start_ts_tui(web_port=web_port, model=model, web_url=web_url)
            return
        except Exception as e:
            logging.getLogger("symphony").warning(
                f"Could not start TS TUI ({e}); falling back to Python TUI."
            )

    from symphony.tui.native_tui import NativeTUI

    tui = NativeTUI(
        pi_bridge=pi_bridge,
        task_manager=task_manager,
        sop_registry=sop_registry,
        event_bus=event_bus,
        event_log=event_log,
        web_port=web_port,
        log_normal_chat=log_normal_chat,
        model=model,
    )
    await tui.run()


async def _start_ts_tui(
    web_port: int = 8080,
    model: str = "",
    web_url: str | None = None,
) -> None:
    """Run the TypeScript Symphony TUI as a child process."""
    project_root = Path(__file__).resolve().parent.parent
    script = project_root / "symphony" / "tui" / "symphony_tui.ts"
    tsx_cli = project_root / "pi-agent" / "node_modules" / "tsx" / "dist" / "cli.mjs"
    if not script.exists():
        raise RuntimeError(f"TS TUI script not found: {script}")
    if not tsx_cli.exists():
        raise RuntimeError(
            "tsx runtime not found. Run `cd pi-agent && npm install`, "
            "or set SYMPHONY_TUI_IMPL=python."
        )

    server_url = f"http://localhost:{web_port}"
    display_url = (web_url or server_url).rstrip("/")
    # Give uvicorn a brief moment to bind before the TS client starts issuing
    # HTTP requests. The TS client still surfaces any connection error clearly.
    await asyncio.sleep(0.35)
    proc = await asyncio.create_subprocess_exec(
        "node",
        str(tsx_cli),
        str(script),
        "--server",
        server_url,
        "--web-url",
        display_url,
        "--model",
        model or "",
        cwd=str(project_root),
    )
    code = await proc.wait()
    if code not in (0, None):
        raise RuntimeError(f"TS TUI exited with code {code}")


if __name__ == "__main__":
    main()
