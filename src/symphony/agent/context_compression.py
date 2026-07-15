"""LLM 上下文压缩。

参考 Pi Agent 的思路：完整历史继续保存在 session/task 日志中，但真正发给模型时
只保留系统提示、较早上下文摘要和最近消息，避免长会话或大工具输出撑爆上下文。
"""

import json
from dataclasses import dataclass
from typing import Any

from symphony.ai.schema import Message, Role


def estimate_message_chars(message: Message) -> int:
    """估算一条消息序列化后的字符长度。"""
    try:
        return len(json.dumps(message.to_api_dict(), ensure_ascii=False, default=str))
    except Exception:
        return len(message.content or "")


def estimate_messages_chars(messages: list[Message]) -> int:
    """估算一组消息序列化后的总字符长度。"""
    return sum(estimate_message_chars(message) for message in messages)


@dataclass
class ContextCompressionResult:
    """一次上下文压缩的结果。"""

    messages: list[Message]
    compacted: bool
    omitted_messages: int = 0
    original_chars: int = 0
    compressed_chars: int = 0


class ContextCompressor:
    """把较早消息折叠为摘要，同时保留最近消息。"""

    def __init__(
        self,
        max_prompt_chars: int = 120_000,
        keep_recent_messages: int = 24,
        min_recent_messages: int = 6,
        summary_max_chars: int = 4_000,
        max_message_chars: int = 16_000,
        enabled: bool = True,
    ) -> None:
        """保存压缩策略参数。

        这里用字符数近似 token 成本，避免引入模型特定 tokenizer。
        """
        self.max_prompt_chars = max_prompt_chars
        self.keep_recent_messages = keep_recent_messages
        self.min_recent_messages = min_recent_messages
        self.summary_max_chars = summary_max_chars
        self.max_message_chars = max_message_chars
        self.enabled = enabled

    def compress(self, messages: list[Message]) -> ContextCompressionResult:
        """返回适合发给 LLM 的消息列表，不修改原始 messages。"""
        original_chars = estimate_messages_chars(messages)
        copied = [self._copy_message(message) for message in messages]
        if not self.enabled or original_chars <= self.max_prompt_chars:
            return ContextCompressionResult(
                messages=copied,
                compacted=False,
                original_chars=original_chars,
                compressed_chars=original_chars,
            )

        system, body = self._split_system(copied)
        if not body:
            truncated = self._truncate_large_messages(copied)
            return ContextCompressionResult(
                messages=truncated,
                compacted=estimate_messages_chars(truncated) < original_chars,
                original_chars=original_chars,
                compressed_chars=estimate_messages_chars(truncated),
            )

        max_keep = min(len(body), max(1, self.keep_recent_messages))
        min_keep = min(max_keep, max(1, self.min_recent_messages))

        best: list[Message] | None = None
        best_omitted = 0
        for keep_count in range(max_keep, min_keep - 1, -1):
            tail_start = self._align_tail_start(body, len(body) - keep_count)
            omitted = body[:tail_start]
            recent = self._truncate_large_messages(body[tail_start:])
            candidate = [
                *system,
                self._summary_message(omitted),
                *recent,
            ]
            best = candidate
            best_omitted = len(omitted)
            if estimate_messages_chars(candidate) <= self.max_prompt_chars:
                break

        if best is None:
            best = [*system, self._summary_message(body)]
            best_omitted = len(body)

        compressed_chars = estimate_messages_chars(best)
        return ContextCompressionResult(
            messages=best,
            compacted=True,
            omitted_messages=best_omitted,
            original_chars=original_chars,
            compressed_chars=compressed_chars,
        )

    def _split_system(self, messages: list[Message]) -> tuple[list[Message], list[Message]]:
        """把开头连续 system 消息和正文消息分开。"""
        system: list[Message] = []
        body: list[Message] = []
        in_system = True
        for message in messages:
            if in_system and message.role == Role.SYSTEM:
                system.append(message)
                continue
            in_system = False
            body.append(message)
        return system, body

    def _align_tail_start(self, messages: list[Message], start: int) -> int:
        """避免保留区从孤立 tool 消息开始，尽量带上对应 assistant tool_call。"""
        start = max(0, start)
        while start > 0 and messages[start].role == Role.TOOL:
            start -= 1
        if start < len(messages) and messages[start].role == Role.ASSISTANT:
            # 如果保留区从 assistant tool_call 开始，保留后续 tool 结果自然在 tail 中。
            return start
        return start

    def _summary_message(self, omitted: list[Message]) -> Message:
        """把被折叠的消息转成一条 system 摘要。"""
        lines = [
            "较早上下文已压缩；完整原文仍保存在 session/task 日志中。",
            "以下摘要用于保留任务背景、用户意图、关键结论和工具观察，不要把它当作逐字引用：",
        ]
        for idx, message in enumerate(omitted, start=1):
            lines.append(f"{idx}. {self._message_summary(message)}")
        content = "\n".join(lines)
        if len(content) > self.summary_max_chars:
            content = content[: self.summary_max_chars - 80].rstrip()
            content += "\n...（摘要因长度限制被截断，请依赖最近消息和日志继续。）"
        return Message(role=Role.SYSTEM, content=content)

    def _message_summary(self, message: Message) -> str:
        """生成单条消息的紧凑摘要。"""
        prefix = message.role.value
        parts: list[str] = []
        if message.content:
            parts.append(self._one_line(message.content, 240))
        if message.tool_calls:
            calls = [
                f"{call.name}({self._json_preview(call.arguments, 120)})"
                for call in message.tool_calls
            ]
            parts.append("tool_calls=" + "; ".join(calls))
        if message.tool_call_id:
            parts.append(f"tool_call_id={message.tool_call_id}")
        body = " | ".join(parts) if parts else "(no content)"
        return f"{prefix}: {body}"

    def _truncate_large_messages(self, messages: list[Message]) -> list[Message]:
        """裁剪单条超长消息，通常用于大工具结果。"""
        result: list[Message] = []
        for message in messages:
            copied = self._copy_message(message)
            if copied.content and len(copied.content) > self.max_message_chars:
                omitted = len(copied.content) - self.max_message_chars
                copied.content = (
                    copied.content[: self.max_message_chars].rstrip()
                    + f"\n...[truncated {omitted} chars for model context; full content is in logs]"
                )
            result.append(copied)
        return result

    def _copy_message(self, message: Message) -> Message:
        """复制消息，确保压缩不会修改调用方持有的原始历史。"""
        return Message.model_validate(message.model_dump(mode="python"))

    def _json_preview(self, value: Any, limit: int) -> str:
        """把 JSON 值转成一行预览。"""
        try:
            text = json.dumps(value, ensure_ascii=False, default=str, sort_keys=True)
        except TypeError:
            text = str(value)
        return self._one_line(text, limit)

    def _one_line(self, text: str, limit: int) -> str:
        """压成单行并截断。"""
        text = " ".join(text.split())
        if len(text) <= limit:
            return text
        return text[: limit - 3] + "..."
