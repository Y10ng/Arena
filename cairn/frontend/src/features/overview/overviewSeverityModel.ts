import type { OverviewResponse } from '@/api/types/overview'
import {
  VULNERABILITY_SEVERITIES,
  vulnerabilitySeverityMeta,
} from '@/features/vulnerabilities/vulnerabilityMetadata'

export const buildSeverityRows = (summary: OverviewResponse['vulnerabilities']) => {
  const maximum = Math.max(
    0,
    ...VULNERABILITY_SEVERITIES.map((key) => summary.by_severity[key] ?? 0),
  )
  return VULNERABILITY_SEVERITIES.map((key) => {
    const count = summary.by_severity[key] ?? 0
    const percent = summary.total > 0 ? (count / summary.total) * 100 : null
    const percentLabel =
      percent === null ? null : percent > 0 && percent < 0.1 ? '<0.1%' : `${percent.toFixed(1)}%`
    const label = vulnerabilitySeverityMeta(key).label
    return {
      key,
      label,
      count,
      widthPercent: maximum > 0 ? (count / maximum) * 100 : 0,
      description: `${label}：${count} 条${percentLabel === null ? '' : `，占 ${percentLabel}`}，查看漏洞记录`,
    }
  })
}

export const overviewRefreshInterval = (data: OverviewResponse | undefined, visible: boolean) =>
  visible &&
  data &&
  (data.projects.by_status.active ?? 0) + (data.projects.by_status.pending ?? 0) > 0
    ? 60_000
    : false
