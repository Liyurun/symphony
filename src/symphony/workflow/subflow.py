"""SubFlow 执行器。

用于 composite 节点内部的子流程调度。第一版支持 fan-out/fan-in 与简单串行，
并提供“重跑当前子节点 + 标记下游过期”的核心语义。
"""

import asyncio
from collections import defaultdict, deque
from typing import Any, Awaitable, Callable

from symphony.agent.events import (
    Event,
    NodeStatus,
    SubFlowCompleted,
    SubNodeCompleted,
    SubNodeFailed,
    SubNodeMarkedStale,
    SubNodeRetried,
    SubNodeStarted,
)
from symphony.workflow.models import Edge, Node, RetryPrompt, SubNodeState

RunSubNode = Callable[[Node, dict[str, Any], str | None], Awaitable[Any]]


class SubFlowExecutor:
    """执行 composite 节点内部子流程。"""

    def __init__(
        self,
        task_id: str,
        parent_node_id: str,
        nodes: list[Node],
        edges: list[Edge],
        variables: dict[str, Any],
        run_node: RunSubNode,
        emit: Callable[[Event], None],
        max_parallelism: int = 3,
    ) -> None:
        """初始化子流程执行器并为每个子节点创建运行状态。"""
        self.task_id = task_id
        self.parent_node_id = parent_node_id
        self.nodes = {node.id: node for node in nodes}
        self.edges = edges
        self.variables = variables
        self.run_node = run_node
        self.emit = emit
        self.max_parallelism = max(1, max_parallelism)
        self.node_states = {
            node.id: SubNodeState(node_id=node.id, parent_node_id=parent_node_id) for node in nodes
        }

    def downstream(self, node_id: str) -> list[str]:
        """返回指定子节点的所有下游节点 id。"""
        graph: dict[str, list[str]] = defaultdict(list)
        for edge in self.edges:
            graph[edge.from_node].append(edge.to)

        seen: set[str] = set()
        queue: deque[str] = deque(graph[node_id])
        while queue:
            cur = queue.popleft()
            if cur in seen:
                continue
            seen.add(cur)
            queue.extend(graph[cur])
        return list(seen)

    def retry_subnode(self, node_id: str, retry_prompt: str) -> None:
        """重跑一个子节点，并将它的所有下游输出标记为过期。"""
        state = self.node_states[node_id]
        state.status = NodeStatus.PENDING
        state.output = None
        state.error = None
        state.stale = False
        state.retry_prompts.append(
            RetryPrompt(attempt_no=state.attempts + 1, prompt=retry_prompt, created_at="", created_by="user")
        )
        self.variables.pop(node_id, None)

        invalidated: list[str] = []
        for downstream_id in self.downstream(node_id):
            downstream_state = self.node_states[downstream_id]
            downstream_state.status = NodeStatus.PENDING
            downstream_state.output = None
            downstream_state.error = None
            downstream_state.stale = True
            self.variables.pop(downstream_id, None)
            invalidated.append(downstream_id)
            self.emit(
                SubNodeMarkedStale(
                    task_id=self.task_id,
                    node_id=self.parent_node_id,
                    sub_node_id=downstream_id,
                )
            )

        self.emit(
            SubNodeRetried(
                task_id=self.task_id,
                node_id=self.parent_node_id,
                sub_node_id=node_id,
                retry_prompt=retry_prompt,
                invalidate_downstream=invalidated,
            )
        )

    def _dependencies(self) -> dict[str, set[str]]:
        """构建每个子节点的直接前置依赖集合。"""
        deps = {node_id: set() for node_id in self.nodes}
        for edge in self.edges:
            if edge.to in deps:
                deps[edge.to].add(edge.from_node)
        return deps

    def _ready_nodes(self) -> list[str]:
        """返回当前可执行的 pending 子节点。"""
        deps = self._dependencies()
        ready: list[str] = []
        for node_id, state in self.node_states.items():
            if state.status != NodeStatus.PENDING:
                continue
            if all(self.node_states[dep].status in (NodeStatus.COMPLETED, NodeStatus.SKIPPED) for dep in deps[node_id]):
                ready.append(node_id)
        return ready

    def _input_for(self, node_id: str) -> dict[str, Any]:
        """为子节点构造输入，包含全局变量和直接上游输出。"""
        payload = dict(self.variables)
        for edge in self.edges:
            if edge.to != node_id:
                continue
            upstream = self.node_states[edge.from_node]
            if upstream.output is not None:
                payload[edge.from_node] = upstream.output
        return payload

    async def run(self) -> Any:
        """执行子流程；任一子节点失败时抛出异常且不发送完成事件。"""
        last_output: Any = None
        while True:
            ready = self._ready_nodes()
            if not ready:
                break

            batch = ready[: self.max_parallelism]
            results = await asyncio.gather(*(self._run_one(node_id) for node_id in batch), return_exceptions=True)
            first_error: BaseException | None = None
            for result in results:
                if isinstance(result, BaseException):
                    first_error = first_error or result
                    continue
                _node_id, output = result
                last_output = output
            if first_error is not None:
                raise first_error

        blocked = [
            node_id
            for node_id, state in self.node_states.items()
            if state.status == NodeStatus.PENDING
        ]
        if blocked:
            raise RuntimeError(f"Subflow has blocked pending nodes: {', '.join(blocked)}")

        self.emit(SubFlowCompleted(task_id=self.task_id, node_id=self.parent_node_id, output=last_output))
        return last_output

    async def _run_one(self, node_id: str) -> tuple[str, Any]:
        """执行单个子节点并写入状态。"""
        node = self.nodes[node_id]
        state = self.node_states[node_id]
        retry_prompt = state.retry_prompts[-1].prompt if state.retry_prompts else None
        state.status = NodeStatus.RUNNING
        state.attempts += 1
        self.emit(SubNodeStarted(task_id=self.task_id, node_id=self.parent_node_id, sub_node_id=node_id))

        try:
            output = await self.run_node(node, self._input_for(node_id), retry_prompt)
        except Exception as e:
            state.status = NodeStatus.FAILED
            state.error = f"{type(e).__name__}: {e}"
            self.emit(
                SubNodeFailed(
                    task_id=self.task_id,
                    node_id=self.parent_node_id,
                    sub_node_id=node_id,
                    error=state.error,
                )
            )
            raise

        state.output = output
        state.status = NodeStatus.COMPLETED
        state.stale = False
        self.variables[node_id] = output
        self.emit(
            SubNodeCompleted(
                task_id=self.task_id,
                node_id=self.parent_node_id,
                sub_node_id=node_id,
                output=output,
            )
        )
        return node_id, output
