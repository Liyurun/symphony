// SopsPage：SOP 模板管理页
// 左栏——SOP 列表（点击加载到右侧编辑器、可删除、可新建）；
// 右栏——SopEditor（AI 生成 / JSON 编辑）。
// 顶层导航由 AppShell 统一提供；SOP 列表统一存到全局 store.sops 供其它页面复用。
import { useEffect, useState } from 'react'
import AppShell from '../components/AppShell'
import { api } from '../lib/api'
import { useStore } from '../lib/store'
import type { SOPTemplate } from '../lib/types'
import SopEditor from '../components/SopEditor'

export default function SopsPage({ embedded = false }: { embedded?: boolean }) {
  const sops = useStore((s) => s.sops)
  const setSops = useStore((s) => s.setSops)

  // 当前选中并加载到编辑器的 SOP；null 表示新建（编辑器进入 AI 生成模式）
  const [selectedSop, setSelectedSop] = useState<SOPTemplate | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  /** 刷新 SOP 列表并写入全局 store */
  async function refresh() {
    setLoading(true)
    setError(null)
    try {
      const list = await api.listSops()
      setSops(list)
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setLoading(false)
    }
  }

  // 首次进入加载列表
  useEffect(() => {
    refresh()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  /** 点击列表项：拉取完整 SOP 加载到编辑器 */
  async function handleSelect(id: string) {
    try {
      const full = await api.getSop(id)
      setSelectedSop(full)
    } catch (e) {
      alert('加载 SOP 失败：' + (e as Error).message)
    }
  }

  /** 删除 SOP（二次确认）后刷新列表 */
  async function handleDelete(id: string, e: React.MouseEvent) {
    // 阻止冒泡，避免触发列表项的选中
    e.stopPropagation()
    if (!window.confirm(`确认删除 SOP「${id}」？`)) return
    try {
      await api.deleteSop(id)
      // 若删除的是当前选中项，清空编辑器
      if (selectedSop?.id === id) setSelectedSop(null)
      await refresh()
    } catch (err) {
      alert('删除失败：' + (err as Error).message)
    }
  }

  /** 编辑器保存/生成成功：刷新列表并把最新模板设为选中 */
  async function handleSaved(t: SOPTemplate) {
    await refresh()
    setSelectedSop(t)
  }

  const content = (
    <div className="flex h-full min-h-0 bg-ctp-base text-ctp-text">
        {/* 左栏：SOP 列表 */}
        <aside className="flex w-[300px] shrink-0 flex-col border-r border-ctp-surface bg-ctp-mantle">
          {/* 列表工具条：新建 */}
          <div className="flex items-center justify-between border-b border-ctp-surface px-3 py-2">
            <span className="text-xs font-semibold uppercase tracking-wide text-ctp-overlay">
              SOP 列表
            </span>
            <button
              onClick={() => setSelectedSop(null)}
              className="rounded bg-ctp-mauve px-2 py-1 text-xs font-medium text-ctp-base hover:brightness-110"
            >
              + 新建 SOP
            </button>
          </div>

          {/* 列表主体 */}
          <div className="min-h-0 flex-1 overflow-y-auto p-2">
            {loading ? (
              <div className="text-xs text-ctp-overlay">加载中…</div>
            ) : error ? (
              <div className="text-xs text-ctp-red">加载失败：{error}</div>
            ) : sops.length === 0 ? (
              <div className="text-xs text-ctp-overlay">
                暂无 SOP，点击「新建 SOP」用 AI 生成一个
              </div>
            ) : (
              <div className="space-y-1">
                {sops.map((s) => {
                  const active = selectedSop?.id === s.id
                  return (
                    <div
                      key={s.id}
                      onClick={() => handleSelect(s.id)}
                      className="group flex cursor-pointer items-center gap-2 rounded px-2 py-1.5 hover:bg-ctp-surface/40"
                      style={{
                        backgroundColor: active
                          ? 'rgba(203,166,247,0.15)'
                          : undefined,
                      }}
                    >
                      <div className="min-w-0 flex-1">
                        <div className="truncate text-sm font-medium text-ctp-text">
                          {s.name || s.id}
                        </div>
                        <div className="truncate font-mono text-[11px] text-ctp-overlay">
                          {s.id} · {s.nodes?.length ?? 0} 节点
                        </div>
                      </div>
                      {/* 删除按钮 */}
                      <button
                        onClick={(e) => handleDelete(s.id, e)}
                        className="rounded px-1.5 py-0.5 text-[11px] text-ctp-red opacity-0 hover:bg-ctp-surface group-hover:opacity-100"
                      >
                        删除
                      </button>
                    </div>
                  )
                })}
              </div>
            )}
          </div>
        </aside>

        {/* 右栏：编辑器 */}
        <main className="min-w-0 flex-1">
          <SopEditor sop={selectedSop} onSaved={handleSaved} />
        </main>
      </div>
  )

  if (embedded) return content

  return (
    <AppShell
      title="SOP Studio"
      eyebrow="Template Builder"
      description="Design reusable SOP templates with structured inputs, outputs, skills, and prompts."
      bodyClassName="flex flex-col"
    >
      {content}
    </AppShell>
  )
}
