import { useEffect, useRef } from 'react'
import type { SymphonyEvent } from '../lib/types'

interface EventLogProps {
  events: SymphonyEvent[]
  compact?: boolean
}

const EVENT_COLOR: Record<string, string> = {
  task_started: '#cba6f7',
  task_completed: '#a6e3a1',
  task_failed: '#f38ba8',
  node_started: '#89b4fa',
  node_completed: '#a6e3a1',
  node_failed: '#f38ba8',
  node_status_changed: '#a6adc8',
  node_waiting_input: '#f9e2af',
  agent_thought: '#cdd6f4',
  agent_message: '#cdd6f4',
  skill_called: '#fab387',
  skill_returned: '#89b4fa',
  user_intervened: '#cba6f7',
  log: '#6c7086',
}

function eventColor(type: string): string {
  return EVENT_COLOR[type] ?? '#a6adc8'
}

function formatTime(ts: string): string {
  const d = new Date(ts)
  if (Number.isNaN(d.getTime())) return ts
  return d.toLocaleTimeString('zh-CN', { hour12: false })
}

function summarize(e: SymphonyEvent): string {
  switch (e.type) {
    case 'agent_thought':
      return e.content ?? ''
    case 'log':
      return typeof e.message === 'string'
        ? e.message
        : (e.content ?? brief(e.message))
    case 'agent_message':
      return typeof e.message === 'string' ? e.message : brief(e.message)
    case 'skill_called':
      return `${e.skill_name ?? '技能'}(${brief(e.args)})`
    case 'skill_returned':
      return `${e.skill_name ?? '技能'} → ${brief(e.result)}`
    case 'node_completed':
      return brief(e.output)
    case 'node_failed':
    case 'task_failed':
      return e.error ?? '失败'
    case 'node_waiting_input':
      return e.reason ?? '等待人工输入'
    case 'node_status_changed':
      return e.status ?? ''
    case 'task_completed':
      return brief(e.final_output)
    default:
      return e.content ?? e.reason ?? ''
  }
}

function brief(v: unknown): string {
  if (v === undefined || v === null) return ''
  const s = typeof v === 'string' ? v : JSON.stringify(v)
  return s.length > 120 ? s.slice(0, 120) + '…' : s
}

export default function EventLog({ events, compact = false }: EventLogProps) {
  const scrollRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    const el = scrollRef.current
    if (el) el.scrollTop = el.scrollHeight
  }, [events])

  return (
    <div
      ref={scrollRef}
      className={`h-full overflow-y-auto font-mono ${
        compact ? 'px-2 py-1 text-[10px] leading-snug' : 'px-3 py-2 text-xs leading-relaxed'
      }`}
    >
      {events.length === 0 ? (
        <div className="text-ctp-overlay">暂无事件</div>
      ) : (
        events.map((e, i) => (
          <div key={i} className={`flex gap-2 ${compact ? 'mb-0.5' : 'mb-1'}`}>
            <span className="shrink-0 text-ctp-overlay">{formatTime(e.timestamp)}</span>
            <span className="shrink-0 font-semibold" style={{ color: eventColor(e.type) }}>
              {e.type}
            </span>
            <span className="break-all text-ctp-subtext">{summarize(e)}</span>
          </div>
        ))
      )}
    </div>
  )
}
