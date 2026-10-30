import { describe, expect, it } from 'vitest'
import { overviewKey } from '@/api/overview'
import type { OverviewResponse } from '@/api/types/overview'
import { buildSeverityRows, overviewRefreshInterval } from './overviewSeverityModel'

const empty: OverviewResponse = {
  generated_at: '2026-10-27T00:00:00Z',
  projects: { total: 0, by_status: {} },
  vulnerabilities: { total: 0, by_severity: {}, by_status: {} },
  project_previews: [],
  vulnerability_previews: [],
}

describe('overview read model', () => {
  it('retains five zero rows without inventing percentages', () => {
    const rows = buildSeverityRows(empty.vulnerabilities)
    expect(rows.map((row) => row.key)).toEqual(['critical', 'high', 'medium', 'low', 'info'])
    expect(rows.every((row) => row.count === 0 && row.widthPercent === 0)).toBe(true)
    expect(rows.every((row) => !row.description.includes('%'))).toBe(true)
  })

  it('scales every bar against the same maximum, without a minimum nonzero width', () => {
    const rows = buildSeverityRows({
      total: 10000,
      by_severity: { critical: 9999, info: 1 },
      by_status: {},
    })
    expect(rows[0].widthPercent).toBe(100)
    expect(rows[1].widthPercent).toBe(0)
    expect(rows[4].widthPercent).toBeCloseTo(100 / 9999)
    expect(rows[4].description).toContain('<0.1%')
    const close = buildSeverityRows({
      total: 9,
      by_severity: { high: 5, medium: 4 },
      by_status: {},
    })
    expect(close[1].widthPercent).toBe(100)
    expect(close[2].widthPercent).toBe(80)
  })

  it('refreshes only visible active or pending ordinary projects, at one minute', () => {
    expect(overviewRefreshInterval(undefined, true)).toBe(false)
    expect(overviewRefreshInterval(empty, true)).toBe(false)
    for (const status of ['active', 'pending'] as const) {
      const data = { ...empty, projects: { total: 1, by_status: { [status]: 1 } } }
      expect(overviewRefreshInterval(data, true)).toBe(60_000)
      expect(overviewRefreshInterval(data, false)).toBe(false)
    }
    expect(
      overviewRefreshInterval({ ...empty, projects: { total: 1, by_status: { paused: 1 } } }, true),
    ).toBe(false)
  })

  it('isolates cached data by user, tenant and administrator scope', () => {
    const session = {
      user_id: 'u1',
      tenant_id: 't1',
      role: 'user' as const,
      must_change_password: false,
    }
    expect(overviewKey(session)).toEqual(['overview', 'u1', 't1', 'user'])
    for (const change of [
      { user_id: 'u2' },
      { tenant_id: 't2' },
      { role: 'super_admin' as const },
    ]) {
      expect(overviewKey({ ...session, ...change })).not.toEqual(overviewKey(session))
    }
  })
})
