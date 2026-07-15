// TaskKanban：左栏任务看板
// 仅负责渲染任务卡片列表（数据由父组件 Dashboard 统一加载到 store）。
// 每张卡片显示 SOP 名、短任务 ID、状态徽章；点击回调交给父组件处理
// （加载快照 / 事件 / 建立 WebSocket）。活跃任务卡片高亮边框。
import { useStore } from '../lib/store'
import { statusStyle } from './status'
import type { TaskMeta } from '../lib/types'

interface TaskKanbanProps {
  /** 点击任务卡片回调（父组件负责后续加载逻辑） */
  onSelect: (taskId: string) => void
}

/** 状态徽章（内联小组件），按任务状态上色 */
function StatusBadge({ status }: { status: string }) {
  const s = statusStyle(status)
  return (
    <span
      className="rounded px-1.5 py-0.5 text-[10px] font-semibold"
      style={{ color: s.color, backgroundColor: s.bg }}
    >
      {s.label}
    </span>
  )
}

/** 单张任务卡片 */
function TaskCard({
  task,
  active,
  onSelect,
}: {
  task: TaskMeta
  active: boolean
  onSelect: (id: string) => void
}) {
  return (
    <button
      onClick={() => onSelect(task.task_id)}
      className="w-full rounded-lg border p-2.5 text-left transition-colors"
      style={{
        backgroundColor: '#313244',
        // 活跃卡片用强调紫边框，其余用普通边框
        borderColor: active ? '#cba6f7' : 'transparent',
      }}
    >
      {/* 第一行：SOP 名 + 状态徽章 */}
      <div className="flex items-center justify-between gap-2">
        <span className="truncate text-sm font-medium text-ctp-text">
          {task.sop_name || task.sop_id}
        </span>
        <StatusBadge status={task.status} />
      </div>
      {/* 第二行：短任务 ID */}
      <div className="mt-1 font-mono text-[11px] text-ctp-overlay">
        {task.task_id.slice(0, 8)}
      </div>
    </button>
  )
}

export default function TaskKanban({ onSelect }: TaskKanbanProps) {
  // 从 store 取任务列表与当前活跃任务
  const tasks = useStore((s) => s.tasks)
  const activeTaskId = useStore((s) => s.activeTaskId)

  return (
    <div className="flex h-full flex-col">
      {/* 标题：运行中任务数量 */}
      <div className="border-b border-ctp-surface px-3 py-2.5 text-sm font-semibold text-ctp-text">
        运行中任务({tasks.length})
      </div>
      {/* 卡片列表（可滚动） */}
      <div className="flex-1 space-y-2 overflow-y-auto p-2">
        {tasks.length === 0 ? (
          <div className="p-2 text-xs text-ctp-overlay">暂无任务</div>
        ) : (
          tasks.map((t) => (
            <TaskCard
              key={t.task_id}
              task={t}
              active={t.task_id === activeTaskId}
              onSelect={onSelect}
            />
          ))
        )}
      </div>
    </div>
  )
}
