import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { computed, reactive, ref } from 'vue'
import { runtimeFixture, runtimeV2Fixture } from './projectRuntimeFixtures'
import type { ProjectRuntime } from '@/api/types'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const apiMock = vi.fn()
const commitProjectResourceBatchMock = vi.fn()
const createProjectResourceBatchMock = vi.fn()
const uploadProjectResourceBatchFileMock = vi.fn()
const getProjectResourceConfigMock = vi.fn()
const cancelProjectResourceBatchMock = vi.fn()
const deactivateProjectResourceMock = vi.fn()
const fetchTextMock = vi.fn()
const getProjectManagementOptionsMock = vi.fn()
const routerPush = vi.fn()
const showToast = vi.fn()
const runtimeOverride = ref<ProjectRuntime | null>(null)
const runtimeFailure = ref(false)
const runtimeRefetch = vi.fn()
const invalidateQueries = vi.fn()

const resourceConfig = {
  kinds: ['note', 'script', 'file'],
  limits: {
    max_active_count: 200,
    max_file_bytes: 10 * 1024 * 1024,
    max_active_total_bytes: 100 * 1024 * 1024,
    max_zip_upload_bytes: 50 * 1024 * 1024,
    max_zip_entries: 400,
    max_compression_ratio: 200,
    max_open_batches_per_user: 4,
    max_staged_bytes_per_user: 200 * 1024 * 1024,
    max_path_bytes: 1024,
    max_path_segment_bytes: 255,
  },
  reserved_names: ['manifest.json'],
  batch_ttl_seconds: 86400,
  zip_policy: {
    allowed_compression_methods: ['stored', 'deflated'],
    nested_archives: 'opaque',
    preserve_relative_directories: true,
  },
}

const emptyBatch = {
  id: 'batch-1',
  state: 'open',
  created_at: '2026-09-01T00:00:00Z',
  expires_at: '2026-09-02T00:00:00Z',
  resource_count: 0,
  total_bytes: 0,
  members: [],
}

const baseProject = {
  id: 'proj_test',
  title: '测试项目',
  status: 'stopped',
  scenario_key: 'default-copy-v2',
  tenant_name: 'admin',
  tenant_id: 'tenant_test',
  tenant_owner_login: 'admin',
  tenant_workers: [],
  authorization_epoch: 2,
  authorization_scope_digest: `authorization-scope:${'a'.repeat(64)}`,
  authorization_expires_at: null,
  explore_attempts_started: 3,
  explore_deadline: null,
  schedule_id: null as string | null,
  report_capability: {
    supported: true,
    policy_version: 1,
    template_profile: 'default',
    reason: null,
  },
}

const workspaceStore = reactive({
  isLoading: false,
  error: '',
  projectDetail: {
    project: { ...baseProject },
    targets: [],
    hints: [],
    intents: [],
    resources: [],
    library_bindings: [],
  },
  loadProject: vi.fn(),
})

function deferred<T>() {
  let resolve!: (value: T | PromiseLike<T>) => void
  let reject!: (reason?: unknown) => void
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise
    reject = rejectPromise
  })
  return { promise, resolve, reject }
}

function mockWorkspaceDependencies() {
  vi.doMock('vue-router', () => ({
    useRouter: () => ({ push: routerPush }),
    useRoute: () => ({ name: 'WorkspaceGraph', params: { id: 'proj_test' } }),
    onBeforeRouteLeave: vi.fn(),
    onBeforeRouteUpdate: vi.fn(),
  }))
  vi.doMock('@tanstack/vue-query', () => ({
    useQuery: (options: { queryKey: readonly unknown[] | { value: readonly unknown[] } }) => {
      const queryKey = 'value' in options.queryKey ? options.queryKey.value : options.queryKey
      if (queryKey[0] === 'project-runtime') {
        return {
          data: computed(
            () =>
              runtimeOverride.value ||
              runtimeFixture({
                project_id: workspaceStore.projectDetail.project.id,
                project_status: workspaceStore.projectDetail.project
                  .status as ProjectRuntime['project_status'],
                state:
                  workspaceStore.projectDetail.project.status === 'active'
                    ? 'blocked'
                    : workspaceStore.projectDetail.project.status,
                actions: {
                  pause: {
                    allowed: workspaceStore.projectDetail.project.status === 'active',
                    reason: null,
                  },
                  resume: {
                    allowed: workspaceStore.projectDetail.project.status === 'paused',
                    reason: null,
                  },
                  reauthorize: { allowed: false, reason: 'project_status_invalid' },
                  reopen: {
                    allowed: workspaceStore.projectDetail.project.status === 'completed',
                    reason: null,
                  },
                  finish: {
                    allowed: ['active', 'paused'].includes(
                      workspaceStore.projectDetail.project.status,
                    ),
                    reason: null,
                  },
                },
              }),
          ),
          dataUpdatedAt: ref(Date.now()),
          isError: runtimeFailure,
          isFetching: ref(false),
          refetch: runtimeRefetch,
          error: ref(new Error('network failure')),
        }
      }
      return { data: ref(undefined), isError: ref(false) }
    },
    useQueryClient: () => ({ invalidateQueries }),
  }))
  vi.doMock('@/stores/auth', () => ({
    useAuthStore: () => ({ isSuperAdmin: false }),
  }))
  vi.doMock('@/stores/workspace', () => ({
    useWorkspaceStore: () => workspaceStore,
  }))
  vi.doMock('@/stores/ui', () => ({
    useUIStore: () => ({ globalRefreshKey: 0, showToast }),
  }))
  vi.doMock('@/api/client', () => ({
    api: apiMock,
    cancelProjectResourceBatch: cancelProjectResourceBatchMock,
    commitProjectResourceBatch: commitProjectResourceBatchMock,
    createProjectResourceBatch: createProjectResourceBatchMock,
    deactivateProjectResource: deactivateProjectResourceMock,
    deleteProjectResourceBatchMember: vi.fn(),
    fetchText: fetchTextMock,
    getAssessmentReportFinalization: vi.fn(),
    getProjectManagementOptions: getProjectManagementOptionsMock,
    getProjectMonitoringSummary: vi.fn(),
    getProjectResourceConfig: getProjectResourceConfigMock,
    importProjectResourceBatchZip: vi.fn(),
    listVulnerabilities: vi.fn(),
    updateProjectResourceBatchMember: vi.fn(),
    uploadProjectResourceBatchFile: uploadProjectResourceBatchFileMock,
  }))
}

async function mountWorkspace(status = 'stopped'): Promise<VueWrapper> {
  workspaceStore.projectDetail.project = { ...baseProject, status }
  mockWorkspaceDependencies()
  const { default: ProjectWorkspace } = await import('./ProjectWorkspace.vue')
  const wrapper = mount(ProjectWorkspace, {
    attachTo: document.body,
    global: {
      stubs: {
        RouterLink: { template: '<a><slot /></a>' },
        RouterView: true,
        ProjectReportDialog: {
          props: ['open', 'projectId', 'projectStatus', 'capability'],
          emits: ['update:open'],
          template:
            '<section v-if="open" data-testid="project-report-dialog">报告流程 {{ projectId }} {{ projectStatus }}</section>',
        },
      },
    },
  })
  await flushPromises()
  return wrapper
}

function buttonByText(text: string) {
  return Array.from(document.body.querySelectorAll<HTMLButtonElement>('button')).find((button) =>
    button.textContent?.includes(text),
  )
}

async function selectMoreItem(wrapper: VueWrapper, label: string) {
  await wrapper.get('button[aria-label="项目更多操作"]').trigger('click')
  await flushPromises()
  const item = Array.from(document.body.querySelectorAll<HTMLElement>('[role="menuitem"]')).find(
    (option) => option.textContent?.includes(label),
  )
  if (!item) throw new Error(`找不到更多菜单项：${label}`)
  item.click()
  await flushPromises()
}

async function openDeleteConfirmation(wrapper: VueWrapper) {
  await selectMoreItem(wrapper, '彻底删除项目')
}

beforeEach(() => {
  getProjectResourceConfigMock.mockResolvedValue(resourceConfig)
  createProjectResourceBatchMock.mockResolvedValue(emptyBatch)
  cancelProjectResourceBatchMock.mockResolvedValue(undefined)
})

afterEach(() => {
  vi.restoreAllMocks()
  vi.doUnmock('vue-router')
  vi.doUnmock('@tanstack/vue-query')
  vi.doUnmock('@/stores/auth')
  vi.doUnmock('@/stores/workspace')
  vi.doUnmock('@/stores/ui')
  vi.doUnmock('@/api/client')
  runtimeOverride.value = null
  runtimeFailure.value = false
  runtimeRefetch.mockReset()
  invalidateQueries.mockReset()
  vi.resetModules()
  document.body.innerHTML = ''
  workspaceStore.error = ''
  workspaceStore.projectDetail.project = { ...baseProject }
  workspaceStore.projectDetail.resources = []
  workspaceStore.loadProject.mockReset()
  apiMock.mockReset()
  commitProjectResourceBatchMock.mockReset()
  createProjectResourceBatchMock.mockReset()
  uploadProjectResourceBatchFileMock.mockReset()
  getProjectResourceConfigMock.mockReset()
  cancelProjectResourceBatchMock.mockReset()
  deactivateProjectResourceMock.mockReset()
  fetchTextMock.mockReset()
  getProjectManagementOptionsMock.mockReset()
  routerPush.mockReset()
  showToast.mockReset()
})

describe('ProjectWorkspace shared control migration', () => {
  it('shows the pause timeout policy near ordinary project controls, not managed projects', async () => {
    const wrapper = await mountWorkspace('active')
    expect(wrapper.get('.workspace-pause-policy').text()).toBe('暂停满12小时将自动结束，工作文件保留')
    Object.assign(workspaceStore.projectDetail.project, { verification_request_id: 'verification-1' })
    await flushPromises()
    expect(wrapper.find('.workspace-pause-policy').exists()).toBe(false)
    expect(apiMock).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it.each(['active', 'completed', 'stopped'])('R4 exposes existing schedule history for %s without lifecycle actions', async (status) => {
    const wrapper = await mountWorkspace(status)
    expect(wrapper.findAll('.workspace-tabs > a').some(link => link.text() === '计划历史')).toBe(false)
    workspaceStore.projectDetail.project.schedule_id = 'schedule-history'
    await flushPromises()
    const link = wrapper.findAll('.workspace-tabs > a').find(item => item.text() === '计划历史')!
    expect(link.attributes('to')).toBe('/project-schedules/schedule-history')
    expect(apiMock).not.toHaveBeenCalled()
    workspaceStore.projectDetail.project.schedule_id = null
    await flushPromises()
    expect(wrapper.findAll('.workspace-tabs > a').some(item => item.text() === '计划历史')).toBe(false)
    wrapper.unmount()
  })

  it('总览替代旧资料弹窗入口，外壳只保留项目导航', async () => {
    const wrapper = await mountWorkspace()
    expect(wrapper.findAll('.workspace-tabs > a').map((link) => link.text())).toEqual(['总览', '漏洞成果', '运行日志', '文件', '图谱', '流量', '人工提示'])
    expect(buttonByText('项目资料')).toBeUndefined()
    expect(document.body.querySelector('[role="dialog"]')).toBeNull()
    expect(createProjectResourceBatchMock).not.toHaveBeenCalled()
    expect(commitProjectResourceBatchMock).not.toHaveBeenCalled()
    await wrapper.get('button[aria-label="项目更多操作"]').trigger('click')
    await flushPromises()
    expect([...document.body.querySelectorAll('[role="menuitem"]')].some((item) => item.textContent?.includes('项目资料'))).toBe(false)
    wrapper.unmount()
  }, 15_000)

  it('R2 shows pausing and stale observations without changing the metadata action', async () => {
    runtimeOverride.value = runtimeV2Fixture({ project_id: baseProject.id, project_status: 'paused', state: 'pausing', code: 'project_pause_draining' })
    const wrapper = await mountWorkspace('paused')
    expect(wrapper.get('.header-left [aria-label="项目状态"]').text()).toBe('已暂停')
    expect(wrapper.get('.header-left').text()).toContain('停止待确认')
    expect(buttonByText('继续项目')?.disabled).toBe(false)
    runtimeFailure.value = true
    await flushPromises()
    expect(wrapper.get('.header-left').text()).toContain('观测未更新')
    expect(buttonByText('继续项目')?.disabled).toBe(false)
    await wrapper.get('button[aria-label="运行详情"]').trigger('click')
    await flushPromises()
    expect(document.body.querySelector('.project-observation-details')?.textContent).toContain('历史观测时间')
    buttonByText('刷新观测')!.click()
    expect(runtimeRefetch).toHaveBeenCalledWith({ cancelRefetch: false })
    expect(apiMock).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('keeps project status beside its title and moves low-frequency metadata into More', async () => {
    const wrapper = await mountWorkspace('stopped')
    expect(wrapper.find('.header-left [aria-label="项目状态"]').exists()).toBe(true)
    expect(wrapper.find('.header-middle').exists()).toBe(false)
    expect(wrapper.find('.workspace-meta').exists()).toBe(false)
    await selectMoreItem(wrapper, '项目信息')
    const information = document.body.querySelector('.workspace-project-information')!
    expect(information.textContent).toContain('执行停止时间不限')
    expect(information.textContent).toContain('授权过期时间无限期')
    wrapper.unmount()
  })

  it('keeps stale workspace data visible in an Alert', async () => {
    workspaceStore.error = '轮询失败'
    const wrapper = await mountWorkspace()

    expect(document.body.textContent).toContain('实时状态刷新失败')
    expect(document.body.textContent).toContain('测试项目')
    expect(wrapper.findAll('.workspace-tabs .tab-item').map((tab) => tab.text())).toEqual([
      '总览', '漏洞成果', '运行日志', '文件', '图谱', '流量', '人工提示',
    ])

    workspaceStore.error = ''
    wrapper.unmount()
  }, 15_000)

  it('guards duplicate pause requests while the status update is pending', async () => {
    const update = deferred<void>()
    apiMock.mockReturnValue(update.promise)
    const wrapper = await mountWorkspace('active')

    const pauseButton = buttonByText('暂停项目')
    expect(pauseButton).toBeDefined()
    pauseButton!.click()
    pauseButton!.click()
    await flushPromises()

    expect(apiMock).toHaveBeenCalledOnce()
    expect(apiMock).toHaveBeenCalledWith('POST', '/projects/proj_test/pause')
    expect(pauseButton?.disabled).toBe(true)
    expect(wrapper.get('.header-action-reasons').text()).toContain('暂停项目：正在提交项目操作。')

    update.resolve()
    await flushPromises()
    wrapper.unmount()
  }, 15_000)

  it('requires an explicit replacement expiry when reauthorizing', async () => {
    runtimeOverride.value = runtimeFixture({
      project_id: 'proj_test',
      project_status: 'paused',
      state: 'paused',
      actions: {
        pause: { allowed: false, reason: 'project_not_active' },
        resume: { allowed: false, reason: 'project_authorization_expired' },
        reauthorize: { allowed: true, reason: null },
        reopen: { allowed: false, reason: 'project_status_invalid' },
        finish: { allowed: true, reason: null },
      },
    })
    apiMock.mockResolvedValue(undefined)
    const wrapper = await mountWorkspace('paused')

    buttonByText('重新确认授权')?.click()
    await flushPromises()

    const confirmButton = buttonByText('确认重新授权')
    expect(confirmButton?.disabled).toBe(true)
    expect(document.body.textContent).toContain('不会沿用或静默延长原期限')
    const summary = document.body.querySelector('.authorization-summary')!
    expect(summary.textContent).toContain('累计 Explore 次数')
    expect(summary.textContent).toContain('执行停止时间')
    expect([...summary.querySelectorAll('dd')].map((item) => item.textContent?.trim())).toEqual(
      expect.arrayContaining(['3', '不限']),
    )
    expect(wrapper.find('.workspace-meta').exists()).toBe(false)
    expect(summary.textContent).not.toContain('预算')

    document.body.querySelector<HTMLInputElement>('input[aria-label="授权无限期有效"]')?.click()
    document.body
      .querySelector<HTMLInputElement>('input[aria-label="确认当前项目授权范围"]')
      ?.click()
    await flushPromises()

    expect(confirmButton?.disabled).toBe(false)
    confirmButton?.click()
    await flushPromises()

    expect(apiMock).toHaveBeenCalledWith('POST', '/projects/proj_test/reauthorize', {
      confirmed: true,
      expected_authorization_epoch: 2,
      expected_scope_digest: `authorization-scope:${'a'.repeat(64)}`,
      authorization_expires_at: null,
    })
    wrapper.unmount()
  }, 15_000)

  it('opens the existing report flow from the workspace header', async () => {
    const wrapper = await mountWorkspace('stopped')

    expect(document.body.querySelector('[data-testid="project-report-dialog"]')).toBeNull()
    buttonByText('查看成果')?.click()
    await flushPromises()

    expect(
      document.body.querySelector('[data-testid="project-report-dialog"]')?.textContent,
    ).toContain('报告流程 proj_test stopped')
    wrapper.unmount()
  }, 15_000)

  it.each(['active', 'paused'])('finishes %s research without automatically opening the report flow', async (status) => {
    const finish = deferred<void>()
    apiMock.mockReturnValue(finish.promise)
    const wrapper = await mountWorkspace(status)

    await selectMoreItem(wrapper, '结束项目')
    expect(document.body.querySelector('[role="alertdialog"]')?.textContent).toContain('不会自动生成报告')
    const confirmButton = buttonByText('确认结束项目')
    confirmButton?.click()
    confirmButton?.click()
    await flushPromises()

    expect(apiMock).toHaveBeenCalledOnce()
    expect(apiMock).toHaveBeenCalledWith('POST', '/projects/proj_test/finish')
    expect(document.body.querySelector('[data-testid="project-report-dialog"]')).toBeNull()

    finish.resolve()
    await flushPromises()
    expect(document.body.querySelector('[role="alertdialog"]')).toBeNull()
    expect(document.body.querySelector('[data-testid="project-report-dialog"]')).toBeNull()

    workspaceStore.projectDetail.project.status = 'stopped'
    runtimeOverride.value = runtimeFixture({
      project_id: 'proj_test',
      project_status: 'stopped',
      state: 'stopping',
      active_runs: [],
    })
    await flushPromises()
    expect(document.body.querySelector('[data-testid="project-report-dialog"]')).toBeNull()

    runtimeOverride.value = runtimeFixture({
      project_id: 'proj_test',
      project_status: 'stopped',
      state: 'stopped',
      active_runs: [
        {
          id: 'run-active',
          task_type: 'explore',
          status: 'running',
          worker_name: 'worker-a',
          execution_stage: 'running',
          started_at: new Date().toISOString(),
        },
      ],
    })
    await flushPromises()
    expect(document.body.querySelector('[data-testid="project-report-dialog"]')).toBeNull()

    runtimeOverride.value = runtimeFixture({
      project_id: 'proj_test',
      project_status: 'stopped',
      state: 'stopped',
      active_runs: [],
    })
    await flushPromises()

    expect(document.body.querySelector('[data-testid="project-report-dialog"]')).toBeNull()
    expect(apiMock).toHaveBeenCalledOnce()
    buttonByText('查看成果')?.click()
    await flushPromises()
    expect(
      document.body.querySelector('[data-testid="project-report-dialog"]')?.textContent,
    ).toContain('报告流程 proj_test stopped')
    wrapper.unmount()
  }, 15_000)

  it('does not open the report flow when finishing fails', async () => {
    apiMock.mockRejectedValue(new Error('结束请求冲突'))
    const wrapper = await mountWorkspace('active')

    await selectMoreItem(wrapper, '结束项目')
    buttonByText('确认结束项目')?.click()
    await flushPromises()

    expect(document.body.textContent).toContain('项目操作失败')
    expect(document.body.textContent).not.toContain('正在确认评估已静默')
    expect(document.body.querySelector('[data-testid="project-report-dialog"]')).toBeNull()
    wrapper.unmount()
  }, 15_000)

  it('trims title edits, exposes saving state, and blocks duplicate submissions', async () => {
    const update = deferred<void>()
    apiMock.mockReturnValue(update.promise)
    const wrapper = await mountWorkspace()

    await selectMoreItem(wrapper, '重命名项目')
    const input = document.body.querySelector<HTMLInputElement>('#workspace-project-title')!
    input.value = '  新标题  '
    input.dispatchEvent(new Event('input', { bubbles: true }))
    input.dispatchEvent(new KeyboardEvent('keyup', { key: 'Enter', bubbles: true }))
    input.dispatchEvent(new KeyboardEvent('keyup', { key: 'Enter', bubbles: true }))
    await flushPromises()

    expect(apiMock).toHaveBeenCalledOnce()
    expect(apiMock).toHaveBeenCalledWith('PUT', '/projects/proj_test/title', { title: '新标题' })
    expect(buttonByText('保存名称')?.getAttribute('aria-busy')).toBe('true')
    expect(input.disabled).toBe(true)

    update.resolve()
    await flushPromises()
    wrapper.unmount()
  }, 15_000)

  it('groups secondary actions and routes delete through ConfirmAction', async () => {
    const wrapper = await mountWorkspace()
    await wrapper.get('button[aria-label="项目更多操作"]').trigger('click')
    await flushPromises()

    const menu = document.body.querySelector<HTMLElement>('[role="menu"]')
    const items = Array.from(document.body.querySelectorAll<HTMLElement>('[role="menuitem"]'))
    expect(menu).not.toBeNull()
    expect(items.map((item) => item.textContent)).toEqual([
      '项目信息',
      '重命名项目',
      '代理设置',
      '导出项目配置 YAML',
      '下载漏洞证据 ZIP（按漏洞分组）',
      '彻底删除项目',
    ])

    items.find((item) => item.textContent === '彻底删除项目')?.click()
    await flushPromises()

    expect(document.body.querySelector('[role="menu"]')).toBeNull()
    expect(document.body.querySelector('[role="alertdialog"]')?.textContent).toContain(
      '删除工作空间',
    )
    expect(document.body.textContent).toContain('您确定要永久删除项目「测试项目」')

    wrapper.unmount()
  }, 15_000)

  it('offers YAML and vulnerability evidence ZIP exports', async () => {
    const wrapper = await mountWorkspace()
    await wrapper.get('button[aria-label="项目更多操作"]').trigger('click')
    await flushPromises()

    const menuItems = Array.from(
      document.body.querySelectorAll<HTMLElement>('[role="menuitem"]'),
    ).map((item) => item.textContent)
    expect(menuItems).toEqual(
      expect.arrayContaining(['导出项目配置 YAML', '下载漏洞证据 ZIP（按漏洞分组）']),
    )
    expect(menuItems).not.toContain('下载项目产物 ZIP（不含日志）')

    wrapper.unmount()
  }, 15_000)

  it('starts the vulnerability evidence ZIP download without navigating the workspace', async () => {
    workspaceStore.projectDetail.project = { ...baseProject, id: 'proj/encoded' }
    const clickSpy = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {})
    const wrapper = await mountWorkspace()
    workspaceStore.projectDetail.project = { ...baseProject, id: 'proj/encoded' }
    await flushPromises()

    await wrapper.get('button[aria-label="项目更多操作"]').trigger('click')
    await flushPromises()
    const archiveItem = Array.from(
      document.body.querySelectorAll<HTMLElement>('[role="menuitem"]'),
    ).find((item) => item.textContent?.includes('下载漏洞证据 ZIP'))
    archiveItem?.click()
    await flushPromises()

    expect(clickSpy).toHaveBeenCalledOnce()
    const link = clickSpy.mock.instances[0] as HTMLAnchorElement
    expect(link.getAttribute('href')).toBe('/projects/proj%2Fencoded/artifacts/archive')
    expect(link.download).toBe('')
    expect(link.target).toBe('_blank')
    expect(link.rel).toBe('noopener')
    expect(link.isConnected).toBe(false)
    expect(showToast).toHaveBeenCalledWith('已开始生成漏洞证据 ZIP', 'success')
    expect(routerPush).not.toHaveBeenCalled()

    wrapper.unmount()
  }, 15_000)

  it('移除项目重绑入口，保留代理设置', async () => {
    const wrapper = await mountWorkspace()
    await wrapper.get('button[aria-label="项目更多操作"]').trigger('click')
    await flushPromises()
    const labels = [...document.body.querySelectorAll('[role="menuitem"]')].map(
      (item) => item.textContent,
    )
    expect(labels.some((label) => label?.includes('Workers'))).toBe(false)
    expect(labels.some((label) => label?.includes('代理设置'))).toBe(true)
    expect(getProjectManagementOptionsMock).not.toHaveBeenCalled()
    wrapper.unmount()
  }, 15_000)

  it('keeps the reopen Dialog modal while saving and submits trimmed feedback once', async () => {
    const reopen = deferred<void>()
    apiMock.mockReturnValue(reopen.promise)
    const wrapper = await mountWorkspace('completed')

    buttonByText('重新开启')?.click()
    await flushPromises()

    const textarea = document.body.querySelector<HTMLTextAreaElement>('#workspace-reopen-feedback')
    textarea!.value = '  已确认存在新的绕过路径  '
    textarea?.dispatchEvent(new Event('input', { bubbles: true }))
    await flushPromises()

    const reopenButton = buttonByText('重开项目')
    reopenButton?.click()
    reopenButton?.click()
    await flushPromises()

    expect(apiMock).toHaveBeenCalledOnce()
    expect(apiMock).toHaveBeenCalledWith('POST', '/projects/proj_test/reopen', {
      description: '已确认存在新的绕过路径',
    })
    expect(buttonByText('取消')?.disabled).toBe(true)
    expect(document.body.querySelector('button[aria-label="关闭"]')).toBeNull()

    reopen.resolve()
    await flushPromises()

    expect(document.body.querySelector('[role="dialog"]')).toBeNull()
    expect(workspaceStore.loadProject).toHaveBeenCalledWith('proj_test')
    wrapper.unmount()
  }, 15_000)

  it('keeps the reopen Dialog open when the API request fails', async () => {
    apiMock.mockRejectedValue(new Error('重开请求失败'))
    const wrapper = await mountWorkspace('completed')

    buttonByText('重新开启')?.click()
    await flushPromises()
    const textarea = document.body.querySelector<HTMLTextAreaElement>('#workspace-reopen-feedback')
    textarea!.value = '继续验证'
    textarea?.dispatchEvent(new Event('input', { bubbles: true }))
    await flushPromises()
    buttonByText('重开项目')?.click()
    await flushPromises()

    expect(document.body.querySelector('[role="dialog"]')).not.toBeNull()
    expect(showToast).toHaveBeenCalledWith('重开请求失败', 'danger')
    wrapper.unmount()
  }, 15_000)

  it('blocks duplicate deletion and keeps the confirmation open after failure', async () => {
    const deletion = deferred<void>()
    apiMock.mockReturnValue(deletion.promise)
    const wrapper = await mountWorkspace()
    await openDeleteConfirmation(wrapper)

    const confirmButton = buttonByText('确认删除')
    confirmButton?.click()
    confirmButton?.click()
    await flushPromises()

    expect(apiMock).toHaveBeenCalledOnce()
    expect(apiMock).toHaveBeenCalledWith('DELETE', '/projects/proj_test')
    expect(buttonByText('取消')?.disabled).toBe(true)

    deletion.reject(new Error('删除请求失败'))
    await flushPromises()

    expect(document.body.querySelector('[role="alertdialog"]')).not.toBeNull()
    expect(showToast).toHaveBeenCalledWith('删除请求失败', 'danger')
    expect(routerPush).not.toHaveBeenCalled()
    wrapper.unmount()
  }, 15_000)

  it('closes the confirmation and returns to Projects after deletion succeeds', async () => {
    apiMock.mockResolvedValue(undefined)
    const wrapper = await mountWorkspace()
    await openDeleteConfirmation(wrapper)

    buttonByText('确认删除')?.click()
    await flushPromises()

    expect(document.body.querySelector('[role="alertdialog"]')).toBeNull()
    expect(showToast).toHaveBeenCalledWith('项目已被彻底删除', 'success')
    expect(routerPush).toHaveBeenCalledWith('/projects')
    wrapper.unmount()
  }, 15_000)
})
