import { flushPromises, mount } from '@vue/test-utils'
import { QueryClient, VueQueryPlugin } from '@tanstack/vue-query'
import { createPinia } from 'pinia'
import { defineComponent } from 'vue'
import { createMemoryHistory, createRouter } from 'vue-router'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { VueWrapper } from '@vue/test-utils'
import type {
  AssessmentQualitySnapshot,
  CoverageCell,
  ProjectDetail,
  SurfaceManifestEntry,
} from '@/api/types'
import { useUIStore } from '@/stores/ui'
import { useWorkspaceStore } from '@/stores/workspace'
import CoveragePage from './CoveragePage.vue'

const mocks = vi.hoisted(() => ({
  getProjectDetail: vi.fn(),
  listProjectCoverageCells: vi.fn(),
  listProjectSurfaceManifest: vi.fn(),
}))

vi.mock('@/api/client', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/client')>()),
  getProjectDetail: mocks.getProjectDetail,
  listProjectCoverageCells: mocks.listProjectCoverageCells,
  listProjectSurfaceManifest: mocks.listProjectSurfaceManifest,
}))

const RouteTarget = defineComponent({ template: '<div />' })
const wrappers: VueWrapper[] = []
const queryClients: QueryClient[] = []

const quality = (
  overrides: Partial<AssessmentQualitySnapshot> = {},
): AssessmentQualitySnapshot => ({
  version: 1,
  status: 'ready',
  required: {
    total_count: 4,
    qualified_count: 3,
    unqualified_count: 1,
    unresolved_count: 0,
    finding_count: 1,
    tested_no_finding_count: 2,
    permanent_limitation_count: 0,
    administrative_skip_count: 0,
  },
  optional: {
    total_count: 2,
    qualified_count: 1,
    unqualified_count: 1,
    unresolved_count: 0,
    finding_count: 0,
    tested_no_finding_count: 1,
    permanent_limitation_count: 0,
    administrative_skip_count: 0,
  },
  manifest_surface_count: 2,
  missing_required_surface_count: 1,
  charged_attempt_count: 6,
  runtime_execution_count: 8,
  runtime_retry_count: 2,
  failed_attempt_count: 1,
  active_blocker_count: 1,
  active_cooldown_count: 1,
  finding_count: 1,
  observation_count: 3,
  confirmed_finding_count: 1,
  independently_verified_finding_count: 1,
  pending_verifier_count: 0,
  chain_candidate_count: 1,
  chain_verification_ready_count: 1,
  confirmed_chain_count: 0,
  stop_reason: 'saturated',
  residual_risks: ['active_target_blocker', 'future_risk'],
  ...overrides,
})

function detail(
  projectId: string,
  overrides: Partial<ProjectDetail['project']> = {},
): ProjectDetail {
  return {
    project: {
      id: projectId,
      title: projectId,
      status: 'active',
      created_at: '2026-08-21T00:00:00Z',
      scenario_key: 'default-copy-v2',
      tenant_workers: [],
      tenant_id: 'tenant-a',
      tenant_name: 'Tenant A',
      assessment_protocol: 'default-copy-v1',
      assessment_quality: quality(),
      ...overrides,
      report_capability: overrides.report_capability ?? {
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
  }
}

function observation(
  entryId: string,
  surfaceId: string,
  overrides: Partial<SurfaceManifestEntry> = {},
): SurfaceManifestEntry {
  return {
    entry_id: entryId,
    project_id: 'project-a',
    surface_id: surfaceId,
    surface_kind: 'endpoint',
    surface_key: `GET /${entryId}`,
    description: `${entryId} description`,
    traits: {},
    evidence: [],
    observation_fingerprint: `fingerprint-${entryId}`,
    manifest_version: 1,
    status: 'observed',
    created_at: '2026-08-21T00:00:00Z',
    ...overrides,
  }
}

function cell(overrides: Partial<CoverageCell> = {}): CoverageCell {
  return {
    project_id: 'project-a',
    slot_key: 'slot-a',
    kind: 'coverage',
    description: '检查 <script>window.pwned = true</script> password=clear-text',
    status: 'pending',
    surface_key: 'GET /accounts/:id',
    surface_id: 'surface-a',
    surface_kind: 'endpoint',
    test_family: 'authorization',
    coverage_requirement: 'optional',
    coverage_rule_id: 'object-authorization',
    applicability_basis: 'Authorization: Bearer credential-token',
    completion_criteria: '跨用户读取必须返回完整证据 pointer=$.secret',
    result: 'temporarily_blocked',
    skip_reason: '',
    blocker_fingerprint: 'password=blocker-secret',
    blocker_strategy_key: 'retry-with-cookie',
    blocker_cooldown_until: '2099-08-21T00:10:00Z',
    tier: 'core',
    resulting_fact_id: 'fact-a',
    verified_run_id: 'run-a',
    created_at: '2026-08-21T00:00:00Z',
    updated_at: '2026-08-21T00:00:00Z',
    ...overrides,
  }
}

function deferred<T>() {
  let resolve!: (value: T | PromiseLike<T>) => void
  let reject!: (reason?: unknown) => void
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise
    reject = rejectPromise
  })
  return { promise, resolve, reject }
}

async function mountPage(
  projectId = 'project-a',
  projectDetail: ProjectDetail | null = detail(projectId),
) {
  const pinia = createPinia()
  const workspaceStore = useWorkspaceStore(pinia)
  workspaceStore.currentProjectId = projectId
  workspaceStore.projectDetail = projectDetail

  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false, gcTime: Infinity },
    },
  })
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/projects/:id/coverage', name: 'WorkspaceCoverage', component: RouteTarget },
      { path: '/projects/:id/runs', name: 'WorkspaceRuns', component: RouteTarget },
      { path: '/projects/:id/graph', name: 'WorkspaceGraph', component: RouteTarget },
      { path: '/projects/:id/artifacts', name: 'WorkspaceArtifacts', component: RouteTarget },
      {
        path: '/projects/:id/vulnerabilities',
        name: 'WorkspaceVulnerabilities',
        component: RouteTarget,
      },
    ],
  })
  await router.push({ name: 'WorkspaceCoverage', params: { id: projectId } })
  await router.isReady()

  const wrapper = mount(CoveragePage, {
    attachTo: document.body,
    global: {
      plugins: [pinia, [VueQueryPlugin, { queryClient }], router],
    },
  })
  wrappers.push(wrapper)
  queryClients.push(queryClient)
  return { pinia, queryClient, router, workspaceStore, wrapper }
}

function buttonByText(text: string): HTMLButtonElement {
  const button = [...document.body.querySelectorAll<HTMLButtonElement>('button')].find(
    (candidate) => candidate.textContent?.trim() === text,
  )
  if (!button) throw new Error(`找不到按钮：${text}`)
  return button
}

beforeEach(() => {
  vi.clearAllMocks()
  mocks.listProjectSurfaceManifest.mockResolvedValue([])
  mocks.listProjectCoverageCells.mockResolvedValue([])
})

afterEach(() => {
  for (const wrapper of wrappers.splice(0)) wrapper.unmount()
  for (const queryClient of queryClients.splice(0)) queryClient.clear()
  document.body.innerHTML = ''
  vi.useRealTimers()
})

describe('CoveragePage', () => {
  it('uses only route-matching workspace detail and keeps unsupported projects disabled', async () => {
    const { pinia, wrapper, workspaceStore } = await mountPage(
      'project-a',
      detail('project-a', { assessment_protocol: 'legacy' }),
    )
    await flushPromises()

    expect(wrapper.text()).toContain('当前项目未启用结构化 Coverage 协议')
    expect(mocks.getProjectDetail).not.toHaveBeenCalled()
    expect(mocks.listProjectSurfaceManifest).not.toHaveBeenCalled()
    expect(mocks.listProjectCoverageCells).not.toHaveBeenCalled()

    const uiStore = useUIStore(pinia)
    const refreshKey = uiStore.globalRefreshKey
    buttonByText('刷新').click()
    await flushPromises()
    expect(uiStore.globalRefreshKey).toBe(refreshKey + 1)
    expect(mocks.listProjectSurfaceManifest).not.toHaveBeenCalled()
    expect(mocks.listProjectCoverageCells).not.toHaveBeenCalled()

    workspaceStore.projectDetail = detail('project-b')
    await flushPromises()
    expect(wrapper.text()).toContain('正在读取项目 Coverage 配置')
    expect(wrapper.text()).not.toContain('当前项目未启用结构化 Coverage 协议')
  })

  it('does not render report panel, action, or query wording', async () => {
    const { wrapper } = await mountPage('project-a', detail('project-a', { status: 'completed' }))
    await flushPromises()

    expect(wrapper.text()).not.toContain('最终评估报告')
    expect(wrapper.text()).not.toContain('生成报告')
    expect(wrapper.text()).not.toContain('报告状态')
    expect(wrapper.text()).not.toContain('Surface Manifest 与 Coverage Cell 按既有周期刷新')
    expect(wrapper.text()).toContain('Coverage Cell')
    expect(wrapper.text()).toContain('Surface Manifest Observations')
  })

  it('renders authoritative semantics, full sensitive text, observations, and accessible cells', async () => {
    mocks.listProjectSurfaceManifest.mockResolvedValue([
      observation('entry-a1', 'surface-a', {
        description: 'Cookie=session-secret <img src=x onerror=alert(1)>',
        evidence: [
          {
            artifact_id: 'artifact-evidence',
            pointer: '$.headers.Authorization Bearer full-token',
          },
        ],
      }),
      observation('entry-a2', 'surface-a'),
      observation('entry-b1', 'surface-b'),
    ])
    mocks.listProjectCoverageCells.mockResolvedValue([cell()])

    const { wrapper } = await mountPage('project-a', detail('project-a', { status: 'completed' }))
    await flushPromises()

    expect(mocks.listProjectSurfaceManifest).toHaveBeenCalledWith(
      'project-a',
      expect.any(AbortSignal),
    )
    expect(mocks.listProjectCoverageCells).toHaveBeenCalledWith(
      'project-a',
      expect.any(AbortSignal),
    )
    expect(wrapper.text()).toContain('3 条 observation · 2 个 Surface')
    expect(wrapper.findAll('.coverage-observation')).toHaveLength(3)
    expect(wrapper.text()).toContain('Cookie=session-secret <img src=x onerror=alert(1)>')
    expect(wrapper.text()).toContain('$.headers.Authorization Bearer full-token')
    expect(wrapper.text()).toContain('password=clear-text')
    expect(wrapper.text()).toContain('password=blocker-secret')
    expect(document.body.querySelector('script')).toBeNull()

    expect(wrapper.get('caption').text()).toBe('项目 Coverage Cell 列表')
    expect(wrapper.get('[role="region"]').attributes('aria-label')).toBe('项目 Coverage Cell 列表')
    expect(wrapper.findAll('thead th').map((heading) => heading.text())).toEqual([
      'Surface',
      '测试族',
      '要求',
      '状态',
      '结果与阻断',
      '证据 / 跳转',
    ])
    for (const heading of wrapper.findAll('thead th'))
      expect(heading.attributes('scope')).toBe('col')
    expect(wrapper.text()).toContain('可选')
    expect(wrapper.text()).toContain('待处理')
    expect(wrapper.text()).toContain('暂时受阻')

    const progress = wrapper.get('[role="progressbar"]')
    expect(progress.attributes('aria-valuenow')).toBe('75')
    expect(wrapper.text()).toContain('无残余风险')
    expect(wrapper.text()).toContain('探索已饱和')
    expect(wrapper.text()).toContain('存在活跃目标阻断')
    expect(wrapper.text()).toContain('future_risk')
  })

  it('preserves exact named-route deep links without inventing a finding id', async () => {
    mocks.listProjectSurfaceManifest.mockResolvedValue([
      observation('entry-a', 'surface-a', {
        evidence: [{ artifact_id: 'artifact-evidence', pointer: '$.pointer' }],
      }),
    ])
    mocks.listProjectCoverageCells.mockResolvedValue([
      cell({ result: 'finding', blocker_cooldown_until: null }),
    ])
    const { router, wrapper } = await mountPage(
      'project-a',
      detail('project-a', { status: 'completed' }),
    )
    await flushPromises()

    const clickLink = async (text: string) => {
      const link = wrapper.findAll('a').find((candidate) => candidate.text().trim() === text)
      if (!link) throw new Error(`找不到链接：${text}`)
      await link.trigger('click')
      await flushPromises()
    }

    await clickLink('Fact')
    expect(router.currentRoute.value).toMatchObject({
      name: 'WorkspaceGraph',
      params: { id: 'project-a' },
      query: { nodeId: 'fact-a' },
    })
    await router.push({ name: 'WorkspaceCoverage', params: { id: 'project-a' } })

    await clickLink('Run')
    expect(router.currentRoute.value).toMatchObject({
      name: 'WorkspaceRuns',
      params: { id: 'project-a' },
      query: { runId: 'run-a' },
    })
    await router.push({ name: 'WorkspaceCoverage', params: { id: 'project-a' } })

    await clickLink('漏洞')
    expect(router.currentRoute.value).toMatchObject({
      name: 'WorkspaceVulnerabilities',
      params: { id: 'project-a' },
      query: {},
    })
    await router.push({ name: 'WorkspaceCoverage', params: { id: 'project-a' } })

    await clickLink('artifact-evidence$.pointer')
    expect(router.currentRoute.value).toMatchObject({
      name: 'WorkspaceArtifacts',
      params: { id: 'project-a' },
      query: { artifactId: 'artifact-evidence' },
    })
    await router.push({ name: 'WorkspaceCoverage', params: { id: 'project-a' } })
  })

  it('keeps partial successes visible and preserves stale data after a refresh error', async () => {
    mocks.listProjectSurfaceManifest.mockRejectedValueOnce(new Error('surface unavailable'))
    mocks.listProjectCoverageCells.mockResolvedValueOnce([
      cell({ slot_key: 'cell-kept', description: 'cell-kept' }),
    ])

    const first = await mountPage()
    await flushPromises()

    expect(first.wrapper.text()).toContain('Surface Manifest 加载失败')
    expect(first.wrapper.text()).toContain('surface unavailable')
    expect(first.wrapper.text()).toContain('cell-kept')

    first.wrapper.unmount()
    first.queryClient.clear()
    mocks.listProjectSurfaceManifest.mockReset()
    mocks.listProjectSurfaceManifest
      .mockResolvedValueOnce([observation('entry-kept', 'surface-kept')])
      .mockRejectedValueOnce(new Error('background surface failure'))

    const second = await mountPage()
    await flushPromises()
    expect(second.wrapper.text()).toContain('entry-kept')

    await second.queryClient.refetchQueries({
      queryKey: ['workspaceCoverageSurfaceManifest', 'project-a'],
    })
    await flushPromises()

    expect(second.wrapper.text()).toContain('Surface Manifest 刷新失败')
    expect(second.wrapper.text()).toContain('background surface failure')
    expect(second.wrapper.text()).toContain('entry-kept')
  })

  it('isolates A to B to A visits and forwards cancellation signals', async () => {
    const firstA = deferred<SurfaceManifestEntry[]>()
    const projectB = deferred<SurfaceManifestEntry[]>()
    const secondA = deferred<SurfaceManifestEntry[]>()
    const signals: AbortSignal[] = []
    let requestIndex = 0
    mocks.listProjectSurfaceManifest.mockImplementation(
      (_projectId: string, signal: AbortSignal) => {
        signals.push(signal)
        requestIndex += 1
        return requestIndex === 1
          ? firstA.promise
          : requestIndex === 2
            ? projectB.promise
            : secondA.promise
      },
    )

    const { router, workspaceStore, wrapper } = await mountPage()
    await flushPromises()

    await router.push({ name: 'WorkspaceCoverage', params: { id: 'project-b' } })
    workspaceStore.currentProjectId = 'project-b'
    workspaceStore.projectDetail = detail('project-b')
    await flushPromises()
    expect(signals[0]?.aborted).toBe(true)

    await router.push({ name: 'WorkspaceCoverage', params: { id: 'project-a' } })
    workspaceStore.currentProjectId = 'project-a'
    workspaceStore.projectDetail = detail('project-a')
    await flushPromises()
    expect(signals[1]?.aborted).toBe(true)

    secondA.resolve([observation('entry-current-a', 'surface-current-a')])
    await flushPromises()
    expect(wrapper.text()).toContain('entry-current-a')

    firstA.resolve([observation('entry-stale-a', 'surface-stale-a')])
    projectB.resolve([observation('entry-stale-b', 'surface-stale-b')])
    await flushPromises()
    expect(wrapper.text()).toContain('entry-current-a')
    expect(wrapper.text()).not.toContain('entry-stale-a')
    expect(wrapper.text()).not.toContain('entry-stale-b')
  })

  it('keeps missing quality data unknown while rendering authoritative resources', async () => {
    mocks.listProjectSurfaceManifest.mockResolvedValue([
      observation('entry-without-quality', 'surface-without-quality'),
    ])
    mocks.listProjectCoverageCells.mockResolvedValue([
      cell({ description: 'cell-without-quality' }),
    ])

    const { wrapper } = await mountPage(
      'project-a',
      detail('project-a', { assessment_quality: null }),
    )
    await flushPromises()

    expect(wrapper.text()).toContain('质量快照尚未生成')
    expect(wrapper.text()).toContain('快照不可用')
    expect(wrapper.text()).toContain('等待权威质量快照')
    expect(wrapper.text()).toContain('必测证据覆盖未知')
    expect(wrapper.text()).toContain('质量快照尚未生成，残余风险未知')
    expect(wrapper.text()).toContain('entry-without-quality')
    expect(wrapper.text()).toContain('cell-without-quality')
    expect(wrapper.text()).not.toContain('存在残余风险')
    expect(wrapper.text()).not.toContain('暂无已登记残余风险')
    expect(wrapper.find('[role="progressbar"]').exists()).toBe(false)
  })

  it('keeps only Coverage resource polling in this page', async () => {
    const { queryClient } = await mountPage()
    await flushPromises()

    const cachedQueries = queryClient.getQueryCache().getAll()
    const surfaceQuery = cachedQueries.find(
      (query) => query.queryKey[0] === 'workspaceCoverageSurfaceManifest',
    )
    const cellQuery = cachedQueries.find((query) => query.queryKey[0] === 'workspaceCoverageCells')
    const reportQuery = cachedQueries.find(
      (query) => query.queryKey[0] === 'workspaceReportFinalization',
    )

    expect(surfaceQuery).toBeDefined()
    expect(cellQuery).toBeDefined()
    if (
      !surfaceQuery ||
      !cellQuery ||
      !('refetchInterval' in surfaceQuery.options) ||
      !('refetchInterval' in cellQuery.options)
    )
      throw new Error('Coverage polling 配置无效')
    expect(surfaceQuery.options.refetchInterval).toBe(10000)
    expect(cellQuery.options.refetchInterval).toBe(10000)
    expect(reportQuery).toBeUndefined()
  })
})
