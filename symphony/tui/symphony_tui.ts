#!/usr/bin/env node
import { spawnSync } from "node:child_process";
import * as os from "node:os";
import * as readline from "node:readline/promises";
import { stdin as input, stdout as output } from "node:process";

type Json = Record<string, any>;

const ANSI = {
	reset: "\x1b[0m",
	bold: "\x1b[1m",
	cyan: "\x1b[36m",
	blue: "\x1b[34m",
	magenta: "\x1b[35m",
	green: "\x1b[32m",
	yellow: "\x1b[33m",
	red: "\x1b[31m",
	gray: "\x1b[90m",
};

function envValue(...names: string[]): string | undefined {
	for (const name of names) {
		const value = process.env[name]?.trim();
		if (value) return value;
	}
	return undefined;
}

const WELCOME_CONFIG = {
	productName: envValue("ADSEEK_PRODUCT_NAME", "SYMPHONY_PRODUCT_NAME") ?? "ADSEEK CODE",
	slogan: envValue("ADSEEK_SLOGAN", "SYMPHONY_SLOGAN") ?? "Stand a little taller",
	piMark: envValue("ADSEEK_PI_MARK", "SYMPHONY_PI_MARK") ?? "𝜋",
	missionTitle: envValue("ADSEEK_MISSION_TITLE", "SYMPHONY_MISSION_TITLE") ?? "Adseek Mission Control",
	readyHint: envValue("ADSEEK_READY_HINT", "SYMPHONY_READY_HINT") ?? "所有普通执行优先走 Mira；Web 保留完整任务记录",
	skillHint: envValue("ADSEEK_SKILL_HINT", "SYMPHONY_SKILL_HINT") ?? "/skills 查看当前加载能力，/sops 查看编排模板",
	capabilityHint: envValue("ADSEEK_CAPABILITY_HINT", "SYMPHONY_CAPABILITY_HINT") ?? "adseek-hive-explorer / adseek-dorado-devops 已就绪",
};

function color(text: string, ...codes: string[]): string {
	if (!process.stdout.isTTY) return text;
	return `${codes.join("")}${text}${ANSI.reset}`;
}

function stripAnsi(text: string): string {
	return text.replace(/\x1b\[[0-9;]*m/g, "");
}

function visualLength(text: string): number {
	let width = 0;
	for (const ch of stripAnsi(text)) {
		const code = ch.codePointAt(0) ?? 0;
		width += code >= 0x1100 && (
			code <= 0x115f ||
			code === 0x2329 || code === 0x232a ||
			(code >= 0x2e80 && code <= 0xa4cf) ||
			(code >= 0xac00 && code <= 0xd7a3) ||
			(code >= 0xf900 && code <= 0xfaff) ||
			(code >= 0xfe10 && code <= 0xfe19) ||
			(code >= 0xfe30 && code <= 0xfe6f) ||
			(code >= 0xff00 && code <= 0xff60) ||
			(code >= 0xffe0 && code <= 0xffe6)
		) ? 2 : 1;
	}
	return width;
}

function padRightAnsi(text: string, width: number): string {
	return text + " ".repeat(Math.max(0, width - visualLength(text)));
}

function centerAnsi(text: string, width: number): string {
	const len = visualLength(text);
	const left = Math.max(0, Math.floor((width - len) / 2));
	return " ".repeat(left) + text + " ".repeat(Math.max(0, width - len - left));
}

function truncatePlain(text: string, width: number): string {
	if ([...text].length <= width) return text;
	return `${[...text].slice(0, Math.max(0, width - 1)).join("")}…`;
}

function compactPath(path: string, width: number): string {
	const home = os.homedir();
	const normalized = path.startsWith(home) ? `~${path.slice(home.length)}` : path;
	return truncatePlain(normalized, width);
}

function parseArgs(): { server: string; webUrl: string; model: string } {
	let server = process.env.SYMPHONY_SERVER ?? "http://localhost:8080";
	let webUrl = process.env.SYMPHONY_WEB_URL ?? server;
	let model = process.env.SYMPHONY_MODEL ?? "";
	for (let i = 2; i < process.argv.length; i++) {
		const arg = process.argv[i];
		if (arg === "--server" && process.argv[i + 1]) server = process.argv[++i];
		else if (arg === "--web-url" && process.argv[i + 1]) webUrl = process.argv[++i];
		else if (arg === "--model" && process.argv[i + 1]) model = process.argv[++i];
	}
	return { server: server.replace(/\/$/, ""), webUrl: webUrl.replace(/\/$/, ""), model };
}

const HELP = `${WELCOME_CONFIG.productName} TS TUI — 命令：
  <直接输入>                  对话（记录为 Web 可追踪任务）

  Pi 原生能力：
  /status                     查看 pi session / model / context 文件
  /model [provider/model]     查看或切换模型；也支持 /model <keyword>
  /models [keyword]           列出可用模型
  /thinking <level>           设置 thinking: off/minimal/low/medium/high/xhigh
  /cycle-thinking             循环切换 thinking level
  /compact [instructions]     手动压缩 pi 上下文
  /auto-compact on|off        开关 pi 自动上下文压缩
  /new                        开启新的 pi session
  /stats                      查看 pi session 统计
  /export [path]              导出 pi session HTML
  /copy                       复制最后一条 pi assistant 消息
  /cycle-model                循环切换模型
  /commands                   列出 pi 侧 slash/prompt/extension/skill 命令
  /skills                     列出 pi skills
  /bash <command>             通过 pi 执行 bash，并进入上下文

  Symphony 增强能力：
  /sop <name> [k=v ...]       运行 SOP（Web 可看到节点级记录）
  /sops                       列出 SOP 模板
  /tasks                      列出最近任务
  /task <task_id>             打印任务详情链接
  /cancel                     中断当前运行任务

  /help                       显示本帮助
  /refresh                    刷新 TUI 补全缓存
  Ctrl+C                      运行中：中断当前任务；空闲中：退出
  /quit                       退出`;

type CommandSpec = {
	name: string;
	aliases?: string[];
	description: string;
	run: (rest: string) => Promise<boolean> | boolean;
};

class ApiClient {
	constructor(private readonly base: string) {}

	async get(path: string): Promise<any> {
		return this.request("GET", path);
	}

	async post(path: string, body: Json = {}): Promise<any> {
		return this.request("POST", path, body);
	}

	private async request(method: string, path: string, body?: Json): Promise<any> {
		const res = await fetch(`${this.base}${path}`, {
			method,
			headers: body ? { "content-type": "application/json" } : undefined,
			body: body ? JSON.stringify(body) : undefined,
		});
		const text = await res.text();
		let data: any = text;
		try {
			data = text ? JSON.parse(text) : {};
		} catch {}
		if (!res.ok) throw new Error(typeof data?.detail === "string" ? data.detail : `${res.status} ${res.statusText}`);
		if (data && typeof data === "object" && data.error) throw new Error(String(data.error));
		return data;
	}
}

function modelLabel(model: any): string {
	if (!model || typeof model !== "object") return "unknown";
	const provider = model.provider ?? model.api ?? "unknown";
	const id = model.id ?? model.model ?? model.name ?? "unknown";
	const thinking = model.thinkingLevel ?? model.thinking;
	return `${provider}/${id}${thinking ? `:${thinking}` : ""}`;
}

function oneLine(value: any, limit = 220): string {
	const text = typeof value === "string" ? value : JSON.stringify(value);
	const compact = text.replace(/\s+/g, " ").trim();
	return compact.length > limit ? `${compact.slice(0, limit - 1)}…` : compact;
}

function parseData(data: any): any {
	if (typeof data !== "string") return data ?? {};
	try {
		return JSON.parse(data);
	} catch {
		return data;
	}
}

function renderMarkdownLite(text: string): string {
	return text
		.replace(/`([^`]+)`/g, (_, c) => color(c, ANSI.cyan))
		.replace(/\*\*([^*]+)\*\*/g, (_, c) => color(c, ANSI.bold));
}

function copyToClipboard(text: string): boolean {
	const candidates = process.platform === "darwin" ? ["pbcopy"] : ["wl-copy", "xclip"];
	for (const command of candidates) {
		const args = command === "xclip" ? ["-selection", "clipboard"] : [];
		const result = spawnSync(command, args, { input: text, stdio: ["pipe", "ignore", "ignore"] });
		if (result.status === 0) return true;
	}
	return false;
}

function parseSopArgs(rest: string): { name: string; inputs: Json; prompt: string } {
	const tokens = rest.match(/(?:[^\s"']+|"[^"]*"|'[^']*')+/g)?.map((s) => s.replace(/^['"]|['"]$/g, "")) ?? [];
	const name = tokens.shift() ?? "";
	const inputs: Json = {};
	const free: string[] = [];
	for (const token of tokens) {
		const idx = token.indexOf("=");
		if (idx > 0) inputs[token.slice(0, idx)] = token.slice(idx + 1);
		else free.push(token);
	}
	return { name, inputs, prompt: free.join(" ") };
}

function buildWelcomeBanner(opts: { model: string; webUrl: string; server: string }): string {
	const termWidth = process.stdout.columns || 120;
	const width = Math.max(84, Math.min(122, termWidth - 2));
	const inner = width - 2;
	const left = Math.max(32, Math.min(42, Math.floor(inner * 0.38)));
	const right = inner - left - 3;
	const user = os.userInfo().username || "operator";
	const cwd = compactPath(process.cwd(), left - 4);
	const model = truncatePlain(opts.model || "mira / re-o-48", left - 11);
	const web = truncatePlain(`${opts.webUrl}/#/tasks`, right - 10);
	const api = truncatePlain(opts.server, right - 9);
	const skillHint = truncatePlain(WELCOME_CONFIG.skillHint, right - 4);
	const statusHint = truncatePlain(WELCOME_CONFIG.readyHint, right - 4);
	const adseekHint = truncatePlain(WELCOME_CONFIG.capabilityHint, right - 4);
	const launchHint = truncatePlain("启动入口：symphony / Symphony / adseek / Adseek", right - 4);
	const product = truncatePlain(`✦ ${WELCOME_CONFIG.productName} ✦`, left);
	const slogan = truncatePlain(WELCOME_CONFIG.slogan, left);

	const logo = [
		centerAnsi(color(product, ANSI.bold, ANSI.cyan), left),
		centerAnsi(color("          |\\", ANSI.cyan), left),
		centerAnsi(color("          | \\", ANSI.cyan), left),
		centerAnsi(color("          |  \\", ANSI.cyan), left),
		centerAnsi(color("          |   \\", ANSI.cyan), left),
		centerAnsi(color("          |    \\", ANSI.cyan), left),
		centerAnsi(color("      ____|_____\\_", ANSI.cyan), left),
		centerAnsi(color("      \\          /", ANSI.magenta), left),
		centerAnsi(color("       \\________/", ANSI.magenta), left),
		centerAnsi(color("~~~~~~~~~~~~~~~~~~~~~~~~", ANSI.blue), left),
		centerAnsi(color(slogan, ANSI.bold, ANSI.magenta), left),
		"",
		color(` Welcome back, ${user}`, ANSI.bold, ANSI.green),
		color(` Model: ${model}`, ANSI.yellow),
		color(` ${cwd}`, ANSI.gray),
	];
	const side = [
		color(WELCOME_CONFIG.missionTitle, ANSI.bold, ANSI.magenta),
		`${color("Web", ANSI.cyan)}: ${web}`,
		`${color("API", ANSI.cyan)}: ${api}`,
		color("─".repeat(Math.min(58, right)), ANSI.gray),
		color("Ready", ANSI.bold, ANSI.green),
		`• ${statusHint}`,
		`• ${skillHint}`,
		`• ${adseekHint}`,
		`• ${launchHint}`,
		"",
		color("提示：输入 /help 查看全部命令，Ctrl+C 可中断任务", ANSI.gray),
	];

	const top = color(`╭${"─".repeat(inner)}╮`, ANSI.cyan);
	const bottom = color(`╰${"─".repeat(inner)}╯`, ANSI.cyan);
	const edge = color("│", ANSI.cyan);
	const middle = color("│", ANSI.gray);
	const lines = [top];
	for (let i = 0; i < Math.max(logo.length, side.length); i++) {
		const l = padRightAnsi(logo[i] ?? "", left);
		const r = padRightAnsi(side[i] ?? "", right);
		lines.push(`${edge} ${l} ${middle} ${r} ${edge}`);
	}
	lines.push(bottom);
	return lines.join("\n");
}

class SymphonyTui {
	private rl: any;
	private activeTaskId = "";
	private shuttingDown = false;
	private commandSpecs: CommandSpec[] = [];
	private commandNames: string[] = [];
	private sopNames: string[] = [];
	private modelNames: string[] = [];

	constructor(
		private readonly api: ApiClient,
		private readonly server: string,
		private readonly webUrl: string,
		private readonly initialModel: string,
	) {
		this.commandSpecs = this.createCommandSpecs();
		this.commandNames = this.commandSpecs.flatMap((c) => [c.name, ...(c.aliases ?? [])]).sort();
		this.rl = readline.createInterface({
			input,
			output,
			completer: (line: string) => this.complete(line),
		});
	}

	async run(): Promise<void> {
		this.installSignalHandlers();
		await this.refreshCompletionCaches(false);
		console.log(buildWelcomeBanner({ model: this.initialModel, webUrl: this.webUrl, server: this.server }));
		console.log(color("输入 /help 查看命令，直接输入问题开始对话。", ANSI.gray));

		while (!this.shuttingDown) {
			let line: string;
			try {
				line = (await this.rl.question("\n你 > ")).trim();
			} catch {
				break;
			}
			if (!line) continue;
			try {
				if (line.startsWith("/")) {
					const keepGoing = await this.handleCommand(line);
					if (!keepGoing) break;
				} else {
					await this.ask(line);
				}
			} catch (error) {
				console.log(color(`错误: ${(error as Error).message}`, ANSI.red));
			}
		}
		this.rl.close();
	}

	private installSignalHandlers(): void {
		process.on("SIGINT", async () => {
			if (this.activeTaskId) {
				console.log("\n正在中断当前任务…");
				await this.cancelActiveTask();
				return;
			}
			console.log("\n再见。");
			this.shuttingDown = true;
			this.rl.close();
		});
	}

	private createCommandSpecs(): CommandSpec[] {
		return [
			{ name: "quit", aliases: ["exit", "q"], description: "退出", run: () => false },
			{ name: "help", aliases: ["h", "?"], description: "显示帮助", run: () => this.print(HELP) },
			{ name: "status", aliases: ["session", "info"], description: "查看 pi session/model/context", run: () => this.status() },
			{ name: "model", description: "查看或切换模型", run: (rest) => this.model(rest) },
			{ name: "models", description: "列出模型", run: (rest) => this.models(rest) },
			{ name: "cycle-model", description: "循环切换模型", run: () => this.cycleModel() },
			{ name: "thinking", description: "设置 thinking level", run: (rest) => this.thinking(rest) },
			{ name: "cycle-thinking", description: "循环切换 thinking level", run: () => this.cycleThinking() },
			{ name: "compact", description: "手动压缩上下文", run: (rest) => this.compact(rest) },
			{ name: "auto-compact", description: "开关自动压缩", run: (rest) => this.autoCompact(rest) },
			{ name: "stats", description: "查看 pi session 统计", run: () => this.stats() },
			{ name: "export", description: "导出 pi session HTML", run: (rest) => this.exportHtml(rest) },
			{ name: "copy", description: "复制最后一条 assistant 消息", run: () => this.copyLastAssistant() },
			{ name: "commands", aliases: ["cmds"], description: "列出 pi 命令", run: () => this.commands() },
			{ name: "skills", description: "列出 pi skills", run: () => this.skills() },
			{ name: "new", aliases: ["new-session"], description: "新建 pi session", run: () => this.newSession() },
			{ name: "bash", description: "通过 pi 执行 bash", run: (rest) => this.bash(rest) },
			{ name: "sops", description: "列出 SOP", run: () => this.sops() },
			{ name: "sop", description: "运行 SOP", run: (rest) => this.sop(rest) },
			{ name: "tasks", description: "列出任务", run: () => this.tasks() },
			{ name: "task", description: "查看任务", run: (rest) => this.task(rest) },
			{ name: "cancel", description: "中断当前任务", run: () => this.cancelActiveTask() },
			{ name: "refresh", description: "刷新补全缓存", run: () => this.refreshCompletionCaches(true) },
		];
	}

	private complete(line: string): [string[], string] {
		if (!line.startsWith("/")) return [[], line];
		const parts = line.slice(1).split(/\s+/);
		const command = parts[0] ?? "";
		if (!line.includes(" ")) {
			const hits = this.commandNames.filter((name) => name.startsWith(command)).map((name) => `/${name}`);
			return [hits.length ? hits : this.commandNames.map((name) => `/${name}`), line];
		}
		const last = parts.at(-1) ?? "";
		if (command === "sop") {
			const hits = this.sopNames.filter((name) => name.startsWith(last));
			return [hits.length ? hits : this.sopNames, last];
		}
		if (command === "model") {
			const hits = this.modelNames.filter((name) => name.toLowerCase().includes(last.toLowerCase()));
			return [hits.length ? hits : this.modelNames.slice(0, 30), last];
		}
		if (command === "thinking") {
			const levels = ["off", "minimal", "low", "medium", "high", "xhigh"];
			return [levels.filter((level) => level.startsWith(last)), last];
		}
		if (command === "auto-compact") return [["on", "off"].filter((v) => v.startsWith(last)), last];
		return [[], last];
	}

	private async refreshCompletionCaches(verbose: boolean): Promise<true> {
		try {
			const [sops, models] = await Promise.allSettled([this.api.get("/api/sop"), this.api.get("/api/pi/models")]);
			if (sops.status === "fulfilled" && Array.isArray(sops.value)) {
				this.sopNames = sops.value.map((s: any) => String(s.name)).filter(Boolean).sort();
			}
			if (models.status === "fulfilled" && Array.isArray(models.value.models)) {
				this.modelNames = models.value.models.map(modelLabel).filter((s: string) => s !== "unknown").sort();
			}
			if (verbose) console.log(`补全缓存已刷新：commands=${this.commandNames.length}, sops=${this.sopNames.length}, models=${this.modelNames.length}`);
		} catch (error) {
			if (verbose) console.log(color(`刷新补全缓存失败: ${(error as Error).message}`, ANSI.red));
		}
		return true;
	}

	private async handleCommand(line: string): Promise<boolean> {
		const [raw, ...restParts] = line.slice(1).split(/\s+/);
		const cmd = raw.toLowerCase();
		const rest = restParts.join(" ").trim();
		const spec = this.commandSpecs.find((c) => c.name === cmd || c.aliases?.includes(cmd));
		if (spec) return await spec.run(rest);
		console.log(`未知命令: /${cmd}（输入 /help 查看）`);
		return true;
	}

	private print(text: string): true {
		console.log(text);
		return true;
	}

	private async status(): Promise<true> {
		const [state, context] = await Promise.all([this.api.get("/api/pi/state"), this.api.get("/api/pi/context")]);
		console.log("当前状态：");
		console.log(`  model: ${modelLabel(state.model)}`);
		console.log(`  thinking: ${state.thinkingLevel ?? "unknown"}`);
		console.log(`  streaming: ${Boolean(state.isStreaming)}`);
		console.log(`  compacting: ${Boolean(state.isCompacting)}`);
		console.log(`  auto_compaction: ${Boolean(state.autoCompactionEnabled)}`);
		console.log(`  session: ${state.sessionName ?? state.sessionId ?? "unknown"}`);
		if (state.sessionFile) console.log(`  session_file: ${state.sessionFile}`);
		console.log(`  pi_cwd: ${context.cwd ?? "unknown"}`);
		if (Array.isArray(context.context_files) && context.context_files.length > 0) {
			console.log("  context_files:");
			for (const info of context.context_files) {
				console.log(`    - ${info.path} sha256=${info.sha256_short ?? "?"} bytes=${info.bytes ?? "?"}`);
			}
		}
		return true;
	}

	private async model(rest: string): Promise<true> {
		if (!rest) {
			await this.status();
			console.log("用法: /model <provider/model> 或 /model <keyword>");
			return true;
		}
		const [provider, modelId] = await this.resolveModel(rest);
		const data = await this.api.post("/api/pi/model", { provider, model_id: modelId });
		console.log(`已切换模型: ${modelLabel(data && Object.keys(data).length ? data : { provider, id: modelId })}`);
		return true;
	}

	private async resolveModel(query: string): Promise<[string, string]> {
		if (query.includes("/")) {
			const [provider, modelId] = query.split("/", 2);
			if (provider && modelId) return [provider, modelId];
		}
		const parts = query.split(/\s+/);
		if (parts.length === 2) return [parts[0], parts[1]];
		const data = await this.api.get("/api/pi/models");
		const models = Array.isArray(data.models) ? data.models : [];
		const needle = query.toLowerCase();
		const matches = models.filter((m: any) => modelLabel(m).toLowerCase().includes(needle));
		if (matches.length === 1) return [String(matches[0].provider), String(matches[0].id)];
		if (matches.length === 0) throw new Error(`找不到匹配模型: ${query}`);
		throw new Error(`模型匹配不唯一，请使用 provider/model。候选: ${matches.slice(0, 10).map(modelLabel).join(", ")}`);
	}

	private async models(rest: string): Promise<true> {
		const data = await this.api.get("/api/pi/models");
		let models = Array.isArray(data.models) ? data.models : [];
		const needle = rest.trim().toLowerCase();
		if (needle) models = models.filter((m: any) => modelLabel(m).toLowerCase().includes(needle));
		console.log(`可用模型（${models.length}）：`);
		for (const m of models.slice(0, 40)) {
			const ctx = m.contextWindow ?? m.context;
			console.log(`  - ${modelLabel(m)}${ctx ? `  ctx=${ctx}` : ""}${m.reasoning ? " reasoning" : ""}`);
		}
		if (models.length > 40) console.log(`  ... 还有 ${models.length - 40} 个，使用 /models <keyword> 过滤`);
		return true;
	}

	private async thinking(level: string): Promise<true> {
		const allowed = new Set(["off", "minimal", "low", "medium", "high", "xhigh"]);
		if (!allowed.has(level)) {
			console.log("用法: /thinking off|minimal|low|medium|high|xhigh");
			return true;
		}
		await this.api.post("/api/pi/thinking", { level });
		console.log(`已设置 thinking: ${level}`);
		return true;
	}

	private async cycleModel(): Promise<true> {
		const data = await this.api.post("/api/pi/model/cycle");
		const model = data?.model ?? data;
		console.log(`已切换模型: ${modelLabel(model)}`);
		await this.refreshCompletionCaches(false);
		return true;
	}

	private async cycleThinking(): Promise<true> {
		const data = await this.api.post("/api/pi/thinking/cycle");
		console.log(`已切换 thinking: ${data?.level ?? data?.thinkingLevel ?? oneLine(data)}`);
		return true;
	}

	private async compact(instructions: string): Promise<true> {
		const data = await this.api.post("/api/pi/compact", { instructions });
		console.log("上下文压缩完成。");
		if (data && Object.keys(data).length > 0) console.log(`  ${oneLine(data)}`);
		return true;
	}

	private async autoCompact(rest: string): Promise<true> {
		const value = rest.trim().toLowerCase();
		if (!["on", "off", "true", "false", "1", "0"].includes(value)) {
			console.log("用法: /auto-compact on|off");
			return true;
		}
		const enabled = ["on", "true", "1"].includes(value);
		await this.api.post("/api/pi/auto-compact", { enabled });
		console.log(`auto-compaction: ${enabled ? "on" : "off"}`);
		return true;
	}

	private async stats(): Promise<true> {
		const data = await this.api.get("/api/pi/session-stats");
		console.log("pi session stats：");
		for (const [key, value] of Object.entries(data)) console.log(`  ${key}: ${oneLine(value, 120)}`);
		return true;
	}

	private async exportHtml(path: string): Promise<true> {
		const data = await this.api.post("/api/pi/export-html", { output_path: path.trim() });
		console.log(`已导出: ${data.path ?? oneLine(data)}`);
		return true;
	}

	private async copyLastAssistant(): Promise<true> {
		const data = await this.api.get("/api/pi/last-assistant-text");
		const text = typeof data.text === "string" ? data.text : "";
		if (!text.trim()) {
			console.log("没有可复制的 assistant 消息。");
			return true;
		}
		const copied = copyToClipboard(text);
		if (copied) console.log(`已复制最后一条 assistant 消息（${text.length} chars）。`);
		else console.log(`当前环境没有可用剪贴板命令，内容如下：\n${text}`);
		return true;
	}

	private async commands(): Promise<true> {
		const data = await this.api.get("/api/pi/commands");
		const commands = Array.isArray(data.commands) ? data.commands : [];
		console.log(`pi 命令（${commands.length}）：`);
		for (const c of commands.slice(0, 80)) console.log(`  /${c.name} [${c.source ?? "unknown"}]${c.description ? ` — ${c.description}` : ""}`);
		return true;
	}

	private async skills(): Promise<true> {
		const skills = await this.api.get("/api/skills");
		const list = Array.isArray(skills) ? skills : skills.skills ?? [];
		console.log(`pi skills（${list.length}）：`);
		for (const s of list) console.log(`  /skill:${s.name}${s.description ? ` — ${s.description}` : ""}`);
		return true;
	}

	private async newSession(): Promise<true> {
		const data = await this.api.post("/api/pi/new-session");
		console.log(data.cancelled ? "已取消创建新 session。" : "已创建新的 pi session。");
		return true;
	}

	private async bash(command: string): Promise<true> {
		if (!command) return this.print("用法: /bash <command>");
		const result = await this.api.post("/api/pi/bash", { command });
		if (result.exitCode && result.exitCode !== 0) console.log(`退出码: ${result.exitCode}`);
		const stdout = result.stdout ?? result.output ?? result.result;
		if (stdout) console.log(String(stdout).trimEnd());
		if (result.stderr) console.log(color(String(result.stderr).trimEnd(), ANSI.red));
		return true;
	}

	private async sops(): Promise<true> {
		const sops = await this.api.get("/api/sop");
		if (!Array.isArray(sops) || sops.length === 0) return this.print("（暂无 SOP 模板，可在 Web 的 #/sop 页面添加）");
		console.log("可用 SOP：");
		for (const s of sops) console.log(`  - ${s.name} (${s.node_count ?? s.nodes?.length ?? 0} nodes)${s.description ? ` — ${s.description}` : ""}`);
		return true;
	}

	private async tasks(): Promise<true> {
		const tasks = await this.api.get("/api/tasks");
		if (!Array.isArray(tasks) || tasks.length === 0) return this.print("（暂无任务）");
		console.log("最近任务：");
		for (const t of tasks.slice(0, 15)) console.log(`  ${String(t.task_id).slice(0, 8)}  ${String(t.status).padEnd(10)}  ${t.sop_name}`);
		return true;
	}

	private async task(taskId: string): Promise<true> {
		if (!taskId) return this.print("用法: /task <task_id>");
		const task = await this.api.get(`/api/tasks/${encodeURIComponent(taskId)}`);
		console.log(`任务: ${task.task_id}`);
		console.log(`  status: ${task.status}`);
		console.log(`  sop: ${task.sop_name}`);
		console.log(`  Web: ${this.webUrl}/#/tasks/${task.task_id}`);
		return true;
	}

	private async sop(rest: string): Promise<true> {
		const parsed = parseSopArgs(rest);
		if (!parsed.name) return this.print("用法: /sop <name> [key=value ...] [自然语言补充]");
		const result = await this.api.post("/api/tasks", {
			sop_name: parsed.name,
			prompt: parsed.prompt,
			inputs: parsed.inputs,
			metadata: { source: "ts-tui", inputs: parsed.inputs, prompt: parsed.prompt },
			auto_start: true,
		});
		console.log(`已创建任务 ${result.task_id}（SOP: ${parsed.name}）`);
		console.log(`Web 详情: ${this.webUrl}/#/tasks/${result.task_id}`);
		await this.watchTask(result.task_id);
		return true;
	}

	private async ask(prompt: string): Promise<void> {
		const result = await this.api.post("/api/ask", { prompt });
		console.log(`任务: ${result.task_id}  ${this.webUrl}/#/tasks/${result.task_id}`);
		await this.watchTask(result.task_id);
	}

	private async cancelActiveTask(): Promise<true> {
		if (!this.activeTaskId) {
			console.log("当前没有运行中的任务。");
			return true;
		}
		const taskId = this.activeTaskId;
		await this.api.post(`/api/tasks/${encodeURIComponent(taskId)}/cancel`);
		console.log(`已中断：${taskId}`);
		this.activeTaskId = "";
		return true;
	}

	private async watchTask(taskId: string): Promise<void> {
		this.activeTaskId = taskId;
		let afterSeq = 0;
		let fullText = "";
		let idlePolls = 0;
		let printedStillRunningHint = false;
		try {
			while (this.activeTaskId === taskId) {
				const events = await this.api.get(`/api/tasks/${encodeURIComponent(taskId)}/events?after_seq=${afterSeq}`);
				idlePolls = events.length ? 0 : idlePolls + 1;
				for (const event of events) {
					afterSeq = Math.max(afterSeq, event.seq ?? 0);
					const type = event.event_type;
					const data = parseData(event.data);
					if (type === "agent_message_delta") {
						const text = String(data.text ?? "");
						if (!text) continue;
						const delta = text.startsWith(fullText) ? text.slice(fullText.length) : text;
						fullText = data.replace ? text : fullText + text;
						if (delta) process.stdout.write(renderMarkdownLite(delta));
					} else if (type === "tool_call_start") {
						console.log(`\n  ${color("🔧 工具", ANSI.yellow)} ${data.tool_name ?? data.name ?? "tool"} ${data.arguments ? color("›", ANSI.gray) + " " + oneLine(data.arguments, 120) : ""}`);
					} else if (type === "tool_call_end") {
						console.log(`  ${color(data.is_error ? "⚠️ 失败" : "✅ 完成", data.is_error ? ANSI.red : ANSI.green)} ${data.tool_name ?? data.name ?? "tool"}`);
					} else if (type === "node_started") {
						console.log(`\n  ▶ ${event.node_id ?? "node"} 运行中…`);
					} else if (type === "node_completed") {
						console.log(`\n  ✔ ${event.node_id ?? "node"} 完成`);
					} else if (type === "node_failed") {
						console.log(`\n  ✖ ${event.node_id ?? "node"} 失败`);
					} else if (type === "human_intervention_required") {
						console.log(`\n  ⏸ ${event.node_id ?? "node"} 需要人工放行 → ${this.webUrl}/#/tasks/${taskId}`);
					}
				}
				const task = await this.api.get(`/api/tasks/${encodeURIComponent(taskId)}`);
				if (["completed", "failed", "cancelled"].includes(task.status)) {
					if (fullText) process.stdout.write("\n");
					console.log(`任务结束: ${task.status}`);
					break;
				}
				if (!printedStillRunningHint && idlePolls === 8) {
					printedStillRunningHint = true;
					process.stdout.write(color("\n  …仍在运行，可 Ctrl+C 或 /cancel 中断，Web 查看详情：", ANSI.gray));
					process.stdout.write(`${this.webUrl}/#/tasks/${taskId}\n`);
				}
				await new Promise((resolve) => setTimeout(resolve, 250));
			}
		} finally {
			if (this.activeTaskId === taskId) this.activeTaskId = "";
		}
	}
}

const { server, webUrl, model } = parseArgs();
const tui = new SymphonyTui(new ApiClient(server), server, webUrl, model);
tui.run().catch((error) => {
	console.error(color(`Symphony TS TUI 启动失败: ${error.message}`, ANSI.red));
	process.exit(1);
});
