import type {
	Api,
	AssistantMessage,
	Context,
	ImageContent,
	Message,
	Model,
	ProviderHeaders,
	SimpleStreamOptions,
	StreamFunction,
	StreamOptions,
	TextContent,
	ThinkingContent,
	Tool,
	ToolCall,
	ToolResultMessage,
	Usage,
} from "../types.ts";
import { AssistantMessageEventStream } from "../utils/event-stream.ts";
import { buildBaseOptions } from "./simple-options.ts";

export interface MiraOptions extends StreamOptions {}

type JsonRecord = Record<string, unknown>;

const MIRA_TOOLS = [
	{ name: "Web", id: 54604802835, scope: "GLOBAL" },
	{ name: "ByteDanceContext", id: 117073920019, scope: "GLOBAL" },
	{ name: "ImageRich", id: 54604820243, scope: "GLOBAL" },
];

class ControlTagFilter {
	private pending = "";
	private hiddenTag: string | undefined;
	private readonly maxOpenLen = Math.max(...["cis-ctrl", "cis-meta"].map((tag) => tag.length + 2));
	private readonly tags = ["cis-ctrl", "cis-meta"];

	push(chunk: string): string {
		let input = this.pending + chunk;
		this.pending = "";
		let output = "";

		while (input.length > 0) {
			if (this.hiddenTag) {
				const close = input.indexOf(`</${this.hiddenTag}>`);
				if (close < 0) return output;
				input = input.slice(close + this.hiddenTag.length + 3);
				this.hiddenTag = undefined;
				continue;
			}

			let earliest: { index: number; tag: string } | undefined;
			for (const tag of this.tags) {
				const index = input.indexOf(`<${tag}>`);
				if (index >= 0 && (!earliest || index < earliest.index)) earliest = { index, tag };
			}

			if (!earliest) {
				const [complete, pending] = this.splitTrailing(input);
				this.pending = pending;
				output += complete;
				break;
			}

			output += input.slice(0, earliest.index);
			input = input.slice(earliest.index + earliest.tag.length + 2);
			this.hiddenTag = earliest.tag;
		}

		return output;
	}

	flush(): string {
		const output = this.pending;
		this.pending = "";
		this.hiddenTag = undefined;
		return output;
	}

	private splitTrailing(input: string): [string, string] {
		const maxLen = Math.min(this.maxOpenLen - 1, input.length);
		for (let length = maxLen; length > 0; length--) {
			const suffix = input.slice(-length);
			if (this.tags.some((tag) => `<${tag}>`.startsWith(suffix))) {
				return [input.slice(0, -length), suffix];
			}
		}
		return [input, ""];
	}
}

class CumulativeDeltaTracker {
	private previous = "";

	push(text: string): string {
		if (!text) return "";
		if (text === this.previous) return "";
		if (text.startsWith(this.previous)) {
			const delta = text.slice(this.previous.length);
			this.previous = text;
			return delta;
		}
		this.previous = text;
		return text;
	}
}

export const stream: StreamFunction<"mira", MiraOptions> = (
	model: Model<"mira">,
	context: Context,
	options?: MiraOptions,
): AssistantMessageEventStream => {
	const events = new AssistantMessageEventStream();

	(async () => {
		const output = createAssistantMessage(model);
		try {
			const token = resolveMiraToken(options?.apiKey, options?.headers);
			const headers = createHeaders(token, options?.headers);
			const sessionId = await createSession(model, headers, context, options);
			const body = createCompletionBody(sessionId, model, context, options);
			const nextBody = await options?.onPayload?.(body, model);
			const response = await fetch(`${model.baseUrl.replace(/\/$/, "")}/mira/api/v1/chat/completion`, {
				method: "POST",
				headers,
				body: JSON.stringify(nextBody ?? body),
				signal: options?.signal,
			});
			await options?.onResponse?.({ status: response.status, headers: headersToRecord(response.headers) }, model);
			if (!response.ok || !response.body) {
				throw new Error(`Mira completion: HTTP ${response.status} ${(await response.text()).slice(0, 300)}`);
			}

			events.push({ type: "start", partial: output });
			await readCompletionStream(response, events, output);
			finishOpenText(events, output);
			finishOpenThinking(events, output);
			output.stopReason = output.content.some((block) => block.type === "toolCall") ? "toolUse" : "stop";
			events.push({ type: "done", reason: output.stopReason, message: output });
		} catch (error) {
			output.stopReason = isAbortError(error) ? "aborted" : "error";
			output.errorMessage = error instanceof Error ? error.message : String(error);
			events.push({ type: "error", reason: output.stopReason, error: output });
		}
	})();

	return events;
};

export const streamSimple: StreamFunction<"mira", SimpleStreamOptions> = (
	model: Model<"mira">,
	context: Context,
	options?: SimpleStreamOptions,
): AssistantMessageEventStream => stream(model, context, buildBaseOptions(model, context, options));

function createAssistantMessage(model: Model<Api>): AssistantMessage {
	return {
		role: "assistant",
		content: [],
		api: model.api,
		provider: model.provider,
		model: model.id,
		usage: emptyUsage(),
		stopReason: "stop",
		timestamp: Date.now(),
	};
}

function emptyUsage(): Usage {
	return {
		input: 0,
		output: 0,
		cacheRead: 0,
		cacheWrite: 0,
		totalTokens: 0,
		cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 },
	};
}

function resolveMiraToken(apiKey: string | undefined, headers: ProviderHeaders | undefined): string {
	if (apiKey?.trim()) return apiKey.trim();
	const jwt = findHeader(headers, "jwt-token");
	if (jwt) return jwt;
	const cookie = findHeader(headers, "cookie");
	const match = cookie?.match(/(?:^|;\s*)mira_session=([^;]+)/);
	if (match?.[1]) return match[1];
	throw new Error("No Mira session token configured");
}

function findHeader(headers: ProviderHeaders | undefined, name: string): string | undefined {
	if (!headers) return undefined;
	const expected = name.toLowerCase();
	for (const [key, value] of Object.entries(headers)) {
		if (key.toLowerCase() === expected && value) return value;
	}
	return undefined;
}

function createHeaders(token: string, extraHeaders: ProviderHeaders | undefined): Headers {
	const headers = new Headers();
	headers.set("content-type", "application/json");
	headers.set("Cookie", `mira_session=${token}`);
	headers.set("jwt-token", token);
	headers.set("x-mira-timezone", "Asia/Shanghai");
	if (extraHeaders) {
		for (const [key, value] of Object.entries(extraHeaders)) {
			if (value === null) headers.delete(key);
			else headers.set(key, value);
		}
	}
	return headers;
}

async function createSession(
	model: Model<Api>,
	headers: Headers,
	context: Context,
	options: MiraOptions | undefined,
): Promise<string> {
	const body = {
		sessionProperties: {
			topic: firstUserText(context) || "Pi task",
			dataSource: "manus",
			dataSources: ["manus"],
			model: model.id,
		},
	};
	const response = await fetch(`${model.baseUrl.replace(/\/$/, "")}/mira/api/v1/chat/create`, {
		method: "POST",
		headers,
		body: JSON.stringify(body),
		signal: options?.signal,
	});
	const text = await response.text();
	let data: JsonRecord;
	try {
		data = JSON.parse(text) as JsonRecord;
	} catch {
		throw new Error(`Mira create session: HTTP ${response.status}, body=${text.slice(0, 300)}`);
	}
	if (!response.ok) throw new Error(`Mira create session: HTTP ${response.status}, body=${text.slice(0, 300)}`);
	const code = data.code;
	const success = data.success;
	if ((code !== undefined && code !== 0) || (success !== undefined && success !== true)) {
		throw new Error(`Mira create session: ${JSON.stringify(data)}`);
	}
	const nestedData = getRecord(data.data);
	const sessionItem = getRecord(data.sessionItem) ?? getRecord(nestedData?.sessionItem) ?? nestedData;
	const sessionId = sessionItem?.sessionId ?? sessionItem?.session_id;
	if (typeof sessionId !== "string" || sessionId.length === 0) {
		throw new Error(`Mira create session: no sessionId in response ${JSON.stringify(data)}`);
	}
	return sessionId;
}

function createCompletionBody(
	sessionId: string,
	model: Model<Api>,
	context: Context,
	options: MiraOptions | undefined,
) {
	return {
		sessionId,
		content: flattenMessages(context),
		messageType: 1,
		summaryAgent: model.id,
		dataSources: ["manus"],
		comprehensive: 0,
		config: {
			online: true,
			mode: "quick",
			model: model.id,
			tool_list: createToolList(context.tools),
		},
		...(options?.temperature !== undefined ? { temperature: options.temperature } : {}),
		...(options?.maxTokens !== undefined ? { max_tokens: options.maxTokens } : {}),
	};
}

function createToolList(tools: Tool[] | undefined): { name: string; id: number; scope: string }[] {
	const list = [...MIRA_TOOLS];
	if (!tools) return list;
	for (const tool of tools) {
		if (!list.some((item) => item.name === tool.name)) list.push({ name: tool.name, id: 0, scope: "GLOBAL" });
	}
	return list;
}

async function readCompletionStream(
	response: Response,
	events: AssistantMessageEventStream,
	output: AssistantMessage,
): Promise<void> {
	const reader = response.body?.getReader();
	if (!reader) throw new Error("Mira completion: missing response body");
	const decoder = new TextDecoder();
	const contentFilter = new ControlTagFilter();
	const reasoningFilter = new ControlTagFilter();
	const contentDeltaTracker = new CumulativeDeltaTracker();
	let buffer = "";
	for (;;) {
		const { value, done } = await reader.read();
		if (done) break;
		buffer += decoder.decode(value, { stream: true });
		const parsed = parseSse(buffer);
		buffer = parsed.rest;
		for (const eventData of parsed.events) {
			const shouldStop = handleMiraEvent(
				eventData,
				events,
				output,
				contentFilter,
				reasoningFilter,
				contentDeltaTracker,
			);
			if (shouldStop) return;
		}
	}
	const trailingContent = contentFilter.flush();
	if (trailingContent) pushText(events, output, trailingContent);
	const trailingReasoning = reasoningFilter.flush();
	if (trailingReasoning) pushThinking(events, output, trailingReasoning);
}

function handleMiraEvent(
	eventData: string,
	events: AssistantMessageEventStream,
	output: AssistantMessage,
	contentFilter: ControlTagFilter,
	reasoningFilter: ControlTagFilter,
	contentDeltaTracker: CumulativeDeltaTracker,
): boolean {
	let outer: JsonRecord;
	try {
		outer = JSON.parse(eventData) as JsonRecord;
	} catch {
		return false;
	}
	if (outer.done) return true;
	if (outer.error) throw new Error(`Mira completion: ${JSON.stringify(outer.error)}`);
	const rawMessage = outer.Message;
	if (!rawMessage) return false;
	let inner: JsonRecord;
	try {
		inner = typeof rawMessage === "string" ? (JSON.parse(rawMessage) as JsonRecord) : (rawMessage as JsonRecord);
	} catch {
		return false;
	}
	if (inner.event === "echo") return false;

	const toolCall = extractToolCall(inner);
	if (toolCall) pushToolCall(events, output, toolCall);

	const content = contentFilter.push(contentDeltaTracker.push(extractContent(inner)));
	if (content) pushText(events, output, content);
	const reasoning = reasoningFilter.push(extractReasoning(inner));
	if (reasoning) pushThinking(events, output, reasoning);
	return false;
}

function parseSse(buffer: string): { events: string[]; rest: string } {
	const frames = buffer.split("\n\n");
	const rest = frames.pop() ?? "";
	const events: string[] = [];
	for (const frame of frames) {
		const lines: string[] = [];
		for (const line of frame.split("\n")) {
			const trimmed = line.trim();
			if (trimmed.startsWith("data:")) lines.push(trimmed.slice(5).trimStart());
		}
		if (lines.length > 0) events.push(lines.join("\n"));
	}
	return { events, rest };
}

function extractContent(inner: JsonRecord): string {
	if (inner.event !== "content") return "";
	const data = inner.data;
	if (typeof data === "string") return extractResultText(data);
	const record = getRecord(data);
	if (!record) return "";
	return extractResultText(record.content);
}

function extractResultText(value: unknown): string {
	if (typeof value === "string") {
		const trimmed = value.trim();
		if (trimmed.startsWith("{") || trimmed.startsWith("[")) {
			try {
				return extractResultText(JSON.parse(trimmed) as unknown) || value;
			} catch {
				return value;
			}
		}
		return value;
	}
	const record = getRecord(value);
	if (record) {
		for (const key of ["result", "answer", "content", "text"]) {
			const found = extractResultText(record[key]);
			if (found) return found;
		}
		return JSON.stringify(record);
	}
	if (Array.isArray(value)) return value.map((item) => extractResultText(item)).join("");
	return value === undefined || value === null ? "" : String(value);
}

function extractReasoning(inner: JsonRecord): string {
	if (inner.event !== "reason") return "";
	const data = getRecord(inner.data);
	const event = getRecord(data?.event);
	const delta = getRecord(event?.delta);
	return delta?.type === "text_delta" && typeof delta.text === "string" ? delta.text : "";
}

function extractToolCall(inner: JsonRecord): ToolCall | undefined {
	const data = getRecord(inner.data);
	const message = getRecord(data?.message);
	const content = message?.content;
	if (Array.isArray(content)) {
		for (const block of content) {
			const record = getRecord(block);
			if (record?.type === "tool_use" && typeof record.name === "string") {
				return {
					type: "toolCall",
					id: typeof record.id === "string" ? record.id : `mira_tool_${Date.now()}`,
					name: record.name,
					arguments: getRecord(record.input) ?? {},
				};
			}
		}
	}
	const streamEvent = getRecord(data?.event);
	if (streamEvent?.type !== "content_block_start") return undefined;
	const contentBlock = getRecord(streamEvent.content_block);
	if (contentBlock?.type !== "tool_use" || typeof contentBlock.name !== "string") return undefined;
	return {
		type: "toolCall",
		id: typeof contentBlock.id === "string" ? contentBlock.id : `mira_tool_${Date.now()}`,
		name: contentBlock.name,
		arguments: getRecord(contentBlock.input) ?? {},
	};
}

function pushText(events: AssistantMessageEventStream, output: AssistantMessage, text: string): void {
	const block = getLastTextBlock(output) ?? createTextBlock(events, output);
	block.text += text;
	events.push({ type: "text_delta", contentIndex: output.content.indexOf(block), delta: text, partial: output });
}

function createTextBlock(events: AssistantMessageEventStream, output: AssistantMessage): TextContent {
	finishOpenThinking(events, output);
	const block: TextContent = { type: "text", text: "" };
	output.content.push(block);
	events.push({ type: "text_start", contentIndex: output.content.length - 1, partial: output });
	return block;
}

function getLastTextBlock(output: AssistantMessage): TextContent | undefined {
	const block = output.content.at(-1);
	return block?.type === "text" ? block : undefined;
}

function finishOpenText(events: AssistantMessageEventStream, output: AssistantMessage): void {
	const block = getLastTextBlock(output);
	if (!block) return;
	events.push({ type: "text_end", contentIndex: output.content.indexOf(block), content: block.text, partial: output });
}

function pushThinking(events: AssistantMessageEventStream, output: AssistantMessage, text: string): void {
	const block = getLastThinkingBlock(output) ?? createThinkingBlock(events, output);
	block.thinking += text;
	events.push({ type: "thinking_delta", contentIndex: output.content.indexOf(block), delta: text, partial: output });
}

function createThinkingBlock(events: AssistantMessageEventStream, output: AssistantMessage): ThinkingContent {
	finishOpenText(events, output);
	const block: ThinkingContent = { type: "thinking", thinking: "" };
	output.content.push(block);
	events.push({ type: "thinking_start", contentIndex: output.content.length - 1, partial: output });
	return block;
}

function getLastThinkingBlock(output: AssistantMessage): ThinkingContent | undefined {
	const block = output.content.at(-1);
	return block?.type === "thinking" ? block : undefined;
}

function finishOpenThinking(events: AssistantMessageEventStream, output: AssistantMessage): void {
	const block = getLastThinkingBlock(output);
	if (!block) return;
	events.push({
		type: "thinking_end",
		contentIndex: output.content.indexOf(block),
		content: block.thinking,
		partial: output,
	});
}

function pushToolCall(events: AssistantMessageEventStream, output: AssistantMessage, toolCall: ToolCall): void {
	finishOpenText(events, output);
	finishOpenThinking(events, output);
	output.content.push(toolCall);
	const contentIndex = output.content.length - 1;
	events.push({ type: "toolcall_start", contentIndex, partial: output });
	events.push({ type: "toolcall_end", contentIndex, toolCall, partial: output });
}

function flattenMessages(context: Context): string {
	const parts: string[] = [];
	if (context.systemPrompt) parts.push(context.systemPrompt);
	for (const message of context.messages) {
		const text = messageToText(message);
		if (text) parts.push(text);
	}
	return parts.join("\n\n");
}

function firstUserText(context: Context): string | undefined {
	for (const message of context.messages) {
		if (message.role !== "user") continue;
		const text = messageToText(message).replace(/^User:\s*/, "");
		if (text) return text.slice(0, 120);
	}
	return undefined;
}

function messageToText(message: Message): string {
	if (message.role === "user") return `User: ${contentToText(message.content)}`;
	if (message.role === "assistant") {
		const blocks = message.content.map((block) => {
			if (block.type === "text") return block.text;
			if (block.type === "thinking") return `<thinking>${block.thinking}</thinking>`;
			return `Tool call ${block.name}(${JSON.stringify(block.arguments)}) id=${block.id}`;
		});
		return `Assistant: ${blocks.filter(Boolean).join("\n")}`;
	}
	return toolResultToText(message);
}

function contentToText(content: string | (TextContent | ImageContent)[]): string {
	if (typeof content === "string") return content;
	return content
		.map((block) => {
			if (block.type === "text") return block.text;
			return `[image:${block.mimeType}]`;
		})
		.join("\n");
}

function toolResultToText(message: ToolResultMessage): string {
	return `Tool result ${message.toolName} id=${message.toolCallId}${message.isError ? " error" : ""}: ${message.content
		.map((block) => (block.type === "text" ? block.text : `[image:${block.mimeType}]`))
		.join("\n")}`;
}

function getRecord(value: unknown): JsonRecord | undefined {
	return typeof value === "object" && value !== null && !Array.isArray(value) ? (value as JsonRecord) : undefined;
}

function headersToRecord(headers: Headers): Record<string, string> {
	const out: Record<string, string> = {};
	headers.forEach((value, key) => {
		out[key] = value;
	});
	return out;
}

function isAbortError(error: unknown): boolean {
	return error instanceof Error && error.name === "AbortError";
}
