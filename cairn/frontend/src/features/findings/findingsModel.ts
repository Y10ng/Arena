import type { VulnerabilityListItem } from '@/api/client'
import {
  vulnerabilitySeverityMeta,
  type VulnerabilitySeverity,
} from '@/features/vulnerabilities/vulnerabilityMetadata'

export type FindingsSeverityCount = Record<VulnerabilitySeverity, number>

export interface FindingsProjectGroup {
  project_id: string
  project_title: string
  vulns: VulnerabilityListItem[]
  sevCount: FindingsSeverityCount
  topSeverity: VulnerabilitySeverity
}

const emptySeverityCount = (): FindingsSeverityCount => ({
  critical: 0,
  high: 0,
  medium: 0,
  low: 0,
  info: 0,
})

const compareFindings = (left: VulnerabilityListItem, right: VulnerabilityListItem): number => {
  const severityDifference =
    vulnerabilitySeverityMeta(right.severity).rank - vulnerabilitySeverityMeta(left.severity).rank
  if (severityDifference !== 0) return severityDifference
  return right.created_at.localeCompare(left.created_at)
}

export const findingsProjectScore = (group: FindingsProjectGroup): number =>
  group.sevCount.critical * 1000 +
  group.sevCount.high * 100 +
  group.sevCount.medium * 10 +
  group.vulns.length

export const findingRowKey = (finding: Pick<VulnerabilityListItem, 'project_id' | 'id'>): string =>
  `${finding.project_id}:${finding.id}`

export const buildFindingsGroups = (
  findings: readonly VulnerabilityListItem[],
): FindingsProjectGroup[] => {
  const groups = new Map<string, FindingsProjectGroup>()

  for (const finding of findings) {
    let group = groups.get(finding.project_id)
    if (!group) {
      group = {
        project_id: finding.project_id,
        project_title: finding.project_title || finding.project_id,
        vulns: [],
        sevCount: emptySeverityCount(),
        topSeverity: 'info',
      }
      groups.set(finding.project_id, group)
    }

    group.vulns.push(finding)
    group.sevCount[finding.severity] += 1
    if (
      vulnerabilitySeverityMeta(finding.severity).rank >
      vulnerabilitySeverityMeta(group.topSeverity).rank
    ) {
      group.topSeverity = finding.severity
    }
  }

  for (const group of groups.values()) group.vulns.sort(compareFindings)

  return [...groups.values()].sort(
    (left, right) => findingsProjectScore(right) - findingsProjectScore(left),
  )
}

export const countFindingsBySeverity = (
  findings: readonly VulnerabilityListItem[],
): FindingsSeverityCount => {
  const counts = emptySeverityCount()
  for (const finding of findings) counts[finding.severity] += 1
  return counts
}
