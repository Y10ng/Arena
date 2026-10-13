import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { ref, unref } from 'vue'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import Input from '@/components/ui/Input.vue'
import NewProject from './NewProject.vue'

const aiAgentOrigin = 'AI Agent origin\n- tenant: test-tenant\n- side effects: reversible only'
const aiAgentGoal = 'AI Agent goal\n1. prompt injection\n2. tool authorization\n3. isolation'

const mocks = vi.hoisted(() => ({
  scenarios: [
    {
      key: 'secondary',
      label: '次要场景',
      is_default: false,
      origin_template: 'secondary origin',
      goal_template: 'secondary goal',
    },
    {
      key: 'default',
      label: '默认场景',
      is_default: true,
      origin_template: 'default origin\npublic environment context',
      goal_template: 'default goal',
    },
    {
      key: 'ai-agent-security',
      label: 'AI Agent 安全评估',
      is_default: false,
      origin_template: 'AI Agent origin\n- tenant: test-tenant\n- side effects: reversible only',
      goal_template: 'AI Agent goal\n1. prompt injection\n2. tool authorization\n3. isolation',
    },
  ],
  workers: [
    {
      id: 'worker-available',
      display_name: '可用 Worker',
      model_id: 'model-a',
      model_display_name: 'Claude',
      engine: 'pi',
      task_types: ['bootstrap', 'reason'],
      available: true,
    },
    {
      id: 'worker-disabled',
      display_name: '不可用 Worker',
      model_id: 'model-b',
      model_display_name: 'Claude',
      task_types: ['reason'],
      available: false,
    },
  ],
  groups: [{ id: 'group-a', name: 'Red Team', owner: 'owner', created_at: '2026-08-20' }],
  libraries: [{ key: 'library-a', name: '授权资料', latest_version: 2, file_count: 3 }],
  templates: [
    {
      id: 'template-a',
      name: '默认 Worker 组',
      tenant_worker_ids: ['worker-available', 'worker-disabled'],
    },
  ],
  queryState: {} as Record<string, { loading?: boolean; error?: boolean }>,
  queryData: {} as Record<string, { value: unknown }>,
  createProject: vi.fn(),
  createResourceBatch: vi.fn(),
  getResourceConfig: vi.fn(),
  uploadResourceFile: vi.fn(),
  cancelResourceBatch: vi.fn(),
  router: { push: vi.fn() },
  uiStore: { showToast: vi.fn() },
}))

vi.mock('@/api/client', () => ({
  api: vi.fn(),
  cancelProjectResourceBatch: mocks.cancelResourceBatch,
  createProject: mocks.createProject,
  createProjectResourceBatch: mocks.createResourceBatch,
  deleteProjectResourceBatchMember: vi.fn(),
  getProjectResourceConfig: mocks.getResourceConfig,
  importProjectResourceBatchZip: vi.fn(),
  listLibraryCatalog: vi.fn(),
  listProjectGroups: vi.fn(),
  listTenantWorkerOptions: vi.fn(),
  listWorkerTemplates: vi.fn(),
  updateProjectResourceBatchMember: vi.fn(),
  uploadProjectResourceBatchFile: mocks.uploadResourceFile,
}))

vi.mock('@/stores/ui', () => ({
  useUIStore: () => mocks.uiStore,
}))

vi.mock('vue-router', () => ({
  useRouter: () => mocks.router,
}))

vi.mock('@tanstack/vue-query', () => ({
  useQuery: ({ queryKey }: { queryKey: unknown }) => {
    const key = String((unref(queryKey) as unknown[])[0])
    const values: Record<string, unknown> = {
      'scenario-catalog': mocks.scenarios,
      'tenant-worker-options': mocks.workers,
      'project-groups': mocks.groups,
      'library-catalog': mocks.libraries,
      'worker-templates': mocks.templates,
    }
    const state = mocks.queryState[key] || {}
    const data = ref(values[key])
    mocks.queryData[key] = data
    return {
      data,
      isLoading: ref(Boolean(state.loading)),
      isError: ref(Boolean(state.error)),
      refetch: vi.fn(),
    }
  },
}))

const wrappers: VueWrapper[] = []

async function mountPage() {
  const wrapper = mount(NewProject, {
    attachTo: document.body,
    global: {
      mocks: { $router: mocks.router },
      stubs: { RouterLink: { template: '<a><slot /></a>' } },
    },
  })
  wrappers.push(wrapper)
  await flushPromises()
  return wrapper
}

function buttonByText(text: string) {
  return [...document.body.querySelectorAll<HTMLButtonElement>('button')].find((button) =>
    button.textContent?.includes(text),
  )
}

async function setFieldValue(selector: string, value: string) {
  const field = document.querySelector<HTMLInputElement | HTMLTextAreaElement>(selector)!
  field.value = value
  field.dispatchEvent(new Event('input', { bubbles: true }))
  await flushPromises()
}

async function selectOption(selectId: string, optionText: string) {
  const trigger = document.querySelector<HTMLButtonElement>(`#${selectId}[role="combobox"]`)!
  trigger.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true, button: 0 }))
  trigger.dispatchEvent(new PointerEvent('pointerup', { bubbles: true, button: 0 }))
  trigger.click()
  await flushPromises()

  const option = [...document.body.querySelectorAll<HTMLElement>('[role="option"]')].find(
    (candidate) => candidate.textContent?.includes(optionText),
  )!
  option.dispatchEvent(new PointerEvent('pointerup', { bubbles: true, button: 0 }))
  option.click()
  await flushPromises()
}

beforeEach(() => {
  HTMLElement.prototype.hasPointerCapture = () => false
  HTMLElement.prototype.setPointerCapture = () => undefined
  HTMLElement.prototype.releasePointerCapture = () => undefined
  HTMLElement.prototype.scrollIntoView = () => undefined

  mocks.queryState = {}
  mocks.queryData = {}
  mocks.groups = [{ id: 'group-a', name: 'Red Team', owner: 'owner', created_at: '2026-08-20' }]
  mocks.createProject.mockReset().mockResolvedValue({ project: { id: 'project-created' } })
  mocks.getResourceConfig.mockReset().mockResolvedValue({
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
  })
  mocks.createResourceBatch.mockReset().mockResolvedValue({
    id: 'batch-created',
    state: 'open',
    created_at: '2026-09-01T00:00:00Z',
    expires_at: '2026-09-02T00:00:00Z',
    resource_count: 0,
    total_bytes: 0,
    members: [],
  })
  mocks.uploadResourceFile.mockReset().mockResolvedValue({
    id: 'member-1',
    kind: 'script',
    path: 'probe.sh',
    size_bytes: 20,
    sha256: 'a'.repeat(64),
    source: 'file',
  })
  mocks.cancelResourceBatch.mockReset().mockResolvedValue(undefined)
  mocks.router.push.mockReset().mockResolvedValue(undefined)
  mocks.uiStore.showToast.mockReset()
})

afterEach(() => {
  wrappers.splice(0).forEach((wrapper) => wrapper.unmount())
  document.body.innerHTML = ''
})

describe('NewProject shared control migration', () => {
  it('focuses the title, selects the default scenario, preserves touched templates, and filters unavailable Workers', async () => {
    await mountPage()

    expect(document.activeElement).toBe(document.querySelector('#np-title'))
    expect(document.querySelector<HTMLTextAreaElement>('#np-origin-input')?.value).toBe(
      'default origin\npublic environment context',
    )
    expect(document.querySelector('button[aria-pressed="true"]')?.textContent).toContain('默认场景')
    expect(document.body.textContent).toContain('可用 Worker')
    expect(document.body.textContent).toContain('pi · Claude')
    expect(document.body.textContent).not.toContain('Claude Code ·')
    expect(document.body.textContent).not.toContain('不可用 Worker')
    expect(document.querySelector('#np-targets-input')).toBeNull()
    expect(document.body.textContent).toContain('Origin（已知信息）')
    expect(document.body.textContent).toContain('Goal（目标）')
    expect(document.body.textContent).not.toContain('入口凭证')
    expect(
      document.querySelector<HTMLInputElement>('input[aria-label="选择 Worker 可用 Worker"]')
        ?.checked,
    ).toBe(true)

    const origin = document.querySelector<HTMLTextAreaElement>('#np-origin-input')!
    origin.value = 'user supplied origin'
    origin.dispatchEvent(new Event('input', { bubbles: true }))
    await flushPromises()
    buttonByText('次要场景')?.click()
    await flushPromises()

    expect(origin.value).toBe('user supplied origin')
    expect(document.querySelector<HTMLTextAreaElement>('#np-goal-input')?.value).toBe(
      'secondary goal',
    )
  })

  it('applies AI Agent templates exactly and preserves user-edited fields', async () => {
    await mountPage()

    buttonByText('AI Agent 安全评估')?.click()
    await flushPromises()

    const origin = document.querySelector<HTMLTextAreaElement>('#np-origin-input')!
    const goal = document.querySelector<HTMLTextAreaElement>('#np-goal-input')!
    expect(origin.value).toBe(aiAgentOrigin)
    expect(goal.value).toBe(aiAgentGoal)

    goal.value = 'user-defined Agent coverage'
    goal.dispatchEvent(new Event('input', { bubbles: true }))
    await flushPromises()
    buttonByText('次要场景')?.click()
    await flushPromises()

    expect(origin.value).toBe('secondary origin')
    expect(goal.value).toBe('user-defined Agent coverage')
  })

  it('defaults to the first Worker but never restores it after a manual change', async () => {
    await mountPage()

    const checkbox = document.querySelector<HTMLInputElement>(
      'input[aria-label="选择 Worker 可用 Worker"]',
    )!
    expect(checkbox.checked).toBe(true)

    document.querySelector<HTMLElement>('.worker-option-name')?.click()
    await flushPromises()
    expect(checkbox.checked).toBe(false)

    mocks.queryData['tenant-worker-options'].value = [
      ...mocks.workers,
      {
        ...mocks.workers[0],
        id: 'worker-new',
        display_name: '新 Worker',
      },
    ]
    await flushPromises()

    expect(checkbox.checked).toBe(false)
    expect(
      document.querySelector<HTMLInputElement>('input[aria-label="选择 Worker 新 Worker"]')?.checked,
    ).toBe(false)
  })

  it('applies Worker templates and ignores unavailable bindings', async () => {
    await mountPage()

    const trigger = document.querySelector<HTMLButtonElement>(
      '#np-worker-template[role="combobox"]',
    )!
    trigger.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true, button: 0 }))
    trigger.dispatchEvent(new PointerEvent('pointerup', { bubbles: true, button: 0 }))
    trigger.click()
    await flushPromises()

    const option = [...document.body.querySelectorAll<HTMLElement>('[role="option"]')].find(
      (candidate) => candidate.textContent?.includes('默认 Worker 组'),
    )!
    option.dispatchEvent(new PointerEvent('pointerup', { bubbles: true, button: 0 }))
    option.click()
    await flushPromises()

    expect(
      document.querySelector<HTMLInputElement>('input[aria-label="选择 Worker 可用 Worker"]')
        ?.checked,
    ).toBe(true)
    expect(mocks.uiStore.showToast).toHaveBeenCalledWith(
      '模板中部分 Worker 当前不可用，已自动忽略',
      'info',
    )
  })

  it('submits address-free Origin and Goal without targets and includes the uploaded resource batch id', async () => {
    await mountPage()

    await setFieldValue('#np-title', '  Workspace title  ')
    await setFieldValue('#np-origin-input', '已有接口说明和两个测试角色，重点关注订单审批相关功能。')
    await setFieldValue('#np-goal-input', '重点检查普通用户之间的数据隔离及审批流程。')
    await setFieldValue('#np-explore-deadline', '2035-04-05T10:30')
    await setFieldValue('#np-authorization-expires-at', '2035-04-06T11:45')

    await selectOption('np-group-select', 'Red Team')
    buttonByText('授权资料')?.click()

    const fileInput = document.querySelector<HTMLInputElement>('input[type="file"]')!
    Object.defineProperty(fileInput, 'files', {
      configurable: true,
      value: [new File(['echo should-not-run\n'], 'probe.sh', { type: 'text/plain' })],
    })
    fileInput.dispatchEvent(new Event('change', { bubbles: true }))
    await flushPromises()

    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', ctrlKey: true }))
    await flushPromises()

    expect(mocks.createProject).toHaveBeenCalledOnce()
    expect(mocks.createProject).toHaveBeenCalledWith({
      title: 'Workspace title',
      origin: '已有接口说明和两个测试角色，重点关注订单审批相关功能。',
      goal: '重点检查普通用户之间的数据隔离及审批流程。',
      scenario: 'default',
      tenant_worker_ids: ['worker-available'],
      group_id: 'group-a',
      explore_deadline: new Date('2035-04-05T10:30').toISOString(),
      authorization_expires_at: new Date('2035-04-06T11:45').toISOString(),
      resource_batch_id: 'batch-created',
      library_keys: ['library-a'],
    })
    expect(mocks.uploadResourceFile).toHaveBeenCalledWith(
      'batch-created',
      expect.any(File),
      'probe.sh',
    )
    expect(mocks.router.push).toHaveBeenCalledWith('/projects/project-created')
  })

  it('creates with explicit null without touching the optional authorization controls', async () => {
    await mountPage()

    expect(document.querySelector('#np-explore-budget')).toBeNull()
    expect(document.body.textContent).toContain('执行停止时间')
    expect(document.body.textContent).toContain('留空为不限')
    expect(document.body.textContent).toContain('有序停止在途执行')
    expect(document.querySelector<HTMLInputElement>('#np-explore-deadline')?.value).toBe('')
    const authorizationInput = document.querySelector<HTMLInputElement>(
      '#np-authorization-expires-at',
    )!
    expect(authorizationInput.value).toBe('')
    expect(authorizationInput.disabled).toBe(false)
    expect(authorizationInput.required).toBe(false)
    expect(document.body.textContent).toContain('留空表示不设到期时间；如实际授权有期限，请填写')
    expect(document.body.textContent).not.toContain('明确确认真实授权无限期有效')
    expect(document.querySelector('input[aria-label="授权无限期有效"]')).toBeNull()
    expect(
      document.querySelector<HTMLInputElement>('input[aria-label="选择 Worker 可用 Worker"]')?.checked,
    ).toBe(true)

    await setFieldValue('#np-title', 'Unlimited authorization')
    await selectOption('np-group-select', 'Red Team')
    buttonByText('确认创建项目')?.click()
    await flushPromises()

    expect(mocks.createProject).toHaveBeenCalledOnce()
    expect(mocks.createProject).toHaveBeenCalledWith({
      title: 'Unlimited authorization',
      origin: 'default origin\npublic environment context',
      goal: 'default goal',
      scenario: 'default',
      tenant_worker_ids: ['worker-available'],
      group_id: 'group-a',
      authorization_expires_at: null,
      library_keys: [],
    })
    expect(mocks.router.push).toHaveBeenCalledWith('/projects/project-created')
  })

  it('validates the optional stop time without deriving an authorization expiry', async () => {
    await mountPage()
    await setFieldValue('#np-title', 'Independent stop time')
    await selectOption('np-group-select', 'Red Team')

    await setFieldValue('#np-explore-deadline', '2000-01-01T00:00')
    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', ctrlKey: true }))
    await flushPromises()
    expect(mocks.createProject).not.toHaveBeenCalled()
    expect(document.body.textContent).toContain('执行停止时间必须晚于当前时间')

    await setFieldValue('#np-explore-deadline', '2035-04-05T10:30')
    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', ctrlKey: true }))
    await flushPromises()
    expect(mocks.createProject).toHaveBeenCalledOnce()
    expect(mocks.createProject.mock.calls[0]?.[0]).toMatchObject({
      explore_deadline: new Date('2035-04-05T10:30').toISOString(),
      authorization_expires_at: null,
    })
    expect(document.querySelector<HTMLInputElement>('#np-authorization-expires-at')?.value).toBe('')
  })

  it('submits a finite authorization expiry and restores explicit null after clearing it', async () => {
    await mountPage()
    await setFieldValue('#np-title', 'Editable authorization')
    await selectOption('np-group-select', 'Red Team')

    await setFieldValue('#np-authorization-expires-at', '2035-04-06T11:45')
    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', ctrlKey: true }))
    await flushPromises()
    expect(mocks.createProject).toHaveBeenCalledOnce()
    expect(mocks.createProject.mock.calls[0]?.[0]).toMatchObject({
      authorization_expires_at: new Date('2035-04-06T11:45').toISOString(),
    })
    expect(mocks.createProject.mock.calls[0]?.[0]).not.toHaveProperty('explore_deadline')
    expect(document.querySelector<HTMLInputElement>('#np-explore-deadline')?.value).toBe('')

    await setFieldValue('#np-authorization-expires-at', '')
    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', ctrlKey: true }))
    await flushPromises()
    expect(mocks.createProject).toHaveBeenCalledTimes(2)
    expect(mocks.createProject.mock.calls[1]?.[0]).toHaveProperty('authorization_expires_at', null)
    expect(mocks.createProject.mock.calls[1]?.[0]).not.toHaveProperty('explore_deadline')
  })

  it.each([
    ['2000-01-01T00:00', '授权到期时间必须晚于当前时间'],
    ['invalid-date', '授权到期时间格式无效'],
  ])('rejects nonempty authorization expiry %s instead of submitting null', async (value, error) => {
    const wrapper = await mountPage()
    await setFieldValue('#np-title', 'Invalid authorization')
    await selectOption('np-group-select', 'Red Team')

    // datetime-local 会清空非法 DOM 值；通过控件事件验证非空非法表单值不会降级成 null。
    const input = wrapper.findAllComponents(Input).find(
      (component) => component.attributes('id') === 'np-authorization-expires-at',
    )!
    input.vm.$emit('update:modelValue', value)
    await flushPromises()
    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', ctrlKey: true }))
    await flushPromises()
    expect(mocks.createProject).not.toHaveBeenCalled()
    expect(document.body.textContent).toContain(error)
    expect(mocks.router.push).not.toHaveBeenCalled()
  })

  it('keeps an uploaded batch after project validation fails so submission can retry', async () => {
    mocks.createProject
      .mockRejectedValueOnce(new Error('项目字段校验失败'))
      .mockResolvedValueOnce({ project: { id: 'project-created' } })
    await mountPage()

    await setFieldValue('#np-title', 'Retry project')
    await selectOption('np-group-select', 'Red Team')

    const fileInput = document.querySelectorAll<HTMLInputElement>('input[type="file"]')[0]
    Object.defineProperty(fileInput, 'files', {
      configurable: true,
      value: [new File(['binary'], 'evidence.bin')],
    })
    fileInput.dispatchEvent(new Event('change', { bubbles: true }))
    await flushPromises()

    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', ctrlKey: true }))
    await flushPromises()
    expect(mocks.uiStore.showToast).toHaveBeenCalledWith('项目字段校验失败', 'danger')
    expect(mocks.cancelResourceBatch).not.toHaveBeenCalled()

    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', ctrlKey: true }))
    await flushPromises()
    expect(mocks.createProject).toHaveBeenCalledTimes(2)
    expect(mocks.createProject.mock.calls[0]?.[0]).toMatchObject({
      authorization_expires_at: null,
      resource_batch_id: 'batch-created',
    })
    expect(mocks.createProject.mock.calls[1]?.[0]).toMatchObject({
      authorization_expires_at: null,
      resource_batch_id: 'batch-created',
    })
    expect(mocks.createResourceBatch).toHaveBeenCalledOnce()
    expect(mocks.uploadResourceFile).toHaveBeenCalledOnce()
  })

  it('requires selecting a project group before creation', async () => {
    await mountPage()

    expect(buttonByText('确认创建项目')?.disabled).toBe(true)
    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', ctrlKey: true }))
    await flushPromises()
    expect(mocks.createProject).not.toHaveBeenCalled()
    expect(mocks.uiStore.showToast).toHaveBeenCalledWith('请选择有效的项目组', 'danger')
  })

  it('blocks creation while project groups are loading', async () => {
    mocks.queryState = { 'project-groups': { loading: true } }
    await mountPage()
    expect(document.querySelector('[aria-label="正在加载项目组"]')).not.toBeNull()
    expect(buttonByText('确认创建项目')?.disabled).toBe(true)
    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', ctrlKey: true }))
    await flushPromises()
    expect(mocks.createProject).not.toHaveBeenCalled()
    expect(mocks.uiStore.showToast).toHaveBeenCalledWith('项目组列表仍在加载，请稍后重试', 'danger')
  })

  it('blocks creation when project groups are unavailable', async () => {
    mocks.queryState = { 'project-groups': { error: true } }
    await mountPage()
    expect(document.body.textContent).toContain('项目组加载失败')
    expect(buttonByText('确认创建项目')?.disabled).toBe(true)
    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', ctrlKey: true }))
    await flushPromises()
    expect(mocks.createProject).not.toHaveBeenCalled()
    expect(mocks.uiStore.showToast).toHaveBeenCalledWith('无法确认可用项目组，请重试加载', 'danger')
  })

  it('blocks creation when the tenant has no project groups', async () => {
    mocks.groups = []
    await mountPage()

    expect(document.body.textContent).toContain('暂无可用项目组')
    expect(buttonByText('确认创建项目')?.disabled).toBe(true)
  })
})
