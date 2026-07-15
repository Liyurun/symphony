// App：页面分发
// 根据 store.page 渲染 Chat、SOP Runs、SOP Studio 或 Logs。
// 页面统一通过 AppShell 呈现顶层导航；这里保留轻量路由分发。
import { useStore } from './lib/store'
import ChatPage from './pages/ChatPage'
import Dashboard from './pages/Dashboard'
import LogsPage from './pages/LogsPage'
import SopsPage from './pages/SopsPage'

export default function App() {
  const page = useStore((s) => s.page)
  if (page === 'chat') return <ChatPage />
  if (page === 'sop-runs') return <Dashboard />
  if (page === 'sop-studio') return <SopsPage />
  return <LogsPage />
}
