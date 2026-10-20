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
  completed: '已完成', failed: '执行失败', cancelled: '已停止', cleanup_pending: '清理未完成',
}
export const outcomeLabels: Record<VerificationOutcome, string> = {
  confirmed: '漏洞存在', not_reproduced: '本次未复现',
  environment_changed: '环境或条件变化', insufficient_evidence: '证据或条件不足',
}
export const statusTone = (status: VerificationStatus): BadgeTone => {
  if (status === 'failed') return 'danger'
  if (status === 'cancelling' || status === 'cleanup_pending') return 'warning'
  if (status === 'running' || status === 'queued') return 'info'
  return 'neutral'
}
export const outcomeTone = (outcome: VerificationOutcome): BadgeTone =>
  outcome === 'confirmed' ? 'danger' : outcome === 'not_reproduced' ? 'neutral' : 'warning'
export const isActiveVerification = (status: VerificationStatus) =>
  status === 'queued' || status === 'running' || status === 'cancelling'
export const errorMessage = (error: unknown) => error instanceof Error ? error.message : '请求失败，请重试。'
