import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import type { Hint, ProjectDetail } from '@/api/types'
import { ApiError, getProjectDetail } from '@/api/client'
import { useWorkspaceStore } from './workspace'

vi.mock('@/api/client', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/client')>()),
  getProjectDetail: vi.fn(),
}))

const projectDetail = (id: string): ProjectDetail => ({
  project: {
    id,
    title: id,
    status: 'active',
    created_at: '2026-01-01T00:00:00Z',
    scenario_key: 'default-copy-v2',
    tenant_workers: [],
    tenant_id: 'tenant-1',
    tenant_name: 'Tenant 1',
    assessment_protocol: 'default-copy-v1',
    report_capability: {
      supported: true,
      policy_version: 1,
      template_profile: 'assessment-report-packet-v1',
      reason: null,
    },
  },
  facts: [],
  intents: [],
  hints: [],
  targets: [],
  resources: [],
  library_bindings: [],
})

const mockedGetProjectDetail = vi.mocked(getProjectDetail)

describe('workspace store refresh behavior', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    mockedGetProjectDetail.mockReset()
  })

  it('preserves the last detail when a same-project refresh fails', async () => {
    mockedGetProjectDetail
      .mockResolvedValueOnce(projectDetail('p1'))
      .mockRejectedValueOnce(new Error('暂时不可用'))
    const store = useWorkspaceStore()

    await store.loadProject('p1')
    await store.loadProject('p1')

    expect(store.projectDetail?.project.id).toBe('p1')
    expect(store.error).toBe('暂时不可用')
  })

  it.each([401, 403, 404])('clears stale detail after terminal status %s', async (status) => {
    mockedGetProjectDetail
      .mockResolvedValueOnce(projectDetail('p1'))
      .mockRejectedValueOnce(new ApiError('不可访问', status, { detail: 'unavailable' }))
    const store = useWorkspaceStore()

    await store.loadProject('p1')
    await store.loadProject('p1')

    expect(store.projectDetail).toBeNull()
    expect(store.error).toBe('不可访问')
  })

  it('does not retain another project while switching routes', async () => {
    mockedGetProjectDetail
      .mockResolvedValueOnce(projectDetail('p1'))
      .mockRejectedValueOnce(new Error('加载失败'))
    const store = useWorkspaceStore()

    await store.loadProject('p1')
    await store.loadProject('p2')

    expect(store.currentProjectId).toBe('p2')
    expect(store.projectDetail).toBeNull()
    expect(store.error).toBe('加载失败')
  })

  it('ignores a late response from an older project request', async () => {
    let resolveFirst!: (detail: ProjectDetail) => void
    mockedGetProjectDetail
      .mockReturnValueOnce(
        new Promise((resolve) => {
          resolveFirst = resolve
        }),
      )
      .mockResolvedValueOnce(projectDetail('p2'))
    const store = useWorkspaceStore()

    const first = store.loadProject('p1')
    await store.loadProject('p2')
    resolveFirst(projectDetail('p1'))
    await first

    expect(store.currentProjectId).toBe('p2')
    expect(store.projectDetail?.project.id).toBe('p2')
  })

  it('clears a refresh error after a successful retry', async () => {
    mockedGetProjectDetail
      .mockRejectedValueOnce(new Error('暂时不可用'))
      .mockResolvedValueOnce(projectDetail('p1'))
    const store = useWorkspaceStore()

    await store.loadProject('p1')
    await store.loadProject('p1')

    expect(store.error).toBeNull()
    expect(store.projectDetail?.project.id).toBe('p1')
  })

  it('immutably replaces hints only for the active project detail', async () => {
    mockedGetProjectDetail.mockResolvedValueOnce(projectDetail('p1'))
    const store = useWorkspaceStore()
    await store.loadProject('p1')
    const originalDetail = store.projectDetail
    const hints: Hint[] = [
      {
        id: 'h001',
        content: 'Authorization: Bearer allowed-secret',
        creator: 'usr-1',
        fact_id: null,
        intent_id: null,
        created_at: '2026-08-21T00:00:00Z',
      },
    ]

    store.replaceProjectHints('p1', hints)

    expect(store.projectDetail).not.toBe(originalDetail)
    expect(store.projectDetail?.hints).toEqual(hints)
    expect(store.projectDetail?.hints).not.toBe(hints)
  })

  it('ignores hint replacement for a different current or detail project', async () => {
    mockedGetProjectDetail.mockResolvedValueOnce(projectDetail('p1'))
    const store = useWorkspaceStore()
    await store.loadProject('p1')
    const originalDetail = store.projectDetail
    const hints: Hint[] = [
      {
        id: 'h001',
        content: 'stale',
        creator: 'usr-1',
        created_at: '2026-08-21T00:00:00Z',
      },
    ]

    store.replaceProjectHints('p2', hints)
    expect(store.projectDetail).toBe(originalDetail)

    store.currentProjectId = 'p2'
    store.replaceProjectHints('p2', hints)
    expect(store.projectDetail).toBe(originalDetail)
  })
})
