"""TaskManager：编排 executor ↔ storage ↔ eventbus 的任务管理器。

负责按 SOP 模板启动工作流任务：为任务创建工作区、构造并驱动
WorkflowExecutor，把执行器发射的事件同步落盘（events.jsonl）并经由
EventBus 发布出去，同时在任务结束时把元信息状态流转为
completed / waiting_input / failed。还提供人工干预、快照查询与任务列举能力。
"""

import asyncio
from typing import Optional

import jsonschema

from symphony.agent.events import (
    Event,
    InteractionAnswered,
    NodeStatus,
    NodeStatusChanged,
    SubFlowRejected,
    UserIntervened,
)
from symphony.ai.provider import LLMProvider
from symphony.config import ContextCompressionConfig
from symphony.server.eventbus import EventBus
from symphony.skills.builtins import register_builtins
from symphony.skills.registry import SkillRegistry
from symphony.storage.workspace import Workspace, WorkspaceManager
from symphony.workflow.dag_log import build_dag_log
from symphony.workflow.executor import WorkflowExecutor
from symphony.workflow.models import Edge, Node
from symphony.workflow.template import TemplateLoader


class TaskManager:
    """任务生命周期管理器，串联执行器、存储与事件总线。"""

    def __init__(
        self,
        template_loader: TemplateLoader,
        workspace_manager: WorkspaceManager,
        llm_provider: LLMProvider,
        event_bus: EventBus,
        skill_registry: Optional[SkillRegistry] = None,
        agent_max_iterations: int = 20,
        agent_max_retries: int = 3,
        context_compression_config: ContextCompressionConfig | None = None,
    ) -> None:
        """初始化任务管理器，保存依赖并准备内存态。"""
        # SOP 模板加载器
        self.template_loader = template_loader
        # 工作区管理器
        self.workspace_manager = workspace_manager
        # 大模型服务提供方
        self.llm_provider = llm_provider
        # 事件总线
        self.event_bus = event_bus
        # 技能注册中心；未提供时新建并注册全部内置技能
        if skill_registry is None:
            skill_registry = SkillRegistry()
            register_builtins(skill_registry)
        self.skill_registry = skill_registry
        # SOP Agent 运行参数，来自配置；默认值保持旧行为兼容。
        self.agent_max_iterations = agent_max_iterations
        self.agent_max_retries = agent_max_retries
        self.context_compression_config = context_compression_config
        # task_id -> 执行器实例
        self._executors: dict[str, WorkflowExecutor] = {}
        # task_id -> 后台运行的 asyncio 任务
        self._tasks: dict[str, asyncio.Task] = {}

    def _make_event_handler(self, workspace: Workspace):
        """构造执行器的同步事件回调：落盘 events.jsonl 并经总线发布。"""

        def handler(event: Event) -> None:
            """把一条执行器事件序列化后落盘并发布。"""
            # 序列化为字典（剔除 None 字段）
            data = event.to_dict()
            # 追加写入事件日志（真相来源）
            workspace.event_log.append(data)
            # 经事件总线发布给订阅者
            self.event_bus.publish(workspace.task_id, data)

        return handler

    def _make_trace_handler(self, workspace: Workspace):
        """构造执行器的 LLM 轨迹回调：把每次 LLM 调用追加写入 traces.jsonl。"""

        def handler(trace: dict) -> None:
            """把一条 LLM 调用轨迹落盘到工作区的 traces.jsonl。"""
            workspace.trace_log.append(trace)

        return handler

    def _persist_final(self, workspace: Workspace, snapshot: dict) -> None:
        """任务一次运行结束后持久化状态快照并流转 meta 状态。"""
        # 落盘状态快照
        workspace.save_state(snapshot)
        # 读取元信息（不存在则跳过状态流转）
        meta = workspace.load_meta()
        if meta is None:
            return
        # 暂停表示等待人工输入，否则视为已完成
        meta.status = "waiting_input" if snapshot.get("paused") else "completed"
        # 同步当前节点位置
        meta.current_node = snapshot.get("current_node")
        # 写回元信息
        workspace.save_meta(meta)

    def _mark_failed(self, workspace: Workspace, error: str) -> None:
        """任务运行异常时把 meta 标记为 failed 并发布 task_failed 事件。"""
        # 读取元信息并写入失败状态
        meta = workspace.load_meta()
        if meta is not None:
            meta.status = "failed"
            meta.error = error
            workspace.save_meta(meta)
        # 组装一条 task_failed 事件字典，落盘并发布
        data = {"type": "task_failed", "task_id": workspace.task_id, "error": error}
        workspace.event_log.append(data)
        self.event_bus.publish(workspace.task_id, data)

    def _spawn_run(self, task_id: str, executor: WorkflowExecutor, workspace: Workspace) -> None:
        """创建后台 asyncio 任务运行执行器并持久化结果。"""

        async def _run() -> None:
            """后台协程：跑一次执行器并落盘/流转状态。"""
            # 运行边界：执行器异常时把任务标记为失败
            try:
                snapshot = await executor.run()
            except Exception as e:
                self._mark_failed(workspace, f"{type(e).__name__}: {e}")
                return
            # 正常结束：持久化快照并流转 meta 状态
            self._persist_final(workspace, snapshot)

        # 保存后台任务句柄，便于外部 await 或取消
        self._tasks[task_id] = asyncio.create_task(_run())

    async def start_task(
        self,
        sop_id: str,
        variables: dict,
        task_id: Optional[str] = None,
    ) -> str:
        """按 SOP 启动一个新任务，返回其 task_id。"""
        # 加载模板，缺失则报错
        template = self.template_loader.load(sop_id)
        if template is None:
            raise ValueError(f"SOP not found: {sop_id}")
        # 校验启动时的工作流输入变量（若模板声明了 typed variables_def 或 variables schema）
        try:
            jsonschema.validate(variables, template.effective_variables_schema())
        except jsonschema.ValidationError as e:
            raise ValueError(f"工作流输入变量未通过校验：{e.message}")
        # 创建任务工作区（写入初始 meta）
        workspace = self.workspace_manager.create(
            sop_id, variables, task_id=task_id, sop_name=template.name
        )
        # 构造事件回调
        handler = self._make_event_handler(workspace)
        # 构造 LLM 轨迹回调
        trace_handler = self._make_trace_handler(workspace)
        # 构造执行器
        executor = WorkflowExecutor(
            task_id=workspace.task_id,
            template=template,
            variables=variables,
            llm_provider=self.llm_provider,
            skill_registry=self.skill_registry,
            on_event=handler,
            on_trace=trace_handler,
            max_iterations=self.agent_max_iterations,
            max_retries=self.agent_max_retries,
            context_compression_config=self.context_compression_config,
        )
        # 登记执行器并启动后台运行
        self._executors[workspace.task_id] = executor
        self._spawn_run(workspace.task_id, executor, workspace)
        return workspace.task_id

    def get_executor(self, task_id: str) -> Optional[WorkflowExecutor]:
        """按 task_id 获取内存中的执行器，不存在返回 None。"""
        return self._executors.get(task_id)

    async def intervene(self, task_id: str, node_id: str, action: str, data: dict) -> None:
        """对指定任务施加一次人工干预并重新调度继续执行。"""
        # 取出执行器，缺失则报错
        executor = self._executors.get(task_id)
        if executor is None:
            raise ValueError(f"Task not found: {task_id}")
        # 施加干预（内部完成 resume/retry/跳过等）
        executor.intervene(node_id, action, data)
        # 重新获取工作区（干预后需继续持久化）
        workspace = self.workspace_manager.get(task_id)
        # 重新调度后台运行继续推进（已完成节点会被执行器跳过）
        self._spawn_run(task_id, executor, workspace)

    async def rerun_node(
        self,
        task_id: str,
        node_id: str,
        supplemental_instruction: str,
        invalidate_downstream: bool = True,
    ) -> dict:
        """追加补充指令重跑主节点，并重新调度任务。"""
        executor = self._require_executor(task_id)
        invalidated = executor.rerun_node_with_instruction(
            node_id,
            supplemental_instruction=supplemental_instruction,
            invalidate_downstream=invalidate_downstream,
        )
        workspace = self._require_workspace(task_id)
        workspace.save_state(executor._snapshot())
        self._spawn_run(task_id, executor, workspace)
        return {
            "ok": True,
            "attempt_no": executor.node_states[node_id].attempts + 1,
            "invalidated_node_ids": invalidated,
        }

    def pending_interactions(self, task_id: str) -> list[dict]:
        """从事件日志中汇总当前仍未回答的 interaction。"""
        workspace = self._require_workspace(task_id)
        events = workspace.event_log.read_all()
        answered = {
            event.get("interaction_id")
            for event in events
            if event.get("type") == "interaction_answered"
        }
        return [
            event
            for event in events
            if event.get("type") == "interaction_requested"
            and event.get("interaction_id") not in answered
        ]

    async def answer_interaction(self, task_id: str, interaction_id: str, answer: dict) -> None:
        """回答 pending interaction，并恢复任务调度。"""
        executor = self._require_executor(task_id)
        target_node_id = None
        target_state = None
        for node_id, state in executor.node_states.items():
            if state.pending_interaction_id == interaction_id:
                target_node_id = node_id
                target_state = state
                break
        if target_node_id is None or target_state is None:
            raise ValueError(f"Interaction not found: {interaction_id}")

        target_state.pending_interaction_id = None
        target_state.status = NodeStatus.COMPLETED
        target_state.output = answer
        target_state.error = None
        target_state.stale = False
        target_state.stale_reason = None
        if target_state.attempt_history:
            target_state.attempt_history[-1]["status"] = "completed"
            target_state.attempt_history[-1]["output"] = answer
            target_state.attempt_history[-1]["error"] = None
        executor.variables[target_node_id] = answer
        executor._emit(
            InteractionAnswered(
                task_id=task_id,
                node_id=target_node_id,
                interaction_id=interaction_id,
                attempt_no=target_state.attempts,
                answer=answer,
            )
        )
        executor._emit(
            NodeStatusChanged(
                task_id=task_id,
                node_id=target_node_id,
                status=NodeStatus.COMPLETED,
            )
        )
        executor.resume()
        workspace = self._require_workspace(task_id)
        workspace.save_state(executor._snapshot())
        self._spawn_run(task_id, executor, workspace)

    def _require_executor(self, task_id: str) -> WorkflowExecutor:
        """获取内存执行器；不存在时抛出可被 API 层转为 404 的错误。"""
        # 子流程干预必须作用于当前内存执行器，磁盘快照不能恢复运行闭包
        executor = self._executors.get(task_id)
        if executor is None:
            raise ValueError(f"Task not found: {task_id}")
        return executor

    def _require_workspace(self, task_id: str) -> Workspace:
        """获取任务工作区；不存在时抛出可被 API 层转为 404 的错误。"""
        # 所有子流程干预后都要同步持久化 draft/state
        workspace = self.workspace_manager.get(task_id)
        if workspace is None:
            raise ValueError(f"Task not found: {task_id}")
        return workspace

    def _require_subflow(self, executor: WorkflowExecutor, node_id: str):
        """获取指定 composite 节点的子流程执行器；不存在时抛 ValueError。"""
        # 按产品语义，不存在的 subflow executor 对外表现为 404
        subflow = executor.subflow_executors.get(node_id)
        if subflow is None:
            raise ValueError(f"Subflow executor not found: {node_id}")
        return subflow

    def _persist_subflow_state(self, workspace: Workspace, node_id: str, subflow) -> None:
        """把子流程运行状态落盘到 subflows/<node_id>/state.json。"""
        # 保存节点状态与子流程变量，供 UI 刷新或后续离线排查
        workspace.save_subflow_state(
            node_id,
            {
                "parent_node_id": node_id,
                "nodes": {k: v.model_dump(mode="json") for k, v in subflow.node_states.items()},
                "variables": subflow.variables,
            },
        )

    def _prepare_parent_for_subflow_resume(self, executor: WorkflowExecutor, node_id: str) -> None:
        """把父 composite 节点恢复到可被 _spawn_run 继续执行的状态。"""
        # 子流程干预后，父节点必须回到 pending/resumed，下一次 run 才会进入 _run_composite_node
        state = executor.node_states[node_id]
        state.status = NodeStatus.PENDING
        state.error = None
        state.output = None
        state.subflow_status = "running_subflow"
        state.focus_path = [node_id]
        executor.variables.pop(node_id, None)
        executor.resume()

    async def confirm_subflow(self, task_id: str, node_id: str, nodes: list[dict], edges: list[dict]) -> None:
        """确认 composite 子流程草案，持久化草案后恢复任务执行。"""
        executor = self._require_executor(task_id)
        if node_id not in executor.node_states:
            raise ValueError(f"Node not found: {node_id}")
        parsed_nodes = [Node.model_validate(item) for item in nodes]
        parsed_edges = [Edge.model_validate(item) for item in edges]

        executor._emit(
            UserIntervened(
                task_id=task_id,
                node_id=node_id,
                action="confirm_subflow",
                data={"nodes": nodes, "edges": edges},
            )
        )
        executor.confirm_subflow(node_id, parsed_nodes, parsed_edges)

        workspace = self._require_workspace(task_id)
        workspace.save_subflow_draft(node_id, executor.subflow_drafts[node_id].model_dump(mode="json"))
        self._persist_subflow_state(workspace, node_id, executor.subflow_executors[node_id])
        self._spawn_run(task_id, executor, workspace)

    async def reject_subflow(self, task_id: str, node_id: str, reason: str) -> None:
        """拒绝 composite 子流程草案，并保持父节点等待用户继续处理。"""
        executor = self._require_executor(task_id)
        if node_id not in executor.node_states:
            raise ValueError(f"Node not found: {node_id}")

        executor._emit(
            UserIntervened(
                task_id=task_id,
                node_id=node_id,
                action="reject_subflow",
                data={"reason": reason},
            )
        )
        executor._emit(SubFlowRejected(task_id=task_id, node_id=node_id, reason=reason))

        draft = executor.subflow_drafts.get(node_id)
        if draft is not None:
            draft.status = "rejected"

        state = executor.node_states[node_id]
        state.status = NodeStatus.WAITING_INPUT
        state.error = reason or "Subflow rejected"
        state.subflow_status = "waiting_subflow_confirm"
        state.focus_path = [node_id]
        executor.pause()

        workspace = self._require_workspace(task_id)
        if draft is not None:
            workspace.save_subflow_draft(node_id, draft.model_dump(mode="json"))
        workspace.save_state(executor._snapshot())

    async def retry_subnode(self, task_id: str, node_id: str, sub_node_id: str, retry_prompt: str) -> None:
        """带提示词重跑一个子节点，并重新调度父 composite 节点。"""
        executor = self._require_executor(task_id)
        subflow = self._require_subflow(executor, node_id)
        if sub_node_id not in subflow.node_states:
            raise ValueError(f"Subnode not found: {sub_node_id}")

        executor._emit(
            UserIntervened(
                task_id=task_id,
                node_id=node_id,
                action="retry_subnode",
                data={"sub_node_id": sub_node_id, "retry_prompt": retry_prompt},
            )
        )
        subflow.retry_subnode(sub_node_id, retry_prompt)
        self._prepare_parent_for_subflow_resume(executor, node_id)

        workspace = self._require_workspace(task_id)
        self._persist_subflow_state(workspace, node_id, subflow)
        self._spawn_run(task_id, executor, workspace)

    async def provide_subnode_output(self, task_id: str, node_id: str, sub_node_id: str, output: dict) -> None:
        """人工提供子节点输出，并将其下游标记为待重算。"""
        executor = self._require_executor(task_id)
        subflow = self._require_subflow(executor, node_id)
        if sub_node_id not in subflow.node_states:
            raise ValueError(f"Subnode not found: {sub_node_id}")

        executor._emit(
            UserIntervened(
                task_id=task_id,
                node_id=node_id,
                action="provide_subnode_output",
                data={"sub_node_id": sub_node_id, "output": output},
            )
        )
        state = subflow.node_states[sub_node_id]
        state.output = output
        state.status = NodeStatus.COMPLETED
        state.error = None
        state.stale = False
        subflow.variables[sub_node_id] = output

        for downstream_id in subflow.downstream(sub_node_id):
            downstream_state = subflow.node_states[downstream_id]
            downstream_state.status = NodeStatus.PENDING
            downstream_state.output = None
            downstream_state.error = None
            downstream_state.stale = True
            subflow.variables.pop(downstream_id, None)

        self._prepare_parent_for_subflow_resume(executor, node_id)
        workspace = self._require_workspace(task_id)
        self._persist_subflow_state(workspace, node_id, subflow)
        self._spawn_run(task_id, executor, workspace)

    async def skip_subnode(self, task_id: str, node_id: str, sub_node_id: str) -> None:
        """跳过指定子节点，并重新调度父 composite 节点。"""
        executor = self._require_executor(task_id)
        subflow = self._require_subflow(executor, node_id)
        if sub_node_id not in subflow.node_states:
            raise ValueError(f"Subnode not found: {sub_node_id}")

        executor._emit(
            UserIntervened(
                task_id=task_id,
                node_id=node_id,
                action="skip_subnode",
                data={"sub_node_id": sub_node_id},
            )
        )
        state = subflow.node_states[sub_node_id]
        state.status = NodeStatus.SKIPPED
        state.error = None
        state.stale = False
        subflow.variables.pop(sub_node_id, None)

        self._prepare_parent_for_subflow_resume(executor, node_id)
        workspace = self._require_workspace(task_id)
        self._persist_subflow_state(workspace, node_id, subflow)
        self._spawn_run(task_id, executor, workspace)

    async def retry_upstreams(
        self,
        task_id: str,
        node_id: str,
        sub_node_ids: list[str],
        retry_prompts: dict[str, str],
    ) -> None:
        """批量重跑多个上游子节点，并只调度一次父 composite。"""
        executor = self._require_executor(task_id)
        subflow = self._require_subflow(executor, node_id)

        executor._emit(
            UserIntervened(
                task_id=task_id,
                node_id=node_id,
                action="retry_upstreams",
                data={"sub_node_ids": sub_node_ids, "retry_prompts": retry_prompts},
            )
        )
        for sub_node_id in sub_node_ids:
            if sub_node_id not in subflow.node_states:
                raise ValueError(f"Subnode not found: {sub_node_id}")
            subflow.retry_subnode(sub_node_id, retry_prompts.get(sub_node_id, ""))

        self._prepare_parent_for_subflow_resume(executor, node_id)
        workspace = self._require_workspace(task_id)
        self._persist_subflow_state(workspace, node_id, subflow)
        self._spawn_run(task_id, executor, workspace)

    def get_dag_log(self, task_id: str) -> dict:
        """返回指定任务的 DAG 化运行日志。"""
        workspace = self._require_workspace(task_id)
        snapshot = self.get_snapshot(task_id) or {}
        meta = workspace.load_meta()
        if meta is None:
            raise ValueError(f"Task not found: {task_id}")
        template = self.template_loader.load(meta.sop_id)
        if template is None:
            raise ValueError(f"SOP not found: {meta.sop_id}")

        events = workspace.event_log.read_all()
        traces = workspace.trace_log.read_all()
        interactions = [
            event
            for event in events
            if str(event.get("type", "")).startswith("interaction_")
        ]
        return build_dag_log(
            task_id,
            template.model_dump(mode="json", by_alias=True),
            snapshot,
            events,
            traces,
            interactions,
        )

    def get_snapshot(self, task_id: str) -> Optional[dict]:
        """获取任务状态快照：优先内存执行器，其次工作区落盘。"""
        # 优先返回内存执行器的实时快照
        executor = self._executors.get(task_id)
        if executor is not None:
            return executor._snapshot()
        # 否则从工作区加载持久化快照
        workspace = self.workspace_manager.get(task_id)
        if workspace is None:
            return None
        return workspace.load_state()

    def list_tasks(self) -> list[dict]:
        """列举所有任务的元信息。"""
        return self.workspace_manager.list_tasks()
