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
const updateProjectWorkersMock = vi.fn()
const routerPush = vi.fn()
const showToast = vi.fn()
const runtimeOverride = ref<ProjectRuntime | null>(null)
const runtimeFailure = ref(false)
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
  group_name: null,
  tenant_name: 'admin',
  tenant_id: 'tenant_test',
  tenant_owner_login: 'admin',
  tenant_workers: [],
  authorization_epoch: 2,
  authorization_scope_digest: `authorization-scope:${'a'.repeat(64)}`,
  authorization_expires_at: null,
  explore_attempts_started: 3,
  explore_budget: 12,
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
          isError: runtimeFailure,
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
    updateProjectWorkers: updateProjectWorkersMock,
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

async function openDeleteConfirmation(wrapper: VueWrapper) {
  await wrapper.get('button[aria-label="项目工具菜单"]').trigger('click')
  await flushPromises()
  document.body.querySelector<HTMLElement>('[role="menuitem"]')?.click()
  await flushPromises()
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
  updateProjectWorkersMock.mockReset()
  routerPush.mockReset()
  showToast.mockReset()
})

describe('ProjectWorkspace shared control migration', () => {
  it('keeps stale workspace data visible in an Alert', async () => {
    workspaceStore.error = '轮询失败'
    const wrapper = await mountWorkspace()

    expect(document.body.textContent).toContain('实时状态刷新失败')
    expect(document.body.textContent).toContain('测试项目')

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

    document.body.querySelector<HTMLInputElement>('input[aria-label="授权无限期有效"]')?.click()
    document.body.querySelector<HTMLInputElement>('input[aria-label="确认当前项目授权范围"]')?.click()
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

    expect(document.body.querySelector('[data-testid="project-report-dialog"]')?.textContent).toContain(
      '报告流程 proj_test stopped',
    )
    wrapper.unmount()
  }, 15_000)

  it('waits for authoritative terminal quiescence before opening the report flow', async () => {
    const finish = deferred<void>()
    apiMock.mockReturnValue(finish.promise)
    const wrapper = await mountWorkspace('active')

    buttonByText('结束并生成报告')?.click()
    await flushPromises()
    const confirmButton = buttonByText('确认结束并等待报告')
    confirmButton?.click()
    confirmButton?.click()
    await flushPromises()

    expect(apiMock).toHaveBeenCalledOnce()
    expect(apiMock).toHaveBeenCalledWith('POST', '/projects/proj_test/finish')
    expect(document.body.querySelector('[data-testid="project-report-dialog"]')).toBeNull()

    finish.resolve()
    await flushPromises()
    expect(document.body.textContent).toContain('正在确认评估已静默')

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

    expect(document.body.querySelector('[data-testid="project-report-dialog"]')?.textContent).toContain(
      '报告流程 proj_test stopped',
    )
    expect(document.body.textContent).not.toContain('正在确认评估已静默')
    wrapper.unmount()
  }, 15_000)

  it('does not open the report flow when finishing fails', async () => {
    apiMock.mockRejectedValue(new Error('结束请求冲突'))
    const wrapper = await mountWorkspace('active')

    buttonByText('结束并生成报告')?.click()
    await flushPromises()
    buttonByText('确认结束并等待报告')?.click()
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

    buttonByText('测试项目')?.click()
    await flushPromises()
    const input = document.body.querySelector<HTMLInputElement>('input[aria-label="项目名称"]')!
    input.value = '  新标题  '
    input.dispatchEvent(new Event('input', { bubbles: true }))
    input.dispatchEvent(new KeyboardEvent('keyup', { key: 'Enter', bubbles: true }))
    input.dispatchEvent(new KeyboardEvent('keyup', { key: 'Enter', bubbles: true }))
    await flushPromises()

    expect(apiMock).toHaveBeenCalledOnce()
    expect(apiMock).toHaveBeenCalledWith('PUT', '/projects/proj_test/title', { title: '新标题' })
    expect(document.body.textContent).toContain('正在保存')
    expect(input.disabled).toBe(true)

    update.resolve()
    await flushPromises()
    wrapper.unmount()
  }, 15_000)

  it('opens the semantic tools menu and routes delete through ConfirmAction', async () => {
    const wrapper = await mountWorkspace()
    await wrapper.get('button[aria-label="项目工具菜单"]').trigger('click')
    await flushPromises()

    const menu = document.body.querySelector<HTMLElement>('[role="menu"]')
    const deleteItem = document.body.querySelector<HTMLElement>('[role="menuitem"]')
    expect(menu).not.toBeNull()
    expect(deleteItem?.textContent).toContain('彻底删除项目')

    deleteItem?.click()
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
    buttonByText('导出')?.click()
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

    buttonByText('导出')?.click()
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

  it('announces the Worker loading state through a live status region', async () => {
    const options = deferred<{ tenant_workers: [] }>()
    getProjectManagementOptionsMock.mockReturnValue(options.promise)
    const wrapper = await mountWorkspace()

    buttonByText('Workers')?.click()
    await wrapper.vm.$nextTick()

    const status = document.body.querySelector<HTMLElement>('[role="status"][aria-live="polite"]')
    expect(status?.textContent).toContain('正在加载可用 Worker')
    expect(status?.querySelectorAll('.worker-dialog-skeleton')).toHaveLength(2)

    options.resolve({ tenant_workers: [] })
    await flushPromises()
    wrapper.unmount()
  }, 15_000)

  it('loads Worker options, preserves the binding payload, and blocks duplicate saves', async () => {
    getProjectManagementOptionsMock.mockResolvedValue({
      tenant_workers: [
        {
          id: 'worker_1',
          display_name: '主 Worker',
          model_id: 'model_1',
          model_display_name: 'Claude',
          task_types: ['bootstrap', 'reason'],
          available: true,
        },
      ],
    })
    const save = deferred<void>()
    updateProjectWorkersMock.mockReturnValue(save.promise)
    const wrapper = await mountWorkspace()

    buttonByText('Workers')?.click()
    await flushPromises()

    const dialog = document.body.querySelector<HTMLElement>('[role="dialog"]')
    expect(dialog?.textContent).toContain('更新项目 Workers')
    expect(dialog?.textContent).toContain('主 Worker')

    const checkbox = document.body.querySelector<HTMLInputElement>('input[type="checkbox"]')!
    dialog?.querySelector<HTMLElement>('.project-worker-option strong')?.click()
    await flushPromises()
    expect(checkbox.checked).toBe(true)

    dialog?.querySelector<HTMLElement>('.project-worker-option small')?.click()
    await flushPromises()
    expect(checkbox.checked).toBe(false)
    dialog?.querySelector<HTMLElement>('.project-worker-option small')?.click()
    await flushPromises()
    expect(checkbox.checked).toBe(true)

    const saveButton = buttonByText('保存绑定')
    saveButton?.click()
    saveButton?.click()
    await flushPromises()

    expect(updateProjectWorkersMock).toHaveBeenCalledOnce()
    expect(updateProjectWorkersMock).toHaveBeenCalledWith('proj_test', {
      tenant_worker_ids: ['worker_1'],
    })
    expect(buttonByText('取消')?.disabled).toBe(true)
    expect(document.body.querySelector('button[aria-label="关闭"]')).toBeNull()

    save.resolve()
    await flushPromises()

    expect(document.body.querySelector('[role="dialog"]')).toBeNull()
    expect(workspaceStore.loadProject).toHaveBeenCalledWith('proj_test')
    wrapper.unmount()
  }, 15_000)

  it('keeps resource mutation available with a trusted v2 stopped snapshot', async () => {
    runtimeOverride.value = runtimeV2Fixture({
      project_id: 'proj_test',
      project_status: 'stopped',
      state: 'stopped',
      active_runs: [],
    })
    const wrapper = await mountWorkspace('stopped')

    buttonByText('资料')?.click()
    await flushPromises()

    const note = document.body.querySelector<HTMLElement>('.resource-mutation-note')
    expect(note?.textContent).toContain('资料变更仅用于后续允许的任务')
    expect(note?.classList.contains('blocked')).toBe(false)
    wrapper.unmount()
  }, 15_000)

  it('cancels a pending resource batch when the resource Dialog closes', async () => {
    uploadProjectResourceBatchFileMock.mockResolvedValue({
      id: 'member-1',
      path: 'login.bin',
      kind: 'file',
      size_bytes: 4,
      sha256: 'a'.repeat(64),
      source: 'file',
    })
    const wrapper = await mountWorkspace()

    buttonByText('资料')?.click()
    await flushPromises()
    const input = document.body.querySelectorAll<HTMLInputElement>('input[type="file"]')[0]
    Object.defineProperty(input, 'files', {
      configurable: true,
      value: [new File([new Uint8Array([0xff, 0x00, 0x80, 0x01])], 'login.bin')],
    })
    input.dispatchEvent(new Event('change', { bubbles: true }))
    await flushPromises()

    expect(
      document.body.querySelector<HTMLInputElement>('[aria-label="资源相对路径"]')?.value,
    ).toBe('login.bin')
    buttonByText('关闭')?.click()
    await flushPromises()

    expect(cancelProjectResourceBatchMock).toHaveBeenCalledWith('batch-1')
    expect(document.body.querySelector('[role="dialog"]')).toBeNull()
    wrapper.unmount()
  }, 15_000)

  it('commits the complete batch once and keeps the Dialog modal while pending', async () => {
    uploadProjectResourceBatchFileMock.mockResolvedValue({
      id: 'member-1',
      path: 'login.bin',
      kind: 'file',
      size_bytes: 4,
      sha256: 'a'.repeat(64),
      source: 'file',
    })
    const commit = deferred<{ resources: [] }>()
    commitProjectResourceBatchMock.mockReturnValue(commit.promise)
    const wrapper = await mountWorkspace()

    buttonByText('资料')?.click()
    await flushPromises()
    const input = document.body.querySelectorAll<HTMLInputElement>('input[type="file"]')[0]
    Object.defineProperty(input, 'files', {
      configurable: true,
      value: [new File(['test'], 'login.bin')],
    })
    input.dispatchEvent(new Event('change', { bubbles: true }))
    await flushPromises()

    buttonByText('原子导入资料')?.click()
    buttonByText('原子导入资料')?.click()
    await flushPromises()

    expect(commitProjectResourceBatchMock).toHaveBeenCalledOnce()
    expect(commitProjectResourceBatchMock).toHaveBeenCalledWith('proj_test', 'batch-1')
    expect(buttonByText('关闭')?.disabled).toBe(true)
    expect(document.body.querySelector('button[aria-label="关闭"]')).toBeNull()

    commit.resolve({ resources: [] })
    await flushPromises()
    expect(document.body.querySelector('[role="dialog"]')).toBeNull()
    expect(showToast).toHaveBeenCalledWith('辅助资料已原子导入，将用于后续 TaskRun', 'success')
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
