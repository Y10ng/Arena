import type { LocationQuery } from 'vue-router'
import type { BadgeTone } from '@/components/ui'
import type { VerificationOutcome, VerificationStatus } from '@/api/verificationRequests'

export function verificationListQuery(query: LocationQuery): Record<string, string> {
  return Object.fromEntries(['external_id', 'mode', 'execution_status', 'outcome', 'offset', 'scroll'].flatMap((key) =>
    typeof query[key] === 'string' ? [[key, query[key] as string]] : [],
  ))
}

export const modeLabels = { reproduce: '漏洞复现', retest: '修复后复测' }
export const statusLabels: Record<VerificationStatus, string> = {
  queued: '等待执行', running: '执行中', cancelling: '停止确认中',
  completed: '已完成', failed: '执行失败', cancelled: '已停止',
}
export const outcomeLabels: Record<VerificationOutcome, string> = {
  confirmed: '本轮确认', not_reproduced: '本轮未复现',
  environment_changed: '环境或条件变化', insufficient_evidence: '证据或条件不足',
}
export const statusTone = (status: VerificationStatus): BadgeTone => {
  if (status === 'failed') return 'danger'
  if (status === 'cancelling') return 'warning'
  if (status === 'running' || status === 'queued') return 'info'
  return 'neutral'
}
export const outcomeTone = (outcome: VerificationOutcome): BadgeTone =>
  outcome === 'confirmed' ? 'danger' : outcome === 'not_reproduced' ? 'neutral' : 'warning'
export const isActiveVerification = (status: VerificationStatus) =>
  status === 'queued' || status === 'running' || status === 'cancelling'
export const errorMessage = (error: unknown) => error instanceof Error ? error.message : '请求失败，请重试。'
export const cleanupLabels: Record<string, string> = {
  pending: '回收尚未确认', removed: '已回收', not_allocated: '未分配运行环境',
}
export const runStatusLabels: Record<string, string> = {
  queued: '等待执行', running: '执行中', succeeded: '运行结束', failed: '运行失败',
  cancelled: '已取消', timed_out: '执行超时',
}
