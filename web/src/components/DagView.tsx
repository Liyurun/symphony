// DagView：中栏 DAG 可视化
// 用 @xyflow/react 把当前 SOP 模板渲染成有向图，节点按任务快照中的
// 运行状态上色，点击节点回调 onSelectNode。布局采用简单横向排布
// （按节点在模板中的顺序 x = index * 220），保持简洁可用。
import { useMemo } from 'react'
import {
  ReactFlow,
  Background,
  Controls,
  type Node,
  type Edge,
} from '@xyflow/react'
import type { SOPTemplate, TaskSnapshot } from '../lib/types'
import { runtimeNodeStyle } from './status'

interface DagViewProps {
  /** 当前任务对应的 SOP 模板（可能尚未加载） */
  sop: SOPTemplate | null
  /** 当前任务快照（提供各节点运行状态） */
  snapshot: TaskSnapshot | null
  /** 点击节点回调 */
  onSelectNode: (id: string) => void
  /** 当前选中节点 ID */
  selectedNodeId: string | null
}

export default function DagView({
  sop,
  snapshot,
  onSelectNode,
  selectedNodeId,
}: DagViewProps) {
  // 把 SOP 节点转成 ReactFlow 节点：横向排布 + 按状态上色。
  // 依赖 sop / snapshot / selectedNodeId 变化时重算。
  const nodes = useMemo<Node[]>(() => {
    if (!sop) return []
    return sop.nodes.map((n, index) => {
      // 节点运行状态取自快照；无快照时视为待执行
      const nodeState = snapshot?.nodes[n.id]
      const status = nodeState?.status ?? 'pending'
      const s = runtimeNodeStyle(status, nodeState?.stale)
      const attemptLabel = nodeState?.attempts ? ` #${nodeState.attempts}` : ''
      const staleLabel = nodeState?.stale ? ' stale' : ''
      const selected = n.id === selectedNodeId
      return {
        id: n.id,
        position: { x: index * 220, y: 80 },
        data: { label: `${n.name || n.id}${attemptLabel}${staleLabel}` },
        // 用 style 给默认节点上色：状态色边框 + 半透明底色；
        // 选中时加粗边框并高亮
        style: {
          background: s.bg,
          color: '#cdd6f4',
          border: `${selected ? 2 : 1}px solid ${s.color}`,
          borderRadius: 8,
          padding: '8px 12px',
          fontSize: 12,
          width: 160,
          boxShadow: selected ? `0 0 0 2px ${s.color}55` : 'none',
        },
      }
    })
  }, [sop, snapshot, selectedNodeId])

  // 把 SOP 边（from/to）转成 ReactFlow 边（source/target）。
  // 若 edges 为空/缺失，则按 nodes 顺序自动生成线性串接边（A→B→C）。
  const edges = useMemo<Edge[]>(() => {
    if (!sop) return []
    const rawEdges =
      sop.edges && sop.edges.length > 0
        ? sop.edges
        : sop.nodes.slice(0, -1).map((n, i) => ({
            from: n.id,
            to: sop.nodes[i + 1].id,
          }))
    return rawEdges.map((e, i) => ({
      id: `${e.from}-${e.to}-${i}`,
      source: e.from,
      target: e.to,
      animated: false,
      style: { stroke: '#6c7086' },
    }))
  }, [sop])

  // 无 SOP 时显示占位
  if (!sop) {
    return (
      <div className="flex h-full items-center justify-center text-sm text-ctp-overlay">
        选择一个任务以查看其工作流
      </div>
    )
  }

  return (
    <div className="h-full w-full">
      <ReactFlow
        nodes={nodes}
        edges={edges}
        fitView
        // 只读画布：禁用拖拽/连线，避免误操作
        nodesDraggable={false}
        nodesConnectable={false}
        elementsSelectable
        onNodeClick={(_, node) => onSelectNode(node.id)}
        proOptions={{ hideAttribution: true }}
      >
        {/* 背景网点 + 缩放控制条 */}
        <Background color="#313244" gap={16} />
        <Controls showInteractive={false} />
      </ReactFlow>
    </div>
  )
}
