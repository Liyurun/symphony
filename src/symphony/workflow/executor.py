"""工作流执行器（线性 DAG 编排引擎）。

WorkflowExecutor 是 Symphony 最核心的编排引擎，负责按 SOP 模板的边关系
线性地推进各节点执行。它统一处理三种节点类型（AGENT / HUMAN / SKILL）的分派，
维护每个节点的执行状态（NodeState），并支持暂停/恢复、重试、跳过、人工干预等
交互能力。执行过程中通过 on_event 回调对外发射结构化事件，便于流式推送与持久化。

关键新特性（相对 MVP 早期版本）：
- **类型化 I/O 校验**：每个节点可声明 ``inputs`` / ``outputs`` 字段列表，
  系统会在节点执行前校验必填输入是否已就绪，执行后校验输出字段是否符合类型/JSON Schema；
- **自动线性化**：当 ``edges`` 为空时，模板的 ``_normalize`` 会自动按 nodes 顺序串接边；
- **输入装配**：agent/skill 节点执行前，系统按字段名在「工作流变量 → 上游节点输出」
  中解析输入值，组装出结构化 ``input_payload`` 喂给节点，而非把整个 variables 字典
  原样塞入。
"""

import json
from datetime import datetime, timezone
from typing import Any, Callable, Optional

import jsonschema

from symphony.agent.context import AgentContext
from symphony.agent.context_compression import ContextCompressor
from symphony.agent.events import (
    DownstreamInvalidated,
    Event,
    InteractionRequested,
    NodeAttemptCompleted,
    NodeAttemptFailed,
    NodeAttemptStarted,
    NodeCompleted,
    NodeFailed,
    NodeMarkedStale,
    NodeRetryRequested,
    NodeStatusChanged,
    NodeSupplementalInstructionAdded,
    NodeWaitingInput,
    SkillCalled,
    SkillReturned,
    SubFlowConfirmed,
    SubFlowDraftCreated,
    TaskCompleted,
    TaskStarted,
    UserIntervened,
)
from symphony.agent.runtime import AgentRuntime
from symphony.ai.provider import LLMProvider
from symphony.config import ContextCompressionConfig
from symphony.skills.base import SkillContext
from symphony.skills.registry import SkillRegistry
from symphony.workflow.models import (
    Edge,
    IOField,
    IOType,
    Node,
    NodeType,
    SOPTemplate,
    SubFlowDraft,
)
from symphony.workflow.subflow import SubFlowExecutor
from symphony.workflow.template import render_prompt

from pydantic import BaseModel, Field

from symphony.agent.events import NodeStatus


class NodeState(BaseModel):
    """单个节点在一次工作流执行中的运行状态。"""

    # 节点 id
    node_id: str
    # 当前执行状态，默认待执行
    status: NodeStatus = NodeStatus.PENDING
    # 喂给节点的结构化输入 dict（字段名 -> 值）
    input: Any = None
    # 节点输出数据（经 output 校验通过）
    output: Any = None
    # 失败原因描述，成功时为 None
    error: Optional[str] = None
    # 已尝试执行的次数
    attempts: int = 0
    # attempt 详细历史，保留每次运行的输入、输出、触发原因和补充指令
    attempt_history: list[dict[str, Any]] = Field(default_factory=list)
    # 当前节点输出是否已因上游重跑而过期
    stale: bool = False
    # 输出过期原因
    stale_reason: Optional[str] = None
    # 当前等待回答的 interaction id
    pending_interaction_id: Optional[str] = None
    # 提示词覆盖，非空时优先于节点默认提示词
    prompt_override: Optional[str] = None
    # composite 节点内部子流程状态，供 UI 区分确认/运行/完成阶段
    subflow_status: Optional[str] = None
    # 当前聚焦路径，composite 节点使用 [父节点 id, 可选子节点 id]
    focus_path: list[str] = Field(default_factory=list)


def _safe_render(template_str: str, variables: dict) -> str:
    """宽松渲染提示词：渲染出错时退化为原始模板字符串。

    render_prompt 使用 StrictUndefined，变量缺失会抛 jinja2 错误。
    这里作为模板渲染边界捕获异常，保证执行器的健壮性。
    """
    try:
        return render_prompt(template_str, variables)
    except Exception:
        return template_str


def _build_context_compressor(config: ContextCompressionConfig) -> ContextCompressor:
    """按配置构造 AgentRuntime 使用的上下文压缩器。"""
    return ContextCompressor(
        max_prompt_chars=config.max_prompt_chars,
        keep_recent_messages=config.keep_recent_messages,
        min_recent_messages=config.min_recent_messages,
        summary_max_chars=config.summary_max_chars,
        max_message_chars=config.max_message_chars,
        enabled=config.enabled,
    )


class WorkflowExecutor:
    """线性 SOP 工作流执行器。"""

    def __init__(
        self,
        task_id: str,
        template: SOPTemplate,
        variables: dict,
        llm_provider: LLMProvider,
        skill_registry: SkillRegistry,
        on_event: Callable[[Event], None],
        on_trace: Optional[Callable[[dict], None]] = None,
        max_iterations: int = 20,
        max_retries: int = 3,
        context_compression_config: ContextCompressionConfig | None = None,
    ) -> None:
        """初始化执行器，保存依赖并为每个节点建立初始状态。"""
        self.task_id = task_id
        self.template = template
        # 拷贝顶层工作流变量（用户传入的输入）
        self.variables = dict(variables)
        self.llm_provider = llm_provider
        self.skill_registry = skill_registry
        self.on_event = on_event
        self.on_trace = on_trace
        self.max_iterations = max_iterations
        self.max_retries = max_retries
        self.context_compression_config = context_compression_config
        self.node_states: dict[str, NodeState] = {
            node.id: NodeState(node_id=node.id, status=NodeStatus.PENDING) for node in template.nodes
        }
        self.subflow_drafts: dict[str, SubFlowDraft] = {}
        self.subflow_executors: dict[str, SubFlowExecutor] = {}
        self._paused = False
        self._current_node_id = template.entry_node or (template.nodes[0].id if template.nodes else "")

    def _emit(self, event: Event) -> None:
        self.on_event(event)

    def _emit_trace(self, trace: dict) -> None:
        trace = {"task_id": self.task_id, **trace}
        if self.on_trace is not None:
            self.on_trace(trace)

    def _mark_attempt_started(self, state: NodeState, trigger: str) -> None:
        """记录一次节点实际执行 attempt 的开始状态。"""
        supplemental_instruction = state.prompt_override if trigger == "user_correction" else None
        self._emit(
            NodeAttemptStarted(
                task_id=self.task_id,
                node_id=state.node_id,
                attempt_no=state.attempts,
                trigger=trigger,
            )
        )
        record = {
            "attempt_no": state.attempts,
            "trigger": trigger,
            "supplemental_instruction": supplemental_instruction,
            "status": "running",
            "input": None,
            "output": None,
            "error": None,
        }
        if (
            state.attempt_history
            and state.attempt_history[-1].get("attempt_no") == state.attempts
            and state.attempt_history[-1].get("status") == "pending"
        ):
            state.attempt_history[-1].update(record)
        else:
            state.attempt_history.append(record)

    def _mark_attempt_input(self, state: NodeState, input_payload: dict[str, Any]) -> None:
        """把已装配的节点输入写回当前 attempt。"""
        if state.attempt_history:
            state.attempt_history[-1]["input"] = input_payload

    def _mark_attempt_completed(self, state: NodeState) -> None:
        """把当前 attempt 标记为完成，并清除 stale 标记。"""
        if state.attempt_history:
            state.attempt_history[-1]["status"] = "completed"
            state.attempt_history[-1]["output"] = state.output
            state.attempt_history[-1]["error"] = None
        state.stale = False
        state.stale_reason = None
        self._emit(
            NodeAttemptCompleted(
                task_id=self.task_id,
                node_id=state.node_id,
                attempt_no=state.attempts,
                output=state.output,
            )
        )

    def _mark_attempt_failed(self, state: NodeState, error: str) -> None:
        """把当前 attempt 标记为失败。"""
        if state.attempt_history:
            state.attempt_history[-1]["status"] = "failed"
            state.attempt_history[-1]["error"] = error
        self._emit(
            NodeAttemptFailed(
                task_id=self.task_id,
                node_id=state.node_id,
                attempt_no=state.attempts,
                error=error,
            )
        )

    def _filtered_registry(self, node: Node) -> SkillRegistry:
        """构造仅包含节点声明技能的子注册中心。"""
        if not node.skills:
            return self.skill_registry
        registry = SkillRegistry()
        for name in node.skills:
            skill = self.skill_registry.get(name)
            if skill is not None:
                registry.register(skill)
        return registry

    def _resolve_input(self, field: IOField, upstream_order: list[str]) -> tuple[bool, Any]:
        """解析单个输入字段的值。

        查找顺序：
        1. 顶层 ``self.variables[field.name]``（工作流级输入）；
        2. 按 ``upstream_order`` 顺序从近到远，查找 ``self.variables[upstream_id][field.name]``。

        :return: (found, value)；found=False 时 value 无意义。
        """
        # 1) 工作流级变量
        if field.name in self.variables and self.variables[field.name] is not None:
            return True, self.variables[field.name]
        # 2) 上游节点输出（最近优先）
        for uid in upstream_order:
            ns = self.variables.get(uid)
            if isinstance(ns, dict) and field.name in ns and ns[field.name] is not None:
                return True, ns[field.name]
        return False, None

    def _build_node_input(self, node: Node) -> tuple[Optional[dict[str, Any]], Optional[str]]:
        """为节点构造结构化输入 dict，并做必填字段校验与 JSON Schema 校验。

        :return: (payload, error)；payload 为 None 表示校验失败，error 为原因描述。
        """
        # 取节点的上游顺序（最近在前）
        linear = self.template.linear_order()
        try:
            idx = linear.index(node.id)
        except ValueError:
            idx = 0
        # 注意：upstream 顺序是"离当前节点从近到远"，这样同名输出优先取最近上游
        upstream_order = list(reversed(linear[:idx]))

        # 未声明 typed inputs：回退到旧行为——使用 variables 全集（浅拷贝）
        if not node.inputs:
            payload = dict(self.variables)
            # 仍按 input_schema 做一次顶层校验（若有）
            sch = node.effective_input_schema()
            try:
                jsonschema.validate(payload, sch)
            except jsonschema.ValidationError as e:
                return None, f"输入未通过 schema 校验：{e.message}"
            return payload, None

        # 按 inputs 声明逐字段解析
        payload: dict[str, Any] = {}
        for f in node.inputs:
            found, value = self._resolve_input(f, upstream_order)
            if not found:
                if f.required:
                    return None, f"缺少必填输入：{f.label or f.name}（{f.name}）"
                # 非必填字段缺失时不写入
                continue
            # 类型校验
            if f.type in (IOType.TEXT, IOType.DOCUMENT):
                if not isinstance(value, str):
                    return None, f"输入 {f.name} 应为字符串，实际为 {type(value).__name__}"
            elif f.type == IOType.JSON:
                if not isinstance(value, (dict, list, str, int, float, bool)):
                    return None, f"输入 {f.name} 应为合法 JSON 值（object/array/string/number/boolean），实际为 {type(value).__name__}"
                # 子 schema 校验（若声明了 json_schema）
                if f.json_schema:
                    try:
                        jsonschema.validate(value, f.json_schema)
                    except jsonschema.ValidationError as e:
                        return None, f"输入 {f.name} 未通过 schema 校验：{e.message}"
            payload[f.name] = value

        # 顶层再用 effective_input_schema 做一次全量校验（兜住 additionalProperties=False 等规则）
        sch = node.effective_input_schema()
        try:
            jsonschema.validate(payload, sch)
        except jsonschema.ValidationError as e:
            return None, f"输入未通过 schema 校验：{e.message}"
        return payload, None

    def _build_runtime(self, node: Node) -> AgentRuntime:
        """为一个 agent 节点构造 AgentRuntime。"""
        system_prompt = _safe_render(node.prompt, self.variables)
        model = node.llm_config.model if node.llm_config is not None else None
        temperature = node.llm_config.temperature if node.llm_config is not None else None
        max_tokens = node.llm_config.max_tokens if node.llm_config is not None else None
        return AgentRuntime(
            llm_provider=self.llm_provider,
            skill_registry=self._filtered_registry(node),
            system_prompt=system_prompt,
            output_schema=node.effective_output_schema(),
            on_event=self._emit,
            max_iterations=self.max_iterations,
            max_retries=self.max_retries,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            on_trace=self._emit_trace,
            context_compressor=(
                _build_context_compressor(self.context_compression_config)
                if self.context_compression_config is not None
                else None
            ),
        )

    async def run(self) -> dict:
        """执行主循环，按线性顺序推进各节点。"""
        self._emit(TaskStarted(task_id=self.task_id, sop_id=self.template.id, variables=self.variables))
        order = self.template.linear_order()
        last_output: Any = None
        for node_id in order:
            if self._paused:
                break
            self._current_node_id = node_id
            node = self.template.get_node(node_id)
            if node is None:
                continue
            state = self.node_states[node_id]
            if state.status in (NodeStatus.COMPLETED, NodeStatus.SKIPPED):
                if state.output is not None:
                    last_output = state.output
                continue
            # 标记进入运行态
            self._emit(NodeStatusChanged(task_id=self.task_id, node_id=node_id, status=NodeStatus.RUNNING))
            state.status = NodeStatus.RUNNING
            state.attempts += 1
            trigger = "user_correction" if state.prompt_override else "normal"
            self._mark_attempt_started(state, trigger)

            # ---- 运行前：装配并校验输入 ----
            input_payload, err = self._build_node_input(node)
            if err is not None:
                state.status = NodeStatus.FAILED
                state.error = err
                self._mark_attempt_failed(state, err)
                self._emit(NodeFailed(task_id=self.task_id, node_id=node_id, error=err))
                self._paused = True
                return self._snapshot()
            state.input = input_payload
            self._mark_attempt_input(state, input_payload)

            done = await self._run_node(node, state, input_payload)
            if not done:
                return self._snapshot()

            # ---- 运行后：输出字段级校验 ----
            out_err = node.validate_output_fields(state.output)
            if out_err is not None:
                state.status = NodeStatus.FAILED
                state.error = out_err
                self._mark_attempt_failed(state, out_err)
                self._emit(NodeFailed(task_id=self.task_id, node_id=node_id, error=out_err))
                self._paused = True
                return self._snapshot()
            # 顶层再按 effective_output_schema 校验一次（兜住 additionalProperties 等）
            if isinstance(state.output, dict):
                try:
                    jsonschema.validate(state.output, node.effective_output_schema())
                except jsonschema.ValidationError as e:
                    msg = f"输出未通过 schema 校验：{e.message}"
                    state.status = NodeStatus.FAILED
                    state.error = msg
                    self._mark_attempt_failed(state, msg)
                    self._emit(NodeFailed(task_id=self.task_id, node_id=node_id, error=msg))
                    self._paused = True
                    return self._snapshot()

            # 节点成功完成
            state.status = NodeStatus.COMPLETED
            self._mark_attempt_completed(state)
            self._emit(NodeStatusChanged(task_id=self.task_id, node_id=node_id, status=NodeStatus.COMPLETED))
            if isinstance(state.output, dict):
                self.variables[node_id] = state.output
            last_output = state.output
        if not self._paused:
            self._emit(TaskCompleted(task_id=self.task_id, final_output=last_output))
        return self._snapshot()

    async def _run_node(self, node: Node, state: NodeState, input_payload: dict[str, Any]) -> bool:
        """按节点类型分派执行。

        :return: True 表示节点产出了合法结果（可能仍要被外层做 output 校验）；False 表示已暂停/失败。
        """
        if node.type == NodeType.AGENT:
            return await self._run_agent_node(node, state, input_payload)
        if node.type == NodeType.HUMAN:
            interaction_id = f"int-{self.task_id}-{node.id}-{state.attempts}"
            state.pending_interaction_id = interaction_id
            self._emit(
                InteractionRequested(
                    task_id=self.task_id,
                    node_id=node.id,
                    interaction_id=interaction_id,
                    attempt_no=state.attempts,
                    prompt=node.description or node.name or "请确认是否继续执行。",
                    input_schema={"type": "object"},
                    options=[
                        {"label": "确认继续", "value": True},
                        {"label": "拒绝", "value": False},
                    ],
                    multi_select=False,
                )
            )
            self._emit(NodeWaitingInput(task_id=self.task_id, node_id=node.id, reason="human_required"))
            state.status = NodeStatus.WAITING_INPUT
            self._paused = True
            return False
        if node.type == NodeType.SKILL:
            return await self._run_skill_node(node, state, input_payload)
        if node.type == NodeType.COMPOSITE:
            return await self._run_composite_node(node, state, input_payload)
        state.status = NodeStatus.FAILED
        state.error = f"Unknown node type: {node.type}"
        self._mark_attempt_failed(state, state.error)
        self._emit(NodeFailed(task_id=self.task_id, node_id=node.id, error=state.error))
        self._paused = True
        return False

    async def _run_composite_node(self, node: Node, state: NodeState, input_payload: dict[str, Any]) -> bool:
        """执行 composite 父节点：先生成草案并暂停，确认后委托 SubFlowExecutor。"""
        draft = self.subflow_drafts.get(node.id)
        if draft is None:
            draft = await self._generate_subflow_draft(node, input_payload)
            self.subflow_drafts[node.id] = draft
            state.status = NodeStatus.WAITING_INPUT
            state.subflow_status = "waiting_subflow_confirm"
            state.focus_path = [node.id]
            self._paused = True
            self._emit(
                SubFlowDraftCreated(
                    task_id=self.task_id,
                    node_id=node.id,
                    draft=draft.model_dump(mode="json"),
                )
            )
            self._emit(NodeWaitingInput(task_id=self.task_id, node_id=node.id, reason="subflow_confirm_required"))
            return False

        if draft.status != "confirmed":
            state.status = NodeStatus.WAITING_INPUT
            state.subflow_status = "waiting_subflow_confirm"
            state.focus_path = [node.id]
            self._paused = True
            self._emit(NodeWaitingInput(task_id=self.task_id, node_id=node.id, reason="subflow_confirm_required"))
            return False

        subflow = self.subflow_executors.get(node.id)
        if subflow is None:
            state.status = NodeStatus.FAILED
            state.error = f"Subflow executor not found: {node.id}"
            self._mark_attempt_failed(state, state.error)
            self._emit(NodeFailed(task_id=self.task_id, node_id=node.id, error=state.error))
            self._paused = True
            return False

        state.subflow_status = "running_subflow"
        state.focus_path = [node.id]
        try:
            output = await subflow.run()
        except Exception as e:
            state.status = NodeStatus.WAITING_INPUT
            state.error = f"{type(e).__name__}: {e}"
            state.subflow_status = "waiting_input"
            state.focus_path = [node.id]
            self._emit(NodeWaitingInput(task_id=self.task_id, node_id=node.id, reason=state.error))
            self._paused = True
            return False
        state.output = output
        state.subflow_status = "completed"
        return True

    async def _generate_subflow_draft(self, node: Node, input_payload: dict[str, Any]) -> SubFlowDraft:
        """调用现有 AgentRuntime 生成 composite 节点的子流程草案。"""
        planner = Node(
            id=f"{node.id}_planner",
            name=f"{node.name} 子流程规划",
            type=NodeType.AGENT,
            prompt=node.subflow_prompt or node.prompt,
            outputs=[],
        )
        runtime = self._build_runtime(planner)
        ctx = AgentContext(task_id=self.task_id, node_id=node.id, variables=dict(self.variables))
        rendered_input = json.dumps(input_payload, ensure_ascii=False, default=str)
        result = await runtime.run(rendered_input, ctx)
        result = result or {"nodes": [], "edges": []}
        nodes = [Node.model_validate(item) for item in result.get("nodes", [])]
        edges = [Edge.model_validate(item) for item in result.get("edges", [])]
        return SubFlowDraft(
            parent_node_id=node.id,
            draft_nodes=nodes,
            draft_edges=edges,
            generated_by="agent",
            created_at=datetime.now(timezone.utc).isoformat(),
        )

    async def _run_agent_node(self, node: Node, state: NodeState, input_payload: dict[str, Any]) -> bool:
        """执行 agent 节点：构造上下文与运行时并跑一次 ReAct 循环。"""
        ctx = AgentContext(task_id=self.task_id, node_id=node.id, variables=dict(self.variables))
        runtime = self._build_runtime(node)
        # 用结构化 input_payload 喂给 agent（替代过去的全量 variables 字符串）
        rendered_input = json.dumps(input_payload, ensure_ascii=False, default=str)
        prompt_override = state.prompt_override
        if prompt_override:
            # 补充指令只作用于本次 attempt，并以高优先级置于原始节点提示词之前。
            rendered_prompt = _safe_render(node.prompt, self.variables)
            prompt_override = (
                "高优先级用户补充指令：\n"
                f"{prompt_override}\n\n"
                "原始节点提示词：\n"
                f"{rendered_prompt}"
            )
        result = await runtime.run(rendered_input, ctx, prompt_override=prompt_override)
        if result is None:
            state.status = NodeStatus.WAITING_INPUT
            self._paused = True
            return False
        state.output = result
        return True

    async def _run_skill_node(self, node: Node, state: NodeState, input_payload: dict[str, Any]) -> bool:
        """执行 skill 节点：直接调用注册中心里的技能，并用 output schema 校验结果。"""
        skill = self.skill_registry.get(node.skill_name)
        if skill is None:
            state.status = NodeStatus.FAILED
            state.error = f"Skill not found: {node.skill_name}"
            self._mark_attempt_failed(state, state.error)
            self._emit(NodeFailed(task_id=self.task_id, node_id=node.id, error=state.error))
            self._paused = True
            return False
        skill_ctx = SkillContext(
            task_id=self.task_id,
            node_id=node.id,
            variables=self.variables,
            emit_event=lambda e: None,
        )
        # 以装配好的 input_payload 作为技能入参
        args = input_payload
        self._emit(SkillCalled(task_id=self.task_id, node_id=node.id, skill_name=node.skill_name, args=args))
        try:
            result = await skill.execute(args, skill_ctx)
        except Exception as e:
            state.status = NodeStatus.FAILED
            state.error = f"{type(e).__name__}: {e}"
            self._mark_attempt_failed(state, state.error)
            self._emit(NodeFailed(task_id=self.task_id, node_id=node.id, error=state.error))
            self._paused = True
            return False
        self._emit(SkillReturned(task_id=self.task_id, node_id=node.id, skill_name=node.skill_name, result=result))
        # 注：字段级和 schema 级校验在外层 run() 统一做，这里只把结果写入 state
        state.output = result
        self._emit(NodeCompleted(task_id=self.task_id, node_id=node.id, output=result))
        return True

    def confirm_subflow(self, node_id: str, nodes: list[Node], edges: list[Edge]) -> None:
        """确认 composite 子流程草案，并准备后续 run() 执行内部 DAG。"""
        draft = self.subflow_drafts.get(node_id)
        if draft is None:
            draft = SubFlowDraft(
                parent_node_id=node_id,
                draft_nodes=nodes,
                draft_edges=edges,
                generated_by="user",
                created_at=datetime.now(timezone.utc).isoformat(),
            )
            self.subflow_drafts[node_id] = draft

        draft.draft_nodes = nodes
        draft.draft_edges = edges
        draft.status = "confirmed"
        parent_node = self.template.get_node(node_id)
        max_parallelism = (
            parent_node.subflow_policy.max_parallelism if parent_node and parent_node.subflow_policy else 3
        )
        self.subflow_executors[node_id] = SubFlowExecutor(
            task_id=self.task_id,
            parent_node_id=node_id,
            nodes=nodes,
            edges=edges,
            variables=dict(self.variables),
            run_node=self._run_subnode_once,
            emit=self._emit,
            max_parallelism=max_parallelism,
        )
        state = self.node_states[node_id]
        state.status = NodeStatus.PENDING
        state.error = None
        state.output = None
        state.subflow_status = "running_subflow"
        state.focus_path = [node_id]
        self._paused = False
        self._emit(SubFlowConfirmed(task_id=self.task_id, node_id=node_id))

    async def _run_subnode_once(self, node: Node, input_payload: dict[str, Any], retry_prompt: Optional[str]) -> Any:
        """局部执行一个子节点，不注册到父 node_states，也不改写父暂停状态。"""
        state = NodeState(node_id=node.id, prompt_override=retry_prompt)
        if node.type == NodeType.AGENT:
            result = await self._run_subnode_agent(node, state, input_payload)
        elif node.type == NodeType.SKILL:
            result = await self._run_subnode_skill(node, input_payload)
        elif node.type == NodeType.HUMAN:
            raise RuntimeError(f"Subnode {node.id} requires human input")
        else:
            raise RuntimeError(f"Unsupported subnode type: {node.type}")

        out_err = node.validate_output_fields(result)
        if out_err is not None:
            raise RuntimeError(out_err)
        if isinstance(result, dict):
            try:
                jsonschema.validate(result, node.effective_output_schema())
            except jsonschema.ValidationError as e:
                raise RuntimeError(f"输出未通过 schema 校验：{e.message}") from e
        return result

    async def _run_subnode_agent(self, node: Node, state: NodeState, input_payload: dict[str, Any]) -> Any:
        """执行 agent 子节点；失败时抛错而不是暂停父执行器。"""
        ctx = AgentContext(task_id=self.task_id, node_id=node.id, variables=dict(self.variables))
        runtime = self._build_runtime(node)
        rendered_input = json.dumps(input_payload, ensure_ascii=False, default=str)
        result = await runtime.run(rendered_input, ctx, prompt_override=state.prompt_override)
        if result is None:
            raise RuntimeError(f"Subnode {node.id} did not complete")
        return result

    async def _run_subnode_skill(self, node: Node, input_payload: dict[str, Any]) -> Any:
        """执行 skill 子节点；复用技能注册中心但不影响父节点状态。"""
        skill = self.skill_registry.get(node.skill_name)
        if skill is None:
            raise RuntimeError(f"Skill not found: {node.skill_name}")
        skill_ctx = SkillContext(
            task_id=self.task_id,
            node_id=node.id,
            variables=self.variables,
            emit_event=lambda e: None,
        )
        self._emit(
            SkillCalled(task_id=self.task_id, node_id=node.id, skill_name=node.skill_name, args=input_payload)
        )
        result = await skill.execute(input_payload, skill_ctx)
        self._emit(SkillReturned(task_id=self.task_id, node_id=node.id, skill_name=node.skill_name, result=result))
        self._emit(NodeCompleted(task_id=self.task_id, node_id=node.id, output=result))
        return result

    def _snapshot(self) -> dict:
        return {
            "task_id": self.task_id,
            "sop_id": self.template.id,
            "current_node": self._current_node_id,
            "paused": self._paused,
            "variables": self.variables,
            "nodes": {nid: state.model_dump() for nid, state in self.node_states.items()},
            "subflows": self._subflows_snapshot(),
        }

    def _subflows_snapshot(self) -> dict[str, dict]:
        """返回 composite 父节点 ID -> 子流程运行快照。"""
        snapshots: dict[str, dict] = {}
        for node_id, draft in self.subflow_drafts.items():
            parent_state = self.node_states.get(node_id)
            subflow = self.subflow_executors.get(node_id)
            status = draft.status
            if parent_state is not None and parent_state.subflow_status == "completed":
                status = "completed"
            elif parent_state is not None and parent_state.subflow_status == "running_subflow":
                status = "running"
            snapshots[node_id] = {
                "parent_node_id": node_id,
                "status": status,
                "draft": draft.model_dump(mode="json"),
                "nodes": {
                    sid: state.model_dump(mode="json")
                    for sid, state in (subflow.node_states.items() if subflow is not None else [])
                },
            }
        return snapshots

    def pause(self) -> None:
        self._paused = True

    def resume(self) -> None:
        self._paused = False

    def _workflow_order(self) -> list[str]:
        """返回主 SOP 的线性执行顺序，兼容新旧模板实现。"""
        linear_order = getattr(self.template, "linear_order", None)
        if linear_order is not None:
            return linear_order()

        cur = self.template.entry_node
        if cur is None:
            return [node.id for node in self.template.nodes]
        order: list[str] = []
        while cur is not None:
            if cur in order:
                break
            order.append(cur)
            next_nodes = self.template.get_next_nodes(cur)
            if len(next_nodes) > 1:
                raise ValueError(f"Linear SOP required; node {cur} has multiple outgoing edges")
            cur = next_nodes[0] if next_nodes else None
        return order

    def downstream_nodes(self, node_id: str) -> list[str]:
        """返回指定节点的所有下游后代，按模板线性顺序去重。"""
        children: dict[str, list[str]] = {}
        for edge in self.template.edges:
            children.setdefault(edge.from_node, []).append(edge.to)

        seen: set[str] = set()
        stack = list(children.get(node_id, []))
        while stack:
            current = stack.pop(0)
            if current in seen:
                continue
            seen.add(current)
            stack.extend(children.get(current, []))

        order = self._workflow_order()
        return [nid for nid in order if nid in seen]

    def rerun_node_with_instruction(
        self,
        node_id: str,
        supplemental_instruction: str,
        invalidate_downstream: bool = True,
    ) -> list[str]:
        """用用户补充指令重跑主节点，并让下游节点失效。"""
        if node_id not in self.node_states:
            raise ValueError(f"Node not found: {node_id}")
        instruction = supplemental_instruction.strip()
        if not instruction:
            raise ValueError("supplemental_instruction is required")

        current = self.node_states[node_id]
        next_attempt = current.attempts + 1
        downstream = self.downstream_nodes(node_id) if invalidate_downstream else []
        invalidated = [node_id, *downstream]

        current.prompt_override = instruction
        current.status = NodeStatus.PENDING
        current.output = None
        current.error = None
        current.input = None
        current.stale = False
        current.stale_reason = None
        current.pending_interaction_id = None
        current.attempt_history.append(
            {
                "attempt_no": next_attempt,
                "trigger": "user_correction",
                "supplemental_instruction": instruction,
                "status": "pending",
            }
        )

        for downstream_id in downstream:
            state = self.node_states[downstream_id]
            state.status = NodeStatus.PENDING
            state.output = None
            state.error = None
            state.input = None
            state.stale = True
            state.stale_reason = "upstream_rerun"
            state.pending_interaction_id = None

        for invalidated_id in invalidated:
            self.variables.pop(invalidated_id, None)

        self._paused = False
        self._emit(
            NodeRetryRequested(
                task_id=self.task_id,
                node_id=node_id,
                attempt_no=next_attempt,
                supplemental_instruction=instruction,
                invalidate_downstream=invalidate_downstream,
                invalidated_node_ids=invalidated,
            )
        )
        self._emit(
            NodeSupplementalInstructionAdded(
                task_id=self.task_id,
                node_id=node_id,
                attempt_no=next_attempt,
                supplemental_instruction=instruction,
            )
        )
        if downstream:
            self._emit(
                DownstreamInvalidated(
                    task_id=self.task_id,
                    node_id=node_id,
                    invalidated_node_ids=downstream,
                    reason="upstream_rerun",
                )
            )
        for downstream_id in downstream:
            self._emit(
                NodeMarkedStale(
                    task_id=self.task_id,
                    node_id=downstream_id,
                    reason="upstream_rerun",
                    upstream_node_id=node_id,
                )
            )
        return invalidated

    def retry_node(self, node_id: str) -> None:
        order = self.template.linear_order()
        if node_id in order:
            idx = order.index(node_id)
            for nid in order[idx:]:
                state = self.node_states[nid]
                state.status = NodeStatus.PENDING
                state.output = None
                state.error = None
                state.input = None
        # 重试后清空该节点及其下游在 variables 中的命名空间
        if node_id in self.variables:
            del self.variables[node_id]
        self._paused = False

    def skip_node(self, node_id: str) -> None:
        state = self.node_states[node_id]
        state.status = NodeStatus.SKIPPED
        self._emit(NodeStatusChanged(task_id=self.task_id, node_id=node_id, status=NodeStatus.SKIPPED))
        self._paused = False

    def intervene(self, node_id: str, action: str, data: dict) -> None:
        self._emit(UserIntervened(task_id=self.task_id, node_id=node_id, action=action, data=data))
        if action == "retry":
            self.retry_node(node_id)
            return
        if action == "edit_prompt":
            self.node_states[node_id].prompt_override = data.get("prompt")
            self.retry_node(node_id)
            return
        if action == "provide_output":
            state = self.node_states[node_id]
            output = data.get("output")
            state.output = output
            state.status = NodeStatus.COMPLETED
            if isinstance(output, dict):
                self.variables[node_id] = output
            self._paused = False
            return
        if action == "provide_input":
            provided = data.get("input")
            if isinstance(provided, dict):
                self.variables.update(provided)
            self.retry_node(node_id)
            return
        if action == "skip":
            self.skip_node(node_id)
            return
