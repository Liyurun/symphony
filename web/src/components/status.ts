// 状态样式辅助
// 集中管理「节点状态 / 任务状态」到「颜色、中文标签」的映射，
// 供任务看板徽章、DAG 节点上色、节点详情面板复用，避免各处硬编码颜色。
import type { NodeStatus } from '../lib/types'

/** 单个状态对应的展示信息 */
export interface StatusStyle {
  /** 中文标签 */
  label: string
  /** 主色（十六进制，Catppuccin Mocha） */
  color: string
  /** 半透明背景色（用于徽章底色，rgba） */
  bg: string
}

/**
 * 节点状态 -> 样式映射。
 * 颜色对应设计稿的 Catppuccin Mocha 语义色。
 */
export const NODE_STATUS_STYLE: Record<NodeStatus, StatusStyle> = {
  pending: { label: '待执行', color: '#6c7086', bg: 'rgba(108,112,134,0.18)' },
  running: { label: '执行中', color: '#89b4fa', bg: 'rgba(137,180,250,0.18)' },
  completed: { label: '已完成', color: '#a6e3a1', bg: 'rgba(166,227,161,0.18)' },
  failed: { label: '失败', color: '#f38ba8', bg: 'rgba(243,139,168,0.18)' },
  waiting_input: {
    label: '等待介入',
    color: '#f9e2af',
    bg: 'rgba(249,226,175,0.18)',
  },
  skipped: { label: '已跳过', color: '#fab387', bg: 'rgba(250,179,135,0.18)' },
}

/** 弱化的默认样式（未知状态兜底） */
const FALLBACK_STYLE: StatusStyle = {
  label: '未知',
  color: '#6c7086',
  bg: 'rgba(108,112,134,0.18)',
}

/**
 * 取节点状态样式（带兜底）。
 * @param status 可能为任意字符串（任务列表 status 字段是宽松的 string）
 */
export function statusStyle(status: string | undefined | null): StatusStyle {
  if (status && status in NODE_STATUS_STYLE) {
    return NODE_STATUS_STYLE[status as NodeStatus]
  }
  return FALLBACK_STYLE
}

/**
 * 取运行时 DAG 节点样式。
 * stale 节点表示输出已因上游重跑过期，需要优先用过期态提醒用户。
 */
export function runtimeNodeStyle(status: string | undefined | null, stale?: boolean): StatusStyle {
  if (stale) {
    return { label: '已过期', color: '#f9e2af', bg: 'rgba(249,226,175,0.12)' }
  }
  return statusStyle(status)
}
