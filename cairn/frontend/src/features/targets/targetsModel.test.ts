import { describe, expect, it } from 'vitest'
import type { Target } from '@/api/types'
import {
  TARGET_STATUSES,
  TARGET_STATUS_FILTER_OPTIONS,
  TARGET_STATUS_META,
  TARGET_TYPES,
  TARGET_TYPE_META,
  TARGET_TYPE_OPTIONS,
  countTargetsByStatus,
  filterAndSortTargets,
  reconcileSelectedTargetIds,
  targetStatusMeta,
  targetTypeMeta,
} from './targetsModel'

let targetSequence = 0

const target = (overrides: Partial<Target> = {}): Target => {
  targetSequence += 1
  return {
    id: `target-${targetSequence}`,
    project_id: 'project-a',
    value: `target-${targetSequence}.example.test`,
    type: 'domain',
    status: 'seed',
    source_fact_id: null,
    discovered_by_run_id: null,
    created_at: '2026-08-20T00:00:00Z',
    ...overrides,
  }
}

describe('target display metadata and options', () => {
  it('preserves canonical target types, labels, and Badge tones', () => {
    expect(TARGET_TYPES).toEqual(['ip', 'cidr', 'domain', 'url'])
    expect(TARGET_TYPES.map(targetTypeMeta)).toEqual([
      { label: 'IP 地址', tone: 'neutral' },
      { label: 'CIDR 网段', tone: 'neutral' },
      { label: '域名 Domain', tone: 'neutral' },
      { label: 'URL 路径', tone: 'neutral' },
    ])
    expect(TARGET_TYPE_META).toEqual({
      ip: { label: 'IP 地址', tone: 'neutral' },
      cidr: { label: 'CIDR 网段', tone: 'neutral' },
      domain: { label: '域名 Domain', tone: 'neutral' },
      url: { label: 'URL 路径', tone: 'neutral' },
    })
  })

  it('provides target type options in canonical order', () => {
    expect(TARGET_TYPE_OPTIONS).toEqual([
      { value: 'ip', label: 'IP 地址' },
      { value: 'cidr', label: 'CIDR 网段' },
      { value: 'domain', label: '域名 Domain' },
      { value: 'url', label: 'URL 路径' },
    ])
  })

  it('preserves canonical statuses, Chinese labels, and Badge tones', () => {
    expect(TARGET_STATUSES).toEqual(['seed', 'discovered', 'approved', 'rejected'])
    expect(TARGET_STATUSES.map(targetStatusMeta)).toEqual([
      { label: '初始种子', tone: 'info' },
      { label: '等待授权', tone: 'warning' },
      { label: '探索中', tone: 'success' },
      { label: '已搁置', tone: 'neutral' },
    ])
    expect(TARGET_STATUS_META).toEqual({
      seed: { label: '初始种子', tone: 'info' },
      discovered: { label: '等待授权', tone: 'warning' },
      approved: { label: '探索中', tone: 'success' },
      rejected: { label: '已搁置', tone: 'neutral' },
    })
  })

  it('provides an all-status filter followed by all canonical statuses', () => {
    expect(TARGET_STATUS_FILTER_OPTIONS).toEqual([
      { value: 'all', label: '全部' },
      { value: 'seed', label: '初始种子' },
      { value: 'discovered', label: '等待授权' },
      { value: 'approved', label: '探索中' },
      { value: 'rejected', label: '已搁置' },
    ])
  })
})

describe('filterAndSortTargets', () => {
  it('trims search whitespace and matches target values case-insensitively', () => {
    const matching = target({ id: 'matching', value: 'API.Example.TEST' })
    const nonMatching = target({ id: 'other', value: 'other.test' })

    expect(filterAndSortTargets([nonMatching, matching], '  example.test  ', 'all')).toEqual([
      matching,
    ])
  })

  it('treats a whitespace-only search as empty and composes search with status filtering', () => {
    const approvedMatch = target({
      id: 'approved-match',
      value: 'https://api.example.test/admin',
      status: 'approved',
      created_at: '2026-08-20T03:00:00Z',
    })
    const discoveredMatch = target({
      id: 'discovered-match',
      value: 'api.example.test',
      status: 'discovered',
      created_at: '2026-08-20T02:00:00Z',
    })
    const approvedNonMatch = target({
      id: 'approved-other',
      value: 'other.test',
      status: 'approved',
      created_at: '2026-08-20T01:00:00Z',
    })
    const source = [approvedNonMatch, discoveredMatch, approvedMatch]

    expect(filterAndSortTargets(source, '   ', 'all')).toEqual([
      approvedMatch,
      discoveredMatch,
      approvedNonMatch,
    ])
    expect(filterAndSortTargets(source, ' API.EXAMPLE ', 'approved')).toEqual([approvedMatch])
    expect(filterAndSortTargets(source, 'api.example', 'discovered')).toEqual([discoveredMatch])
  })

  it('sorts newest creation time first and preserves source order for equal timestamps', () => {
    const tiedFirst = target({ id: 'tied-first', created_at: '2026-08-20T02:00:00Z' })
    const oldest = target({ id: 'oldest', created_at: '2026-08-20T01:00:00Z' })
    const tiedSecond = target({ id: 'tied-second', created_at: '2026-08-20T02:00:00Z' })
    const newest = target({ id: 'newest', created_at: '2026-08-20T03:00:00Z' })

    expect(filterAndSortTargets([tiedFirst, oldest, tiedSecond, newest], '', 'all')).toEqual([
      newest,
      tiedFirst,
      tiedSecond,
      oldest,
    ])
  })

  it('does not mutate the source array or target objects', () => {
    const first = Object.freeze(
      target({ id: 'first', value: 'first.test', created_at: '2026-08-20T01:00:00Z' }),
    )
    const second = Object.freeze(
      target({ id: 'second', value: 'second.test', created_at: '2026-08-20T02:00:00Z' }),
    )
    const source = Object.freeze([first, second])

    const result = filterAndSortTargets(source, '', 'all')

    expect(result).toEqual([second, first])
    expect(source).toEqual([first, second])
    expect(result[0]).toBe(second)
    expect(result[1]).toBe(first)
  })
})

describe('target counts and selection reconciliation', () => {
  it('counts every target status and returns zeroes for absent statuses', () => {
    expect(
      countTargetsByStatus([
        target({ status: 'seed' }),
        target({ status: 'discovered' }),
        target({ status: 'approved' }),
        target({ status: 'approved' }),
      ]),
    ).toEqual({ seed: 1, discovered: 1, approved: 2, rejected: 0 })
    expect(countTargetsByStatus([])).toEqual({ seed: 0, discovered: 0, approved: 0, rejected: 0 })
  })

  it('removes stale selections while retaining selections hidden by the active filter', () => {
    const visible = target({ id: 'visible', status: 'approved' })
    const hiddenByFilter = target({ id: 'hidden', status: 'seed' })
    const fullTargets = [visible, hiddenByFilter]
    const filteredTargets = filterAndSortTargets(fullTargets, '', 'approved')
    const selectedIds = new Set(['visible', 'hidden', 'deleted'])

    expect(filteredTargets.map((item) => item.id)).toEqual(['visible'])
    expect(reconcileSelectedTargetIds(selectedIds, fullTargets)).toEqual(
      new Set(['visible', 'hidden']),
    )
    expect(selectedIds).toEqual(new Set(['visible', 'hidden', 'deleted']))
    expect(fullTargets).toEqual([visible, hiddenByFilter])
  })
})
