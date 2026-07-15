// NodeCorrectionModal：主流程节点追加补充指令重跑弹窗。
// 弹窗只负责收集 supplemental instruction，并预览当前节点与下游受影响范围；
// 实际重跑、刷新快照和事件由 Dashboard 注入的 onSubmit 完成。
import { useMemo, useState } from 'react'
import type { SOPTemplate } from '../lib/types'

interface Props {
  /** 是否展示弹窗 */
  open: boolean
  /** 当前要纠偏重跑的主流程节点 ID */
  nodeId: string | null
  /** 当前任务的 SOP 模板，用于计算下游影响范围 */
  sop: SOPTemplate | null
  /** 关闭弹窗 */
  onClose: () => void
  /** 提交补充指令 */
  onSubmit: (instruction: string) => Promise<void>
}

/** 基于 SOP edges 计算指定节点的所有下游节点；旧 SOP 没有 edges 时按 nodes 顺序兜底。 */
function downstreamNodeIds(
  sop: SOPTemplate | null,
  nodeId: string | null,
): string[] {
  if (!sop || !nodeId) return []
  const edges =
    sop.edges && sop.edges.length > 0
      ? sop.edges
      : sop.nodes.slice(0, -1).map((node, index) => ({
          from: node.id,
          to: sop.nodes[index + 1].id,
        }))

  const children = new Map<string, string[]>()
  for (const edge of edges) {
    children.set(edge.from, [...(children.get(edge.from) ?? []), edge.to])
  }

  const seen = new Set<string>()
  const queue = [...(children.get(nodeId) ?? [])]
  while (queue.length > 0) {
    const current = queue.shift()!
    if (seen.has(current)) continue
    seen.add(current)
    queue.push(...(children.get(current) ?? []))
  }

  return sop.nodes.map((node) => node.id).filter((id) => seen.has(id))
}

export default function NodeCorrectionModal({
  open,
  nodeId,
  sop,
  onClose,
  onSubmit,
}: Props) {
  const [instruction, setInstruction] = useState('')
  const [submitting, setSubmitting] = useState(false)

  const selectedNode = useMemo(
    () => sop?.nodes.find((node) => node.id === nodeId),
    [sop, nodeId],
  )
  const downstream = useMemo(
    () => downstreamNodeIds(sop, nodeId),
    [sop, nodeId],
  )

  if (!open || !nodeId) return null

  async function submit() {
    const text = instruction.trim()
    if (!text) return
    setSubmitting(true)
    try {
      await onSubmit(text)
      setInstruction('')
      onClose()
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/50"
      onClick={onClose}
    >
      <div
        className="w-[520px] rounded-lg border border-ctp-surface bg-ctp-mantle p-4 shadow-xl"
        onClick={(event) => event.stopPropagation()}
      >
        <div className="mb-1 text-sm font-semibold text-ctp-text">
          追加指令重跑
        </div>
        <div className="mb-3 text-xs text-ctp-subtext">
          当前节点：
          <span className="font-mono text-ctp-text">
            {selectedNode?.name || nodeId}
          </span>
        </div>

        <label className="mb-1 block text-xs font-medium text-ctp-subtext">
          补充指令
        </label>
        <textarea
          value={instruction}
          onChange={(event) => setInstruction(event.target.value)}
          rows={5}
          className="w-full rounded border border-ctp-surface bg-ctp-base p-2 text-sm text-ctp-text"
          placeholder="说明这次重跑需要优先遵循的新要求"
          aria-label="Supplemental instruction"
        />

        <div className="mt-3 rounded border border-ctp-surface bg-ctp-base/70 p-2 text-xs text-ctp-subtext">
          <div className="mb-1 font-semibold text-ctp-text">
            影响范围预览
          </div>
          <div>
            当前节点会创建新的 attempt，并清空旧输出后重跑。
          </div>
          {downstream.length > 0 ? (
            <div className="mt-2 space-y-1">
              {downstream.map((id) => {
                const node = sop?.nodes.find((item) => item.id === id)
                return (
                  <div key={id}>
                    下游节点：
                    <span className="font-mono text-ctp-yellow">
                      {node?.name || id}
                    </span>
                  </div>
                )
              })}
            </div>
          ) : (
            <div className="mt-2 text-ctp-overlay">无下游节点</div>
          )}
        </div>

        <div className="mt-4 flex justify-end gap-2">
          <button
            type="button"
            onClick={onClose}
            className="rounded bg-ctp-surface px-3 py-1 text-sm text-ctp-subtext hover:brightness-110"
          >
            取消
          </button>
          <button
            type="button"
            onClick={() => void submit()}
            disabled={submitting || instruction.trim().length === 0}
            className="rounded bg-ctp-mauve px-3 py-1 text-sm font-medium text-ctp-base hover:brightness-110 disabled:opacity-50"
          >
            {submitting ? '重跑中…' : '重跑当前节点及下游'}
          </button>
        </div>
      </div>
    </div>
  )
}
