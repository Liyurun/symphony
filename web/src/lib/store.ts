// Symphony 全局状态管理（zustand v5）
// 集中管理任务、快照、事件、SOP 模板、技能、连接状态与页面切换。
import { create } from 'zustand'
import type {
  TaskMeta,
  TaskSnapshot,
  SymphonyEvent,
  SOPTemplate,
  SkillDef,
  NodeState,
  SubFlowRuntime,
  SubNodeState,
} from './types'
import type { SocketStatus } from './ws'

/** 顶层页面枚举 */
export type Page = 'chat' | 'sop-runs' | 'sop-studio' | 'logs'

/** SOP Runs 页面内的子 Tab */
export type SopRunTab = 'dag' | 'tasks' | 'events' | 'studio' | 'logs'

/** 节点详情面板内的 Tab */
export type NodeDetailTab = 'summary' | 'io' | 'attempts' | 'raw'

/** 全局状态与 actions 定义 */
interface SymphonyState {
  // ---------------- state ----------------
  /** 任务列表 */
  tasks: TaskMeta[]
  /** 当前活跃任务 ID */
  activeTaskId: string | null
  /** 当前活跃任务的快照 */
  snapshot: TaskSnapshot | null
  /** 按任务 ID 分组的事件列表 */
  eventsByTask: Record<string, SymphonyEvent[]>
  /** 当前选中的节点 ID */
  selectedNodeId: string | null
  /** SOP 模板列表 */
  sops: SOPTemplate[]
  /** 技能列表 */
  skills: SkillDef[]
  /** WebSocket 连接状态 */
  connection: SocketStatus
  /** 当前页面 */
  page: Page
  /** SOP Runs 页面当前子 Tab */
  sopRunTab: SopRunTab
  /** 右栏宽度（像素） */
  rightPanelWidth: number
  /** 节点详情当前 Tab */
  nodeDetailTab: NodeDetailTab
  /** 节点 I/O 展示模式：summary 摘要 / raw 原始 JSON */
  nodeViewMode: 'summary' | 'raw'

  // ---------------- actions ----------------
  setTasks: (tasks: TaskMeta[]) => void
  setActiveTask: (id: string | null) => void
  setSnapshot: (s: TaskSnapshot | null) => void
  appendEvent: (taskId: string, event: SymphonyEvent) => void
  setEventsForTask: (taskId: string, events: SymphonyEvent[]) => void
  selectNode: (id: string | null) => void
  setSops: (sops: SOPTemplate[]) => void
  setSkills: (skills: SkillDef[]) => void
  setConnection: (s: SocketStatus) => void
  setPage: (p: Page) => void
  setSopRunTab: (tab: SopRunTab) => void
  setRightPanelWidth: (w: number) => void
  setNodeDetailTab: (tab: NodeDetailTab) => void
  setNodeViewMode: (mode: 'summary' | 'raw') => void
}

/**
 * 根据一条事件，计算出对应节点状态的增量更新。
 * 若该事件不影响节点状态，则返回 null。
 * @param prev 该节点原有状态（可能不存在）
 * @param event 事件
 */
function nodePatchFromEvent(
  prev: NodeState | undefined,
  event: SymphonyEvent,
): NodeState | null {
  const nodeId = event.node_id
  if (!nodeId) return null

  // 已有状态为基准，缺省填充一个空节点
  const base: NodeState = prev ?? {
    node_id: nodeId,
    status: 'pending',
    input: null,
    output: null,
    attempts: 0,
  }

  switch (event.type) {
    // 节点状态显式变更事件
    case 'node_status_changed':
      if (!event.status) return null
      return { ...base, status: event.status }
    // 节点因上游重跑失效，回到待执行态并清空旧产出
    case 'node_marked_stale':
      return {
        ...base,
        status: 'pending',
        stale: true,
        stale_reason: event.reason ?? 'upstream_rerun',
        output: null,
        error: undefined,
      }
    // 节点一次 attempt 开始
    case 'node_attempt_started':
      return {
        ...base,
        status: 'running',
        stale: false,
        stale_reason: null,
        attempts: event.attempt_no ?? base.attempts + 1,
      }
    // 节点一次 attempt 完成
    case 'node_attempt_completed':
      return {
        ...base,
        status: 'completed',
        output: event.output,
        stale: false,
        stale_reason: null,
      }
    // 运行中请求用户补充信息或确认
    case 'interaction_requested':
      return {
        ...base,
        status: 'waiting_input',
        pending_interaction_id: event.interaction_id,
      }
    // 用户已回答当前交互
    case 'interaction_answered':
      return {
        ...base,
        pending_interaction_id: null,
      }
    // 节点开始执行
    case 'node_started':
      return { ...base, status: 'running' }
    // 节点完成：更新产出与状态
    case 'node_completed':
      return { ...base, status: 'completed', output: event.output }
    // 节点失败：记录错误
    case 'node_failed':
      return { ...base, status: 'failed', error: event.error }
    // 节点等待人工输入
    case 'node_waiting_input':
      return { ...base, status: 'waiting_input' }
    // 复合节点生成子流程草案：父节点暂停等待确认
    case 'subflow_draft_created':
      return {
        ...base,
        status: 'waiting_input',
        subflow_status: 'waiting_subflow_confirm',
      }
    // 子流程确认后，父节点进入内部子流程运行阶段
    case 'subflow_confirmed':
      return { ...base, status: 'running', subflow_status: 'running_subflow' }
    // 子流程完成后，父节点用聚合输出完成
    case 'subflow_completed':
      return {
        ...base,
        status: 'completed',
        output: event.output,
        subflow_status: 'completed',
      }
    default:
      return null
  }
}

/** 从草案事件中尽量提取子节点 id，兼容不同后端草案结构。 */
function draftNodeIds(draft: any): string[] {
  const rawNodes = draft?.draft_nodes ?? draft?.nodes ?? []
  if (!Array.isArray(rawNodes)) return []
  return rawNodes
    .map((n) => {
      if (typeof n === 'string') return n
      if (n && typeof n === 'object') return n.id ?? n.node_id
      return null
    })
    .filter((id): id is string => typeof id === 'string' && id.length > 0)
}

/** 创建子节点默认运行态，用于事件先于完整 snapshot 到达时的本地兜底。 */
function makeSubNode(
  parentNodeId: string,
  subNodeId: string,
  patch: Partial<SubNodeState> = {},
): SubNodeState {
  return {
    node_id: subNodeId,
    parent_node_id: parentNodeId,
    status: 'pending',
    input: null,
    output: null,
    error: null,
    attempts: 0,
    retry_prompts: [],
    stale: false,
    ...patch,
  }
}

/** 根据子流程事件增量更新 snapshot.subflows；没有子流程信息时保持兼容。 */
function subflowPatchFromEvent(
  prev: SubFlowRuntime | undefined,
  event: SymphonyEvent,
): SubFlowRuntime | null {
  const parentNodeId = event.node_id
  if (!parentNodeId) return null

  const base: SubFlowRuntime = prev ?? {
    parent_node_id: parentNodeId,
    status: 'draft',
    nodes: {},
  }

  switch (event.type) {
    case 'subflow_draft_created': {
      const nodes = { ...base.nodes }
      for (const subNodeId of draftNodeIds(event.draft)) {
        nodes[subNodeId] = nodes[subNodeId] ?? makeSubNode(parentNodeId, subNodeId)
      }
      return { ...base, status: 'draft', draft: event.draft, nodes }
    }
    case 'subflow_confirmed':
      return { ...base, status: 'confirmed' }
    case 'subflow_rejected':
      return { ...base, status: 'rejected' }
    case 'subflow_completed':
      return { ...base, status: 'completed' }
    case 'subnode_started':
      if (!event.sub_node_id) return null
      return {
        ...base,
        status: 'running',
        nodes: {
          ...base.nodes,
          [event.sub_node_id]: makeSubNode(
            parentNodeId,
            event.sub_node_id,
            { ...base.nodes[event.sub_node_id], status: 'running' },
          ),
        },
      }
    case 'subnode_completed':
      if (!event.sub_node_id) return null
      return {
        ...base,
        nodes: {
          ...base.nodes,
          [event.sub_node_id]: makeSubNode(
            parentNodeId,
            event.sub_node_id,
            {
              ...base.nodes[event.sub_node_id],
              status: 'completed',
              output: event.output,
              error: null,
              stale: false,
            },
          ),
        },
      }
    case 'subnode_failed':
      if (!event.sub_node_id) return null
      return {
        ...base,
        nodes: {
          ...base.nodes,
          [event.sub_node_id]: makeSubNode(
            parentNodeId,
            event.sub_node_id,
            {
              ...base.nodes[event.sub_node_id],
              status: 'failed',
              error: event.error ?? '子节点执行失败',
            },
          ),
        },
      }
    case 'subnode_marked_stale':
      if (!event.sub_node_id) return null
      return {
        ...base,
        nodes: {
          ...base.nodes,
          [event.sub_node_id]: makeSubNode(
            parentNodeId,
            event.sub_node_id,
            {
              ...base.nodes[event.sub_node_id],
              status: 'pending',
              output: null,
              error: null,
              stale: true,
            },
          ),
        },
      }
    case 'subnode_retried': {
      if (!event.sub_node_id) return null
      const current = base.nodes[event.sub_node_id]
      const retryPrompt = {
        attempt_no: (current?.attempts ?? 0) + 1,
        prompt: event.retry_prompt ?? '',
        created_at: event.timestamp ?? '',
        created_by: 'user',
      }
      const nodes: Record<string, SubNodeState> = {
        ...base.nodes,
        [event.sub_node_id]: makeSubNode(parentNodeId, event.sub_node_id, {
          ...current,
          status: 'pending',
          output: null,
          error: null,
          stale: false,
          retry_prompts: [...(current?.retry_prompts ?? []), retryPrompt],
        }),
      }
      const downstreamIds = Array.isArray(event.invalidate_downstream)
        ? event.invalidate_downstream.filter(
            (id): id is string => typeof id === 'string',
          )
        : []
      for (const downstreamId of downstreamIds) {
        nodes[downstreamId] = makeSubNode(parentNodeId, downstreamId, {
          ...nodes[downstreamId],
          status: 'pending',
          output: null,
          error: null,
          stale: true,
        })
      }
      return { ...base, status: 'running', nodes }
    }
    default:
      return null
  }
}

/** 创建全局 store（zustand v5 写法：create<State>()(...)） */
export const useStore = create<SymphonyState>()((set) => ({
  // ---------------- 初始 state ----------------
  tasks: [],
  activeTaskId: null,
  snapshot: null,
  eventsByTask: {},
  selectedNodeId: null,
  sops: [],
  skills: [],
  connection: 'closed',
  page: 'chat',
  sopRunTab: 'dag',
  rightPanelWidth: 420,
  nodeDetailTab: 'summary',
  nodeViewMode: 'summary',

  // ---------------- actions ----------------
  setTasks: (tasks) => set({ tasks }),

  setActiveTask: (id) => set({ activeTaskId: id }),

  setSnapshot: (s) => set({ snapshot: s }),

  /**
   * 追加一条事件，同时按需联动更新当前活跃任务快照的节点状态。
   * 规则：
   *  - 事件先按 taskId 存入 eventsByTask。
   *  - 仅当 snapshot 存在且 taskId === activeTaskId 时才更新节点状态，
   *    否则只存事件、不动 snapshot。
   */
  appendEvent: (taskId, event) =>
    set((state) => {
      // 1) 追加事件到对应任务的事件列表
      const prevEvents = state.eventsByTask[taskId] ?? []
      const eventsByTask = {
        ...state.eventsByTask,
        [taskId]: [...prevEvents, event],
      }

      // 2) 判断是否需要联动 snapshot
      const snap = state.snapshot
      if (!snap || taskId !== state.activeTaskId) {
        return { eventsByTask }
      }

      // 3) 计算节点状态增量
      const patch = nodePatchFromEvent(snap.nodes[event.node_id ?? ''], event)
      const subflowPatch = subflowPatchFromEvent(
        event.node_id ? snap.subflows?.[event.node_id] : undefined,
        event,
      )
      if (!patch && !subflowPatch) {
        return { eventsByTask }
      }

      // 4) 写回 snapshot.nodes / snapshot.subflows（不可变更新）
      const snapshot: TaskSnapshot = {
        ...snap,
        nodes: patch ? { ...snap.nodes, [patch.node_id]: patch } : snap.nodes,
        subflows: subflowPatch
          ? { ...(snap.subflows ?? {}), [subflowPatch.parent_node_id]: subflowPatch }
          : snap.subflows,
      }
      return { eventsByTask, snapshot }
    }),

  setEventsForTask: (taskId, events) =>
    set((state) => ({
      eventsByTask: { ...state.eventsByTask, [taskId]: events },
    })),

  selectNode: (id) => set({ selectedNodeId: id }),

  setSops: (sops) => set({ sops }),

  setSkills: (skills) => set({ skills }),

  setConnection: (s) => set({ connection: s }),

  setPage: (p) => set({ page: p }),

  setSopRunTab: (tab) => set({ sopRunTab: tab }),

  setRightPanelWidth: (w) => set({ rightPanelWidth: Math.max(280, Math.min(800, w)) }),

  setNodeDetailTab: (tab) => set({ nodeDetailTab: tab }),

  setNodeViewMode: (mode) => set({ nodeViewMode: mode }),
}))
