"""Agent 运行时（ReAct 循环）实现。

AgentRuntime 编排一次节点级 Agent 执行：把系统提示词与用户输入注入对话，
反复调用 LLM，处理其发起的工具（技能）调用，并对最终自然语言输出做 JSON 解析
与 JSON Schema 校验。校验失败时按重试策略引导模型修正，超过上限则转为等待用户输入。
执行过程中通过 on_event 回调对外发射结构化事件，便于流式推送与持久化。
"""

import json
from typing import Any, Callable, Optional

import jsonschema

from symphony.agent.context_compression import ContextCompressor
from symphony.agent.context import AgentContext
from symphony.agent.events import (
    AgentThought,
    Event,
    LogMessage,
    NodeCompleted,
    NodeFailed,
    NodeStarted,
    NodeWaitingInput,
    SkillCalled,
    SkillFailed,
    SkillReturned,
)
from symphony.agent.tools import build_tool_defs, build_tool_guidance, run_skill
from symphony.ai.provider import LLMProvider
from symphony.ai.schema import Message, Role, ToolCall, ToolDef
from symphony.skills.registry import SkillRegistry


class AgentRuntime:
    """基于 ReAct 模式的节点级 Agent 运行时。"""

    def __init__(
        self,
        llm_provider: LLMProvider,
        skill_registry: SkillRegistry,
        system_prompt: str,
        output_schema: dict[str, Any],
        on_event: Callable[[Event], None],
        max_retries: int = 3,
        max_iterations: int = 20,
        model: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        on_trace: Optional[Callable[[dict], None]] = None,
        context_compressor: Optional[ContextCompressor] = None,
    ) -> None:
        """初始化运行时，保存依赖与执行策略参数。"""
        # 大模型服务提供方
        self.llm = llm_provider
        # 技能注册中心
        self.registry = skill_registry
        # 默认系统提示词
        self.system_prompt = system_prompt
        # 节点输出的 JSON Schema
        self.output_schema = output_schema
        # 事件回调
        self.on_event = on_event
        # LLM 调用轨迹回调，可选：每次 chat 后记录完整请求/响应/用量
        self.on_trace = on_trace
        # schema 校验/解析失败的最大重试次数
        self.max_retries = max_retries
        # ReAct 循环的最大迭代轮数
        self.max_iterations = max_iterations
        # 覆盖 provider 默认模型的模型名，可选
        self.model = model
        # 采样温度，可选
        self.temperature = temperature
        # 最大生成 token 数，可选
        self.max_tokens = max_tokens
        # 只压缩发给模型的请求副本，不修改 AgentContext 中的完整历史
        self.context_compressor = context_compressor or ContextCompressor()

    def _emit(self, event: Event) -> None:
        """向外部回调发射一个事件。"""
        self.on_event(event)

    def _record_trace(
        self,
        node_id: str,
        request_messages: list,
        resp,
        context_compaction: Optional[dict[str, Any]] = None,
    ) -> None:
        """把一次 LLM 调用组装为轨迹并交给 on_trace 回调（未设置则跳过）。"""
        # 未配置轨迹回调时不做任何事
        if self.on_trace is None:
            return
        # 取首个候选回复
        choice = resp.choices[0] if resp.choices else None
        # 提取工具调用（转为可序列化的字典列表），无则为 None
        tool_calls = None
        if choice is not None and choice.tool_calls:
            tool_calls = [
                {"id": tc.id, "name": tc.name, "arguments": tc.arguments} for tc in choice.tool_calls
            ]
        # 组装轨迹字典：完整请求消息、响应正文、用量与工具调用
        trace = {
            "node_id": node_id,
            "model": resp.model,
            "request_messages": request_messages,
            "response": {
                "id": resp.id,
                "content": choice.content if choice is not None else None,
                "tool_calls": tool_calls,
            },
            "usage": resp.usage.model_dump(),
            "tool_calls": tool_calls,
            "context_compaction": context_compaction or {"compacted": False},
        }
        # 交给外部回调持久化
        self.on_trace(trace)

    def _build_tool_defs(self) -> list[ToolDef]:
        """把已注册技能转换为供 LLM 调用的工具定义列表。"""
        return build_tool_defs(self.registry)

    def _parse_json_output(self, content: str) -> Optional[dict]:
        """从模型输出中解析出 JSON 对象，解析失败返回 None。"""
        # 优先按完整 JSON 直接解析
        try:
            return json.loads(content)
        except (json.JSONDecodeError, TypeError):
            pass
        # 回退：截取首个 { 到末个 } 之间的子串再解析
        start = content.find("{")
        end = content.rfind("}")
        if start != -1 and end != -1 and end > start:
            try:
                return json.loads(content[start : end + 1])
            except json.JSONDecodeError:
                return None
        return None

    def _build_chat_kwargs(self) -> dict[str, Any]:
        """构造传给 provider.chat 的关键字参数，仅包含非 None 值。"""
        kwargs: dict[str, Any] = {}
        # model 通过 kwargs 透传（provider.chat 签名无 model 形参）
        if self.model is not None:
            kwargs["model"] = self.model
        # 温度仅在显式设置时传入
        if self.temperature is not None:
            kwargs["temperature"] = self.temperature
        # 最大 token 数仅在显式设置时传入
        if self.max_tokens is not None:
            kwargs["max_tokens"] = self.max_tokens
        return kwargs

    async def run(
        self,
        initial_input: str,
        context: AgentContext,
        prompt_override: Optional[str] = None,
        reset: bool = True,
    ) -> Any:
        """执行一次 ReAct 循环并返回符合 schema 的输出。

        :param initial_input: 用户初始输入，仅在 reset=True 时作为首条 user 消息注入。
        :param context: 运行时上下文，会被读写。
        :param prompt_override: 提示词覆盖，非空时优先于 self.system_prompt。
        :param reset: 为 True 时重置消息并注入系统/用户消息；为 False 时假定
            context.messages 已就绪（用于 resume 场景），直接进入循环。
        :return: 校验通过的输出字典；无法产出时返回 None。
        """
        # 计算本次使用的系统提示词
        effective_prompt = prompt_override or self.system_prompt
        # 构造工具定义（无技能时后续传 None）
        tools = self._build_tool_defs()
        # 通知外部节点开始执行
        self._emit(NodeStarted(task_id=context.task_id, node_id=context.node_id))
        # 首次执行时重置消息历史并注入系统提示词与用户输入
        if reset:
            guidance = build_tool_guidance(tools)
            if guidance:
                effective_prompt = f"{effective_prompt}\n\n{guidance}"
            context.reset_messages(effective_prompt)
            context.add_message(Message(role=Role.USER, content=initial_input))
        # schema 校验/解析失败的重试计数
        retries = 0
        # 预备好透传给 chat 的可选参数
        chat_kwargs = self._build_chat_kwargs()

        for _ in range(self.max_iterations):
            compressed = self.context_compressor.compress(context.messages)
            # 记录本轮请求的消息快照（在追加助手回复前，即真实发给模型的输入）
            request_messages = [m.to_api_dict() for m in compressed.messages]
            # 调用大模型；无工具时传 None 以省略 tools 字段
            resp = await self.llm.chat(
                messages=compressed.messages,
                tools=tools if tools else None,
                **chat_kwargs,
            )
            # 取首个候选作为助手消息并记录进历史
            assistant_msg = resp.choices[0]
            context.add_message(assistant_msg)
            # 记录一条完整 LLM 调用轨迹（请求/响应/用量/工具调用），供调试审计
            self._record_trace(
                context.node_id,
                request_messages,
                resp,
                context_compaction={
                    "compacted": compressed.compacted,
                    "omitted_messages": compressed.omitted_messages,
                    "original_chars": compressed.original_chars,
                    "compressed_chars": compressed.compressed_chars,
                },
            )
            # 有正文时发射思考事件（截断至 500 字符）
            if assistant_msg.content:
                self._emit(
                    AgentThought(
                        task_id=context.task_id,
                        node_id=context.node_id,
                        content=assistant_msg.content[:500],
                    )
                )
            # 模型发起工具调用：逐个执行后进入下一轮让模型观察结果
            if assistant_msg.tool_calls:
                for tc in assistant_msg.tool_calls:
                    await self._execute_tool_call(tc, context)
                continue

            # 无工具调用：尝试从正文解析 JSON 输出
            parsed = self._parse_json_output(assistant_msg.content or "")
            if parsed is not None:
                # 用 JSON Schema 校验解析结果
                try:
                    jsonschema.validate(parsed, self.output_schema)
                except jsonschema.ValidationError as e:
                    # 校验失败记录告警，并按策略重试或转为等待用户输入
                    self._emit(
                        LogMessage(
                            task_id=context.task_id,
                            node_id=context.node_id,
                            level="warn",
                            message=f"输出未通过 schema 校验：{e.message}",
                        )
                    )
                    if retries < self.max_retries:
                        retries += 1
                        context.add_message(
                            Message(
                                role=Role.USER,
                                content=f"你的输出不符合 schema，错误：{e.message}，请只输出合法 JSON。",
                            )
                        )
                        continue
                    self._emit(
                        NodeWaitingInput(
                            task_id=context.task_id,
                            node_id=context.node_id,
                            reason="max_retries",
                        )
                    )
                    return None
                # 校验通过：发射完成事件并返回结果
                self._emit(
                    NodeCompleted(
                        task_id=context.task_id,
                        node_id=context.node_id,
                        output=parsed,
                    )
                )
                return parsed
            # 无法解析为 JSON：按策略重试或转为等待用户输入
            if retries < self.max_retries:
                retries += 1
                context.add_message(
                    Message(role=Role.USER, content="请输出符合 schema 的合法 JSON 对象。")
                )
                continue
            self._emit(
                NodeWaitingInput(
                    task_id=context.task_id,
                    node_id=context.node_id,
                    reason="max_retries",
                )
            )
            return None

        # 循环耗尽仍未产出合法输出：判定节点失败
        self._emit(
            NodeFailed(
                task_id=context.task_id,
                node_id=context.node_id,
                error="Max iterations reached",
            )
        )
        return None

    async def _execute_tool_call(self, tc: ToolCall, context: AgentContext) -> None:
        """执行单个工具调用，并把结果作为 tool 消息写回上下文。"""
        self._emit(
            SkillCalled(
                task_id=context.task_id,
                node_id=context.node_id,
                skill_name=tc.name,
                args=tc.arguments,
            )
        )
        result, error = await run_skill(
            self.registry,
            tc,
            task_id=context.task_id,
            node_id=context.node_id,
            variables=context.variables,
        )
        if error is not None:
            self._emit(
                SkillFailed(
                    task_id=context.task_id,
                    node_id=context.node_id,
                    skill_name=tc.name,
                    error=error,
                )
            )
            context.add_message(
                Message(
                    role=Role.TOOL,
                    tool_call_id=tc.id,
                    content=json.dumps({"error": error}, ensure_ascii=False),
                )
            )
            return
        self._emit(
            SkillReturned(
                task_id=context.task_id,
                node_id=context.node_id,
                skill_name=tc.name,
                result=result,
            )
        )
        context.add_message(
            Message(
                role=Role.TOOL,
                tool_call_id=tc.id,
                content=json.dumps(result, ensure_ascii=False, default=str),
            )
        )

    async def resume_with_input(
        self,
        user_input: str,
        context: AgentContext,
        prompt_override: Optional[str] = None,
    ) -> Any:
        """在等待用户输入后，用新输入恢复执行。

        追加一条用户消息到既有历史，随后以 reset=False 复用 run 的循环逻辑，
        从而保留此前的对话上下文而不被重置清空。
        """
        # 把用户新输入作为新一轮 user 消息追加
        context.add_message(Message(role=Role.USER, content=user_input))
        # reset=False：沿用现有消息历史直接进入循环
        return await self.run(user_input, context, prompt_override=prompt_override, reset=False)
