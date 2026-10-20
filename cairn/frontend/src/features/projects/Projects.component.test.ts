import { flushPromises, mount } from '@vue/test-utils'
import { ref, unref } from 'vue'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { VueWrapper } from '@vue/test-utils'
import type { ProjectSummary } from '@/api/types'
import Projects from './Projects.vue'
import { runtimeFixture } from './projectRuntimeFixtures'

const mocks = vi.hoisted(() => ({
  projects: [] as ProjectSummary[] | undefined,
  projectsLoading: false,
  projectsFetching: false,
  projectsError: false,
  runtimeError: false,
  scenarios: [{ key: 'default', label: '默认场景' }],
  api: vi.fn(),
  fetchText: vi.fn(),
  getProjectManagementOptions: vi.fn(),
  getTenantMonitoringSummary: vi.fn(),
  projectsRefetch: vi.fn(),
  getQueryData: vi.fn(),
  setQueryData: vi.fn(),
  invalidateQueries: vi.fn(),
  authStore: { isSuperAdmin: false },
  uiStore: { globalRefreshKey: 0, showToast: vi.fn() },
  router: { push: vi.fn() },
  route: { query: {} as Record<string, unknown> },
  runtimeOverrides: {} as Record<string, Record<string, unknown>>,
}))

vi.mock('@/api/client', () => ({
  api: mocks.api,
  fetchText: mocks.fetchText,
  getProjectManagementOptions: mocks.getProjectManagementOptions,
  getTenantMonitoringSummary: mocks.getTenantMonitoringSummary,
  tenantMonitoringQueryKey: ['tenant-monitoring-summary'],
}))

vi.mock('@/stores/auth', () => ({
  useAuthStore: () => mocks.authStore,
}))

vi.mock('@/stores/ui', () => ({
  useUIStore: () => mocks.uiStore,
}))

vi.mock('vue-router', () => ({
  useRoute: () => mocks.route,
  useRouter: () => mocks.router,
}))

vi.mock('@tanstack/vue-query', () => ({
  useQuery: ({ queryKey }: { queryKey: unknown }) => {
    const resolvedKey = unref(queryKey)
    const key = Array.isArray(resolvedKey) ? resolvedKey : []

    if (key[0] === 'projects') {
      return {
        data: ref(mocks.projects),
        isLoading: ref(mocks.projectsLoading),
        isFetching: ref(mocks.projectsFetching),
        isError: ref(mocks.projectsError),
        refetch: mocks.projectsRefetch,
      }
    }
    if (key[0] === 'project-runtime-batch')
      return {
        data: ref({
          items: (mocks.projects || []).map((project) =>
            runtimeFixture({
              project_id: project.id,
              project_status: project.status,
              state: project.status,
              actions: {
                pause: { allowed: project.status === 'active', reason: null },
                resume: { allowed: project.status === 'paused', reason: null },
                reauthorize: { allowed: false, reason: 'project_status_invalid' },
                reopen: { allowed: project.status === 'completed', reason: null },
                finish: { allowed: ['active', 'paused'].includes(project.status), reason: null },
              },
              ...mocks.runtimeOverrides[project.id],
            }),
          ),
        }),
        isError: ref(mocks.runtimeError),
      }
    if (key[0] === 'scenarios') return { data: ref(mocks.scenarios) }
    return { data: ref(undefined), isError: ref(false) }
  },
  useQueryClient: () => ({
    getQueryData: mocks.getQueryData,
    setQueryData: mocks.setQueryData,
    invalidateQueries: mocks.invalidateQueries,
  }),
}))

const project: ProjectSummary = {
  id: 'project-a',
  title: 'Assessment Workspace',
  status: 'stopped',
  created_at: '2026-08-19T00:00:00Z',
  scenario_key: 'default',
  assessment_protocol: '',
  report_capability: {
    supported: false,
    policy_version: 0,
    template_profile: null,
    reason: 'report_policy_not_enabled',
  },
  coverage_stop_reason: null,
  reason: null,
  tenant_workers: [],
  preferred_workers: [],
  tenant_id: 'tenant-a',
  tenant_name: 'Tenant A',
  tenant_owner_login: 'tenant-owner',
  fact_count: 4,
  intent_count: 3,
  working_intent_count: 1,
  unclaimed_intent_count: 2,
  hint_count: 5,
}

const wrappers: VueWrapper[] = []

function mountPage() {
  const wrapper = mount(Projects, {
    attachTo: document.body,
    global: { mocks: { $router: mocks.router } },
  })
  wrappers.push(wrapper)
  return wrapper
}

function bodyButton(label: string) {
  const button = [...document.body.querySelectorAll<HTMLButtonElement>('button')].find(
    (candidate) => candidate.textContent?.trim() === label,
  )
  if (!button) throw new Error(`找不到按钮：${label}`)
  return button
}

async function openProjectMenu() {
  const trigger = document.body.querySelector<HTMLButtonElement>(
    'button[aria-label="项目操作菜单"]',
  )
  if (!trigger) throw new Error('找不到项目操作菜单')
  trigger.click()
  await flushPromises()
}

async function chooseMenuItem(label: string) {
  const item = [...document.body.querySelectorAll<HTMLElement>('[role="menuitem"]')].find(
    (candidate) => candidate.textContent?.includes(label),
  )
  if (!item) throw new Error(`找不到菜单项：${label}`)
  item.click()
  await flushPromises()
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

beforeEach(() => {
  HTMLElement.prototype.hasPointerCapture = () => false
  HTMLElement.prototype.setPointerCapture = () => undefined
  HTMLElement.prototype.releasePointerCapture = () => undefined

  mocks.projects = [{ ...project }]
  mocks.projectsLoading = false
  mocks.projectsFetching = false
  mocks.projectsError = false
  mocks.runtimeError = false
  mocks.authStore.isSuperAdmin = false
  mocks.uiStore.globalRefreshKey = 0
  mocks.route.query = {}
  mocks.runtimeOverrides = {}

  mocks.api.mockReset().mockResolvedValue(undefined)
  mocks.fetchText.mockReset().mockResolvedValue('title: Assessment Workspace')
  mocks.getProjectManagementOptions.mockReset().mockResolvedValue({
    tenant_workers: [],
  })
  mocks.projectsRefetch.mockReset()
  mocks.getQueryData.mockReset().mockReturnValue(mocks.projects)
  mocks.setQueryData.mockReset()
  mocks.invalidateQueries.mockReset().mockResolvedValue(undefined)
  mocks.router.push.mockReset()
  mocks.uiStore.showToast.mockReset()
})

afterEach(() => {
  wrappers.splice(0).forEach((wrapper) => wrapper.unmount())
  document.body.innerHTML = ''
})

describe('Projects shared control migration', () => {
  it('moves the selected state when a project status filter is clicked', async () => {
    mountPage()
    await flushPromises()

    const segmentButtons = [...document.body.querySelectorAll<HTMLButtonElement>('.segment-btn')]
    const allButton = segmentButtons.find((button) => button.textContent?.includes('全部项目'))
    const stoppedButton = segmentButtons.find((button) => button.textContent?.includes('已结束'))
    if (!allButton || !stoppedButton) throw new Error('找不到项目状态筛选按钮')

    expect(document.body.querySelector('[role="status"]')?.textContent).toContain('1 个项目')
    expect(allButton.classList.contains('active')).toBe(true)
    expect(allButton.getAttribute('aria-pressed')).toBe('true')
    expect(stoppedButton.classList.contains('active')).toBe(false)
    expect(stoppedButton.getAttribute('aria-pressed')).toBe('false')

    stoppedButton.click()
    await flushPromises()

    expect(allButton.classList.contains('active')).toBe(false)
    expect(allButton.getAttribute('aria-pressed')).toBe('false')
    expect(stoppedButton.classList.contains('active')).toBe(true)
    expect(stoppedButton.getAttribute('aria-pressed')).toBe('true')
    expect(document.body.textContent).toContain('Assessment Workspace')
  })

  it('keeps a stale project snapshot visible when a background refresh fails', async () => {
    mocks.projectsError = true
    mocks.projectsFetching = false
    mountPage()
    await flushPromises()

    expect(document.body.textContent).toContain('项目刷新失败')
    expect(document.body.textContent).toContain('Assessment Workspace')
    expect(document.body.textContent).not.toContain('项目数据加载失败')
  })

  it('shows a root error only when no project snapshot exists', async () => {
    mocks.projects = undefined
    mocks.projectsError = true
    mountPage()
    await flushPromises()

    expect(document.body.textContent).toContain('项目数据加载失败')
    expect(document.body.textContent).not.toContain('项目刷新失败')
  })

  it('keeps the project count and keyboard list stable during background fetching', async () => {
    mocks.projectsFetching = true
    mountPage()
    await flushPromises()

    const status = document.body.querySelector<HTMLElement>('[role="status"]')
    expect(status?.textContent).toContain('1 个项目')
    expect(status?.getAttribute('aria-live')).toBe('polite')
    expect(document.body.querySelector('[role="list"]')).not.toBeNull()
  })

  it('renders an accessible project menu without resuming a stopped project', async () => {
    mountPage()
    await openProjectMenu()

    expect(document.body.querySelector('[role="menu"]')).not.toBeNull()
    expect(document.body.textContent).not.toContain('继续项目')
    expect(document.body.textContent).not.toContain('暂停项目')

    expect(mocks.api).not.toHaveBeenCalled()
  })

  it.each([
    ['active', '暂停项目', 'pause'],
    ['paused', '继续项目', 'resume'],
  ] as const)('posts the explicit action for %s projects', async (status, label, action) => {
    mocks.projects = [{ ...project, status }]
    mountPage()
    await openProjectMenu()
    await chooseMenuItem(label)
    expect(mocks.api).toHaveBeenCalledWith('POST', `/projects/project-a/${action}`)
    expect(mocks.setQueryData).not.toHaveBeenCalled()
  })

  it('submits resume despite a failed state query and displays the server rejection', async () => {
    mocks.projects = [{ ...project, status: 'paused' }]
    mocks.runtimeError = true
    mocks.api.mockRejectedValueOnce({ status: 409, data: { detail: 'project_pause_draining' } })
    mountPage()
    await openProjectMenu()
    await chooseMenuItem('继续项目')

    expect(mocks.api).toHaveBeenCalledWith('POST', '/projects/project-a/resume')
    expect(document.body.textContent).toContain('项目操作失败')
    expect(document.body.textContent).toContain('旧执行仍在取消或清理中')
    expect(document.body.querySelector('[aria-label="项目运行诊断"]')).toBeNull()
  })

  it('shows reauthorization only when allowed and submits the confirmed frozen scope', async () => {
    const scopeDigest = `authorization-scope:${'c'.repeat(64)}`
    mocks.projects = [
      {
        ...project,
        status: 'paused',
        authorization_epoch: 7,
        authorization_scope_digest: scopeDigest,
        authorization_window_seconds: 21600,
        explore_attempts_started: 9,
        explore_deadline: '2026-09-18T06:00:00Z',
      },
    ]
    mocks.runtimeOverrides['project-a'] = {
      actions: {
        pause: { allowed: false, reason: 'project_not_active' },
        resume: { allowed: false, reason: 'project_reauthorization_required' },
        reauthorize: { allowed: true, reason: null },
        reopen: { allowed: false, reason: 'project_status_invalid' },
        finish: { allowed: true, reason: null },
      },
    }
    mountPage()
    await openProjectMenu()
    await chooseMenuItem('重新确认授权')

    expect(document.body.querySelector('[role="alertdialog"]')?.textContent).toContain(scopeDigest)
    const summary = document.body.querySelector('.authorization-summary')!
    expect(summary.textContent).toContain('累计 Explore 次数')
    expect(summary.textContent).toContain('执行停止时间')
    expect([...summary.querySelectorAll('dd')].map((item) => item.textContent?.trim())).toEqual(
      expect.arrayContaining(['9', '2026-09-18T06:00:00Z']),
    )
    expect(summary.textContent).not.toContain('预算')
    expect(bodyButton('确认重新授权').disabled).toBe(true)
    expect(mocks.api).not.toHaveBeenCalled()

    document.body
      .querySelector<HTMLInputElement>('input[aria-label="确认当前项目授权范围"]')!
      .click()
    await flushPromises()
    bodyButton('确认重新授权').click()
    await flushPromises()

    expect(mocks.api).toHaveBeenCalledWith('POST', '/projects/project-a/reauthorize', {
      confirmed: true,
      expected_authorization_epoch: 7,
      expected_scope_digest: scopeDigest,
    })
    expect(
      mocks.api.mock.calls.some(
        (call) => call[0] === 'POST' && call[1] === '/projects/project-a/resume',
      ),
    ).toBe(false)
    expect(mocks.uiStore.showToast).toHaveBeenCalledWith(
      '授权已更新，项目仍处于暂停状态',
      'success',
    )
  })

  it('requires finish confirmation and keeps the failure inline', async () => {
    mocks.projects = [{ ...project, status: 'active' }]
    mocks.api.mockRejectedValueOnce(new Error('project_dispatcher_unavailable'))
    mountPage()
    await openProjectMenu()
    await chooseMenuItem('结束评估')
    expect(mocks.api).not.toHaveBeenCalled()
    expect(document.body.querySelector('[role="alertdialog"]')?.textContent).toContain('不能恢复')
    bodyButton('确认结束评估').click()
    await flushPromises()
    expect(mocks.api).toHaveBeenCalledWith('POST', '/projects/project-a/finish')
    expect(document.body.textContent).toContain('项目操作失败')
    expect(document.body.textContent).toContain('关联码：project_dispatcher_unavailable')
  })

  it('trims rename input, blocks duplicate submission, and closes after success', async () => {
    const pending = deferred<void>()
    mocks.api.mockReturnValueOnce(pending.promise)
    mountPage()

    await openProjectMenu()
    await chooseMenuItem('重命名项目')

    const input = document.body.querySelector<HTMLInputElement>('#project-rename-title')
    expect(document.body.querySelector('[role="dialog"]')?.textContent).toContain('重命名项目')
    if (!input) throw new Error('找不到项目名称输入框')
    input.value = '  Renamed Workspace  '
    input.dispatchEvent(new Event('input', { bubbles: true }))
    await flushPromises()

    bodyButton('保存').click()
    bodyButton('保存').click()
    await flushPromises()

    expect(mocks.api).toHaveBeenCalledTimes(1)
    expect(mocks.api).toHaveBeenCalledWith('PUT', '/projects/project-a/title', {
      title: 'Renamed Workspace',
    })
    expect(bodyButton('取消').disabled).toBe(true)

    pending.resolve()
    await flushPromises()

    expect(document.body.querySelector('[role="dialog"]')).toBeNull()
  })

  it('keeps the rename dialog open when saving fails', async () => {
    mocks.api.mockRejectedValueOnce(new Error('rename rejected'))
    mountPage()

    await openProjectMenu()
    await chooseMenuItem('重命名项目')
    bodyButton('保存').click()
    await flushPromises()

    expect(document.body.querySelector('[role="dialog"]')).not.toBeNull()
    expect(mocks.uiStore.showToast).toHaveBeenCalledWith('rename rejected', 'danger')
  })

  it('keeps ordinary projects visible with no group filtering or move action', async () => {
    mocks.route.query = { group: 'retired-group', create: '1' }
    const wrapper = mountPage()
    await flushPromises()
    expect(wrapper.text()).toContain('Assessment Workspace')
    expect(wrapper.text()).not.toContain('Red Team')
    expect(document.body.querySelector('[role="dialog"]')).toBeNull()
    await openProjectMenu()
    expect(document.body.querySelector('[role="menu"]')?.textContent).not.toContain('移动到分组')
    expect(mocks.getProjectManagementOptions).not.toHaveBeenCalled()
  })

  it('keeps delete confirmation open and blocks duplicate deletion on failure', async () => {
    const pending = deferred<void>()
    mocks.api.mockReturnValueOnce(pending.promise)
    mountPage()

    await openProjectMenu()
    await chooseMenuItem('删除项目')

    expect(document.body.querySelector('[role="alertdialog"]')?.textContent).toContain(
      'Assessment Workspace',
    )

    bodyButton('确认删除').click()
    bodyButton('确认删除').click()
    await flushPromises()

    expect(mocks.api).toHaveBeenCalledTimes(1)
    expect(mocks.api).toHaveBeenCalledWith('DELETE', '/projects/project-a')
    expect(bodyButton('取消').disabled).toBe(true)

    pending.reject(new Error('delete rejected'))
    await flushPromises()

    expect(document.body.querySelector('[role="alertdialog"]')).not.toBeNull()
    expect(mocks.uiStore.showToast).toHaveBeenCalledWith('delete rejected', 'danger')
  })
})
