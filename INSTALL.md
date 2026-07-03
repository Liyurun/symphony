# Symphony 安装与使用教程（当前版本 v0.1.0）

Symphony 是一个基于 **pi agent** 的 SOP（标准作业流程）多智能体任务编排器：
你用 YAML 定义一条 `A → B → C` 的节点流水线，每个节点跑 **pi 的完整 Agent 能力**
（加载 Skill、循环调用大模型、执行工具），上一个节点的输出作为下一个节点的输入。

- **TUI**：pi 原生对话终端 + 一个 Symphony 专属斜杠命令 `/sop`。
- **Web**：SOP 看板，展示所有任务/节点的完整记录。
- **存储**：已完全移除 SQLite，改用**本地追加式日志**。TUI 与 Web 共享同一份
  本地日志（单一事实来源），无需 WebSocket 同步。

> ⚠️ **安全须知**：Web 服务需要监听端口，**必须在你自己的机器上启动**。
> 请勿在任何禁止起监听进程的沙箱环境里运行 `symphony`（不加 `--tui-only` 时会起 Web 服务）。

---

## 一、环境要求

| 组件 | 版本要求 | 用途 |
|---|---|---|
| Python | **≥ 3.11** | Symphony 后端（FastAPI / asyncio） |
| Node.js | **≥ 18**（建议 20+） | 构建 pi（rpc 模式所需的 `dist/cli.js`） |
| uv | 最新版 | Python 依赖管理与命令安装（`install.sh` 会自动安装） |
| 一个 OpenAI 兼容 API | — | 提供大模型能力（已内置火山引擎，开箱即用） |

检查环境：

```bash
python3 --version   # >= 3.11
node --version      # >= 18
```

---

## 二、一键安装（推荐）

解压后进入目录，运行安装脚本即可：

```bash
unzip symphony-final.zip -d symphony-final
cd symphony-final
bash install.sh
```

`install.sh` 会依次完成：

1. 定位项目根目录
2. 检测 / 安装 uv（若缺失，自动装到 `~/.local/bin`）
3. 构建 pi（`cd pi-agent && npm install --ignore-scripts && npm run build`，产出 rpc 模式所需的 `dist/cli.js`）
4. `uv tool install --editable .` 把 `symphony` 命令装进 PATH
5. 确保 `~/.local/bin` 在 PATH 中

完成后，在任意位置直接输入：

```bash
symphony
```

即可启动（TUI + Web）。**无需再传任何 `--provider-*` 参数**——`data/config.toml`
已内置火山引擎（Volcengine Ark）provider，开箱即用。

> 若在其它目录运行，需带上数据目录：
> `symphony --data-dir "<解压路径>/symphony-final/data"`

---

## 三、目录结构

```
symphony-final/
├── install.sh            # 一键安装脚本
├── symphony/             # Python 后端（CLI、SOP 执行器、pi 桥接、Web 服务）
│   └── tui/native_tui.py #   原生 pi TUI（pi 原生对话 + /sop）
├── pi-agent/             # pi agent 源码（install.sh 会构建出 dist/cli.js）
├── data/
│   ├── config.toml       #   配置（已内置火山引擎 provider）
│   ├── logs/*.jsonl      #   事件日志（每个任务一个 JSONL，单一事实来源）
│   ├── tasks/*.json      #   任务元数据（每个任务一个 JSON）
│   └── sop_templates/
│       └── code_review.yaml   # 示例 SOP：analyze → review → report
├── tests/                # 124 个测试
├── pyproject.toml
└── INSTALL.md            # 本文件
```

---

## 四、手动安装（如需分步）

如果不想用 `install.sh`，可手动执行：

### 4.1 构建 pi（**最关键的一步**）

Symphony 通过 pi 的 **rpc 模式**（`pi --mode rpc`）驱动 pi 的完整 Agent 能力。
rpc 模式只有 pi 的**构建产物** `dist/cli.js` 支持，压缩包里**不含** `dist/`（它是构建产物），
因此必须先构建一次：

pi 是一个 **npm workspaces monorepo**，必须在**仓库根目录**（`pi-agent/`）安装依赖并**整体构建**（内部会按 `tui → ai → agent → coding-agent → orchestrator` 顺序编译；单独进 `coding-agent` 构建会因缺少工作区依赖而失败）：

```bash
cd pi-agent
npm install --ignore-scripts   # --ignore-scripts 跳过根仓库的 husky prepare 钩子
npm run build                  # 按依赖顺序整体构建，产出 coding-agent/dist/cli.js
cd ..                          # 回到项目根目录
ls pi-agent/packages/coding-agent/dist/cli.js   # 确认存在
```

> 💡 **为什么要加 `--ignore-scripts`？**
> pi 仓库根 `package.json` 里有 `"prepare": "husky"` 生命周期脚本，仅用于开发时的 git 钩子。
> 在终端用户机器（非 git 仓库、没装 husky）上直接 `npm install` 会触发它并报
> `sh: husky: command not found`（`npm error code 127`）。`--ignore-scripts` 跳过该钩子，
> 但不影响随后显式的 `npm run build`。

> ⚠️ **如果跳过这一步会怎样？**
> 节点会退化为「单次大模型调用」，**无法执行 Skill、无法循环调用工具**。务必先构建 pi。
> （Symphony 会自动探测 `dist/cli.js`；探测不到时才回退到 `pi-test.sh`，
> 而 `pi-test.sh` 不支持 rpc，启动时会打印醒目告警。）

### 4.2 安装 symphony 命令

```bash
uv tool install --editable .     # 或：pip install -e .
```

---

## 五、启动

### 5.1 默认模式：TUI + Web 一起启动（推荐）

**不传 `--tui-only` / `--web-only` 时两者同时启动**，终端进入 TUI，
浏览器打开 `http://localhost:8080` 即可看到 Web 看板。

```bash
symphony
```

开箱即用（读取 `data/config.toml` 内置的火山引擎 provider）。启动后：

- **终端**：进入 TUI 交互界面（pi 原生对话 + `/sop`）
- **浏览器**：手动打开 <http://localhost:8080>

### 5.2 只启动 Web

```bash
symphony --web-only
```

### 5.3 只启动 TUI（不起 Web 服务）

```bash
symphony --tui-only
```

---

## 六、TUI 用法（原生 pi + /sop）

TUI 是 pi 原生 agent 的交互终端，外加一个 Symphony 专属斜杠命令 `/sop`：

| 输入 | 作用 |
|------|------|
| `<直接输入>` | 与 pi 原生对话（正常会话）。默认也会写入本地日志（`log_normal_chat = true`），便于日后在 Web 端分析 |
| `/sop <name> [k=v ...] [自然语言]` | 在后端运行一条 SOP，Web 端可看到全部节点记录 |
| `/sops` | 列出可用 SOP 模板 |
| `/tasks` | 列出最近任务 |
| `/help` | 帮助 |
| `/quit` | 退出 |

示例：

```
你 > /sop code-review repo_path=/path/to/repo 请重点看安全问题
```

从 TUI 发起的 SOP 在后端执行并写入共享日志，因此 Web 看板可完整看到每个节点的进度与输出。

---

## 七、更换大模型提供商

默认已内置火山引擎（Volcengine Ark）。如需切换，编辑 `data/config.toml`
的 `[provider]` 段，或用命令行参数临时覆盖：

```bash
# DeepSeek
symphony --provider-url https://api.deepseek.com/v1 --provider-key 你的KEY --provider-model deepseek-chat

# OpenAI
symphony --provider-url https://api.openai.com/v1 --provider-key 你的KEY --provider-model gpt-4o
```

也可用环境变量：

```bash
export SYMPHONY_PROVIDER_URL=https://api.deepseek.com/v1
export SYMPHONY_PROVIDER_KEY=你的KEY
export SYMPHONY_PROVIDER_MODEL=deepseek-chat
symphony
```

`data/config.toml` 也支持通过 `active_provider` 在多个命名 provider 间切换
（见文件内 `[providers.*]` 示例）。

---

## 八、完整命令行参数

| 参数 | 默认值 | 说明 |
|---|---|---|
| `--tui-only` | 关 | 只启动 TUI |
| `--web-only` | 关 | 只启动 Web 服务 |
| `--host` | `0.0.0.0` | Web 监听地址 |
| `--port` | `8080` | Web 监听端口 |
| `--pi-binary` | 自动探测 | pi 可执行文件路径（默认自动探测 `dist/cli.js`） |
| `--pi-model` | 无 | 覆盖 pi 使用的模型 |
| `--provider-type` | `openai` | 提供商类型：`openai` 或 `mira` |
| `--provider-url` | — | API Base URL（默认读配置文件） |
| `--provider-key` | — | API Key（默认读配置文件） |
| `--provider-model` | `doubao-1.5-pro-32k` | 模型 ID |
| `--active-provider` | 无 | 使用哪个命名 provider |
| `--data-dir` | `data` | 数据目录（SOP 模板、事件日志、配置） |
| `--log-level` | `INFO` | 日志级别 |

对应环境变量：`SYMPHONY_PROVIDER_TYPE` / `SYMPHONY_PROVIDER_URL` /
`SYMPHONY_PROVIDER_KEY` / `SYMPHONY_PROVIDER_MODEL`。

---

## 九、SOP 定义速览

示例见 `data/sop_templates/code_review.yaml`，它定义了一条
`analyze → review → report` 的链：

```yaml
name: code-review
version: "1.0"
description: "Standard code review workflow: analyze -> review -> report"

nodes:
  - id: analyze
    name: "Code Analysis"
    skill: "code-analysis"
    executor: pi            # 默认就是 pi：跑完整 Agent 循环（加载 skill + 循环 LLM + 执行工具）
    input_schema:           # 节点输入契约（JSON Schema）
      type: object
      properties: { repo_path: { type: string } }
    output_schema:          # 节点输出契约；跑完后按此校验，作为结构化结果传给下游
      type: object
      properties: { changed_files: { type: array } }
    retry: { max_attempts: 3, backoff: exponential }
    timeout: 300

  - id: review
    skill: "code-review"
    depends_on: [analyze]   # review 依赖 analyze 的输出
    human_intervention: true # 该节点执行前会暂停，等待人工放行
    retry: { max_attempts: 2, backoff: fixed }
    timeout: 600

  - id: report
    skill: "report-generation"
    depends_on: [review]
    timeout: 120
```

关键字段：

- **`executor`**：`pi`（默认，完整 Agent 能力）/ `llm`（单次大模型）/ `auto`（有 pi 用 pi，否则用 LLM）。
- **`skill`**：节点要调用的 pi Skill 名。Symphony 用 pi 原生 `/skill:<name>` 指令确定性触发该技能。
- **`depends_on`**：声明依赖，决定 A→B→C 的执行顺序；上游输出自动作为下游输入。
- **`input_schema` / `output_schema`**：JSON Schema 契约。输出跑完后校验、输入运行前校验，
  校验失败按 `retry` 重试，仍失败则节点 FAILED。
- **`human_intervention: true`**：该节点执行时暂停，等待在 Web 上人工放行。
- **`retry` / `timeout`**：重试策略与超时。

把你自己的 SOP YAML 放到 `data/sop_templates/` 目录即可被加载。

---

## 十、核心行为说明

- **节点默认 `executor: pi`**：每个节点跑 pi 的完整 Agent 循环——加载 Skill、
  循环调用大模型、执行工具，而非单次问答。这就是「Agent 能力全部来自 pi」。
- **技能触发是确定性的**：节点通过 pi 原生 `/skill:<name>` 指令触发，会完整加载技能定义
  （system prompt + 工具 + 资源），不依赖不可靠的自然语言提示。
- **输入/输出契约校验已生效**：A 跑完后其输出按 `output_schema` 校验，并作为结构化 payload
  传给 B；B 运行前按 `input_schema` 校验上游产物；校验失败按 `retry` 重试。
- **本地日志是单一事实来源**：所有事件写入 `data/logs/<task_id>.jsonl`，任务元数据写入
  `data/tasks/<task_id>.json`。TUI 与 Web 都读写同一份日志目录，无需 WebSocket 同步。
- **正常会话也写日志**：TUI 的普通 pi 对话默认也会记录为单节点任务（`log_normal_chat = true`），
  便于日后在 Web 端查看与分析。可在 `data/config.toml` 的 `[tui]` 段关闭。
- **任务级控制已实现**：暂停 / 恢复 / 取消（`pause` / `resume` / `cancel`）。
- **节点级「打断并重来」已实现**：在 Web 任务详情页每个节点卡片上点 `↻ Redo`，可附带一条
  额外指令，即可**中断该节点并整节点重跑**，其**下游节点自动级联重跑**。
- **方案A · 一切皆任务**：Web「新建任务」里可以**只填一个问题**（不选 SOP 模板）就直接创建并自动
  开跑——合成为一个单节点的隐式 SOP（Q&A 任务）。在任务详情页底部输入框继续追问，会作为
  **多轮对话**追加一个依赖上一轮的新节点（`turn-2` → `turn-1`）。

---

## 十一、运行测试（可选，验证安装完整）

```bash
uv run pytest tests/ -q      # 或：python3 -m pytest tests/ -q
```

预期：**124 个测试全部通过**。

---

## 十二、常见问题

**Q：启动时看到「Using pi-test.sh, which does NOT support `--mode rpc`」告警？**
说明没探测到 `dist/cli.js`。请回到第四步构建 pi（或重新运行 `install.sh`），
Symphony 会自动探测 `pi-agent/packages/coding-agent/dist/cli.js`。

**Q：`symphony` 命令找不到？**
确保 `~/.local/bin` 在 PATH 中。`install.sh` 会自动写入 `~/.bashrc` / `~/.zshrc`，
执行 `source ~/.bashrc` 或重开终端即可。

**Q：TUI 报找不到 Node.js / npx？**
安装 Node.js（<https://nodejs.org>）并确保在 PATH 中，然后重新构建 pi。

**Q：数据/日志存在哪里？**
默认在 `data/` 下：事件日志 `data/logs/*.jsonl`、任务元数据 `data/tasks/*.json`、
SOP 模板 `data/sop_templates/*`。已不再使用 SQLite。

**Q：LLM provider not configured 提示？**
表示没配直连 LLM，会走 pi 桥接。只要 pi 构建好并配了 provider，节点即可正常执行。
