// InteractionCard：SOP 运行中等待用户确认的反问卡片。
// 支持选项单选/多选与自由文本输入，提交时统一把用户答案包装成 answer object。
import { useState } from 'react'
import type { PendingInteraction } from '../lib/types'

export default function InteractionCard({
  interaction,
  onAnswer,
}: {
  interaction: PendingInteraction
  onAnswer: (answer: Record<string, any>) => Promise<void>
}) {
  const [text, setText] = useState('')
  const [selected, setSelected] = useState<any[]>([])
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const options = interaction.options ?? []

  async function submit(answer: Record<string, any>) {
    setSubmitting(true)
    setError(null)
    try {
      await onAnswer(answer)
      setText('')
      setSelected([])
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div className="mb-3 rounded border border-ctp-yellow bg-ctp-base p-3 text-xs">
      <div className="mb-2 flex items-center justify-between gap-2">
        <div className="font-semibold text-ctp-yellow">待确认</div>
        <div className="font-mono text-[10px] text-ctp-overlay">
          attempt {interaction.attempt_no}
        </div>
      </div>
      <div className="mb-2 whitespace-pre-wrap text-ctp-text">
        {interaction.prompt}
      </div>

      {options.length > 0 ? (
        <>
          <div className="mb-2 flex flex-wrap gap-2">
            {options.map((option, index) => {
              const active = selected.includes(option.value)
              return (
                <button
                  key={`${String(option.value)}-${index}`}
                  type="button"
                  onClick={() => {
                    if (interaction.multi_select) {
                      setSelected((prev) =>
                        active
                          ? prev.filter((value) => value !== option.value)
                          : [...prev, option.value],
                      )
                    } else {
                      setSelected([option.value])
                    }
                  }}
                  className={`rounded px-2 py-1 ${
                    active
                      ? 'bg-ctp-mauve text-ctp-base'
                      : 'bg-ctp-surface text-ctp-text'
                  }`}
                >
                  {option.label}
                </button>
              )
            })}
          </div>
          <button
            type="button"
            disabled={submitting || selected.length === 0}
            onClick={() =>
              void submit({
                value: interaction.multi_select ? selected : selected[0],
              })
            }
            className="rounded bg-ctp-green px-2 py-1 text-ctp-base disabled:opacity-50"
          >
            {submitting ? '提交中…' : '确认提交'}
          </button>
        </>
      ) : (
        <>
          <textarea
            value={text}
            onChange={(event) => setText(event.target.value)}
            rows={3}
            className="mb-2 w-full rounded border border-ctp-surface bg-ctp-mantle p-2 text-ctp-text outline-none focus:border-ctp-mauve"
            placeholder="请输入确认内容"
          />
          <button
            type="button"
            disabled={submitting || text.trim().length === 0}
            onClick={() => void submit({ text })}
            className="rounded bg-ctp-green px-2 py-1 text-ctp-base disabled:opacity-50"
          >
            {submitting ? '提交中…' : '确认提交'}
          </button>
        </>
      )}

      {error && <div className="mt-2 text-ctp-red">提交失败：{error}</div>}
    </div>
  )
}
