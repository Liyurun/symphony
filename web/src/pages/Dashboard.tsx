import { useEffect, useRef, useState } from 'react'
import { useStore } from '../lib/store'
import { api } from '../lib/api'
import { createEventSocket } from '../lib/ws'
import type { IOField, PendingInteraction, SOPTemplate } from '../lib/types'
import AppShell from '../components/AppShell'
import TaskKanban from '../components/TaskKanban'
import DagView from '../components/DagView'
import NodePanel from '../components/NodePanel'
import NodeCorrectionModal from '../components/NodeCorrectionModal'
import TraceDrawer from '../components/TraceDrawer'
import Resizer from '../components/Resizer'
import EventLog from '../components/EventLog'
import SopsPage from './SopsPage'
import LogsPage from './LogsPage'

export default function Dashboard() {
  const setTasks = useStore((s) => s.setTasks)
  const setSkills = useStore((s) => s.setSkills)
  const setActiveTask = useStore((s) => s.setActiveTask)
  const activeTaskId = useStore((s) => s.activeTaskId)
  const setSnapshot = useStore((s) => s.setSnapshot)
  const snapshot = useStore((s) => s.snapshot)
  const setEventsForTask = useStore((s) => s.setEventsForTask)
  const appendEvent = useStore((s) => s.appendEvent)
  const setConnection = useStore((s) => s.setConnection)
  const connection = useStore((s) => s.connection)
  const selectNode = useStore((s) => s.selectNode)
  const selectedNodeId = useStore((s) => s.selectedNodeId)
  const eventsByTask = useStore((s) => s.eventsByTask)
  const sops = useStore((s) => s.sops)
  const setSops = useStore((s) => s.setSops)
  const sopRunTab = useStore((s) => s.sopRunTab)
  const setSopRunTab = useStore((s) => s.setSopRunTab)
  const rightPanelWidth = useStore((s) => s.rightPanelWidth)
  const setRightPanelWidth = useStore((s) => s.setRightPanelWidth)

  const [activeSop, setActiveSop] = useState<SOPTemplate | null>(null)
  const [showNewTask, setShowNewTask] = useState(false)
  const [traceOpen, setTraceOpen] = useState(false)
  const [correctionNodeId, setCorrectionNodeId] = useState<string | null>(null)
  const [pendingInteractions, setPendingInteractions] = useState<PendingInteraction[]>([])
  const [dagLog, setDagLog] = useState<any | null>(null)

  const wsCleanupRef = useRef<(() => void) | null>(null)

  useEffect(() => {
    api.listTasks().then(setTasks).catch((e) => console.error('加载任务列表失败', e))
    api.listSkills().then(setSkills).catch((e) => console.error('加载技能列表失败', e))
  }, [setTasks, setSkills])

  useEffect(() => {
    return () => {
      wsCleanupRef.current?.()
      wsCleanupRef.current = null
    }
  }, [])

  async function refreshDagLog(taskId: string) {
    try {
      const log = await api.getTaskDagLog(taskId)
      setDagLog(log)
    } catch (e) {
      console.error('加载 DAG 日志失败', e)
      setDagLog(null)
    }
  }

  async function handleSelectTask(taskId: string) {
    wsCleanupRef.current?.()
    wsCleanupRef.current = null

    setActiveTask(taskId)
    selectNode(null)
    setCorrectionNodeId(null)
    setPendingInteractions([])
    setDagLog(null)
    setSopRunTab('dag')

    try {
      const snap = await api.getTask(taskId)
      setSnapshot(snap)

      try {
        const pending = await api.getPendingInteractions(taskId)
        setPendingInteractions(pending)
      } catch (e) {
        console.error('加载待确认交互失败', e)
        setPendingInteractions([])
      }

      const cached = sops.find((s) => s.id === snap.sop_id)
      if (cached) {
        setActiveSop(cached)
      } else {
        try {
          const sop = await api.getSop(snap.sop_id)
          setActiveSop(sop)
        } catch (e) {
          console.error('加载 SOP 失败', e)
          setActiveSop(null)
        }
      }

      const evs = await api.getTaskEvents(taskId)
      setEventsForTask(taskId, evs)

      await refreshDagLog(taskId)
    } catch (e) {
      console.error('加载任务快照失败', e)
    }

    wsCleanupRef.current = createEventSocket(
      taskId,
      (e) => appendEvent(taskId, e),
      setConnection,
    )
  }

  async function handleIntervene(nodeId: string, action: string, data: Record<string, any>) {
    if (!activeTaskId) return
    try {
      await api.intervene(activeTaskId, nodeId, action, data)
      const snap = await api.getTask(activeTaskId)
      setSnapshot(snap)
    } catch (e) {
      console.error('介入失败', e)
      alert('介入失败：' + (e as Error).message)
    }
  }

  async function handleRetrySubnode(nodeId: string, subNodeId: string, retryPrompt: string) {
    if (!activeTaskId) return
    try {
      await api.retrySubnode(activeTaskId, nodeId, subNodeId, retryPrompt)
      const snap = await api.getTask(activeTaskId)
      setSnapshot(snap)
    } catch (e) {
      console.error('子节点重跑失败', e)
      alert('子节点重跑失败：' + (e as Error).message)
    }
  }

  async function handleRerunNode(nodeId: string, instruction: string) {
    if (!activeTaskId) return
    try {
      await api.rerunNode(activeTaskId, nodeId, instruction, true)
      const snap = await api.getTask(activeTaskId)
      setSnapshot(snap)
      const evs = await api.getTaskEvents(activeTaskId)
      setEventsForTask(activeTaskId, evs)
      await refreshDagLog(activeTaskId)
    } catch (e) {
      console.error('节点纠偏重跑失败', e)
      alert('重跑失败：' + (e as Error).message)
      throw e
    }
  }

  async function handleAnswerInteraction(interactionId: string, answer: Record<string, any>) {
    if (!activeTaskId) return
    await api.answerTaskInteraction(activeTaskId, interactionId, answer)
    const [snap, pending] = await Promise.all([
      api.getTask(activeTaskId),
      api.getPendingInteractions(activeTaskId),
    ])
    setSnapshot(snap)
    setPendingInteractions(pending)
    await refreshDagLog(activeTaskId)
  }

  const activeEvents = activeTaskId ? (eventsByTask[activeTaskId] ?? []) : []
  const secondaryTabs = [
    { id: 'dag', label: 'DAG 视图', active: sopRunTab === 'dag', onClick: () => setSopRunTab('dag') },
    { id: 'tasks', label: '任务列表', active: sopRunTab === 'tasks', onClick: () => setSopRunTab('tasks') },
    { id: 'events', label: '事件日志', active: sopRunTab === 'events', onClick: () => setSopRunTab('events') },
    { id: 'studio', label: 'SOP 模板', active: sopRunTab === 'studio', onClick: () => setSopRunTab('studio') },
    { id: 'logs', label: '系统日志', active: sopRunTab === 'logs', onClick: () => setSopRunTab('logs') },
  ]

  return (
    <AppShell
      title="SOP Runs"
      eyebrow="Runtime Control"
      description="Monitor DAG execution, inspect node I/O, and correct stale workflow branches."
      secondaryTabs={secondaryTabs}
      actions={
        <>
          {pendingInteractions.length > 0 && (
            <button
              onClick={() => selectNode(pendingInteractions[0].node_id)}
              className="rounded-lg bg-ctp-yellow px-3 py-1.5 text-xs font-semibold text-ctp-base hover:brightness-110"
            >
              当前任务等待确认：{pendingInteractions.length}
            </button>
          )}
          <button
            onClick={() => {
              if (sops.length === 0) {
                api.listSops().then(setSops).catch(() => {})
              }
              setShowNewTask(true)
            }}
            className="rounded-lg bg-emerald-300 px-3 py-1.5 text-sm font-semibold text-ctp-base hover:brightness-110"
          >
            + 新建 SOP Run
          </button>
        </>
      }
      meta={
        activeTaskId && sopRunTab === 'dag' ? (
          <>
            <span className="max-w-[260px] truncate font-medium text-ctp-text">
              {activeSop?.name ?? snapshot?.sop_id ?? '未选择 SOP Run'}
            </span>
            <span className="font-mono text-ctp-overlay">{activeTaskId.slice(0, 8)}</span>
            <span
              className="text-[11px]"
              style={{
                color:
                  connection === 'open'
                    ? '#a6e3a1'
                    : connection === 'connecting'
                      ? '#f9e2af'
                      : '#6c7086',
              }}
            >
              ● {connection}
            </span>
            <button
              onClick={() => setTraceOpen(true)}
              className="rounded border border-ctp-surface bg-ctp-base/70 px-2 py-1 text-[11px] text-emerald-100 hover:border-emerald-300/40"
            >
              LLM Trace
            </button>
          </>
        ) : null
      }
    >

      {sopRunTab === 'studio' ? (
        <SopsPage embedded />
      ) : sopRunTab === 'logs' ? (
        <LogsPage embedded />
      ) : (
        <div className="flex min-h-0 flex-1 flex-col">

          {sopRunTab === 'tasks' ? (
            <div className="flex min-h-0 flex-1">
              <div className="w-[320px] shrink-0 border-r border-ctp-surface bg-ctp-mantle">
                <TaskKanban onSelect={(id) => { handleSelectTask(id); setSopRunTab('dag') }} />
              </div>
              <div className="flex min-h-0 flex-1 items-center justify-center text-ctp-overlay text-sm">
                {activeTaskId ? '已选中任务，点击「DAG 视图」查看执行详情' : '从左侧选择一个 SOP Run 开始查看'}
              </div>
            </div>
          ) : sopRunTab === 'events' ? (
            <div className="flex min-h-0 flex-1 flex-col bg-ctp-mantle">
              <div className="border-b border-ctp-surface px-4 py-2 text-xs text-ctp-subtext">
                {activeTaskId ? `任务 ${activeTaskId.slice(0, 16)}... 事件流` : '请先选择任务'}
              </div>
              <div className="min-h-0 flex-1">
                {activeTaskId ? (
                  <EventLog events={activeEvents} />
                ) : (
                  <div className="flex h-full items-center justify-center text-ctp-overlay text-sm">未选择任务</div>
                )}
              </div>
            </div>
          ) : (
            <div className="flex min-h-0 flex-1">
              <aside className="w-[280px] shrink-0 border-r border-ctp-surface bg-ctp-mantle">
                <TaskKanban onSelect={handleSelectTask} />
              </aside>

              <main className="flex min-w-0 flex-1 flex-col">
                <div className="min-h-0 flex-1">
                  <DagView
                    sop={activeSop}
                    snapshot={snapshot}
                    onSelectNode={selectNode}
                    selectedNodeId={selectedNodeId}
                  />
                </div>
              </main>

              <Resizer
                direction="horizontal"
                onResize={(delta) => setRightPanelWidth(rightPanelWidth + delta)}
              />

              <aside
                className="shrink-0 border-l border-ctp-surface"
                style={{ width: rightPanelWidth }}
              >
                <NodePanel
                  snapshot={snapshot}
                  selectedNodeId={selectedNodeId}
                  sop={activeSop}
                  events={activeEvents}
                  dagLog={dagLog}
                  onIntervene={handleIntervene}
                  onRetrySubnode={handleRetrySubnode}
                  onOpenCorrection={setCorrectionNodeId}
                  pendingInteractions={pendingInteractions}
                  onAnswerInteraction={handleAnswerInteraction}
                />
              </aside>
            </div>
          )}
        </div>
      )}

      {showNewTask && (
        <NewTaskModal
          onClose={() => setShowNewTask(false)}
          onCreated={async (taskId) => {
            setShowNewTask(false)
            try {
              const list = await api.listTasks()
              setTasks(list)
            } catch (e) {
              console.error('刷新任务列表失败', e)
            }
            handleSelectTask(taskId)
          }}
        />
      )}

      <TraceDrawer
        taskId={activeTaskId}
        open={traceOpen}
        onClose={() => setTraceOpen(false)}
      />

      <NodeCorrectionModal
        open={correctionNodeId !== null}
        nodeId={correctionNodeId}
        sop={activeSop}
        onClose={() => setCorrectionNodeId(null)}
        onSubmit={async (instruction) => {
          if (correctionNodeId) {
            await handleRerunNode(correctionNodeId, instruction)
          }
        }}
      />
    </AppShell>
  )
}

type FormValues = Record<string, any>

function defaultValueForField(field: IOField): any {
  if (field.type === 'json') return {}
  return ''
}

function buildInitialValues(fields: IOField[]): FormValues {
  return Object.fromEntries(fields.map((f) => [f.name, defaultValueForField(f)]))
}

function buildInitialJsonDrafts(fields: IOField[]): Record<string, string> {
  return Object.fromEntries(
    fields
      .filter((f) => f.type === 'json')
      .map((f) => [f.name, JSON.stringify(defaultValueForField(f), null, 2)]),
  )
}

function jsonObjectProperties(field: IOField): Record<string, any> {
  const schema = field.json_schema
  if (!schema || schema.type !== 'object' || !schema.properties) return {}
  return schema.properties as Record<string, any>
}

function coerceJsonScalar(value: string, schema: any): any {
  if (schema?.type === 'number') return value.trim() === '' ? undefined : Number(value)
  if (schema?.type === 'integer') return value.trim() === '' ? undefined : Number.parseInt(value, 10)
  if (schema?.type === 'boolean') return value === 'true'
  return value
}

function NewTaskModal({
  onClose,
  onCreated,
}: {
  onClose: () => void
  onCreated: (taskId: string, sessionId: string) => void
}) {
  const sops = useStore((s) => s.sops)
  const [sopId, setSopId] = useState('')
  const [variables, setVariables] = useState('{}')
  const selectedSop = sops.find((s) => s.id === sopId)
  const fields = selectedSop?.variables_def ?? []
  const useForm = fields.length > 0
  const [formValues, setFormValues] = useState<FormValues>({})
  const [jsonDrafts, setJsonDrafts] = useState<Record<string, string>>({})
  const [error, setError] = useState<string | null>(null)
  const [warning, setWarning] = useState<string | null>(null)
  const [submitting, setSubmitting] = useState(false)

  function updateSop(id: string) {
    const next = sops.find((s) => s.id === id)
    setSopId(id)
    setError(null)
    setWarning(null)
    setFormValues(buildInitialValues(next?.variables_def ?? []))
    setJsonDrafts(buildInitialJsonDrafts(next?.variables_def ?? []))
  }

  function setFieldValue(name: string, value: any) {
    setFormValues((prev) => ({ ...prev, [name]: value }))
  }

  function setJsonFieldValue(fieldName: string, key: string, value: any) {
    setFormValues((prev) => ({
      ...prev,
      [fieldName]: { ...(prev[fieldName] ?? {}), [key]: value },
    }))
  }

  function setJsonDraftValue(field: IOField, raw: string) {
    setJsonDrafts((prev) => ({ ...prev, [field.name]: raw }))
    try {
      setFieldValue(field.name, JSON.parse(raw.trim() === '' ? '{}' : raw))
      setWarning(null)
      setError(null)
    } catch {
      setFieldValue(field.name, raw)
      setWarning(`${field.label || field.name} 不是合法 JSON，将按文本提交`)
      setError(null)
    }
  }

  function buildVarsFromForm(): Record<string, any> | null {
    const vars: Record<string, any> = {}
    for (const field of fields) {
      const value = formValues[field.name]
      if (field.required !== false) {
        if (field.type === 'json') {
          const raw = jsonDrafts[field.name]
          const hasRawInput = typeof raw === 'string' && raw.trim() !== ''
          if (!hasRawInput && (value === undefined || value === null || value === '')) {
            setError(`请填写必填字段：${field.label || field.name}`)
            return null
          }
        } else if (typeof value !== 'string' || value.trim() === '') {
          setError(`请填写必填字段：${field.label || field.name}`)
          return null
        }
      }
      if (field.type === 'json' && jsonDrafts[field.name] !== undefined) {
        const raw = jsonDrafts[field.name]
        try {
          vars[field.name] = JSON.parse(raw.trim() === '' ? '{}' : raw)
        } catch {
          vars[field.name] = raw
          setWarning(`${field.label || field.name} 不是合法 JSON，将按文本提交`)
        }
      } else if (value !== undefined && value !== '') {
        vars[field.name] = value
      }
    }
    return vars
  }

  async function submit() {
    if (!sopId) {
      setError('请选择一个 SOP 模板')
      return
    }
    let varsToSubmit: Record<string, any>
    if (useForm) {
      const built = buildVarsFromForm()
      if (built === null) return
      varsToSubmit = built
    } else {
      try {
        varsToSubmit = variables.trim() === '' ? {} : JSON.parse(variables)
        setWarning(null)
      } catch {
        varsToSubmit = { _raw: variables }
        setWarning('变量不是合法 JSON，将按 _raw 文本提交')
      }
    }
    setError(null)
    setSubmitting(true)
    try {
      const title = selectedSop?.name || sopId
      const res = await api.startSopSession(sopId, varsToSubmit, title)
      onCreated(res.task_id, res.session_id)
    } catch (e) {
      setError('启动任务失败：' + (e as Error).message)
      setSubmitting(false)
    }
  }

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/50"
      onClick={onClose}
    >
      <div
        className="w-[420px] rounded-lg border border-ctp-surface bg-ctp-mantle p-4"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="mb-3 text-sm font-semibold text-ctp-text">新建 SOP Run</div>

        <label className="mb-1 block text-xs text-ctp-subtext">SOP 模板</label>
        <select
          value={sopId}
          onChange={(e) => updateSop(e.target.value)}
          className="mb-3 w-full rounded border border-ctp-surface bg-ctp-base p-2 text-sm text-ctp-text"
        >
          <option value="">请选择…</option>
          {sops.map((s) => (
            <option key={s.id} value={s.id}>
              {s.name || s.id}
            </option>
          ))}
        </select>

        {useForm ? (
          <div className="mb-2 max-h-[52vh] space-y-3 overflow-y-auto pr-1">
            {fields.map((field) => {
              const label = field.label || field.name
              const required = field.required !== false
              const type = field.type ?? 'text'
              const jsonProps = jsonObjectProperties(field)
              return (
                <div key={field.name}>
                  <label className="mb-1 block text-xs font-medium text-ctp-subtext">
                    {label}
                    {required && <span className="ml-1 text-ctp-red">*</span>}
                    <span className="ml-2 rounded bg-ctp-surface px-1 py-0.5 font-mono text-[10px] text-ctp-overlay">
                      {type}
                    </span>
                  </label>
                  {field.description && (
                    <div className="mb-1 text-[11px] text-ctp-overlay">{field.description}</div>
                  )}
                  {type === 'document' ? (
                    <textarea
                      value={formValues[field.name] ?? ''}
                      onChange={(e) => setFieldValue(field.name, e.target.value)}
                      rows={4}
                      className="w-full rounded border border-ctp-surface bg-ctp-base p-2 text-xs text-ctp-text"
                      placeholder="可粘贴正文，也可填写本地文档路径或文档链接"
                    />
                  ) : type === 'json' && Object.keys(jsonProps).length > 0 ? (
                    <div className="space-y-2 rounded border border-ctp-surface bg-ctp-base/50 p-2">
                      {Object.entries(jsonProps).map(([key, schema]) => (
                        <div key={key}>
                          <label className="mb-1 block font-mono text-[11px] text-ctp-overlay">{key}</label>
                          <input
                            value={(formValues[field.name]?.[key] ?? '') as string}
                            onChange={(e) =>
                              setJsonFieldValue(field.name, key, coerceJsonScalar(e.target.value, schema))
                            }
                            className="w-full rounded border border-ctp-surface bg-ctp-base p-2 text-xs text-ctp-text"
                            placeholder={schema?.description || schema?.title || key}
                          />
                        </div>
                      ))}
                    </div>
                  ) : type === 'json' ? (
                    <textarea
                      value={jsonDrafts[field.name] ?? JSON.stringify(formValues[field.name] ?? {}, null, 2)}
                      onChange={(e) => setJsonDraftValue(field, e.target.value)}
                      rows={4}
                      className="w-full rounded border border-ctp-surface bg-ctp-base p-2 font-mono text-xs text-ctp-text"
                      placeholder="{}"
                    />
                  ) : (
                    <input
                      value={formValues[field.name] ?? ''}
                      onChange={(e) => setFieldValue(field.name, e.target.value)}
                      className="w-full rounded border border-ctp-surface bg-ctp-base p-2 text-xs text-ctp-text"
                      placeholder={field.description || label}
                    />
                  )}
                </div>
              )
            })}
          </div>
        ) : (
          <>
            <label className="mb-1 block text-xs text-ctp-subtext">变量（JSON）</label>
            <textarea
              value={variables}
              onChange={(e) => setVariables(e.target.value)}
              rows={5}
              className="mb-2 w-full rounded border border-ctp-surface bg-ctp-base p-2 font-mono text-xs text-ctp-text"
              placeholder="{}"
            />
          </>
        )}

        {error && <div className="mb-2 text-xs text-ctp-red">{error}</div>}
        {warning && !error && <div className="mb-2 text-xs text-ctp-yellow">{warning}</div>}

        <div className="flex justify-end gap-2">
          <button
            onClick={onClose}
            className="rounded bg-ctp-surface px-3 py-1 text-sm text-ctp-subtext"
          >
            取消
          </button>
          <button
            onClick={submit}
            disabled={submitting}
            className="rounded bg-ctp-mauve px-3 py-1 text-sm font-medium text-ctp-base disabled:opacity-50"
          >
            {submitting ? '启动中…' : '启动'}
          </button>
        </div>
      </div>
    </div>
  )
}
