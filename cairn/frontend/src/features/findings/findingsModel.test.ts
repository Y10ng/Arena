import { describe, expect, it } from 'vitest'
import type { VulnerabilityListItem } from '@/api/client'
import {
  buildFindingsGroups,
  countFindingsBySeverity,
  findingRowKey,
  findingsProjectScore,
} from './findingsModel'

let findingSequence = 0

const finding = (overrides: Partial<VulnerabilityListItem> = {}): VulnerabilityListItem => {
  findingSequence += 1
  return {
    id: `finding-${findingSequence}`,
    project_id: 'project-a',
    project_title: '项目 A',
    title: `漏洞 ${findingSequence}`,
    severity: 'info',
    vuln_type: 'test',
    target: 'https://example.test',
    confidence: 0.8,
    status: 'new',
    source_fact_id: null,
    remediation: '',
    evidence_items: [],
    repro_steps: '',
    discovered_by_run_id: null,
    discovered_by_worker: null,
    slot_key: null,
    fingerprint: null,
    identity: {},
    created_at: '2026-08-20T00:00:00Z',
    updated_at: '2026-08-20T00:00:00Z',
    ...overrides,
  }
}

describe('Findings grouping model', () => {
  it('groups by project and sorts findings by severity then newest creation time', () => {
    const oldestHigh = finding({
      id: 'high-old',
      severity: 'high',
      created_at: '2026-08-19T10:00:00Z',
    })
    const critical = finding({
      id: 'critical',
      severity: 'critical',
      created_at: '2026-08-18T10:00:00Z',
    })
    const newestHigh = finding({
      id: 'high-new',
      severity: 'high',
      created_at: '2026-08-20T10:00:00Z',
    })
    const source = [oldestHigh, critical, newestHigh]

    const [group] = buildFindingsGroups(source)

    expect(group).toMatchObject({
      project_id: 'project-a',
      project_title: '项目 A',
      topSeverity: 'critical',
      sevCount: { critical: 1, high: 2, medium: 0, low: 0, info: 0 },
    })
    expect(group.vulns.map((item) => item.id)).toEqual(['critical', 'high-new', 'high-old'])
    expect(source.map((item) => item.id)).toEqual(['high-old', 'critical', 'high-new'])
  })

  it('uses the exact weighted project score formula', () => {
    const [group] = buildFindingsGroups([
      finding({ severity: 'critical' }),
      finding({ severity: 'high' }),
      finding({ severity: 'medium' }),
      finding({ severity: 'low' }),
      finding({ severity: 'info' }),
    ])

    expect(findingsProjectScore(group)).toBe(1115)
  })

  it('lets low and info findings affect project rank through total count', () => {
    const findings = [
      finding({ project_id: 'project-medium', project_title: '中危项目', severity: 'medium' }),
      ...Array.from({ length: 12 }, (_, index) =>
        finding({
          project_id: 'project-info',
          project_title: '信息项目',
          severity: index === 0 ? 'low' : 'info',
        }),
      ),
    ]

    const groups = buildFindingsGroups(findings)

    expect(groups.map((group) => group.project_id)).toEqual(['project-info', 'project-medium'])
    expect(groups.map(findingsProjectScore)).toEqual([12, 11])
  })

  it('preserves first project appearance when scores are equal', () => {
    const groups = buildFindingsGroups([
      finding({ project_id: 'project-b', project_title: '项目 B', severity: 'high' }),
      finding({ project_id: 'project-a', project_title: '项目 A', severity: 'high' }),
    ])

    expect(groups.map((group) => group.project_id)).toEqual(['project-b', 'project-a'])
  })

  it('counts all severities with a complete typed result', () => {
    expect(
      countFindingsBySeverity([
        finding({ severity: 'critical' }),
        finding({ severity: 'critical' }),
        finding({ severity: 'low' }),
      ]),
    ).toEqual({ critical: 2, high: 0, medium: 0, low: 1, info: 0 })
  })

  it('uses project and vulnerability IDs for composite row keys', () => {
    const first = finding({ id: 'shared-id', project_id: 'project-a' })
    const second = finding({ id: 'shared-id', project_id: 'project-b' })

    expect(findingRowKey(first)).toBe('project-a:shared-id')
    expect(findingRowKey(second)).toBe('project-b:shared-id')
    expect(findingRowKey(first)).not.toBe(findingRowKey(second))
  })

  it('falls back to the project ID when a project title is empty', () => {
    const [group] = buildFindingsGroups([
      finding({ project_id: 'project-without-title', project_title: '' }),
    ])

    expect(group.project_title).toBe('project-without-title')
  })
})
