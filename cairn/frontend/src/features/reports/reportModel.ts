import type { AssessmentReportFinalization } from '@/api/types'
import type { BadgeTone } from '@/components/ui'

export interface ReportBadgeMeta {
  label: string
  tone: BadgeTone
}

const REPORT_STATE_LABELS: Record<string, string> = {
  pending: '等待生成',
  running: '生成中',
  ready: '报告已就绪',
  failed: '生成失败',
  cancelled: '已取消',
  'not-started': '尚未生成',
  'not-eligible': '当前不可生成',
}

export function reportNextRetryAt(
  report: AssessmentReportFinalization | null | undefined,
  now = Date.now(),
): string | null {
  if (!report || !['pending', 'failed'].includes(report.status) || !report.next_attempt_at)
    return null
  return Date.parse(report.next_attempt_at) > now ? report.next_attempt_at : null
}

export function reportPollInterval(report: AssessmentReportFinalization | null | undefined) {
  if (report?.status === 'pending' || report?.status === 'running') return 10_000
  const nextRetryAt = reportNextRetryAt(report)
  // 到达计划时间后再确认一次状态；过期且没有新计划的失败记录不继续轮询。
  return nextRetryAt ? Math.min(10_000, Date.parse(nextRetryAt) - Date.now() + 100) : false
}

export const reportStateLabel = (status: string) => REPORT_STATE_LABELS[status] || status

export const reportStateMeta = (status: string): ReportBadgeMeta => ({
  label: reportStateLabel(status),
  tone:
    status === 'ready'
      ? 'success'
      : status === 'failed'
        ? 'danger'
        : status === 'running'
          ? 'info'
          : status === 'pending'
            ? 'warning'
            : 'neutral',
})
