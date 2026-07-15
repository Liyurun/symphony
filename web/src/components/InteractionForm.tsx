import { useState } from 'react'
import type { InteractionRecord } from '../lib/types'

export default function InteractionForm({
  interaction,
  onSubmit,
}: {
  interaction: InteractionRecord
  onSubmit: (answer: Record<string, any>) => void | Promise<void>
}) {
  const [text, setText] = useState('')
  const [selected, setSelected] = useState<any[]>([])
  const [submitting, setSubmitting] = useState(false)
  const options = interaction.options ?? []

  async function submit(answer: Record<string, any>) {
    setSubmitting(true)
    try {
      await onSubmit(answer)
    } finally {
      setSubmitting(false)
    }
  }

  if (options.length > 0) {
    return (
      <div className="space-y-2 rounded border border-ctp-surface bg-ctp-base p-3">
        <div className="text-sm text-ctp-text">
          {interaction.prompt ?? 'Please choose an option'}
        </div>
        <div className="flex flex-wrap gap-2">
          {options.map((option) => {
            const active = selected.includes(option.value)
            return (
              <button
                key={String(option.value)}
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
                className={`rounded px-2 py-1 text-xs ${
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
          onClick={() =>
            void submit({
              value: interaction.multi_select ? selected : selected[0],
            })
          }
          disabled={submitting || selected.length === 0}
          className="rounded bg-ctp-green px-2 py-1 text-xs text-ctp-base disabled:opacity-50"
        >
          {submitting ? 'Submitting...' : 'Submit'}
        </button>
      </div>
    )
  }

  return (
    <div className="space-y-2 rounded border border-ctp-surface bg-ctp-base p-3">
      <div className="text-sm text-ctp-text">
        {interaction.prompt ?? 'Please provide input'}
      </div>
      <textarea
        value={text}
        onChange={(event) => setText(event.target.value)}
        rows={3}
        className="w-full rounded border border-ctp-surface bg-ctp-mantle p-2 text-sm outline-none focus:border-ctp-mauve"
      />
      <button
        type="button"
        onClick={() => void submit({ text })}
        disabled={submitting || text.trim().length === 0}
        className="rounded bg-ctp-green px-2 py-1 text-xs text-ctp-base disabled:opacity-50"
      >
        {submitting ? 'Submitting...' : 'Submit'}
      </button>
    </div>
  )
}
