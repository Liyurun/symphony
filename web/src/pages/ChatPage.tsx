import { useEffect, useRef, useState } from 'react'
import AppShell from '../components/AppShell'
import { ToolCallSummary, ToolResultSummary } from '../components/ToolSummary'
import { api } from '../lib/api'
import { createChatSocket, type ChatEvent } from '../lib/ws'

type DialogueMessage = {
  role: 'user' | 'assistant'
  content: string
}

type Message = DialogueMessage | { role: 'status'; event: ChatEvent }

function isDialogueMessage(message: Message): message is DialogueMessage {
  return message.role === 'user' || message.role === 'assistant'
}

function appendAssistantDelta(messages: Message[], text: string): Message[] {
  const next = [...messages]
  for (let i = next.length - 1; i >= 0; i -= 1) {
    const message = next[i]
    if (message.role === 'assistant') {
      next[i] = { role: 'assistant', content: message.content + text }
      return next
    }
  }
  return [...next, { role: 'assistant', content: text }]
}

export default function ChatPage() {
  const [sessionId, setSessionId] = useState<string | null>(null)
  const [messages, setMessages] = useState<Message[]>([])
  const [input, setInput] = useState('')
  const [streaming, setStreaming] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const socketCleanup = useRef<(() => void) | null>(null)

  useEffect(() => {
    return () => {
      socketCleanup.current?.()
      socketCleanup.current = null
    }
  }, [])

  function closeCurrentSocket() {
    const cleanup = socketCleanup.current
    socketCleanup.current = null
    cleanup?.()
  }

  async function send() {
    const question = input.trim()
    if (!question || streaming) return

    setInput('')
    setError(null)
    setStreaming(true)
    closeCurrentSocket()

    try {
      let activeSessionId = sessionId
      if (!activeSessionId) {
        const session = await api.createChatSession(
          question.slice(0, 40) || 'New chat',
        )
        activeSessionId = session.session_id
        setSessionId(activeSessionId)
      }

      const history = messages
        .filter(isDialogueMessage)
        .map((message) => ({
          role: message.role,
          content: message.content,
        }))

      setMessages((prev) => [
        ...prev,
        { role: 'user', content: question },
        { role: 'assistant', content: '' },
      ])

      let cleanup: (() => void) | null = null
      cleanup = createChatSocket(
        activeSessionId,
        question,
        history,
        (event) => {
          if (event.type === 'chat_answer_delta') {
            setMessages((prev) =>
              appendAssistantDelta(prev, event.text ?? ''),
            )
          } else if (event.type === 'chat_tool_call' || event.type === 'chat_tool_result') {
            setMessages((prev) => [...prev, { role: 'status', event }])
          } else if (event.type === 'chat_failed') {
            setError(event.error || 'Chat failed')
            setStreaming(false)
            closeCurrentSocket()
          } else if (event.type === 'chat_completed') {
            setStreaming(false)
            closeCurrentSocket()
          }
        },
        () => {
          if (cleanup && socketCleanup.current === cleanup) {
            socketCleanup.current = null
            setStreaming(false)
          }
        },
      )
      socketCleanup.current = cleanup
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
      setStreaming(false)
    }
  }

  return (
    <AppShell
      title="Chat Console"
      eyebrow="Live Agent"
      description="Start an agent session and inspect compact tool-call summaries."
      bodyClassName="flex flex-col"
      meta={
        sessionId ? (
          <span className="rounded border border-ctp-surface bg-ctp-base/60 px-2 py-1 font-mono text-ctp-overlay">
            {sessionId}
          </span>
        ) : (
          <span className="rounded border border-ctp-surface bg-ctp-base/60 px-2 py-1 text-ctp-overlay">
            no session
          </span>
        )
      }
    >
      <main className="min-h-0 flex-1 overflow-y-auto p-4">
        <div className="mx-auto max-w-3xl space-y-3">
          {messages.length === 0 && (
            <div className="rounded-lg border border-ctp-surface bg-ctp-mantle p-4 text-sm text-ctp-subtext shadow-[0_18px_60px_rgba(0,0,0,0.18)]">
              Start a Chat session. Tool calls and results will appear as compact one-line summaries.
            </div>
          )}

          {messages.map((message, index) => {
            if (message.role === 'status') {
              const event = message.event
              if (event.type === 'chat_tool_call') {
                return (
                  <ToolCallSummary
                    key={index}
                    name={event.skill_name || 'tool'}
                    args={event.args || {}}
                  />
                )
              }
              return (
                <ToolResultSummary
                  key={index}
                  name={event.skill_name || 'tool'}
                  ok={Boolean(event.ok)}
                  detail={event.detail}
                />
              )
            }

            return (
              <div
                key={index}
                className={`rounded-lg border border-ctp-surface p-3 ${
                  message.role === 'user' ? 'bg-ctp-surface/40' : 'bg-ctp-mantle'
                }`}
              >
                <div className="mb-1 text-xs uppercase tracking-wide text-ctp-overlay">
                  {message.role}
                </div>
                <pre className="whitespace-pre-wrap break-words text-sm">
                  {message.content}
                </pre>
              </div>
            )
          })}

          {error && (
            <div className="rounded-lg border border-ctp-red/30 bg-ctp-surface p-2 text-sm text-ctp-red">
              {error}
            </div>
          )}
        </div>
      </main>
      <footer className="border-t border-ctp-surface bg-ctp-mantle p-3">
        <div className="mx-auto flex max-w-3xl gap-2">
          <input
            value={input}
            onChange={(event) => setInput(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === 'Enter') {
                event.preventDefault()
                void send()
              }
            }}
            className="min-w-0 flex-1 rounded-lg border border-ctp-surface bg-ctp-base p-2 text-sm outline-none focus:border-emerald-300/70"
            aria-label="Ask Symphony"
            placeholder="Ask Symphony..."
            disabled={streaming}
          />
          <button
            onClick={() => void send()}
            disabled={streaming || !input.trim()}
            className="rounded-lg bg-emerald-300 px-4 py-2 text-sm font-semibold text-ctp-base disabled:opacity-50"
          >
            {streaming ? 'Streaming' : 'Send'}
          </button>
        </div>
      </footer>
    </AppShell>
  )
}
