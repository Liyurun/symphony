"""SOP definition models — Pydantic schemas for SOPs and their nodes.

SOPs are defined in YAML files under data/sop_templates/.
Each SOP has a list of nodes, each mapping to a pi skill.
"""

from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field

from symphony.sop.artifact import ArtifactType


class RetryStrategy(str, Enum):
    """Backoff strategy for node retries."""

    FIXED = "fixed"
    LINEAR = "linear"
    EXPONENTIAL = "exponential"


class NodeExecutor(str, Enum):
    """Which engine executes a node.

    - ``pi``:   Drive the pi agent to completion — full agent loop: loads the
                node's skill, loops the LLM, runs pi's tools. This is the
                intended default: "the agent's capability comes from pi".
    - ``llm``:  Single-shot direct LLM provider (Mira/OpenAI). No agent loop,
                no real skill loading, no pi tools. A lightweight fallback for
                trivial nodes or when pi is unavailable.
    - ``auto``: Use pi if the bridge is running, else fall back to the direct
                LLM provider. Legacy-compatible behaviour.
    """

    PI = "pi"
    LLM = "llm"
    AUTO = "auto"


class NodeRetry(BaseModel):
    """Retry configuration for a single SOP node."""

    max_attempts: int = Field(default=3, ge=1, le=10)
    backoff: RetryStrategy = RetryStrategy.EXPONENTIAL
    initial_delay: float = Field(default=1.0, ge=0.1)


class NodeDefinition(BaseModel):
    """Definition of a single SOP node.

    Each node maps to a pi skill and defines its input/output contract.
    """

    id: str = Field(description="Unique node identifier within the SOP")
    name: str = Field(description="Human-readable node name")
    description: str = Field(default="", description="What this node does")
    input_requirements: str = Field(
        default="",
        description="Natural-language description of required input and its constraints",
    )
    output_requirements: str = Field(
        default="",
        description="Natural-language description of required output and its constraints",
    )
    skill: str = Field(description="Pi skill name to invoke")
    executor: NodeExecutor = Field(
        default=NodeExecutor.PI,
        description=(
            "Execution engine: 'pi' (full pi agent loop — default), "
            "'llm' (single-shot direct LLM), or 'auto' (pi if available else llm)."
        ),
    )
    depends_on: list[str] = Field(
        default_factory=list,
        description="Node IDs this node depends on",
    )
    input_schema: dict = Field(
        default_factory=dict,
        description="JSON Schema for node input",
    )
    output_schema: dict = Field(
        default_factory=dict,
        description="JSON Schema for node output",
    )
    retry: NodeRetry = Field(default_factory=NodeRetry)
    human_intervention: bool = Field(
        default=False,
        description="Require human approval after execution",
    )
    timeout: int = Field(default=300, ge=10, description="Max execution time in seconds")
    parallel_group: str | None = Field(
        default=None,
        description="Group ID for parallel execution",
    )
    input_artifact_type: ArtifactType = Field(
        default=ArtifactType.TEXT,
        description="Expected structured input artifact type",
    )
    output_artifact_type: ArtifactType = Field(
        default=ArtifactType.TEXT,
        description="Structured output artifact type this node produces",
    )
    input_conditions: str = Field(
        default="",
        description="Natural-language constraints on the input artifact (pi self-checks)",
    )
    output_conditions: str = Field(
        default="",
        description="Natural-language constraints on the output artifact (e.g. must contain 背景/SQL/DAG)",
    )


class SOPDefinition(BaseModel):
    """Complete SOP definition — reusable workflow template."""

    name: str = Field(description="Unique SOP name, used as template identifier")
    version: str = Field(default="1.0")
    description: str = Field(default="")
    input_requirements: str = Field(
        default="",
        description="Natural-language description of SOP input and constraints",
    )
    output_requirements: str = Field(
        default="",
        description="Natural-language description of SOP output and constraints",
    )
    nodes: list[NodeDefinition] = Field(min_length=1)
    metadata: dict = Field(default_factory=dict)

    def get_node(self, node_id: str) -> NodeDefinition | None:
        """Find a node by ID."""
        for node in self.nodes:
            if node.id == node_id:
                return node
        return None

    def get_dependencies(self, node_id: str) -> list[str]:
        """Get the dependency node IDs for a given node."""
        node = self.get_node(node_id)
        return node.depends_on if node else []

    def get_dependents(self, node_id: str) -> list[str]:
        """Get nodes that depend on the given node."""
        return [n.id for n in self.nodes if node_id in n.depends_on]

    def topological_order(self) -> list[list[NodeDefinition]]:
        """Return nodes grouped by topological level for parallel execution.

        Each inner list contains nodes that can run in parallel.
        """
        in_degree: dict[str, int] = {n.id: len(n.depends_on) for n in self.nodes}
        adj: dict[str, list[str]] = {n.id: [] for n in self.nodes}
        for n in self.nodes:
            for dep in n.depends_on:
                adj[dep].append(n.id)

        # Start with nodes that have no dependencies
        queue = [nid for nid, deg in in_degree.items() if deg == 0]
        result = []

        while queue:
            level: list[NodeDefinition] = []
            next_queue: list[str] = []
            for nid in queue:
                node = self.get_node(nid)
                if node:
                    level.append(node)
                for dep in adj[nid]:
                    in_degree[dep] -= 1
                    if in_degree[dep] == 0:
                        next_queue.append(dep)

            # Group by parallel_group — nodes with the same group run together
            # Nodes without a group all run together
            explicit_groups: dict[str, list[NodeDefinition]] = {}
            ungrouped: list[NodeDefinition] = []
            for node in level:
                if node.parallel_group:
                    explicit_groups.setdefault(node.parallel_group, []).append(node)
                else:
                    ungrouped.append(node)

            if ungrouped:
                result.append(ungrouped)
            for group_nodes in explicit_groups.values():
                result.append(group_nodes)

            queue = next_queue

        return result


# ── Ad-hoc SOP helpers (方案A: everything is a task) ──────────────────

def make_adhoc_node(
    prompt: str,
    *,
    node_id: str = "turn-1",
    name: str | None = None,
    skill: str = "",
    depends_on: list[str] | None = None,
    timeout: int = 300,
) -> NodeDefinition:
    """Build a single ad-hoc node representing one Q&A turn.

    方案A treats every single-turn question as a one-node SOP task. The node
    has NO input/output schema (free-form Q&A), runs the full pi agent loop
    (executor=pi), and stores the user's prompt in its ``description`` so pi
    receives the question directly. When ``skill`` is empty the pi bridge sends
    the raw prompt (no ``/skill:`` prefix).
    """
    return NodeDefinition(
        id=node_id,
        name=name or (prompt[:40] + ("…" if len(prompt) > 40 else "")) or node_id,
        description=prompt,
        input_requirements="用户输入是一段自然语言问题或任务说明；必须完整保留用户的原始意图、上下文和限制条件。",
        output_requirements="输出应直接回答用户问题；如果执行了工具或检查，应说明关键结果、结论和必要的后续建议。",
        skill=skill,
        executor=NodeExecutor.PI,
        depends_on=depends_on or [],
        input_schema={},
        output_schema={},
        human_intervention=False,
        timeout=timeout,
    )


def make_adhoc_sop(
    prompt: str,
    *,
    name: str | None = None,
    skill: str = "",
    timeout: int = 300,
) -> SOPDefinition:
    """Build a one-node ad-hoc SOP for a single-turn question (方案A).

    The SOP name defaults to a short, unique ``ask-<n>`` identifier so it can
    be tracked, observed and interrupted like any other task.
    """
    import uuid

    sop_name = name or f"ask-{uuid.uuid4().hex[:8]}"
    node = make_adhoc_node(prompt, node_id="turn-1", skill=skill, timeout=timeout)
    return SOPDefinition(
        name=sop_name,
        version="1.0",
        description=prompt[:120],
        input_requirements="用户输入是一段自然语言问题或任务说明。",
        output_requirements="输出为面向用户的自然语言回答，包含必要结论和关键依据。",
        nodes=[node],
        metadata={"adhoc": True, "kind": "qa"},
    )


def append_turn_node(
    sop: SOPDefinition,
    prompt: str,
    *,
    skill: str = "",
    timeout: int = 300,
) -> NodeDefinition:
    """Append a new Q&A turn to an existing ad-hoc SOP (multi-turn chat).

    The new node depends on the LAST node so conversation context flows
    forward exactly like a chat thread (方案A multi-turn = append-node). The
    node is added in place and also returned.
    """
    prev_id = sop.nodes[-1].id if sop.nodes else None
    next_index = len(sop.nodes) + 1
    node = make_adhoc_node(
        prompt,
        node_id=f"turn-{next_index}",
        skill=skill,
        depends_on=[prev_id] if prev_id else [],
        timeout=timeout,
    )
    sop.nodes.append(node)
    return node
