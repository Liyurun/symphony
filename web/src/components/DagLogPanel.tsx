// DagLogPanel：DAG 化运行日志面板。
// 左侧按节点列出状态、attempt 次数和 stale 标记；右侧展示当前选中节点的
// attempts / events / traces / interactions，方便按工作流节点复盘运行细节。
import type { DagLog } from '../lib/types'

function pretty(value: unknown): string {
  if (value === undefined || value === null) return '-'
  if (typeof value === 'string') return value
  try {
    return JSON.stringify(value, null, 2)
  } catch {
    return String(value)
  }
}

function DetailSection({
  title,
  value,
}: {
  title: string
  value: unknown
}) {
  return (
    <section className="mb-3">
      <div className="mb-1 text-[11px] font-semibold uppercase tracking-wide text-ctp-overlay">
        {title}
      </div>
      <pre className="max-h-40 overflow-auto whitespace-pre-wrap rounded bg-ctp-base p-2 text-xs text-ctp-subtext">
        {pretty(value)}
      </pre>
    </section>
  )
}

export default function DagLogPanel({
  dagLog,
  selectedNodeId,
}: {
  dagLog: DagLog | null
  selectedNodeId: string | null
}) {
  if (!dagLog) {
    return (
      <div className="flex h-full items-center justify-center bg-ctp-mantle p-3 text-xs text-ctp-overlay">
        暂无 DAG 日志
      </div>
    )
  }

  const activeNode =
    dagLog.nodes.find((node) => node.node_id === selectedNodeId) ??
    dagLog.nodes[0]

  if (!activeNode) {
    return (
      <div className="flex h-full flex-col border-t border-ctp-surface bg-ctp-mantle">
        <div className="border-b border-ctp-surface px-3 py-1.5 text-[11px] font-semibold uppercase tracking-wide text-ctp-overlay">
          DAG Log
        </div>
        <div className="flex flex-1 items-center justify-center text-xs text-ctp-overlay">
          当前任务没有 DAG 节点日志
        </div>
      </div>
    )
  }

  return (
    <div className="flex h-full min-h-0 flex-col border-t border-ctp-surface bg-ctp-mantle">
      <div className="flex items-center justify-between border-b border-ctp-surface px-3 py-1.5">
        <div className="text-[11px] font-semibold uppercase tracking-wide text-ctp-overlay">
          DAG Log
        </div>
        <div className="font-mono text-[10px] text-ctp-overlay">
          events {dagLog.raw_event_count}
        </div>
      </div>

      <div className="grid min-h-0 flex-1 grid-cols-[240px_1fr]">
        <div className="min-h-0 overflow-y-auto border-r border-ctp-surface p-2">
          {dagLog.nodes.map((node) => {
            const active = node.node_id === activeNode.node_id
            return (
              <div
                key={node.node_id}
                className={`mb-1 rounded p-2 text-xs ${
                  active ? 'bg-ctp-surface' : 'bg-ctp-base/60'
                }`}
              >
                <div className="truncate font-semibold text-ctp-text">
                  {node.name || node.node_id}
                </div>
                <div className="mt-0.5 truncate font-mono text-[11px] text-ctp-overlay">
                  {node.node_id}
                </div>
                <div className="mt-1 font-mono text-[11px] text-ctp-subtext">
                  {node.status} | attempts {node.attempts}
                </div>
                {node.stale && (
                  <div className="mt-1 truncate text-[11px] text-ctp-yellow">
                    stale: {node.stale_reason || 'upstream_rerun'}
                  </div>
                )}
                {node.pending_interaction_id && (
                  <div className="mt-1 truncate text-[11px] text-ctp-peach">
                    waiting: {node.pending_interaction_id}
                  </div>
                )}
              </div>
            )
          })}
        </div>

        <div className="min-h-0 overflow-y-auto p-3">
          <div className="mb-2 flex items-center justify-between gap-2">
            <div className="min-w-0">
              <div className="truncate text-sm font-semibold text-ctp-mauve">
                {activeNode.name || activeNode.node_id}
              </div>
              <div className="truncate font-mono text-[11px] text-ctp-overlay">
                {activeNode.node_id}
              </div>
            </div>
            <div className="shrink-0 rounded bg-ctp-surface px-2 py-1 font-mono text-[11px] text-ctp-subtext">
              {activeNode.status} / {activeNode.attempts}
            </div>
          </div>

          <DetailSection title="Attempts" value={activeNode.attempt_history} />
          <DetailSection title="Events" value={activeNode.events} />
          <DetailSection title="Traces" value={activeNode.traces} />
          <DetailSection
            title="Interactions"
            value={activeNode.interactions}
          />
        </div>
      </div>
    </div>
  )
}
