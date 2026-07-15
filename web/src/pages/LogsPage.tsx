import { useEffect, useState } from 'react'
import AppShell from '../components/AppShell'
import InteractionForm from '../components/InteractionForm'
import { api } from '../lib/api'
import type { InteractionRecord, SessionMeta, TranscriptEntry } from '../lib/types'

function pretty(value: unknown): string {
  if (value === undefined || value === null) return '-'
  if (typeof value === 'string') return value
  return JSON.stringify(value, null, 2)
}

export default function LogsPage({ embedded = false }: { embedded?: boolean }) {
  const [sessions, setSessions] = useState<SessionMeta[]>([])
  const [active, setActive] = useState<SessionMeta | null>(null)
  const [transcript, setTranscript] = useState<TranscriptEntry[]>([])
  const [events, setEvents] = useState<unknown[]>([])
  const [traces, setTraces] = useState<unknown[]>([])
  const [interactions, setInteractions] = useState<InteractionRecord[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function loadSession(session: SessionMeta) {
    setActive(session)
    setLoading(true)
    setError(null)
    try {
      const [
        nextTranscript,
        nextEvents,
        nextTraces,
        nextInteractions,
      ] = await Promise.all([
        api.getSessionTranscript(session.session_id),
        api.getSessionEvents(session.session_id),
        api.getSessionTraces(session.session_id),
        api.getSessionInteractions(session.session_id),
      ])
      setTranscript(nextTranscript)
      setEvents(nextEvents)
      setTraces(nextTraces)
      setInteractions(nextInteractions)
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
      setTranscript([])
      setEvents([])
      setTraces([])
      setInteractions([])
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    let alive = true

    async function init() {
      setError(null)
      try {
        const list = await api.listSessions()
        if (!alive) return
        setSessions(list)
        if (list.length > 0) {
          await loadSession(list[0])
        }
      } catch (err) {
        if (alive) {
          setError(err instanceof Error ? err.message : String(err))
        }
      }
    }

    void init()
    return () => {
      alive = false
    }
  }, [])

  const answeredInteractionIds = new Set(
    interactions
      .filter((interaction) => interaction.type === 'interaction_answered')
      .map((interaction) => interaction.interaction_id),
  )
  const pendingInteractions = interactions.filter(
    (interaction) =>
      interaction.type === 'interaction_requested' &&
      interaction.status !== 'answered' &&
      !answeredInteractionIds.has(interaction.interaction_id),
  )

  const content = (
    <div className="flex h-full min-h-0 bg-ctp-base text-ctp-text">
        <aside className="w-80 overflow-y-auto border-r border-ctp-surface bg-ctp-mantle p-2">
          {sessions.length === 0 ? (
            <div className="p-2 text-sm text-ctp-subtext">No sessions</div>
          ) : (
            sessions.map((session) => (
              <button
                key={session.session_id}
                type="button"
                onClick={() => void loadSession(session)}
                className={`mb-1 block w-full rounded p-2 text-left ${
                  active?.session_id === session.session_id
                    ? 'bg-ctp-surface'
                    : 'hover:bg-ctp-surface/50'
                }`}
              >
                <div className="truncate text-sm text-ctp-text">
                  {session.title}
                </div>
                <div className="font-mono text-[11px] text-ctp-overlay">
                  {session.type} | {session.status}
                </div>
                <div className="truncate font-mono text-[10px] text-ctp-overlay">
                  {session.session_id}
                </div>
              </button>
            ))
          )}
        </aside>
        <main className="min-w-0 flex-1 overflow-y-auto p-4">
          {!active ? (
            <div className="text-sm text-ctp-subtext">Select a session</div>
          ) : (
            <div className="space-y-4">
              <section>
                <div className="mb-2 flex items-start justify-between gap-3">
                  <div>
                    <h2 className="text-sm font-semibold text-ctp-mauve">
                      {active.title}
                    </h2>
                    <div className="font-mono text-xs text-ctp-overlay">
                      {active.session_id}
                    </div>
                  </div>
                  {loading && (
                    <span className="rounded bg-ctp-surface px-2 py-1 text-xs text-ctp-subtext">
                      Loading...
                    </span>
                  )}
                </div>
                <pre className="overflow-auto rounded bg-ctp-mantle p-3 text-xs">
                  {pretty(active)}
                </pre>
              </section>

              {error && (
                <div className="rounded border border-ctp-red bg-ctp-mantle p-3 text-sm text-ctp-red">
                  {error}
                </div>
              )}

              {pendingInteractions.map((interaction) => (
                <InteractionForm
                  key={interaction.interaction_id}
                  interaction={interaction}
                  onSubmit={async (answer) => {
                    await api.answerInteraction(
                      active.session_id,
                      interaction.interaction_id,
                      answer,
                    )
                    await loadSession(active)
                  }}
                />
              ))}

              <section>
                <h3 className="mb-2 text-xs font-semibold uppercase text-ctp-overlay">
                  Transcript
                </h3>
                <pre className="max-h-72 overflow-auto rounded bg-ctp-mantle p-3 text-xs">
                  {pretty(transcript)}
                </pre>
              </section>
              <section>
                <h3 className="mb-2 text-xs font-semibold uppercase text-ctp-overlay">
                  Events
                </h3>
                <pre className="max-h-72 overflow-auto rounded bg-ctp-mantle p-3 text-xs">
                  {pretty(events)}
                </pre>
              </section>
              <section>
                <h3 className="mb-2 text-xs font-semibold uppercase text-ctp-overlay">
                  Traces
                </h3>
                <pre className="max-h-72 overflow-auto rounded bg-ctp-mantle p-3 text-xs">
                  {pretty(traces)}
                </pre>
              </section>
              <section>
                <h3 className="mb-2 text-xs font-semibold uppercase text-ctp-overlay">
                  Interactions
                </h3>
                <pre className="max-h-72 overflow-auto rounded bg-ctp-mantle p-3 text-xs">
                  {pretty(interactions)}
                </pre>
              </section>
            </div>
          )}
        </main>
      </div>
  )

  if (embedded) return content

  return (
    <AppShell
      title="Session Logs"
      eyebrow="Observability"
      description="Browse chat transcripts, SOP events, traces, and pending human interactions."
      bodyClassName="flex flex-col"
    >
      {content}
    </AppShell>
  )
}
