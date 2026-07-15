import type { ReactNode } from 'react'
import { useStore, type Page } from '../lib/store'

type SecondaryTab = {
  id: string
  label: string
  active: boolean
  onClick: () => void
  hint?: string
}

interface AppShellProps {
  title: string
  eyebrow?: string
  description?: string
  actions?: ReactNode
  meta?: ReactNode
  secondaryTabs?: SecondaryTab[]
  bodyClassName?: string
  children: ReactNode
}

const primaryTabs: Array<{ page: Page; label: string; hint: string }> = [
  { page: 'chat', label: 'Chat', hint: 'Agent console' },
  { page: 'sop-runs', label: 'SOP Runs', hint: 'Runtime monitor' },
  { page: 'sop-studio', label: 'SOP Studio', hint: 'Template builder' },
  { page: 'logs', label: 'Logs', hint: 'Session records' },
]

function PrimaryTabButton({
  page,
  label,
  hint,
  active,
  onClick,
}: {
  page: Page
  label: string
  hint: string
  active: boolean
  onClick: (page: Page) => void
}) {
  return (
    <button
      type="button"
      onClick={() => onClick(page)}
      className={`group rounded-md border px-3 py-1.5 text-left transition ${
        active
          ? 'border-emerald-300/50 bg-emerald-300/10 text-emerald-100 shadow-[0_0_18px_rgba(110,231,183,0.12)]'
          : 'border-transparent text-ctp-subtext hover:border-ctp-surface hover:bg-ctp-surface/40 hover:text-ctp-text'
      }`}
    >
      <div className="text-xs font-semibold leading-none tracking-wide">{label}</div>
      <div className="mt-1 hidden text-[10px] leading-none text-ctp-overlay xl:block">
        {hint}
      </div>
    </button>
  )
}

export default function AppShell({
  title,
  eyebrow = 'Workspace',
  description,
  actions,
  meta,
  secondaryTabs,
  bodyClassName = '',
  children,
}: AppShellProps) {
  const page = useStore((s) => s.page)
  const setPage = useStore((s) => s.setPage)

  return (
    <div className="flex h-screen flex-col bg-ctp-base text-ctp-text">
      <header className="shrink-0 border-b border-ctp-surface/80 bg-[#10111d]/95 shadow-[0_16px_60px_rgba(0,0,0,0.24)]">
        <div className="flex min-h-[56px] items-center justify-between gap-4 px-4">
          <div className="flex min-w-0 items-center gap-4">
            <button
              type="button"
              onClick={() => setPage('chat')}
              className="group flex items-center gap-2 rounded-lg border border-ctp-surface/70 bg-ctp-base/60 px-3 py-2 text-left hover:border-emerald-300/40"
            >
              <span className="flex h-5 w-5 items-center justify-center rounded border border-emerald-300/50 bg-emerald-300/10 font-mono text-[10px] text-emerald-200">
                S
              </span>
              <span>
                <span className="block text-sm font-semibold tracking-wide text-ctp-text">
                  Symphony
                </span>
                <span className="block font-mono text-[10px] uppercase tracking-[0.18em] text-ctp-overlay">
                  agent os
                </span>
              </span>
            </button>
            <nav className="flex items-stretch gap-1 rounded-lg border border-ctp-surface/70 bg-ctp-base/40 p-1">
              {primaryTabs.map((tab) => (
                <PrimaryTabButton
                  key={tab.page}
                  page={tab.page}
                  label={tab.label}
                  hint={tab.hint}
                  active={page === tab.page}
                  onClick={setPage}
                />
              ))}
            </nav>
          </div>
          {actions && <div className="flex shrink-0 items-center gap-2">{actions}</div>}
        </div>

        <div className="flex min-h-[64px] items-center justify-between gap-4 border-t border-ctp-surface/40 bg-gradient-to-r from-ctp-mantle/90 via-ctp-mantle/70 to-ctp-base px-4">
          <div className="min-w-0">
            <div className="font-mono text-[10px] uppercase tracking-[0.2em] text-emerald-200/70">
              {eyebrow}
            </div>
            <div className="mt-1 flex min-w-0 items-baseline gap-3">
              <h1 className="truncate text-lg font-semibold tracking-tight text-ctp-text">
                {title}
              </h1>
              {description && (
                <p className="hidden truncate text-xs text-ctp-subtext md:block">
                  {description}
                </p>
              )}
            </div>
          </div>
          {meta && <div className="flex shrink-0 items-center gap-3 text-xs">{meta}</div>}
        </div>

        {secondaryTabs && secondaryTabs.length > 0 && (
          <div className="flex min-h-[42px] items-center justify-between border-t border-ctp-surface/50 bg-ctp-mantle/55 px-4">
            <nav className="flex min-w-0 gap-1 overflow-x-auto">
              {secondaryTabs.map((tab) => (
                <button
                  key={tab.id}
                  type="button"
                  onClick={tab.onClick}
                  title={tab.hint}
                  className={`rounded-md border px-3 py-1.5 text-xs transition ${
                    tab.active
                      ? 'border-emerald-300/40 bg-emerald-300/10 text-emerald-100'
                      : 'border-transparent text-ctp-subtext hover:border-ctp-surface hover:bg-ctp-surface/40 hover:text-ctp-text'
                  }`}
                >
                  {tab.label}
                </button>
              ))}
            </nav>
          </div>
        )}
      </header>

      <div className={`min-h-0 flex-1 ${bodyClassName}`}>{children}</div>
    </div>
  )
}
