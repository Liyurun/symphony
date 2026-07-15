import { useState } from 'react'

function isPlainObject(v: unknown): v is Record<string, unknown> {
  return typeof v === 'object' && v !== null && !Array.isArray(v)
}

function extractResult(value: unknown): unknown {
  if (!isPlainObject(value)) return value
  if ('result' in value) return value.result
  if ('output' in value) return value.output
  if ('content' in value) return value.content
  if ('answer' in value) return value.answer
  if ('data' in value) return value.data
  return value
}

function SummaryView({ value }: { value: unknown }) {
  const result = extractResult(value)

  if (result === undefined || result === null) {
    return <div className="text-xs text-ctp-overlay">无内容</div>
  }

  if (typeof result === 'string') {
    return (
      <pre className="max-h-[400px] overflow-auto whitespace-pre-wrap rounded bg-ctp-base p-2 text-xs text-ctp-text">
        {result}
      </pre>
    )
  }

  if (typeof result === 'number' || typeof result === 'boolean') {
    return (
      <div className="rounded bg-ctp-base p-2 font-mono text-xs text-ctp-text">
        {String(result)}
      </div>
    )
  }

  if (Array.isArray(result)) {
    return (
      <div className="space-y-2">
        {result.map((item, idx) => (
          <div key={idx} className="rounded border border-ctp-surface bg-ctp-base p-2">
            <div className="mb-1 font-mono text-[10px] text-ctp-overlay">[{idx}]</div>
            <SummaryView value={item} />
          </div>
        ))}
      </div>
    )
  }

  if (isPlainObject(result)) {
    const entries = Object.entries(result)
    const importantKeys = [
      'result',
      'content',
      'answer',
      'output',
      'data',
      'text',
      'message',
      'title',
      'summary',
      'deploy_result',
      'test_report',
      'etl_code',
      'requirement_spec',
      'data_model',
    ]
    const important = entries.filter(([k]) => importantKeys.includes(k))
    const others = entries.filter(([k]) => !importantKeys.includes(k))

    return (
      <div className="space-y-2">
        {important.map(([k, v]) => (
          <div key={k} className="rounded border-l-2 border-ctp-mauve bg-ctp-base p-2">
            <div className="mb-1 font-mono text-[10px] font-semibold text-ctp-mauve">{k}</div>
            {typeof v === 'string' ? (
              <pre className="whitespace-pre-wrap text-xs text-ctp-text">{v}</pre>
            ) : (
              <SummaryView value={v} />
            )}
          </div>
        ))}
        {others.length > 0 && (
          <details className="rounded bg-ctp-surface/40">
            <summary className="cursor-pointer select-none px-2 py-1 text-[10px] text-ctp-overlay hover:text-ctp-subtext">
              其他字段 ({others.length})
            </summary>
            <div className="space-y-1 p-2 pt-0">
              {others.map(([k, v]) => (
                <div key={k} className="text-xs">
                  <span className="font-mono text-ctp-blue">{k}:</span>{' '}
                  <span className="text-ctp-subtext">
                    {typeof v === 'string' ? v.slice(0, 200) : JSON.stringify(v).slice(0, 200)}
                    {((typeof v === 'string' ? v.length : JSON.stringify(v).length) > 200) ? '…' : ''}
                  </span>
                </div>
              ))}
            </div>
          </details>
        )}
      </div>
    )
  }

  return <div className="text-xs text-ctp-overlay">不支持的类型</div>
}

function RawView({ value }: { value: unknown }) {
  const text = (() => {
    if (value === undefined || value === null) return ''
    if (typeof value === 'string') return value
    try {
      return JSON.stringify(value, null, 2)
    } catch {
      return String(value)
    }
  })()

  return (
    <pre className="max-h-[500px] overflow-auto whitespace-pre-wrap rounded bg-ctp-base p-2 font-mono text-[11px] text-ctp-subtext">
      {text || '—'}
    </pre>
  )
}

export default function ValuePreview({
  value,
  label,
  defaultMode = 'summary',
}: {
  value: unknown
  label?: string
  defaultMode?: 'summary' | 'raw'
}) {
  const [mode, setMode] = useState<'summary' | 'raw'>(defaultMode)

  return (
    <div>
      <div className="mb-1.5 flex items-center justify-between">
        {label && <span className="text-[11px] font-semibold uppercase tracking-wide text-ctp-overlay">{label}</span>}
        <div className="flex gap-0.5 rounded bg-ctp-surface/60 p-0.5">
          <button
            onClick={() => setMode('summary')}
            className={`rounded px-1.5 py-0.5 text-[10px] ${
              mode === 'summary' ? 'bg-ctp-mauve text-ctp-base' : 'text-ctp-subtext hover:text-ctp-text'
            }`}
          >
            摘要
          </button>
          <button
            onClick={() => setMode('raw')}
            className={`rounded px-1.5 py-0.5 text-[10px] ${
              mode === 'raw' ? 'bg-ctp-mauve text-ctp-base' : 'text-ctp-subtext hover:text-ctp-text'
            }`}
          >
            原始
          </button>
        </div>
      </div>
      {mode === 'summary' ? <SummaryView value={value} /> : <RawView value={value} />}
    </div>
  )
}
