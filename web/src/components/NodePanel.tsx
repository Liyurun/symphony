import { useState } from 'react'
import type {
  PendingInteraction,
  SOPTemplate,
  TaskSnapshot,
  SymphonyEvent,
  NodeState,
  NodeStatus,
} from '../lib/types'
import { statusStyle } from './status'
import EventLog from './EventLog'
import InteractionCard from './InteractionCard'
import ValuePreview from './ValuePreview'
import { useStore } from '../lib/store'

interface NodePanelProps {
  snapshot: TaskSnapshot | null
  selectedNodeId: string | null
  sop: SOPTemplate | null
  events: SymphonyEvent[]
  dagLog: any
  onIntervene: (nodeId: string, action: string, data: Record<string, any>) => void
  onRetrySubnode: (nodeId: string, subNodeId: string, retryPrompt: string) => void
  onOpenCorrection: (nodeId: string) => void
  pendingInteractions: PendingInteraction[]
  onAnswerInteraction: (interactionId: string, answer: Record<string, any>) => Promise<void>
}

function TabButton({
  active,
  onClick,
  children,
}: {
  active: boolean
  onClick: () => void
  children: React.ReactNode
}) {
  return (
    <button
      onClick={onClick}
      className={`border-b-2 px-3 py-1.5 text-xs transition-colors ${
        active
          ? 'border-ctp-mauve text-ctp-mauve'
          : 'border-transparent text-ctp-subtext hover:text-ctp-text'
      }`}
    >
      {children}
    </button>
  )
}

function pretty(v: unknown): string {
  if (v === undefined || v === null) return '—'
  if (typeof v === 'string') return v
  try {
    return JSON.stringify(v, null, 2)
  } catch {
    return String(v)
  }
}

type TaskNodeSummary = {
  id: string
  name: string
  state?: NodeState
}

function taskNodes(snapshot: TaskSnapshot | null, sop: SOPTemplate | null): TaskNodeSummary[] {
  if (!snapshot) return []
  if (sop?.nodes?.length) {
    return sop.nodes.map((node) => ({
      id: node.id,
      name: node.name || node.id,
      state: snapshot.nodes?.[node.id],
    }))
  }
  return Object.entries(snapshot.nodes ?? {}).map(([id, state]) => ({
    id,
    name: id,
    state,
  }))
}

function summarizeTaskStatus(nodes: TaskNodeSummary[], snapshot: TaskSnapshot | null): NodeStatus {
  if (!snapshot) return 'pending'
  if (nodes.some((node) => node.state?.status === 'failed')) return 'failed'
  if (snapshot.paused || nodes.some((node) => node.state?.status === 'waiting_input')) return 'waiting_input'
  if (nodes.length > 0 && nodes.every((node) => node.state?.status === 'completed' || node.state?.status === 'skipped')) {
    return 'completed'
  }
  if (nodes.some((node) => node.state?.status === 'running')) return 'running'
  return 'pending'
}

function taskFinalOutput(
  snapshot: TaskSnapshot | null,
  sop: SOPTemplate | null,
  events: SymphonyEvent[],
): { nodeId: string | null; value: unknown } {
  const completed = [...events].reverse().find((event) => event.type === 'task_completed' && event.final_output !== undefined)
  if (completed) return { nodeId: snapshot?.current_node ?? null, value: completed.final_output }

  if (!snapshot) return { nodeId: null, value: undefined }
  if (snapshot.current_node && snapshot.nodes?.[snapshot.current_node]?.output !== undefined) {
    return { nodeId: snapshot.current_node, value: snapshot.nodes[snapshot.current_node].output }
  }

  const ordered = taskNodes(snapshot, sop)
  const lastWithOutput = [...ordered].reverse().find((node) => node.state?.output !== undefined && node.state?.output !== null)
  return { nodeId: lastWithOutput?.id ?? null, value: lastWithOutput?.state?.output }
}

export default function NodePanel({
  snapshot,
  selectedNodeId,
  sop,
  events,
  dagLog,
  onIntervene,
  onRetrySubnode,
  onOpenCorrection,
  pendingInteractions,
  onAnswerInteraction,
}: NodePanelProps) {
  const nodeDetailTab = useStore((s) => s.nodeDetailTab)
  const setNodeDetailTab = useStore((s) => s.setNodeDetailTab)

  const [showEditPrompt, setShowEditPrompt] = useState(false)
  const [promptDraft, setPromptDraft] = useState('')
  const [showProvideOutput, setShowProvideOutput] = useState(false)
  const [outputDraft, setOutputDraft] = useState('')
  const [outputError, setOutputError] = useState<string | null>(null)
  const [selectedSubNodeId, setSelectedSubNodeId] = useState<string | null>(null)
  const [retryPrompt, setRetryPrompt] = useState('')

  const nodeState: NodeState | undefined = selectedNodeId
    ? (snapshot?.nodes as Record<string, NodeState> | undefined)?.[selectedNodeId]
    : undefined
  const sopNode = selectedNodeId ? sop?.nodes.find((n) => n.id === selectedNodeId) : undefined
  const subflow = selectedNodeId ? snapshot?.subflows?.[selectedNodeId] : undefined
  const selectedSubNode =
    selectedSubNodeId && subflow ? subflow.nodes[selectedSubNodeId] : undefined
  const subNodes = subflow ? Object.values(subflow.nodes) : []
  const canOpenCorrection = Boolean(selectedNodeId && nodeState && nodeState.status !== 'running')

  const activeNodeDagLog = dagLog?.nodes?.find((n: any) => n.node_id === selectedNodeId)
  const taskSummaryNodes = taskNodes(snapshot, sop)
  const taskStatus = summarizeTaskStatus(taskSummaryNodes, snapshot)
  const taskStatusView = statusStyle(taskStatus)
  const finalOutput = taskFinalOutput(snapshot, sop, events)
  const completedCount = taskSummaryNodes.filter((node) => node.state?.status === 'completed').length
  const failedCount = taskSummaryNodes.filter((node) => node.state?.status === 'failed').length
  const waitingCount = taskSummaryNodes.filter((node) => node.state?.status === 'waiting_input').length
  const runningCount = taskSummaryNodes.filter((node) => node.state?.status === 'running').length

  function openEditPrompt() {
    setPromptDraft(nodeState?.prompt_override ?? sopNode?.prompt ?? '')
    setShowEditPrompt((v) => !v)
  }

  function submitEditPrompt() {
    if (!selectedNodeId) return
    onIntervene(selectedNodeId, 'edit_prompt', { prompt: promptDraft })
    setShowEditPrompt(false)
  }

  function submitProvideOutput() {
    if (!selectedNodeId) return
    let parsed: any
    try {
      parsed = outputDraft.trim() === '' ? {} : JSON.parse(outputDraft)
    } catch {
      setOutputError('JSON 解析失败，请检查格式')
      return
    }
    setOutputError(null)
    onIntervene(selectedNodeId, 'provide_output', { output: parsed })
    setShowProvideOutput(false)
  }

  return (
    <div className="flex h-full flex-col bg-ctp-mantle">
      <div className="border-b border-ctp-surface p-3">
        {nodeState || sopNode ? (
          <div className="flex items-center justify-between gap-2">
            <span className="truncate text-sm font-semibold text-ctp-text">
              {sopNode?.name || selectedNodeId}
            </span>
            {nodeState && (
              <span
                className="rounded px-1.5 py-0.5 text-[10px] font-semibold"
                style={{
                  color: statusStyle(nodeState.status).color,
                  backgroundColor: statusStyle(nodeState.status).bg,
                }}
              >
                {statusStyle(nodeState.status).label}
              </span>
            )}
          </div>
        ) : (
          <div className="text-xs text-ctp-subtext">
            {snapshot ? (
              <>
                <div className="mb-1 text-sm font-semibold text-ctp-text">
                  {sop?.name || snapshot.sop_id}
                </div>
                <div>当前节点：{snapshot.current_node || '—'}</div>
                <div>状态：{taskStatusView.label}</div>
              </>
            ) : (
              <span className="text-ctp-overlay">未选择任务</span>
            )}
          </div>
        )}
      </div>

      {nodeState || sopNode ? (
        <>
          <div className="flex shrink-0 border-b border-ctp-surface px-1">
            <TabButton active={nodeDetailTab === 'summary'} onClick={() => setNodeDetailTab('summary')}>
              概览
            </TabButton>
            <TabButton active={nodeDetailTab === 'io'} onClick={() => setNodeDetailTab('io')}>
              输入输出
            </TabButton>
            <TabButton active={nodeDetailTab === 'attempts'} onClick={() => setNodeDetailTab('attempts')}>
              历史
            </TabButton>
            <TabButton active={nodeDetailTab === 'raw'} onClick={() => setNodeDetailTab('raw')}>
              日志
            </TabButton>
          </div>

          <div className="min-h-0 flex-1 overflow-y-auto p-3">
            {nodeDetailTab === 'summary' && (
              <div className="space-y-3">
                {pendingInteractions
                  .filter((interaction) => interaction.node_id === selectedNodeId)
                  .map((interaction) => (
                    <InteractionCard
                      key={interaction.interaction_id}
                      interaction={interaction}
                      onAnswer={(answer) => onAnswerInteraction(interaction.interaction_id, answer)}
                    />
                  ))}

                {nodeState?.stale && (
                  <div className="rounded border border-ctp-yellow/60 bg-ctp-surface/80 p-2 text-xs text-ctp-yellow">
                    输出已过期：{nodeState.stale_reason || 'upstream_rerun'}
                  </div>
                )}

                {(sopNode?.prompt || nodeState?.prompt_override) && (
                  <div>
                    <div className="mb-1 text-[11px] font-semibold uppercase tracking-wide text-ctp-overlay">
                      提示词
                    </div>
                    <pre className="max-h-32 overflow-y-auto whitespace-pre-wrap rounded bg-ctp-surface/60 p-2 text-xs text-ctp-subtext">
                      {nodeState?.prompt_override ?? sopNode?.prompt}
                    </pre>
                  </div>
                )}

                {nodeState?.output !== undefined && nodeState?.output !== null && (
                  <ValuePreview value={nodeState.output} label="输出" defaultMode="summary" />
                )}

                <div className="text-xs text-ctp-subtext">
                  <div>尝试次数：{nodeState?.attempts ?? 0}</div>
                  {nodeState?.subflow_status && (
                    <div className="mt-1">子流程状态：{nodeState.subflow_status}</div>
                  )}
                  {nodeState?.error && (
                    <div className="mt-2 rounded bg-ctp-red/10 p-2 text-ctp-red">
                      {nodeState.error}
                    </div>
                  )}
                </div>

                {nodeState && (
                  <div>
                    <div className="mb-1 text-[11px] font-semibold uppercase tracking-wide text-ctp-overlay">
                      操作
                    </div>
                    {(() => {
                      const st = nodeState.status
                      const canIntervene = st === 'failed' || st === 'waiting_input'
                      const running = st === 'running'
                      if (!canIntervene && !canOpenCorrection) {
                        return (
                          <div className="text-xs text-ctp-overlay">
                            {running ? '节点执行中…' : '当前状态无可用操作'}
                          </div>
                        )
                      }
                      return (
                        <div className="space-y-2">
                          <div className="flex flex-wrap gap-2">
                            {canOpenCorrection && (
                              <button
                                onClick={() => onOpenCorrection(selectedNodeId!)}
                                className="rounded bg-ctp-surface px-2 py-1 text-xs text-ctp-mauve hover:brightness-110"
                              >
                                追加指令重跑
                              </button>
                            )}
                            {canIntervene && (
                              <>
                                <button
                                  onClick={() => onIntervene(selectedNodeId!, 'retry', {})}
                                  className="rounded bg-ctp-surface px-2 py-1 text-xs text-ctp-blue hover:brightness-110"
                                >
                                  重试
                                </button>
                                <button
                                  onClick={openEditPrompt}
                                  className="rounded bg-ctp-surface px-2 py-1 text-xs text-ctp-mauve hover:brightness-110"
                                >
                                  改提示词重试
                                </button>
                                <button
                                  onClick={() => setShowProvideOutput((v) => !v)}
                                  className="rounded bg-ctp-surface px-2 py-1 text-xs text-ctp-green hover:brightness-110"
                                >
                                  提供输出
                                </button>
                                <button
                                  onClick={() => onIntervene(selectedNodeId!, 'skip', {})}
                                  className="rounded bg-ctp-surface px-2 py-1 text-xs text-ctp-peach hover:brightness-110"
                                >
                                  跳过
                                </button>
                              </>
                            )}
                          </div>

                          {canIntervene && showEditPrompt && (
                            <div className="space-y-1">
                              <textarea
                                value={promptDraft}
                                onChange={(e) => setPromptDraft(e.target.value)}
                                rows={4}
                                className="w-full rounded border border-ctp-surface bg-ctp-base p-2 text-xs text-ctp-text"
                                placeholder="输入新的提示词"
                              />
                              <button
                                onClick={submitEditPrompt}
                                className="rounded bg-ctp-mauve px-2 py-1 text-xs font-medium text-ctp-base"
                              >
                                确认重试
                              </button>
                            </div>
                          )}

                          {canIntervene && showProvideOutput && (
                            <div className="space-y-1">
                              <textarea
                                value={outputDraft}
                                onChange={(e) => setOutputDraft(e.target.value)}
                                rows={4}
                                className="w-full rounded border border-ctp-surface bg-ctp-base p-2 font-mono text-xs text-ctp-text"
                                placeholder='JSON 输出，如 {"result": "..."}'
                              />
                              {outputError && <div className="text-xs text-ctp-red">{outputError}</div>}
                              <button
                                onClick={submitProvideOutput}
                                className="rounded bg-ctp-green px-2 py-1 text-xs font-medium text-ctp-base"
                              >
                                提交输出
                              </button>
                            </div>
                          )}
                        </div>
                      )
                    })()}
                  </div>
                )}
              </div>
            )}

            {nodeDetailTab === 'io' && (
              <div className="space-y-4">
                <ValuePreview value={nodeState?.input} label="输入" defaultMode="summary" />
                <ValuePreview value={nodeState?.output} label="输出" defaultMode="summary" />
                {subflow && selectedSubNode && (
                  <div className="mt-4 rounded border border-ctp-surface p-2">
                    <div className="mb-2 text-xs font-semibold text-ctp-peach">
                      子节点：{selectedSubNode.node_id}
                    </div>
                    <ValuePreview value={selectedSubNode.input} label="子节点输入" />
                    <div className="mt-2">
                      <ValuePreview value={selectedSubNode.output} label="子节点输出" />
                    </div>
                  </div>
                )}
              </div>
            )}

            {nodeDetailTab === 'attempts' && (
              <div className="space-y-3">
                {nodeState?.attempt_history && nodeState.attempt_history.length > 0 ? (
                  nodeState.attempt_history.map((attempt: any, idx: number) => (
                    <div key={idx} className="rounded border border-ctp-surface p-2">
                      <div className="mb-2 flex items-center justify-between">
                        <span className="text-xs font-semibold text-ctp-text">
                          Attempt #{attempt.attempt_no ?? idx + 1}
                        </span>
                        <span
                          className="rounded px-1.5 py-0.5 text-[10px]"
                          style={{
                            color:
                              attempt.status === 'completed'
                                ? '#a6e3a1'
                                : attempt.status === 'failed'
                                  ? '#f38ba8'
                                  : '#f9e2af',
                          }}
                        >
                          {attempt.status}
                        </span>
                      </div>
                      {attempt.trigger && (
                        <div className="mb-1 text-[10px] text-ctp-overlay">触发：{attempt.trigger}</div>
                      )}
                      {attempt.supplemental_instruction && (
                        <div className="mb-2 rounded bg-ctp-surface/60 p-1 text-[11px] text-ctp-subtext">
                          补充指令：{attempt.supplemental_instruction}
                        </div>
                      )}
                      {attempt.error && (
                        <div className="mb-2 rounded bg-ctp-red/10 p-2 text-xs text-ctp-red">
                          {attempt.error}
                        </div>
                      )}
                      {attempt.input !== undefined && attempt.input !== null && (
                        <div className="mb-2">
                          <ValuePreview value={attempt.input} label="输入" />
                        </div>
                      )}
                      {attempt.output !== undefined && attempt.output !== null && (
                        <ValuePreview value={attempt.output} label="输出" />
                      )}
                    </div>
                  ))
                ) : (
                  <div className="text-xs text-ctp-overlay">暂无 attempt 历史</div>
                )}

                {subflow && subNodes.length > 0 && (
                  <div className="mt-4">
                    <div className="mb-2 text-[11px] font-semibold uppercase tracking-wide text-ctp-overlay">
                      子流程节点
                    </div>
                    <div className="space-y-1">
                      {subNodes.map((n: any) => {
                        const selected = n.node_id === selectedSubNodeId
                        const s = statusStyle(n.status)
                        return (
                          <button
                            key={n.node_id}
                            onClick={() => setSelectedSubNodeId(n.node_id)}
                            className="flex w-full items-center justify-between rounded border px-2 py-1.5 text-xs hover:brightness-110"
                            style={{ borderColor: selected ? s.color : 'transparent', backgroundColor: selected ? 'rgba(203,166,247,0.1)' : 'rgba(49,50,68,0.5)' }}
                          >
                            <span className="truncate text-ctp-text">{n.node_id}</span>
                            <span
                              className="ml-2 shrink-0 rounded px-1.5 py-0.5 text-[10px] font-semibold"
                              style={{ color: s.color, backgroundColor: s.bg }}
                            >
                              {n.stale ? 'stale' : s.label}
                            </span>
                          </button>
                        )
                      })}
                    </div>
                    {selectedSubNode && (
                      <div className="mt-3">
                        <textarea
                          value={retryPrompt}
                          onChange={(e) => setRetryPrompt(e.target.value)}
                          rows={3}
                          className="mb-2 w-full rounded border border-ctp-surface bg-ctp-base p-2 text-xs text-ctp-text"
                          placeholder="输入本次重跑的修复提示词"
                        />
                        <button
                          onClick={() => {
                            if (!selectedNodeId || !selectedSubNodeId) return
                            onRetrySubnode(selectedNodeId, selectedSubNodeId, retryPrompt)
                            setRetryPrompt('')
                          }}
                          className="rounded bg-ctp-mauve px-2 py-1 text-xs font-medium text-ctp-base"
                        >
                          带提示词重跑
                        </button>
                      </div>
                    )}
                  </div>
                )}
              </div>
            )}

            {nodeDetailTab === 'raw' && (
              <div className="space-y-4">
                {activeNodeDagLog && (
                  <>
                    <div>
                      <div className="mb-1 text-[11px] font-semibold uppercase tracking-wide text-ctp-overlay">
                        节点事件 ({activeNodeDagLog.events?.length ?? 0})
                      </div>
                      <pre className="max-h-60 overflow-auto whitespace-pre-wrap rounded bg-ctp-base p-2 font-mono text-[11px] text-ctp-subtext">
                        {pretty(activeNodeDagLog.events)}
                      </pre>
                    </div>
                    <div>
                      <div className="mb-1 text-[11px] font-semibold uppercase tracking-wide text-ctp-overlay">
                        LLM Traces
                      </div>
                      <pre className="max-h-60 overflow-auto whitespace-pre-wrap rounded bg-ctp-base p-2 font-mono text-[11px] text-ctp-subtext">
                        {pretty(activeNodeDagLog.traces)}
                      </pre>
                    </div>
                    <div>
                      <div className="mb-1 text-[11px] font-semibold uppercase tracking-wide text-ctp-overlay">
                        Interactions
                      </div>
                      <pre className="max-h-60 overflow-auto whitespace-pre-wrap rounded bg-ctp-base p-2 font-mono text-[11px] text-ctp-subtext">
                        {pretty(activeNodeDagLog.interactions)}
                      </pre>
                    </div>
                  </>
                )}
                <div>
                  <div className="mb-1 text-[11px] font-semibold uppercase tracking-wide text-ctp-overlay">
                    全量事件流
                  </div>
                  <div className="max-h-[400px] overflow-hidden rounded border border-ctp-surface">
                    <EventLog events={events.filter((e) => e.node_id === selectedNodeId)} compact />
                  </div>
                </div>
              </div>
            )}
          </div>
        </>
      ) : (
        <div className="min-h-0 flex-1 overflow-y-auto p-3">
          {snapshot ? (
            <div className="space-y-4">
              <div className="rounded border border-ctp-surface bg-ctp-base/60 p-3">
                <div className="mb-2 flex items-center justify-between gap-2">
                  <div>
                    <div className="text-[11px] font-semibold uppercase tracking-wide text-ctp-overlay">
                      任务总结
                    </div>
                    <div className="mt-1 truncate text-sm font-semibold text-ctp-text">
                      {sop?.name || snapshot.sop_id}
                    </div>
                  </div>
                  <span
                    className="shrink-0 rounded px-1.5 py-0.5 text-[10px] font-semibold"
                    style={{
                      color: taskStatusView.color,
                      backgroundColor: taskStatusView.bg,
                    }}
                  >
                    {taskStatusView.label}
                  </span>
                </div>

                <div className="grid grid-cols-2 gap-2 text-xs text-ctp-subtext">
                  <div className="rounded bg-ctp-surface/50 p-2">
                    <div className="text-[10px] uppercase tracking-wide text-ctp-overlay">Task ID</div>
                    <div className="mt-1 truncate font-mono text-ctp-text" title={snapshot.task_id}>
                      {snapshot.task_id}
                    </div>
                  </div>
                  <div className="rounded bg-ctp-surface/50 p-2">
                    <div className="text-[10px] uppercase tracking-wide text-ctp-overlay">当前节点</div>
                    <div className="mt-1 truncate text-ctp-text">
                      {sop?.nodes.find((node) => node.id === snapshot.current_node)?.name || snapshot.current_node || '—'}
                    </div>
                  </div>
                  <div className="rounded bg-ctp-surface/50 p-2">
                    <div className="text-[10px] uppercase tracking-wide text-ctp-overlay">节点进度</div>
                    <div className="mt-1 text-ctp-text">
                      {completedCount}/{taskSummaryNodes.length || 0} 已完成
                    </div>
                  </div>
                  <div className="rounded bg-ctp-surface/50 p-2">
                    <div className="text-[10px] uppercase tracking-wide text-ctp-overlay">需要关注</div>
                    <div className="mt-1 text-ctp-text">
                      {failedCount > 0
                        ? `${failedCount} 失败`
                        : waitingCount > 0
                          ? `${waitingCount} 等待确认`
                          : runningCount > 0
                            ? `${runningCount} 运行中`
                            : '无'}
                    </div>
                  </div>
                </div>
              </div>

              <div className="rounded border border-ctp-surface bg-ctp-base/60 p-3">
                <div className="mb-2 flex items-center justify-between">
                  <div className="text-[11px] font-semibold uppercase tracking-wide text-ctp-overlay">
                    最终输出
                  </div>
                  <div className="font-mono text-[10px] text-ctp-overlay">
                    {finalOutput.nodeId || '—'}
                  </div>
                </div>
                {finalOutput.value !== undefined && finalOutput.value !== null ? (
                  <ValuePreview value={finalOutput.value} defaultMode="summary" />
                ) : (
                  <div className="rounded bg-ctp-surface/40 p-2 text-xs text-ctp-overlay">
                    暂无最终输出，任务完成后会在这里展示最后节点产出。
                  </div>
                )}
              </div>

              <div className="rounded border border-ctp-surface bg-ctp-base/60 p-3">
                <div className="mb-2 text-[11px] font-semibold uppercase tracking-wide text-ctp-overlay">
                  节点概览
                </div>
                <div className="space-y-1">
                  {taskSummaryNodes.map((node) => {
                    const s = statusStyle(node.state?.status ?? 'pending')
                    return (
                      <div key={node.id} className="flex items-center justify-between gap-2 rounded bg-ctp-surface/40 px-2 py-1.5 text-xs">
                        <span className="min-w-0 truncate text-ctp-text">{node.name}</span>
                        <span
                          className="shrink-0 rounded px-1.5 py-0.5 text-[10px] font-semibold"
                          style={{ color: s.color, backgroundColor: s.bg }}
                        >
                          {node.state?.stale ? 'stale' : s.label}
                        </span>
                      </div>
                    )
                  })}
                </div>
              </div>

              <div className="text-xs text-ctp-overlay">
                点击 DAG 中的节点可查看该节点的输入、输出、历史和日志。
              </div>
            </div>
          ) : (
            <div className="flex h-full items-center justify-center text-xs text-ctp-overlay">
              未选择任务
            </div>
          )}
        </div>
      )}
    </div>
  )
}
