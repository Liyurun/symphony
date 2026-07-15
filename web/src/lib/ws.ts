// Symphony WebSocket 客户端
// 连接后端事件流：服务端先回放该任务历史事件，再实时推送新事件。
// 客户端发送的消息会被后端忽略。
import type { SymphonyEvent } from './types'

/** 连接状态 */
export type SocketStatus = 'connecting' | 'open' | 'closed'

/** Chat 流式事件：字段与后端 chat_events.py 对齐，按 type 区分含义。 */
export type ChatEvent = {
  type: string
  text?: string
  answer?: string
  error?: string
  skill_name?: string
  args?: Record<string, any>
  ok?: boolean
  detail?: string
  summary?: string
}

/** 重连的最大次数（指数退避） */
const MAX_RETRIES = 5
/** 首次重连延迟（毫秒），之后翻倍 */
const BASE_DELAY = 1000

/**
 * 建立到指定任务的事件 WebSocket 连接。
 *
 * @param taskId   任务 ID
 * @param onEvent  收到事件时的回调
 * @param onStatus 连接状态变化回调（可选）
 * @returns cleanup 函数：调用后关闭连接并停止重连
 */
export function createEventSocket(
  taskId: string,
  onEvent: (e: SymphonyEvent) => void,
  onStatus?: (s: SocketStatus) => void,
): () => void {
  // 根据当前页面协议选择 ws / wss；host 走 Vite 代理（dev 时为 5273）
  const proto = location.protocol === 'https:' ? 'wss' : 'ws'
  const url = `${proto}://${location.host}/ws?task_id=${encodeURIComponent(taskId)}`

  let ws: WebSocket | null = null
  let retries = 0
  // 是否已被主动关闭（cleanup 调用后不再重连）
  let closedByUser = false
  // 重连定时器句柄
  let retryTimer: ReturnType<typeof setTimeout> | null = null

  /** 建立一次连接，并挂载事件处理器 */
  function connect() {
    onStatus?.('connecting')
    ws = new WebSocket(url)

    // 连接成功：重置重连计数
    ws.onopen = () => {
      retries = 0
      onStatus?.('open')
    }

    // 收到消息：解析 JSON 后回调；解析失败则忽略该条
    ws.onmessage = (ev) => {
      try {
        const data = JSON.parse(ev.data) as SymphonyEvent
        onEvent(data)
      } catch {
        // 非 JSON 消息，忽略
      }
    }

    // 连接关闭：若非用户主动关闭，则按指数退避重连
    ws.onclose = () => {
      onStatus?.('closed')
      if (closedByUser) return
      if (retries < MAX_RETRIES) {
        const delay = BASE_DELAY * 2 ** retries
        retries += 1
        retryTimer = setTimeout(connect, delay)
      }
    }

    // 出错：交给 onclose 统一处理重连（部分浏览器 error 后会触发 close）
    ws.onerror = () => {
      ws?.close()
    }
  }

  connect()

  // 返回 cleanup：停止重连并关闭连接
  return () => {
    closedByUser = true
    if (retryTimer !== null) {
      clearTimeout(retryTimer)
      retryTimer = null
    }
    ws?.close()
  }
}

/**
 * 建立到 Chat session 的流式 WebSocket 连接。
 *
 * 连接成功后发送一帧 `{ question, history }`；服务端随后逐条返回 chat_* 事件。
 * 本 helper 不做重连，ChatPage 会按单次提问生命周期清理连接。
 */
export function createChatSocket(
  sessionId: string,
  question: string,
  history: Array<{ role: 'user' | 'assistant'; content: string }>,
  onEvent: (event: ChatEvent) => void,
  onClose?: () => void,
): () => void {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws'
  const url = `${proto}://${location.host}/ws/chat?session_id=${encodeURIComponent(sessionId)}`
  const ws = new WebSocket(url)

  ws.onopen = () => {
    ws.send(JSON.stringify({ question, history }))
  }

  ws.onmessage = (message) => {
    try {
      onEvent(JSON.parse(message.data) as ChatEvent)
    } catch {
      onEvent({ type: 'chat_failed', error: '收到无法解析的 Chat 事件' })
    }
  }

  ws.onclose = () => {
    onClose?.()
  }

  ws.onerror = () => {
    ws.close()
  }

  return () => ws.close()
}
