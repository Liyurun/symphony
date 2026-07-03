import { afterEach, describe, expect, it, vi } from "vitest";
import { stream } from "../src/api/mira.ts";
import type { Context, Model } from "../src/types.ts";

const model: Model<"mira"> = {
	id: "re-o-48",
	api: "mira",
	provider: "mira",
	name: "Mira",
	baseUrl: "https://mira.example.test",
	reasoning: true,
	input: ["text"],
	cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
	contextWindow: 128000,
	maxTokens: 4096,
};

const context: Context = {
	messages: [{ role: "user", content: "hello", timestamp: 1 }],
};

afterEach(() => {
	vi.restoreAllMocks();
});

describe("mira", () => {
	it("converts cumulative content snapshots into text deltas", async () => {
		mockFetch([
			{ event: "content", data: { content: { result: "Hello" } } },
			{ event: "content", data: { content: { result: "Hello world" } } },
			{ event: "content", data: { content: { result: "Hello world" } } },
			{ done: true },
		]);

		const events = stream(model, context, { apiKey: "token" });
		let text = "";
		const deltas: string[] = [];
		for await (const event of events) {
			if (event.type !== "text_delta") continue;
			deltas.push(event.delta);
			text += event.delta;
		}

		expect(deltas).toEqual(["Hello", " world"]);
		expect(text).toBe("Hello world");
		expect((await events.result()).content).toEqual([{ type: "text", text: "Hello world" }]);
	});

	it("keeps true incremental content deltas unchanged", async () => {
		mockFetch([
			{ event: "content", data: { content: { result: "Hello" } } },
			{ event: "content", data: { content: { result: " world" } } },
			{ done: true },
		]);

		const events = stream(model, context, { apiKey: "token" });
		let text = "";
		for await (const event of events) {
			if (event.type === "text_delta") text += event.delta;
		}

		expect(text).toBe("Hello world");
	});
});

function mockFetch(completionEvents: unknown[]): void {
	vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
		const url = String(input);
		if (url.endsWith("/mira/api/v1/chat/create")) {
			return new Response(JSON.stringify({ code: 0, data: { sessionItem: { sessionId: "session-1" } } }), {
				status: 200,
				headers: { "content-type": "application/json" },
			});
		}
		if (url.endsWith("/mira/api/v1/chat/completion")) {
			return new Response(toSseStream(completionEvents), {
				status: 200,
				headers: { "content-type": "text/event-stream" },
			});
		}
		return new Response("not found", { status: 404 });
	});
}

function toSseStream(events: unknown[]): ReadableStream<Uint8Array> {
	const encoder = new TextEncoder();
	return new ReadableStream({
		start(controller) {
			for (const event of events) {
				controller.enqueue(encoder.encode(`data: ${JSON.stringify(wrapMessage(event))}\n\n`));
			}
			controller.close();
		},
	});
}

function wrapMessage(event: unknown): unknown {
	if (typeof event === "object" && event !== null && "done" in event) return event;
	return { Message: JSON.stringify(event) };
}
