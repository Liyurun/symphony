// Symphony 前端类型定义
// 这里的字段名严格对齐后端 API 契约，不要随意改动键名。

/** 节点运行状态（与后端 NodeStatus 一一对应） */
export type NodeStatus =
  | 'pending' // 待执行
  | 'running' // 执行中
  | 'completed' // 已完成
  | 'failed' // 失败
  | 'waiting_input' // 等待人工输入
  | 'skipped' // 已跳过

/**
 * Symphony 事件对象。
 * 由 WebSocket 推送，也由 `/api/tasks/{id}/events` 返回。
 * 后端是动态 dict，字段随 `type` 不同而不同，因此除固定字段外
 * 其余均为可选，并用索引签名兼容未来扩展。
 */
export interface SymphonyEvent {
  /** 事件类型，如 task_started / node_completed / agent_thought 等 */
  type: string
  /** 关联的任务 ID，可能为 null */
  task_id: string | null
  /** 关联的节点 ID，可能为 null */
  node_id: string | null
  /** ISO 格式时间戳 */
  timestamp: string

  // ---- 以下为按事件类型出现的可选字段 ----
  /** SOP 模板 ID（task_started 等） */
  sop_id?: string
  /** 任务变量快照 */
  variables?: Record<string, any>
  /** 任务最终产出（task_completed） */
  final_output?: any
  /** 错误信息（*_failed） */
  error?: string
  /** 节点产出（node_completed） */
  output?: any
  /** 原因说明（如跳过/等待输入的 reason） */
  reason?: string
  /** 节点状态（node_status_changed） */
  status?: NodeStatus
  /** 文本内容（agent_thought / log 等） */
  content?: string
  /** 消息体（agent_message，dict 结构） */
  message?: any
  /** 技能名称（skill_called / skill_returned） */
  skill_name?: string
  /** 技能入参（skill_called，dict） */
  args?: Record<string, any>
  /** 技能返回（skill_returned） */
  result?: any
  /** 人工干预动作（user_intervened） */
  action?: string
  /** 附加数据（dict） */
  data?: Record<string, any>
  /** 日志级别（log 事件） */
  level?: string

  /** 兼容后端未来新增字段 */
  [key: string]: any
}

/** 统一 session 类型：Chat 与 SOP 运行共享 */
export type SessionType = 'chat' | 'sop'

/** 统一 session 状态 */
export type SessionStatus = 'running' | 'waiting_input' | 'completed' | 'failed'

/** Session 列表/详情元信息 */
export interface SessionMeta {
  session_id: string
  type: SessionType
  title: string
  status: SessionStatus
  created_at: string
  updated_at: string
  task_id?: string | null
  sop_id?: string | null
  source?: string
  error?: string | null
}

/** Chat/SOP 对用户可见的 transcript 条目 */
export interface TranscriptEntry {
  role: 'user' | 'assistant' | 'system'
  content: string
  timestamp?: string
}

/** 统一反问/回答记录 */
export interface InteractionRecord {
  type: 'interaction_requested' | 'interaction_answered'
  interaction_id: string
  session_id: string
  task_id?: string | null
  node_id?: string | null
  prompt?: string
  input_schema?: any
  options?: Array<{ label: string; value: any }>
  multi_select?: boolean
  status?: string
  answer?: Record<string, any>
  timestamp?: string
}

/** 节点一次执行 attempt 的记录 */
export interface NodeAttemptRecord {
  attempt_no: number
  trigger?: string
  supplemental_instruction?: string | null
  status: string
  input?: any
  output?: any
  error?: string | null
}

/** 任务运行中等待用户回答的交互请求 */
export interface PendingInteraction {
  type: 'interaction_requested'
  interaction_id: string
  task_id: string
  node_id: string
  attempt_no: number
  prompt: string
  input_schema: any
  options?: Array<{ label: string; value: any }>
  multi_select?: boolean
  status?: string
}

/** 单个节点的运行时状态（任务快照里 nodes 的值） */
export interface NodeState {
  node_id: string
  status: NodeStatus
  input: any
  output: any
  error?: string
  /** 已尝试次数 */
  attempts: number
  /** 节点历史执行 attempt */
  attempt_history?: NodeAttemptRecord[]
  /** 是否因上游变化而过期 */
  stale?: boolean
  /** 过期原因 */
  stale_reason?: string | null
  /** 当前等待回答的交互 ID */
  pending_interaction_id?: string | null
  /** 运行时对该节点 prompt 的覆盖值 */
  prompt_override?: string
  /** 复合节点内部子流程状态 */
  subflow_status?: string | null
  /** 当前聚焦路径，用于主流程 / 子流程终端定位 */
  focus_path?: string[]
}

/** DAG 日志里的节点投影 */
export interface DagLogNode {
  node_id: string
  name: string
  status: string
  attempts: number
  attempt_history: NodeAttemptRecord[]
  stale?: boolean
  stale_reason?: string | null
  pending_interaction_id?: string | null
  events: any[]
  traces: any[]
  interactions: any[]
}

/** DAG 日志里的边投影 */
export interface DagLogEdge {
  from: string
  to: string
  reason?: string | null
}

/** 任务 DAG 化日志投影 */
export interface DagLog {
  task_id: string
  nodes: DagLogNode[]
  edges: DagLogEdge[]
  raw_event_count: number
}

/** 一次带提示词重跑的用户修复说明 */
export interface RetryPrompt {
  attempt_no: number
  prompt: string
  created_at: string
  created_by: string
}

/** 复合节点内部子节点运行状态 */
export interface SubNodeState {
  node_id: string
  parent_node_id: string
  status: NodeStatus
  input: any
  output: any
  error?: string | null
  attempts: number
  retry_prompts: RetryPrompt[]
  stale: boolean
}

/** 复合节点内部子流程运行时快照 */
export interface SubFlowRuntime {
  parent_node_id: string
  status: 'draft' | 'confirmed' | 'rejected' | 'running' | 'completed'
  draft?: any
  nodes: Record<string, SubNodeState>
}

/** 任务快照 GET /api/tasks/{id} */
export interface TaskSnapshot {
  task_id: string
  sop_id: string
  /** 当前正在执行的节点 ID */
  current_node: string
  /** 是否处于暂停状态 */
  paused: boolean
  variables: Record<string, any>
  /** 节点 ID -> 节点状态 的映射 */
  nodes: Record<string, NodeState>
  /** 复合父节点 ID -> 子流程运行时快照；旧后端可能不返回该字段 */
  subflows?: Record<string, SubFlowRuntime>
}

/** 任务列表元信息 GET /api/tasks */
export interface TaskMeta {
  task_id: string
  sop_id: string
  sop_name: string
  status: string
  created_at: string
  variables: Record<string, any>
  current_node?: string
  error?: string
}

/** 节点输入/输出字段的类型 */
export type IOFieldType = 'text' | 'document' | 'json'

/** 具名 I/O 字段定义（与后端 IOField 对齐） */
export interface IOField {
  /** 字段英文标识（jinja2 模板中用 {{name}} 引用） */
  name: string
  /** 中文名，UI 展示用 */
  label?: string
  /** 字段类型：短文本 / 长文档 / JSON 对象 */
  type?: IOFieldType
  /** 字段说明 */
  description?: string
  /** 是否必填（默认 true） */
  required?: boolean
  /** 仅 type=json 时使用：子 JSON Schema */
  json_schema?: any
}

/** SOP 节点定义（模板中的节点） */
export interface SopNode {
  id: string
  name: string
  /** 节点描述 */
  description?: string
  /** 节点类型：agent（智能体）| human（人工）| skill（技能调用）| composite（复合子流程） */
  type: 'agent' | 'human' | 'skill' | 'composite'
  /** 智能体提示词 */
  prompt?: string
  /** 可用技能名列表 */
  skills?: string[]
  /** 【新版】具名输入字段 */
  inputs?: IOField[]
  /** 【新版】具名输出字段 */
  outputs?: IOField[]
  /** 【旧版兼容】输入 JSON schema（若 inputs 为空则使用） */
  input_schema?: any
  /** 【旧版兼容】输出 JSON schema（若 outputs 为空则使用） */
  output_schema?: any
  /** 重试策略 */
  retry_policy?: {
    max_retries: number
    retry_on: any
  }
  /** 超时时间（秒） */
  timeout_seconds?: number
  /** LLM 配置（可选） */
  llm_config?: any
  /** skill 类型节点绑定的技能名（可选） */
  skill_name?: string
}

/** SOP 边定义（注意后端用 "from" 键） */
export interface SopEdge {
  from: string
  to: string
}

/** SOP 模板 GET /api/sops/{id} */
export interface SOPTemplate {
  id: string
  name: string
  version?: string
  description?: string
  /** 【新版】具名工作流输入变量 */
  variables_def?: IOField[]
  /** 【旧版兼容】变量定义（JSON schema） */
  variables?: any
  nodes: SopNode[]
  edges?: SopEdge[]
  /** 入口节点 ID（可空，后端按 nodes[0] 兜底） */
  entry_node?: string | null
}

/** 技能定义 GET /api/skills */
export interface SkillDef {
  name: string
  description: string
  input_schema: any
  output_schema: any
}
