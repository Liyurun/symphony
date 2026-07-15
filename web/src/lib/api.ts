// Symphony REST API 客户端
// 使用原生 fetch，baseURL 为空——开发环境下由 Vite 代理把 /api 转发到后端。
import type {
  SOPTemplate,
  TaskMeta,
  TaskSnapshot,
  SymphonyEvent,
  SkillDef,
  SessionMeta,
  TranscriptEntry,
  InteractionRecord,
  PendingInteraction,
  DagLog,
} from './types'

/**
 * 内部请求 helper：统一处理 fetch + JSON 解析 + 错误抛出。
 * @param method HTTP 方法
 * @param path   请求路径（以 /api 开头）
 * @param body   可选请求体，会被序列化为 JSON
 * @returns 解析后的响应 JSON
 */
async function request<T>(
  method: string,
  path: string,
  body?: unknown,
): Promise<T> {
  const init: RequestInit = {
    method,
    headers: { 'Content-Type': 'application/json' },
  }
  // 仅在有 body 时序列化，避免 GET/DELETE 携带空体
  if (body !== undefined) {
    init.body = JSON.stringify(body)
  }

  const res = await fetch(path, init)

  // 非 2xx 视为错误：尝试从响应体里读取后端返回的 detail 字段
  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`
    try {
      const errBody = await res.json()
      // FastAPI 风格错误通常在 detail 里
      if (errBody && typeof errBody === 'object' && 'detail' in errBody) {
        detail = String((errBody as any).detail)
      } else {
        detail = JSON.stringify(errBody)
      }
    } catch {
      // 响应体不是 JSON，忽略，使用默认 detail
    }
    throw new Error(`请求失败 [${method} ${path}]: ${detail}`)
  }

  // 204 或空响应体时返回 undefined（转型为 T）
  if (res.status === 204) {
    return undefined as T
  }
  return (await res.json()) as T
}

/** Symphony API 客户端：所有后端端点的封装 */
export const api = {
  // ---------------- Sessions ----------------
  /** 获取统一 Session 列表 */
  listSessions: (): Promise<SessionMeta[]> => request('GET', '/api/sessions'),

  /** 获取单个 Session 元信息 */
  getSession: (id: string): Promise<SessionMeta> =>
    request('GET', `/api/sessions/${encodeURIComponent(id)}`),

  /** 获取 Session transcript */
  getSessionTranscript: (id: string): Promise<TranscriptEntry[]> =>
    request('GET', `/api/sessions/${encodeURIComponent(id)}/transcript`),

  /** 获取 Session 事件 */
  getSessionEvents: (id: string): Promise<any[]> =>
    request('GET', `/api/sessions/${encodeURIComponent(id)}/events`),

  /** 获取 Session trace */
  getSessionTraces: (id: string): Promise<any[]> =>
    request('GET', `/api/sessions/${encodeURIComponent(id)}/traces`),

  /** 获取 Session 反问记录 */
  getSessionInteractions: (id: string): Promise<InteractionRecord[]> =>
    request('GET', `/api/sessions/${encodeURIComponent(id)}/interactions`),

  /** 回答一次 Session 反问 */
  answerInteraction: (
    sessionId: string,
    interactionId: string,
    answer: Record<string, any>,
  ): Promise<{ ok: boolean }> =>
    request(
      'POST',
      `/api/sessions/${encodeURIComponent(sessionId)}/interactions/${encodeURIComponent(interactionId)}/answer`,
      { answer },
    ),

  /** 创建 Chat Session */
  createChatSession: (title: string): Promise<SessionMeta> =>
    request('POST', '/api/chat/sessions', { title, source: 'web' }),

  /** 启动 SOP Session */
  startSopSession: (
    sop_id: string,
    variables: Record<string, any>,
    title: string,
  ): Promise<{ session_id: string; task_id: string }> =>
    request('POST', '/api/sop-sessions', { sop_id, variables, title }),

  // ---------------- SOP 模板 ----------------
  /** 获取所有 SOP 模板 */
  listSops: (): Promise<SOPTemplate[]> => request('GET', '/api/sops'),

  /** 获取单个 SOP 模板 */
  getSop: (id: string): Promise<SOPTemplate> =>
    request('GET', `/api/sops/${encodeURIComponent(id)}`),

  /** 新建 SOP 模板 */
  createSop: (t: SOPTemplate): Promise<SOPTemplate> =>
    request('POST', '/api/sops', t),

  /** 更新 SOP 模板 */
  updateSop: (id: string, t: SOPTemplate): Promise<SOPTemplate> =>
    request('PUT', `/api/sops/${encodeURIComponent(id)}`, t),

  /** 删除 SOP 模板 */
  deleteSop: (id: string): Promise<{ deleted: boolean }> =>
    request('DELETE', `/api/sops/${encodeURIComponent(id)}`),

  /** 由自然语言描述生成 SOP 模板（可选传入 sop_id 以在已有模板上迭代） */
  generateSop: (description: string, sop_id?: string): Promise<SOPTemplate> =>
    request('POST', '/api/sops/generate', { description, sop_id }),

  /** 由自然语言描述生成 SOP 草案，不保存，等待用户确认 */
  generateSopDraft: (description: string, sop_id?: string): Promise<SOPTemplate> =>
    request('POST', '/api/sops/generate-draft', { description, sop_id }),

  // ---------------- 任务 ----------------
  /** 获取任务列表（元信息） */
  listTasks: (): Promise<TaskMeta[]> => request('GET', '/api/tasks'),

  /** 启动一个任务 */
  startTask: (
    sop_id: string,
    variables: Record<string, any>,
  ): Promise<{ task_id: string }> =>
    request('POST', '/api/tasks', { sop_id, variables }),

  /** 获取任务快照 */
  getTask: (id: string): Promise<TaskSnapshot> =>
    request('GET', `/api/tasks/${encodeURIComponent(id)}`),

  /**
   * 获取任务事件列表。
   * @param since 从第几条事件开始（默认 0，全量）
   */
  getTaskEvents: (id: string, since = 0): Promise<SymphonyEvent[]> =>
    request(
      'GET',
      `/api/tasks/${encodeURIComponent(id)}/events?since=${since}`,
    ),

  /** 获取任务追踪信息（trace） */
  getTaskTraces: (id: string): Promise<any[]> =>
    request('GET', `/api/tasks/${encodeURIComponent(id)}/traces`),

  /** 带补充指令重跑指定主流程节点，并默认让下游失效 */
  rerunNode: (
    taskId: string,
    nodeId: string,
    supplementalInstruction: string,
    invalidateDownstream = true,
  ): Promise<{ ok: boolean; attempt_no: number; invalidated_node_ids: string[] }> =>
    request(
      'POST',
      `/api/tasks/${encodeURIComponent(taskId)}/nodes/${encodeURIComponent(nodeId)}/rerun`,
      {
        supplemental_instruction: supplementalInstruction,
        invalidate_downstream: invalidateDownstream,
      },
    ),

  /** 获取任务中等待回答的运行时交互 */
  getPendingInteractions: (taskId: string): Promise<PendingInteraction[]> =>
    request(
      'GET',
      `/api/tasks/${encodeURIComponent(taskId)}/interactions/pending`,
    ),

  /** 回答任务运行中的指定交互 */
  answerTaskInteraction: (
    taskId: string,
    interactionId: string,
    answer: Record<string, any>,
  ): Promise<{ ok: boolean }> =>
    request(
      'POST',
      `/api/tasks/${encodeURIComponent(taskId)}/interactions/${encodeURIComponent(interactionId)}/answer`,
      { answer },
    ),

  /** 获取任务的 DAG 化日志投影 */
  getTaskDagLog: (taskId: string): Promise<DagLog> =>
    request('GET', `/api/tasks/${encodeURIComponent(taskId)}/dag-log`),

  /** 人工干预某个节点 */
  intervene: (
    id: string,
    node_id: string,
    action: string,
    data: Record<string, any>,
  ): Promise<{ ok: boolean }> =>
    request('POST', `/api/tasks/${encodeURIComponent(id)}/intervene`, {
      node_id,
      action,
      data,
    }),

  /** 确认复合节点的子流程草案 */
  confirmSubflow: (
    taskId: string,
    nodeId: string,
    nodes: any[],
    edges: any[] = [],
  ): Promise<{ ok: boolean }> =>
    request(
      'POST',
      `/api/tasks/${encodeURIComponent(taskId)}/nodes/${encodeURIComponent(nodeId)}/subflow/confirm`,
      { nodes, edges },
    ),

  /** 拒绝复合节点的子流程草案 */
  rejectSubflow: (
    taskId: string,
    nodeId: string,
    reason: string,
  ): Promise<{ ok: boolean }> =>
    request(
      'POST',
      `/api/tasks/${encodeURIComponent(taskId)}/nodes/${encodeURIComponent(nodeId)}/subflow/reject`,
      { reason },
    ),

  /** 带提示词重跑指定子节点，并默认让下游失效 */
  retrySubnode: (
    taskId: string,
    nodeId: string,
    subNodeId: string,
    retryPrompt: string,
  ): Promise<{ ok: boolean }> =>
    request(
      'POST',
      `/api/tasks/${encodeURIComponent(taskId)}/nodes/${encodeURIComponent(nodeId)}/subnodes/${encodeURIComponent(subNodeId)}/retry`,
      { retry_prompt: retryPrompt, invalidate_downstream: true },
    ),

  /** 人工提供子节点输出 */
  provideSubnodeOutput: (
    taskId: string,
    nodeId: string,
    subNodeId: string,
    output: Record<string, any>,
  ): Promise<{ ok: boolean }> =>
    request(
      'POST',
      `/api/tasks/${encodeURIComponent(taskId)}/nodes/${encodeURIComponent(nodeId)}/subnodes/${encodeURIComponent(subNodeId)}/provide-output`,
      { output },
    ),

  /** 跳过指定子节点 */
  skipSubnode: (
    taskId: string,
    nodeId: string,
    subNodeId: string,
  ): Promise<{ ok: boolean }> =>
    request(
      'POST',
      `/api/tasks/${encodeURIComponent(taskId)}/nodes/${encodeURIComponent(nodeId)}/subnodes/${encodeURIComponent(subNodeId)}/skip`,
      {},
    ),

  /** 批量重跑上游子节点 */
  retryUpstreams: (
    taskId: string,
    nodeId: string,
    subNodeIds: string[],
    retryPrompts: Record<string, string>,
  ): Promise<{ ok: boolean }> =>
    request(
      'POST',
      `/api/tasks/${encodeURIComponent(taskId)}/nodes/${encodeURIComponent(nodeId)}/subflow/retry-upstreams`,
      { sub_node_ids: subNodeIds, retry_prompts: retryPrompts },
    ),

  /** 删除任务 */
  deleteTask: (id: string): Promise<{ deleted: boolean }> =>
    request('DELETE', `/api/tasks/${encodeURIComponent(id)}`),

  // ---------------- 技能 / 配置 ----------------
  /** 获取所有可用技能 */
  listSkills: (): Promise<SkillDef[]> => request('GET', '/api/skills'),

  /** 获取（脱敏后的）配置 */
  getConfig: (): Promise<any> => request('GET', '/api/config'),
}
