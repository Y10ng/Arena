import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { defineComponent, h, reactive } from 'vue'
import { QueryClient, VueQueryPlugin } from '@tanstack/vue-query'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { AuthSession } from '@/api/auth'
import type { VerificationConfiguration, VerificationConfigurationOptions } from '@/api/verificationRequests'
import router from '@/router'
import { commandItemsFromRegistry, sidebarNavigation } from '@/router/navigationRegistry'
import VerificationIntegrations from './VerificationIntegrations.vue'
import VerificationForm from './VerificationForm.vue'
import Verifications from './Verifications.vue'

const mocks = vi.hoisted(() => ({
  auth: {} as { session: AuthSession | null; authGeneration: number; isSuperAdmin: boolean },
  route: {} as { name: string; query: Record<string, string | string[]> },
  push: vi.fn(), leave: vi.fn(), update: vi.fn(), api: vi.fn(),
  configuration: vi.fn(), options: vi.fn(), tenants: vi.fn(), saveConfiguration: vi.fn(),
  integrations: vi.fn(), createIntegration: vi.fn(), updateIntegration: vi.fn(), disableIntegration: vi.fn(), rotateIntegration: vi.fn(),
  readiness: vi.fn(), vulnerabilities: vi.fn(), requests: vi.fn(), createRequest: vi.fn(), request: vi.fn(), createBatch: vi.fn(),
}))
vi.mock('vue-router', async (original) => ({
  ...await original<typeof import('vue-router')>(),
  useRoute: () => mocks.route,
  useRouter: () => ({ push: mocks.push }),
  onBeforeRouteLeave: mocks.leave,
  onBeforeRouteUpdate: mocks.update,
}))
vi.mock('@/stores/auth', () => ({ useAuthStore: () => mocks.auth }))
vi.mock('@/stores/ui', () => ({ useUIStore: () => ({ showToast: vi.fn() }) }))
vi.mock('@/api/client', async (original) => ({
  ...await original<typeof import('@/api/client')>(),
  api: mocks.api,
  listAllVulnerabilities: mocks.vulnerabilities,
}))
vi.mock('@/api/verificationRequests', async (original) => ({
  ...await original<typeof import('@/api/verificationRequests')>(),
  getVerificationConfiguration: mocks.configuration,
  getVerificationConfigurationOptions: mocks.options,
  listVerificationTenants: mocks.tenants,
  saveVerificationConfiguration: mocks.saveConfiguration,
  listVerificationIntegrations: mocks.integrations,
  createVerificationIntegration: mocks.createIntegration,
  updateVerificationIntegration: mocks.updateIntegration,
  disableVerificationIntegration: mocks.disableIntegration,
  rotateVerificationIntegrationToken: mocks.rotateIntegration,
  getVerificationReadiness: mocks.readiness,
  listVerificationRequests: mocks.requests,
  createVerificationRequest: mocks.createRequest,
  getVerificationRequest: mocks.request,
  createVerificationBatch: mocks.createBatch,
}))

const SelectStub = defineComponent({
  props: ['modelValue', 'options', 'disabled'], emits: ['update:modelValue'],
  setup: (props, { emit }) => () => h('select', {
    value: props.modelValue, disabled: props.disabled,
    onChange: (event: Event) => emit('update:modelValue', (event.target as HTMLSelectElement).value),
  }, (props.options || []).map((option: { value: string; label: string }) => h('option', { value: option.value }, option.label))),
})
const TabsStub = defineComponent({
  props: ['modelValue', 'options'], emits: ['update:modelValue'],
  setup: (props, { emit, slots }) => () => h('div', [
    ...props.options.map((option: { value: string; label: string }) => h('button', { onClick: () => emit('update:modelValue', option.value) }, option.label)),
    slots.panel?.({ option: props.options.find((option: { value: string }) => option.value === props.modelValue) }),
  ]),
})
const DialogStub = defineComponent({
  props: ['open', 'title'],
  setup: (props, { slots }) => () => props.open ? h('section', [h('h2', props.title), slots.default?.(), slots.footer?.()]) : null,
})
const LinkStub = defineComponent({
  props: ['to', 'custom'],
  setup: (props, { slots }) => () => slots.default?.({ href: `#${JSON.stringify(props.to)}` }),
})
const wrappers: VueWrapper[] = []
const clients: QueryClient[] = []
async function mountPage(component: typeof VerificationIntegrations | typeof VerificationForm | typeof Verifications, props = {}) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } })
  clients.push(client)
  const wrapper = mount(component, {
    props, attachTo: document.body,
    global: {
      plugins: [[VueQueryPlugin, { queryClient: client }]],
      stubs: {
        UiSelect: SelectStub, UiTabs: TabsStub, UiDialog: DialogStub,
        UiPopover: { template: '<div><slot name="trigger" /><slot /></div>' },
        RouterLink: LinkStub, ConfirmAction: true, VerificationMaterials: true,
        VerificationSourcePreview: true, VerificationActions: true, NewVerification: true,
      },
    },
  })
  wrappers.push(wrapper)
  await flushPromises()
  return wrapper
}
async function click(wrapper: VueWrapper, label: string) {
  const button = wrapper.findAll('button').find((item) => item.text() === label)
  expect(button, label).toBeTruthy()
  await button!.trigger('click')
  await flushPromises()
}
function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>((done) => { resolve = done })
  return { promise, resolve }
}
function identity(user = 'user-a', tenant = 'tenant-a', admin = false) {
  mocks.auth.session = { user_id: user, tenant_id: tenant, role: admin ? 'super_admin' : 'user', must_change_password: false }
  mocks.auth.isSuperAdmin = admin
  mocks.auth.authGeneration++
}
function saved(tenant = 'tenant-a', name = '已保存名称'): VerificationConfiguration {
  return {
    tenant_id: tenant, tenant_worker_id: `worker-${tenant}`, worker_name: 'verification', display_name: name,
    engine: 'pi', model_id: 'model-a', model_name: 'local', model_display_name: '本地模型',
    max_running: 1, priority: 100, enabled: true, revision: 1, ready: true, reason: null,
    message: '配置已保存', updated_at: null,
  }
}
function choices(name = '本地模型'): VerificationConfigurationOptions {
  return {
    models: [{ id: 'model-a', display_name: name, model_name: 'local', protocol: 'openai', enabled: true, credential_configured: true }],
    engines: [{ id: 'pi', name: 'pi' }],
    defaults: { display_name: '独立复测', engine: 'pi', max_running: 1, priority: 100, enabled: true },
  }
}
function fieldValue(wrapper: VueWrapper, selector = '#verification-config-name') {
  return (wrapper.get(selector).element as HTMLInputElement).value
}
function modelLinks(wrapper: VueWrapper) {
  return wrapper.findAllComponents(LinkStub).filter((link) => (link.props('to') as { name?: string }).name === 'ConsoleModels')
}

beforeEach(() => {
  vi.resetAllMocks()
  mocks.auth = reactive({ session: null, authGeneration: 0, isSuperAdmin: false })
  mocks.route = reactive({ name: 'VerificationConfiguration', query: {} })
  identity()
  mocks.push.mockResolvedValue(undefined)
  mocks.configuration.mockResolvedValue(saved())
  mocks.options.mockResolvedValue(choices())
  mocks.saveConfiguration.mockImplementation(async (_tenant, body) => ({ ...saved(), ...body }))
  mocks.tenants.mockResolvedValue([{ id: 'tenant-a', name: '本人租户', kind: 'personal' }, { id: 'tenant-b', name: '代管租户', kind: 'personal' }])
  mocks.integrations.mockResolvedValue([])
  mocks.readiness.mockResolvedValue({ configured: false, ready: false, message: '尚未配置复测执行', reason: 'not_configured' })
  mocks.vulnerabilities.mockResolvedValue([])
  mocks.requests.mockResolvedValue({ items: [], total: 0 })
})
afterEach(() => {
  wrappers.splice(0).forEach((wrapper) => wrapper.unmount())
  clients.splice(0).forEach((client) => client.clear())
  vi.restoreAllMocks()
  document.body.innerHTML = ''
})

describe('个人复测执行配置', () => {
  it('普通路由、旧地址与导航可达，旧地址保留显式租户供页面拒绝越界', () => {
    const current = router.resolve({ name: 'VerificationConfiguration' })
    expect(current.matched.some((record) => record.meta.superAdmin)).toBe(false)
    const legacy = router.resolve('/console/verification-integrations?tenant_id=other-tenant')
    const redirect = legacy.matched.at(-1)!.redirect
    expect(typeof redirect).toBe('function')
    if (typeof redirect !== 'function') throw new Error('旧地址必须显式保留查询范围')
    expect(redirect(legacy, router.currentRoute.value)).toEqual({ name: 'VerificationConfiguration', query: { tenant_id: 'other-tenant' } })
    expect(sidebarNavigation('system', false).some((item) => item.routeName === 'VerificationConfiguration')).toBe(true)
    expect(commandItemsFromRegistry(false).find((item) => item.id === 'go:VerificationConfiguration')?.superAdminOnly).toBe(false)
  })

  it('配置 API 省略 tenant_id，原有显式目标调用保持不变', async () => {
    const api = await vi.importActual<typeof import('@/api/verificationRequests')>('@/api/verificationRequests')
    const body = { model_id: 'model-a', ...choices().defaults }
    await api.getVerificationConfiguration()
    await api.getVerificationConfigurationOptions()
    await api.saveVerificationConfiguration(undefined, body)
    expect(mocks.api.mock.calls.slice(0, 3)).toEqual([
      ['GET', '/api/v1/verification-configuration'],
      ['GET', '/api/v1/verification-configuration/options'],
      ['PUT', '/api/v1/verification-configuration', body],
    ])
    await api.getVerificationConfiguration('tenant-b')
    expect(mocks.api).toHaveBeenLastCalledWith('GET', '/api/v1/verification-configuration?tenant_id=tenant-b')
  })

  it('普通用户仅请求本人配置、可保存现有字段，失败和离开取消均保留编辑', async () => {
    const wrapper = await mountPage(VerificationIntegrations)
    expect(mocks.configuration).toHaveBeenCalledWith(undefined)
    expect(mocks.options).toHaveBeenCalledWith(undefined)
    expect(mocks.tenants).not.toHaveBeenCalled()
    expect(mocks.integrations).not.toHaveBeenCalled()
    expect(wrapper.text()).not.toContain('外部调用')
    expect(wrapper.find('#verification-tenant').exists()).toBe(false)
    expect(modelLinks(wrapper)).toHaveLength(1)
    await wrapper.get('#verification-config-name').setValue('本人修改')
    mocks.saveConfiguration.mockRejectedValueOnce(new Error('本地保存失败'))
    await wrapper.get('form.config-form').trigger('submit')
    await flushPromises()
    expect(mocks.saveConfiguration).toHaveBeenCalledWith(undefined, { model_id: 'model-a', ...choices().defaults, display_name: '本人修改' })
    expect(wrapper.text()).toContain('本地保存失败')
    expect(fieldValue(wrapper)).toBe('本人修改')
    vi.spyOn(window, 'confirm').mockReturnValue(false)
    expect(mocks.leave.mock.calls[0]![0]()).toBe(false)
    await click(wrapper, '重新读取已保存配置')
    expect(mocks.configuration).toHaveBeenCalledTimes(1)
    expect(fieldValue(wrapper)).toBe('本人修改')
  })

  it('无模型时只引导个人模型；普通 URL 越界明确拒绝且不退回本人加载', async () => {
    mocks.options.mockResolvedValueOnce({ ...choices(), models: [] })
    const own = await mountPage(VerificationIntegrations)
    expect(own.text()).toContain('尚无模型，请先在个人模型管理中配置')
    expect(modelLinks(own)).toHaveLength(1)
    const calls = mocks.configuration.mock.calls.length
    mocks.route.query = { tenant_id: 'tenant-b' }
    await flushPromises()
    expect(own.text()).toContain('无权访问其他用户')
    expect(own.find('form.config-form').exists()).toBe(false)
    expect(mocks.configuration).toHaveBeenCalledTimes(calls)
    expect(mocks.options).toHaveBeenCalledTimes(calls)
    expect(mocks.tenants).not.toHaveBeenCalled()
    await click(own, '返回本人配置')
    expect(mocks.push).toHaveBeenLastCalledWith({ name: 'VerificationConfiguration' })
  })

  it('同租户换身份时清空配置，迟到 GET 和 options 不覆盖新身份', async () => {
    const oldConfig = deferred<VerificationConfiguration>()
    const oldOptions = deferred<VerificationConfigurationOptions>()
    mocks.configuration.mockReturnValueOnce(oldConfig.promise).mockResolvedValueOnce(saved('tenant-a', '新身份名称'))
    mocks.options.mockReturnValueOnce(oldOptions.promise).mockResolvedValueOnce(choices('新身份模型'))
    const wrapper = await mountPage(VerificationIntegrations)
    identity('user-b')
    await flushPromises()
    expect(fieldValue(wrapper)).toBe('新身份名称')
    oldConfig.resolve(saved('tenant-a', '旧身份名称'))
    oldOptions.resolve(choices('旧身份模型'))
    await flushPromises()
    expect(fieldValue(wrapper)).toBe('新身份名称')
    expect(wrapper.get('#verification-config-model').text()).toContain('新身份模型')
    expect(wrapper.text()).not.toContain('旧身份模型')
  })

  it('迟到保存不能串到同租户新身份或清掉该身份的新编辑', async () => {
    const oldSave = deferred<VerificationConfiguration>()
    mocks.saveConfiguration.mockReturnValueOnce(oldSave.promise)
    const wrapper = await mountPage(VerificationIntegrations)
    await wrapper.get('#verification-config-name').setValue('旧身份提交')
    await wrapper.get('form.config-form').trigger('submit')
    expect(mocks.leave.mock.calls[0]![0]()).toBe(false)
    mocks.configuration.mockResolvedValueOnce(saved('tenant-a', '新身份配置'))
    identity('user-b')
    await flushPromises()
    await wrapper.get('#verification-config-name').setValue('新身份编辑')
    oldSave.resolve(saved('tenant-a', '旧身份提交'))
    await flushPromises()
    expect(fieldValue(wrapper)).toBe('新身份编辑')
    expect(wrapper.text()).toContain('有未保存修改')
    expect(mocks.tenants).not.toHaveBeenCalled()
  })

  it('超管保留代管与外部调用，外租户不显示伪装的模型管理入口', async () => {
    identity('admin', 'tenant-a', true)
    mocks.route.query = { tenant_id: 'tenant-b' }
    mocks.configuration.mockResolvedValueOnce(saved('tenant-b'))
    const wrapper = await mountPage(VerificationIntegrations)
    expect(mocks.tenants).toHaveBeenCalledTimes(1)
    expect(mocks.configuration).toHaveBeenCalledWith('tenant-b')
    expect(mocks.options).toHaveBeenCalledWith('tenant-b')
    expect(modelLinks(wrapper)).toHaveLength(0)
    expect(wrapper.text()).toContain('模型由该租户用户')
    expect(mocks.integrations).not.toHaveBeenCalled()
    await click(wrapper, '外部调用')
    expect(mocks.integrations).toHaveBeenCalledWith('tenant-b')
  })

  it('代管范围切换清空编辑，迟到令牌列表和创建响应不进入新范围', async () => {
    identity('admin', 'tenant-a', true)
    const oldList = deferred<{ id: string; name: string; enabled: boolean; created_at: string }[]>()
    const oldCreate = deferred<{ integration: { id: string; name: string; enabled: boolean; created_at: string }; token: string }>()
    mocks.integrations.mockReturnValueOnce(oldList.promise)
    mocks.createIntegration.mockReturnValueOnce(oldCreate.promise)
    const wrapper = await mountPage(VerificationIntegrations)
    await click(wrapper, '外部调用')
    await click(wrapper, '创建调用令牌')
    await wrapper.get('#integration-name').setValue('旧范围调用')
    await wrapper.get('#verification-integration-form').trigger('submit')
    mocks.configuration.mockResolvedValueOnce(saved('tenant-b', '新范围配置'))
    mocks.route.query = { tenant_id: 'tenant-b' }
    await flushPromises()
    oldList.resolve([{ id: 'old', name: '旧范围列表', enabled: true, created_at: '' }])
    oldCreate.resolve({ integration: { id: 'old', name: '旧范围调用', enabled: true, created_at: '' }, token: 'test-only-old-scope-token' })
    await flushPromises()
    expect(wrapper.text()).not.toContain('旧范围列表')
    expect(wrapper.text()).not.toContain('test-only-old-scope-token')
    expect(wrapper.find('#integration-name').exists()).toBe(false)
    await click(wrapper, '执行配置')
    expect(fieldValue(wrapper)).toBe('新范围配置')
  })

  it('超管降权立即清空一次性令牌，不请求普通用户不可见的令牌页签', async () => {
    identity('admin', 'tenant-a', true)
    mocks.createIntegration.mockResolvedValueOnce({ integration: { id: 'one', name: '本地调用', enabled: true, created_at: '' }, token: 'test-only-once-token' })
    const wrapper = await mountPage(VerificationIntegrations)
    await click(wrapper, '外部调用')
    await click(wrapper, '创建调用令牌')
    await wrapper.get('#integration-name').setValue('本地调用')
    await wrapper.get('#verification-integration-form').trigger('submit')
    await flushPromises()
    expect(wrapper.text()).toContain('test-only-once-token')
    identity('admin', 'tenant-a', false)
    await flushPromises()
    expect(wrapper.text()).not.toContain('test-only-once-token')
    expect(wrapper.text()).not.toContain('外部调用')
    expect(mocks.integrations).toHaveBeenCalledTimes(1)
    expect(mocks.configuration).toHaveBeenLastCalledWith(undefined)
    identity('admin', 'tenant-a', true)
    await flushPromises()
    await click(wrapper, '外部调用')
    expect(wrapper.text()).not.toContain('test-only-once-token')
  })

  it('创建 readiness 提供本人配置入口，身份切换清正文且忽略旧 readiness', async () => {
    const oldReadiness = deferred<{ ready: boolean; message: string }>()
    mocks.readiness.mockReturnValueOnce(oldReadiness.promise)
    const wrapper = await mountPage(VerificationForm, { initialInput: 'manual' })
    await wrapper.get('#new-verification-title').setValue('旧身份标题')
    await wrapper.get('#new-verification-content').setValue('旧身份正文')
    identity('user-b')
    await flushPromises()
    oldReadiness.resolve({ ready: true, message: '旧身份已配置' })
    await flushPromises()
    expect(fieldValue(wrapper, '#new-verification-title')).toBe('')
    expect(fieldValue(wrapper, '#new-verification-content')).toBe('')
    expect(wrapper.text()).toContain('去配置复测执行')
    expect(wrapper.text()).not.toContain('联系管理员')
    const start = wrapper.findAll('button').find((button) => button.text() === '开始复测')!
    expect(start.attributes('disabled')).toBeDefined()
    expect(mocks.tenants).not.toHaveBeenCalled()
  })

  it('身份切换后旧创建响应不触发 accepted 或恢复旧表单', async () => {
    const oldRequest = deferred<{ id: string; execution_status: string }>()
    mocks.createRequest.mockReturnValueOnce(oldRequest.promise)
    mocks.readiness.mockResolvedValue({ ready: true, message: '本地可配置状态' })
    const wrapper = await mountPage(VerificationForm, { initialInput: 'manual' })
    await wrapper.get('#new-verification-title').setValue('旧身份提交')
    await wrapper.get('#new-verification-content').setValue('仅本地测试正文')
    await wrapper.get('#new-verification-form').trigger('submit')
    identity('user-b')
    await flushPromises()
    oldRequest.resolve({ id: 'old-request', execution_status: 'queued' })
    await flushPromises()
    expect(wrapper.emitted('accepted')).toBeUndefined()
    expect(fieldValue(wrapper, '#new-verification-title')).toBe('')
    expect(wrapper.text()).not.toContain('请求已受理')
  })

  it('普通用户列表提供执行配置按钮', async () => {
    const wrapper = await mountPage(Verifications)
    await click(wrapper, '漏洞复测配置')
    expect(mocks.push).toHaveBeenLastCalledWith({ name: 'VerificationConfiguration' })
  })
})
