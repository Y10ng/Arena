import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { QueryClient, VueQueryPlugin } from '@tanstack/vue-query'
import { reactive, nextTick } from 'vue'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { ProjectDetail, ProjectRuntime, ProjectSummary } from '@/api/types'
import ProjectWorkspace from './ProjectWorkspace.vue'
import RunsPage from '@/features/runs/RunsPage.vue'
import Projects from './Projects.vue'
import { runtimeFixture } from './projectRuntimeFixtures'
import { projectRuntimeKey } from './projectRuntime'

const mocks = vi.hoisted(() => ({
  api: vi.fn(),
  workspace: null as unknown as {
    currentProjectId: string
    projectDetail: ProjectDetail
    isLoading: boolean
    error: string
    loadProject: ReturnType<typeof vi.fn>
  },
  ui: { globalRefreshKey: 0, showToast: vi.fn() },
  route: { query: {} },
}))
vi.mock('@/api/client', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/client')>()),
  api: mocks.api,
  listVulnerabilities: vi.fn().mockResolvedValue([]),
}))
vi.mock('@/stores/workspace', () => ({ useWorkspaceStore: () => mocks.workspace }))
vi.mock('@/stores/ui', () => ({ useUIStore: () => mocks.ui }))
vi.mock('@/stores/auth', () => ({ useAuthStore: () => ({ isSuperAdmin: false }) }))
vi.mock('vue-router', () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
  useRoute: () => mocks.route,
}))

let runtime: ProjectRuntime
let projectList: ProjectSummary[]
let queryClient: QueryClient
let wrapper: VueWrapper | undefined
const detail = (id = 'p1'): ProjectDetail => ({
  project: {
    id,
    title: `项目 ${id}`,
    status: 'active',
    created_at: '2026-09-01T00:00:00Z',
    scenario_key: 'default',
    assessment_protocol: 'web-assessment-v1',
    tenant_workers: [],
    tenant_id: 'tenant1',
    tenant_name: 'Tenant',
    report_capability: {
      supported: false,
      policy_version: 0,
      template_profile: null,
      reason: null,
    },
  },
  facts: [],
  intents: [
    {
      id: 'intent1',
      project_id: id,
      from: [],
      description: '待探索',
      creator: 'test',
      created_at: '2026-09-01T00:00:00Z',
    },
  ],
  hints: [],
  targets: [],
  resources: [],
  library_bindings: [],
})
const button = (text: string) =>
  [...document.body.querySelectorAll<HTMLButtonElement>('button')].find(
    (item) => item.textContent?.trim() === text,
  )!
const settle = async () => {
  await flushPromises()
  await nextTick()
  await flushPromises()
}
const mountWorkspace = async () => {
  wrapper = mount(ProjectWorkspace, {
    attachTo: document.body,
    global: {
      plugins: [[VueQueryPlugin, { queryClient }]],
      stubs: { RouterLink: { template: '<a><slot /></a>' }, RouterView: RunsPage },
    },
  })
  await settle()
  return wrapper
}

const summary = (id: string, status: ProjectSummary['status'] = 'active'): ProjectSummary => ({
  ...detail(id).project,
  status,
  fact_count: 0,
  intent_count: 1,
  working_intent_count: 0,
  unclaimed_intent_count: 1,
  hint_count: 0,
})
const mountProjects = async () => {
  wrapper = mount(Projects, {
    attachTo: document.body,
    global: {
      plugins: [[VueQueryPlugin, { queryClient }]],
      mocks: { $router: { push: vi.fn() } },
      stubs: { RouterLink: { template: '<a><slot /></a>' } },
    },
  })
  await settle()
  return wrapper
}

beforeEach(() => {
  vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval', 'Date'] })
  runtime = runtimeFixture()
  projectList = []
  queryClient = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: Infinity } } })
  mocks.workspace = reactive({
    currentProjectId: 'p1',
    projectDetail: detail(),
    isLoading: false,
    error: '',
    loadProject: vi.fn().mockResolvedValue(undefined),
  })
  mocks.api.mockImplementation(async (method: string, path: string) => {
    if (path === '/projects') return projectList
    if (path.startsWith('/projects/runtime?'))
      return { items: projectList.map((project) => runtimeFixture({ project_id: project.id })) }
    if (path.includes('/runtime')) return runtime
    if (method === 'POST') return mocks.workspace.projectDetail.project
    return []
  })
})
afterEach(() => {
  wrapper?.unmount()
  wrapper = undefined
  queryClient.clear()
  vi.useRealTimers()
  vi.clearAllMocks()
  document.body.innerHTML = ''
})

describe('project runtime status and lifecycle with real queries and components', () => {
  it('shows metadata status without runtime reasons or another diagnostic entry', async () => {
    const view = await mountWorkspace()
    expect(view.findAll('[aria-label="项目状态"]')).toHaveLength(1)
    expect(view.find('[aria-label="项目状态"]').text()).toBe('进行中')
    expect(view.find('.workspace-header').text()).not.toContain(runtime.message)
    expect(view.text()).not.toContain('运行诊断')
    expect(
      mocks.api.mock.calls.filter(
        (call) => call[1] === '/projects/p1/runtime?schema_version=2',
      ),
    ).toHaveLength(1)
    expect(mocks.api).toHaveBeenCalledWith('GET', '/projects/p1/task-runs')
  })

  it('keeps the metadata label and pause entry when a runtime query fails', async () => {
    const view = await mountWorkspace()
    mocks.api.mockImplementation(async (_method: string, path: string) => {
      if (path.includes('/runtime')) throw new Error('secret upstream exception')
      return []
    })
    await queryClient.invalidateQueries({ queryKey: projectRuntimeKey('p1') })
    await settle()
    expect(view.find('[aria-label="项目状态"]').text()).toBe(
      mocks.workspace.projectDetail.project.status === 'paused' ? '已暂停' : '进行中',
    )
    expect(view.find('.workspace-header').text()).not.toContain('状态暂未更新')
    expect(view.text()).not.toContain('secret upstream exception')
    expect(button('暂停项目').disabled).toBe(false)
  })

  it('shows paused metadata without adding runtime draining reasons to the title', async () => {
    const view = await mountWorkspace()
    mocks.api.mockImplementation(async (method: string, path: string) => {
      if (method === 'POST') {
        runtime = runtimeFixture({
          project_status: 'paused',
          state: 'pausing',
          code: 'project_pause_draining',
          message: '正在暂停，等待旧执行停止确认。',
          actions: {
            pause: { allowed: false, reason: null },
            resume: { allowed: false, reason: 'project_pause_draining' },
            reauthorize: { allowed: false, reason: 'project_pause_draining' },
            reopen: { allowed: false, reason: 'project_status_invalid' },
            finish: { allowed: true, reason: null },
          },
        })
        mocks.workspace.projectDetail.project.status = 'paused'
        return mocks.workspace.projectDetail.project
      }
      if (path.includes('/runtime')) return runtime
      return []
    })
    button('暂停项目').click()
    await settle()
    expect(mocks.api).toHaveBeenCalledWith('POST', '/projects/p1/pause')
    expect(view.find('[aria-label="项目状态"]').text()).toBe('已暂停')
    expect(view.find('.workspace-header').text()).not.toContain('正在暂停')
    expect(button('继续项目').disabled).toBe(false)
    expect(view.find('.workspace-status-reason').exists()).toBe(false)
    expect(
      mocks.api.mock.calls.some((call) => call[0] === 'PUT' && String(call[1]).includes('/status')),
    ).toBe(false)

    runtime = runtimeFixture({
      project_status: 'paused',
      state: 'paused',
      code: 'project_paused',
      actions: {
        pause: { allowed: false, reason: 'project_not_active' },
        resume: { allowed: true, reason: null },
        reauthorize: { allowed: false, reason: 'project_authorization_current' },
        reopen: { allowed: false, reason: 'project_status_invalid' },
        finish: { allowed: true, reason: null },
      },
    })
    await queryClient.invalidateQueries({ queryKey: projectRuntimeKey('p1') })
    await settle()
    expect(view.find('.workspace-header').text()).toContain('已暂停')
    expect(button('继续项目').disabled).toBe(false)
  })

  it('uses POST resume only for safely paused projects', async () => {
    mocks.workspace.projectDetail.project.status = 'paused'
    runtime = runtimeFixture({
      project_status: 'paused',
      state: 'paused',
      actions: {
        pause: { allowed: false, reason: null },
        resume: { allowed: true, reason: null },
        reauthorize: { allowed: false, reason: 'project_authorization_current' },
        reopen: { allowed: false, reason: 'project_status_invalid' },
        finish: { allowed: true, reason: null },
      },
    })
    await mountWorkspace()
    button('继续项目').click()
    await settle()
    expect(mocks.api).toHaveBeenCalledWith('POST', '/projects/p1/resume')
  })

  it('reaches the action API despite failed state queries and shows the server refusal', async () => {
    mocks.workspace.projectDetail.project.status = 'paused'
    mocks.api.mockImplementation(async (method: string, path: string) => {
      if (path.includes('/runtime')) throw new Error('runtime unavailable')
      if (method === 'POST' && path.endsWith('/resume'))
        throw { status: 409, data: { detail: 'project_pause_draining' } }
      return []
    })
    const view = await mountWorkspace()
    expect(button('继续项目').disabled).toBe(false)
    button('继续项目').click()
    await settle()
    expect(mocks.api).toHaveBeenCalledWith('POST', '/projects/p1/resume')
    expect(view.text()).toContain('项目操作失败')
    expect(view.text()).toContain('旧执行仍在取消或清理中')
    expect(view.find('[aria-label="项目状态"]').text()).toBe(
      mocks.workspace.projectDetail.project.status === 'paused' ? '已暂停' : '进行中',
    )
    expect(view.find('.workspace-header').text()).not.toContain('状态暂未更新')
  })

  it('requires explicit scope confirmation and does not resume after reauthorization', async () => {
    const scopeDigest = `authorization-scope:${'a'.repeat(64)}`
    Object.assign(mocks.workspace.projectDetail.project, {
      status: 'paused',
      authorization_epoch: 4,
      authorization_scope_digest: scopeDigest,
      authorization_window_seconds: 7200,
      explore_attempts_started: 3,
      explore_deadline: '2026-09-18T02:00:00Z',
    })
    runtime = runtimeFixture({
      project_status: 'paused',
      state: 'paused',
      actions: {
        pause: { allowed: false, reason: 'project_not_active' },
        resume: { allowed: false, reason: 'project_reauthorization_required' },
        reauthorize: { allowed: true, reason: null },
        reopen: { allowed: false, reason: 'project_status_invalid' },
        finish: { allowed: true, reason: null },
      },
    })
    await mountWorkspace()

    button('重新确认授权').click()
    await settle()
    expect(document.body.querySelector('[role="alertdialog"]')?.textContent).toContain(scopeDigest)
    const summary = document.body.querySelector('.authorization-summary')!
    expect(summary.textContent).toContain('累计 Explore 次数')
    expect(summary.textContent).toContain('执行停止时间')
    expect([...summary.querySelectorAll('dd')].map((item) => item.textContent?.trim())).toEqual(
      expect.arrayContaining(['3', '2026-09-18T02:00:00Z']),
    )
    expect(document.querySelector('.workspace-meta')?.textContent).toContain(
      '执行停止时间：2026-09-18T02:00:00Z',
    )
    expect(summary.textContent).not.toContain('预算')
    expect(button('确认重新授权').disabled).toBe(true)
    expect(mocks.api.mock.calls.some((call) => call[0] === 'POST')).toBe(false)

    document.body
      .querySelector<HTMLInputElement>('input[aria-label="确认当前项目授权范围"]')!
      .click()
    document.body
      .querySelector<HTMLInputElement>('input[aria-label="授权无限期有效"]')!
      .click()
    await settle()
    button('确认重新授权').click()
    await settle()

    expect(mocks.api).toHaveBeenCalledWith('POST', '/projects/p1/reauthorize', {
      confirmed: true,
      expected_authorization_epoch: 4,
      expected_scope_digest: scopeDigest,
      authorization_expires_at: null,
    })
    expect(
      mocks.api.mock.calls.some(
        (call) => call[0] === 'POST' && call[1] === '/projects/p1/resume',
      ),
    ).toBe(false)
    expect(mocks.ui.showToast).toHaveBeenCalledWith(
      '授权已更新，项目仍处于暂停状态',
      'success',
    )
  })

  it('rejects a reauthorization confirmation after the visible scope changes', async () => {
    const scopeDigest = `authorization-scope:${'b'.repeat(64)}`
    Object.assign(mocks.workspace.projectDetail.project, {
      status: 'paused',
      authorization_epoch: 2,
      authorization_scope_digest: scopeDigest,
    })
    runtime = runtimeFixture({
      project_status: 'paused',
      state: 'paused',
      actions: {
        pause: { allowed: false, reason: 'project_not_active' },
        resume: { allowed: false, reason: 'project_reauthorization_required' },
        reauthorize: { allowed: true, reason: null },
        reopen: { allowed: false, reason: 'project_status_invalid' },
        finish: { allowed: true, reason: null },
      },
    })
    const view = await mountWorkspace()
    button('重新确认授权').click()
    await settle()
    mocks.workspace.projectDetail.project.authorization_epoch = 3
    document.body
      .querySelector<HTMLInputElement>('input[aria-label="确认当前项目授权范围"]')!
      .click()
    document.body
      .querySelector<HTMLInputElement>('input[aria-label="授权无限期有效"]')!
      .click()
    await settle()
    button('确认重新授权').click()
    await settle()

    expect(mocks.api.mock.calls.some((call) => call[0] === 'POST')).toBe(false)
    expect(view.text()).toContain('项目授权版本或范围已变化')
  })

  it('offers reopen only when the authoritative runtime action allows it', async () => {
    mocks.workspace.projectDetail.project.status = 'completed'
    runtime = runtimeFixture({
      project_status: 'completed',
      state: 'completed',
      actions: {
        pause: { allowed: false, reason: 'Completed projects cannot change status' },
        resume: { allowed: false, reason: 'Completed projects cannot change status' },
        reauthorize: { allowed: false, reason: 'Completed projects cannot change status' },
        reopen: { allowed: false, reason: 'assessment_new_project_required' },
        finish: { allowed: false, reason: 'Completed projects cannot change status' },
      },
    })
    await mountWorkspace()
    expect(button('重新开启 (Reopen)')).toBeUndefined()
  })

  it('submits reopen only after the server-authorized gate remains open', async () => {
    mocks.workspace.projectDetail.project.status = 'completed'
    runtime = runtimeFixture({
      project_status: 'completed',
      state: 'completed',
      actions: {
        pause: { allowed: false, reason: 'Completed projects cannot change status' },
        resume: { allowed: false, reason: 'Completed projects cannot change status' },
        reauthorize: { allowed: false, reason: 'Completed projects cannot change status' },
        reopen: { allowed: true, reason: null },
        finish: { allowed: false, reason: 'Completed projects cannot change status' },
      },
    })
    await mountWorkspace()
    button('重新开启 (Reopen)').click()
    await settle()
    const feedback = document.body.querySelector<HTMLTextAreaElement>('#workspace-reopen-feedback')!
    feedback.value = '新增人工确认事实'
    feedback.dispatchEvent(new Event('input', { bubbles: true }))
    await settle()
    button('重开项目').click()
    await settle()

    expect(mocks.api).toHaveBeenCalledWith('POST', '/projects/p1/reopen', {
      description: '新增人工确认事实',
    })
  })

  it('confirms finish before POST and retains a Chinese error beyond the Toast lifetime', async () => {
    const view = await mountWorkspace()
    mocks.api.mockImplementation(async (method: string, path: string) => {
      if (method === 'POST') throw new Error('project_pause_draining')
      if (path.includes('/runtime')) return runtime
      return []
    })
    button('结束并生成报告').click()
    await settle()
    expect(document.body.querySelector('[role="alertdialog"]')?.textContent).toContain(
      '结束后不能恢复',
    )
    expect(mocks.api.mock.calls.some((call) => call[0] === 'POST')).toBe(false)
    button('确认结束并等待报告').click()
    await settle()
    expect(mocks.api).toHaveBeenCalledWith('POST', '/projects/p1/finish')
    await vi.advanceTimersByTimeAsync(4000)
    expect(view.text()).toContain('项目操作失败')
    expect(view.text()).toContain('关联码：project_pause_draining')
    expect(view.text()).toContain('等待停止确认')
  })

  it('keeps newly refreshed states current between clock ticks', async () => {
    const view = await mountWorkspace()
    await vi.advanceTimersByTimeAsync(6000)
    queryClient.setQueryData(projectRuntimeKey('p1'), runtimeFixture())
    await settle()
    expect(view.text()).not.toContain('状态暂未更新')
    expect(button('暂停项目').disabled).toBe(false)
  })

  it('keeps metadata status without crashing on missing runtime structures', async () => {
    runtime = runtimeFixture({
      actions: undefined as never,
      active_runs: undefined as never,
      dispatcher: undefined as never,
    })
    const view = await mountWorkspace()
    expect(view.find('[aria-label="项目状态"]').text()).toBe(
      mocks.workspace.projectDetail.project.status === 'paused' ? '已暂停' : '进行中',
    )
    expect(view.find('.workspace-header').text()).not.toContain('状态暂未更新')
    expect(button('暂停项目').disabled).toBe(false)
  })

  it('never offers resume for historical stopped projects', async () => {
    mocks.workspace.projectDetail.project.status = 'stopped'
    runtime = runtimeFixture({
      project_status: 'stopped',
      state: 'stopped',
      message: '评估已结束，不能恢复。',
    })
    await mountWorkspace()
    expect(button('继续项目')).toBeUndefined()
    expect(document.body.textContent).toContain('已结束')
  })

  it('does not inject Dispatcher warnings into the project title or run list', async () => {
    runtime = runtimeFixture({
      dispatcher: { status: 'unknown', freshness: 'stale', updated_at: null },
    })
    const view = await mountWorkspace()
    expect(view.find('[aria-label="项目状态"]').text()).toBe('进行中')
    expect(view.find('.workspace-header').text()).not.toContain('配置阻塞')
    expect(view.find('.workspace-header').text()).not.toContain('Dispatcher 连接未及时更新')
    expect(view.find('.runs-sidebar').text()).toContain('没有运行记录')
    expect(view.find('.runs-sidebar').text()).not.toContain('Dispatcher 连接未及时更新')
  })

  it('shows project metadata even when the first runtime query fails', async () => {
    mocks.api.mockImplementation(async (_method: string, path: string) => {
      if (path.includes('/runtime')) throw new Error('upstream secret')
      return []
    })
    const view = await mountWorkspace()
    expect(view.find('[aria-label="项目状态"]').text()).toBe('进行中')
    expect(view.find('.workspace-header').text()).not.toContain('状态暂未更新')
    expect(view.text()).not.toContain('upstream secret')
    expect(button('暂停项目').disabled).toBe(false)
  })

  it('does not replace project metadata when runtime expires after sixty seconds', async () => {
    const view = await mountWorkspace()
    mocks.api.mockImplementation((_method: string, path: string) =>
      path.includes('/runtime') ? new Promise(() => {}) : Promise.resolve([]),
    )
    await vi.advanceTimersByTimeAsync(64000)
    await settle()
    expect(view.find('[aria-label="项目状态"]').text()).toBe(
      mocks.workspace.projectDetail.project.status === 'paused' ? '已暂停' : '进行中',
    )
    expect(view.find('.workspace-header').text()).not.toContain('状态暂未更新')
    expect(button('暂停项目').disabled).toBe(false)
  })

  it('posts pause once from active metadata when diagnostics fail, without inventing a paused state', async () => {
    let resolvePause!: (value: unknown) => void
    mocks.api.mockImplementation((method: string, path: string) => {
      if (method === 'POST')
        return new Promise((resolve) => {
          resolvePause = resolve
        })
      if (path.includes('/runtime')) return Promise.reject(new Error('runtime_unavailable'))
      return Promise.resolve([])
    })
    const view = await mountWorkspace()
    button('暂停项目').click()
    button('暂停项目').click()
    await settle()
    expect(mocks.api.mock.calls.filter((call) => call[0] === 'POST')).toEqual([
      ['POST', '/projects/p1/pause'],
    ])
    expect(view.text()).not.toContain('已暂停')
    expect(button('暂停项目').disabled).toBe(true)
    resolvePause({ ...detail().project, status: 'paused' })
    await settle()
    expect(mocks.workspace.loadProject).toHaveBeenCalledWith('p1')
    expect(view.text()).not.toContain('已暂停')
  })

  it('ignores lifecycle errors arriving after a project switch', async () => {
    let rejectAction!: (reason: Error) => void
    await mountWorkspace()
    mocks.api.mockImplementation((method: string, path: string) => {
      if (method === 'POST')
        return new Promise((_resolve, reject) => {
          rejectAction = reject
        })
      if (path.includes('/runtime')) return Promise.resolve(runtimeFixture({ project_id: 'p2' }))
      return Promise.resolve([])
    })
    button('暂停项目').click()
    await settle()
    mocks.workspace.currentProjectId = 'p2'
    mocks.workspace.projectDetail = detail('p2')
    await settle()
    rejectAction(new Error('project_pause_draining'))
    await settle()
    expect(document.body.textContent).not.toContain('关联码：project_pause_draining')
    expect(mocks.workspace.loadProject).not.toHaveBeenCalledWith('p1')
  })

  it('polls diagnostics and stops all intervals on unmount', async () => {
    await mountWorkspace()
    await vi.advanceTimersByTimeAsync(8000)
    await settle()
    expect(
      mocks.api.mock.calls.filter((call) => call[1] === '/projects/p1/runtime?schema_version=2').length,
    ).toBeGreaterThan(1)
    wrapper!.unmount()
    wrapper = undefined
    const count = mocks.api.mock.calls.length
    await vi.advanceTimersByTimeAsync(24000)
    expect(mocks.api.mock.calls).toHaveLength(count)
    expect(vi.getTimerCount()).toBe(0)
  })

  it('aborts obsolete queries on project switches and ignores their late results', async () => {
    let finishOld!: (value: ProjectRuntime) => void
    let oldSignal: AbortSignal | undefined
    mocks.api.mockImplementation(
      (_method: string, path: string, _body: unknown, signal?: AbortSignal) => {
        if (path === '/projects/p1/runtime?schema_version=2') {
          oldSignal = signal
          return new Promise((resolve) => {
            finishOld = resolve
          })
        }
        if (path === '/projects/p2/runtime?schema_version=2')
          return Promise.resolve(
            runtimeFixture({ project_id: 'p2', message: '第二项目缺少模型配置。' }),
          )
        return Promise.resolve([])
      },
    )
    const view = await mountWorkspace()
    mocks.workspace.currentProjectId = 'p2'
    mocks.workspace.projectDetail = detail('p2')
    await settle()
    expect(oldSignal?.aborted).toBe(true)
    finishOld(runtimeFixture({ message: '不应显示的旧项目结果' }))
    await settle()
    expect(view.find('[aria-label="项目状态"]').text()).toBe('进行中')
    expect(view.find('.workspace-header').text()).toContain('项目 p2')
    expect(view.text()).not.toContain('第二项目缺少模型配置')
    expect(view.text()).not.toContain('不应显示的旧项目结果')
  })

  it('project list maps pending and active metadata without waiting for runtime', async () => {
    projectList = [summary('p1', 'pending'), summary('p2')]
    let finishBatch!: (value: { items: ProjectRuntime[] }) => void
    mocks.api.mockImplementation(async (_method: string, path: string) => {
      if (path === '/projects') return projectList
      if (path.startsWith('/projects/runtime?'))
        return new Promise((resolve) => { finishBatch = resolve })
      return []
    })
    const view = await mountProjects()
    expect(view.findAll('.project-row-status').map((row) => row.text())).toEqual(['待启动', '进行中'])
    expect(view.text()).not.toContain('正在获取状态')
    expect(view.text()).not.toContain('状态查询失败')
    expect(view.text()).not.toContain('状态暂未更新')
    finishBatch({ items: [
      runtimeFixture({ project_id: 'p1', project_status: 'pending', state: 'pending',
        code: 'project_pending_schedule_required', message: '尚未进入执行调度，请查看启动计划。',
        dispatcher: { status: 'unknown', freshness: 'unknown', updated_at: null } }),
      runtimeFixture({ project_id: 'p2' }),
    ] })
    await settle()
    expect(view.findAll('.project-row-status')[0]!.text()).toContain('待启动')
    expect(view.findAll('.project-row-status')[0]!.text()).not.toContain('Dispatcher')
    expect(view.findAll('.project-row-status')[1]!.text()).toBe('进行中')
    await vi.advanceTimersByTimeAsync(8000)
    await settle()
    expect(view.text()).toContain('2 个项目')
    expect(view.text()).not.toContain('正在获取最新项目数据')
    expect(view.text()).not.toContain('实时刷新')
    expect(view.find('.refresh-indicator').exists()).toBe(false)
    expect(mocks.api.mock.calls.filter((call) => call[1] === '/projects').length).toBeGreaterThan(1)
    expect(mocks.api.mock.calls.filter((call) => String(call[1]).startsWith('/projects/runtime?')).length).toBeGreaterThan(1)
  })

  it('keeps runtime failure out of list status while retaining metadata failure and retry', async () => {
    projectList = [summary('p1')]
    const view = await mountProjects()
    let failMetadata = false
    let failRuntime = true
    mocks.api.mockImplementation(async (_method: string, path: string) => {
      if (path === '/projects') {
        if (failMetadata) throw new Error('metadata unavailable')
        return projectList
      }
      if (path.startsWith('/projects/runtime?')) {
        if (failRuntime) throw new Error('private upstream failure')
        return { items: [runtimeFixture({ message: '配置已核对，等待 Worker 容量。', state: 'waiting' })] }
      }
      return []
    })
    await queryClient.invalidateQueries({ queryKey: ['project-runtime-batch'] })
    await settle()
    expect(view.find('.project-row-status').text()).toBe('进行中')
    expect(view.text()).not.toContain('运行状态未更新')
    expect(view.text()).not.toContain('上次状态')
    expect(view.text()).not.toContain('状态查询失败')
    expect(view.text()).not.toContain('private upstream failure')
    expect(view.find('.projects-stale-alert').exists()).toBe(false)

    failMetadata = true
    await queryClient.invalidateQueries({ queryKey: ['projects'] })
    await settle()
    expect(view.find('.projects-stale-alert').text()).toContain('项目刷新失败')
    expect(view.text()).toContain('项目列表仍显示最近一次成功获取的数据')
    const beforeMetadata = mocks.api.mock.calls.filter((call) => call[1] === '/projects').length
    const beforeRuntime = mocks.api.mock.calls.filter((call) => String(call[1]).startsWith('/projects/runtime?')).length
    failMetadata = false
    failRuntime = false
    button('重试刷新').click()
    await settle()
    expect(mocks.api.mock.calls.filter((call) => call[1] === '/projects')).toHaveLength(beforeMetadata + 1)
    expect(mocks.api.mock.calls.filter((call) => String(call[1]).startsWith('/projects/runtime?'))).toHaveLength(beforeRuntime + 1)
    expect(view.find('.project-row-status').text()).toBe('进行中')
    expect(view.find('.projects-stale-alert').exists()).toBe(false)
    expect(view.text()).not.toContain('配置已核对，等待 Worker 容量。')
  })

  it('project list metadata retry joins the runtime query for the refreshed ID set', async () => {
    projectList = [summary('p1')]
    const view = await mountProjects()
    mocks.api.mockImplementation(async (_method: string, path: string) => {
      if (path === '/projects') throw new Error('metadata unavailable')
      if (path.startsWith('/projects/runtime?')) throw new Error('batch unavailable')
      return []
    })
    await queryClient.invalidateQueries({ queryKey: ['projects'] })
    await settle()
    projectList = [summary('p1'), summary('p2', 'pending')]
    mocks.api.mockImplementation(async (_method: string, path: string) => {
      if (path === '/projects') return projectList
      if (path.startsWith('/projects/runtime?')) {
        const ids = new URL(path, 'https://example.test').searchParams.get('project_ids')!.split(',')
        return { items: ids.map((project_id) => runtimeFixture({ project_id })) }
      }
      return []
    })
    const before = mocks.api.mock.calls.length
    button('重试刷新').click()
    await settle()
    const batches = mocks.api.mock.calls.slice(before).filter((call) => String(call[1]).startsWith('/projects/runtime?'))
    expect(batches).toHaveLength(1)
    expect(new URL(String(batches[0]![1]), 'https://example.test').searchParams.get('project_ids')).toBe('p1,p2')
    expect(view.findAll('.project-row')).toHaveLength(2)
    expect(view.text()).not.toContain('运行状态未更新')
  })

  it('project list status ignores expired and incompatible runtime snapshots', async () => {
    projectList = [summary('p1')]
    const view = await mountProjects()
    for (const snapshot of [
      runtimeFixture({ observed_at: new Date(Date.now() - 60_001).toISOString() }),
      runtimeFixture({ schema_version: 99 as 1 }),
    ]) {
      queryClient.setQueryData(['project-runtime-batch', ['p1']], { items: [snapshot] })
      await settle()
      expect(view.find('.project-row-status').text()).toBe('进行中')
      expect(view.find('.projects-stale-alert').exists()).toBe(false)
      expect(view.text()).not.toContain('上次状态')
      expect(view.text()).not.toContain('运行快照已过期')
      expect(view.text()).not.toContain('状态不兼容')
    }
  })

  it('project list has no duplicate metadata refetch on the global refresh event', async () => {
    projectList = [summary('p1')]
    mocks.ui = reactive({ globalRefreshKey: 0, showToast: vi.fn() })
    await mountProjects()
    const beforeMetadata = mocks.api.mock.calls.filter((call) => call[1] === '/projects').length
    mocks.ui.globalRefreshKey++
    await queryClient.invalidateQueries()
    await settle()
    expect(mocks.api.mock.calls.filter((call) => call[1] === '/projects')).toHaveLength(beforeMetadata + 1)
  })

  it('requests one authorized batch for a list larger than ten projects', async () => {
    projectList = Array.from({ length: 12 }, (_, index) => ({
      ...detail(`p${index}`).project,
      fact_count: 0,
      intent_count: 1,
      working_intent_count: 0,
      unclaimed_intent_count: 1,
      hint_count: 0,
    }))
    wrapper = mount(Projects, {
      attachTo: document.body,
      global: {
        plugins: [[VueQueryPlugin, { queryClient }]],
        mocks: { $router: { push: vi.fn() } },
      },
    })
    await settle()
    const batches = mocks.api.mock.calls.filter((call) =>
      String(call[1]).startsWith('/projects/runtime?'),
    )
    expect(batches).toHaveLength(1)
    const ids = new URL(String(batches[0]![1]), 'https://example.test').searchParams
      .get('project_ids')!
      .split(',')
    expect(ids).toHaveLength(12)
    expect(
      mocks.api.mock.calls.some((call) => /\/projects\/p\d+\/runtime/.test(String(call[1]))),
    ).toBe(false)
    expect(wrapper.findAll('.project-row')).toHaveLength(12)
    expect(wrapper.findAll('.project-row-status').every((row) => row.text() === '进行中')).toBe(true)
  })
})
