import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { createMemoryHistory, createRouter, RouterView } from 'vue-router'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { AgentCleanupResult, AgentMessage, AgentWork } from '@/api/projectAgent'
import { ApiError } from '@/api/client'
import ProjectAgent from './ProjectAgent.vue'
import { createPinia, setActivePinia } from 'pinia'
import { useUIStore } from '@/stores/ui'

const mocks = vi.hoisted(() => ({
  api: vi.fn(), listAgentConversations: vi.fn(), createAgentConversation: vi.fn(),
  listAgentMessages: vi.fn(), listAgentWork: vi.fn(), sendAgentMessage: vi.fn(),
  deleteAgentConversation: vi.fn(), listAgentCleanupRequests: vi.fn(), getAgentCleanup: vi.fn(), stopAgentTurn: vi.fn(),
  listAgentAttachments: vi.fn(), getAgentAttachmentLimits: vi.fn(),
  uploadAgentAttachment: vi.fn(), deleteAgentAttachment: vi.fn(),
  getAgentConfiguration: vi.fn(), saveAgentConfiguration: vi.fn(), listTenantModels: vi.fn(),
}))
vi.mock('@/api/projectAgent', async original => ({
  ...await original<typeof import('@/api/projectAgent')>(), ...mocks,
}))
vi.mock('@/api/client', async original => ({
  ...await original<typeof import('@/api/client')>(), api: mocks.api, listTenantModels: mocks.listTenantModels,
}))

class FakeEventSource extends EventTarget {
  static instances: FakeEventSource[] = []
  onopen: (() => void) | null = null
  onerror: ((event?: Event) => void) | null = null
  close = vi.fn()
  url: string
  constructor(url: string) {
    super()
    this.url = url
    FakeEventSource.instances.push(this)
  }
  messages(rows: AgentMessage[], cursor: string) {
    this.dispatchEvent(new MessageEvent('messages', { data: JSON.stringify(rows), lastEventId: cursor }))
  }
}
const conversations = [
  { id: 'c1', project_id: 'p1', model_id: 'm1', model_display_name: '测试模型', model_name: 'test-model', available: true, unavailable_reason: null, created_at: '2026-10-28T00:00:00Z' },
  { id: 'c2', project_id: 'p1', model_id: 'm1', model_display_name: '测试模型', model_name: 'test-model', available: true, unavailable_reason: null, created_at: '2026-10-28T01:00:00Z' },
]
function message(overrides: Partial<AgentMessage> = {}): AgentMessage {
  return { id: 1, project_id: null, client_message_id: 'input-1', text: '查询已有进展', created_at: '2026-10-28T00:00:00Z',
    task_run_id: 'run1', status: 'running', answer: null, error_code: null, artifacts: [], actions: [], attachments: [], ...overrides }
}
function work(overrides: Partial<AgentWork> = {}): AgentWork {
  return { id: 'action-1', kind: 'verification', title: '复测 v001', status: 'running', error: null,
    url: '/verification-requests/vr_1', download_url: null, result: null, next_attempt_at: null, ...overrides }
}
function bodyButton(label: string) {
  const button = [...document.body.querySelectorAll<HTMLButtonElement>('button')].find(item => item.textContent?.trim() === label)
  if (!button) throw new Error(`找不到按钮：${label}`)
  return button
}
async function requestDelete(wrapper: VueWrapper, label = '会话 1菜单') {
  await wrapper.get(`button[aria-label="${label}"]`).trigger('click')
  await flushPromises()
  const item = document.body.querySelector<HTMLElement>('[role="menuitem"]')
  if (!item) throw new Error('找不到删除菜单项')
  item.click()
  await flushPromises()
}
const wrappers: VueWrapper[] = []
async function openPage(conversation = 'c1') {
  const router = createRouter({ history: createMemoryHistory(), routes: [
    { path: '/agent', component: ProjectAgent },
    { path: '/projects/:id/runs', component: { template: '<p>运行记录</p>' } },
    { path: '/console/workers', component: { template: '<p>Worker 配置</p>' } },
  ] })
  await router.push(`/agent${conversation ? `?conversation=${conversation}` : ''}`)
  await router.isReady()
  const wrapper = mount(RouterView, { global: { plugins: [router] }, attachTo: document.body })
  wrappers.push(wrapper)
  await flushPromises()
  return { wrapper, router }
}

beforeEach(() => {
  vi.clearAllMocks()
  setActivePinia(createPinia())
  FakeEventSource.instances = []
  vi.stubGlobal('EventSource', FakeEventSource)
  vi.stubGlobal('ResizeObserver', class { observe() {} disconnect() {} })
  mocks.listAgentConversations.mockResolvedValue(conversations)
  mocks.listAgentCleanupRequests.mockResolvedValue([])
  mocks.getAgentCleanup.mockResolvedValue({ conversation_id: 'c1', cleanup: null })
  mocks.listAgentMessages.mockResolvedValue([])
  mocks.listAgentWork.mockResolvedValue([])
  mocks.listAgentAttachments.mockResolvedValue([])
  mocks.getAgentAttachmentLimits.mockResolvedValue({ max_file_bytes: 10485760, max_count: 200, max_total_bytes: 104857600 })
  mocks.deleteAgentConversation.mockResolvedValue({ conversation_id: 'c1', cleanup: {
    request_id: 'cleanup-1', state: 'requested', attempts: 0, retry_at: 0, error_code: null, finished_at: null,
  } })
  mocks.deleteAgentAttachment.mockResolvedValue(undefined)
  mocks.getAgentConfiguration.mockResolvedValue({ model_id: 'm1', model_display_name: '测试模型', model_name: 'test-model', available: true, unavailable_reason: null })
  mocks.listTenantModels.mockResolvedValue([{ id: 'm1', display_name: '测试模型', model_name: 'test-model', enabled: true, credential_configured: true }])
  mocks.api.mockResolvedValue([{ id: 'p1', title: '项目一' }, { id: 'p2', title: '项目二' }])
})
afterEach(() => {
  wrappers.splice(0).forEach(wrapper => wrapper.unmount())
  useUIStore().$dispose()
  document.body.innerHTML = ''
  vi.unstubAllGlobals()
  vi.useRealTimers()
})

describe('全局 Agent', () => {
  it('停止只登记意图，旧消息不能清掉意图，确认退出前不显示已取消', async () => {
    mocks.listAgentMessages.mockResolvedValue([message()])
    mocks.stopAgentTurn.mockResolvedValue(message({ cancel_requested_at: '2026-11-02T00:00:00Z' }))
    const { wrapper } = await openPage()
    bodyButton('停止本轮').click()
    await flushPromises()
    expect(mocks.stopAgentTurn).toHaveBeenCalledExactlyOnceWith('c1', 1)
    expect(wrapper.text()).toContain('停止待确认')
    expect(wrapper.text()).toContain('独立复测／报告不受影响')
    FakeEventSource.instances.at(-1)!.messages([message()], '2')
    await flushPromises()
    expect(wrapper.text()).toContain('停止待确认')
    expect(bodyButton('停止本轮').disabled).toBe(true)
    FakeEventSource.instances.at(-1)!.messages([message({ status: 'cancelled' })], '3')
    await flushPromises()
    expect(wrapper.text()).not.toContain('停止待确认')
    expect(wrapper.findAll('button').some(button => button.text() === '停止本轮')).toBe(false)
  })

  it.each([
    { cancel_requested_at: '2026-11-03T00:00:00Z' },
    { status: 'cancelled' as const },
  ])('停止失败只由同轮权威确认清除：%j', async confirmation => {
    mocks.listAgentMessages.mockResolvedValue([message()])
    mocks.stopAgentTurn.mockRejectedValue(new TypeError('响应丢失'))
    const { wrapper } = await openPage()
    bodyButton('停止本轮').click()
    await flushPromises()
    expect(wrapper.text()).toContain('第 1 轮停止请求未确认')
    const source = FakeEventSource.instances.at(-1)!
    source.messages([message({ id: 2, status: 'cancelled' })], '2')
    await flushPromises()
    expect(wrapper.text()).toContain('第 1 轮停止请求未确认')
    source.messages([message(confirmation)], '3')
    await flushPromises()
    expect(wrapper.text()).not.toContain('停止请求未确认')
  })

  it.each([
    { cancel_requested_at: '2026-11-03T00:00:00Z' },
    { status: 'cancelled' as const },
  ])('同轮 SSE 先确认后迟到停止错误不覆盖：%j', async confirmation => {
    mocks.listAgentMessages.mockResolvedValue([message()])
    let rejectStop!: (error: Error) => void
    mocks.stopAgentTurn.mockReturnValue(new Promise((_, reject) => { rejectStop = reject }))
    const { wrapper } = await openPage()
    bodyButton('停止本轮').click()
    await flushPromises()
    FakeEventSource.instances.at(-1)!.messages([message(confirmation)], '2')
    await flushPromises()
    rejectStop(new TypeError('迟到的响应错误'))
    await flushPromises()
    expect(wrapper.text()).not.toContain('停止请求未确认')
    expect(wrapper.text()).not.toContain('迟到的响应错误')
  })

  it('切换会话后丢弃迟到的停止响应，不污染另一会话同编号消息', async () => {
    mocks.listAgentMessages.mockResolvedValue([message()])
    let resolveStop!: (value: AgentMessage) => void
    mocks.stopAgentTurn.mockReturnValue(new Promise<AgentMessage>(resolve => { resolveStop = resolve }))
    const { wrapper, router } = await openPage()
    bodyButton('停止本轮').click()
    await flushPromises()
    await router.replace({ query: { conversation: 'c2' } })
    await flushPromises()
    resolveStop(message({ cancel_requested_at: '2026-11-02T00:00:00Z' }))
    await flushPromises()
    expect(wrapper.text()).not.toContain('停止待确认')
    expect(bodyButton('停止本轮').disabled).toBe(false)
  })

  it('历史 completed 初次读取和重新进入均不显示成功条或重放通知', async () => {
    mocks.listAgentCleanupRequests.mockResolvedValue([{ conversation_id: 'old', cleanup: {
      request_id: 'old-cleanup', state: 'completed', attempts: 1, retry_at: 0, error_code: null, finished_at: '2026-11-03T00:00:00Z',
    } }])
    const { wrapper: first } = await openPage()
    expect(first.find('.agent-cleanup-entry').exists()).toBe(false)
    expect(first.text()).not.toContain('已从历史移除')
    expect(useUIStore().toasts).toHaveLength(0)
    first.unmount()
    wrappers.splice(wrappers.indexOf(first), 1)
    const { wrapper } = await openPage()
    expect(wrapper.find('.agent-cleanup-entry').exists()).toBe(false)
    expect(useUIStore().toasts).toHaveLength(0)
  })

  it('本次删除直接 completed 只通知一次，不产生常驻成功条', async () => {
    mocks.deleteAgentConversation.mockResolvedValue({ conversation_id: 'c1', cleanup: {
      request_id: 'cleanup-1', state: 'completed', attempts: 1, retry_at: 0, error_code: null, finished_at: '2026-11-03T00:00:00Z',
    } })
    const { wrapper } = await openPage()
    await requestDelete(wrapper)
    bodyButton('确认删除').click()
    await flushPromises()
    expect(useUIStore().toasts.map(toast => toast.text)).toEqual(['会话已移除，运行资源已回收'])
    expect(wrapper.find('.agent-cleanup-entry').exists()).toBe(false)
    expect(wrapper.text()).not.toContain('运行资源已确认回收')
  })

  it('列表刷新合并最新状态，completed 按 request_id 去重', async () => {
    const pending: AgentCleanupResult = { conversation_id: 'old', cleanup: {
      request_id: 'cleanup-old', state: 'requested', attempts: 0, retry_at: 0, error_code: null, finished_at: null,
    } }
    const completed: AgentCleanupResult = { ...pending, cleanup: { ...pending.cleanup!, state: 'completed' } }
    mocks.listAgentCleanupRequests.mockResolvedValueOnce([pending]).mockResolvedValue([completed])
    const { wrapper } = await openPage()
    expect(useUIStore().toasts).toHaveLength(0)
    await wrapper.get('.agent-cleanup-entry').trigger('click')
    await flushPromises()
    expect(wrapper.find('.agent-cleanup-entry').exists()).toBe(false)
    expect(useUIStore().toasts).toHaveLength(1)
    bodyButton('刷新状态').click()
    await flushPromises()
    expect(useUIStore().toasts).toHaveLength(1)
    expect(document.body.textContent).toContain('暂无待处理回收请求')
  })

  it('迟到列表不覆盖本次删除回执，卸载后不再通知', async () => {
    let resolveList!: (rows: AgentCleanupResult[]) => void
    mocks.listAgentCleanupRequests.mockReturnValueOnce(new Promise<AgentCleanupResult[]>(resolve => { resolveList = resolve }))
    const completed: AgentCleanupResult = { conversation_id: 'c1', cleanup: {
      request_id: 'cleanup-1', state: 'completed', attempts: 1, retry_at: 0, error_code: null, finished_at: null,
    } }
    mocks.deleteAgentConversation.mockResolvedValue(completed)
    const { wrapper } = await openPage()
    await requestDelete(wrapper)
    bodyButton('确认删除').click()
    await flushPromises()
    resolveList([{ ...completed, cleanup: { ...completed.cleanup!, state: 'requested' } }])
    await flushPromises()
    expect(wrapper.find('.agent-cleanup-entry').exists()).toBe(false)
    expect(useUIStore().toasts).toHaveLength(1)
    wrapper.unmount()
    wrappers.splice(wrappers.indexOf(wrapper), 1)
    useUIStore().toasts.forEach(toast => useUIStore().removeToast(toast.id))
    let resolveDelete!: (row: AgentCleanupResult) => void
    mocks.deleteAgentConversation.mockReturnValueOnce(new Promise<AgentCleanupResult>(resolve => { resolveDelete = resolve }))
    mocks.listAgentCleanupRequests.mockResolvedValue([])
    const { wrapper: next } = await openPage()
    await requestDelete(next)
    bodyButton('确认删除').click()
    await flushPromises()
    next.unmount()
    wrappers.splice(wrappers.indexOf(next), 1)
    resolveDelete(completed)
    await flushPromises()
    expect(useUIStore().toasts).toHaveLength(0)
  })

  it('历史无回收请求保留详情，不标成回收中或补发删除', async () => {
    mocks.deleteAgentConversation.mockResolvedValue({ conversation_id: 'c1', cleanup: null })
    const { wrapper } = await openPage()
    await requestDelete(wrapper)
    bodyButton('确认删除').click()
    await flushPromises()
    expect(wrapper.text()).toContain('无回收请求 1')
    expect(wrapper.text()).not.toContain('回收中')
    await wrapper.get('.agent-cleanup-entry').trigger('click')
    await flushPromises()
    expect(document.body.textContent).toContain('此历史删除没有回收请求')
    bodyButton('刷新回收状态').click()
    await flushPromises()
    expect(mocks.deleteAgentConversation).toHaveBeenCalledTimes(1)
    expect(useUIStore().toasts.map(toast => toast.text)).toEqual(['已从历史移除'])
  })

  it('删除受理与资源回收分开显示，手动刷新读取权威结果', async () => {
    const { wrapper } = await openPage()
    await requestDelete(wrapper)
    bodyButton('确认删除').click()
    await flushPromises()
    expect(wrapper.text()).toContain('回收中 1')
    expect(useUIStore().toasts.map(toast => toast.text)).toEqual(['已从历史移除'])
    await wrapper.get('.agent-cleanup-entry').trigger('click')
    await flushPromises()
    expect(document.body.textContent).toContain('运行资源回收已请求')
    expect(wrapper.text()).not.toContain('运行资源已确认回收')
    mocks.getAgentCleanup.mockResolvedValue({ conversation_id: 'c1', cleanup: {
      request_id: 'cleanup-1', state: 'failed', attempts: 1, retry_at: 1, error_code: 'conversation_cleanup_failed', finished_at: null,
    } })
    bodyButton('刷新回收状态').click()
    await flushPromises()
    expect(mocks.getAgentCleanup).toHaveBeenCalledWith('c1')
    expect(wrapper.text()).toContain('回收异常 1')
    expect(document.body.textContent).toContain('暂未确认')
    mocks.getAgentCleanup.mockResolvedValue({ conversation_id: 'c1', cleanup: {
      request_id: 'cleanup-1', state: 'completed', attempts: 2, retry_at: 0, error_code: null, finished_at: '2026-11-02T00:00:00Z',
    } })
    bodyButton('刷新回收状态').click()
    await flushPromises()
    expect(wrapper.find('.agent-cleanup-entry').exists()).toBe(false)
    expect(document.body.textContent).not.toContain('运行资源已确认回收')
    expect(useUIStore().toasts.map(toast => toast.text)).toEqual(['已从历史移除', '会话已移除，运行资源已回收'])
    bodyButton('刷新状态').click()
    await flushPromises()
    expect(useUIStore().toasts).toHaveLength(2)
  })

  it.each(['requested', 'failed'] as const)('重新挂载发现已有 %s 回收请求并查询权威状态', async state => {
    const { wrapper: first } = await openPage()
    await requestDelete(first)
    bodyButton('确认删除').click()
    await flushPromises()
    first.unmount()
    wrappers.splice(wrappers.indexOf(first), 1)
    const result: AgentCleanupResult = { conversation_id: 'c1', cleanup: {
      request_id: 'cleanup-1', state, attempts: state === 'failed' ? 6 : 0, retry_at: 0,
      error_code: state === 'failed' ? 'conversation_cleanup_failed' : null, finished_at: null,
    } }
    mocks.listAgentConversations.mockResolvedValue([conversations[1]])
    mocks.listAgentCleanupRequests.mockResolvedValue([result])
    const { wrapper } = await openPage('c2')
    expect(mocks.listAgentCleanupRequests).toHaveBeenCalledTimes(2)
    expect(wrapper.text()).toContain(state === 'failed' ? '回收异常 1' : '回收中 1')
    await wrapper.get('.agent-cleanup-entry').trigger('click')
    await flushPromises()
    expect(document.body.textContent).toContain('会话 c1 已从历史移除')
    expect(document.body.textContent).toContain(state === 'failed' ? '自动重试已用尽' : '运行资源回收已请求')
    mocks.getAgentCleanup.mockResolvedValue({ ...result, cleanup: { ...result.cleanup!, state: 'completed' } })
    bodyButton('刷新回收状态').click()
    await flushPromises()
    expect(mocks.getAgentCleanup).toHaveBeenCalledExactlyOnceWith('c1')
    expect(wrapper.text()).not.toContain('运行资源已确认回收')
    expect(useUIStore().toasts.filter(toast => toast.text === '会话已移除，运行资源已回收')).toHaveLength(1)
    expect(mocks.deleteAgentConversation).toHaveBeenCalledTimes(1)
  })

  it('DELETE 已受理但响应丢失时先保留身份和草稿，权威查询确认后才移除', async () => {
    mocks.deleteAgentConversation.mockRejectedValueOnce(new TypeError('响应丢失'))
    let resolveCleanup!: (result: AgentCleanupResult) => void
    mocks.getAgentCleanup.mockReturnValueOnce(new Promise<AgentCleanupResult>(resolve => { resolveCleanup = resolve }))
    const { wrapper, router } = await openPage()
    await wrapper.get('textarea[aria-label="消息"]').setValue('保留这份草稿')
    await requestDelete(wrapper)
    bodyButton('确认删除').click()
    await flushPromises()
    expect(document.body.textContent).toContain('删除结果未确认')
    expect(document.body.textContent).not.toContain('会话未删除')
    expect(router.currentRoute.value.query.conversation).toBe('c1')
    expect(wrapper.get<HTMLTextAreaElement>('textarea[aria-label="消息"]').element.value).toBe('保留这份草稿')
    expect(mocks.getAgentCleanup).toHaveBeenCalledExactlyOnceWith('c1')
    resolveCleanup({ conversation_id: 'c1', cleanup: {
      request_id: 'cleanup-1', state: 'requested', attempts: 0, retry_at: 0, error_code: null, finished_at: null,
    } })
    await flushPromises()
    expect(router.currentRoute.value.query.conversation).toBe('c2')
    expect(wrapper.text()).not.toContain('删除结果未确认')
    expect(wrapper.text()).toContain('回收中 1')
    expect(wrapper.get<HTMLTextAreaElement>('textarea[aria-label="消息"]').element.value).toBe('')
    expect(mocks.deleteAgentConversation).toHaveBeenCalledTimes(1)
  })

  it.each([new TypeError('响应丢失'), new ApiError('不可读取', 404, {})])('DELETE 异常与 cleanup 404／空请求不推断删除结果：%s', async deleteError => {
    mocks.deleteAgentConversation.mockRejectedValueOnce(deleteError)
    mocks.getAgentCleanup.mockRejectedValueOnce(new ApiError('不可读取', 404, {}))
    const { wrapper, router } = await openPage()
    await wrapper.get('textarea[aria-label="消息"]').setValue('未确认前保留')
    await requestDelete(wrapper)
    bodyButton('确认删除').click()
    await flushPromises()
    bodyButton('关闭').click()
    await flushPromises()
    expect(wrapper.text()).toContain('删除结果未确认')
    expect(wrapper.text()).not.toContain('会话已从历史移除')
    expect(wrapper.get<HTMLTextAreaElement>('textarea[aria-label="消息"]').element.value).toBe('未确认前保留')
    expect(router.currentRoute.value.query.conversation).toBe('c1')
    expect(bodyButton('发送').disabled).toBe(true)
    bodyButton('核实删除结果').click()
    await flushPromises()
    expect(wrapper.text()).toContain('尚未查到已受理的回收请求')
    expect(wrapper.text()).toContain('删除结果未确认')
    mocks.getAgentCleanup.mockResolvedValueOnce({ conversation_id: 'c1', cleanup: {
      request_id: 'cleanup-1', state: 'completed', attempts: 1, retry_at: 0, error_code: null, finished_at: '2026-11-03T00:00:00Z',
    } })
    bodyButton('核实删除结果').click()
    await flushPromises()
    expect(router.currentRoute.value.query.conversation).toBe('c2')
    expect(wrapper.text()).not.toContain('运行资源已确认回收')
    expect(useUIStore().toasts.filter(toast => toast.text === '会话已移除，运行资源已回收')).toHaveLength(1)
    expect(wrapper.text()).not.toContain('删除结果未确认')
    expect(mocks.deleteAgentConversation).toHaveBeenCalledTimes(1)
  })

  it('R5 distinguishes recorded subtotal, historical unknown and zero missing responses', async () => {
    mocks.listAgentMessages.mockResolvedValue([
      message({ id: 1, status: 'failed', usage_missing_responses: 2,
        consumption: { tokens: 15, responses: 2, partial_responses: 2, unpriced_responses: 2,
          amounts: [{ currency: 'USD', amount: '0.000000000001' }, { currency: 'CNY', amount: '0.0200' }],
          usage: { input_tokens: 10, output_tokens: 5, cache_read_tokens: null, cache_write_tokens: null } },
        usage: { input_tokens: 10, output_tokens: 5, cache_read_tokens: null, cache_write_tokens: null } }),
      message({ id: 2, usage_missing_responses: null }),
      message({ id: 3, usage_missing_responses: 0 }),
    ])
    const { wrapper } = await openPage()
    const usages = wrapper.findAll('.agent-usage-detail')
    expect(usages[0]!.get('summary').text()).toBe('本轮用量（已记录小计）')
    expect(usages[0]!.text()).toContain('2 次响应为部分计量')
    expect(usages[0]!.text()).toContain('已计价部分估算 USD 0.000000000001 · CNY 0.02')
    expect(usages[0]!.findAll('dd').map(item => item.text())).toEqual(['10', '5', '未记录', '未记录'])
    expect(usages[1]!.text()).toContain('当前未提供消费回执，无法判断完整用量')
    expect(usages[2]!.text()).toContain('非完整账单')
    expect(mocks.sendAgentMessage).not.toHaveBeenCalled()
  })

  it('shows unobserved bindings without inventing usage and reads recovery on re-entry', async () => {
    const knownUsage = { input_tokens: 10, output_tokens: 5, cache_read_tokens: null, cache_write_tokens: null }
    const recorded = { tokens: 15, responses: 1, partial_responses: 0, unpriced_responses: 0,
      amounts: [{ currency: 'USD', amount: '0.01' }], usage: knownUsage }
    mocks.listAgentMessages.mockResolvedValue([
      message({ id: 1, status: 'succeeded', usage: null, consumption: null, unobserved_consumption_bindings: 2 }),
      message({ id: 2, status: 'succeeded', usage: knownUsage, consumption: recorded, unobserved_consumption_bindings: 3 }),
      message({ id: 3, status: 'succeeded', unobserved_consumption_bindings: 0 }),
      message({ id: 4, status: 'succeeded' }),
    ])
    const { wrapper, router } = await openPage()
    const usages = wrapper.findAll('.agent-usage-detail')
    expect(usages[0]!.get('summary').text()).toBe('本轮用量（计量回执未齐）')
    expect(usages[0]!.findAll('dd').map(item => item.text())).toEqual(Array(4).fill('未记录'))
    expect(usages[0]!.text()).toContain('当前未提供消费回执，无法判断完整用量')
    expect(usages[0]!.text()).toContain('有 2 个执行绑定尚无计量回执，已记录用量可能不完整')
    expect(usages[0]!.text()).toContain('重新进入会话可读取最新计量')
    expect(usages[1]!.text()).toContain('有 3 个执行绑定尚无计量回执')
    expect(usages[1]!.findAll('dd').map(item => item.text())).toEqual(['10', '5', '未记录', '未记录'])
    expect(usages[1]!.text()).toContain('估算 USD 0.01')
    expect(usages[2]!.text()).not.toContain('个执行绑定尚无计量回执')
    expect(usages[3]!.text()).not.toContain('个执行绑定尚无计量回执')
    const recovered = message({ id: 1, status: 'succeeded', usage: knownUsage, consumption: recorded, unobserved_consumption_bindings: 0 })
    FakeEventSource.instances.at(-1)!.messages([recovered], '2')
    await flushPromises()
    expect(wrapper.findAll('.agent-usage-detail')[0]!.text()).toContain('有 2 个执行绑定尚无计量回执')
    mocks.listAgentMessages.mockResolvedValue([recovered])
    await router.replace({ query: { conversation: 'c2' } })
    await flushPromises()
    await router.replace({ query: { conversation: 'c1' } })
    await flushPromises()
    expect(wrapper.get('.agent-usage-detail').text()).not.toContain('个执行绑定尚无计量回执')
    expect(wrapper.get('.agent-usage-detail').text()).toContain('非完整账单')
    expect(mocks.sendAgentMessage).not.toHaveBeenCalled()
  })

  it('distinguishes consumption read failure from missing or unpriced usage', async () => {
    const missingUsage = { input_tokens: null, output_tokens: null, cache_read_tokens: null, cache_write_tokens: null }
    const zeroUsage = { ...missingUsage, input_tokens: 0 }
    mocks.listAgentMessages.mockResolvedValue([
      message({ id: 1, status: 'succeeded', answer: '原有回复', consumption_error: true, consumption: null,
        usage: missingUsage, usage_missing_responses: null, unobserved_consumption_bindings: null }),
      message({ id: 2, status: 'succeeded', consumption_error: false, consumption: null,
        usage: missingUsage, unobserved_consumption_bindings: 0 }),
      message({ id: 3, status: 'succeeded', usage: zeroUsage }),
    ])
    const { wrapper } = await openPage()
    const turns = wrapper.findAll('.agent-turn')
    expect(turns[0]!.get('.agent-answer').text()).toBe('原有回复')
    expect(turns[0]!.get('.agent-consumption-error').attributes('role')).toBe('alert')
    expect(turns[0]!.get('.agent-consumption-error').text()).toContain('本轮计量读取失败，实际用量与费用未知')
    expect(turns[0]!.get('.agent-consumption-error').text()).toContain('重新进入会话可再次读取')
    const failedUsage = turns[0]!.get('.agent-usage-detail')
    expect(failedUsage.get('summary').text()).toBe('本轮用量（读取失败）')
    expect(failedUsage.text()).toContain('计量读取失败，实际用量与费用未知')
    expect(failedUsage.findAll('dd')).toHaveLength(0)
    expect(failedUsage.text()).not.toMatch(/未提供|未定价|未记录|尚无计量回执/)
    expect(turns[1]!.find('.agent-consumption-error').exists()).toBe(false)
    expect(turns[1]!.get('.agent-usage-detail').text()).toContain('当前未提供消费回执')
    expect(turns[2]!.find('.agent-consumption-error').exists()).toBe(false)
    expect(turns[2]!.get('.agent-usage-detail').findAll('dd')[0]!.text()).toBe('0')
    expect(mocks.sendAgentMessage).not.toHaveBeenCalled()
  })

  it('R4 reads failed-turn usage without replacing zero or exposing a project bill', async () => {
    mocks.listAgentMessages.mockResolvedValue([message({ status: 'failed', answer: '原有失败回复', error_code: 'agent_timeout',
      usage: { input_tokens: 123, output_tokens: 0, cache_read_tokens: null, cache_write_tokens: 7 } })])
    const { wrapper } = await openPage()
    const usage = wrapper.get('.agent-usage-detail')
    expect(usage.attributes('open')).toBeUndefined()
    expect(usage.get('summary').text()).toBe('本轮用量')
    usage.element.setAttribute('open', '')
    expect(usage.findAll('dd').map(item => item.text())).toEqual(['123', '0', '未记录', '7'])
    expect(usage.text()).toContain('仅本轮对话，不含关联项目／复测／报告任务')
    expect(usage.text()).toContain('费用未定价')
    expect(wrapper.get('.agent-answer').text()).toBe('原有失败回复')
    expect(mocks.sendAgentMessage).not.toHaveBeenCalled()
  })

  it('R4 keeps missing usage unknown and switches conversations without stale usage', async () => {
    mocks.listAgentMessages.mockResolvedValueOnce([message({ status: 'succeeded', usage: null })])
      .mockResolvedValueOnce([message({ id: 2, status: 'running' })])
    const { wrapper, router } = await openPage()
    const oldSource = FakeEventSource.instances[0]!
    expect(wrapper.get('.agent-usage-detail').findAll('dd').map(item => item.text())).toEqual(Array(4).fill('未记录'))
    await router.replace({ query: { conversation: 'c2' } })
    await flushPromises()
    oldSource.messages([message({ usage: { input_tokens: 999, output_tokens: 999, cache_read_tokens: 999, cache_write_tokens: 999 } })], '90')
    await flushPromises()
    expect(wrapper.findAll('.agent-usage-detail')).toHaveLength(1)
    expect(wrapper.get('.agent-usage-detail').findAll('dd').map(item => item.text())).toEqual(Array(4).fill('未记录'))
    expect(wrapper.get('.agent-usage-detail').text()).toContain('本轮尚未结束，当前记录不代表完整用量')
    expect(mocks.sendAgentMessage).not.toHaveBeenCalled()
  })

  it('成品详情区分发布核验、未核验历史声明与证据限制，并保留下载', async () => {
    mocks.listAgentMessages.mockResolvedValue([message({ status: 'succeeded', artifacts: [
      { id: 'new', title: '已核验合成成品', download_url: '/agent-conversations/c1/artifacts/new', sources: {
        verified: [{ label: '会话附件 · 合成.txt', scope: 'Unicode 字符 [0, 20)；仅此范围', version: 'a'.repeat(64),
          verified_at: '2026-11-01T01:00:00Z', reference: { method: 'read_conversation_attachment' },
          limitations: '来源保留的缺失限制', material_version: { sha256: 'b'.repeat(64) } }],
        declared: ['模型自行声明'], declared_read_at: null, limitations: ['未测试范围不能说安全'],
      } },
      { id: 'old', title: '历史成品', download_url: '/agent-conversations/c1/artifacts/old', sources: {
        verified: [], declared: ['历史来源'], declared_read_at: '2001-01-01T00:00:00Z', limitations: [],
      } },
    ] })])
    const { wrapper } = await openPage()
    await wrapper.get('[aria-controls="agent-work-panel"]').trigger('click')
    const details = wrapper.findAll('.agent-artifact-sources')
    expect(details[0]!.get('summary').text()).toBe('来源 · 已核验 1 项')
    expect(details[1]!.get('summary').text()).toBe('来源 · 未核验')
    details[0]!.element.setAttribute('open', '')
    expect(details[0]!.text()).toContain('Unicode 字符 [0, 20)；仅此范围')
    expect(details[0]!.text()).toContain('核验时间不是首次读取时间')
    expect(details[0]!.text()).toContain('来源声明（未核验）')
    expect(details[0]!.text()).toContain('证据限制（交付声明）')
    expect(details[0]!.text()).toContain('未测试范围不能说安全')
    expect(details[0]!.text()).toContain('来源证据限制：来源保留的缺失限制')
    expect(details[0]!.text()).toContain('b'.repeat(64))
    expect(details[1]!.text()).toContain('2001-01-01T00:00:00Z（未经服务端核验）')
    expect(wrapper.findAll('a[download]')).toHaveLength(2)
  })

  it('上下文切换不重连或丢草稿，已提交输入和重试仍绑定原项目', async () => {
    mocks.sendAgentMessage.mockRejectedValueOnce(new TypeError('响应丢失'))
      .mockImplementation(async (_conversation, body) => message({ ...body, status: 'queued' }))
    mocks.listAgentMessages.mockResolvedValue([message({ project_id: 'p1', status: 'succeeded', answer: '已有回复' })])
    const { wrapper, router } = await openPage()
    await wrapper.get('textarea').setValue('保留上下文草稿')
    await router.replace({ query: { conversation: 'c1', project: 'p2' } })
    await flushPromises()
    expect(FakeEventSource.instances).toHaveLength(1)
    expect(wrapper.get('.agent-message-context').text()).toContain('p1')
    expect(wrapper.get<HTMLTextAreaElement>('textarea').element.value).toBe('保留上下文草稿')
    await wrapper.get('form.agent-composer').trigger('submit')
    await flushPromises()
    const original = mocks.sendAgentMessage.mock.calls[0]![1]
    expect(original.project_id).toBe('p2')
    await router.replace({ query: { conversation: 'c1' } })
    await wrapper.get('form.agent-composer').trigger('submit')
    await flushPromises()
    expect(mocks.sendAgentMessage.mock.calls[1]![1]).toEqual(original)
    expect(FakeEventSource.instances).toHaveLength(1)
  })

  it('项目列表延迟返回后显示真实来源名称，不缓存临时不可读标签', async () => {
    let resolveProjects!: (rows: { id: string; title: string }[]) => void
    mocks.api.mockReturnValue(new Promise(resolve => { resolveProjects = resolve }))
    const { wrapper, router } = await openPage()
    await router.replace({ query: { conversation: 'c1', project: 'p1' } })
    expect(wrapper.text()).toContain('正在读取项目…')
    expect(wrapper.find('[aria-label="本条消息的项目上下文"]').exists()).toBe(false)
    resolveProjects([{ id: 'p1', title: '项目一' }])
    await flushPromises()
    expect(wrapper.get('[aria-label="本条消息的项目上下文"]').text()).toContain('项目一')
    expect(wrapper.text()).not.toContain('不可读取')
  })

  it('未知 session 仅禁续聊，保留历史成品且不自动创建替代会话', async () => {
    mocks.listAgentConversations.mockResolvedValue([{ ...conversations[0], available: false, unavailable_reason: 'session_verification_required' }])
    mocks.listAgentMessages.mockResolvedValue([message({ status: 'failed', answer: '保留失败事实', artifacts: [
      { id: 'a1', title: '已有成品', download_url: '/agent-conversations/c1/artifacts/a1' },
    ] })])
    const { wrapper, router } = await openPage()
    await router.replace({ query: { conversation: 'c1', project: 'deleted-project' } })
    await flushPromises()
    expect(wrapper.text()).toContain('原 session 来源尚未验证')
    expect(wrapper.text()).toContain('所选项目已不可读取')
    await wrapper.get('textarea').setValue('不能重开旧 session')
    await wrapper.get('form.agent-composer').trigger('submit')
    expect(mocks.sendAgentMessage).not.toHaveBeenCalled()
    expect(mocks.createAgentConversation).not.toHaveBeenCalled()
    await wrapper.get('[aria-controls="agent-work-panel"]').trigger('click')
    expect(wrapper.get('a[download]').attributes('href')).toContain('/agent-conversations/c1/artifacts/a1')
  })

  it('历史固定可达，成果按需打开并保留草稿、阅读锚点及回焦', async () => {
    mocks.listAgentMessages.mockResolvedValue([message({ status: 'succeeded', answer: '受控长回复', artifacts: [
      { id: 'a1', title: '受控资料', download_url: '/projects/p1/artifacts/a1' },
    ] })])
    const { wrapper } = await openPage()
    expect(wrapper.get('#agent-history-panel').isVisible()).toBe(true)
    expect(wrapper.find('.agent-deliverables').exists()).toBe(false)
    expect(wrapper.findAll('.agent-new-conversation')).toHaveLength(1)
    await wrapper.get('textarea').setValue('保留中文草稿')
    const list = wrapper.get('.agent-messages')
    Object.defineProperties(list.element, { scrollHeight: { configurable: true, value: 1400 }, clientHeight: { configurable: true, value: 400 } })
    list.element.scrollTop = 120
    await list.trigger('scroll')
    expect(wrapper.findAll('.agent-history-select')).toHaveLength(2)
    expect(wrapper.find('.agent-history-select time').exists()).toBe(false)
    expect(list.element.scrollTop).toBe(120)
    const turn = wrapper.get('.agent-turn').element
    vi.spyOn(turn, 'getBoundingClientRect').mockImplementation(() => ({
      top: wrapper.find('.agent-deliverables').exists() ? 60 : 20, bottom: 300,
    } as DOMRect))
    await wrapper.get('[aria-controls="agent-work-panel"]').trigger('click')
    await flushPromises()
    expect(wrapper.get('#agent-history-panel').isVisible()).toBe(true)
    expect(wrapper.find('.agent-deliverables').exists()).toBe(true)
    expect(list.element.scrollTop).toBe(160)
    expect((wrapper.get('textarea').element as HTMLTextAreaElement).value).toBe('保留中文草稿')
    await wrapper.get('[aria-label="收起工作与成果"]').trigger('click')
    await flushPromises()
    expect(list.element.scrollTop).toBe(120)
    expect(document.activeElement).toBe(wrapper.get('[aria-controls="agent-work-panel"]').element)
    expect(mocks.createAgentConversation).not.toHaveBeenCalled()
    expect(mocks.sendAgentMessage).not.toHaveBeenCalled()
  })

  it('输入法组字和普通 Enter 不发送，快捷键发送保留原文', async () => {
    mocks.sendAgentMessage.mockImplementation(async (_conversation, body) => message({ ...body }))
    const { wrapper } = await openPage()
    const input = wrapper.get('textarea')
    await input.setValue('只读输入\n第二行')
    await input.trigger('keydown', { key: 'Enter' })
    await input.trigger('keydown', { key: 'Enter', ctrlKey: true, isComposing: true })
    await input.trigger('keydown', { key: 'Enter', metaKey: true, keyCode: 229 })
    expect(mocks.sendAgentMessage).not.toHaveBeenCalled()
    await input.trigger('keydown', { key: 'Enter', ctrlKey: true })
    await flushPromises()
    expect(mocks.sendAgentMessage).toHaveBeenCalledExactlyOnceWith('c1', expect.objectContaining({ text: '只读输入\n第二行' }))
  })

  it('阅读旧消息时更新不抢滚动，回到最新后继续跟随', async () => {
    mocks.listAgentMessages.mockResolvedValue([message()])
    const { wrapper } = await openPage()
    const list = wrapper.get('.agent-messages')
    Object.defineProperties(list.element, { scrollHeight: { configurable: true, value: 1400 }, clientHeight: { configurable: true, value: 400 } })
    list.element.scrollTop = 120
    await list.trigger('scroll')
    FakeEventSource.instances[0]!.messages([message({ status: 'succeeded', answer: '更新后的回复' })], '1')
    await flushPromises()
    expect(list.element.scrollTop).toBe(120)
    await wrapper.get('.agent-latest').trigger('click')
    expect(list.element.scrollTop).toBe(1400)
    expect(wrapper.find('.agent-latest').exists()).toBe(false)
  })

  it('从 URL 恢复会话和失败／成品，只信任服务端同源下载而不执行回复链接', async () => {
    mocks.listAgentMessages.mockResolvedValue([message({ status: 'failed', answer: '[伪造下载](https://example.test/run)', error_code: 'agent_timeout', artifacts: [
      { id: 'a1', title: '已有漏洞报告', download_url: '/agent-conversations/c2/artifacts/a1' },
      { id: 'a2', title: '无效成果', download_url: 'https://example.test/file' },
    ] })])
    const { wrapper } = await openPage('c2')
    await wrapper.get('[aria-controls="agent-work-panel"]').trigger('click')
    expect(mocks.listAgentMessages).toHaveBeenCalledWith('c2', expect.any(AbortSignal))
    expect(FakeEventSource.instances[0]?.url).toContain('/c2/events?after=0')
    expect(wrapper.text()).toContain('agent_timeout')
    expect(wrapper.text()).toContain('伪造下载')
    expect(wrapper.findAll('a[download]')).toHaveLength(1)
    expect(wrapper.get('a[download]').attributes('href')).toContain('/c2/artifacts/a1')
    expect(wrapper.find('a[href*="/runs"]').exists()).toBe(false)
    expect(wrapper.get('.agent-error-detail').text()).toContain('run1')
    expect(wrapper.find('a[href="https://example.test/run"]').exists()).toBe(false)
    expect(wrapper.text()).toContain('无效成果（下载地址不可用）')
    expect(mocks.sendAgentMessage).not.toHaveBeenCalled()
    expect(mocks.createAgentConversation).not.toHaveBeenCalled()
  })

  it('受理失败重试保留 UUID，运行中继续发送新的消息排队', async () => {
    mocks.listAgentMessages.mockResolvedValue([message()])
    mocks.sendAgentMessage.mockRejectedValueOnce(new TypeError('网络断开'))
      .mockImplementation(async (_conversation, body) => message({ id: 2, ...body, status: 'queued' }))
    const { wrapper } = await openPage()
    const input = wrapper.get('textarea')
    await input.setValue('请读取已有漏洞')
    await wrapper.get('form.agent-composer').trigger('submit')
    await flushPromises()
    const original = mocks.sendAgentMessage.mock.calls[0]?.[1]
    expect(original.client_message_id).toMatch(/^[\da-f-]{36}$/i)
    expect(wrapper.text()).toContain('消息受理结果未确认')
    expect(input.attributes('readonly')).toBeDefined()
    await wrapper.get('form.agent-composer').trigger('submit')
    await flushPromises()
    expect(mocks.sendAgentMessage.mock.calls[1]?.[1]).toEqual(original)
    expect((input.element as HTMLTextAreaElement).value).toBe('')
    expect(wrapper.text()).toContain('发送并排队')
    await input.setValue('再查询项目状态')
    await wrapper.get('form.agent-composer').trigger('submit')
    await flushPromises()
    expect(mocks.sendAgentMessage.mock.calls[2]?.[1].client_message_id).not.toBe(original.client_message_id)
  })

  it('断线补读旧消息状态，事件游标独立且迟到数据不会回退终态', async () => {
    vi.useFakeTimers()
    mocks.listAgentMessages.mockResolvedValueOnce([message()])
      .mockResolvedValueOnce([message({ status: 'queued' })])
    const { wrapper } = await openPage()
    const first = FakeEventSource.instances[0]!
    first.messages([message({ status: 'succeeded', answer: '已有进展已读取。' })], '57')
    await flushPromises()
    first.onerror?.()
    expect(first.close).toHaveBeenCalled()
    await vi.advanceTimersByTimeAsync(1000)
    await flushPromises()
    expect(mocks.listAgentMessages).toHaveBeenCalledTimes(2)
    expect(FakeEventSource.instances[1]?.url).toContain('events?after=57')
    expect(wrapper.text()).toContain('已有进展已读取。')
    expect(wrapper.find('.agent-answer-heading .ui-badge').exists()).toBe(false)
    FakeEventSource.instances[1]!.messages([message({ id: 2, client_message_id: 'input-2', status: 'failed', error_code: 'worker_unavailable' })], '58')
    await flushPromises()
    expect(wrapper.text()).toContain('worker_unavailable')
  })

  it('健康 SSE 周期轮换可持续重连，快速 open/error 与重放消息仍受连续失败上限约束', async () => {
    vi.useFakeTimers()
    await openPage()
    for (let cycle = 0; cycle < 5; cycle++) {
      const source = FakeEventSource.instances.at(-1)!
      source.onopen?.()
      await vi.advanceTimersByTimeAsync(60_000)
      source.onerror?.()
      await vi.advanceTimersByTimeAsync(1000)
      await flushPromises()
      expect(FakeEventSource.instances).toHaveLength(cycle + 2)
    }
    // 上次健康轮换已消费第一次重连；之后两次快速失败按 2s、4s 退避，不能因 open 或重放恢复预算。
    for (const delay of [2000, 4000]) {
      const source = FakeEventSource.instances.at(-1)!
      source.onopen?.()
      source.messages([message()], '57')
      source.onerror?.()
      await vi.advanceTimersByTimeAsync(delay)
      await flushPromises()
    }
    expect(FakeEventSource.instances).toHaveLength(8)
    const last = FakeEventSource.instances.at(-1)!
    last.onopen?.()
    last.messages([message()], '57')
    last.onerror?.()
    await vi.advanceTimersByTimeAsync(120_000)
    await flushPromises()
    expect(FakeEventSource.instances).toHaveLength(8)
    expect(mocks.listAgentMessages).toHaveBeenCalledTimes(8)
  })

  it('明确正常轮换和短故障静默，持续失败保留草稿并显示手动重连', async () => {
    vi.useFakeTimers()
    const { wrapper } = await openPage()
    await wrapper.get('textarea').setValue('保留草稿')
    for (let cycle = 0; cycle < 2; cycle++) {
      const source = FakeEventSource.instances.at(-1)!
      source.onopen?.()
      await vi.advanceTimersByTimeAsync(60_000)
      source.dispatchEvent(new MessageEvent('close', { data: JSON.stringify({ reason: 'rotate' }) }))
      await vi.advanceTimersByTimeAsync(1)
      await flushPromises()
      expect(wrapper.text()).not.toContain('消息更新暂不可用')
    }
    for (const delay of [1000, 2000, 4000]) {
      const source = FakeEventSource.instances.at(-1)!
      source.onopen?.()
      source.onerror?.()
      await vi.advanceTimersByTimeAsync(delay)
      await flushPromises()
      expect(wrapper.text()).not.toContain('消息更新暂不可用')
    }
    FakeEventSource.instances.at(-1)!.onerror?.()
    await flushPromises()
    expect(wrapper.text()).toContain('消息更新暂不可用')
    expect(wrapper.text()).toContain('已显示状态可能滞后')
    const count = FakeEventSource.instances.length
    await vi.advanceTimersByTimeAsync(120_000)
    expect(FakeEventSource.instances).toHaveLength(count)
    expect((wrapper.get('textarea').element as HTMLTextAreaElement).value).toBe('保留草稿')
    await wrapper.findAll('button').find(button => button.text() === '重新连接')!.trigger('click')
    await flushPromises()
    FakeEventSource.instances.at(-1)!.onopen?.()
    await flushPromises()
    expect(wrapper.text()).not.toContain('消息更新暂不可用')
    expect(mocks.sendAgentMessage).not.toHaveBeenCalled()
  })

  it.each(['auth', 'parse'])('SSE %s 错误立即可见且停止自动重试', async kind => {
    vi.useFakeTimers()
    const { wrapper } = await openPage()
    const source = FakeEventSource.instances[0]!
    if (kind === 'auth') source.onerror?.(new MessageEvent('error', { data: JSON.stringify({ detail: '会话已删除', status: 404 }) }))
    else source.dispatchEvent(new MessageEvent('messages', { data: '{invalid' }))
    await flushPromises()
    expect(wrapper.text()).toContain(kind === 'auth' ? '会话已删除' : '消息更新无法解析')
    await vi.advanceTimersByTimeAsync(120_000)
    expect(FakeEventSource.instances).toHaveLength(1)
  })

  it('执行未确认在折叠态明确展示，并保留运行中排队语义直到服务端更新', async () => {
    mocks.listAgentMessages.mockResolvedValue([message({ error_code: 'execution_unconfirmed' })])
    const { wrapper } = await openPage()
    expect(wrapper.get('.agent-turn .ui-badge').text()).toBe('执行状态待确认')
    expect(wrapper.get('.agent-turn .ui-alert').text()).toContain('暂时无法确认执行是否停止')
    expect(wrapper.get('.agent-error-detail summary').text()).toBe('执行诊断')
    expect(wrapper.text()).not.toContain('本轮处理失败')
    expect(wrapper.text()).toContain('发送并排队')
    FakeEventSource.instances[0]!.messages([message({ status: 'succeeded', answer: '已处理完成。' })], '58')
    await flushPromises()
    expect(wrapper.find('.agent-answer-heading .ui-badge').exists()).toBe(false)
    expect(wrapper.find('.agent-turn .ui-alert').exists()).toBe(false)
    expect(wrapper.text()).not.toContain('执行状态待确认')
  })

  it('切会话保留草稿且忽略旧连接，回到原会话可继续输入', async () => {
    const { wrapper, router } = await openPage()
    const old = FakeEventSource.instances[0]!
    await wrapper.get('textarea').setValue('尚未发送的输入')
    await router.replace({ query: { conversation: 'c2' } })
    await flushPromises()
    expect(router.currentRoute.value.query.conversation).toBe('c2')
    expect(old.close).toHaveBeenCalled()
    old.messages([message({ answer: '不应显示的旧会话回复' })], '90')
    await flushPromises()
    expect(wrapper.text()).not.toContain('不应显示的旧会话回复')
    expect((wrapper.get('textarea').element as HTMLTextAreaElement).value).toBe('')
    await router.replace({ query: { conversation: 'c1' } })
    await flushPromises()
    expect((wrapper.get('textarea').element as HTMLTextAreaElement).value).toBe('尚未发送的输入')
  })

  it('会话恢复后独立读取业务状态，聊天结束仍更新成果且不阻塞继续输入', async () => {
    vi.useFakeTimers()
    mocks.listAgentMessages.mockResolvedValue([message({ status: 'succeeded', answer: '已受理复测。',
      actions: [{ id: 'action-1', kind: 'verification', title: '复测 v001' }] })])
    mocks.listAgentWork.mockResolvedValue([work()])
    const { wrapper } = await openPage()
    await wrapper.get('[aria-controls="agent-work-panel"]').trigger('click')
    expect(wrapper.get('.agent-work').text()).toContain('执行中')
    expect(wrapper.get('.agent-work a').attributes('href')).toBe('/verification-requests/vr_1')
    expect(wrapper.text()).not.toContain('发送并排队')
    await wrapper.get('textarea').setValue('继续查看项目进展')
    expect(wrapper.get('button[type="submit"]').attributes('disabled')).toBeUndefined()
    mocks.listAgentWork.mockResolvedValue([work({ status: 'completed', result: {
      outcome: 'not_reproduced', summary: '本轮未复现，详见测试过程。', tested_scope: '仅合成材料',
      limitations: ['未访问真实目标'], reason_code: null,
    }, download_url: '/verification-requests/vr_1/report' })])
    await vi.advanceTimersByTimeAsync(5000)
    await flushPromises()
    expect(wrapper.get('.agent-work').text()).toContain('已完成')
    expect(wrapper.get('.agent-work').text()).toContain('本轮未复现')
    expect(wrapper.get('.agent-work a[download]').attributes('href')).toContain('/vr_1/report')
    const completedReads = mocks.listAgentWork.mock.calls.length
    await vi.advanceTimersByTimeAsync(15_000)
    expect(mocks.listAgentWork).toHaveBeenCalledTimes(completedReads)
    expect((wrapper.get('textarea').element as HTMLTextAreaElement).value).toBe('继续查看项目进展')
  })

  it.each(['succeeded', 'failed'] as const)('报告退避继续补读直至 %s，保留错误与代次边界后停止', async terminalStatus => {
    vi.useFakeTimers()
    mocks.listAgentMessages.mockResolvedValue([message({ status: 'succeeded', answer: '已提交报告。',
      actions: [{ id: 'action-1', kind: 'report', title: '项目综合报告' }] })])
    const report = work({ kind: 'report', title: '项目综合报告', status: 'queued',
      error: '本次尝试暂时失败', next_attempt_at: '2026-10-28T00:00:30Z',
      generation_token: 'current', accepted_generation_token: 'previous' })
    mocks.listAgentWork.mockResolvedValue([report])
    const { wrapper } = await openPage()
    await wrapper.get('[aria-controls="agent-work-panel"]').trigger('click')
    expect(wrapper.get('.agent-work').text()).toContain('等待重试')
    expect(wrapper.get('.agent-work').text()).toContain('本次尝试暂时失败')
    expect(wrapper.get('.agent-work time').attributes('datetime')).toBe(report.next_attempt_at)
    expect(wrapper.find('.agent-work a[download]').exists()).toBe(false)
    const initialReads = mocks.listAgentWork.mock.calls.length
    await vi.advanceTimersByTimeAsync(5000)
    await flushPromises()
    expect(mocks.listAgentWork).toHaveBeenCalledTimes(initialReads + 1)
    expect(wrapper.get('.agent-work').text()).toContain('等待重试')
    mocks.listAgentWork.mockResolvedValue([work({ ...report, status: 'running', next_attempt_at: null, error: null })])
    await vi.advanceTimersByTimeAsync(5000)
    await flushPromises()
    expect(wrapper.get('.agent-work').text()).toContain('执行中')
    expect(wrapper.find('.agent-work time').exists()).toBe(false)
    mocks.listAgentWork.mockResolvedValue([work({ ...report, status: terminalStatus, next_attempt_at: null,
      error: terminalStatus === 'failed' ? '最终失败，不再重试' : null,
      accepted_generation_token: terminalStatus === 'succeeded' ? 'current' : 'previous',
      download_url: terminalStatus === 'succeeded' ? '/projects/p1/artifacts/current/content' : null })])
    await vi.advanceTimersByTimeAsync(5000)
    await flushPromises()
    expect(wrapper.get('.agent-work').text()).toContain(terminalStatus === 'succeeded' ? '已完成' : '最终失败，不再重试')
    expect(wrapper.find('.agent-work a[download]').exists()).toBe(terminalStatus === 'succeeded')
    expect(wrapper.get('.agent-work').text().includes('上次报告不是本轮成果')).toBe(terminalStatus === 'failed')
    expect(wrapper.get('.agent-work').text()).not.toContain('等待重试')
    const terminalReads = mocks.listAgentWork.mock.calls.length
    await vi.advanceTimersByTimeAsync(15_000)
    expect(mocks.listAgentWork).toHaveBeenCalledTimes(terminalReads)
  })

  it.each(['project', 'intent'] as const)('%s 动作展示真实业务链接，提交成功不冒充研究完成', async kind => {
    mocks.listAgentMessages.mockResolvedValue([message({ status: 'succeeded', actions: [{ id: 'action-1', kind, title: kind === 'project' ? '新建项目' : '追加项目 Intent' }] })])
    mocks.listAgentWork.mockResolvedValue([work({ kind, status: 'succeeded', url: '/projects/p2', result: { project_id: 'p2', project_status: 'paused' } })])
    const { wrapper } = await openPage()
    await wrapper.get('[aria-controls="agent-work-panel"]').trigger('click')
    expect(wrapper.get('.agent-work .ui-badge').text()).toBe(kind === 'project' ? '已创建' : '已追加')
    expect(wrapper.get('.agent-work a').text()).toBe(kind === 'project' ? '查看项目' : '查看研究任务')
    expect(wrapper.get('.agent-work a').attributes('href')).toBe('/projects/p2')
    expect(wrapper.find('.agent-work-result').exists()).toBe(false)
  })

  it('新业务引用立即补读，旧代报告不能成为本轮下载，权限错误在工作处可见', async () => {
    const { wrapper } = await openPage()
    mocks.listAgentWork.mockResolvedValue([work({ kind: 'report', title: '项目综合报告', status: 'failed',
      error: '本轮报告生成失败', generation_token: 'new', accepted_generation_token: 'old',
      url: '/projects/p1/reports', download_url: '/projects/p1/report/download' })])
    FakeEventSource.instances[0]!.messages([message({ status: 'succeeded', answer: '已提交生成。',
      actions: [{ id: 'action-1', kind: 'report', title: '项目综合报告' }] })], '58')
    await flushPromises()
    await wrapper.get('[aria-controls="agent-work-panel"]').trigger('click')
    expect(wrapper.get('.agent-work').text()).toContain('本轮报告生成失败')
    expect(wrapper.get('.agent-work').text()).toContain('上次报告不是本轮成果')
    expect(wrapper.find('.agent-work a[download]').exists()).toBe(false)
    mocks.listAgentWork.mockResolvedValue([work({ kind: 'report', title: '项目综合报告', status: 'unavailable',
      error: '当前用户无权读取此工作', url: '' })])
    await wrapper.get('button[aria-label="刷新工作"]').trigger('click')
    await flushPromises()
    expect(wrapper.get('.agent-work').text()).toContain('当前用户无权读取此工作')
    expect(wrapper.find('.agent-work a').exists()).toBe(false)
  })

  it('业务读取连续失败有界停止并显示旧状态提示，手动读取可恢复', async () => {
    vi.useFakeTimers()
    mocks.listAgentMessages.mockResolvedValue([message({ status: 'succeeded',
      actions: [{ id: 'action-1', kind: 'verification', title: '复测 v001' }] })])
    mocks.listAgentWork.mockResolvedValue([work()])
    const { wrapper } = await openPage()
    await wrapper.get('[aria-controls="agent-work-panel"]').trigger('click')
    const initialReads = mocks.listAgentWork.mock.calls.length
    mocks.listAgentWork.mockRejectedValue(new TypeError('网络断开'))
    await vi.advanceTimersByTimeAsync(30_000)
    await flushPromises()
    expect(mocks.listAgentWork).toHaveBeenCalledTimes(initialReads + 3)
    expect(wrapper.text()).toContain('工作状态未更新')
    expect(wrapper.get('.agent-work').text()).toContain('执行中')
    mocks.listAgentWork.mockResolvedValue([work({ status: 'completed' })])
    await wrapper.findAll('button').find(button => button.text() === '重新读取')!.trigger('click')
    await flushPromises()
    expect(wrapper.text()).not.toContain('工作状态未更新')
    expect(wrapper.get('.agent-work').text()).toContain('已完成')
  })

  it('直接使用默认模型新建会话，不选择 Worker 或自动发起模型输入', async () => {
    mocks.listAgentConversations.mockResolvedValue([])
    mocks.createAgentConversation.mockResolvedValue(conversations[0])
    const { wrapper, router } = await openPage('')
    await wrapper.get('.agent-new-conversation').trigger('click')
    await flushPromises()
    expect(mocks.createAgentConversation).toHaveBeenCalledExactlyOnceWith(null)
    expect(router.currentRoute.value.query.conversation).toBe('c1')
    expect(wrapper.find('[aria-label="本条消息的项目上下文"]').exists()).toBe(true)
    expect(wrapper.text()).not.toContain('选择 Worker')
    expect(mocks.sendAgentMessage).not.toHaveBeenCalled()
  })

  it('普通 HTTP 没有 randomUUID 时按钮仍发送安全 UUID，生成失败可见且保留输入', async () => {
    const getRandomValues = vi.fn((bytes: Uint8Array<ArrayBuffer>) => cryptoSource.getRandomValues(bytes))
    const cryptoSource = globalThis.crypto
    vi.stubGlobal('crypto', { getRandomValues })
    mocks.sendAgentMessage.mockImplementation(async (_conversation, body) => message({ ...body }))
    const { wrapper } = await openPage()
    await wrapper.get('textarea').setValue('局域网发送')
    await wrapper.get('form.agent-composer').trigger('submit')
    await flushPromises()
    expect(getRandomValues).toHaveBeenCalledTimes(1)
    expect(mocks.sendAgentMessage.mock.calls[0]?.[1].client_message_id).toMatch(/^[\da-f]{8}-[\da-f]{4}-4[\da-f]{3}-[89ab][\da-f]{3}-[\da-f]{12}$/)
    getRandomValues.mockImplementationOnce(() => { throw new Error('随机源暂不可用') })
    await wrapper.get('textarea').setValue('保留的输入')
    await wrapper.get('form.agent-composer').trigger('submit')
    await flushPromises()
    expect(wrapper.text()).toContain('消息未受理')
    expect(wrapper.text()).toContain('随机源暂不可用')
    expect((wrapper.get('textarea').element as HTMLTextAreaElement).value).toBe('保留的输入')
    expect(wrapper.get('textarea').attributes('readonly')).toBeUndefined()
    expect(mocks.sendAgentMessage).toHaveBeenCalledTimes(1)
  })

  it('历史菜单删除先确认，取消和服务端失败保留草稿，成功切换并修正 URL', async () => {
    const { wrapper, router } = await openPage()
    await wrapper.get('textarea').setValue('当前草稿')
    await router.replace({ query: { conversation: 'c2' } })
    await flushPromises()
    await wrapper.get('textarea').setValue('其他会话草稿')
    await router.replace({ query: { conversation: 'c1' } })
    await flushPromises()
    const stream = FakeEventSource.instances.at(-1)!
    await requestDelete(wrapper)
    expect(document.body.textContent).toContain('已保存的工作区、session、证据及成果保留，不取消独立复测／报告')
    expect(document.body.textContent).toContain('未发送的输入或附件')
    bodyButton('取消').click()
    await flushPromises()
    expect(mocks.deleteAgentConversation).not.toHaveBeenCalled()
    mocks.deleteAgentConversation.mockRejectedValueOnce(new ApiError('会话仍有排队消息，暂不可删除', 409, {}))
    await requestDelete(wrapper)
    bodyButton('确认删除').click()
    await flushPromises()
    expect(document.body.textContent).toContain('会话仍有排队消息')
    expect(document.querySelectorAll('.agent-history-select')).toHaveLength(2)
    expect((wrapper.get('textarea').element as HTMLTextAreaElement).value).toBe('当前草稿')
    bodyButton('确认删除').click()
    await flushPromises()
    expect(router.currentRoute.value.query.conversation).toBe('c2')
    expect(stream.close).toHaveBeenCalled()
    expect(document.querySelectorAll('.agent-history-select')).toHaveLength(1)
    expect((wrapper.get('textarea').element as HTMLTextAreaElement).value).toBe('其他会话草稿')
    expect(mocks.createAgentConversation).not.toHaveBeenCalled()
  })

  it('删除最后一个会话回到空态，不自动创建', async () => {
    mocks.listAgentConversations.mockResolvedValue([conversations[0]])
    const { wrapper, router } = await openPage()
    await requestDelete(wrapper)
    bodyButton('确认删除').click()
    await flushPromises()
    expect(router.currentRoute.value.query.conversation).toBeUndefined()
    expect(wrapper.find('.agent-composer').exists()).toBe(false)
    expect(wrapper.text()).toContain('暂无会话')
    expect(mocks.createAgentConversation).not.toHaveBeenCalled()
  })

  it('上传失败可重试和移除，发送重试冻结附件集合，续聊展示附件', async () => {
    const attachment = { id: 'file-1', filename: 'sample.md', size: 14, download_url: '/projects/p1/files/file-1', message_id: null }
    mocks.uploadAgentAttachment.mockRejectedValueOnce(new Error('上传中断')).mockResolvedValue(attachment)
    mocks.sendAgentMessage.mockRejectedValueOnce(new TypeError('受理响应中断'))
      .mockImplementation(async (_conversation, body) => message({ ...body, attachments: [{ ...attachment, message_id: 1 }] }))
    const { wrapper, router } = await openPage()
    const file = new File(['# 合成样本'], 'sample.md', { type: 'text/markdown' })
    const input = wrapper.get('input[type="file"]')
    Object.defineProperty(input.element, 'files', { configurable: true, value: [file] })
    await input.trigger('change')
    await flushPromises()
    expect(wrapper.text()).toContain('上传中断')
    await wrapper.get('textarea').setValue('读取附件')
    expect(wrapper.get('button[type="submit"]').attributes('disabled')).toBeDefined()
    await wrapper.findAll('button').find(button => button.text() === '重试上传')!.trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('已上传')
    await wrapper.get('form.agent-composer').trigger('submit')
    await flushPromises()
    const pending = mocks.sendAgentMessage.mock.calls[0]![1]
    expect(pending.attachment_ids).toEqual(['file-1'])
    expect(wrapper.get('button[aria-label="移除附件 sample.md"]').attributes('disabled')).toBeDefined()
    await wrapper.get('form.agent-composer').trigger('submit')
    await flushPromises()
    expect(mocks.sendAgentMessage.mock.calls[1]![1]).toEqual(pending)
    expect(wrapper.find('.agent-draft-attachments').exists()).toBe(false)
    expect(wrapper.get('.agent-message-attachments a').text()).toBe('sample.md')
    mocks.listAgentMessages.mockResolvedValue([message({ attachments: [{ ...attachment, message_id: 1 }] })])
    await router.replace({ query: { conversation: 'c2' } })
    await flushPromises()
    await router.replace({ query: { conversation: 'c1' } })
    await flushPromises()
    expect(wrapper.get('.agent-message-attachments a').attributes('href')).toContain('/files/file-1')
    Object.defineProperty(input.element, 'files', { configurable: true, value: [file] })
    await input.trigger('change')
    await flushPromises()
    await wrapper.get('button[aria-label="移除附件 sample.md"]').trigger('click')
    await flushPromises()
    expect(mocks.deleteAgentAttachment).toHaveBeenCalledExactlyOnceWith('c1', 'file-1')
  })

  it('刷新恢复未发送附件，读取失败不将附件集合当成空集合发送', async () => {
    const attachment = { id: 'f1', filename: 'restored.txt', size: 3, download_url: '/file/f1', message_id: null }
    mocks.listAgentAttachments.mockResolvedValue([attachment])
    const { wrapper, router } = await openPage()
    expect(wrapper.get('.agent-draft-attachments').text()).toContain('restored.txt')
    mocks.listAgentAttachments.mockRejectedValueOnce(new Error('附件不可读取'))
    await router.replace({ query: { conversation: 'c2' } })
    await flushPromises()
    await wrapper.get('textarea').setValue('不能误发空附件')
    await wrapper.get('form.agent-composer').trigger('submit')
    expect(mocks.sendAgentMessage).not.toHaveBeenCalled()
    expect(wrapper.text()).toContain('附件不可读取')
    bodyButton('重新读取附件').click()
    await flushPromises()
    expect(wrapper.get('button[type="submit"]').attributes('disabled')).toBeUndefined()
  })

  it('回复原文可直接阅读，关闭后回入口且保留本地输入', async () => {
    const answer = '# 完整回复\n\n多行正文，不执行链接。'
    mocks.listAgentMessages.mockResolvedValue([message({ status: 'succeeded', answer })])
    const { wrapper } = await openPage()
    await wrapper.get('textarea[aria-label="消息"]').setValue('未发送草稿')
    const trigger = bodyButton('回复原文')
    trigger.focus()
    trigger.click()
    await flushPromises()
    const source = document.querySelector<HTMLTextAreaElement>('textarea[aria-label="回复原文"]')!
    expect(source.value).toBe(answer)
    expect(source.readOnly).toBe(true)
    document.querySelector<HTMLButtonElement>('[role="dialog"] button[aria-label="关闭"]')!.click()
    await flushPromises()
    expect(document.activeElement).toBe(trigger)
    expect(wrapper.get<HTMLTextAreaElement>('textarea[aria-label="消息"]').element.value).toBe('未发送草稿')
    expect(mocks.sendAgentMessage).not.toHaveBeenCalled()
  })

  it('复制只在实际写入后成功并复位，拒绝时提供原文选择', async () => {
    vi.useFakeTimers()
    let resolveCopy!: () => void
    const writeText = vi.fn().mockImplementationOnce(() => new Promise<void>(resolve => { resolveCopy = resolve }))
      .mockRejectedValueOnce(new DOMException('denied', 'NotAllowedError'))
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText } })
    const answer = '# 原文\r\n```text\r\n多行\r\n```'
    mocks.listAgentMessages.mockResolvedValue([message({ status: 'succeeded', answer })])
    const { wrapper } = await openPage()
    await wrapper.get('button[aria-label="复制第 1 轮回复"]').trigger('click')
    expect(wrapper.text()).not.toContain('已复制原文')
    resolveCopy()
    await flushPromises()
    expect(writeText).toHaveBeenCalledWith(answer)
    expect(wrapper.text()).toContain('已复制原文')
    await vi.advanceTimersByTimeAsync(2500)
    expect(wrapper.text()).not.toContain('已复制原文')
    await wrapper.get('button[aria-label="复制第 1 轮回复"]').trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('浏览器未完成复制')
    bodyButton('选择回复原文').click()
    await flushPromises()
    expect(document.querySelector<HTMLTextAreaElement>('textarea[aria-label="回复原文"]')?.value).toBe(answer.replaceAll('\r\n', '\n'))
  })

  it('默认模型配置复用模型管理，保存不重建历史会话', async () => {
    mocks.saveAgentConfiguration.mockResolvedValue({ model_id: 'm1', model_display_name: '测试模型', model_name: 'test-model', available: true, unavailable_reason: null })
    const { wrapper } = await openPage()
    await wrapper.get('button[aria-label="配置 ChatBot 默认模型"]').trigger('click')
    await flushPromises()
    expect(document.body.textContent).toContain('仅影响本人租户的新会话')
    expect(document.querySelector('a[href="/console/workers#models"]')).not.toBeNull()
    document.querySelector('#agent-model-form')!.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }))
    await flushPromises()
    expect(mocks.saveAgentConfiguration).toHaveBeenCalledExactlyOnceWith('m1')
    expect(mocks.createAgentConversation).not.toHaveBeenCalled()
    expect(wrapper.findAll('.agent-history-select')).toHaveLength(2)
  })
})
