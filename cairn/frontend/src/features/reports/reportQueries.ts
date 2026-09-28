import type { ReportInventoryFilters } from '@/api/types'

export const reportKeys = {
  all: () => ['reports'] as const,
  lists: () => [...reportKeys.all(), 'list'] as const,
  list: (filters: ReportInventoryFilters) =>
    [
      ...reportKeys.lists(),
      filters.q ?? '',
      filters.project_id ?? '',
      filters.template_key ?? '',
      filters.date_from ?? '',
      filters.date_to ?? '',
      filters.page ?? 1,
      filters.page_size ?? 20,
    ] as const,
  preview: (projectId: string, artifactId: string) =>
    [...reportKeys.all(), 'preview', projectId, artifactId] as const,
}
