import { DOMWrapper, flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { defineComponent, h } from 'vue'
import { QueryClient, VueQueryPlugin } from '@tanstack/vue-query'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiError } from '@/api/client'
import NewVerification from './NewVerification.vue'
import VerificationForm from './VerificationForm.vue'
import TestEgressFields from '@/components/patterns/TestEgressFields.vue'
import VerificationDetail from './VerificationDetail.vue'
import VerificationSourcePreview from './VerificationSourcePreview.vue'
import EvidenceItemDisclosure from '@/components/patterns/EvidenceItemDisclosure.vue'
import VerificationIntegrations from './VerificationIntegrations.vue'
import VulnerabilityDetail from '@/features/vulnerabilities/VulnerabilityDetail.vue'
import EvidenceReader from '@/components/patterns/EvidenceReader.vue'
import type { EvidenceItem, EvidenceRef, TrafficFlowDetail, TrafficProjection, TrafficReadable } from '@/api/types'

const mocks = vi.hoisted(() => ({
  route: { query: {} as Record<string, string>, params: { id: 'request-a' } },
  auth: { session: { tenant_id: 'tenant-a' }, isSuperAdmin: true },
  push: vi.fn(), leave: vi.fn(),
  createRequest: vi.fn(), readiness: vi.fn(), createBatch: vi.fn(), cancelBatch: vi.fn(),
  listVulnerabilities: vi.fn(), preview: vi.fn(), history: vi.fn(),
  configuration: vi.fn(), options: vi.fn(), tenants: vi.fn(),
  project: vi.fn(), vulnerability: vi.fn(), duplicates: vi.fn(),
  request: vi.fn(), fetchText: vi.fn(), artifactPreview: vi.fn(), materialPreview: vi.fn(), selectRaw: vi.fn(),
  trafficFlow: vi.fn(), trafficReadable: vi.fn(), trafficProjection: vi.fn(), trafficPreview: vi.fn(), trafficBody: vi.fn(), selectTextRange: vi.fn(),
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
  fetchText: mocks.fetchText,
  getProjectDetail: mocks.project,
  getVulnerability: mocks.vulnerability,
  getDuplicateVulnerabilityCandidates: mocks.duplicates,
  getTrafficFlow: mocks.trafficFlow,
  getTrafficReadable: mocks.trafficReadable,
  getTrafficProjection: mocks.trafficProjection,
  getTrafficPreview: mocks.trafficPreview,
  getTrafficBodyPreview: mocks.trafficBody,
}))
vi.mock('@/api/verificationRequests', async (original) => ({
  ...await original<typeof import('@/api/verificationRequests')>(),
  createVerificationRequest: mocks.createRequest,
  getVerificationRequest: mocks.request,
  getVerificationArtifactPreview: mocks.artifactPreview,
  getVerificationMaterialPreview: mocks.materialPreview,
  getVerificationReadiness: mocks.readiness,
  createVerificationBatch: mocks.createBatch,
  cancelVerificationBatch: mocks.cancelBatch,
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
async function mountPage(component: typeof NewVerification | typeof VerificationForm | typeof VerificationIntegrations | typeof VulnerabilityDetail | typeof VerificationDetail | typeof EvidenceReader | typeof VerificationSourcePreview, props = {}) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } })
  clients.push(client)
  const wrapper = mount(component, {
    props, attachTo: document.body,
    global: {
      plugins: [[VueQueryPlugin, { queryClient: client }]],
      stubs: {
        UiDialog: OverlayStub, UiDrawer: OverlayStub, UiPopover: OverlayStub, UiSelect: SelectStub,
        RouterLink: { template: '<a><slot /></a>' },
        MonacoEditor: defineComponent({ props: ['modelValue'], setup: (props, { expose }) => { expose({ selectAll: mocks.selectRaw, selectTextRange: mocks.selectTextRange }); return () => h('pre', { 'data-testid': 'evidence-content' }, props.modelValue) } }), MarkdownPreview: true,
      },
    },
  })
  wrappers.push(wrapper)
  await flushPromises()
  return wrapper
}
async function click(wrapper: Pick<VueWrapper, 'findAll'>, text: string) {
  const button = wrapper.findAll('button').find((item) => item.text() === text)
  expect(button, text).toBeTruthy()
  await button!.trigger('click')
  await flushPromises()
}
async function expectOriginalUrl(wrapper: Pick<VueWrapper, 'get'>, label: string, url: string) {
  const trigger = wrapper.get(`button[aria-label="${label}更多"]`)
  await trigger.trigger('click')
  await flushPromises()
  // Portal 关闭动画期间可能保留另一侧菜单，按当前触发器关联核对原件。
  const menu = document.getElementById(trigger.attributes('aria-controls') || '')
  expect(menu?.querySelector('a[download]')?.getAttribute('href')).toBe(url)
  menu?.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))
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
const flowDetail: TrafficFlowDetail = {
  id: 'flow-a', project_id: 'project-a', revision: 3, state: 'partial', source_state: 'partial',
  header_text: { request: 'GET /中文 HTTP/1.1\r\nX-Sample: first\r\nX-Sample: second\r\n\r\n', response: null },
  scope: { id: 'scope-a', project_id: 'project-a', task_run_id: 'run-original', generation: 2, container_id: 'container-a' },
  sync: { bodies: { request: { status: 'available', artifact_id: 'body-a', revision: 1, accepted_bytes: 70000 }, response: { status: 'absent', artifact_id: null } } },
}
const flowHeader = 'GET /中文 HTTP/1.1\r\nX-Sample: first\r\nX-Sample: second\r\n\r\n'
const flowProjection: TrafficProjection = {
  flow_id: 'flow-a', project_id: 'project-a', scope: flowDetail.scope, revision: 3, state: 'partial', side: 'request',
  representation: 'http-evidence-v1', sha256: 'a'.repeat(64), size: 70004, offset: 0, next_offset: 65536, has_more: true,
  text: flowHeader.padEnd(65532, 'x'), header_size: new TextEncoder().encode(flowHeader).byteLength,
  body_artifact_id: 'body-a', body_sha256: 'b'.repeat(64), body_display: 'utf8',
}
const flowReadable: TrafficReadable = {
  project_id: 'project-a', flow_id: 'flow-a', side: 'request', revision: 3,
  representation: 'http-readable', excerpt_eligible: false, header_text: flowHeader, body_text: '中文'.padEnd(69996, 'x'),
  read_mode: 'auto', deferred_reason: null, body_bytes: 70000, decoded_body_bytes: 70000, body_limit: 10485760,
  decode_status: 'complete', decoded: false, text_escaped: false, display_truncated: false, content_encodings: [],
  body_artifact_id: 'body-a', body_sha256: 'b'.repeat(64),
}
const flowEvidence: EvidenceItem = {
  id: 'flow-item', kind: 'http', flow_id: 'flow-a', role: 'primary', title: '同一 URL 的选定交互', source: 'historical',
  excerpts: [{ side: 'request', pointer: 'utf8-bytes=65536:65542', representation: 'http-evidence-v1', sha256: 'a'.repeat(64), revision: 3 }],
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
  vi.stubGlobal('ResizeObserver', class { observe() {} disconnect() {} })
  vi.clearAllMocks()
  HTMLElement.prototype.scrollIntoView = vi.fn()
  mocks.trafficFlow.mockReset().mockResolvedValue(flowDetail)
  mocks.trafficReadable.mockReset().mockResolvedValue(flowReadable)
  mocks.trafficProjection.mockReset().mockImplementation(async (_project, _flow, _side, offset) => offset === 0 ? flowProjection : {
    ...flowProjection, offset, next_offset: 70004, has_more: false, text: '中文'.padEnd(4464, 'y'),
  })
  mocks.trafficPreview.mockReset().mockResolvedValue({ project_id: 'project-a', flow_id: 'flow-a', side: 'request', revision: 3, representation: 'http-preview', stable: false, header_text: flowHeader, header_truncated: false, text: '已确认前缀', body_display: 'utf8', offset: 0, next_offset: 15, accepted_bytes: 15, captured_bytes: 20, has_more: false, sealed: false, source_sealed: false })
  mocks.trafficBody.mockReset().mockResolvedValue({ side: 'request', revision: 1, sealed: true, source_sealed: true, body_complete: true, captured_bytes: 4, accepted_bytes: 4, sealed_bytes: 4, size: 4, offset: 0, next_offset: 4, bytes_read: 4, has_more: false, content_base64: 'AP9BQg==' })
  mocks.route.query = { input: 'manual' }
  mocks.route.params.id = 'request-a'
  mocks.push.mockResolvedValue(undefined)
  mocks.readiness.mockResolvedValue({ ready: true, worker_name: '本地配置' })
  mocks.listVulnerabilities.mockResolvedValue([{ id: 'vuln-a', project_id: 'project-a', project_title: '当前租户项目', title: '当前租户漏洞' }])
  mocks.createBatch.mockResolvedValue(batch)
  mocks.configuration.mockResolvedValue(saved)
  mocks.options.mockResolvedValue({ models: [{ id: 'model-a', display_name: '本地模型', model_name: 'local', enabled: true }], engines: [{ id: 'pi', name: 'pi' }], defaults: saved })
  mocks.tenants.mockResolvedValue([{ id: 'tenant-a', name: '当前租户' }])
  mocks.project.mockResolvedValue({ project: { id: 'project-a', tenant_id: 'tenant-a' } })
  mocks.vulnerability.mockResolvedValue({ id: 'vuln-a', project_id: 'project-a', title: '全局成果原文', severity: 'high', status: 'new', confidence: 0.8, observation_id: 'observation-a', readable: { vulnerability_id: 'vuln-a', observation_id: 'observation-a', description_markdown: '全局成果原文', reproduction_markdown: '', remediation_markdown: '', limitations_markdown: '', key_evidence_markdown: '' }, source: 'capture', evidence_items: [], record_evidence_items: [], observations: [], verifications: [], run_artifacts: [] })
  mocks.duplicates.mockResolvedValue({ project_id: 'project-a', requested_vulnerability_id: 'vuln-a', candidates: [] })
  mocks.history.mockResolvedValue({ items: [], total: 0 })
  mocks.preview.mockResolvedValue({ project_id: 'project-a', vulnerability_id: 'vuln-a', project_title: '当前租户项目', vulnerability: { id: 'vuln-a', title: '全局成果原文', severity: 'high', status: 'new' }, evidence: { observation_id: 'observation-a', source: 'capture', evidence_items: [], reproduction_steps: [] }, project_context: {}, facts: [], observations: [], verifications: [], fact_evidence: [], artifacts: [], missing_materials: [] })
})
afterEach(() => {
  wrappers.splice(0).forEach((wrapper) => wrapper.unmount())
  clients.splice(0).forEach((client) => client.clear())
  vi.restoreAllMocks()
  document.body.innerHTML = ''
})

describe('复测收尾交互边界', () => {
  it('本次目标独立提交，响应未知时锁定且重试不改变目标', async () => {
    mocks.createRequest.mockRejectedValue(new TypeError('response lost'))
    const wrapper = await mountPage(NewVerification)
    await fillManual(wrapper)
    await wrapper.get('#new-verification-target').setValue('https://new.example.test/instance')
    await submit(wrapper)
    const payload = mocks.createRequest.mock.calls[0]![0]
    expect(payload.target_url).toBe('https://new.example.test/instance')
    expect(payload.report.content).toBe('原正文，保留换行\n第二行')
    expect(wrapper.get('#new-verification-target').element.matches(':disabled')).toBe(true)
    await click(wrapper, '用原请求重试')
    expect(mocks.createRequest.mock.calls[1]![0]).toEqual(payload)
  })

  it('复制复测保留已绑定目标而不回退历史地址', async () => {
    mocks.route.query = { source_request_id: 'request-a' }
    mocks.request.mockResolvedValue({ id: 'request-a', title: '复制资料', target_url: 'https://new.example.test/instance', source_target: 'http://old.example.test/', report: { resource_id: 'resource-a' }, input_source: null })
    mocks.createRequest.mockRejectedValue(new ApiError('受控拒绝', 422, {}))
    const wrapper = await mountPage(NewVerification)
    expect((wrapper.get('#new-verification-target').element as HTMLInputElement).value).toBe('https://new.example.test/instance')
    await click(wrapper, '复制上一轮输入')
    await submit(wrapper)
    expect(mocks.createRequest.mock.calls[0]![0].target_url).toBe('https://new.example.test/instance')
  })

  it('代理设置：外部默认直连，凭据变更触发离开保护且未知受理重试保持原请求', async () => {
    mocks.createRequest.mockRejectedValue(new TypeError('response lost'))
    const wrapper = await mountPage(NewVerification)
    const fields = wrapper.getComponent(TestEgressFields)
    expect(fields.props('modelValue').mode).toBe('direct')
    fields.vm.$emit('update:modelValue', { mode: 'socks5', host: 'proxy.example.test', port: 1080, auth_action: 'replace', username: 'sample-user', password: 'sample-password' })
    await flushPromises()
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false)
    expect(wrapper.getComponent(VerificationForm).vm.canLeave()).toBe(false)
    expect(confirm).toHaveBeenCalled()
    await fillManual(wrapper)
    const store = vi.spyOn(Storage.prototype, 'setItem')
    await submit(wrapper)
    const payload = mocks.createRequest.mock.calls[0]![0]
    expect(payload.test_egress).toEqual({ mode: 'socks5', host: 'proxy.example.test', port: 1080, auth_action: 'replace', username: 'sample-user', password: 'sample-password' })
    expect(fields.props('disabled')).toBe(true)
    expect(wrapper.getComponent(VerificationForm).vm.canLeave()).toBe(false)
    await click(wrapper, '用原请求重试')
    expect(mocks.createRequest.mock.calls[1]![0]).toEqual(payload)
    expect(store).not.toHaveBeenCalled()
  })

  it('代理设置：复制平台慢响应期间禁止单独设置，完成后默认继承CURRENT且可显式编辑', async () => {
    mocks.route.query = { source_request_id: 'request-a' }
    mocks.request.mockResolvedValue({ id: 'request-a', title: '复制资料', report: { resource_id: 'resource-a' }, input_source: { project_id: 'project-a', vulnerability_id: 'vuln-a' }, test_egress: { mode: 'http', host: 'old.example.test', port: 8080, auth_configured: false, revision: 1 } })
    const current = { project: { id: 'project-a', test_egress: { mode: 'socks5', host: 'current.example.test', port: 1080, auth_configured: true, revision: 5 } } }
    let resolveProject!: (value: typeof current) => void
    mocks.project.mockReturnValueOnce(new Promise((resolve) => { resolveProject = resolve }))
    mocks.createRequest.mockRejectedValue(new ApiError('受控拒绝', 422, {}))
    const wrapper = await mountPage(NewVerification)
    expect(mocks.project).toHaveBeenCalledWith('project-a')
    const choice = wrapper.get<HTMLSelectElement>('#verification-egress-choice')
    expect(choice.element.disabled).toBe(true)
    expect(choice.element.value).toBe('inherit')
    expect(wrapper.findComponent(TestEgressFields).exists()).toBe(false)
    await click(wrapper, '复制上一轮输入')
    await submit(wrapper)
    expect(mocks.createRequest).not.toHaveBeenCalled()
    resolveProject(current)
    await flushPromises()
    expect(choice.element.disabled).toBe(false)
    const fields = wrapper.getComponent(TestEgressFields)
    expect(fields.props('modelValue')).toMatchObject({ mode: 'socks5', host: 'current.example.test', port: 1080, password: '' })
    expect(fields.props('disabled')).toBe(true)
    await submit(wrapper)
    expect(mocks.createRequest.mock.calls[0]![0]).not.toHaveProperty('test_egress')
    await click(wrapper, '修改未受理的输入')
    await choice.setValue('custom')
    expect(fields.props('disabled')).toBe(false)
    expect(fields.props('modelValue')).toMatchObject({ mode: 'socks5', host: 'current.example.test', port: 1080 })
    await submit(wrapper)
    expect(mocks.createRequest.mock.calls[1]![0].test_egress).toEqual({ mode: 'socks5', host: 'current.example.test', port: 1080, auth_action: 'keep' })
  })

  it('代理设置：复制平台读取失败及重试中禁止单独设置，成功后从来源代理初始化', async () => {
    mocks.route.query = { source_request_id: 'request-a' }
    mocks.request.mockResolvedValue({ id: 'request-a', title: '复制资料', report: { resource_id: 'resource-a' }, input_source: { project_id: 'project-a', vulnerability_id: 'vuln-a' } })
    const current = { project: { id: 'project-a', test_egress: { mode: 'http', host: 'retry.example.test', port: 8080, auth_configured: false, revision: 2 } } }
    let resolveProject!: (value: typeof current) => void
    mocks.project.mockRejectedValueOnce(new Error('读取失败')).mockReturnValueOnce(new Promise((resolve) => { resolveProject = resolve }))
    const wrapper = await mountPage(NewVerification)
    const choice = wrapper.get<HTMLSelectElement>('#verification-egress-choice')
    expect(choice.element.disabled).toBe(true)
    expect(choice.element.value).toBe('inherit')
    expect(wrapper.text()).toContain('无法读取来源项目当前代理设置，请重试。')
    await click(wrapper, '重新读取')
    expect(mocks.project).toHaveBeenCalledTimes(2)
    expect(choice.element.disabled).toBe(true)
    expect(choice.element.value).toBe('inherit')
    expect(wrapper.findComponent(TestEgressFields).exists()).toBe(false)
    resolveProject(current)
    await flushPromises()
    expect(choice.element.disabled).toBe(false)
    expect(wrapper.text()).not.toContain('无法读取来源项目当前代理设置，请重试。')
    await choice.setValue('custom')
    expect(wrapper.getComponent(TestEgressFields).props('modelValue')).toMatchObject({ mode: 'http', host: 'retry.example.test', port: 8080 })
    await click(wrapper, '复制上一轮输入')
    mocks.createRequest.mockResolvedValueOnce({ id: 'accepted-copy', execution_status: 'queued' })
    await submit(wrapper)
    expect(mocks.createRequest.mock.calls[0]![0].test_egress).toEqual({ mode: 'http', host: 'retry.example.test', port: 8080, auth_action: 'keep' })
  })

  it('代理设置：复制平台切换来源后，迟到出口响应不覆盖本轮真实编辑', async () => {
    mocks.request.mockImplementation(async (id: string) => ({ id, title: '复制资料', report: { resource_id: 'resource-a' }, input_source: { project_id: id === 'request-a' ? 'project-a' : 'project-b', vulnerability_id: 'vuln-a' } }))
    const current = { project: { id: 'project-b', test_egress: { mode: 'socks5', host: 'current.example.test', port: 1080, auth_configured: false, revision: 2 } } }
    let resolveOldProject!: (value: typeof current) => void
    mocks.project.mockReturnValueOnce(new Promise((resolve) => { resolveOldProject = resolve })).mockResolvedValueOnce(current)
    const wrapper = await mountPage(VerificationForm, { sourceRequestId: 'request-a' })
    expect(mocks.project).toHaveBeenCalledWith('project-a')
    await wrapper.setProps({ sourceRequestId: 'request-b' })
    await flushPromises()
    expect(mocks.project).toHaveBeenCalledWith('project-b')
    await wrapper.get('#verification-egress-choice').setValue('custom')
    const fields = wrapper.getComponent(TestEgressFields)
    const edited = { ...fields.props('modelValue'), host: 'edited.example.test', port: 1081 }
    fields.vm.$emit('update:modelValue', edited)
    await flushPromises()
    resolveOldProject({ project: { id: 'project-a', test_egress: { ...current.project.test_egress, host: 'late.example.test' } } })
    await flushPromises()
    expect(fields.props('modelValue')).toEqual(edited)
    await click(wrapper, '复制上一轮输入')
    mocks.createRequest.mockResolvedValueOnce({ id: 'accepted-copy', execution_status: 'queued' })
    await submit(wrapper)
    expect(mocks.createRequest.mock.calls[0]![0].test_egress).toEqual({ mode: 'socks5', host: 'edited.example.test', port: 1081, auth_action: 'keep' })
  })

  it('代理设置：平台来源显示CURRENT公共设置，默认省略覆盖且允许显式清除认证', async () => {
    mocks.route.query = { input: 'platform', source_project_id: 'project-a', source_vulnerability_id: 'vuln-a' }
    const preview = await mocks.preview()
    mocks.preview.mockResolvedValue({ ...preview, test_egress: { mode: 'http', host: 'current.example.test', port: 8080, auth_configured: true, revision: 4 } })
    mocks.createRequest.mockRejectedValue(new ApiError('受控拒绝', 422, {}))
    const wrapper = await mountPage(NewVerification)
    const fields = wrapper.getComponent(TestEgressFields)
    expect(fields.props('modelValue')).toMatchObject({ mode: 'http', host: 'current.example.test', password: '' })
    expect(fields.props('authConfigured')).toBe(true)
    expect(fields.props('disabled')).toBe(true)
    await submit(wrapper)
    expect(mocks.createRequest.mock.calls[0]![0]).not.toHaveProperty('test_egress')
    await click(wrapper, '修改未受理的输入')
    await wrapper.get('#verification-egress-choice').setValue('custom')
    fields.vm.$emit('update:modelValue', { ...fields.props('modelValue'), auth_action: 'clear' })
    await submit(wrapper)
    expect(mocks.createRequest.mock.calls[1]![0].test_egress).toEqual({ mode: 'http', host: 'current.example.test', port: 8080, auth_action: 'clear' })
  })


  it.each([3, 4])('v%s仅展示三段结果并按实际步骤读取证据，不读取输入快照', async (version) => {
    const item = { id: 'http-a', kind: 'http', role: 'primary', source: 'capture', title: '关键交互', request: { artifact_id: 'result-artifact' }, response: null }
    const result = {
      schema_version: version, ...(version === 4 ? { remediation: '**检查归属**\n\n- 增加跨身份验证。' } : {}), outcome: 'not_reproduced', summary: '本次请求被拒绝，不能据此断言已修复。', tested_scope: '仅使用隔离样本，未测试其他身份。', limitations: ['未验证生产环境，不据此判断已修复。'], reason_code: null,
      evidence_refs: [{ artifact_id: 'result-artifact' }], evidence_items: [item],
      key_evidence: [{ reason: '请求未返回受保护内容。', evidence_item_ids: ['http-a'] }],
      test_steps: [{ action: '发送隔离样本请求', observation: '未取得响应原件。', evidence_item_ids: ['http-a'] }],
    }
    mocks.request.mockResolvedValue({
      id: 'request-a', project_id: 'execution-project', title: '证据对照', mode: 'retest', execution_status: 'completed', can_delete: true,
      target_url: 'https://new.example.test/instance', source_target: 'http://old.example.test/',
      result, input_source: { project_id: 'project-a', vulnerability_id: 'vuln-a', observation_id: 'observation-a', input_mode: 'current_vulnerability' },
      report: { resource_id: 'snapshot-a' }, materials: [{ id: 'material-a', path: 'input/original.http' }],
      runs: [], artifacts: [{ id: 'result-artifact', size: 24 }], history: [], cleanup: { status: 'pending', error: null }, source: { type: 'user', name: '本地测试' }, tenant_workers: [],
    })
    mocks.artifactPreview.mockResolvedValue({ project_id: 'execution-project', artifact_id: 'result-artifact', content: 'GET /current HTTP/1.1', truncated: false })
    const wrapper = await mountPage(VerificationDetail)
    expect(wrapper.text()).toContain('历史来源：http://old.example.test/')
    expect(wrapper.text()).toContain('本次目标：https://new.example.test/instance')
    expect(wrapper.find('a[href="https://new.example.test/instance"]').exists()).toBe(false)
    expect(wrapper.findAll('.verification-report-section h2').map((item) => item.text())).toEqual(['一、漏洞概述', '二、复现过程与数据包', '三、修复建议'])
    if (version === 4) expect(wrapper.get('.verification-remediation strong').text()).toBe('检查归属')
    else expect(wrapper.get('.verification-remediation').text()).toContain('历史结果未记录本轮修复建议')
    expect(wrapper.text()).toContain('本次未复现')
    expect(wrapper.text()).toContain('请求未返回受保护内容。')
    expect(wrapper.findAllComponents(EvidenceReader)).toHaveLength(0)
    expect(mocks.artifactPreview).not.toHaveBeenCalled()
    await click(wrapper, '关键交互 · 步骤 1')
    expect(wrapper.text()).toContain('GET /current HTTP/1.1')
    expect(wrapper.text()).toContain('未记录响应')
    const conclusion = wrapper.get('.verification-conclusion')
    expect(conclusion.text()).toContain(result.tested_scope)
    expect(conclusion.text()).toContain(result.limitations[0])
    expect(wrapper.find('[role="tablist"]').exists()).toBe(false)
    expect(wrapper.text()).not.toContain('原发现')
    expect(mocks.artifactPreview).toHaveBeenCalledWith('request-a', 'result-artifact', expect.any(AbortSignal))
    expect(mocks.fetchText).not.toHaveBeenCalled()
    expect(mocks.materialPreview).not.toHaveBeenCalled()
    expect(mocks.preview).not.toHaveBeenCalled()
    expect(mocks.vulnerability).not.toHaveBeenCalled()
    expect(wrapper.find('.verification-step-navigation').exists()).toBe(false)
    expect(wrapper.findAllComponents(EvidenceReader)).toHaveLength(1)
    expect(wrapper.get('.verification-result-summary').text()).toBe(result.summary)
    expect(HTMLElement.prototype.scrollIntoView).toHaveBeenCalledWith({ block: 'nearest', behavior: 'auto' })
    expect(document.activeElement).toBe(wrapper.get('.evidence-item-disclosure > summary').element)
    const disclosure = wrapper.get('.evidence-item-disclosure')
    ;(disclosure.element as HTMLDetailsElement).open = false
    await disclosure.trigger('toggle')
    expect(wrapper.findAllComponents(EvidenceReader)).toHaveLength(0)
  })

  it('真实步骤连续展示，判断依据定位首个真实引用且各交互默认折叠', async () => {
    const http = { id: 'http-a', kind: 'http', role: 'primary', source: 'capture', title: '受保护响应', request: { artifact_id: 'request-original' }, response: { artifact_id: 'response-original' } }
    const log = { id: 'log-a', kind: 'text', role: 'supporting', source: 'capture', title: '本轮观察', artifact: { artifact_id: 'log-original' } }
    const contents: Record<string, string> = { 'request-original': 'GET /sample HTTP/1.1', 'response-original': 'HTTP/1.1 403 Forbidden', 'log-original': '同一交互的独立观察原文' }
    mocks.artifactPreview.mockImplementation((_requestId: string, artifactId: string) => Promise.resolve({ project_id: 'execution-project', artifact_id: artifactId, content: contents[artifactId], truncated: false }))
    mocks.request.mockResolvedValue({
      id: 'request-a', project_id: 'execution-project', title: '步骤阅读', mode: 'retest', execution_status: 'completed', can_delete: true,
      result: { schema_version: 3, outcome: 'not_reproduced', summary: '请求未取得受保护内容。', tested_scope: '仅核对隔离合成原件，不访问目标或验证生产环境。', limitations: [], evidence_refs: [http.request, http.response, log.artifact], evidence_items: [http, log], key_evidence: [{ reason: '响应与观察一致。', evidence_item_ids: ['http-a', 'log-a'] }], test_steps: [
        { action: '确认本次条件', observation: '仅检查本地样本。', evidence_item_ids: [] },
        { action: '发送样本请求', observation: '本次响应为拒绝访问。', evidence_item_ids: ['http-a', 'log-a'] },
        { action: '对照同一份记录', observation: '引用已有交互，不声称再次发送。', evidence_item_ids: ['http-a'] },
      ] },
      input_source: null, report: null, runs: [], artifacts: Object.keys(contents).map((id) => ({ id, size: 64 })), history: [], cleanup: { status: 'removed', error: null }, source: { type: 'user', name: '本地测试' }, tenant_workers: [],
    })
    const wrapper = await mountPage(VerificationDetail)
    expect(wrapper.findAll('.verification-test-step h3').map((heading) => heading.text())).toEqual(['步骤 1', '步骤 2', '步骤 3'])
    expect(wrapper.text()).toContain('引用已有交互，不声称再次发送。')
    expect(wrapper.findAllComponents(EvidenceReader)).toHaveLength(0)
    expect(mocks.artifactPreview).not.toHaveBeenCalled()
    await click(wrapper, '本轮观察 · 步骤 2')
    expect(wrapper.text()).toContain('同一交互的独立观察原文')
    expect(wrapper.text()).not.toContain('GET /sample HTTP/1.1')
    expect(wrapper.findComponent(EvidenceReader).props('selectedItemId')).toBe('log-a')
    await click(wrapper, '受保护响应 · 步骤 2')
    expect(wrapper.findAllComponents(EvidenceReader)).toHaveLength(2)
    expect(wrapper.findAll('.verification-test-step')[1]!.text()).toContain('HTTP/1.1 403 Forbidden')
    expect(wrapper.findAll('.verification-test-step')[2]!.findComponent(EvidenceReader).exists()).toBe(false)
    expect(document.activeElement).toBe(wrapper.findAll('.verification-test-step')[1]!.get('summary').element)
    expect(mocks.artifactPreview).toHaveBeenCalledWith('request-a', 'response-original', expect.any(AbortSignal))
    expect(mocks.artifactPreview).toHaveBeenCalledWith('request-a', 'log-original', expect.any(AbortSignal))
  })

  it.each([false, true])('v3未关联证据可直接阅读和依据定位，空步骤=%s且不虚构步骤', async (emptySteps) => {
    const primary = { id: 'primary', kind: 'http', role: 'primary', source: 'capture', title: '本轮交互', request: { artifact_id: 'request-original' } }
    const keyOnly = { id: 'key-only', kind: 'text', role: 'supporting', source: 'capture', title: '独立关键原文', artifact: { artifact_id: 'key-original' } }
    const supporting = { id: 'supporting', kind: 'text', role: 'supporting', source: 'capture', title: '独立补充记录', artifact: { artifact_id: 'supporting-original' } }
    const contents: Record<string, string> = { 'request-original': 'GET /sample HTTP/1.1', 'key-original': '只被关键依据引用的原文\n原始第二行', 'supporting-original': '没有步骤或关键依据引用的补充原文' }
    mocks.artifactPreview.mockImplementation((_requestId: string, artifactId: string) => Promise.resolve({ project_id: 'execution-project', artifact_id: artifactId, content: contents[artifactId], truncated: false }))
    mocks.request.mockResolvedValue({
      id: 'request-a', project_id: 'execution-project', title: '未关联证据', mode: 'retest', execution_status: 'completed', can_delete: true,
      result: { schema_version: 3, outcome: 'not_reproduced', summary: '原件独立留存，不推造操作。', tested_scope: '仅核对隔离合成原件，不访问目标或验证生产环境。', limitations: [], reason_code: null,
        evidence_refs: [primary.request, keyOnly.artifact, supporting.artifact], evidence_items: [keyOnly, primary, supporting],
        key_evidence: [{ reason: '直接核对独立原文及已有交互。', evidence_item_ids: ['key-only', 'primary'] }],
        test_steps: emptySteps ? [] : [{ action: '核对已有交互', observation: '未再次发送请求。', evidence_item_ids: ['primary'] }, { action: '对照同一原件', observation: '共享同一证据。', evidence_item_ids: ['primary'] }],
      },
      input_source: null, report: null, runs: [], artifacts: Object.keys(contents).map((id) => ({ id, size: 64 })), history: [], cleanup: { status: 'removed', error: null }, source: { type: 'user', name: '本地测试' }, tenant_workers: [],
    })
    const writeText = vi.fn().mockResolvedValue(undefined)
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText } })
    const wrapper = await mountPage(VerificationDetail)
    expect(wrapper.findAllComponents(EvidenceReader)).toHaveLength(0)
    expect(mocks.artifactPreview).not.toHaveBeenCalled()
    await click(wrapper, '独立关键原文 · 未关联步骤')
    const materials = wrapper.get('section[aria-label="其他材料"]')
    expect(materials.text()).toContain(contents['key-original'])
    expect(wrapper.findComponent(EvidenceReader).props('selectedItemId')).toBe('key-only')
    await expectOriginalUrl(materials, '记录原文', '/api/v1/verification-requests/request-a/artifacts/key-original')
    await materials.get('button[aria-label="记录原文复制原文"]').trigger('click')
    await flushPromises()
    expect(writeText).toHaveBeenCalledWith(contents['key-original'])
    const supportingDisclosure = wrapper.findAllComponents(EvidenceItemDisclosure).find((item) => item.props('item').id === 'supporting')!
    await supportingDisclosure.vm.reveal()
    await flushPromises()
    expect(materials.text()).toContain(contents['supporting-original'])
    expect(wrapper.findAllComponents(EvidenceReader)).toHaveLength(2)
    expect(wrapper.findAll('.verification-test-step')).toHaveLength(emptySteps ? 1 : 3)
    if (emptySteps) {
      expect(wrapper.text()).toContain('本次未记录已执行的测试步骤。')
      expect(wrapper.text()).not.toContain('步骤 1')
    } else {
      await click(wrapper, '本轮交互 · 步骤 1')
      expect(wrapper.findAll('.verification-test-step')[0]!.text()).toContain(contents['request-original'])
      expect(wrapper.findAll('.verification-test-step')[1]!.findComponent(EvidenceReader).exists()).toBe(false)
    }
  })

  it('来源步骤仅使用所选Observation，inline懒挂且切换来源释放阅读器', async () => {
    const preview = await mocks.preview()
    preview.evidence = {
      observation_id: 'selected-old', source: 'capture',
      reproduction_steps: [{ action: '旧发现真实操作', observation: '旧发现真实观察', evidence_item_ids: ['inline', 'missing'] }],
      evidence_items: [
        { id: 'inline', kind: 'http', title: '旧发现交互', source: 'manual', request_text: 'GET /old HTTP/1.1' },
        { id: 'attachment', kind: 'artifact', title: '未绑定附件', artifact: { artifact_id: 'large-file' } },
      ],
    }
    preview.artifacts = [{ artifact_id: 'large-file', size_bytes: 800000, available: true }]
    preview.observations = [{ id: 'latest-other' }]
    const wrapper = await mountPage(VerificationSourcePreview, { preview })
    expect(wrapper.text()).toContain('旧发现真实操作')
    expect(wrapper.text()).toContain('旧发现真实观察')
    expect(wrapper.text()).toContain('missing（未找到引用）')
    expect(wrapper.findAllComponents(EvidenceReader)).toHaveLength(0)
    const shells = wrapper.findAllComponents(EvidenceItemDisclosure)
    expect(shells[1]!.props('artifactSize')!({ artifact_id: 'large-file' })).toBe(800000)
    await shells[0]!.vm.reveal()
    await flushPromises()
    expect(wrapper.text()).toContain('GET /old HTTP/1.1')
    expect(wrapper.findComponent(EvidenceReader).props('entityKey')).toContain('selected-old')
    await wrapper.setProps({ preview: { ...preview, evidence: { observation_id: 'new-source', source: 'capture', reproduction_steps: [], evidence_items: [] }, vulnerability: { ...preview.vulnerability, repro_steps: '真实历史复现正文' } } })
    expect(wrapper.findAllComponents(EvidenceReader)).toHaveLength(0)
    expect(wrapper.text()).toContain('真实历史复现正文')
    expect(wrapper.text()).not.toContain('GET /old HTTP/1.1')
    expect(mocks.artifactPreview).not.toHaveBeenCalled()
  })

  it('复制API拒绝按侧反馈且不假成功，手工恢复选中Raw原文', async () => {
    const request = 'POST /sample HTTP/1.1\r\nContent-Type: application/json\r\n\r\n{"dup":1,"dup":2,"large":9007199254740993123}'
    const writeText = vi.fn().mockRejectedValue(new DOMException('denied', 'NotAllowedError'))
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText } })
    const wrapper = await mountPage(EvidenceReader, {
      entityKey: 'copy-a', projectId: 'project-a', items: [{ id: 'http-a', kind: 'http', role: 'primary', source: 'manual', request_text: request, response_text: 'HTTP/1.1 200 OK' }], loadPreview: vi.fn(), originalUrl: () => null,
    })
    const panel = wrapper.get('article[aria-label="请求阅读器"]')
    await click(panel, 'Pretty')
    await panel.get('button[aria-label="请求复制原文"]').trigger('click')
    await flushPromises()
    expect(writeText).toHaveBeenCalledWith(request)
    expect(panel.get('[role="status"]').text()).toContain('浏览器未允许剪贴板写入')
    expect(wrapper.get('article[aria-label="响应阅读器"]').find('[role="status"]').exists()).toBe(false)
    expect(wrapper.text()).not.toContain('已复制原文')
    await click(new DOMWrapper(document.body), '选择当前文本手工复制')
    expect(mocks.selectRaw).toHaveBeenCalledOnce()
    expect(panel.get('[data-testid="evidence-content"]').text()).toContain('"dup":1,"dup":2,"large":9007199254740993123')
    expect(panel.findAll('button').find((button) => button.text() === 'Raw')?.attributes('aria-pressed')).toBe('true')
  })

  it('flow默认读取正文，显式投影按绝对UTF8字节分页和定位选段，保留历史来源及缺侧', async () => {
    const loadPreview = vi.fn()
    const writeText = vi.fn().mockResolvedValue(undefined)
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText } })
    const wrapper = await mountPage(EvidenceReader, {
      entityKey: 'observation-a', projectId: 'project-a', items: [flowEvidence], selectedItemId: 'flow-item', loadPreview, originalUrl: () => null,
    })
    expect(mocks.trafficFlow).toHaveBeenCalledWith('project-a', 'flow-a', expect.any(AbortSignal))
    expect(mocks.trafficReadable).toHaveBeenCalledExactlyOnceWith('project-a', 'flow-a', 'request', expect.any(AbortSignal), 'auto')
    expect(mocks.trafficProjection).not.toHaveBeenCalled()
    expect(wrapper.get('[data-testid="evidence-content"]').element.textContent).toBe(flowHeader + flowReadable.body_text)
    await wrapper.get('button[aria-label="请求复制正文视图"]').trigger('click')
    await flushPromises()
    expect(writeText).toHaveBeenLastCalledWith(flowHeader + flowReadable.body_text)
    expect(wrapper.get('[role="status"]').text()).toBe('已复制正文视图')
    await click(wrapper, '投影')
    expect(mocks.trafficProjection).toHaveBeenCalledTimes(1)
    await wrapper.get('button[aria-label="请求复制 v1 投影片段"]').trigger('click')
    await flushPromises()
    expect(writeText).toHaveBeenLastCalledWith(flowProjection.text)
    expect(wrapper.get('[role="status"]').text()).toBe('已复制 v1 投影片段')
    expect(loadPreview).not.toHaveBeenCalled()
    expect(wrapper.text()).toContain('历史原文')
    expect(wrapper.text()).toContain('结构化重建，非原始字节流')
    expect(wrapper.text()).toContain('run-original')
    expect(wrapper.text()).toContain('部分记录')
    expect(wrapper.text()).toContain('未记录响应')
    expect(wrapper.get('[data-testid="evidence-content"]').text()).toContain(flowHeader)
    await expectOriginalUrl(wrapper, '请求', '/projects/project-a/traffic/flow-a/bodies/request')
    await click(wrapper, '下一页')
    expect(mocks.trafficProjection).toHaveBeenLastCalledWith('project-a', 'flow-a', 'request', 65536, 65536, expect.any(AbortSignal))
    expect(wrapper.text()).toContain('UTF-8 字节 65536–70004 / 70004')
    await click(wrapper, '上一页')
    expect(wrapper.text()).toContain('UTF-8 字节 0–65536 / 70004')
    await click(wrapper, '选段 1 · utf8-bytes=65536:65542')
    expect(mocks.selectTextRange).toHaveBeenCalledWith(0, 2)
    expect(wrapper.getComponent(EvidenceReader).props('selectedItemId')).toBe('flow-item')
  })

  it('flow选段拒绝摘要变化和二进制占位，终态原正文下载仍可达', async () => {
    mocks.trafficReadable.mockResolvedValue({ ...flowReadable, decode_status: 'non-text', body_text: '', decoded_body_bytes: null })
    mocks.trafficProjection.mockResolvedValue({ ...flowProjection, body_display: 'binary' })
    const wrapper = await mountPage(EvidenceReader, { entityKey: 'binary', projectId: 'project-a', items: [flowEvidence], loadPreview: vi.fn(), originalUrl: () => null })
    expect(mocks.trafficProjection).not.toHaveBeenCalled()
    await click(wrapper, '投影')
    expect(wrapper.text()).toContain('二进制正文 · 投影仅含头部与占位')
    expect(wrapper.findAll('button').some((button) => button.text() === 'Pretty')).toBe(false)
    await expectOriginalUrl(wrapper, '请求', '/projects/project-a/traffic/flow-a/bodies/request')
    mocks.trafficProjection.mockResolvedValue({ ...flowProjection, offset: 65536, body_display: 'binary' })
    await click(wrapper, '选段 1 · utf8-bytes=65536:65542')
    expect(wrapper.get('[role="alert"]').text()).toContain('二进制占位不是正文证据')
    expect(mocks.selectTextRange).not.toHaveBeenCalled()
    mocks.trafficProjection.mockResolvedValue({ ...flowProjection, offset: 65536, sha256: 'c'.repeat(64) })
    await click(wrapper, '重试')
    expect(wrapper.get('[role="alert"]').text()).toContain('版本或摘要不匹配')
    expect(wrapper.find('[data-testid="evidence-content"]').exists()).toBe(false)
  })

  it('flow尚未终结不读投影，切换条目后迟到投影不覆盖历史Artifact原件', async () => {
    mocks.trafficFlow.mockResolvedValueOnce({ ...flowDetail, state: 'receiving' })
    const loadPreview = vi.fn().mockResolvedValue({ artifact_id: 'old-a', project_id: 'project-a', content: '历史原件未改变', size: 24, bytes_read: 24, truncated: false })
    const wrapper = await mountPage(EvidenceReader, { entityKey: 'pending', projectId: 'project-a', items: [flowEvidence], autoRefresh: false, loadPreview, originalUrl: () => null })
    expect(mocks.trafficProjection).not.toHaveBeenCalled()
    expect(wrapper.text()).toContain('当前预览非稳定证据')
    expect(mocks.trafficPreview).not.toHaveBeenCalled()
    expect(mocks.trafficReadable).not.toHaveBeenCalled()
    expect(wrapper.get('[data-testid="evidence-content"]').text()).toContain(flowHeader.trim())
    await click(wrapper, '加载请求正文')
    expect(mocks.trafficPreview).toHaveBeenCalledExactlyOnceWith('project-a', 'flow-a', 'request', 0, expect.any(AbortSignal))
    expect(wrapper.get('[data-testid="evidence-content"]').text()).toContain('已确认前缀')
    let resolve!: (value: TrafficProjection) => void
    mocks.trafficProjection.mockImplementationOnce(() => new Promise<TrafficProjection>((done) => { resolve = done }))
    await click(wrapper, '刷新状态')
    expect(mocks.trafficReadable).toHaveBeenCalledOnce()
    await click(wrapper, '投影')
    expect(mocks.trafficProjection).toHaveBeenCalledOnce()
    const projectionSignal = mocks.trafficProjection.mock.calls[0]![5] as AbortSignal
    await wrapper.setProps({ items: [{ id: 'old', kind: 'artifact', role: 'primary', source: 'historical', title: '旧原件', artifact: { artifact_id: 'old-a' } }] })
    await flushPromises()
    expect(projectionSignal.aborted).toBe(true)
    expect(loadPreview).not.toHaveBeenCalled()
    await click(wrapper, '加载证据原件内容')
    expect(loadPreview).toHaveBeenCalledExactlyOnceWith({ artifact_id: 'old-a' }, expect.any(AbortSignal))
    resolve(flowProjection)
    await flushPromises()
    expect(wrapper.get('[data-testid="evidence-content"]').text()).toBe('历史原件未改变')
    expect(wrapper.text()).not.toContain('GET /中文')
  })

  it('flow接收时按需读正文，进度未变化不重复读取，终态回到正文且Hex读取真实字节', async () => {
    mocks.trafficFlow.mockResolvedValueOnce({ ...flowDetail, state: 'receiving' }).mockResolvedValueOnce({ ...flowDetail, state: 'receiving' })
    const wrapper = await mountPage(EvidenceReader, { entityKey: 'live', projectId: 'project-a', items: [flowEvidence], autoRefresh: false, loadPreview: vi.fn(), originalUrl: () => null })
    expect(mocks.trafficPreview).not.toHaveBeenCalled()
    await click(wrapper, '加载请求正文')
    expect(mocks.trafficPreview).toHaveBeenCalledTimes(1)
    await wrapper.getComponent(EvidenceReader).vm.refreshFlow()
    expect(mocks.trafficPreview).toHaveBeenCalledTimes(1)
    await wrapper.getComponent(EvidenceReader).vm.refreshFlow()
    await flushPromises()
    expect(mocks.trafficReadable).toHaveBeenCalledExactlyOnceWith('project-a', 'flow-a', 'request', expect.any(AbortSignal), 'auto')
    expect(mocks.trafficPreview).toHaveBeenCalledTimes(1)
    expect(mocks.trafficProjection).not.toHaveBeenCalled()
    expect(wrapper.get('[data-testid="evidence-content"]').element.textContent).toBe(flowHeader + flowReadable.body_text)
    await click(wrapper, 'Hex')
    expect(mocks.trafficBody).toHaveBeenCalledWith('project-a', 'flow-a', 'request', 0, expect.any(AbortSignal))
    expect(wrapper.get('[data-testid="evidence-content"]').text()).toContain('00000000  00 ff 41 42')
    expect(wrapper.get('button[aria-label="请求复制当前 Hex 展示"]').exists()).toBe(true)
    await wrapper.get('button[aria-label="请求更多"]').trigger('click')
    await flushPromises()
    expect(document.body.textContent).toContain('正文 Hex / ASCII 显示已保存的正文原字节')
    await new DOMWrapper(document.body).get('[role="menu"]').trigger('keydown', { key: 'Escape' })
    await flushPromises()
    expect(wrapper.findAll('button').some((button) => button.text() === 'Pretty')).toBe(false)
    await click(wrapper, '选段 1 · utf8-bytes=65536:65542')
    expect(mocks.trafficProjection).toHaveBeenLastCalledWith('project-a', 'flow-a', 'request', 65536, 65536, expect.any(AbortSignal))
    expect(mocks.selectTextRange).toHaveBeenCalledWith(0, 2)
  })

  it('复制失败浮层的稳定关联支持ArrowDown进入恢复并以Escape返回同侧按钮', async () => {
    const writeText = vi.fn().mockRejectedValue(new DOMException('denied', 'NotAllowedError'))
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText } })
    const wrapper = await mountPage(EvidenceReader, {
      entityKey: 'copy-keyboard', projectId: 'project-a', items: [{ id: 'http-a', kind: 'http', role: 'primary', source: 'manual', request_text: 'GET /sample HTTP/1.1\r\n\r\n', response_text: 'HTTP/1.1 200 OK\r\n\r\n' }], loadPreview: vi.fn(), originalUrl: () => null,
    })
    const copyButton = wrapper.get('button[aria-label="响应复制原文"]')
    ;(copyButton.element as HTMLElement).focus()
    await copyButton.trigger('click')
    await flushPromises()
    expect(copyButton.attributes('aria-expanded')).toBe('true')
    const controlledId = copyButton.attributes('aria-controls')
    expect(controlledId).toBeTruthy()
    const feedbackBody = document.getElementById(controlledId!)
    expect(feedbackBody).not.toBeNull()
    expect(feedbackBody!.classList.contains('evidence-reader__copy-body')).toBe(true)
    expect(wrapper.element.contains(feedbackBody)).toBe(false)
    expect(feedbackBody!.closest('[role="dialog"]')?.getAttribute('aria-label')).toBe('响应复制反馈')
    const recovery = feedbackBody!.querySelector<HTMLButtonElement>('button')!
    expect(recovery.textContent).toBe('选择当前文本手工复制')
    await copyButton.trigger('keydown', { key: 'ArrowDown' })
    expect(document.activeElement).toBe(recovery)
    expect(copyButton.attributes('aria-controls')).toBe(controlledId)
    recovery.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))
    await flushPromises()
    expect(copyButton.attributes('aria-expanded')).toBe('false')
    expect(copyButton.attributes('aria-controls')).toBeUndefined()
    expect(document.activeElement).toBe(copyButton.element)
    expect(document.getElementById(controlledId!)).toBeNull()
  })

  it('复制API缺失使用用户触发命令，检查返回值并恢复焦点且不遗留节点', async () => {
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: undefined })
    const setData = vi.fn()
    const command = vi.fn().mockImplementationOnce(() => {
      const event = new Event('copy', { bubbles: true, cancelable: true })
      Object.defineProperty(event, 'clipboardData', { value: { setData } })
      document.activeElement?.dispatchEvent(event)
      expect(event.defaultPrevented).toBe(true)
      return true
    }).mockReturnValueOnce(false).mockReturnValueOnce(true)
    Object.defineProperty(document, 'execCommand', { configurable: true, value: command })
    const request = 'GET /sample HTTP/1.1\r\nX-Sample: first\r\nX-Sample: second\r\n\r\n'
    const wrapper = await mountPage(EvidenceReader, {
      entityKey: 'copy-a', projectId: 'project-a', items: [{ id: 'http-a', kind: 'http', role: 'primary', source: 'manual', request_text: request, response_text: 'HTTP/1.1 200 OK' }], loadPreview: vi.fn(), originalUrl: () => null,
    })
    const requestCopy = wrapper.get('button[aria-label="请求复制原文"]')
    ;(requestCopy.element as HTMLElement).focus()
    await requestCopy.trigger('click')
    await flushPromises()
    expect(command).toHaveBeenCalledWith('copy')
    expect(setData).toHaveBeenCalledWith('text/plain', request)
    expect(wrapper.get('article[aria-label="请求阅读器"] [role="status"]').text()).toBe('已复制原文')
    expect(document.activeElement).toBe(requestCopy.element)
    expect(wrapper.find('textarea').exists()).toBe(false)
    await wrapper.get('button[aria-label="响应复制原文"]').trigger('click')
    await flushPromises()
    expect(wrapper.get('article[aria-label="响应阅读器"] [role="status"]').text()).toContain('浏览器复制命令也未完成')
    expect(wrapper.find('textarea').exists()).toBe(false)
    await requestCopy.trigger('click')
    await flushPromises()
    expect(wrapper.get('article[aria-label="请求阅读器"] [role="status"]').text()).toContain('浏览器复制命令也未完成')
    expect(setData).toHaveBeenCalledOnce()
    delete (document as unknown as { execCommand?: unknown }).execCommand
  })

  it('迟到复制结果不覆盖同侧新操作或新交互', async () => {
    let rejectFirst!: (error: Error) => void
    let resolveOld!: () => void
    const writeText = vi.fn().mockImplementationOnce(() => new Promise((_, reject) => { rejectFirst = reject }))
      .mockResolvedValueOnce(undefined).mockImplementationOnce(() => new Promise<void>((resolve) => { resolveOld = resolve }))
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText } })
    const item: EvidenceItem = { id: 'http-a', kind: 'http', role: 'primary', source: 'manual', title: '受控复制样本', request_text: 'GET /sample HTTP/1.1', response_text: null }
    const wrapper = await mountPage(EvidenceReader, { entityKey: 'copy-a', projectId: 'project-a', items: [item], loadPreview: vi.fn(), originalUrl: () => null })
    const button = wrapper.get('button[aria-label="请求复制原文"]')
    await button.trigger('click')
    await button.trigger('click')
    await flushPromises()
    rejectFirst(new Error('old failure'))
    await flushPromises()
    expect(wrapper.get('[role="status"]').text()).toBe('已复制原文')
    await button.trigger('click')
    await wrapper.setProps({ items: [{ ...item, request_text: 'GET /changed HTTP/1.1' }] })
    resolveOld()
    await flushPromises()
    expect(wrapper.find('[role="status"]').exists()).toBe(false)
    expect(wrapper.text()).toContain('GET /changed HTTP/1.1')
  })

  it('清理未完成仅显示最小投影和显式重试，不读取原档案或提供再次复测', async () => {
    mocks.request.mockResolvedValue({
      id: 'request-a', project_id: 'execution-project', title: '清理未完成的复测', mode: 'retest', execution_status: 'cleanup_pending', can_delete: true,
      purge_state: 'pending', purge_error: '文件清理失败，请重试。', result: null, error: null, input_source: null,
      report: null, runs: [], artifacts: [], history: [], cleanup: { status: 'removed', error: null }, source: { type: 'user', name: '本地测试' }, tenant_workers: [],
    })
    const wrapper = await mountPage(VerificationDetail)
    expect(wrapper.text()).toContain('清理未完成，可重试')
    expect(wrapper.text()).toContain('文件清理失败，请重试。')
    expect(wrapper.text()).not.toContain('再次复测')
    expect(wrapper.find('.verification-reading').exists()).toBe(false)
    expect(wrapper.find('.verification-activity').exists()).toBe(false)
    expect(wrapper.findAllComponents(EvidenceReader)).toHaveLength(0)
    await click(wrapper, '重试清理')
    expect(document.body.textContent).toContain('永久删除此轮复测')
    expect(mocks.artifactPreview).not.toHaveBeenCalled()
    expect(mocks.materialPreview).not.toHaveBeenCalled()
    expect(mocks.fetchText).not.toHaveBeenCalled()
  })

  it.each(['running', 'failed', 'cancelled'])('%s无业务结果时不展示空报告或虚构步骤', async (status) => {
    mocks.request.mockResolvedValue({
      id: 'request-a', project_id: 'execution-project', title: '无结果状态', mode: 'retest', execution_status: status, can_delete: false,
      result: null, error: status === 'failed' ? { code: 'local_failure', message: '隔离样本失败原因' } : null, execution_message: status === 'running' ? '正在处理本地样本。' : null, input_source: null,
      report: null, runs: [], artifacts: [], history: [], cleanup: { status: 'pending', error: null }, source: { type: 'user', name: '本地测试' }, tenant_workers: [],
    })
    const wrapper = await mountPage(VerificationDetail)
    expect(wrapper.find('.verification-reading').exists()).toBe(false)
    expect(wrapper.findComponent(EvidenceReader).exists()).toBe(false)
    expect(wrapper.find('.verification-activity').exists()).toBe(status === 'running')
    if (status === 'failed') expect(wrapper.text()).toContain('隔离样本失败原因')
    if (status === 'cancelled') expect(wrapper.text()).toContain('本次执行已停止，尚无结果')
  })

  it('单条主证据可从两条补充材料返回，已取消的迟到预览不覆盖当前内容', async () => {
    const http = { id: 'http-a', kind: 'http', role: 'primary', source: 'capture', title: '关键交互', request: { artifact_id: 'request-artifact' }, response: { artifact_id: 'response-artifact' } }
    const support = (id: string, title: string) => ({ id, kind: 'artifact', role: 'supporting', source: 'capture', title, artifact: { artifact_id: id } })
    const items = [http, support('log-artifact', '请求日志'), support('context-artifact', '环境记录')]
    let resolveLog!: (value: unknown) => void
    const delayedLog = new Promise((resolve) => { resolveLog = resolve })
    const preview = (artifactId: string, content: string) => ({ project_id: 'execution-project', artifact_id: artifactId, content, truncated: false })
    const contents: Record<string, string> = { 'request-artifact': 'GET /current HTTP/1.1', 'response-artifact': 'HTTP/1.1 200 OK', 'context-artifact': '当前环境原文' }
    mocks.artifactPreview.mockImplementation((_requestId: string, artifactId: string) => artifactId === 'log-artifact' ? delayedLog : Promise.resolve(preview(artifactId, contents[artifactId]!)))
    const wrapper = await mountPage(EvidenceReader, {
      entityKey: 'request-a', projectId: 'execution-project', items,
      loadPreview: (reference: EvidenceRef, signal: AbortSignal) => mocks.artifactPreview('request-a', reference.artifact_id, signal),
      originalUrl: (reference: EvidenceRef) => `/controlled/${reference.artifact_id}`,
    })
    expect(wrapper.find('details.evidence-reader__supporting').exists()).toBe(false)
    expect(mocks.artifactPreview).not.toHaveBeenCalled()
    expect(wrapper.text()).toContain('已保存大小未知')
    await click(wrapper, '加载请求正文')
    await click(wrapper, '加载响应正文')
    expect(mocks.artifactPreview).toHaveBeenCalledTimes(2)
    expect(wrapper.text()).toContain('GET /current HTTP/1.1')
    expect(wrapper.text()).toContain('HTTP/1.1 200 OK')
    const interaction = wrapper.get('select[aria-label="选择证据交互"]')
    expect((interaction.element as HTMLSelectElement).value).toBe('http-a')
    expect(interaction.findAll('option').map((option) => option.text())).toEqual(['主证据 · 关键交互', '补充 · 请求日志', '补充 · 环境记录'])

    await interaction.setValue('log-artifact')
    await flushPromises()
    expect(mocks.artifactPreview).toHaveBeenCalledTimes(2)
    await click(wrapper, '加载证据原件内容')
    const logSignal = mocks.artifactPreview.mock.calls.find((call) => call[1] === 'log-artifact')![2] as AbortSignal
    const primary = wrapper.get('nav[aria-label="关键交互"]').get('button')
    expect(primary.attributes('aria-pressed')).toBe('false')
    const supporting = wrapper.get('details.evidence-reader__supporting')
    await supporting.get('summary').trigger('click')
    await click(wrapper, '环境记录')
    expect(mocks.artifactPreview).toHaveBeenCalledTimes(3)
    await click(wrapper, '加载证据原件内容')
    expect(mocks.artifactPreview).toHaveBeenCalledTimes(4)
    expect(logSignal.aborted).toBe(true)
    resolveLog(preview('log-artifact', '迟到的旧日志'))
    await flushPromises()
    expect(wrapper.text()).toContain('当前环境原文')
    expect(wrapper.text()).not.toContain('迟到的旧日志')

    await primary.trigger('click')
    await flushPromises()
    expect((wrapper.get('select[aria-label="选择证据交互"]').element as HTMLSelectElement).value).toBe('http-a')
    expect(mocks.artifactPreview).toHaveBeenCalledTimes(4)
    await click(wrapper, '加载请求正文')
    await click(wrapper, '加载响应正文')
    expect(mocks.artifactPreview).toHaveBeenCalledTimes(6)
    expect(wrapper.text()).toContain('GET /current HTTP/1.1')
    expect(wrapper.text()).toContain('HTTP/1.1 200 OK')
    expect(wrapper.text()).not.toContain('当前环境原文')
    await expectOriginalUrl(wrapper, '请求', '/controlled/request-artifact')
    await expectOriginalUrl(wrapper, '响应', '/controlled/response-artifact')
  })

  it.each([1, 2])('历史v%s保留真实正文与限制，不推造步骤或回查当前漏洞', async (version) => {
    mocks.request.mockResolvedValue({
      id: 'request-a', project_id: 'execution-project', title: '旧证据', mode: 'retest', execution_status: 'completed', can_delete: true,
      result: { schema_version: version, outcome: 'confirmed', summary: 'Original historical conclusion remains unchanged.', tested_scope: '历史范围', notes: 'Historical observations stay in their original language.', limitations: ['历史实际限制'], reason_code: null, evidence_refs: [{ artifact_id: 'old-artifact' }], ...(version === 2 ? { evidence_items: [] } : {}) },
      input_source: { project_id: 'project-a', vulnerability_id: 'vuln-a', input_mode: 'copied_snapshot' }, report: { resource_id: 'snapshot-old' },
      materials: [], runs: [], artifacts: [], history: [], cleanup: { status: 'cleaned', error: null }, source: { type: 'user', name: '本地测试' }, tenant_workers: [],
    })
    const wrapper = await mountPage(VerificationDetail)
    expect(wrapper.text()).toContain('Original historical conclusion remains unchanged.')
    expect(wrapper.text()).toContain('Historical observations stay in their original language.')
    expect(wrapper.text()).toContain('历史实际限制')
    expect(wrapper.text()).toContain('历史记录未单独记录判断依据')
    expect(wrapper.find('.verification-test-steps').exists()).toBe(false)
    if (version === 1) {
      expect(wrapper.text()).toContain('历史结果未记录交互配对')
      expect(wrapper.get('a[download]').attributes('href')).toBe('/api/v1/verification-requests/request-a/artifacts/old-artifact')
    }
    expect(mocks.fetchText).not.toHaveBeenCalled()
    expect(mocks.artifactPreview).not.toHaveBeenCalled()
    expect(mocks.materialPreview).not.toHaveBeenCalled()
    expect(mocks.preview).not.toHaveBeenCalled()
    expect(mocks.vulnerability).not.toHaveBeenCalled()
  })


  it('资料收起后仍由同一表单持有待上传文件，离开拒绝不清空队列', async () => {
    const wrapper = await mountPage(NewVerification)
    const file = new File(['synthetic input'], 'input.txt', { type: 'text/plain' })
    const materials = wrapper.findComponent({ name: 'VerificationMaterials' })
    materials.vm.$emit('update:files', [{ id: 'pending-a', file, path: 'input.txt', kind: 'other', error: '' }])
    await flushPromises()
    await click(wrapper, '收起')
    expect(materials.props('files')[0].file).toBe(file)
    expect(wrapper.text()).toContain('1 个待上传')
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false)
    await click(wrapper, '取消')
    expect(confirm).toHaveBeenCalledOnce()
    expect(wrapper.emitted('close')).toBeUndefined()
    expect(materials.props('files')[0].file).toBe(file)
    expect(mocks.createRequest).not.toHaveBeenCalled()
  })

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

  it('原UUID已删除的权威410是可关闭终点，不自动换ID或继续创建', async () => {
    mocks.createRequest.mockRejectedValueOnce(new ApiError('Gone', 410, { detail: '此 request_id 对应的复测记录已删除，请使用新的请求 ID' }))
    const wrapper = await mountPage(NewVerification)
    await fillManual(wrapper)
    await submit(wrapper)
    expect(wrapper.text()).toContain('原请求已删除')
    expect(wrapper.text()).not.toContain('用原请求重试')
    expect(wrapper.text()).not.toContain('修改未受理的输入')
    expect(wrapper.get('fieldset').attributes('disabled')).toBeDefined()
    expect(mocks.createRequest).toHaveBeenCalledOnce()
    const confirm = vi.spyOn(window, 'confirm')
    await click(wrapper, '取消')
    expect(wrapper.emitted('close')).toHaveLength(1)
    expect(confirm).not.toHaveBeenCalled()
    expect(mocks.createRequest).toHaveBeenCalledOnce()
  })

  it('来源清理未完成时拒绝复制或受理，不读取残缺输入', async () => {
    mocks.route.query = { source_request_id: 'request-a' }
    mocks.request.mockResolvedValue({ id: 'request-a', title: '待清理来源', purge_state: 'pending', report: null })
    const wrapper = await mountPage(NewVerification)
    expect(wrapper.text()).toContain('来源复测已进入永久清理，不能再复制或受理')
    expect(wrapper.get('fieldset').attributes('disabled')).toBeDefined()
    await submit(wrapper)
    expect(mocks.createBatch).not.toHaveBeenCalled()
    expect(mocks.createRequest).not.toHaveBeenCalled()
    expect(mocks.materialPreview).not.toHaveBeenCalled()
  })

  it('复制后来源删除的权威410明确未受理，可关闭且不清除独立资料批次', async () => {
    mocks.route.query = { source_request_id: 'request-a' }
    mocks.request.mockResolvedValue({ id: 'request-a', title: '旧轮资料', purge_state: null, report: { resource_id: 'resource-a' }, input_source: null })
    mocks.createRequest.mockRejectedValueOnce(new ApiError('Gone', 410, { detail: '来源复测已删除，不能复制或作为上一轮受理，请从原漏洞新建复测' }))
    const wrapper = await mountPage(NewVerification)
    await click(wrapper, '复制上一轮输入')
    expect(mocks.createBatch).toHaveBeenCalledWith('request-a')
    await submit(wrapper)
    expect(mocks.createRequest).toHaveBeenCalledWith(expect.objectContaining({ previous_request_id: 'request-a', resource_batch_id: 'batch-a', report: { type: 'file', batch_member_id: 'member-a' } }))
    expect(wrapper.text()).toContain('服务端确认新一轮未受理')
    expect(wrapper.text()).toContain('已复制的独立资料批次仍保留')
    expect(wrapper.text()).not.toContain('请求可能已受理')
    expect(wrapper.text()).not.toContain('用原请求重试')
    expect(wrapper.text()).not.toContain('修改未受理的输入')
    const confirm = vi.spyOn(window, 'confirm')
    await click(wrapper, '取消')
    expect(wrapper.emitted('close')).toHaveLength(1)
    expect(confirm).not.toHaveBeenCalled()
    expect(mocks.cancelBatch).not.toHaveBeenCalled()
    expect(mocks.createRequest).toHaveBeenCalledOnce()
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

  it('来源列表刷新移除已选漏洞时清空预览但保留草稿，不自动换来源', async () => {
    mocks.route.query = { input: 'platform', source_project_id: 'project-a', source_vulnerability_id: 'vuln-a' }
    const wrapper = await mountPage(NewVerification)
    await wrapper.get('#new-verification-title').setValue('保留任务名称')
    await wrapper.get('#new-verification-origin').setValue('保留环境补充')
    expect(wrapper.findComponent(VerificationSourcePreview).exists()).toBe(true)
    mocks.listVulnerabilities.mockResolvedValue([{ id: 'other', project_id: 'project-a', project_title: '当前租户项目', title: '另一个漏洞' }])
    await clients[clients.length - 1]!.invalidateQueries({ queryKey: ['vulnerabilities', 'all'] })
    await flushPromises()
    expect(wrapper.findComponent(VerificationSourcePreview).exists()).toBe(false)
    expect(wrapper.text()).toContain('请重新选择来源')
    expect((wrapper.get('#new-verification-title').element as HTMLInputElement).value).toBe('保留任务名称')
    expect((wrapper.get('#new-verification-origin').element as HTMLTextAreaElement).value).toBe('保留环境补充')
    await submit(wrapper)
    expect(mocks.createRequest).not.toHaveBeenCalled()
    expect(mocks.preview).toHaveBeenCalledTimes(1)
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

  it('全局成果的其他租户行内详情保留查看，但不请求复测历史或提供复测入口', async () => {
    mocks.project.mockResolvedValue({ project: { id: 'project-a', tenant_id: 'tenant-b' } })
    const wrapper = await mountPage(VulnerabilityDetail, { projectId: 'project-a', vulnId: 'vuln-a' })
    expect(wrapper.text()).toContain('全局成果原文')
    expect(wrapper.text()).toContain('复测及关联历史仅适用于当前租户')
    expect(wrapper.text()).not.toContain('复测此漏洞')
    expect(mocks.history).not.toHaveBeenCalled()
  })

  it('当前租户行内详情原地复测，锁定所选发现且补充说明可空', async () => {
    const wrapper = await mountPage(VulnerabilityDetail, { projectId: 'project-a', vulnId: 'vuln-a' })
    expect(mocks.history).toHaveBeenCalledWith('project-a', 'vuln-a', 10, 0, expect.any(AbortSignal))
    await click(wrapper, '复测此漏洞')
    expect(mocks.push).not.toHaveBeenCalled()
    expect(mocks.preview).toHaveBeenCalledWith('project-a', 'vuln-a', expect.any(AbortSignal), 'observation-a')
    expect(mocks.listVulnerabilities).not.toHaveBeenCalled()
    expect(wrapper.find('#verification-source-vulnerability').exists()).toBe(false)
    expect(wrapper.get('#new-verification-origin').attributes('required')).toBeUndefined()
    mocks.createRequest.mockResolvedValueOnce({ id: 'accepted-platform', execution_status: 'queued' })
    await submit(wrapper)
    expect(mocks.createRequest).toHaveBeenCalledWith(expect.objectContaining({ origin: '', report: { type: 'platform_vulnerability', project_id: 'project-a', vulnerability_id: 'vuln-a', observation_id: 'observation-a' } }))
    expect(mocks.push).toHaveBeenCalledWith({ name: 'VerificationDetail', params: { id: 'accepted-platform' } })
  })
})
