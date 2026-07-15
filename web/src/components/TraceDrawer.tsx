// TraceDrawer：LLM 调用轨迹查看抽屉
// 从右侧滑出，展示某个任务的全部 LLM 调用记录（trace）。
// 每条 trace 是后端 record_llm_call 落盘的 JSONL 记录，字段包括：
//   timestamp / node_id / model / request_messages / response / usage / tool_calls
// 用途是事后调试与复盘：可展开查看每次请求的完整 messages、模型响应与 token 用量。
import { useEffect, useState } from 'react'
import { api } from '../lib/api'

interface TraceDrawerProps {
  /** 目标任务 ID，为 null 时不加载 */
  taskId: string | null
  /** 抽屉是否打开 */
  open: boolean
  /** 关闭回调 */
  onClose: () => void
}

/** 单条 trace 的宽松类型（后端为动态 dict，字段可能缺省） */
interface TraceRecord {
  timestamp?: string
  node_id?: string
  model?: string
  request_messages?: Array<{ role?: string; content?: any }>
  response?: any
  usage?: Record<string, any> | null
  tool_calls?: any
  [key: string]: any
}

/** 把任意值格式化为可读文本（字符串原样返回，对象 pretty JSON） */
function pretty(v: unknown): string {
  if (v === undefined || v === null) return '—'
  if (typeof v === 'string') return v
  try {
    return JSON.stringify(v, null, 2)
  } catch {
    return String(v)
  }
}

/** 从 ISO 时间戳提取「时:分:秒」，解析失败则返回原串 */
function formatTime(ts?: string): string {
  if (!ts) return '—'
  const d = new Date(ts)
  if (Number.isNaN(d.getTime())) return ts
  return d.toLocaleTimeString('zh-CN', { hour12: false })
}

/** 单张可折叠的 trace 卡片 */
function TraceCard({ trace, index }: { trace: TraceRecord; index: number }) {
  // 每张卡片独立管理展开状态，默认折叠
  const [expanded, setExpanded] = useState(false)

  const totalTokens = trace.usage?.total_tokens
  // response 里可能是 { content } 或直接是文本 / tool_calls
  const respContent =
    trace.response && typeof trace.response === 'object'
      ? trace.response.content
      : trace.response
  const respToolCalls = trace.tool_calls ?? trace.response?.tool_calls

  return (
    <div className="rounded border border-ctp-surface bg-ctp-base">
      {/* 卡片标题：点击折叠/展开 */}
      <button
        onClick={() => setExpanded((v) => !v)}
        className="flex w-full items-center gap-2 px-2 py-1.5 text-left text-xs hover:bg-ctp-surface/40"
      >
        {/* 折叠箭头 */}
        <span className="text-ctp-overlay">{expanded ? '▾' : '▸'}</span>
        {/* 节点 ID */}
        <span className="font-mono font-semibold text-ctp-mauve">
          {trace.node_id ?? `#${index + 1}`}
        </span>
        {/* 模型名 */}
        {trace.model && (
          <span className="truncate text-ctp-blue">{trace.model}</span>
        )}
        {/* 时间 */}
        <span className="ml-auto font-mono text-ctp-overlay">
          {formatTime(trace.timestamp)}
        </span>
        {/* token 总数（若有） */}
        {totalTokens !== undefined && totalTokens !== null && (
          <span className="rounded bg-ctp-surface px-1.5 py-0.5 text-[10px] text-ctp-green">
            {totalTokens} tok
          </span>
        )}
      </button>

      {/* 展开内容 */}
      {expanded && (
        <div className="space-y-2 border-t border-ctp-surface p-2">
          {/* 请求消息 */}
          <div>
            <div className="mb-1 text-[10px] font-semibold uppercase tracking-wide text-ctp-overlay">
              请求消息
            </div>
            {Array.isArray(trace.request_messages) &&
            trace.request_messages.length > 0 ? (
              <div className="space-y-1">
                {trace.request_messages.map((m, i) => (
                  <div
                    key={i}
                    className="rounded bg-ctp-surface/50 p-1.5"
                  >
                    <div className="mb-0.5 text-[10px] font-semibold text-ctp-peach">
                      {m.role ?? 'message'}
                    </div>
                    <pre className="max-h-40 overflow-auto whitespace-pre-wrap break-words text-[11px] text-ctp-subtext">
                      {pretty(m.content)}
                    </pre>
                  </div>
                ))}
              </div>
            ) : (
              <div className="text-[11px] text-ctp-overlay">—</div>
            )}
          </div>

          {/* 响应内容 */}
          <div>
            <div className="mb-1 text-[10px] font-semibold uppercase tracking-wide text-ctp-overlay">
              响应
            </div>
            {respContent !== undefined && respContent !== null ? (
              <pre className="max-h-48 overflow-auto whitespace-pre-wrap break-words rounded bg-ctp-surface/50 p-1.5 text-[11px] text-ctp-text">
                {pretty(respContent)}
              </pre>
            ) : (
              <div className="text-[11px] text-ctp-overlay">—</div>
            )}
          </div>

          {/* 工具调用（若有） */}
          {respToolCalls && (
            <div>
              <div className="mb-1 text-[10px] font-semibold uppercase tracking-wide text-ctp-overlay">
                工具调用
              </div>
              <pre className="max-h-40 overflow-auto whitespace-pre-wrap break-words rounded bg-ctp-surface/50 p-1.5 text-[11px] text-ctp-yellow">
                {pretty(respToolCalls)}
              </pre>
            </div>
          )}

          {/* token 用量明细 */}
          {trace.usage && (
            <div>
              <div className="mb-1 text-[10px] font-semibold uppercase tracking-wide text-ctp-overlay">
                token 用量
              </div>
              <pre className="overflow-auto whitespace-pre-wrap break-words rounded bg-ctp-surface/50 p-1.5 text-[11px] text-ctp-subtext">
                {pretty(trace.usage)}
              </pre>
            </div>
          )}
        </div>
      )}
    </div>
  )
}

export default function TraceDrawer({ taskId, open, onClose }: TraceDrawerProps) {
  // 本地 trace 列表状态
  const [traces, setTraces] = useState<TraceRecord[]>([])
  // 加载中 / 错误状态
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  // 打开且 taskId 非空时加载 traces
  useEffect(() => {
    // 未打开或无任务：不加载
    if (!open || !taskId) return
    let cancelled = false
    setLoading(true)
    setError(null)
    api
      .getTaskTraces(taskId)
      .then((data) => {
        if (cancelled) return
        setTraces(Array.isArray(data) ? data : [])
      })
      .catch((e) => {
        if (cancelled) return
        setError((e as Error).message)
        setTraces([])
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    // 组件卸载 / 依赖变化时取消回填，避免竞态
    return () => {
      cancelled = true
    }
  }, [open, taskId])

  // 未打开时不渲染任何内容
  if (!open) return null

  return (
    <>
      {/* 半透明遮罩：点击关闭 */}
      <div
        className="fixed inset-0 z-40 bg-black/50"
        onClick={onClose}
      />

      {/* 右侧抽屉主体 */}
      <div className="fixed right-0 top-0 z-50 flex h-full w-[480px] flex-col border-l border-ctp-surface bg-ctp-mantle text-ctp-text shadow-xl">
        {/* 抽屉标题栏 */}
        <div className="flex items-center justify-between border-b border-ctp-surface px-3 py-2">
          <div className="flex items-center gap-2">
            <span className="text-sm font-semibold text-ctp-mauve">
              LLM Trace
            </span>
            {taskId && (
              <span className="font-mono text-[11px] text-ctp-overlay">
                {taskId.slice(0, 8)}
              </span>
            )}
            {!loading && (
              <span className="text-[11px] text-ctp-overlay">
                {traces.length} 条
              </span>
            )}
          </div>
          <button
            onClick={onClose}
            className="rounded px-2 py-0.5 text-sm text-ctp-subtext hover:bg-ctp-surface"
          >
            ✕
          </button>
        </div>

        {/* 列表主体（可滚动） */}
        <div className="min-h-0 flex-1 space-y-2 overflow-y-auto p-3">
          {loading ? (
            <div className="text-xs text-ctp-overlay">加载中…</div>
          ) : error ? (
            <div className="text-xs text-ctp-red">加载失败：{error}</div>
          ) : traces.length === 0 ? (
            <div className="text-xs text-ctp-overlay">暂无 LLM 调用记录</div>
          ) : (
            traces.map((t, i) => (
              <TraceCard key={i} trace={t} index={i} />
            ))
          )}
        </div>
      </div>
    </>
  )
}
