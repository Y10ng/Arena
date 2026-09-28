import { describe, expect, it } from 'vitest'
import type { ProjectSummary } from '@/api/types'
import { projectMatchesSearch } from './useProjectsPage'

const project: ProjectSummary = {
  id: 'project-foreign',
  title: 'Foreign Assessment',
  status: 'active',
  created_at: '2026-08-19T00:00:00Z',
  scenario_key: 'default',
  assessment_protocol: '',
  report_capability: {
    supported: false,
    policy_version: 0,
    template_profile: null,
    reason: 'report_policy_not_enabled',
  },
  tenant_workers: [],
  tenant_id: 'tenant-foreign',
  tenant_name: 'Foreign Tenant',
  tenant_owner_login: 'foreign-owner',
  group_id: 'group-red',
  group_name: 'Red Team',
  fact_count: 0,
  intent_count: 0,
  working_intent_count: 0,
  unclaimed_intent_count: 0,
  hint_count: 0,
}

describe('projectMatchesSearch', () => {
  it.each([
    'foreign assessment',
    'red team',
    '运行中',
    '默认场景',
  ])('matches ordinary project fields for %s', query => {
    expect(projectMatchesSearch(project, query, '运行中', '默认场景')).toBe(true)
  })

  it.each([
    'TENANT-FOREIGN',
    'foreign tenant',
    'foreign-owner',
  ])('only matches tenant attribution for super administrators: %s', query => {
    expect(projectMatchesSearch(project, query, '运行中', '默认场景')).toBe(false)
    expect(projectMatchesSearch(project, query, '运行中', '默认场景', true)).toBe(true)
  })

  it('trims input and rejects unrelated values', () => {
    expect(projectMatchesSearch(project, '  red team  ', '运行中', '默认场景')).toBe(true)
    expect(projectMatchesSearch(project, 'other tenant', '运行中', '默认场景', true)).toBe(false)
  })
})
