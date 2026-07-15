"""默认流式对话运行时。

ChatRuntime 面向 Pi Agent 默认对话：以流式方式调用 LLM，逐段产出自然语言，
并在模型发起工具调用时执行 ReAct 循环（调用技能 -> 观察结果 -> 继续）。
它产出一个异步事件流（chat_events），不做 JSON Schema 校验，也不创建 SOP 任务。
"""

import json
from typing import Any, AsyncIterator

from symphony.agent.chat_events import (
    ChatAnswerDelta,
    ChatCompleted,
    ChatEvent,
    ChatFailed,
    ChatThinking,
    ChatToolCall,
    ChatToolResult,
)
from symphony.agent.context_compression import ContextCompressor
from symphony.agent.tools import build_tool_defs, build_tool_guidance, run_skill, summarize_value
from symphony.ai.schema import Message, Role
from symphony.skills.references import (
    SkillReferenceIndex,
    build_skill_reference_guidance,
    is_skill_inventory_query,
)
from symphony.skills.registry import SkillRegistry

CHAT_SYSTEM_PROMPT = """你是 Symphony 默认对话中的 Pi Agent。

像正常 Pi Agent 一样结合上下文自然作答，并在需要时调用可用工具。
这不是 SOP 任务，不要创建任务记录。直接用自然语言回复；
当用户明确要求结构化结果时，在回复中用 ```json 代码块给出对应内容。
"""

_TASK_ID = "chat"
_NODE_ID = "pi_agent_chat"


class ChatRuntime:
    """默认流式对话的 ReAct 运行时。"""

    def __init__(
        self,
        llm_provider: Any,
        skill_registry: SkillRegistry,
        system_prompt: str = CHAT_SYSTEM_PROMPT,
        max_iterations: int = 999,
        skill_reference_index: SkillReferenceIndex | None = None,
        skill_reference_limit: int = 999,
        on_trace: Any = None,
        context_compressor: ContextCompressor | None = None,
    ) -> None:
        """保存依赖与循环上限。"""
        self.llm = llm_provider
        self.registry = skill_registry
        self.system_prompt = system_prompt
        self.max_iterations = max_iterations
        self.skill_reference_index = skill_reference_index
        self.skill_reference_limit = skill_reference_limit
        self.on_trace = on_trace
        self.context_compressor = context_compressor or ContextCompressor()

    def _build_messages(self, question: str, history: list[dict]) -> list[Message]:
        """把系统提示、历史与本轮问题组装成消息列表。"""
        messages = [Message(role=Role.SYSTEM, content=self.system_prompt)]
        for item in history:
            role = Role.USER if item.get("role") == "user" else Role.ASSISTANT
            messages.append(Message(role=role, content=item.get("content", "")))
        messages.append(Message(role=Role.USER, content=question))
        return messages

    async def stream(
        self, question: str, history: list[dict]
    ) -> AsyncIterator[ChatEvent]:
        """执行一轮流式对话，逐个产出 chat 事件。"""
        messages = self._build_messages(question, history)
        tools = build_tool_defs(self.registry)
        tool_runtime_variables = {
            "available_skills": [
                {
                    "name": skill.name,
                    "description": skill.description,
                    "source": self.registry.source_of(skill.name) or "",
                }
                for skill in self.registry.list_skills()
            ],
            "skill_reference_index": self.skill_reference_index,
        }
        guidance = build_tool_guidance(tools)
        if guidance:
            messages[0].content = f"{messages[0].content}\n\n{guidance}"
        reference_guidance = self._skill_reference_guidance(question)
        if reference_guidance:
            messages[0].content = f"{messages[0].content}\n\n{reference_guidance}"
        inventory_guidance = self._skill_inventory_guidance(question, tool_runtime_variables)
        if inventory_guidance:
            messages[0].content = f"{messages[0].content}\n\n{inventory_guidance}"
        answer_parts: list[str] = []

        for _ in range(self.max_iterations):
            compressed = self.context_compressor.compress(messages)
            request_messages = [message.to_api_dict() for message in compressed.messages]
            round_text: list[str] = []
            tool_calls = None
            async for delta in self.llm.chat_stream(
                compressed.messages, tools=tools if tools else None
            ):
                if delta.content:
                    round_text.append(delta.content)
                    answer_parts.append(delta.content)
                    yield ChatAnswerDelta(text=delta.content)
                if delta.tool_calls:
                    tool_calls = delta.tool_calls

            # 记录本轮助手消息（含文本与工具调用）以维持上下文
            messages.append(
                Message(
                    role=Role.ASSISTANT,
                    content="".join(round_text) or None,
                    tool_calls=tool_calls,
                )
            )

            if self.on_trace is not None:
                self.on_trace(
                    {
                        "node_id": _NODE_ID,
                        "model": getattr(self.llm, "model", None),
                        "request_messages": request_messages,
                        "response": {
                            "content": "".join(round_text) or None,
                            "tool_calls": [
                                {
                                    "id": tc.id,
                                    "name": tc.name,
                                    "arguments": tc.arguments,
                                }
                                for tc in (tool_calls or [])
                            ]
                            or None,
                        },
                        "usage": None,
                        "tool_calls": [
                            {
                                "id": tc.id,
                                "name": tc.name,
                                "arguments": tc.arguments,
                            }
                            for tc in (tool_calls or [])
                        ]
                        or None,
                        "context_compaction": {
                            "compacted": compressed.compacted,
                            "omitted_messages": compressed.omitted_messages,
                            "original_chars": compressed.original_chars,
                            "compressed_chars": compressed.compressed_chars,
                        },
                    }
                )

            if not tool_calls:
                yield ChatCompleted(answer="".join(answer_parts))
                return

            yield ChatThinking(content=f"调用 {len(tool_calls)} 个工具")
            for tc in tool_calls:
                yield ChatToolCall(
                    skill_name=tc.name,
                    args=tc.arguments,
                    summary=summarize_value(tc.arguments),
                )
                result, error = await run_skill(
                    self.registry,
                    tc,
                    task_id=_TASK_ID,
                    node_id=_NODE_ID,
                    variables=tool_runtime_variables,
                )
                if error is not None:
                    yield ChatToolResult(
                        skill_name=tc.name, ok=False, detail=error
                    )
                    messages.append(
                        Message(
                            role=Role.TOOL,
                            tool_call_id=tc.id,
                            content=json.dumps({"error": error}, ensure_ascii=False),
                        )
                    )
                else:
                    yield ChatToolResult(
                        skill_name=tc.name,
                        ok=True,
                        detail=summarize_value(result),
                    )
                    messages.append(
                        Message(
                            role=Role.TOOL,
                            tool_call_id=tc.id,
                            content=json.dumps(
                                result, ensure_ascii=False, default=str
                            ),
                        )
                    )

        async for event in self._final_answer_without_tools(messages, answer_parts):
            yield event

    def _skill_reference_guidance(self, question: str) -> str:
        """按用户问题检索外部 Skill 文档，并转成系统提示片段。"""
        if self.skill_reference_index is None:
            return ""
        matches = self.skill_reference_index.search(
            question,
            limit=self.skill_reference_limit,
        )
        return build_skill_reference_guidance(matches)

    def _skill_inventory_guidance(self, question: str, variables: dict[str, Any]) -> str:
        """当用户询问可用能力时，直接注入完整能力清单摘要。"""
        if not is_skill_inventory_query(question):
            return ""

        executable = variables.get("available_skills") or []
        lines = [
            "当前 Symphony Skill 清单：",
            "executable_skills 是当前可直接调用的 Python 工具；external_references 是只读 SKILL.md 参考资料。",
            "回答用户能力清单问题时，应先说明这两类的区别。",
            "可执行工具：",
        ]
        for item in executable:
            lines.append(f"- {item.get('name')}: {item.get('description', '')}")

        if self.skill_reference_index is not None:
            lines.append(f"外部参考 Skill 总数：{len(self.skill_reference_index.references)}")
            for match in self.skill_reference_index.list_all(
                limit=self.skill_reference_limit,
            ):
                ref = match.reference
                lines.append(f"- {ref.name} ({ref.source}): {ref.description[:160]}")
        return "\n".join(lines)

    async def _final_answer_without_tools(
        self,
        messages: list[Message],
        answer_parts: list[str],
    ) -> AsyncIterator[ChatEvent]:
        """工具循环达到上限后，禁用工具生成一次最终回答。"""
        messages.append(
            Message(
                role=Role.USER,
                content=(
                    "工具调用已达到上限。请基于以上对话和工具结果直接回答用户，"
                    "不要再调用任何工具。"
                ),
            )
        )
        yield ChatThinking(
            content="Tool loop reached limit; generating final answer without tools."
        )
        fallback_parts: list[str] = []
        try:
            compressed = self.context_compressor.compress(messages)
            async for delta in self.llm.chat_stream(compressed.messages, tools=None):
                if delta.content:
                    fallback_parts.append(delta.content)
                    answer_parts.append(delta.content)
                    yield ChatAnswerDelta(text=delta.content)
        except Exception as exc:
            yield ChatFailed(error=f"final_answer_failed: {exc}")
            return

        if fallback_parts:
            yield ChatCompleted(answer="".join(answer_parts))
            return
        yield ChatFailed(error="tool_loop_limit_reached")
