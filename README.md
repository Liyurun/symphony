# pi-symphony

Symphony is an agent workflow runtime for building, running, and inspecting SOP-style AI workflows. It combines a typed DAG executor, skill/tool calling, runtime traces, a terminal UI, and a dark web console for reviewing node inputs, outputs, logs, and final task summaries.

## Highlights

- **SOP workflow runtime**: Define reusable multi-step workflows with typed inputs and outputs.
- **Agent + skill execution**: Agent nodes can call registered skills; an empty `skills: []` list exposes all registered skills to that node.
- **Traceable DAG runs**: Inspect each node's prompt, input, output, retry history, logs, and LLM traces.
- **Task-level summaries**: Review the overall run status, node progress, and final output without opening each node.
- **Web and TUI interfaces**: Use the React web workspace or the terminal-native interface.
- **Config-first setup**: Runtime settings live in `config.yaml`; local secrets stay in `config.local.yaml`.

## Tech Stack

- Python 3.10+
- FastAPI + WebSocket runtime server
- Pydantic typed workflow models
- React + TypeScript + Vite web UI
- Textual terminal UI
- Zustand state management

## Quick Start

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

Create a local config file for secrets and environment-specific values:

```bash
cp config.yaml config.local.yaml
```

Then set the required LLM environment variables or edit `config.local.yaml`.

Start the terminal UI:

```bash
symphony
```

Start the web server:

```bash
symphony server --config config.local.yaml
```

Open:

```text
http://127.0.0.1:8899
```

## Web Workspace

The web UI is organized around a single-page workspace:

- **Chat**: Run agent conversations and inspect compact tool-call summaries.
- **SOP Runs**: Monitor workflow DAGs, inspect node I/O, review logs, and see task summaries.
- **SOP Studio**: Create and edit SOP templates.
- **Logs**: Inspect sessions, events, traces, and interactions.

## Development

Run Python tests:

```bash
python -m pytest
```

Build the web UI:

```bash
cd web
npm run build
```

If `npm` is installed outside the default shell path on macOS, use:

```bash
PATH=/opt/homebrew/bin:$PATH npm run build
```

## Repository Notes

- `config.local.yaml`, `.runtime/`, `.trae/`, `.symphony/`, virtualenvs, and build caches are ignored.
- Frontend source lives in `web/src`; generated `web/dist` is ignored.
- Runtime parameters should be added to `config.yaml` with clear comments, while local secrets belong in `config.local.yaml`.
