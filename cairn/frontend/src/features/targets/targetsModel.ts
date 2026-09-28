import type { Target, TargetStatus, TargetType } from '@/api/types'
import type { BadgeTone } from '@/components/ui'

export interface TargetDisplayMeta {
  label: string
  tone: BadgeTone
}

export interface TargetOption<T extends string> {
  value: T
  label: string
}

export type TargetStatusFilter = 'all' | TargetStatus
export type TargetStatusCounts = Record<TargetStatus, number>

export const TARGET_TYPES = ['ip', 'cidr', 'domain', 'url'] as const satisfies readonly TargetType[]

export const TARGET_STATUSES = [
  'seed',
  'discovered',
  'approved',
  'rejected',
] as const satisfies readonly TargetStatus[]

export const TARGET_TYPE_META = {
  ip: { label: 'IP 地址', tone: 'neutral' },
  cidr: { label: 'CIDR 网段', tone: 'neutral' },
  domain: { label: '域名 Domain', tone: 'neutral' },
  url: { label: 'URL 路径', tone: 'neutral' },
} as const satisfies Record<TargetType, TargetDisplayMeta>

export const TARGET_STATUS_META = {
  seed: { label: '初始种子', tone: 'info' },
  discovered: { label: '等待授权', tone: 'warning' },
  approved: { label: '探索中', tone: 'success' },
  rejected: { label: '已搁置', tone: 'neutral' },
} as const satisfies Record<TargetStatus, TargetDisplayMeta>

export const TARGET_TYPE_OPTIONS: readonly TargetOption<TargetType>[] = TARGET_TYPES.map(
  (type) => ({
    value: type,
    label: TARGET_TYPE_META[type].label,
  }),
)

export const TARGET_STATUS_FILTER_OPTIONS: readonly TargetOption<TargetStatusFilter>[] = [
  { value: 'all', label: '全部' },
  ...TARGET_STATUSES.map((status) => ({
    value: status,
    label: TARGET_STATUS_META[status].label,
  })),
]

export const targetTypeMeta = (type: TargetType): TargetDisplayMeta => TARGET_TYPE_META[type]

export const targetStatusMeta = (status: TargetStatus): TargetDisplayMeta =>
  TARGET_STATUS_META[status]

export const filterAndSortTargets = (
  targets: readonly Target[],
  searchValue: string,
  statusFilter: TargetStatusFilter,
): Target[] => {
  const normalizedSearch = searchValue.trim().toLowerCase()

  return targets
    .map((target, index) => ({ target, index }))
    .filter(({ target }) => {
      const matchesValue = target.value.toLowerCase().includes(normalizedSearch)
      const matchesStatus = statusFilter === 'all' || target.status === statusFilter
      return matchesValue && matchesStatus
    })
    .sort(
      (left, right) =>
        right.target.created_at.localeCompare(left.target.created_at) || left.index - right.index,
    )
    .map(({ target }) => target)
}

export const countTargetsByStatus = (targets: readonly Target[]): TargetStatusCounts => {
  const counts: TargetStatusCounts = {
    seed: 0,
    discovered: 0,
    approved: 0,
    rejected: 0,
  }

  for (const target of targets) counts[target.status] += 1
  return counts
}

export const reconcileSelectedTargetIds = (
  selectedTargetIds: ReadonlySet<string>,
  targets: readonly Target[],
): Set<string> => {
  const targetIds = new Set(targets.map((target) => target.id))
  return new Set([...selectedTargetIds].filter((targetId) => targetIds.has(targetId)))
}
