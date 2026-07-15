"""Workflow（SOP 工作流）数据模型。

使用 pydantic v2 定义 SOP 模板的结构，重点是**类型化的输入输出字段**：
每个节点声明一组具名 IO 字段（IOField），每个字段有明确的类型（text / document / json）、
描述、是否必填，以及（仅 json 类型时的）JSON Schema。运行时会把 IO 字段派生为 JSON Schema，
供 AgentRuntime 校验输出、供执行器在节点前后做类型与 schema 校验。

设计上保留旧版 ``input_schema`` / ``output_schema`` 字典字段以兼容存量 SOP；
新 SOP 应优先使用 ``inputs`` / ``outputs`` 列表，``effective_input_schema()`` 与
``effective_output_schema()`` 会在两种描述之间做统一。
"""

from enum import Enum
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, model_validator

from symphony.agent.events import NodeStatus


class NodeType(str, Enum):
    """节点类型枚举。"""

    # 由 Agent（LLM + 技能）执行的节点
    AGENT = "agent"
    # 需要人工介入的节点
    HUMAN = "human"
    # 直接调用某个技能的节点
    SKILL = "skill"
    # 复杂节点，运行时展开为可确认、可重试的内部子流程
    COMPOSITE = "composite"


class IOType(str, Enum):
    """节点输入输出字段的类型。

    - ``text``：短文本（如标题、标语、标签），通常不超过几百字。
    - ``document``：长文档（如正文、报告、纪要），长度不限。
    - ``json``：结构化 JSON 对象，需配套 ``json_schema`` 定义其结构。
    """

    TEXT = "text"
    DOCUMENT = "document"
    JSON = "json"


class IOField(BaseModel):
    """节点的一个具名输入/输出字段定义。"""

    # 字段标识（英文/下划线），在提示词中通过 {{field_name}} 引用
    name: str
    # 人类可读名称，UI 展示用
    label: str = ""
    # 字段类型：text / document / json
    type: IOType = IOType.TEXT
    # 给 LLM 或执行者看的字段说明
    description: str = ""
    # 是否必填（默认必填）
    required: bool = True
    # 仅 type=json 时使用：该字段的 JSON Schema 定义
    json_schema: Optional[dict[str, Any]] = None

    def to_json_schema(self) -> dict[str, Any]:
        """把单个字段转换为 JSON Schema 属性描述。

        - text / document：``{"type": "string"}``，document 额外加 ``contentMediaType: "text/markdown"``
          作为语义提示（不强制）；
        - json：若提供了 ``json_schema`` 则直接采用，否则不限制类型（允许任意合法 JSON 值），
          因为 json 字段的值可能是 object、array、string、number 等任意类型。
        """
        if self.type == IOType.TEXT:
            return {"type": "string", "title": self.label or self.name, "description": self.description}
        if self.type == IOType.DOCUMENT:
            return {
                "type": "string",
                "title": self.label or self.name,
                "description": self.description or "长文档/正文",
                "contentMediaType": "text/markdown",
            }
        # json
        if self.json_schema:
            sch = dict(self.json_schema)
            sch.setdefault("title", self.label or self.name)
            if self.description and "description" not in sch:
                sch["description"] = self.description
            return sch
        return {
            "title": self.label or self.name,
            "description": self.description or "结构化 JSON 数据（任意类型）",
        }


class RetryPolicy(BaseModel):
    """节点执行的重试策略。"""

    # 最大重试次数
    max_retries: int = 3
    # 触发重试的错误类型列表
    retry_on: list[str] = Field(default_factory=lambda: ["validation_error", "timeout"])


class LLMConfig(BaseModel):
    """节点级 LLM 覆盖配置。

    此处仅覆盖单个节点的模型/采样参数，与全局 ``config.py`` 中的
    LLMConfig 用途不同：这里所有字段均可选，未设置时沿用全局配置。
    """

    # 覆盖使用的模型名称
    model: Optional[str] = None
    # 覆盖采样温度
    temperature: Optional[float] = None
    # 覆盖最大生成 token 数
    max_tokens: Optional[int] = None


class SubFlowPolicy(BaseModel):
    """复杂节点展开为子流程时的运行策略。"""

    # MVP 仅支持运行时动态生成子流程草案
    generation: Literal["dynamic"] = "dynamic"
    # 草案执行前是否需要用户确认
    require_confirm: bool = True
    # 子流程内部最大并行节点数
    max_parallelism: int = 3
    # 重跑时失效当前子节点及其下游
    retry_scope: Literal["node_and_downstream"] = "node_and_downstream"


class RetryPrompt(BaseModel):
    """一次带提示词重跑的用户修复说明。"""

    # 关联的重跑 attempt 序号
    attempt_no: int
    # 用户补充的重跑提示词
    prompt: str
    # 创建时间 ISO 字符串
    created_at: str
    # 创建者，默认用户
    created_by: str = "user"


class SubNodeState(BaseModel):
    """复杂节点内部子节点运行状态。"""

    # 子节点 id
    node_id: str
    # 所属 composite 父节点 id
    parent_node_id: str
    # 子节点执行状态
    status: NodeStatus = NodeStatus.PENDING
    # 子节点输入快照
    input: Any = None
    # 子节点输出快照
    output: Any = None
    # 子节点错误信息
    error: Optional[str] = None
    # 已执行次数
    attempts: int = 0
    # 用户发起的带提示词重跑记录
    retry_prompts: list[RetryPrompt] = Field(default_factory=list)
    # 上游重跑后标记当前节点输出过期
    stale: bool = False


class SubFlowDraft(BaseModel):
    """由 composite 节点运行时生成、等待用户确认的子流程草案。"""

    # 所属 composite 父节点 id
    parent_node_id: str
    # 草案节点列表
    draft_nodes: list["Node"] = Field(default_factory=list)
    # 草案边列表
    draft_edges: list["Edge"] = Field(default_factory=list)
    # 草案生成者
    generated_by: str = "agent"
    # 创建时间 ISO 字符串
    created_at: str
    # 草案确认状态
    status: Literal["draft", "confirmed", "rejected"] = "draft"


class Node(BaseModel):
    """SOP 中的单个节点。"""

    # 节点唯一 id
    id: str
    # 节点显示名称
    name: str
    # 节点描述（给 LLM 或 UI 展示）
    description: str = ""
    # 节点类型，默认为 Agent 节点
    type: NodeType = NodeType.AGENT
    # 节点提示词（可含 jinja2 变量）
    prompt: str = ""
    # 节点可用的技能名称列表；空列表表示继承全部已注册技能。
    skills: list[str] = Field(default_factory=list)

    # ---- 新版类型化 I/O（优先）----
    # 具名输入字段列表
    inputs: list[IOField] = Field(default_factory=list)
    # 具名输出字段列表
    outputs: list[IOField] = Field(default_factory=list)

    # ---- 旧版泛化 schema（兼容存量 SOP）----
    # 输入数据的 JSON Schema（新版若提供 inputs，会由其派生）
    input_schema: Optional[dict] = None
    # 输出数据的 JSON Schema（新版若提供 outputs，会由其派生）
    output_schema: Optional[dict] = None

    # 重试策略
    retry_policy: RetryPolicy = Field(default_factory=RetryPolicy)
    # 节点执行超时时间（秒）
    timeout_seconds: int = 120
    # 节点级 LLM 覆盖配置，未设置时沿用全局
    llm_config: Optional[LLMConfig] = None
    # 当 type 为 skill 时指定要调用的技能名称
    skill_name: Optional[str] = None
    # 当 type 为 composite 时使用的子流程策略；未显式提供时自动补默认值
    subflow_policy: Optional[SubFlowPolicy] = None
    # 生成子流程草案时追加给 Agent 的提示
    subflow_prompt: str = ""

    @model_validator(mode="after")
    def _normalize_composite(self) -> "Node":
        """为 composite 节点补齐默认子流程策略。"""
        if self.type == NodeType.COMPOSITE and self.subflow_policy is None:
            self.subflow_policy = SubFlowPolicy()
        return self

    def effective_input_schema(self) -> dict[str, Any]:
        """派生本节点最终用于校验的输入 JSON Schema。

        优先使用 ``inputs`` 列表生成 object-schema；否则回退到 ``input_schema``；
        最后兜底为 ``{"type": "object"}``。
        """
        if self.inputs:
            return _fields_to_object_schema(self.inputs)
        if self.input_schema:
            return self.input_schema
        return {"type": "object"}

    def effective_output_schema(self) -> dict[str, Any]:
        """派生本节点最终用于校验的输出 JSON Schema。

        规则同 ``effective_input_schema()``，作用于输出。
        """
        if self.outputs:
            return _fields_to_object_schema(self.outputs)
        if self.output_schema:
            return self.output_schema
        return {"type": "object"}

    def missing_inputs(self, data: dict[str, Any]) -> list[str]:
        """返回 data 中缺失或类型不符的必填字段名列表（仅当使用 typed inputs 时）。

        - 未使用 typed inputs（``self.inputs`` 为空）时，不做字段级检查，返回 []。
        - 字段缺失 / None：视为缺失。
        - text / document 要求是字符串且非空。
        - json 要求是 dict。
        """
        if not self.inputs:
            return []
        problems: list[str] = []
        for f in self.inputs:
            if not f.required:
                continue
            v = data.get(f.name)
            if v is None:
                problems.append(f"{f.name}（必填，缺失）")
                continue
            if f.type in (IOType.TEXT, IOType.DOCUMENT):
                if not isinstance(v, str) or not v.strip():
                    problems.append(f"{f.name}（应为非空字符串）")
            elif f.type == IOType.JSON:
                if not isinstance(v, (dict, list, str, int, float, bool)):
                    problems.append(f"{f.name}（应为合法 JSON 值：object/array/string/number/boolean）")
        return problems

    def validate_output_fields(self, output: Any) -> Optional[str]:
        """对节点实际输出做字段级类型校验，返回错误信息；通过时返回 None。

        - 未使用 typed outputs：不检查（交给 schema 层校验）。
        - 要求输出是 dict，且每个必填字段存在、类型符合。
        - json 类型字段若声明了 ``json_schema``，还会做一次 schema 校验。
        """
        if not self.outputs:
            return None
        if not isinstance(output, dict):
            return f"节点输出应为 object，实际为 {type(output).__name__}"
        for f in self.outputs:
            v = output.get(f.name)
            if f.required and v is None:
                return f"输出缺少必填字段：{f.name}"
            if v is None:
                continue
            if f.type in (IOType.TEXT, IOType.DOCUMENT):
                if not isinstance(v, str):
                    return f"输出字段 {f.name} 应为字符串，实际为 {type(v).__name__}"
                if f.required and not v.strip():
                    return f"输出字段 {f.name} 不能为空字符串"
            elif f.type == IOType.JSON:
                if not isinstance(v, (dict, list, str, int, float, bool)):
                    return f"输出字段 {f.name} 应为合法 JSON 值（object/array/string/number/boolean）"
                if f.json_schema:
                    try:
                        import jsonschema

                        jsonschema.validate(v, f.json_schema)
                    except Exception as e:  # jsonschema.ValidationError 等
                        return f"输出字段 {f.name} 未通过 schema 校验：{e}"
        return None


def _fields_to_object_schema(fields: list[IOField]) -> dict[str, Any]:
    """把一组 IOField 转换为一个 object 型 JSON Schema。"""
    properties: dict[str, Any] = {}
    required: list[str] = []
    for f in fields:
        properties[f.name] = f.to_json_schema()
        if f.required:
            required.append(f.name)
    schema: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    # 不允许额外字段，保持严格
    schema["additionalProperties"] = False
    return schema


class Edge(BaseModel):
    """连接两个节点的有向边。"""

    # 允许用字段名 from_node 或别名 from 构造
    model_config = {"populate_by_name": True}

    # 起始节点 id（from 为 Python 关键字，故字段名用 from_node + 别名 from）
    from_node: str = Field(alias="from")
    # 目标节点 id
    to: str


class SOPTemplate(BaseModel):
    """SOP 工作流模板，聚合节点与边。"""

    # 模板唯一 id
    id: str
    # 模板名称
    name: str
    # 模板版本
    version: str = "1.0.0"
    # 模板描述
    description: str = ""

    # ---- 工作流级输入变量（新版 typed 风格）----
    # 具名输入变量定义（与 IOField 结构一致）；提供时将覆盖 variables 字段
    variables_def: list[IOField] = Field(default_factory=list)
    # 工作流级输入变量的 JSON Schema（兼容旧版；优先由 variables_def 派生）
    variables: dict = Field(default_factory=lambda: {"type": "object", "properties": {}})

    # 节点列表
    nodes: list[Node]
    # 边列表（串行编辑时允许为空，由 ``ensure_linear_edges`` 自动生成）
    edges: list[Edge] = Field(default_factory=list)
    # 入口节点 id（可空；为空时取 nodes[0].id）
    entry_node: Optional[str] = None

    @model_validator(mode="after")
    def _normalize(self) -> "SOPTemplate":
        """模型构造后兜底：自动补全 entry_node 与线性边。

        - ``entry_node`` 为空时取首个节点 id；
        - ``edges`` 为空且节点数 >= 1 时，按 nodes 顺序自动串接为线性链。
        """
        if not self.nodes:
            return self
        if not self.entry_node:
            self.entry_node = self.nodes[0].id
        if not self.edges and len(self.nodes) > 1:
            self.edges = [
                Edge(from_node=self.nodes[i].id, to=self.nodes[i + 1].id)
                for i in range(len(self.nodes) - 1)
            ]
        return self

    def effective_variables_schema(self) -> dict[str, Any]:
        """派生工作流级输入变量的有效 JSON Schema。"""
        if self.variables_def:
            return _fields_to_object_schema(self.variables_def)
        return self.variables or {"type": "object", "properties": {}}

    def get_node(self, node_id: str) -> Optional[Node]:
        """按 id 查找节点，找不到返回 None。"""
        return next((node for node in self.nodes if node.id == node_id), None)

    def get_next_nodes(self, node_id: str) -> list[str]:
        """返回从指定节点出发的所有后继节点 id。"""
        return [edge.to for edge in self.edges if edge.from_node == node_id]

    def get_prev_nodes(self, node_id: str) -> list[str]:
        """返回指向指定节点的所有前驱节点 id。"""
        return [edge.from_node for edge in self.edges if edge.to == node_id]

    def linear_order(self) -> list[str]:
        """返回严格线性顺序下的节点 id 列表（从入口走到末端）。

        MVP 严格线性：每个节点至多一个后继，遇分支抛 ValueError。
        """
        if not self.entry_node:
            return [n.id for n in self.nodes]
        order: list[str] = []
        cur: Optional[str] = self.entry_node
        seen: set[str] = set()
        while cur is not None:
            if cur in seen:
                break
            seen.add(cur)
            order.append(cur)
            nxts = self.get_next_nodes(cur)
            if len(nxts) > 1:
                raise ValueError(f"Linear SOP required; node {cur} has multiple outgoing edges")
            cur = nxts[0] if nxts else None
        return order
