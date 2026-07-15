// SopEditor：串行可视化 SOP 编辑器
// 设计目标：用户按节点顺序依次编辑，每个节点显式声明名称、描述、输入（text/document/json）、
// 输出（text/document/json）、提示词、可用技能。后端根据 nodes 顺序自动串接 edges，
// 不需要用户关心 DAG 连线。
//
// 布局（三栏）：
//  ┌─────────────────────────────────────────────────────┐
//  │ 顶部：SOP 基本信息 + 工作流输入变量 + [AI 填充] [保存] │
//  ├──────────────┬──────────────────────────────────────┤
//  │ 左栏：节点    │ 右栏：选中节点详情                    │
//  │ 顺序列表      │  （名称/描述/类型/prompt/输入/输出/技能）│
//  │ （上下/增删） │                                      │
//  └──────────────┴──────────────────────────────────────┘
//
// 为兼顾高级用户，保留一个「JSON 预览」折叠区，可查看/导出最终 JSON。
import { useEffect, useMemo, useState } from 'react'
import { api } from '../lib/api'
import type { IOField, IOFieldType, SOPTemplate, SopNode } from '../lib/types'

/* -------------------- 小工具：slug / 默认值 / 深拷贝 -------------------- */

function slugify(input: string): string {
  const s = input
    .toLowerCase()
    .replace(/[^a-z0-9\u4e00-\u9fa5]+/g, '-')
    .replace(/^-+|-+$/g, '')
  if (/^[a-z0-9]/.test(s)) {
    // 只保留 ascii，避免 jinja2 变量名里出现中文
    return s.replace(/[^a-z0-9-]/g, '').replace(/-+/g, '-').slice(0, 48) || `node-${Date.now().toString(36)}`
  }
  return `node-${Date.now().toString(36)}`
}

function emptyIOField(name = ''): IOField {
  return { name, label: '', type: 'text', description: '', required: true }
}

function emptyNode(idx: number): SopNode {
  const id = `node-${idx + 1}`
  return {
    id,
    name: `节点 ${idx + 1}`,
    description: '',
    type: 'agent',
    prompt: '',
    skills: [],
    inputs: [],
    outputs: [],
  }
}

function emptySop(): SOPTemplate {
  const n1 = emptyNode(0)
  // 默认第一个节点有一个输入和一个输出，方便用户理解
  n1.inputs = []
  n1.outputs = [emptyIOField('result')]
  return {
    id: `sop-${Date.now().toString(36)}`,
    name: '新建 SOP',
    version: '1.0.0',
    description: '',
    variables_def: [],
    nodes: [n1],
    edges: [],
    entry_node: null,
  }
}

function clone<T>(v: T): T {
  return JSON.parse(JSON.stringify(v))
}

/* -------------------- IOField 编辑器（可复用） -------------------- */

interface IOFieldEditorProps {
  title: string
  fields: IOField[]
  onChange: (fields: IOField[]) => void
  /** 可选：名字建议（下拉选择） */
  nameSuggestions?: string[]
}

function IOFieldList({ title, fields, onChange, nameSuggestions }: IOFieldEditorProps) {
  function update(i: number, patch: Partial<IOField>) {
    const next = fields.slice()
    next[i] = { ...next[i], ...patch }
    onChange(next)
  }
  function remove(i: number) {
    const next = fields.slice()
    next.splice(i, 1)
    onChange(next)
  }
  function add() {
    onChange([...fields, emptyIOField()])
  }
  function move(i: number, delta: number) {
    const j = i + delta
    if (j < 0 || j >= fields.length) return
    const next = fields.slice()
    ;[next[i], next[j]] = [next[j], next[i]]
    onChange(next)
  }

  const typeColor: Record<IOFieldType, string> = {
    text: '#89b4fa',
    document: '#a6e3a1',
    json: '#fab387',
  }

  return (
    <div className="rounded border border-ctp-surface bg-ctp-mantle/60 p-2">
      <div className="mb-2 flex items-center justify-between">
        <span className="text-xs font-semibold text-ctp-subtext">{title}</span>
        <button
          onClick={add}
          className="rounded bg-ctp-surface px-2 py-0.5 text-[11px] text-ctp-text hover:bg-ctp-overlay"
        >
          + 添加字段
        </button>
      </div>
      {fields.length === 0 && (
        <div className="text-[11px] text-ctp-overlay">（暂无字段）</div>
      )}
      <div className="space-y-2">
        {fields.map((f, i) => (
          <div
            key={i}
            className="rounded border border-ctp-surface bg-ctp-base p-2"
          >
            <div className="flex items-center gap-2">
              <input
                value={f.name ?? ''}
                list={nameSuggestions ? `sugg-${title}-${i}` : undefined}
                onChange={(e) => update(i, { name: e.target.value })}
                placeholder="字段名（英文）"
                className="w-[140px] rounded border border-ctp-surface bg-ctp-base px-1.5 py-1 font-mono text-xs text-ctp-text"
              />
              <input
                value={f.label ?? ''}
                onChange={(e) => update(i, { label: e.target.value })}
                placeholder="中文名（可选）"
                className="flex-1 rounded border border-ctp-surface bg-ctp-base px-1.5 py-1 text-xs text-ctp-text"
              />
              <select
                value={f.type ?? 'text'}
                onChange={(e) => update(i, { type: e.target.value as IOFieldType })}
                className="rounded border border-ctp-surface bg-ctp-base px-1 py-1 text-xs"
                style={{ color: typeColor[(f.type ?? 'text') as IOFieldType] }}
              >
                <option value="text">text（短文本）</option>
                <option value="document">document（长文档）</option>
                <option value="json">json（结构化）</option>
              </select>
              <label className="flex items-center gap-1 text-[11px] text-ctp-subtext">
                <input
                  type="checkbox"
                  checked={f.required !== false}
                  onChange={(e) => update(i, { required: e.target.checked })}
                />
                必填
              </label>
              <div className="ml-auto flex gap-0.5">
                <button
                  onClick={() => move(i, -1)}
                  className="rounded px-1 text-xs text-ctp-subtext hover:bg-ctp-surface"
                  title="上移"
                >
                  ↑
                </button>
                <button
                  onClick={() => move(i, 1)}
                  className="rounded px-1 text-xs text-ctp-subtext hover:bg-ctp-surface"
                  title="下移"
                >
                  ↓
                </button>
                <button
                  onClick={() => remove(i)}
                  className="rounded px-1 text-xs text-ctp-red hover:bg-ctp-surface"
                  title="删除"
                >
                  ✕
                </button>
              </div>
            </div>
            <input
              value={f.description ?? ''}
              onChange={(e) => update(i, { description: e.target.value })}
              placeholder="字段说明（给 LLM 看的）"
              className="mt-1 w-full rounded border border-ctp-surface bg-ctp-base px-1.5 py-1 text-xs text-ctp-text"
            />
            {f.type === 'json' && (
              <textarea
                value={f.json_schema ? JSON.stringify(f.json_schema, null, 2) : ''}
                onChange={(e) => {
                  let sch: any = undefined
                  try {
                    sch = e.target.value ? JSON.parse(e.target.value) : undefined
                  } catch {
                    sch = { $parseError: e.target.value }
                  }
                  update(i, { json_schema: sch })
                }}
                rows={3}
                placeholder='JSON Schema（可选），例如 {"type":"array","items":{"type":"string"}}'
                className="mt-1 w-full rounded border border-ctp-surface bg-ctp-base px-1.5 py-1 font-mono text-[11px] text-ctp-peach"
              />
            )}
            {nameSuggestions && (
              <datalist id={`sugg-${title}-${i}`}>
                {nameSuggestions.map((s) => (
                  <option key={s} value={s} />
                ))}
              </datalist>
            )}
          </div>
        ))}
      </div>
    </div>
  )
}

/* -------------------- 主组件 -------------------- */

interface SopEditorProps {
  sop: SOPTemplate | null
  onSaved: (t: SOPTemplate) => void
}

type RightTab = 'general' | 'inputs' | 'outputs' | 'prompt' | 'skills' | 'json'

export default function SopEditor({ sop, onSaved }: SopEditorProps) {
  // 正在编辑的本地草稿
  const [draft, setDraft] = useState<SOPTemplate>(() => sop ?? emptySop())
  // 当前选中节点 id
  const [selectedId, setSelectedId] = useState<string>(() => {
    const s = sop ?? emptySop()
    return s.nodes[0]?.id ?? ''
  })
  const [rightTab, setRightTab] = useState<RightTab>('general')
  const [saving, setSaving] = useState(false)
  const [saveError, setSaveError] = useState<string | null>(null)
  const [savedAt, setSavedAt] = useState<string | null>(null)

  // AI 生成侧栏
  const [aiOpen, setAiOpen] = useState(false)
  const [aiDesc, setAiDesc] = useState('')
  const [aiLoading, setAiLoading] = useState(false)
  const [aiError, setAiError] = useState<string | null>(null)

  // 选中/切换 props.sop 时同步
  useEffect(() => {
    const target = sop ?? emptySop()
    setDraft(target)
    setSelectedId(target.nodes[0]?.id ?? '')
    setRightTab('general')
    setSaveError(null)
    setSavedAt(null)
  }, [sop])

  const selectedNode = useMemo(
    () => draft.nodes.find((n) => n.id === selectedId) ?? draft.nodes[0],
    [draft, selectedId],
  )

  const selectedIndex = useMemo(
    () => draft.nodes.findIndex((n) => n.id === selectedNode?.id),
    [draft, selectedNode],
  )

  /* -------- 草稿修改辅助 -------- */
  function updateDraft(patch: Partial<SOPTemplate>) {
    setDraft((d) => ({ ...d, ...patch }))
  }
  function updateNode(nodeId: string, patch: Partial<SopNode>) {
    setDraft((d) => ({
      ...d,
      nodes: d.nodes.map((n) => (n.id === nodeId ? { ...n, ...patch } : n)),
    }))
  }

  /* -------- 节点增删改序 -------- */
  function addNode(afterIdx = draft.nodes.length - 1) {
    const newIdx = afterIdx + 1
    const n = emptyNode(newIdx)
    // 尝试让新节点默认接收上一个节点的输出字段名作为建议输入
    const prev = draft.nodes[afterIdx]
    if (prev && prev.outputs && prev.outputs.length > 0) {
      n.inputs = prev.outputs.map((o) => ({ ...o }))
    }
    const next = draft.nodes.slice()
    next.splice(newIdx, 0, n)
    // 重命名 id 冲突时保证唯一
    const seen = new Set<string>()
    for (const nn of next) {
      if (seen.has(nn.id)) {
        nn.id = `${nn.id}-${Date.now().toString(36).slice(-4)}`
      }
      seen.add(nn.id)
    }
    setDraft({ ...draft, nodes: next })
    setSelectedId(n.id)
    setRightTab('general')
  }
  function removeNode(i: number) {
    if (draft.nodes.length <= 1) {
      alert('SOP 至少要有一个节点')
      return
    }
    const next = draft.nodes.slice()
    next.splice(i, 1)
    setDraft({ ...draft, nodes: next })
    if (selectedId === draft.nodes[i].id) {
      setSelectedId(next[Math.min(i, next.length - 1)].id)
    }
  }
  function moveNode(i: number, delta: number) {
    const j = i + delta
    if (j < 0 || j >= draft.nodes.length) return
    const next = draft.nodes.slice()
    ;[next[i], next[j]] = [next[j], next[i]]
    setDraft({ ...draft, nodes: next })
  }

  /* -------- 校验与保存 -------- */
  function validate(): string | null {
    if (!draft.id.trim()) return 'SOP ID 不能为空'
    if (!draft.name.trim()) return 'SOP 名称不能为空'
    if (draft.nodes.length === 0) return '至少要有一个节点'
    const ids = new Set<string>()
    for (const n of draft.nodes) {
      if (!n.id.trim()) return `存在节点没有 id`
      if (ids.has(n.id)) return `节点 id 重复：${n.id}`
      ids.add(n.id)
      if (!n.name.trim()) return `节点 ${n.id} 没有名称`
      for (const f of n.inputs ?? []) {
        if (!f.name?.trim()) return `节点 ${n.id} 存在未命名的输入字段`
      }
      for (const f of n.outputs ?? []) {
        if (!f.name?.trim()) return `节点 ${n.id} 存在未命名的输出字段`
      }
    }
    return null
  }

  async function handleSave() {
    const err = validate()
    if (err) {
      setSaveError(err)
      return
    }
    setSaveError(null)
    setSaving(true)
    try {
      // 构造最终 payload：清空 edges/entry_node，让后端自动线性化
      const payload: SOPTemplate = {
        ...clone(draft),
        edges: [],
        entry_node: null,
      }
      const saved = sop
        ? await api.updateSop(payload.id, payload)
        : await api.createSop(payload)
      setSavedAt(new Date().toLocaleTimeString())
      onSaved(saved)
    } catch (e) {
      setSaveError('保存失败：' + (e as Error).message)
    } finally {
      setSaving(false)
    }
  }

  async function handleAiGenerate() {
    if (!aiDesc.trim()) {
      setAiError('请输入需求描述')
      return
    }
    setAiError(null)
    setAiLoading(true)
    try {
      const t = await api.generateSopDraft(aiDesc.trim())
      setDraft(t)
      setSelectedId(t.nodes[0]?.id ?? '')
      setAiOpen(false)
      setAiDesc('')
      setSavedAt(null)
    } catch (e) {
      setAiError((e as Error).message)
    } finally {
      setAiLoading(false)
    }
  }

  /* -------- 右侧面板：节点名建议 -------- */
  // 给下游节点的 inputs 字段名提供建议（工作流输入 + 上游节点 outputs）
  const inputNameSuggestions = useMemo(() => {
    const names = new Set<string>()
    for (const v of draft.variables_def ?? []) {
      if (v.name) names.add(v.name)
    }
    for (let i = 0; i < selectedIndex; i++) {
      for (const o of draft.nodes[i]?.outputs ?? []) {
        if (o.name) names.add(o.name)
      }
    }
    return Array.from(names)
  }, [draft, selectedIndex])

  /* -------- 渲染 -------- */
  const typeTag: Record<string, { label: string; color: string }> = {
    agent: { label: 'Agent', color: '#cba6f7' },
    human: { label: '人工', color: '#f9e2af' },
    skill: { label: 'Skill', color: '#94e2d5' },
  }

  return (
    <div className="flex h-full flex-col bg-ctp-base text-ctp-text">
      {/* ---------------- 顶部：SOP 元信息 + 工具条 ---------------- */}
      <div className="border-b border-ctp-surface bg-ctp-mantle p-3">
        <div className="mb-3 rounded border border-ctp-surface bg-ctp-base/60 p-3 text-xs text-ctp-subtext">
          <div className="font-semibold text-ctp-text">两种编辑模式</div>
          <div>手动模式：按步骤编辑节点、输入输出、技能和顺序。</div>
          <div>AI 生成模式：描述理想 SOP，生成草案后确认或继续修改再保存。</div>
        </div>
        <div className="grid grid-cols-12 gap-2">
          <div className="col-span-2">
            <label className="mb-0.5 block text-[11px] text-ctp-subtext">SOP ID</label>
            <input
              value={draft.id}
              onChange={(e) => updateDraft({ id: e.target.value })}
              className="w-full rounded border border-ctp-surface bg-ctp-base px-2 py-1 font-mono text-xs text-ctp-text"
              placeholder="my-sop"
            />
          </div>
          <div className="col-span-4">
            <label className="mb-0.5 block text-[11px] text-ctp-subtext">SOP 名称</label>
            <input
              value={draft.name}
              onChange={(e) => updateDraft({ name: e.target.value })}
              className="w-full rounded border border-ctp-surface bg-ctp-base px-2 py-1 text-sm text-ctp-text"
              placeholder="例如：营销文案三步法"
            />
          </div>
          <div className="col-span-1">
            <label className="mb-0.5 block text-[11px] text-ctp-subtext">版本</label>
            <input
              value={draft.version ?? '1.0.0'}
              onChange={(e) => updateDraft({ version: e.target.value })}
              className="w-full rounded border border-ctp-surface bg-ctp-base px-2 py-1 font-mono text-xs text-ctp-text"
            />
          </div>
          <div className="col-span-5 flex items-end justify-end gap-2">
            <button
              onClick={() => setAiOpen((v) => !v)}
              className="rounded bg-ctp-surface px-3 py-1 text-xs text-ctp-subtext hover:bg-ctp-overlay"
            >
              ✨ AI 填充
            </button>
            <button
              onClick={() => {
                setDraft(emptySop())
                setSelectedId('node-1')
                setSavedAt(null)
              }}
              className="rounded bg-ctp-surface px-3 py-1 text-xs text-ctp-subtext hover:bg-ctp-overlay"
            >
              清空新建
            </button>
            <button
              onClick={handleSave}
              disabled={saving}
              className="rounded bg-ctp-mauve px-3 py-1 text-sm font-medium text-ctp-base disabled:opacity-50"
            >
              {saving ? '保存中…' : '保存'}
            </button>
          </div>
          <div className="col-span-12">
            <label className="mb-0.5 block text-[11px] text-ctp-subtext">描述</label>
            <input
              value={draft.description ?? ''}
              onChange={(e) => updateDraft({ description: e.target.value })}
              className="w-full rounded border border-ctp-surface bg-ctp-base px-2 py-1 text-xs text-ctp-text"
              placeholder="简述 SOP 做什么、输入输出是什么…"
            />
          </div>
          {saveError && (
            <div className="col-span-12 text-xs text-ctp-red">{saveError}</div>
          )}
          {savedAt && (
            <div className="col-span-12 text-xs text-ctp-green">已保存 · {savedAt}</div>
          )}
        </div>

        {/* AI 填充 展开区 */}
        {aiOpen && (
          <div className="mt-2 rounded border border-ctp-surface bg-ctp-base p-2">
            <div className="mb-1 text-xs text-ctp-subtext">
              用自然语言描述 SOP，AI 会生成未保存草案。确认内容后点击保存才会持久化。
            </div>
            <textarea
              value={aiDesc}
              onChange={(e) => setAiDesc(e.target.value)}
              rows={3}
              className="w-full rounded border border-ctp-surface bg-ctp-base p-2 text-sm text-ctp-text"
              placeholder="例如：营销文案流水线，先提取产品卖点，再生成三条候选文案，最后做合规审查并输出 JSON 结果…"
            />
            <div className="mt-1 flex items-center gap-2">
              <button
                onClick={handleAiGenerate}
                disabled={aiLoading}
                className="rounded bg-ctp-mauve px-3 py-1 text-xs font-medium text-ctp-base disabled:opacity-50"
              >
                {aiLoading ? '生成中…' : '生成草案'}
              </button>
              <button
                onClick={() => setAiOpen(false)}
                className="rounded bg-ctp-surface px-2 py-1 text-xs text-ctp-subtext"
              >
                取消
              </button>
              {aiError && <span className="text-xs text-ctp-red">{aiError}</span>}
            </div>
          </div>
        )}

        {/* 工作流级输入变量 */}
        <div className="mt-2">
          <IOFieldList
            title="工作流输入变量（启动任务时用户需要填写的字段）"
            fields={draft.variables_def ?? []}
            onChange={(f) => updateDraft({ variables_def: f })}
          />
        </div>
      </div>

      {/* ---------------- 主体两栏 ---------------- */}
      <div className="flex min-h-0 flex-1">
        {/* 左栏：节点顺序列表 */}
        <aside className="flex w-[280px] shrink-0 flex-col border-r border-ctp-surface bg-ctp-mantle">
          <div className="flex items-center justify-between border-b border-ctp-surface px-3 py-2">
            <span className="text-xs font-semibold uppercase tracking-wide text-ctp-overlay">
              节点顺序（串行执行）
            </span>
            <button
              onClick={() => addNode()}
              className="rounded bg-ctp-mauve px-2 py-0.5 text-[11px] font-medium text-ctp-base hover:brightness-110"
            >
              + 节点
            </button>
          </div>
          <div className="min-h-0 flex-1 overflow-y-auto p-2">
            {draft.nodes.map((n, i) => {
              const active = n.id === selectedId
              const tag = typeTag[n.type]
              return (
                <div
                  key={n.id}
                  onClick={() => {
                    setSelectedId(n.id)
                    setRightTab('general')
                  }}
                  className="group mb-1.5 cursor-pointer rounded border px-2 py-2"
                  style={{
                    borderColor: active ? 'rgba(203,166,247,0.6)' : 'transparent',
                    backgroundColor: active ? 'rgba(203,166,247,0.12)' : undefined,
                  }}
                >
                  <div className="flex items-center gap-2">
                    <span className="inline-flex h-5 w-5 items-center justify-center rounded-full bg-ctp-surface text-[10px] font-bold text-ctp-subtext">
                      {i + 1}
                    </span>
                    <span className="truncate text-sm font-medium text-ctp-text">
                      {n.name || '(未命名)'}
                    </span>
                    <span
                      className="ml-auto rounded px-1.5 py-0.5 text-[10px]"
                      style={{ backgroundColor: 'rgba(0,0,0,0.2)', color: tag?.color }}
                    >
                      {tag?.label}
                    </span>
                  </div>
                  <div className="mt-1 flex items-center gap-1 text-[10px] text-ctp-overlay">
                    <span title="输入字段数">⬇ {n.inputs?.length ?? 0}</span>
                    <span>·</span>
                    <span title="输出字段数">⬆ {n.outputs?.length ?? 0}</span>
                    <span className="ml-auto flex gap-0.5 opacity-0 group-hover:opacity-100">
                      <button
                        onClick={(e) => {
                          e.stopPropagation()
                          moveNode(i, -1)
                        }}
                        className="rounded px-1 hover:bg-ctp-surface"
                      >
                        ↑
                      </button>
                      <button
                        onClick={(e) => {
                          e.stopPropagation()
                          moveNode(i, 1)
                        }}
                        className="rounded px-1 hover:bg-ctp-surface"
                      >
                        ↓
                      </button>
                      <button
                        onClick={(e) => {
                          e.stopPropagation()
                          addNode(i)
                        }}
                        className="rounded px-1 text-ctp-green hover:bg-ctp-surface"
                        title="在该节点后插入"
                      >
                        +
                      </button>
                      <button
                        onClick={(e) => {
                          e.stopPropagation()
                          removeNode(i)
                        }}
                        className="rounded px-1 text-ctp-red hover:bg-ctp-surface"
                      >
                        ✕
                      </button>
                    </span>
                  </div>
                  <div className="mt-0.5 truncate font-mono text-[10px] text-ctp-overlay">
                    {n.id}
                  </div>
                </div>
              )
            })}
          </div>
        </aside>

        {/* 右栏：节点详情 */}
        <main className="min-w-0 flex-1 overflow-y-auto p-3">
          {selectedNode ? (
            <>
              {/* 节点 Tab */}
              <div className="mb-3 flex flex-wrap gap-1 border-b border-ctp-surface pb-2 text-xs">
                {(
                  [
                    ['general', '基本信息'],
                    ['inputs', '输入'],
                    ['outputs', '输出'],
                    ['prompt', '提示词'],
                    ['skills', '技能/类型'],
                    ['json', 'JSON 预览'],
                  ] as [RightTab, string][]
                ).map(([k, label]) => (
                  <button
                    key={k}
                    onClick={() => setRightTab(k)}
                    className="rounded px-2 py-1"
                    style={{
                      color: rightTab === k ? '#cba6f7' : '#a6adc8',
                      backgroundColor:
                        rightTab === k ? 'rgba(203,166,247,0.15)' : 'transparent',
                    }}
                  >
                    {label}
                  </button>
                ))}
              </div>

              {rightTab === 'general' && (
                <div className="space-y-3">
                  <div>
                    <label className="mb-1 block text-xs text-ctp-subtext">节点 ID</label>
                    <input
                      value={selectedNode.id}
                      onChange={(e) => updateNode(selectedNode.id, { id: slugify(e.target.value) })}
                      className="w-full rounded border border-ctp-surface bg-ctp-base p-2 font-mono text-xs text-ctp-text"
                    />
                  </div>
                  <div>
                    <label className="mb-1 block text-xs text-ctp-subtext">节点名称</label>
                    <input
                      value={selectedNode.name}
                      onChange={(e) => {
                        const v = e.target.value
                        updateNode(selectedNode.id, { name: v })
                        // 若是新节点（默认 id node-X），同步把 id 同步成 name 的 slug
                        if (/^node-\d+(-[a-z0-9]{4})?$/.test(selectedNode.id)) {
                          const slug = slugify(v)
                          if (slug) updateNode(selectedNode.id, { id: slug })
                        }
                      }}
                      className="w-full rounded border border-ctp-surface bg-ctp-base p-2 text-sm text-ctp-text"
                    />
                  </div>
                  <div>
                    <label className="mb-1 block text-xs text-ctp-subtext">节点描述</label>
                    <textarea
                      value={selectedNode.description ?? ''}
                      onChange={(e) => updateNode(selectedNode.id, { description: e.target.value })}
                      rows={2}
                      className="w-full rounded border border-ctp-surface bg-ctp-base p-2 text-sm text-ctp-text"
                      placeholder="简要描述这个节点要做什么"
                    />
                  </div>
                  <div className="rounded border border-ctp-surface bg-ctp-mantle/60 p-2 text-[11px] text-ctp-overlay">
                    💡 节点按左侧列表顺序串行执行。下游节点可以通过{' '}
                    <code className="text-ctp-peach">{'{{field_name}}'}</code> 或{' '}
                    <code className="text-ctp-peach">{'{{upstream_node_id.field_name}}'}</code>{' '}
                    引用上游输出。
                  </div>
                </div>
              )}

              {rightTab === 'inputs' && (
                <IOFieldList
                  title="输入字段（运行前系统会校验必填与类型）"
                  fields={selectedNode.inputs ?? []}
                  onChange={(f) => updateNode(selectedNode.id, { inputs: f })}
                  nameSuggestions={inputNameSuggestions}
                />
              )}

              {rightTab === 'outputs' && (
                <IOFieldList
                  title="输出字段（运行后系统会校验类型与结构）"
                  fields={selectedNode.outputs ?? []}
                  onChange={(f) => updateNode(selectedNode.id, { outputs: f })}
                />
              )}

              {rightTab === 'prompt' && (
                <div className="space-y-2">
                  <div className="text-xs text-ctp-subtext">
                    提示词（可使用 jinja2 模板语法引用输入/上游输出）
                  </div>
                  <textarea
                    value={selectedNode.prompt ?? ''}
                    onChange={(e) => updateNode(selectedNode.id, { prompt: e.target.value })}
                    rows={18}
                    className="w-full rounded border border-ctp-surface bg-ctp-base p-2 font-mono text-xs leading-relaxed text-ctp-text"
                    placeholder={
                      '示例：\n你是一个营销文案助手。\n产品：{{product_name}}\n卖点：{{key_selling_points}}\n请输出一段不超过 50 字的 slogan。'
                    }
                  />
                  <div className="rounded border border-ctp-surface bg-ctp-mantle/60 p-2 text-[11px] text-ctp-overlay">
                    可用变量：
                    <div className="mt-1 flex flex-wrap gap-1">
                      {Array.from(
                        new Set([
                          ...(draft.variables_def ?? []).map((v) => v.name).filter(Boolean),
                          ...((): string[] => {
                            const out: string[] = []
                            for (let i = 0; i < selectedIndex; i++) {
                              const nd = draft.nodes[i]
                              for (const o of nd.outputs ?? []) {
                                if (o.name) out.push(o.name)
                              }
                            }
                            return out
                          })(),
                        ]),
                      ).map((n) => (
                        <code
                          key={n}
                          className="rounded bg-ctp-surface px-1 text-ctp-peach"
                        >
                          {`{{${n}}}`}
                        </code>
                      ))}
                    </div>
                  </div>
                </div>
              )}

              {rightTab === 'skills' && (
                <div className="space-y-3">
                  <div>
                    <label className="mb-1 block text-xs text-ctp-subtext">节点类型</label>
                    <select
                      value={selectedNode.type}
                      onChange={(e) =>
                        updateNode(selectedNode.id, { type: e.target.value as SopNode['type'] })
                      }
                      className="rounded border border-ctp-surface bg-ctp-base p-2 text-sm"
                    >
                      <option value="agent">Agent（由 LLM 自主规划+工具调用）</option>
                      <option value="human">人工审核（暂停等待人处理）</option>
                      <option value="skill">Skill（直接调用某个技能）</option>
                    </select>
                  </div>
                  {selectedNode.type === 'skill' && (
                    <div>
                      <label className="mb-1 block text-xs text-ctp-subtext">技能名</label>
                      <input
                        value={selectedNode.skill_name ?? ''}
                        onChange={(e) => updateNode(selectedNode.id, { skill_name: e.target.value })}
                        className="w-full rounded border border-ctp-surface bg-ctp-base p-2 font-mono text-xs"
                          placeholder="例如 workspace_search / bash_execute / file_patch"
                      />
                    </div>
                  )}
                  <div>
                    <label className="mb-1 block text-xs text-ctp-subtext">
                      可用技能（Agent 节点可调用的工具列表，逗号分隔）
                    </label>
                    <input
                      value={(selectedNode.skills ?? []).join(',')}
                      onChange={(e) =>
                        updateNode(selectedNode.id, {
                          skills: e.target.value
                            .split(',')
                            .map((s) => s.trim())
                            .filter(Boolean),
                        })
                      }
                      className="w-full rounded border border-ctp-surface bg-ctp-base p-2 font-mono text-xs"
                        placeholder="workspace_search,bash_execute,file_patch"
                    />
                  </div>
                </div>
              )}

              {rightTab === 'json' && (
                <div>
                  <div className="mb-1 text-xs text-ctp-subtext">
                    最终将发送到后端的 JSON（edges/entry_node 由后端自动生成）
                  </div>
                  <pre className="max-h-[60vh] overflow-auto rounded border border-ctp-surface bg-ctp-base p-2 font-mono text-[11px] text-ctp-text">
                    {JSON.stringify(
                      { ...draft, edges: [], entry_node: null },
                      null,
                      2,
                    )}
                  </pre>
                </div>
              )}
            </>
          ) : (
            <div className="text-sm text-ctp-overlay">请选择或新建一个节点</div>
          )}
        </main>
      </div>
    </div>
  )
}
