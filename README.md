# Symphony — SOP-Based Multi-Agent Task Orchestrator

Symphony adds SOP (Standard Operating Procedure) workflow orchestration on top of pi agent.

## Quick Start（一键安装）

在解压后的 `symphony-final` 目录里执行：

```bash
bash install.sh
```

脚本会自动：检测/安装 uv → 构建 pi（`dist/cli.js`，rpc 模式所需）→ `uv tool install --editable .`
把 `symphony` 命令装进 PATH。之后在任意位置直接输入：

```bash
symphony
```

即可启动（TUI + Web）。开箱即用——`data/config.toml` 已内置火山引擎（Volcengine Ark）
provider，无需再传任何 `--provider-*` 参数。

Symphony 会自动将 provider 配置写入 `~/.pi/agent/settings.json`。

## 配置

### 方式 1：命令行参数（一次性）

```bash
symphony --provider-url https://ark.cn-beijing.volces.com/api/v3 --provider-key sk-xxx --provider-model doubao-1.5-pro-32k
```

### 方式 2：环境变量（推荐）

```bash
export SYMPHONY_PROVIDER_URL=https://ark.cn-beijing.volces.com/api/v3
export SYMPHONY_PROVIDER_KEY=sk-xxx
export SYMPHONY_PROVIDER_MODEL=doubao-1.5-pro-32k
symphony
```

### 方式 3：配置文件 `data/config.toml`

Symphony 首次启动会自动创建 `data/config.toml`。也可以手动编辑：

```toml
[provider]
base_url = "https://ark.cn-beijing.volces.com/api/v3"
api_key = "sk-xxx"
model = "doubao-1.5-pro-32k"
max_tokens = 4096
temperature = 0.7

[pi_agent]
binary_path = "pi"
default_model = "custom/doubao-1.5-pro-32k"
thinking_level = "medium"
auto_compaction = true

[web_ui]
host = "0.0.0.0"
port = 8080
theme = "dark"
auto_scroll = true
max_log_entries = 1000

[tui]
theme = "textual-dark"
compact_view = false
# 是否把正常会话（非 SOP 的 pi 原生对话）也记录为任务写入本地日志，
# 便于日后在 Web 端查看与分析。默认开启。
log_normal_chat = true
```

| 字段 | 说明 | 默认值 |
|------|------|--------|
| `provider.base_url` | OpenAI 兼容 API 地址 | - |
| `provider.api_key` | API Key | - |
| `provider.model` | 模型 ID | `doubao-1.5-pro-32k` |
| `provider.max_tokens` | 最大输出 token | `4096` |
| `provider.temperature` | 温度 | `0.7` |
| `pi_agent.binary_path` | pi 可执行文件路径 | 自动检测 |
| `pi_agent.thinking_level` | 思考深度 | `medium` |
| `web_ui.port` | Web 端口 | `8080` |
| `web_ui.theme` | 主题 | `dark` |

### 方式 4：pi 的 settings.json（高级）

如果已配置过 pi 的内置 provider（如 Anthropic/OpenAI），直接在 `~/.pi/settings.json` 中配置：

```json
{
  "providers": {
    "doubao": {
      "baseUrl": "https://ark.cn-beijing.volces.com/api/v3",
      "apiKey": "sk-xxx",
      "models": [
        {
          "id": "doubao-1.5-pro-32k",
          "name": "Doubao Pro 32K",
          "reasoning": true,
          "input": ["text"],
          "cost": {"input": 1, "output": 2, "cacheRead": 0.25, "cacheWrite": 4},
          "contextWindow": 32768,
          "maxTokens": 4096
        }
      ]
    }
  }
}
```

然后用：

```bash
symphony --pi-model doubao/doubao-1.5-pro-32k
```

## 项目结构

```
symphony/
├── pyproject.toml        # Python 项目（uv 管理）
├── install.sh            # 一键安装脚本
├── data/
│   ├── config.toml       #   配置（已内置火山引擎 provider）
│   ├── logs/*.jsonl      #   事件日志（每个任务一个 JSONL，单一事实来源）
│   ├── tasks/*.json      #   任务元数据（每个任务一个 JSON）
│   └── sop_templates/    #   SOP 模板
├── symphony/             # Python 后端
│   ├── cli.py            #   入口（自动配置 provider）
│   ├── config/           #   配置模块（config.toml）
│   ├── core/             #   EventBus, EventLog（文件日志）, PiBridge, TaskManager
│   ├── sop/              #   SOP 定义、执行器、重试、人工介入
│   ├── tui/              #   原生 pi TUI（native_tui.py：pi 原生对话 + /sop）
│   └── web/              #   Web 服务器 + SPA 前端
├── pi-agent/             # pi 源码
└── tests/                # Python 测试（124 个）
```

> 存储说明：Symphony 已完全移除 SQLite，改用**本地追加式日志**。事件写入
> `data/logs/<task_id>.jsonl`，任务元数据写入 `data/tasks/<task_id>.json`。
> TUI 与 Web 共享同一份本地日志（单一事实来源），无需 WebSocket 同步。

## Web UI

打开 `http://localhost:8080`：

| Hash | 页面 |
|------|------|
| `#/tasks` | 任务列表 |
| `#/tasks/:id` | 任务详情（节点图 + 实时输出 + 人工审批 + 节点级「打断并重来」） |
| `#/sop` | SOP 模板编辑 |
| `#/logs` | 事件日志查看 |
| `#/settings` | 配置编辑 |

### 关键交互能力

- **一切皆任务（方案A）**：「新建任务」里可只填一个问题（不选 SOP）直接创建并自动开跑，
  合成为单节点 Q&A 任务；任务详情页底部输入框继续追问即为多轮对话（追加依赖上一轮的新节点）。
- **节点级「打断并重来」**：任务详情页每个节点卡片上的 `↻ Redo` 按钮可中断并整节点重跑，
  下游节点自动级联重跑。

## TUI（原生 pi + /sop）

TUI 是 pi 原生 agent 的交互终端，外加一个 Symphony 专属斜杠命令 `/sop`：

| 输入 | 作用 |
|------|------|
| `<直接输入>` | 与 pi 原生对话（正常会话）。默认也会写入本地日志（`log_normal_chat = true`），便于日后在 Web 端分析 |
| `/sop <name> [k=v ...] [自然语言]` | 在后端运行一条 SOP，Web 端可看到全部节点记录 |
| `/sops` | 列出可用 SOP 模板 |
| `/tasks` | 列出最近任务 |
| `/help` | 帮助 |
| `/quit` | 退出 |

TUI 从 TUI 发起的 SOP 在后端执行并写入共享日志，因此 Web 看板可完整看到每个节点的进度与输出。

## 测试

```bash
uv run pytest tests/ -v      # 或：python3 -m pytest tests/ -q
```
