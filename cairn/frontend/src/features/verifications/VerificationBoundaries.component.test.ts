import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { defineComponent, h } from 'vue'
import { QueryClient, VueQueryPlugin } from '@tanstack/vue-query'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiError } from '@/api/client'
import NewVerification from './NewVerification.vue'
import VerificationIntegrations from './VerificationIntegrations.vue'
import VulnerabilityDrawer from '@/features/vulnerabilities/VulnerabilityDrawer.vue'

const mocks = vi.hoisted(() => ({
  route: { query: {} as Record<string, string> },
  auth: { session: { tenant_id: 'tenant-a' }, isSuperAdmin: true },
  push: vi.fn(), leave: vi.fn(),
  createRequest: vi.fn(), readiness: vi.fn(), createBatch: vi.fn(),
  listVulnerabilities: vi.fn(), preview: vi.fn(), history: vi.fn(),
  configuration: vi.fn(), options: vi.fn(), tenants: vi.fn(),
  project: vi.fn(), vulnerability: vi.fn(), duplicates: vi.fn(),
}))
vi.mock('vue-router', async (original) => ({
  ...await original<typeof import('vue-router')>(),
  useRoute: () => mocks.route,
  useRouter: () => ({ push: mocks.push }),
  onBeforeRouteLeave: mocks.leave,
  onBeforeRouteUpdate: vi.fn(),
}))
vi.mock('@/stores/auth', () => ({ useAuthStore: () => mocks.auth }))
vi.mock('@/stores/ui', () => ({ useUIStore: () => ({ showToast: vi.fn() }) }))
vi.mock('@/api/client', async (original) => ({
  ...await original<typeof import('@/api/client')>(),
  listAllVulnerabilities: mocks.listVulnerabilities,
  getProjectDetail: mocks.project,
  getVulnerability: mocks.vulnerability,
  getDuplicateVulnerabilityCandidates: mocks.duplicates,
}))
vi.mock('@/api/verificationRequests', async (original) => ({
  ...await original<typeof import('@/api/verificationRequests')>(),
  createVerificationRequest: mocks.createRequest,
  getVerificationReadiness: mocks.readiness,
  createVerificationBatch: mocks.createBatch,
  getVerificationSourcePreview: mocks.preview,
  getVerificationSourceHistory: mocks.history,
  getVerificationConfiguration: mocks.configuration,
  getVerificationConfigurationOptions: mocks.options,
  listVerificationTenants: mocks.tenants,
}))

const SelectStub = defineComponent({
  props: ['modelValue', 'options', 'disabled'],
  emits: ['update:modelValue'],
  setup: (props, { emit }) => () => h('select', {
    value: props.modelValue, disabled: props.disabled,
    onChange: (event: Event) => emit('update:modelValue', (event.target as HTMLSelectElement).value),
  }, (props.options || []).map((option: { value: string; label: string }) => h('option', { value: option.value }, option.label))),
})
const OverlayStub = { template: '<div><slot name="title" /><slot name="trigger" /><slot /><slot name="footer" /></div>' }
const wrappers: VueWrapper[] = []
const clients: QueryClient[] = []
async function mountPage(component: typeof NewVerification | typeof VerificationIntegrations | typeof VulnerabilityDrawer, props = {}) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } })
  clients.push(client)
  const wrapper = mount(component, {
    props, attachTo: document.body,
    global: {
      plugins: [[VueQueryPlugin, { queryClient: client }]],
      stubs: {
        UiDialog: OverlayStub, UiDrawer: OverlayStub, UiPopover: OverlayStub, UiSelect: SelectStub,
        RouterLink: { template: '<a><slot /></a>' },
        MonacoEditor: true, MarkdownPreview: true,
      },
    },
  })
  wrappers.push(wrapper)
  await flushPromises()
  return wrapper
}
async function click(wrapper: VueWrapper, text: string) {
  const button = wrapper.findAll('button').find((item) => item.text() === text)
  expect(button, text).toBeTruthy()
  await button!.trigger('click')
  await flushPromises()
}
async function fillManual(wrapper: VueWrapper) {
  await wrapper.get('#new-verification-title').setValue('本地交互检查')
  await wrapper.get('#new-verification-content').setValue('原正文，保留换行\n第二行')
  await wrapper.get('#new-verification-origin').setValue('仅本地工程资料，不访问目标')
}
async function submit(wrapper: VueWrapper) {
  await wrapper.get('#new-verification-form').trigger('submit')
  await flushPromises()
}
const expiredError = () => new ApiError('Project resource batch expired', 410, { detail: 'Project resource batch expired' })
const saved = {
  tenant_id: 'tenant-a', model_id: 'model-a', display_name: '已保存名称', engine: 'pi',
  max_running: 1, priority: 100, enabled: true, ready: true, message: '本地工程配置',
}
const batch = {
  id: 'batch-a', state: 'open', expires_at: '2099-01-01T00:00:00Z',
  members: [{ id: 'member-a', path: 'report.md', source_resource_id: 'resource-a' }],
}

beforeEach(() => {
  vi.clearAllMocks()
  mocks.route.query = { input: 'manual' }
  mocks.push.mockResolvedValue(undefined)
  mocks.readiness.mockResolvedValue({ ready: true, worker_name: '本地配置' })
  mocks.listVulnerabilities.mockResolvedValue([{ id: 'vuln-a', project_id: 'project-a', project_title: '当前租户项目', title: '当前租户漏洞' }])
  mocks.createBatch.mockResolvedValue(batch)
  mocks.configuration.mockResolvedValue(saved)
  mocks.options.mockResolvedValue({ models: [{ id: 'model-a', display_name: '本地模型', model_name: 'local', enabled: true }], engines: [{ id: 'pi', name: 'pi' }], defaults: saved })
  mocks.tenants.mockResolvedValue([{ id: 'tenant-a', name: '当前租户' }])
  mocks.project.mockResolvedValue({ project: { id: 'project-a', tenant_id: 'tenant-a' } })
  mocks.vulnerability.mockResolvedValue({ id: 'vuln-a', project_id: 'project-a', title: '全局成果原文', severity: 'high', status: 'new', confidence: 0.8, observations: [], verifications: [], run_artifacts: [] })
  mocks.duplicates.mockResolvedValue({ project_id: 'project-a', requested_vulnerability_id: 'vuln-a', candidates: [] })
  mocks.history.mockResolvedValue({ items: [], total: 0 })
})
afterEach(() => {
  wrappers.splice(0).forEach((wrapper) => wrapper.unmount())
  clients.splice(0).forEach((client) => client.clear())
  vi.restoreAllMocks()
  document.body.innerHTML = ''
})

describe('复测收尾交互边界', () => {
  it('首次响应未知后403不解锁，权威重放仍使用原ID和原正文进入原详情', async () => {
    mocks.createRequest.mockRejectedValueOnce(new TypeError('response lost'))
      .mockRejectedValueOnce(new ApiError('Forbidden', 403, { detail: 'Forbidden' }))
      .mockResolvedValueOnce({ id: 'accepted-a', execution_status: 'queued' })
    const wrapper = await mountPage(NewVerification)
    await fillManual(wrapper)
    await submit(wrapper)
    const first = mocks.createRequest.mock.calls[0]![0]
    await click(wrapper, '用原请求重试')
    expect(wrapper.text()).toContain('请求可能已受理')
    expect(wrapper.text()).not.toContain('修改未受理的输入')
    expect(wrapper.get('fieldset').attributes('disabled')).toBeDefined()
    expect(mocks.leave.mock.calls[0]![0]()).toBe(false)
    expect(mocks.createRequest.mock.calls[1]![0]).toEqual(first)
    await click(wrapper, '用原请求重试')
    expect(mocks.createRequest.mock.calls[2]![0]).toEqual(first)
    expect(mocks.push).toHaveBeenCalledWith(expect.objectContaining({ name: 'VerificationDetail', params: { id: 'accepted-a' } }))
  })

  it('首次明确拒绝可以编辑，不把其他410当作批次未受理证明', async () => {
    mocks.createRequest.mockRejectedValueOnce(new ApiError('Forbidden', 403, {}))
      .mockRejectedValueOnce(new ApiError('Gateway gone', 410, { detail: 'Gateway gone' }))
    const wrapper = await mountPage(NewVerification)
    await fillManual(wrapper)
    await submit(wrapper)
    await click(wrapper, '修改未受理的输入')
    expect(wrapper.get('fieldset').attributes('disabled')).toBeUndefined()
    await submit(wrapper)
    expect(wrapper.text()).toContain('请求可能已受理')
    expect(wrapper.text()).not.toContain('更换过期资料并继续编辑')
  })

  it('服务端确认批次过期后保留正文授权并恢复换批次，包括此前响应未知', async () => {
    mocks.createRequest.mockRejectedValueOnce(new TypeError('response lost')).mockRejectedValueOnce(expiredError())
    const wrapper = await mountPage(NewVerification)
    await fillManual(wrapper)
    wrapper.findComponent({ name: 'VerificationMaterials' }).vm.$emit('update:modelValue', batch)
    await flushPromises()
    await submit(wrapper)
    await click(wrapper, '用原请求重试')
    await click(wrapper, '更换过期资料并继续编辑')
    expect(wrapper.get('fieldset').attributes('disabled')).toBeUndefined()
    expect((wrapper.get('#new-verification-content').element as HTMLTextAreaElement).value).toBe('原正文，保留换行\n第二行')
    expect((wrapper.get('#new-verification-origin').element as HTMLTextAreaElement).value).toBe('仅本地工程资料，不访问目标')
    const materials = wrapper.findComponent({ name: 'VerificationMaterials' })
    expect(materials.props('modelValue')).toBeNull()
    materials.vm.$emit('update:modelValue', { ...batch, id: 'batch-b' })
    mocks.createRequest.mockResolvedValueOnce({ id: 'accepted-b', execution_status: 'queued' })
    await submit(wrapper)
    expect(mocks.createRequest.mock.calls[2]![0].resource_batch_id).toBe('batch-b')
  })

  it('复测候选明确请求当前租户范围，不复用全局候选缓存', async () => {
    mocks.route.query = { input: 'platform' }
    const wrapper = await mountPage(NewVerification)
    expect(mocks.listVulnerabilities).toHaveBeenCalledWith(undefined, undefined, expect.any(AbortSignal), true)
    expect(wrapper.get('#verification-source-vulnerability').text()).toContain('当前租户漏洞')
  })

  it('延迟重读期间全部执行输入锁定，取消放弃确认和重读失败都保留编辑', async () => {
    const wrapper = await mountPage(VerificationIntegrations)
    await wrapper.get('#verification-config-name').setValue('未保存名称')
    const confirm = vi.spyOn(window, 'confirm').mockReturnValueOnce(false).mockReturnValue(true)
    await click(wrapper, '重新读取已保存配置')
    expect(mocks.configuration).toHaveBeenCalledTimes(1)
    let rejectRead!: (reason: Error) => void
    mocks.configuration.mockImplementationOnce(() => new Promise((_, reject) => { rejectRead = reject }))
    await click(wrapper, '重新读取已保存配置')
    for (const selector of ['#verification-config-model', '#verification-config-name', '#verification-config-engine', '#verification-config-running', '#verification-config-priority', 'input[type="checkbox"]']) {
      expect(wrapper.get(selector).attributes('disabled'), selector).toBeDefined()
    }
    rejectRead(new Error('本地读取失败'))
    await flushPromises()
    expect(wrapper.text()).toContain('本地读取失败')
    expect((wrapper.get('#verification-config-name').element as HTMLInputElement).value).toBe('未保存名称')
    expect(wrapper.get('#verification-config-name').attributes('disabled')).toBeUndefined()
    expect(confirm).toHaveBeenCalledTimes(2)
  })

  it('全局成果的其他租户抽屉保留查看，但不请求复测历史或提供复测入口', async () => {
    mocks.project.mockResolvedValue({ project: { id: 'project-a', tenant_id: 'tenant-b' } })
    const wrapper = await mountPage(VulnerabilityDrawer, { projectId: 'project-a', vulnId: 'vuln-a', open: true })
    expect(wrapper.text()).toContain('全局成果原文')
    expect(wrapper.text()).toContain('复测及关联历史仅适用于当前租户')
    expect(wrapper.text()).not.toContain('复测此漏洞')
    expect(mocks.history).not.toHaveBeenCalled()
  })

  it('当前租户抽屉可查历史并携带原项目漏洞进入复测', async () => {
    const wrapper = await mountPage(VulnerabilityDrawer, { projectId: 'project-a', vulnId: 'vuln-a', open: true })
    expect(mocks.history).toHaveBeenCalledWith('project-a', 'vuln-a', 10, 0, expect.any(AbortSignal))
    await click(wrapper, '复测此漏洞')
    expect(mocks.push).toHaveBeenCalledWith({ name: 'NewVerification', query: { source_project_id: 'project-a', source_vulnerability_id: 'vuln-a', input: 'platform' } })
  })
})
