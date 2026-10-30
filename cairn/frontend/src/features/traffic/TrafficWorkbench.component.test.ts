import { flushPromises, mount } from '@vue/test-utils'
import { defineComponent, h, ref } from 'vue'
import { createMemoryHistory, createRouter } from 'vue-router'
import { afterEach, expect, it, vi } from 'vitest'
import TrafficWorkbench from './TrafficWorkbench.vue'
import type { TrafficFilters, TrafficPage, TrafficScopeStatus, TrafficSummary } from '@/api/traffic'
import DataTable from '@/components/patterns/DataTable.vue'
import { ApiError } from '@/api/client'
import { Select } from '@/components/ui'
import EvidenceReader from '@/components/patterns/EvidenceReader.vue'
import type { TrafficBodyPreview, TrafficFlowDetail, TrafficProjection, TrafficReadable, TrafficSide } from '@/api/types'

const mocks = vi.hoisted(() => ({ listTraffic: vi.fn(), getTrafficScopes: vi.fn(), getTrafficFlow: vi.fn(), getTrafficReadable: vi.fn(), getTrafficProjection: vi.fn(), getTrafficBodyPreview: vi.fn() }))
vi.mock('@/api/traffic', () => mocks)
vi.mock('@/api/client', async (original) => ({
  ...await original<typeof import('@/api/client')>(),
  api: vi.fn().mockResolvedValue([]),
  getTrafficFlow: mocks.getTrafficFlow,
  getTrafficReadable: mocks.getTrafficReadable,
  getTrafficProjection: mocks.getTrafficProjection,
  getTrafficBodyPreview: mocks.getTrafficBodyPreview,
}))
const Reader = defineComponent({
  name: 'EvidenceReader',
  props: ['entityKey', 'projectId', 'items'],
  emits: ['flowLoaded'],
  setup(_, { expose }) { expose({ refreshFlow: vi.fn().mockResolvedValue(undefined) }) },
  template: '<div data-testid="reader"><header><slot name="header-context" /><slot name="header-actions" /></header>{{ projectId }} / {{ items[0].flow_id }}</div>',
})
function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>((done) => { resolve = done })
  return { promise, resolve }
}
function row(id: string): TrafficSummary {
  return {
    id, project_id: 'project-a', project_title: '合成项目', capture_scope_id: 'scope-a', task_run_id: 'run-a',
    revision: 1, received_at: `2026-10-22T12:00:00Z`, updated_at: '2026-10-22T12:00:00Z', started_at: 1, finished_at: 2,
    method: 'GET', host: 'synthetic.test', path: `/${id}`, url: `http://synthetic.test/${id}`, status_code: 200,
    state: 'complete', source_state: 'complete', error_code: null,
    sync: { bodies: { request: { status: 'available', artifact_id: 'request', captured_bytes: 0 }, response: { status: 'available', artifact_id: 'response', captured_bytes: 4 } } },
    associations: { observation_count: 0, vulnerability_count: 0, items: [], has_more: false },
  }
}
function page(...rows: TrafficSummary[]): TrafficPage { return { items: rows, next_cursor: null, previous_cursor: null, has_more: false, has_previous: false } }
let unmount: (() => void) | undefined
afterEach(() => { unmount?.(); document.body.innerHTML = ''; vi.useRealTimers(); vi.clearAllMocks(); vi.unstubAllGlobals() })

it('迟到筛选响应不能覆盖新列表，深链选择独立于分页和轮询，搜索不进入URL', async () => {
  vi.useFakeTimers()
  let notifyResize!: (entries: { contentRect: { height: number } }[]) => void
  vi.stubGlobal('ResizeObserver', class {
    constructor(callback: typeof notifyResize) { notifyResize = callback }
    observe() {}
    disconnect() {}
  })
  const stale = deferred<TrafficPage>()
  const latest = deferred<TrafficPage>()
  mocks.listTraffic.mockReturnValueOnce(stale.promise).mockReturnValueOnce(latest.promise)
  mocks.getTrafficScopes.mockResolvedValue({ items: [], has_more: false, next_cursor: null })
  const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/traffic', name: 'Traffic', component: TrafficWorkbench }] })
  await router.push('/traffic?flow=pinned&project=project-a')
  await router.isReady()
  const wrapper = mount(TrafficWorkbench, { attachTo: document.body, global: { plugins: [router], stubs: { EvidenceReader: Reader } } })
  unmount = () => wrapper.unmount()
  await flushPromises()
  expect(wrapper.get('[data-testid="reader"]').text()).toContain('project-a / pinned')
  notifyResize([{ contentRect: { height: 1008 } }])
  await flushPromises()
  const splitter = wrapper.get('[role="separator"]')
  expect(Number(splitter.attributes('aria-valuenow'))).toBe(42)
  await splitter.trigger('keydown', { key: 'Home' })
  expect(Number(splitter.attributes('aria-valuenow'))).toBe(15)
  notifyResize([{ contentRect: { height: 618 } }])
  await flushPromises()
  expect(Number(splitter.attributes('aria-valuenow')) * 610 / 100).toBeCloseTo(150)
  await splitter.trigger('keydown', { key: 'End' })
  expect((100 - Number(splitter.attributes('aria-valuenow'))) * 610 / 100).toBeGreaterThanOrEqual(180)
  const staleSignal = mocks.listTraffic.mock.calls[0]![2] as AbortSignal
  await router.push({ query: { ...router.currentRoute.value.query, method: 'POST' } })
  await flushPromises()
  expect(staleSignal.aborted).toBe(true)
  latest.resolve(page(row('current')))
  await flushPromises()
  stale.resolve(page(row('obsolete')))
  await flushPromises()
  expect(wrapper.text()).toContain('/current')
  expect(wrapper.text()).not.toContain('/obsolete')
  expect(wrapper.text()).toContain('当前定位流量不在本页结果中')
  expect(router.currentRoute.value.query.flow).toBe('pinned')

  const polling = deferred<TrafficPage>()
  mocks.listTraffic.mockReturnValueOnce(polling.promise)
  await vi.advanceTimersByTimeAsync(3000)
  await flushPromises()
  expect(mocks.listTraffic).toHaveBeenCalledTimes(3)
  await vi.advanceTimersByTimeAsync(15000)
  expect(mocks.listTraffic).toHaveBeenCalledTimes(3)
  polling.resolve(page(row('new-arrival')))
  await flushPromises()
  expect(router.currentRoute.value.query.flow).toBe('pinned')
  expect(wrapper.text()).toContain('有新流量')

  mocks.listTraffic.mockResolvedValue(page(row('filtered')))
  await wrapper.get('input[aria-label="搜索 URL、Host 或路径"]').setValue('synthetic.test/private?secret=synthetic')
  await wrapper.get('form').trigger('submit')
  await flushPromises()
  expect(mocks.listTraffic.mock.lastCall?.[0].url).toBe('synthetic.test/private?secret=synthetic')
  expect(router.currentRoute.value.fullPath).not.toContain('secret')
  expect(wrapper.get('[data-testid="reader"]').text()).toContain('project-a / pinned')
  wrapper.findAllComponents(Select).find((select) => select.props('ariaLabel') === '筛选项目')!.vm.$emit('update:modelValue', 'project-b')
  await flushPromises()
  expect(router.currentRoute.value.query.flow_project).toBe('project-a')
  expect(wrapper.get('[data-testid="reader"]').text()).toContain('project-a / pinned')

  const moreFilters = wrapper.findAll('button').find((button) => button.text() === '更多筛选')!
  await moreFilters.trigger('click')
  await flushPromises()
  expect(moreFilters.attributes('aria-expanded')).toBe('true')
  wrapper.findAllComponents(Select).find((select) => select.props('ariaLabel') === '筛选采集错误')!.vm.$emit('update:modelValue', 'transport_error')
  await flushPromises()
  expect(router.currentRoute.value.query.error_code).toBe('transport_error')
  expect(mocks.listTraffic.mock.lastCall?.[0].error_code).toBe('transport_error')
  expect(wrapper.get('[data-testid="reader"]').text()).toContain('project-a / pinned')

  mocks.listTraffic.mockRejectedValueOnce(new ApiError('合成权限拒绝', 403, null))
  await vi.advanceTimersByTimeAsync(3000)
  await flushPromises()
  expect(wrapper.text()).toContain('无权读取当前范围')
  expect(wrapper.find('[data-testid="reader"]').exists()).toBe(false)
  const callsAfterDenied = mocks.listTraffic.mock.calls.length
  await vi.advanceTimersByTimeAsync(60000)
  expect(mocks.listTraffic).toHaveBeenCalledTimes(callsAfterDenied)
})

it.each(['全局', '项目内'])('%s清除筛选保留选中报文、固定作用域、来源及页大小，清除选择后仍可返回漏洞', async (entry) => {
  vi.useFakeTimers()
  vi.stubGlobal('ResizeObserver', class { observe() {} disconnect() {} })
  mocks.listTraffic.mockReset().mockResolvedValue(page(row('other')))
  mocks.getTrafficScopes.mockResolvedValue({ items: [], has_more: false, next_cursor: null })
  const router = createRouter({ history: createMemoryHistory(), routes: [
    { path: '/traffic', name: 'Traffic', component: TrafficWorkbench },
    { path: '/projects/:id/traffic', name: 'WorkspaceTraffic', component: TrafficWorkbench },
    { path: '/findings', name: 'Findings', component: { template: '<div />' } },
  ] })
  const source = { vulnerabilityId: 'v-source', vulnerabilityProjectId: 'project-a', vulnerabilityObservationId: 'o-source' }
  await router.push({ name: entry === '全局' ? 'Traffic' : 'WorkspaceTraffic', params: entry === '项目内' ? { id: 'project-a' } : {}, query: {
    flow: 'pinned', project: 'project-a', cursor: 'older-page', limit: '200', method: 'POST', state: 'partial', associated: 'true',
    task_run: 'run-filter', status_family: '7xx', status_code: '777', error_code: 'transport_error', ...source,
  } })
  await router.isReady()
  const wrapper = mount(TrafficWorkbench, { attachTo: document.body, global: { plugins: [router], stubs: { EvidenceReader: Reader } } })
  unmount = () => wrapper.unmount()
  await flushPromises()
  const readerUid = wrapper.getComponent(Reader).vm.$.uid
  expect(wrapper.find('.traffic-selection').exists()).toBe(false)
  expect(wrapper.get('[data-testid="reader"] header').text()).toContain('当前定位流量不在本页结果中')
  expect(wrapper.get('[data-testid="reader"] header').text()).toContain('清除选择')
  expect(wrapper.get('.traffic-filters').text()).toContain('更多筛选 · 5')
  expect(wrapper.find('[aria-label="筛选采集完整性"]').exists()).toBe(false)
  await wrapper.get('input[aria-label="搜索 URL、Host 或路径"]').setValue('synthetic.test/private')
  await wrapper.get('form').trigger('submit')
  await flushPromises()
  expect(mocks.listTraffic.mock.lastCall?.[0].url).toBe('synthetic.test/private')
  const clear = wrapper.findAll('button').find((button) => button.text() === '清除筛选')!
  await clear.trigger('click')
  await flushPromises()
  const filters = mocks.listTraffic.mock.lastCall![0]
  for (const key of ['url', 'cursor', 'task_run_id', 'method', 'state', 'status_family', 'status_code', 'error_code', 'associated']) expect(filters[key]).toBeUndefined()
  expect(filters.project_id).toBe(entry === '项目内' ? 'project-a' : undefined)
  expect(filters.limit).toBe(200)
  expect(router.currentRoute.value.query).toEqual({ flow: 'pinned', limit: '200', ...(entry === '全局' ? { flow_project: 'project-a' } : {}), ...source })
  expect(wrapper.getComponent(Reader).vm.$.uid).toBe(readerUid)
  expect(wrapper.getComponent(Reader).props('projectId')).toBe('project-a')
  expect(wrapper.get<HTMLInputElement>('input[aria-label="搜索 URL、Host 或路径"]').element.value).toBe('')
  expect(clear.attributes('disabled')).toBeDefined()
  expect(wrapper.get('.traffic-return').attributes('href')).toBe(router.resolve({ name: 'Findings', query: source }).href)
  const moreFilters = wrapper.findAll('button').find((button) => button.text() === '更多筛选')!
  await moreFilters.trigger('click')
  await flushPromises()
  expect(moreFilters.attributes('aria-expanded')).toBe('true')
  expect(document.body.querySelector<HTMLInputElement>('input[aria-label="筛选 TaskRun"]')?.value).toBe('')
  expect(document.body.querySelector<HTMLInputElement>('input[aria-label="精确状态码"]')?.value).toBe('')
  await wrapper.get('[data-testid="reader"] header button').trigger('click')
  await flushPromises()
  expect(wrapper.findComponent(Reader).exists()).toBe(false)
  expect(wrapper.get('.traffic-return').text()).toBe('返回漏洞')
  await wrapper.get('.traffic-toolbar').findAll('button').find((button) => button.text() === '刷新')!.trigger('click')
  await flushPromises()
  expect(router.currentRoute.value.query.flow).toBeUndefined()
  expect(router.currentRoute.value.query.cleared).toBe('1')
  expect(wrapper.get('.traffic-toolbar').text()).toContain('详情')
})

it('关联数使用独立键盘按钮选择正确项目的 flow 并聚焦 Inspector，迟到详情不能串流', async () => {
  vi.useFakeTimers()
  vi.stubGlobal('ResizeObserver', class { observe() {} disconnect() {} })
  const scrollIntoView = vi.fn()
  const originalScroll = Object.getOwnPropertyDescriptor(HTMLElement.prototype, 'scrollIntoView')
  Object.defineProperty(HTMLElement.prototype, 'scrollIntoView', { configurable: true, value: scrollIntoView })
  const first = row('shared-id')
  const associated = { ...row('shared-id'), project_id: 'project-b', project_title: '另一合成项目', path: '/linked', associations: { observation_count: 1, vulnerability_count: 1, items: [], has_more: false } }
  mocks.listTraffic.mockReset().mockResolvedValue(page(first, associated))
  mocks.getTrafficScopes.mockResolvedValue({ items: [], has_more: false, next_cursor: null })
  const router = createRouter({ history: createMemoryHistory(), routes: [
    { path: '/traffic', name: 'Traffic', component: TrafficWorkbench },
    { path: '/findings', name: 'Findings', component: { template: '<div />' } },
  ] })
  await router.push('/traffic?flow=shared-id&flow_project=project-a')
  await router.isReady()
  const wrapper = mount(TrafficWorkbench, { attachTo: document.body, global: { plugins: [router], stubs: { EvidenceReader: Reader } } })
  unmount = () => {
    wrapper.unmount()
    if (originalScroll) Object.defineProperty(HTMLElement.prototype, 'scrollIntoView', originalScroll)
    else Reflect.deleteProperty(HTMLElement.prototype, 'scrollIntoView')
  }
  await flushPromises()
  const oldReader = wrapper.getComponent(Reader)
  const link = wrapper.get<HTMLButtonElement>('.traffic-association-link')
  expect(link.element.tagName).toBe('BUTTON')
  expect(link.element.tabIndex).toBe(0)
  link.element.focus()
  expect(document.activeElement).toBe(link.element)
  // jsdom 不执行原生按钮的键盘默认点击；分开核对键盘冒泡与实际激活动作。
  await link.trigger('keydown', { key: 'Enter' })
  expect(router.currentRoute.value.query.flow_project).toBe('project-a')
  await link.trigger('click')
  await flushPromises()
  expect(router.currentRoute.value.query).toEqual({ flow: 'shared-id', flow_project: 'project-b' })
  expect(wrapper.getComponent(Reader).props('projectId')).toBe('project-b')
  expect(document.activeElement).toBe(wrapper.get('[aria-label="关联漏洞"]').element)
  expect(scrollIntoView).toHaveBeenCalledWith({ block: 'nearest' })
  const detail: TrafficFlowDetail = { ...associated, header_text: { request: '', response: null }, scope: { id: 'scope-b', project_id: 'project-b', task_run_id: 'run-b', generation: 1, container_id: 'container-b' }, observations: [{ observation_id: 'o-linked', vulnerability_id: 'v-linked', status: 'accepted', task_run_id: 'run-b' }] }
  wrapper.getComponent(Reader).vm.$emit('flowLoaded', detail)
  oldReader.vm.$emit('flowLoaded', { ...detail, project_id: 'project-a', observations: [] })
  await flushPromises()
  expect(wrapper.get('.traffic-associations a').attributes('href')).toBe(router.resolve({ name: 'Findings', query: { vulnerabilityProjectId: 'project-b', vulnerabilityId: 'v-linked', vulnerabilityObservationId: 'o-linked' } }).href)
  expect(wrapper.findAll('tbody tr').filter((tr) => tr.attributes('aria-current') === 'true')).toHaveLength(1)
  expect(wrapper.get('tr[aria-current="true"]').text()).toContain('/linked')
})

it.each(['自动', '暂停后手动'])('终态流量%s刷新接受关联，不重读正文或重置当前阅读', async (refreshMode) => {
  vi.useFakeTimers()
  vi.stubGlobal('ResizeObserver', class { observe() {} disconnect() {} })
  const selected = row('pinned')
  const detail: TrafficFlowDetail = {
    ...selected,
    header_text: { request: 'GET /pinned HTTP/1.1\r\n\r\n', response: 'HTTP/1.1 200 OK\r\n\r\n' },
    scope: { id: 'scope-a', project_id: 'project-a', task_run_id: 'run-a', generation: 1, container_id: 'container-a' },
    sync: { bodies: {
      request: { status: 'available', artifact_id: 'request', revision: 1, accepted_bytes: 70000, captured_bytes: 70000 },
      response: { status: 'available', artifact_id: 'response', revision: 1, accepted_bytes: 8192, captured_bytes: 8192 },
    } },
    observations: [],
  }
  mocks.listTraffic.mockReset().mockResolvedValue(page(selected))
  mocks.getTrafficScopes.mockResolvedValue({ items: [], has_more: false, next_cursor: null })
  mocks.getTrafficFlow.mockReset().mockImplementation(async () => structuredClone(detail))
  mocks.getTrafficReadable.mockReset().mockImplementation(async (_project: string, _flow: string, side: TrafficSide): Promise<TrafficReadable> => ({
    project_id: 'project-a', flow_id: 'pinned', side, revision: 1, representation: 'http-readable', excerpt_eligible: false,
    header_text: 'GET /pinned HTTP/1.1\r\n\r\n', body_text: 'x'.repeat(side === 'request' ? 70000 : 8192),
    decode_status: 'complete', read_mode: 'auto', deferred_reason: null, body_bytes: side === 'request' ? 70000 : 8192,
    decoded: false, text_escaped: false, display_truncated: false, content_encodings: [], body_limit: 10485760,
    decoded_body_bytes: side === 'request' ? 70000 : 8192, body_artifact_id: side, body_sha256: 'b'.repeat(64),
  }))
  mocks.getTrafficProjection.mockReset().mockImplementation(async (_project: string, _flow: string, side: TrafficSide, offset: number): Promise<TrafficProjection> => ({
    project_id: 'project-a', flow_id: 'pinned', scope: detail.scope, revision: 1, state: 'complete', side,
    representation: 'http-evidence-v1', sha256: 'a'.repeat(64), body_sha256: 'b'.repeat(64), body_artifact_id: side,
    body_display: 'utf8', header_size: 30, size: 70000, offset, next_offset: offset ? 70000 : 65536, has_more: offset === 0,
    text: offset ? 'stable page two' : 'GET /pinned HTTP/1.1\r\n\r\nstable body',
  }))
  mocks.getTrafficBodyPreview.mockReset().mockImplementation(async (_project: string, _flow: string, side: TrafficSide, offset: number): Promise<TrafficBodyPreview> => ({
    side, revision: 1, sealed: true, source_sealed: true, body_complete: true, captured_bytes: 8192, accepted_bytes: 8192,
    sealed_bytes: 8192, size: 8192, offset, next_offset: offset + 4096, bytes_read: 4096, has_more: offset === 0,
    content_base64: btoa('x'.repeat(4096)),
  }))
  // 真实工作台和阅读器走完整读取链，只替换编辑器；保留节点用于观察滚动与选区是否丢失。
  const Editor = defineComponent({
    props: ['modelValue', 'readingState', 'readingView'],
    setup: (props) => () => h('textarea', { value: props.modelValue, readonly: true, 'data-testid': 'editor' }),
  })
  const router = createRouter({ history: createMemoryHistory(), routes: [
    { path: '/traffic', name: 'Traffic', component: TrafficWorkbench },
    { path: '/findings', name: 'Findings', component: { template: '<div />' } },
  ] })
  await router.push('/traffic?flow=pinned&flow_project=project-a')
  await router.isReady()
  const wrapper = mount(TrafficWorkbench, { global: { plugins: [router], stubs: { MonacoEditor: Editor } } })
  unmount = () => wrapper.unmount()
  await flushPromises()
  await wrapper.get('.traffic-toolbar').findAll('button').find((button) => button.text() === '详情')!.trigger('click')
  expect(wrapper.get('.traffic-inspector').text()).toContain('未记录已接受的关联发现')
  expect(wrapper.getComponent(EvidenceReader).emitted('flowLoaded')?.at(-1)?.[0]).toEqual(detail)
  expect(wrapper.find('.traffic-selection').exists()).toBe(false)
  expect(wrapper.get('.evidence-reader__summary').text()).toContain('清除选择')

  const requestPanel = wrapper.get('article[aria-label="请求阅读器"]')
  const responsePanel = wrapper.get('article[aria-label="响应阅读器"]')
  expect(requestPanel.getComponent(Editor).props('modelValue')).toContain('x'.repeat(70000))
  expect(requestPanel.findAll('button').some((button) => button.text() === '下一页')).toBe(false)
  expect(mocks.getTrafficProjection).not.toHaveBeenCalled()
  await requestPanel.findAll('button').find((button) => button.text() === 'Raw')!.trigger('click')
  await flushPromises()
  await requestPanel.findAll('button').find((button) => button.text() === '下一页')!.trigger('click')
  await responsePanel.findAll('button').find((button) => button.text() === 'Hex')!.trigger('click')
  await flushPromises()
  await responsePanel.findAll('button').find((button) => button.text() === '下一页')!.trigger('click')
  await flushPromises()
  expect(requestPanel.text()).toContain('UTF-8 字节 65536–70000')
  expect(responsePanel.text()).toContain('正文 4096–8192')
  const readerUid = wrapper.getComponent(EvidenceReader).vm.$.uid
  const editors = wrapper.findAllComponents(Editor)
  const readingStates = editors.map((editor) => editor.props('readingState'))
  const textareas = wrapper.findAll<HTMLTextAreaElement>('[data-testid="editor"]').map((editor) => editor.element)
  textareas.forEach((element) => { element.scrollTop = 120; element.setSelectionRange(2, 8) })
  const projectionCalls = mocks.getTrafficProjection.mock.calls.length
  const bodyCalls = mocks.getTrafficBodyPreview.mock.calls.length
  const detailCalls = mocks.getTrafficFlow.mock.calls.length
  expect(mocks.getTrafficReadable).toHaveBeenCalledTimes(2)
  expect(projectionCalls).toBe(2)
  expect(bodyCalls).toBe(2)

  if (refreshMode === '暂停后手动') {
    await wrapper.get('.traffic-toolbar').findAll('button').find((button) => button.text() === '暂停刷新')!.trigger('click')
  }
  const association = { observation_id: 'observation-later', vulnerability_id: 'vulnerability-later', status: 'accepted', task_run_id: 'run-a' }
  mocks.getTrafficFlow.mockResolvedValue({ ...detail, observations: [association] })
  // 新列表不含当前选择，关联必须仍从选中 flow 的授权详情取得。
  mocks.listTraffic.mockResolvedValue(page(row('new-arrival')))
  await vi.advanceTimersByTimeAsync(3000)
  await flushPromises()
  if (refreshMode === '暂停后手动') {
    expect(mocks.getTrafficFlow).toHaveBeenCalledTimes(detailCalls)
    expect(wrapper.get('.traffic-inspector').text()).toContain('未记录已接受的关联发现')
    await wrapper.get('.traffic-toolbar').findAll('button').find((button) => button.text() === '刷新')!.trigger('click')
    await flushPromises()
  }
  expect(mocks.getTrafficFlow).toHaveBeenCalledTimes(detailCalls + 1)
  expect(mocks.getTrafficFlow).toHaveBeenLastCalledWith('project-a', 'pinned', expect.any(AbortSignal))
  expect(wrapper.get('.traffic-associations a').attributes('href')).toBe(router.resolve({ name: 'Findings', query: {
    vulnerabilityProjectId: 'project-a', vulnerabilityId: association.vulnerability_id, vulnerabilityObservationId: association.observation_id,
  } }).href)
  expect(wrapper.get('.traffic-associations').text()).toContain('Observation observation-later')
  expect(router.currentRoute.value.query).toEqual({ flow: 'pinned', flow_project: 'project-a' })
  expect(wrapper.get('.evidence-reader__summary').text()).toContain('当前定位流量不在本页结果中')
  await router.push({ query: { ...router.currentRoute.value.query, method: 'GET' } })
  await flushPromises()
  await wrapper.findAll('button').find((button) => button.text() === '清除筛选')!.trigger('click')
  await flushPromises()
  expect(router.currentRoute.value.query).toEqual({ flow: 'pinned', flow_project: 'project-a' })
  expect(wrapper.getComponent(EvidenceReader).vm.$.uid).toBe(readerUid)
  expect(mocks.getTrafficReadable).toHaveBeenCalledTimes(2)
  expect(mocks.getTrafficProjection).toHaveBeenCalledTimes(projectionCalls)
  expect(mocks.getTrafficBodyPreview).toHaveBeenCalledTimes(bodyCalls)
  expect(requestPanel.text()).toContain('UTF-8 字节 65536–70000')
  expect(responsePanel.text()).toContain('正文 4096–8192')
  wrapper.findAllComponents(Editor).forEach((editor, index) => {
    expect(editor.props('readingState')).toBe(readingStates[index])
    expect(editor.props('readingView')).toBe(index === 0 ? 'raw' : 'hex')
    const element = editor.element as HTMLTextAreaElement
    expect(element).toBe(textareas[index])
    expect(element.scrollTop).toBe(120)
    expect([element.selectionStart, element.selectionEnd]).toEqual([2, 8])
  })
})

it('列宽仅显式启用；指针捕获、最小宽度和键盘调宽不选行，刷新翻页与浏览器返回保留阅读状态', async () => {
  vi.useFakeTimers()
  vi.stubGlobal('ResizeObserver', class { observe() {} disconnect() {} })
  const longRow = { ...row('first'), path: `/${'long-path/'.repeat(80)}` }
  longRow.url = `https://synthetic.test${longRow.path}`
  const first = { ...page(longRow), next_cursor: 'second', has_more: true }
  const second = { ...page(row('second')), previous_cursor: 'before-second', has_previous: true, next_cursor: 'third', has_more: true }
  const third = { ...page(row('third')), previous_cursor: 'before-third', has_previous: true }
  mocks.listTraffic.mockReset().mockImplementation(async (filters: TrafficFilters) => filters.cursor === 'second' ? second : filters.cursor === 'third' ? third : first)
  mocks.getTrafficScopes.mockResolvedValue({ items: [], has_more: false, next_cursor: null })
  const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/traffic', name: 'Traffic', component: TrafficWorkbench }] })
  await router.push('/traffic?flow=pinned&flow_project=project-a')
  await router.isReady()
  const wrapper = mount(TrafficWorkbench, { global: { plugins: [router], stubs: { EvidenceReader: Reader } } })
  unmount = () => wrapper.unmount()
  await flushPromises()
  const readerUid = wrapper.getComponent(Reader).vm.$.uid
  const button = (label: string) => wrapper.findAll('button').find((entry) => entry.text() === label)!
  const position = () => wrapper.get('.traffic-page-position').text()
  expect(position()).toBe('第 1 页 · 本页 1 条')
  const handle = wrapper.get('[aria-label="调整Host / 路径列宽"]')
  const capture = vi.fn()
  const release = vi.fn()
  Object.assign(handle.element, { setPointerCapture: capture, releasePointerCapture: release })
  for (const [type, clientX] of [['pointerdown', 420], ['pointermove', 560], ['pointerup', 560]] as const) {
    const event = new MouseEvent(type, { bubbles: true, button: 0, clientX })
    Object.defineProperty(event, 'pointerId', { value: 4 })
    handle.element.dispatchEvent(event)
    await flushPromises()
  }
  expect(capture).toHaveBeenCalledWith(4)
  expect(release).toHaveBeenCalledWith(4)
  // 右侧状态码列只能从 80 缩至 72，剩余拖动不能扩大整表。
  expect(handle.attributes('aria-valuenow')).toBe('428')
  await handle.trigger('keydown', { key: 'Home' })
  await handle.trigger('keydown', { key: 'ArrowLeft' })
  expect(handle.attributes('aria-valuenow')).toBe('160')
  await handle.trigger('keydown', { key: 'ArrowRight', shiftKey: true })
  await handle.trigger('click')
  expect(handle.attributes('aria-valuenow')).toBe('200')
  expect(wrapper.findAll('col')[3]!.attributes('style')).toContain('width: 200px')
  expect(wrapper.get('.traffic-target').attributes('title')).toBe(longRow.url)
  expect(router.currentRoute.value.query.flow).toBe('pinned')

  await button('下一页').trigger('click')
  await flushPromises()
  expect(position()).toBe('第 2 页 · 本页 1 条')
  expect(handle.attributes('aria-valuenow')).toBe('200')
  const failed = deferred<TrafficPage>()
  mocks.listTraffic.mockReturnValueOnce(failed.promise)
  await button('下一页').trigger('click')
  await flushPromises()
  expect(position()).toBe('第 2 页 · 本页 1 条')
  // 失败请求不写入页序；先离开这个未完成请求，再检查成功访问记录恢复。
  router.back()
  await flushPromises()
  failed.resolve(third)
  await flushPromises()
  expect(position()).toBe('第 2 页 · 本页 1 条')
  mocks.listTraffic.mockRejectedValueOnce(new Error('合成分页失败'))
  await button('下一页').trigger('click')
  await flushPromises()
  expect(position()).toBe('第 2 页 · 本页 1 条')
  expect(wrapper.text()).toContain('刷新失败')
  expect(router.options.history.state.trafficPage).toBeUndefined()
  await button('刷新').trigger('click')
  await flushPromises()
  expect(position()).toBe('第 3 页 · 本页 1 条')
  router.back()
  await flushPromises()
  expect(position()).toBe('第 2 页 · 本页 1 条')
  await button('详情').trigger('click')
  await vi.advanceTimersByTimeAsync(3000)
  await flushPromises()
  expect(position()).toBe('第 2 页 · 本页 1 条')
  expect(wrapper.getComponent(Reader).vm.$.uid).toBe(readerUid)
  expect(handle.attributes('aria-valuenow')).toBe('200')
  expect(router.currentRoute.value.query.flow).toBe('pinned')
  await button('返回最新页').trigger('click')
  await flushPromises()
  expect(position()).toBe('第 1 页 · 本页 1 条')
  expect(router.currentRoute.value.query.cursor).toBeUndefined()
  await button('下一页').trigger('click')
  await flushPromises()
  wrapper.findAllComponents(Select).find((select) => select.props('ariaLabel') === '每页流量数')!.vm.$emit('update:modelValue', '100')
  await flushPromises()
  expect(position()).toBe('第 1 页 · 本页 1 条')
  expect(mocks.listTraffic.mock.lastCall?.[0].limit).toBe(100)
  expect(handle.attributes('aria-valuenow')).toBe('200')

  const ordinary = mount(DataTable, { props: { rows: [longRow], columns: [{ key: 'path', label: '路径', width: 200, minWidth: 160, value: (value) => (value as TrafficSummary).path }], rowKey: (value) => (value as TrafficSummary).id, caption: '普通表格' } })
  expect(ordinary.find('colgroup').exists()).toBe(false)
  expect(ordinary.find('[role="separator"]').exists()).toBe(false)
  expect(ordinary.get('table').classes()).not.toContain('data-table--resizable')
  ordinary.unmount()
})

it('未知外部cursor显示历史定位，空历史页保留导航；成功页序在离页重挂载后恢复', async () => {
  vi.useFakeTimers()
  vi.stubGlobal('ResizeObserver', class { observe() {} disconnect() {} })
  mocks.getTrafficScopes.mockResolvedValue({ items: [], has_more: false, next_cursor: null })
  mocks.listTraffic.mockReset().mockImplementation(async (filters: TrafficFilters) => filters.cursor ? { ...page(), previous_cursor: 'newer', has_previous: true } : { ...page(row('first')), next_cursor: 'second', has_more: true })
  const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/traffic', name: 'Traffic', component: TrafficWorkbench }] })
  await router.push('/traffic?cursor=external&cleared=1')
  await router.isReady()
  const visible = ref(true)
  const host = defineComponent({ setup: () => () => visible.value ? h(TrafficWorkbench) : h('div') })
  const wrapper = mount(host, { global: { plugins: [router], stubs: { EvidenceReader: Reader } } })
  unmount = () => wrapper.unmount()
  await flushPromises()
  const button = (label: string) => wrapper.findAll('button').find((entry) => entry.text() === label)!
  expect(wrapper.get('.traffic-page-position').text()).toBe('历史定位 · 本页 0 条')
  expect(button('上一页').attributes('disabled')).toBeUndefined()
  await button('上一页').trigger('click')
  await flushPromises()
  expect(wrapper.get('.traffic-page-position').text()).toBe('历史定位 · 本页 0 条')
  await button('返回最新页').trigger('click')
  await flushPromises()
  await button('下一页').trigger('click')
  await flushPromises()
  expect(wrapper.get('.traffic-page-position').text()).toBe('第 2 页 · 本页 0 条')
  await router.push({ query: { ...router.currentRoute.value.query, flow: 'pinned', flow_project: 'project-a' } })
  await flushPromises()
  expect(router.options.history.state.trafficPage).toBe(2)
  visible.value = false
  await flushPromises()
  visible.value = true
  await flushPromises()
  expect(wrapper.get('.traffic-page-position').text()).toBe('第 2 页 · 本页 0 条')
  await wrapper.get('input[aria-label="搜索 URL、Host 或路径"]').setValue('synthetic.test/private?secret=sample')
  await wrapper.get('form').trigger('submit')
  await flushPromises()
  expect(wrapper.get('.traffic-page-position').text()).toBe('第 1 页 · 本页 1 条')
  await button('下一页').trigger('click')
  await flushPromises()
  expect(wrapper.get('.traffic-page-position').text()).toBe('第 2 页 · 本页 0 条')
  router.back()
  await flushPromises()
  expect(wrapper.get('.traffic-page-position').text()).toBe('第 1 页 · 本页 1 条')
  expect(router.currentRoute.value.fullPath).not.toContain('secret')
  expect(JSON.stringify(router.options.history.state)).not.toContain('secret')
  expect(wrapper.get('.traffic-pagination').text()).not.toContain('共')
})

it('搜索第二页与查询上下文一起恢复；整页刷新丢失上下文时请求前回到最新页', async () => {
  vi.useFakeTimers()
  vi.stubGlobal('ResizeObserver', class { observe() {} disconnect() {} })
  const identities = new Map<string, string>()
  const mismatches: string[] = []
  mocks.listTraffic.mockReset().mockImplementation(async (filters: TrafficFilters) => {
    const identity = JSON.stringify({ ...filters, cursor: undefined })
    if (filters.cursor && identities.get(filters.cursor) !== identity) {
      mismatches.push(filters.cursor)
      throw new ApiError('traffic_summary_cursor_invalid', 422, null)
    }
    if (filters.cursor) return { ...page(row('second')), has_previous: true, previous_cursor: 'previous' }
    const next = `opaque-${identities.size}`
    identities.set(next, identity)
    return { ...page(row('first')), has_more: true, next_cursor: next }
  })
  mocks.getTrafficScopes.mockResolvedValue({ items: [], has_more: false, next_cursor: null })
  const routes = [{ path: '/traffic', name: 'Traffic', component: TrafficWorkbench }]
  const router = createRouter({ history: createMemoryHistory(), routes })
  await router.push('/traffic?project=project-a&flow=pinned')
  await router.isReady()
  const visible = ref(true)
  const host = defineComponent({ setup: () => () => visible.value ? h(TrafficWorkbench) : h('div') })
  const wrapper = mount(host, { global: { plugins: [router], stubs: { EvidenceReader: Reader } } })
  unmount = () => wrapper.unmount()
  await flushPromises()
  const search = 'synthetic.test/private?secret=sample'
  const button = (label: string) => wrapper.findAll('button').find((entry) => entry.text() === label)!
  // 从非搜索历史页切入搜索，也不能发出新搜索加旧 cursor 的中间请求。
  await button('下一页').trigger('click')
  await flushPromises()
  await wrapper.get('input[aria-label="搜索 URL、Host 或路径"]').setValue(search)
  await wrapper.get('form').trigger('submit')
  await flushPromises()
  await button('下一页').trigger('click')
  await flushPromises()
  const searchCursor = router.currentRoute.value.query.cursor
  const readerUid = wrapper.getComponent(Reader).vm.$.uid
  await button('刷新').trigger('click')
  await flushPromises()
  expect(wrapper.getComponent(Reader).vm.$.uid).toBe(readerUid)
  expect(wrapper.get('.traffic-page-position').text()).toBe('第 2 页 · 本页 1 条')
  visible.value = false
  await flushPromises()
  visible.value = true
  await flushPromises()
  expect(router.currentRoute.value.query.cursor).toBe(searchCursor)
  expect(wrapper.get<HTMLInputElement>('input[aria-label="搜索 URL、Host 或路径"]').element.value).toBe(search)
  expect(wrapper.get('.traffic-page-position').text()).toBe('第 2 页 · 本页 1 条')
  expect(wrapper.get('[data-testid="reader"]').text()).toContain('project-a / pinned')
  expect(mocks.listTraffic.mock.lastCall?.[0].url).toBe(search)
  const location = router.currentRoute.value.fullPath
  const history = { ...router.options.history.state }
  expect(location).not.toContain('secret')
  expect(JSON.stringify(history)).not.toContain('secret')
  expect(mismatches).toEqual([])
  wrapper.unmount()

  // 新应用／Router 不共享内存，模拟整页刷新；仅保留地址和不含原文的 history。
  const reloadedRouter = createRouter({ history: createMemoryHistory(), routes })
  await reloadedRouter.push(location)
  await reloadedRouter.isReady()
  reloadedRouter.options.history.replace(location, history)
  mocks.listTraffic.mockClear()
  const reloaded = mount(TrafficWorkbench, { global: { plugins: [reloadedRouter], stubs: { EvidenceReader: Reader } } })
  unmount = () => reloaded.unmount()
  await flushPromises()
  expect(mocks.listTraffic).toHaveBeenCalledTimes(1)
  expect(mocks.listTraffic.mock.calls[0]![0]).toMatchObject({ cursor: undefined, url: undefined })
  expect(reloadedRouter.currentRoute.value.query.cursor).toBeUndefined()
  expect(reloadedRouter.currentRoute.value.query.search_context).toBeUndefined()
  expect(reloadedRouter.options.history.state.trafficPage).toBe(1)
  expect(reloaded.text()).toContain('搜索上下文已失效，已返回最新页。')
  expect(reloaded.get('.traffic-page-position').text()).toBe('第 1 页 · 本页 1 条')
  expect(reloaded.get('[data-testid="reader"]').text()).toContain('project-a / pinned')
  expect(mismatches).toEqual([])
})

it('采集、等待终止、停止补传与同步结果分开，历史故障不覆盖当前完成且未知积压不报零', async () => {
  vi.useFakeTimers()
  vi.stubGlobal('ResizeObserver', class { observe() {} disconnect() {} })
  const status = (state: string, extra: Partial<NonNullable<TrafficScopeStatus['status']>> = {}): TrafficScopeStatus['status'] => ({ state, code: null, pending: null, pending_bytes: null, control_stopped: false, test_stopped: false, collector_stopped: false, upload_complete: false, ...extra })
  const stopped = { control_stopped: true, test_stopped: true, collector_stopped: true }
  const statuses = [status('running'), status('stopping'), status('stopped', stopped), status('stopped', { ...stopped, upload_complete: true, failure_code: 'old_failure' }), status('retrying', stopped), status('blocked', stopped),
    status('blocked', { ...stopped, code: 'status_persist_failed', pending: 3, pending_bytes: 17 }),
    status('blocked', { code: 'status_persist_failed' })]
  mocks.listTraffic.mockReset().mockResolvedValue(page(row('first')))
  mocks.getTrafficScopes.mockResolvedValue({ items: statuses.map((value, index) => ({ id: `scope-${index}`, task_run_id: `run-${index}`, status: value, status_updated_at: null })), has_more: false, next_cursor: null })
  const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/traffic', name: 'Traffic', component: TrafficWorkbench }] })
  await router.push('/traffic?project=project-a&cleared=1')
  await router.isReady()
  const wrapper = mount(TrafficWorkbench, { global: { plugins: [router], stubs: { EvidenceReader: Reader } } })
  unmount = () => wrapper.unmount()
  await flushPromises()
  for (const text of ['正在采集', '等待终止确认', '采集已停止，正在同步已有流量', '同步完成', '同步失败，正在重试']) expect(wrapper.get('.traffic-scope-summary').text()).toContain(text)
  await wrapper.findAll('button').find((button) => button.text() === '详情')!.trigger('click')
  const details = wrapper.findAll('.traffic-inspector details')
  expect(details[3]!.get('summary').text()).toContain('同步完成')
  expect(details[3]!.text()).toContain('历史故障old_failure')
  expect(details[5]!.get('summary').text()).toContain('同步失败')
  expect(details[0]!.text()).toContain('条数未知 / 字节数未知')
  expect(details[0]!.text()).not.toContain('0 条')
  expect(details[6]!.get('summary').text()).toContain('同步失败，上传已停止')
  expect(details[6]!.text()).toContain('3 条 / 17 B')
  expect(details[6]!.get('summary').text()).not.toMatch(/正在同步|正在重试/)
  expect(details[7]!.get('summary').text()).toContain('同步失败，上传已停止；等待终止确认')
})
