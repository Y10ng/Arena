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
  'not-started': '等待手动生成',
  'not-eligible': '暂停或完成后可生成',
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
