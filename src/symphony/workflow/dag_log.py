"""DAG 化运行日志投影。

该模块只做纯数据投影：输入 SOP 模板、任务快照、事件日志、LLM trace 和
interaction 事件，输出前端可直接渲染的 DAG 日志结构。它不读取磁盘，也不修改
运行状态，方便单元测试和 API 层复用。
"""

from typing import Any


def _event_type(record: dict[str, Any]) -> str:
    """取日志记录类型，缺失时返回空字符串。"""
    return str(record.get("type", ""))


def _normalize_status(status: Any) -> str:
    """把 NodeStatus 枚举或字符串统一成 API 里的状态字符串。"""
    value = getattr(status, "value", status)
    if value is None:
        return "pending"
    return str(value)


def _node_events(events: list[dict[str, Any]], node_id: str) -> list[dict[str, Any]]:
    """取指定节点相关事件。"""
    return [event for event in events if event.get("node_id") == node_id]


def _node_traces(traces: list[dict[str, Any]], node_id: str) -> list[dict[str, Any]]:
    """取指定节点相关 trace。"""
    return [trace for trace in traces if trace.get("node_id") == node_id]


def _node_interactions(interactions: list[dict[str, Any]], node_id: str) -> list[dict[str, Any]]:
    """取指定节点相关 interaction 记录。"""
    return [item for item in interactions if item.get("node_id") == node_id]


def _node_id(node: dict[str, Any]) -> str:
    """兼容 SOP 节点 id 与投影后 node_id 两种键名。"""
    return str(node.get("id") or node.get("node_id") or "")


def _edge_source(edge: dict[str, Any]) -> str:
    """兼容模型导出和手写 dict 中常见的边起点键名。"""
    return str(edge.get("from") or edge.get("from_node") or edge.get("source") or "")


def _edge_target(edge: dict[str, Any]) -> str:
    """兼容模型导出和手写 dict 中常见的边终点键名。"""
    return str(edge.get("to") or edge.get("target") or "")


def _raw_edges(sop: dict[str, Any]) -> list[dict[str, Any]]:
    """从 SOP 中取边；若没有显式 edges，则按节点顺序补一条线性 DAG。"""
    explicit_edges = sop.get("edges") or []
    if explicit_edges:
        return explicit_edges

    raw_nodes = sop.get("nodes") or []
    return [
        {"from": _node_id(raw_nodes[index]), "to": _node_id(raw_nodes[index + 1])}
        for index in range(len(raw_nodes) - 1)
    ]


def _edge_reason(events: list[dict[str, Any]], source: str, target: str) -> Any:
    """从 node_marked_stale 事件中反查边失效原因。"""
    for event in reversed(events):
        if _event_type(event) != "node_marked_stale":
            continue
        if event.get("node_id") != target:
            continue
        upstream_node_id = event.get("upstream_node_id")
        if upstream_node_id is not None and upstream_node_id != source:
            continue
        return event.get("reason")
    return None


def build_dag_log(
    task_id: str,
    sop: dict[str, Any],
    snapshot: dict[str, Any],
    events: list[dict[str, Any]],
    traces: list[dict[str, Any]],
    interactions: list[dict[str, Any]],
) -> dict[str, Any]:
    """把 SOP、快照和运行日志投影为 DAG 化日志。"""
    states = snapshot.get("nodes") or {}
    nodes: list[dict[str, Any]] = []
    for node in sop.get("nodes") or []:
        node_id = _node_id(node)
        state = states.get(node_id) or {}
        nodes.append(
            {
                "node_id": node_id,
                "name": node.get("name") or node_id,
                "status": _normalize_status(state.get("status", "pending")),
                "attempts": state.get("attempts", 0),
                "attempt_history": state.get("attempt_history", []),
                "stale": bool(state.get("stale", False)),
                "stale_reason": state.get("stale_reason"),
                "pending_interaction_id": state.get("pending_interaction_id"),
                "events": _node_events(events, node_id),
                "traces": _node_traces(traces, node_id),
                "interactions": _node_interactions(interactions, node_id),
            }
        )

    edges = []
    for edge in _raw_edges(sop):
        source = _edge_source(edge)
        target = _edge_target(edge)
        edges.append(
            {
                "from": source,
                "to": target,
                "reason": _edge_reason(events, source, target),
            }
        )

    return {
        "task_id": task_id,
        "nodes": nodes,
        "edges": edges,
        "raw_event_count": len(events),
    }
