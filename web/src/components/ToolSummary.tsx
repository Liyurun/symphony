// Chat 工具事件的一行摘要展示。
// Web Chat 中只展示可扫读的调用/结果摘要，完整细节保留在 session 日志里。

function brief(value: unknown): string {
  let text: string
  if (typeof value === 'string') {
    text = value
  } else {
    try {
      text = JSON.stringify(value) ?? String(value)
    } catch {
      text = String(value)
    }
  }
  return text.length > 140 ? `${text.slice(0, 137)}...` : text
}

function argPreview(args: Record<string, any> = {}): string {
  for (const key of ['command', 'cmd', 'url', 'path', 'query', 'operation']) {
    if (args[key] !== undefined && args[key] !== null) {
      return `${key}="${brief(args[key])}"`
    }
  }
  return brief(args)
}

export function ToolCallSummary({
  name,
  args,
}: {
  name: string
  args: Record<string, any>
}) {
  return (
    <div className="truncate font-mono text-xs text-ctp-peach">
      {`-> ${name} ${argPreview(args)}`}
    </div>
  )
}

export function ToolResultSummary({
  name,
  ok,
  detail,
}: {
  name: string
  ok: boolean
  detail?: string
}) {
  return (
    <div
      className={`truncate font-mono text-xs ${ok ? 'text-ctp-green' : 'text-ctp-red'}`}
    >
      {ok ? `<- ${name} ok` : `[!] ${name} ${brief(detail || 'failed')}`}
    </div>
  )
}
