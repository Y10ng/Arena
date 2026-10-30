import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { ref, unref } from 'vue'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { api, ApiError } from '@/api/client'
import { onBeforeRouteLeave } from 'vue-router'
import NewProject from './NewProject.vue'
import TestEgressFields from '@/components/patterns/TestEgressFields.vue'
import CreationRequestRecovery from './CreationRequestRecovery.vue'
import type { ProjectSchedule } from '@/api/projectSchedules'

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
  libraries: [{ key: 'library-a', name: '授权资料', latest_version: 2, file_count: 3 }],
  queryState: {} as Record<string, { loading?: boolean; fetching?: boolean; error?: boolean }>,
  queryData: {} as Record<string, { value: unknown }>,
  queryFlags: {} as Record<
    string,
    { isLoading: { value: boolean }; isFetching: { value: boolean } }
  >,
  createProject: vi.fn(),
  createResourceBatch: vi.fn(),
  getResourceConfig: vi.fn(),
  getResourceBatch: vi.fn(),
  uploadResourceFile: vi.fn(),
  cancelResourceBatch: vi.fn(),
  router: { push: vi.fn() },
  uiStore: { showToast: vi.fn() },
}))

vi.mock('@/api/client', async (importOriginal) => ({
  ApiError: (await importOriginal<typeof import('@/api/client')>()).ApiError,
  api: vi.fn(),
  cancelProjectResourceBatch: mocks.cancelResourceBatch,
  createProject: mocks.createProject,
  createProjectResourceBatch: mocks.createResourceBatch,
  deleteProjectResourceBatchMember: vi.fn(),
  getProjectResourceConfig: mocks.getResourceConfig,
  getProjectResourceBatch: mocks.getResourceBatch,
  importProjectResourceBatchZip: vi.fn(),
  listLibraryCatalog: vi.fn(),
  updateProjectResourceBatchMember: vi.fn(),
  uploadProjectResourceBatchFile: mocks.uploadResourceFile,
}))

vi.mock('@/stores/ui', () => ({
  useUIStore: () => mocks.uiStore,
}))

vi.mock('vue-router', async (importOriginal) => ({
  isNavigationFailure: (await importOriginal<typeof import('vue-router')>()).isNavigationFailure,
  onBeforeRouteLeave: vi.fn(),
  useRouter: () => mocks.router,
}))

vi.mock('@tanstack/vue-query', () => ({
  useQuery: ({ queryKey }: { queryKey: unknown }) => {
    const key = String((unref(queryKey) as unknown[])[0])
    const values: Record<string, unknown> = {
      'scenario-catalog': mocks.scenarios,
      'library-catalog': mocks.libraries,
    }
    const state = mocks.queryState[key] || {}
    const data = ref(values[key])
    mocks.queryData[key] = data
    const flags = {
      isLoading: ref(Boolean(state.loading)),
      isFetching: ref(Boolean(state.fetching ?? state.loading)),
    }
    mocks.queryFlags[key] = flags
    return {
      data,
      ...flags,
      isError: ref(Boolean(state.error)),
      refetch: vi.fn(),
    }
  },
}))

const wrappers: VueWrapper[] = []

async function mountPage(schedule?: ProjectSchedule) {
  const wrapper = mount(NewProject, {
    props: { open: true, returnFocus: null, schedule },
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

  vi.mocked(api).mockReset()
  mocks.queryState = {}
  mocks.queryData = {}
  mocks.queryFlags = {}
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
  mocks.uploadResourceFile
    .mockReset()
    .mockImplementation(async (_batchId: string, file: File, path: string) => ({
      id: 'member-1',
      kind: path.endsWith('.sh') ? 'script' : 'file',
      path,
      size_bytes: file.size,
      sha256: 'a'.repeat(64),
      source: 'file',
    }))
  mocks.cancelResourceBatch.mockReset().mockResolvedValue(undefined)
  mocks.getResourceBatch.mockReset()
  mocks.router.push.mockReset().mockResolvedValue(undefined)
  mocks.uiStore.showToast.mockReset()
})

afterEach(() => {
  wrappers.splice(0).forEach((wrapper) => wrapper.unmount())
  document.body.innerHTML = ''
  vi.restoreAllMocks()
  sessionStorage.clear()
})

describe('NewProject creation boundaries', () => {
  it('代理设置：即时创建默认直连，代理凭据仅随原请求重试而不写恢复缓存', async () => {
    mocks.createProject.mockRejectedValueOnce(new Error('连接断开'))
    const wrapper = await mountPage()
    const fields = wrapper.getComponent(TestEgressFields)
    expect(fields.props('modelValue').mode).toBe('direct')
    fields.vm.$emit('update:modelValue', { mode: 'socks5', host: 'proxy.example.test', port: 1080, auth_action: 'replace', username: 'sample-user', password: 'sample-password' })
    await setFieldValue('#np-title', '出口样本')
    buttonByText('创建并启动')!.click()
    await flushPromises()
    const payload = mocks.createProject.mock.calls[0]![0]
    expect(payload.test_egress).toEqual({ mode: 'socks5', host: 'proxy.example.test', port: 1080, auth_action: 'replace', username: 'sample-user', password: 'sample-password' })
    expect(payload.origin + payload.goal).not.toContain('sample-password')
    expect(sessionStorage.getItem('arena.creation-request')).not.toMatch(/sample-password|sample-user|test_egress/)
    buttonByText('用原请求恢复提交')!.click()
    await flushPromises()
    expect(mocks.createProject.mock.calls[1]![0]).toEqual(payload)
    expect(fields.props('modelValue').password).toBe('')
  })

  it('代理设置：单次计划创建携带HTTP出口，周期编辑只从公共值保留认证', async () => {
    const wrapper = await mountPage()
    await setFieldValue('#np-title', '单次出口')
    wrapper.getComponent(TestEgressFields).vm.$emit('update:modelValue', { mode: 'http', host: 'proxy.example.test', port: 8080, auth_action: 'keep', username: '', password: '' })
    await selectOption('schedule-mode', '单次定时')
    await setFieldValue('#schedule-start', '2099-04-05T09:00')
    vi.mocked(api).mockRejectedValue(new ApiError('受控拒绝', 422, {}))
    buttonByText('保存单次预约')!.click()
    await flushPromises()
    expect(api).toHaveBeenCalledWith('POST', '/project-schedules', expect.objectContaining({ project: expect.objectContaining({ test_egress: { mode: 'http', host: 'proxy.example.test', port: 8080, auth_action: 'keep' } }) }))
    wrapper.unmount()
    const plan = await mountPage({ id: 'egress-plan', tenant_id: 'tenant-test', creator_id: 'user-test', status: 'active', next_run_at: null, project_id: null, last_project_id: null, generated_count: 0, block_reason: null, created_at: '2026-10-26T00:00:00Z', updated_at: '2026-10-26T00:00:00Z', last_result: null, mode: 'daily', timezone: 'Asia/Shanghai', start_at: '2099-06-01T09:00:00+08:00', local_time: '09:00', weekdays: [], end_date: null, max_runs: null, version: 7, resources: [], library_bindings: [], project: { title: '出口计划', origin: '背景', goal: '目标', scenario: 'default', authorization_expires_at: null, test_egress: { mode: 'socks5', host: 'proxy.example.test', port: 1080, auth_configured: true, revision: 3 } } } as ProjectSchedule)
    expect(plan.getComponent(TestEgressFields).props('authConfigured')).toBe(true)
    expect(plan.getComponent(TestEgressFields).props('modelValue').password).toBe('')
    buttonByText('保存未来配置')!.click()
    await flushPromises()
    expect(api).toHaveBeenCalledWith('PUT', '/project-schedules/egress-plan', expect.objectContaining({ project: expect.objectContaining({ test_egress: { mode: 'socks5', host: 'proxy.example.test', port: 1080, auth_action: 'keep' } }) }))
  })

  it('不查询模板或 Worker，保留正文并由服务端确定执行配置', async () => {
    await mountPage()
    expect(Object.keys(mocks.queryData)).not.toContain('worker-templates')
    expect(Object.keys(mocks.queryData)).not.toContain('tenant-worker-options')
    expect(document.querySelector('#np-worker-template')).toBeNull()
    expect(document.body.textContent).not.toContain('执行设置')
    await setFieldValue('#np-title', '正文先填写')
    await setFieldValue('#np-origin-input', '  原始背景\n  保留格式  ')
    buttonByText('创建并启动')!.click()
    await flushPromises()
    expect(mocks.createProject).toHaveBeenCalledExactlyOnceWith(expect.objectContaining({ origin: '  原始背景\n  保留格式  ' }))
    expect(mocks.createProject.mock.calls[0]![0]).not.toHaveProperty('tenant_worker_ids')
  })

  it('代理设置默认收起，可独立展开且保留摘要与首错定位', async () => {
    await mountPage()
    const panel = document.querySelector<HTMLElement>('#np-proxy-settings')!
    const trigger = document.querySelector<HTMLButtonElement>('#np-proxy-trigger')!
    expect(panel.style.display).toBe('none')
    expect(trigger.textContent).toContain('代理设置')
    expect(trigger.textContent).toContain('直接连接')
    trigger.click()
    await flushPromises()
    expect(panel.style.display).not.toBe('none')
    expect(panel.querySelector('fieldset')?.getAttribute('aria-label')).toBe('代理设置')
    expect(panel.querySelector('legend')).toBeNull()
    const mode = panel.querySelector<HTMLElement>('[role="combobox"]')!
    await selectOption(mode.id, 'HTTP 代理')
    trigger.click()
    await flushPromises()
    expect(trigger.textContent).toContain('HTTP 代理')
    await setFieldValue('#np-title', '代理校验')
    buttonByText('创建并启动')!.click()
    await flushPromises()
    expect(mocks.createProject).not.toHaveBeenCalled()
    expect(panel.style.display).not.toBe('none')
    expect(document.body.textContent).toContain('请填写代理主机地址')
  })

  it.each(['daily', 'weekly'] as const)('旧 %s 计划期限在时间设置编辑，未改保值、清空取消且不提交 Worker IDs', async (mode) => {
    const authorization = '2099-07-01T10:30:20+08:00'
    const deadline = '2099-06-30T18:20:10+08:00'
    vi.mocked(api).mockRejectedValue(new ApiError('受控拒绝', 422, {}))
    await mountPage({
      id: 'retained-plan', tenant_id: 'tenant-test', creator_id: 'user-test',
      mode, timezone: 'Asia/Shanghai', start_at: '2099-06-01T09:00:00+08:00',
      local_time: '09:00', weekdays: mode === 'weekly' ? [1, 3] : [], end_date: null, max_runs: null, version: 7,
      status: 'active', next_run_at: '2099-06-01T09:00:00+08:00',
      project_id: null, last_project_id: null, generated_count: 0, block_reason: null,
      created_at: '2099-05-01T00:00:00Z', updated_at: '2099-05-01T00:00:00Z', last_result: null,
      resources: [], library_bindings: [], project: {
        title: '既有计划', origin: '历史背景', goal: '历史目标', scenario: 'default',
        authorization_expires_at: authorization, explore_deadline: deadline,
      },
    })
    expect(document.querySelector('#np-timing-settings #np-explore-deadline')).not.toBeNull()
    expect(document.querySelector('#np-proxy-settings #np-explore-deadline')).toBeNull()
    buttonByText('保存未来配置')!.click()
    await flushPromises()
    const first = vi.mocked(api).mock.calls.find(([method]) => method === 'PUT')![2] as { project: Record<string, unknown> }
    expect(first.project).toMatchObject({ authorization_expires_at: authorization, explore_deadline: deadline })
    expect(first.project).not.toHaveProperty('tenant_worker_ids')
    await setFieldValue('#np-explore-deadline', '')
    await setFieldValue('#np-authorization-expires-at', '')
    buttonByText('保存未来配置')!.click()
    await flushPromises()
    expect(api).toHaveBeenLastCalledWith('PUT', '/project-schedules/retained-plan', expect.objectContaining({
      project: expect.objectContaining({ authorization_expires_at: null, explore_deadline: null }), version: 7,
    }))
  })

  it('缺少模型的明确拒绝保留草稿，并提供新页配置入口', async () => {
    mocks.createProject.mockRejectedValueOnce(new ApiError('Worker explore (worker-explore) 不可用：model_unconfigured', 422, {}))
    await mountPage()
    await setFieldValue('#np-title', '等待配置')
    await setFieldValue('#np-origin-input', '保留原始背景')
    buttonByText('创建并启动')!.click()
    await flushPromises()
    expect(document.querySelector<HTMLInputElement>('#np-title')?.value).toBe('等待配置')
    expect(document.querySelector<HTMLTextAreaElement>('#np-origin-input')?.value).toBe('保留原始背景')
    expect(document.querySelector('a[target="_blank"]')?.textContent).toContain('前往 Worker 配置')
    expect(document.body.textContent).not.toContain('保存结果未确认')
  })

  it('reports a random source failure before any request and allows retry', async () => {
    const random = vi.spyOn(crypto, 'getRandomValues').mockImplementationOnce(() => { throw new Error('随机源不可用') })
    await mountPage()
    await setFieldValue('#np-title', '本地失败')
    buttonByText('创建并启动')!.click()
    await flushPromises()
    expect(mocks.createProject).not.toHaveBeenCalled()
    expect(sessionStorage.getItem('arena.creation-request')).toBeNull()
    expect(document.body.textContent).toContain('请求尚未发出：随机源不可用')
    expect(document.body.textContent).not.toContain('保存结果未确认')
    expect(buttonByText('创建并启动')?.disabled).toBe(false)
    random.mockRestore()
    buttonByText('创建并启动')!.click()
    await flushPromises()
    expect(mocks.createProject).toHaveBeenCalledOnce()
    expect(mocks.createProject.mock.calls[0]![0].request_id).toMatch(/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/)
  })

  it('keeps the original unknown payload when optional cache writes fail', async () => {
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new Error('缓存写入不可用') })
    mocks.createProject.mockRejectedValueOnce(new Error('连接断开'))
    await mountPage()
    await setFieldValue('#np-title', '缓存不可用')
    buttonByText('创建并启动')!.click()
    await flushPromises()
    const original = JSON.parse(JSON.stringify(mocks.createProject.mock.calls[0]![0]))
    expect(document.body.textContent).toContain('跨刷新查询入口未保存')
    expect(document.body.textContent).toContain('连接断开')
    expect(buttonByText('用原请求恢复提交')?.disabled).toBe(false)
    buttonByText('用原请求恢复提交')!.click()
    await flushPromises()
    expect(mocks.createProject.mock.calls.map(([payload]) => payload)).toEqual([original, original])
    expect(document.body.textContent).toContain('项目已创建，等待调度')
    expect(document.body.textContent).not.toContain('保存结果未确认')
  })

  it('keeps confirmed success when cache cleanup and success feedback fail', async () => {
    vi.spyOn(Storage.prototype, 'removeItem').mockImplementation(() => { throw new Error('清理不可用') })
    mocks.uiStore.showToast.mockImplementationOnce(() => { throw new Error('反馈不可用') })
    await mountPage()
    await setFieldValue('#np-title', '已确认成功')
    buttonByText('创建并启动')!.click()
    await flushPromises()
    expect(document.body.textContent).toContain('项目已创建，等待调度')
    expect(document.body.textContent).toContain('请求引用缓存清理未完成')
    expect(document.body.textContent).toContain('已确认保存，后续页面反馈未完成：反馈不可用')
    expect(document.body.textContent).not.toContain('保存结果未确认')
    expect(buttonByText('取消')?.disabled).toBe(false)
    buttonByText('进入已创建项目')!.click()
    await flushPromises()
    expect(mocks.router.push).toHaveBeenLastCalledWith('/projects/project-created')
    expect(mocks.createProject).toHaveBeenCalledOnce()
  })

  it.each([{ targets: null }, { targets: [] }, { targets: [{ type: 'url', value: 'https://example.test/path?x=1', label: '原始附加字段' }] }])('preserves stored targets when editing a recurring plan: %j', async ({ targets }) => {
    vi.mocked(api).mockRejectedValue(new ApiError('受控编辑拒绝', 422, {}))
    const wrapper = await mountPage({
      id: 'history', mode: 'daily', timezone: 'Asia/Shanghai', start_at: '2099-06-01T09:00:00+08:00',
      local_time: '09:00', weekdays: [], end_date: null, max_runs: null, version: 7,
      resources: [], library_bindings: [], project: {
        title: '既有计划', origin: '历史背景', goal: '历史目标', scenario: 'default',
        targets,
      },
    } as unknown as ProjectSchedule)
    if (targets === null) expect(wrapper.findComponent({ name: 'ScheduleTiming' }).props('project')).not.toHaveProperty('targets')
    await setFieldValue('#np-title', '仅修改标题')
    expect(document.querySelector('#np-targets')).toBeNull()
    buttonByText('保存未来配置')!.click()
    await flushPromises()
    expect(api).toHaveBeenCalledWith('PUT', '/project-schedules/history', expect.objectContaining({ version: 7, project: expect.objectContaining({ title: '仅修改标题', targets }) }))
  })

  it.each(['{bad json', '[{"request_id":"broken","operation":"project"}]'])('clears damaged cache and shows its warning in the refresh recovery entry: %s', async (raw) => {
    sessionStorage.setItem('arena.creation-request', raw)
    const wrapper = mount(CreationRequestRecovery)
    wrappers.push(wrapper)
    await flushPromises()
    expect(wrapper.text()).toContain('请求引用缓存已损坏并清除')
    expect(sessionStorage.getItem('arena.creation-request')).toBeNull()
    expect(api).not.toHaveBeenCalled()
  })

  it('shows optional cache read failures without failing the recovery entry', async () => {
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => { throw new Error('读取不可用') })
    const wrapper = mount(CreationRequestRecovery)
    wrappers.push(wrapper)
    await flushPromises()
    expect(wrapper.text()).toContain('请求引用缓存不可用')
    expect(wrapper.text()).toContain('读取不可用')
    expect(api).not.toHaveBeenCalled()
  })
})

describe('NewProject shared control migration', () => {
  it('focuses the title, selects the default scenario, preserves touched scenario templates', async () => {
    await mountPage()

    expect(document.activeElement).toBe(document.querySelector('#np-title'))
    expect(document.querySelector<HTMLTextAreaElement>('#np-origin-input')?.value).toBe(
      'default origin\npublic environment context',
    )
    expect(document.querySelector('button[aria-pressed="true"]')?.textContent).toContain('默认场景')
    expect(document.querySelector('#np-targets-input')).toBeNull()
    expect(document.body.textContent).toContain('Origin（已知信息）')
    expect(document.body.textContent).toContain('Goal（目标）')
    expect(document.body.textContent).not.toContain('入口凭证')

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
    const scenarios = document.querySelector<HTMLDetailsElement>('.scenario-settings')!
    expect(scenarios.open).toBe(true)
    scenarios.open = false
    scenarios.dispatchEvent(new Event('toggle'))
    await flushPromises()
    mocks.queryData['scenario-catalog'].value = mocks.scenarios.map((scenario) => ({
      ...scenario,
      goal_template: `${scenario.goal_template} refreshed`,
    }))
    await flushPromises()
    expect(scenarios.open).toBe(false)
    expect(origin.value).toBe('user supplied origin')
    expect(document.querySelector('button[aria-pressed="true"]')?.textContent).toContain('次要场景')
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

  it('submits address-free Origin and Goal without targets and includes the uploaded resource batch id', async () => {
    await mountPage()

    await setFieldValue('#np-title', '  Workspace title  ')
    await setFieldValue(
      '#np-origin-input',
      '已有接口说明和两个测试角色，重点关注订单审批相关功能。',
    )
    await setFieldValue('#np-goal-input', '重点检查普通用户之间的数据隔离及审批流程。')

    buttonByText('从资料库选择')?.click()
    await flushPromises()
    buttonByText('授权资料')?.click()
    buttonByText('完成选择')?.click()
    await flushPromises()

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
      test_egress: { mode: 'direct', auth_action: 'clear' },
      title: 'Workspace title',
      origin: '已有接口说明和两个测试角色，重点关注订单审批相关功能。',
      goal: '重点检查普通用户之间的数据隔离及审批流程。',
      scenario: 'default',
      request_id: expect.any(String),
      explore_deadline: null,
      authorization_expires_at: null,
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
    expect(document.querySelector('#np-explore-deadline')).toBeNull()
    expect(document.querySelector('#np-authorization-expires-at')).toBeNull()
    expect(document.body.textContent).not.toContain('执行边界')
    expect(document.body.textContent).not.toContain('明确确认真实授权无限期有效')
    expect(document.querySelector('input[aria-label="授权无限期有效"]')).toBeNull()

    await setFieldValue('#np-title', 'Unlimited authorization')
    buttonByText('创建并启动')?.click()
    await flushPromises()

    expect(mocks.createProject).toHaveBeenCalledOnce()
    expect(mocks.createProject).toHaveBeenCalledWith({
      test_egress: { mode: 'direct', auth_action: 'clear' },
      title: 'Unlimited authorization',
      request_id: expect.any(String),
      explore_deadline: null,
      origin: 'default origin\npublic environment context',
      goal: 'default goal',
      scenario: 'default',
      authorization_expires_at: null,
      library_keys: [],
    })
    expect(mocks.router.push).toHaveBeenCalledWith('/projects/project-created')
  })

  it('validates the optional once stop in the schedule timezone and ignores it in immediate mode', async () => {
    vi.mocked(api).mockRejectedValue(new ApiError('受控计划拒绝', 422, {}))
    await mountPage()
    await setFieldValue('#np-title', 'Independent stop time')
    await selectOption('schedule-mode', '单次定时')
    await setFieldValue('#schedule-timezone', 'Asia/Shanghai')
    await setFieldValue('#schedule-start', '2035-04-05T09:00')
    await selectOption('schedule-stop-mode', '指定停止时间')
    await setFieldValue('#schedule-stop', '2035-04-05T09:00')
    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', ctrlKey: true }))
    await flushPromises()
    expect(api).not.toHaveBeenCalledWith('POST', '/project-schedules', expect.anything())
    expect(document.body.textContent).toContain('停止时间必须严格晚于启动时间')
    await setFieldValue('#schedule-stop', '2035-04-05T10:30')
    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', ctrlKey: true }))
    await flushPromises()
    expect(api).toHaveBeenCalledWith('POST', '/project-schedules', expect.objectContaining({
      start_at: '2035-04-05T09:00:00+08:00',
      project: expect.objectContaining({ explore_deadline: '2035-04-05T10:30:00+08:00', authorization_expires_at: null }),
    }))
    await selectOption('schedule-mode', '立即启动')
    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', ctrlKey: true }))
    await flushPromises()
    expect(mocks.createProject).toHaveBeenCalledWith(expect.objectContaining({ explore_deadline: null, authorization_expires_at: null }))
  })

  it('keeps an uploaded batch after project validation fails so submission can retry', async () => {
    mocks.createProject
      .mockRejectedValueOnce(new ApiError('项目字段校验失败', 422, {}))
      .mockResolvedValueOnce({ project: { id: 'project-created' } })
    await mountPage()

    await setFieldValue('#np-title', 'Retry project')

    const fileInput = document.querySelectorAll<HTMLInputElement>('input[type="file"]')[0]
    Object.defineProperty(fileInput, 'files', {
      configurable: true,
      value: [new File(['binary'], 'evidence.bin')],
    })
    fileInput.dispatchEvent(new Event('change', { bubbles: true }))
    await flushPromises()

    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', ctrlKey: true }))
    await flushPromises()
    expect(document.body.textContent).toContain('项目字段校验失败')
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

  it.each([
    new Error('连接断开'),
    new ApiError('服务异常', 503, {}),
    new ApiError('请求超时', 408, {}),
  ])('keeps an unconfirmed creation protected after %s', async (error) => {
    mocks.createProject.mockRejectedValueOnce(error)
    const wrapper = await mountPage()
    await setFieldValue('#np-title', 'Unconfirmed project')

    const fileInput = document.querySelector<HTMLInputElement>('input[type="file"]')!
    Object.defineProperty(fileInput, 'files', {
      configurable: true,
      value: [new File(['binary'], 'evidence.bin')],
    })
    fileInput.dispatchEvent(new Event('change', { bubbles: true }))
    await flushPromises()
    buttonByText('创建并启动')?.click()
    await flushPromises()

    expect(document.body.textContent).toContain('保存结果未确认')
    expect(document.querySelector<HTMLInputElement>('#np-title')?.value).toBe('Unconfirmed project')
    expect(document.body.textContent).toContain('evidence.bin')
    expect(buttonByText('创建并启动')?.disabled).toBe(true)
    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', ctrlKey: true }))
    buttonByText('取消')?.click()
    await flushPromises()
    expect(mocks.createProject).toHaveBeenCalledOnce()
    expect(mocks.cancelResourceBatch).not.toHaveBeenCalled()
    expect(wrapper.emitted('discarded')).toBeUndefined()
    expect(wrapper.emitted('update:open')).toContainEqual([false])
  })

  it('restores an unknown creation with the original request and allows normal navigation', async () => {
    mocks.createProject.mockRejectedValueOnce(new Error('连接断开'))
    await mountPage()
    await setFieldValue('#np-title', 'Recover original request')
    buttonByText('创建并启动')?.click()
    await flushPromises()

    const original = mocks.createProject.mock.calls[0]![0]
    expect(original).not.toHaveProperty('tenant_worker_ids')
    await flushPromises()
    expect(original.request_id).toMatch(/^[0-9a-f-]{36}$/i)
    const references = JSON.parse(sessionStorage.getItem('arena.creation-request')!)
    expect(references).toEqual([{ request_id: original.request_id, operation: 'project' }])
    const leave = vi.mocked(onBeforeRouteLeave).mock.calls.at(-1)![0] as () => unknown
    expect(leave()).toBe(true)

    vi.mocked(api).mockResolvedValueOnce({
      request_id: original.request_id,
      operation: 'project',
      status: 'unknown',
      project_id: null,
      schedule_id: null,
    })
    buttonByText('查询请求结果')?.click()
    await flushPromises()
    expect(api).toHaveBeenLastCalledWith(
      'GET',
      `/creation-requests/${original.request_id}?operation=project`,
    )
    expect(document.body.textContent).toContain('尚未查到已提交结果，不代表请求失败')
    expect(mocks.createProject).toHaveBeenCalledOnce()

    buttonByText('用原请求恢复提交')?.click()
    await flushPromises()
    expect(mocks.createProject).toHaveBeenCalledTimes(2)
    expect(mocks.createProject.mock.calls[1]![0]).toEqual(original)
    expect(mocks.cancelResourceBatch).not.toHaveBeenCalled()
    expect(sessionStorage.getItem('arena.creation-request')).toBeNull()
    expect(mocks.router.push).toHaveBeenLastCalledWith('/projects/project-created')
  })

  it('keeps a created project entry after navigation fails without creating or cancelling again', async () => {
    mocks.router.push.mockRejectedValueOnce(new Error('导航不可用'))
    await mountPage()
    await setFieldValue('#np-title', 'Created project')
    buttonByText('创建并启动')?.click()
    await flushPromises()

    expect(document.body.textContent).toContain('项目已创建，等待调度')
    expect(document.body.textContent).toContain('跳转失败：导航不可用')
    expect(buttonByText('创建并启动')?.disabled).toBe(true)
    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', ctrlKey: true }))
    buttonByText('进入已创建项目')?.click()
    await flushPromises()
    expect(mocks.createProject).toHaveBeenCalledOnce()
    expect(mocks.cancelResourceBatch).not.toHaveBeenCalled()
    expect(mocks.router.push).toHaveBeenCalledTimes(2)
    expect(mocks.router.push).toHaveBeenLastCalledWith('/projects/project-created')
  })

  it('retains the form and batch when cancellation fails and retries only cleanup', async () => {
    mocks.cancelResourceBatch.mockRejectedValueOnce(new ApiError('清理暂不可用', 503, {}))
    const wrapper = await mountPage()
    await setFieldValue('#np-title', 'Keep after cleanup failure')
    const fileInput = document.querySelector<HTMLInputElement>('input[type="file"]')!
    Object.defineProperty(fileInput, 'files', {
      configurable: true,
      value: [new File(['binary'], 'evidence.bin')],
    })
    fileInput.dispatchEvent(new Event('change', { bubbles: true }))
    await flushPromises()
    buttonByText('取消')?.click()
    await flushPromises()
    expect(document.body.textContent).toContain('放弃本次填写？')
    buttonByText('放弃并关闭')?.click()
    await flushPromises()
    expect(document.body.textContent).toContain('临时批次取消失败')
    expect(document.body.textContent).toContain('清理暂不可用')
    expect(wrapper.emitted('discarded')).toBeUndefined()
    expect(document.querySelector<HTMLInputElement>('#np-title')?.value).toBe(
      'Keep after cleanup failure',
    )
    expect(mocks.router.push).not.toHaveBeenCalled()

    buttonByText('放弃并关闭')?.click()
    await flushPromises()
    expect(mocks.cancelResourceBatch).toHaveBeenCalledTimes(2)
    expect(mocks.cancelResourceBatch).toHaveBeenLastCalledWith('batch-created')
    expect(wrapper.emitted('discarded')).toEqual([[]])
    expect(mocks.createProject).not.toHaveBeenCalled()
  })

  it('allows only one in-flight creation and blocks closing until its response', async () => {
    let resolveCreation!: (value: { project: { id: string } }) => void
    mocks.createProject.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          resolveCreation = resolve
        }),
    )
    const wrapper = await mountPage()
    await setFieldValue('#np-title', 'Single request')
    buttonByText('创建并启动')?.click()
    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', ctrlKey: true }))
    await flushPromises()
    expect(mocks.createProject).toHaveBeenCalledOnce()
    expect(buttonByText('取消')?.disabled).toBe(true)
    document
      .querySelector<HTMLElement>('[role="dialog"]')
      ?.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))
    await flushPromises()
    expect(wrapper.emitted('discarded')).toBeUndefined()
    expect(wrapper.emitted('update:open')).toBeUndefined()

    resolveCreation({ project: { id: 'project-created' } })
    await flushPromises()
    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', ctrlKey: true }))
    await flushPromises()
    expect(mocks.createProject).toHaveBeenCalledOnce()
    expect(mocks.router.push).toHaveBeenCalledWith('/projects/project-created')
  })

  it('creates without loading, selecting, or submitting project groups', async () => {
    await mountPage()
    await setFieldValue('#np-title', '无组项目')
    expect(document.querySelector('#np-group-select')).toBeNull()
    expect(mocks.queryData['project-groups']).toBeUndefined()
    expect(buttonByText('创建并启动')?.disabled).toBe(false)
    buttonByText('创建并启动')!.click()
    await flushPromises()
    expect(mocks.createProject).toHaveBeenCalledOnce()
    expect(mocks.createProject.mock.calls[0]![0]).not.toHaveProperty('group_id')
  })
})
