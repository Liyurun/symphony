"""Symphony Agent 层事件类型定义。

使用 Pydantic v2 定义 SOP 工作流执行过程中产生的各类事件，覆盖任务、节点、
Agent 思考/消息、技能调用、用户干预与日志等场景。所有事件统一继承自 Event 基类，
携带 type/task_id/node_id/timestamp 通用字段，并支持序列化为字典用于流式推送或持久化。
"""

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field


def now_iso() -> str:
    """返回当前 UTC 时间的 ISO 8601 字符串。"""
    return datetime.now(timezone.utc).isoformat()


class NodeStatus(str, Enum):
    """节点执行状态枚举。"""

    # 待执行，尚未开始
    PENDING = "pending"
    # 执行中
    RUNNING = "running"
    # 已完成
    COMPLETED = "completed"
    # 执行失败
    FAILED = "failed"
    # 等待用户输入
    WAITING_INPUT = "waiting_input"
    # 被跳过
    SKIPPED = "skipped"


class Event(BaseModel):
    """所有事件的基类，携带通用元信息。"""

    # 事件类型标识，由各子类设置默认值
    type: str
    # 所属任务 id，可能为空
    task_id: Optional[str] = None
    # 所属节点 id，可能为空
    node_id: Optional[str] = None
    # 事件产生时间的 ISO 字符串，缺失时自动填充
    timestamp: Optional[str] = None

    def __init__(self, **data: Any) -> None:
        """初始化事件，timestamp 缺失或为 None 时自动填充当前时间。"""
        # 当调用方未提供 timestamp 或显式传入 None 时，补充当前 UTC 时间
        if data.get("timestamp") is None:
            data["timestamp"] = now_iso()
        super().__init__(**data)

    def to_dict(self) -> dict:
        """序列化为字典，剔除值为 None 的字段。"""
        return self.model_dump(exclude_none=True)


class TaskStarted(Event):
    """任务开始事件。"""

    # 事件类型
    type: str = "task_started"
    # 关联的 SOP 定义 id
    sop_id: str
    # 任务启动时的初始变量
    variables: dict[str, Any] = Field(default_factory=dict)


class TaskCompleted(Event):
    """任务完成事件。"""

    # 事件类型
    type: str = "task_completed"
    # 任务最终输出
    final_output: Any = None


class TaskFailed(Event):
    """任务失败事件。"""

    # 事件类型
    type: str = "task_failed"
    # 失败原因描述
    error: str


class NodeStarted(Event):
    """节点开始事件。"""

    # 事件类型
    type: str = "node_started"


class NodeCompleted(Event):
    """节点完成事件。"""

    # 事件类型
    type: str = "node_completed"
    # 节点输出
    output: Any = None


class NodeFailed(Event):
    """节点失败事件。"""

    # 事件类型
    type: str = "node_failed"
    # 失败原因描述
    error: str


class NodeWaitingInput(Event):
    """节点等待用户输入事件。"""

    # 事件类型
    type: str = "node_waiting_input"
    # 等待输入的原因说明
    reason: str


class NodeStatusChanged(Event):
    """节点状态变更事件。"""

    # 事件类型
    type: str = "node_status_changed"
    # 变更后的节点状态
    status: NodeStatus


class NodeAttemptStarted(Event):
    """节点一次执行 attempt 开始事件。"""

    # 事件类型
    type: str = "node_attempt_started"
    # attempt 序号，从 1 开始递增
    attempt_no: int
    # 触发来源，例如 normal / retry / correction
    trigger: str = "normal"


class NodeAttemptCompleted(Event):
    """节点一次执行 attempt 完成事件。"""

    # 事件类型
    type: str = "node_attempt_completed"
    # attempt 序号
    attempt_no: int
    # 本次 attempt 的输出
    output: Any = None


class NodeAttemptFailed(Event):
    """节点一次执行 attempt 失败事件。"""

    # 事件类型
    type: str = "node_attempt_failed"
    # attempt 序号
    attempt_no: int
    # 失败原因描述
    error: str


class NodeRetryRequested(Event):
    """用户请求对节点追加指令并重跑。"""

    # 事件类型
    type: str = "node_retry_requested"
    # 即将执行的 attempt 序号
    attempt_no: int
    # 用户补充指令
    supplemental_instruction: str
    # 是否同步标记下游节点过期
    invalidate_downstream: bool = True
    # 本次请求标记过期的下游节点 id
    invalidated_node_ids: list[str] = Field(default_factory=list)


class NodeSupplementalInstructionAdded(Event):
    """节点 attempt 记录了用户补充指令。"""

    # 事件类型
    type: str = "node_supplemental_instruction_added"
    # attempt 序号
    attempt_no: int
    # 用户补充指令
    supplemental_instruction: str


class NodeMarkedStale(Event):
    """节点因上游变化被标记为过期。"""

    # 事件类型
    type: str = "node_marked_stale"
    # 过期原因
    reason: str
    # 触发过期的上游节点 id
    upstream_node_id: Optional[str] = None


class DownstreamInvalidated(Event):
    """一次重跑导致的下游失效汇总事件。"""

    # 事件类型
    type: str = "downstream_invalidated"
    # 被标记过期的下游节点 id
    invalidated_node_ids: list[str] = Field(default_factory=list)
    # 失效原因
    reason: str = "upstream_rerun"


class InteractionRequested(Event):
    """运行中请求用户确认或补充信息。"""

    # 事件类型
    type: str = "interaction_requested"
    # 交互请求 id
    interaction_id: str
    # 当前 attempt 序号
    attempt_no: int
    # 展示给用户的确认或补充问题
    prompt: str
    # 回答数据的结构约束
    input_schema: dict[str, Any] = Field(default_factory=dict)
    # 可选项配置
    options: list[dict[str, Any]] = Field(default_factory=list)
    # 是否允许多选
    multi_select: bool = False
    # 交互状态
    status: str = "pending"


class InteractionAnswered(Event):
    """用户回答运行中确认请求。"""

    # 事件类型
    type: str = "interaction_answered"
    # 交互请求 id
    interaction_id: str
    # 当前 attempt 序号
    attempt_no: int
    # 用户提交的回答
    answer: dict[str, Any] = Field(default_factory=dict)


class SubFlowDraftCreated(Event):
    """复杂节点生成子流程草案事件。"""

    # 事件类型
    type: str = "subflow_draft_created"
    # 子流程草案快照
    draft: dict[str, Any]


class SubFlowConfirmed(Event):
    """用户确认子流程草案事件。"""

    # 事件类型
    type: str = "subflow_confirmed"


class SubFlowRejected(Event):
    """用户拒绝子流程草案事件。"""

    # 事件类型
    type: str = "subflow_rejected"
    # 拒绝原因
    reason: str = ""


class SubNodeStarted(Event):
    """子流程内部子节点开始事件。"""

    # 事件类型
    type: str = "subnode_started"
    # 子节点 id
    sub_node_id: str


class SubNodeCompleted(Event):
    """子流程内部子节点完成事件。"""

    # 事件类型
    type: str = "subnode_completed"
    # 子节点 id
    sub_node_id: str
    # 子节点输出
    output: Any = None


class SubNodeFailed(Event):
    """子流程内部子节点失败事件。"""

    # 事件类型
    type: str = "subnode_failed"
    # 子节点 id
    sub_node_id: str
    # 失败原因描述
    error: str


class SubNodeRetried(Event):
    """用户对指定子节点发起带提示词重跑事件。"""

    # 事件类型
    type: str = "subnode_retried"
    # 子节点 id
    sub_node_id: str
    # 用户追加的重跑提示词
    retry_prompt: str
    # 本次重跑需要标记失效的下游子节点 id
    invalidate_downstream: list[str] = Field(default_factory=list)


class SubNodeMarkedStale(Event):
    """子节点因上游变化被标记为过期事件。"""

    # 事件类型
    type: str = "subnode_marked_stale"
    # 子节点 id
    sub_node_id: str


class SubFlowCompleted(Event):
    """复杂节点内部子流程完成事件。"""

    # 事件类型
    type: str = "subflow_completed"
    # 子流程聚合输出
    output: Any = None


class AgentThought(Event):
    """Agent 思考过程事件。"""

    # 事件类型
    type: str = "agent_thought"
    # 思考内容文本
    content: str


class AgentMessage(Event):
    """Agent 消息事件。"""

    # 事件类型
    type: str = "agent_message"
    # 结构化消息内容
    message: dict[str, Any]


class SkillCalled(Event):
    """技能调用事件。"""

    # 事件类型
    type: str = "skill_called"
    # 被调用的技能名称
    skill_name: str
    # 调用参数
    args: dict[str, Any] = Field(default_factory=dict)


class SkillReturned(Event):
    """技能返回事件。"""

    # 事件类型
    type: str = "skill_returned"
    # 技能名称
    skill_name: str
    # 技能返回结果
    result: Any = None


class SkillFailed(Event):
    """技能调用失败事件。"""

    # 事件类型
    type: str = "skill_failed"
    # 技能名称
    skill_name: str
    # 失败原因描述
    error: str


class UserIntervened(Event):
    """用户干预事件。"""

    # 事件类型
    type: str = "user_intervened"
    # 用户执行的操作标识
    action: str
    # 操作附带的数据
    data: dict[str, Any] = Field(default_factory=dict)


class LogMessage(Event):
    """日志消息事件。"""

    # 事件类型
    type: str = "log"
    # 日志级别，默认 info
    level: str = "info"
    # 日志正文
    message: str
