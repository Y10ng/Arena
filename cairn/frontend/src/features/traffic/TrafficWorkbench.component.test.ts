import { DOMWrapper, flushPromises, mount } from '@vue/test-utils'
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
  emits: ['flowLoaded', 'expandedChange'],
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
    method: 'GET', host: 'synthetic.test', display_host: 'http://synthetic.test', display_target: `/${id}`, path: `/${id}`, url: `http://synthetic.test/${id}`, status_code: 200,
    state: 'complete', source_state: 'complete', error_code: null,
    sync: { bodies: { request: { status: 'available', artifact_id: 'request', captured_bytes: 0 }, response: { status: 'available', artifact_id: 'response', captured_bytes: 4 } } },
    associations: { observation_count: 0, vulnerability_count: 0, items: [], has_more: false },
  }
}
function page(...rows: TrafficSummary[]): TrafficPage { return { items: rows, page: 1, limit: 50, total: rows.length, total_pages: rows.length ? 1 : 0 } }
const overlay = () => new DOMWrapper(document.body)
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
  expect(Number(splitter.attributes('aria-valuenow'))).toBe(34)
  await splitter.trigger('keydown', { key: 'Home' })
  expect(Number(splitter.attributes('aria-valuenow')) * 996 / 100).toBeCloseTo(200)
  notifyResize([{ contentRect: { height: 618 } }])
  await flushPromises()
  expect(Number(splitter.attributes('aria-valuenow')) * 606 / 100).toBeCloseTo(200)
  await splitter.trigger('keydown', { key: 'End' })
  expect((100 - Number(splitter.attributes('aria-valuenow'))) * 606 / 100).toBeGreaterThanOrEqual(240)
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
  expect(wrapper.text()).toContain('/new-arrival')
  expect(wrapper.text()).not.toContain('有新流量')

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

  const readerInstance = wrapper.getComponent(Reader).vm
  const tableElement = wrapper.get('.traffic-list').element
  const filtersElement = wrapper.get('.traffic-filters').element
  const splitBeforeExpand = splitter.attributes('aria-valuenow')
  readerInstance.$emit('expandedChange', true)
  await flushPromises()
  expect(wrapper.get('.traffic-list').isVisible()).toBe(false)
  expect(wrapper.get('.traffic-filters').isVisible()).toBe(false)
  expect(splitter.isVisible()).toBe(false)
  expect(wrapper.get('.traffic-main').attributes('style')).toContain('minmax(0, 1fr)')
  readerInstance.$emit('expandedChange', false)
  await flushPromises()
  expect(wrapper.get('.traffic-list').element).toBe(tableElement)
  expect(wrapper.get('.traffic-filters').element).toBe(filtersElement)
  expect(wrapper.get<HTMLInputElement>('input[aria-label="搜索 URL、Host 或路径"]').element.value).toBe('synthetic.test/private?secret=synthetic')
  expect(wrapper.getComponent(Reader).vm.$.uid).toBe(readerInstance.$.uid)
  expect(wrapper.get('.traffic-list').isVisible()).toBe(true)
  expect(splitter.attributes('aria-valuenow')).toBe(splitBeforeExpand)
  readerInstance.$emit('expandedChange', true)
  await flushPromises()

  mocks.listTraffic.mockRejectedValueOnce(new ApiError('合成权限拒绝', 403, null))
  await vi.advanceTimersByTimeAsync(3000)
  await flushPromises()
  expect(wrapper.text()).toContain('无权读取当前范围')
  expect(wrapper.find('[data-testid="reader"]').exists()).toBe(false)
  expect(wrapper.get('.traffic-list').isVisible()).toBe(true)
  expect(splitter.isVisible()).toBe(true)
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
    flow: 'pinned', project: 'project-a', page: '2', limit: '200', method: 'POST', state: 'partial', associated: 'true',
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
  expect(filters.page).toBe(1)
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
  expect(wrapper.get('.traffic-toolbar').text()).toContain('采集信息')
})

it('关联数选择正确项目的 flow 并聚焦采集信息浮层，迟到详情不能串流且关闭回焦', async () => {
  vi.useFakeTimers()
  vi.stubGlobal('ResizeObserver', class { observe() {} disconnect() {} })
  const first = row('shared-id')
  const associated = { ...row('shared-id'), project_id: 'project-b', project_title: '另一合成项目', path: '/linked', display_target: '/linked', associations: { observation_count: 1, vulnerability_count: 1, items: [], has_more: false } }
  mocks.listTraffic.mockReset().mockResolvedValue(page(first, associated))
  mocks.getTrafficScopes.mockResolvedValue({ items: [], has_more: false, next_cursor: null })
  const router = createRouter({ history: createMemoryHistory(), routes: [
    { path: '/traffic', name: 'Traffic', component: TrafficWorkbench },
    { path: '/findings', name: 'Findings', component: { template: '<div />' } },
  ] })
  await router.push('/traffic?flow=shared-id&flow_project=project-a')
  await router.isReady()
  const wrapper = mount(TrafficWorkbench, { attachTo: document.body, global: { plugins: [router], stubs: { EvidenceReader: Reader } } })
  unmount = () => wrapper.unmount()
  await flushPromises()
  const oldReader = wrapper.getComponent(Reader)
  const link = wrapper.get<HTMLButtonElement>('.traffic-table__link')
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
  expect(document.activeElement).toBe(overlay().get('[aria-label="关联漏洞"]').element)
  expect(overlay().get('[role="dialog"]').text()).toContain('采集信息')
  const detail: TrafficFlowDetail = { ...associated, header_text: { request: '', response: null }, scope: { id: 'scope-b', project_id: 'project-b', task_run_id: 'run-b', generation: 1, container_id: 'container-b' }, observations: [{ observation_id: 'o-linked', vulnerability_id: 'v-linked', status: 'accepted', task_run_id: 'run-b', source: 'observation' }] }
  wrapper.getComponent(Reader).vm.$emit('flowLoaded', detail)
  oldReader.vm.$emit('flowLoaded', { ...detail, project_id: 'project-a', observations: [] })
  await flushPromises()
  expect(overlay().get('.traffic-associations a').attributes('href')).toBe(router.resolve({ name: 'Findings', query: { vulnerabilityProjectId: 'project-b', vulnerabilityId: 'v-linked', vulnerabilityObservationId: 'o-linked' } }).href)
  expect(wrapper.findAll('tbody tr').filter((tr) => tr.attributes('aria-current') === 'true')).toHaveLength(1)
  expect(wrapper.get('tr[aria-current="true"]').text()).toContain('/linked')
  await overlay().get('[role="dialog"]').trigger('keydown', { key: 'Escape' })
  await flushPromises()
  expect(overlay().find('[role="dialog"]').exists()).toBe(false)
  expect(document.activeElement).toBe(link.element)
  expect(wrapper.getComponent(Reader).props('projectId')).toBe('project-b')
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
  await wrapper.get('.traffic-toolbar').findAll('button').find((button) => button.text() === '采集信息')!.trigger('click')
  await flushPromises()
  expect(overlay().get('.traffic-metadata').text()).toContain('未记录已接受的关联发现')
  await overlay().get('[aria-label="关闭"]').trigger('click')
  await flushPromises()
  expect(wrapper.getComponent(EvidenceReader).emitted('flowLoaded')?.at(-1)?.[0]).toEqual(detail)
  expect(wrapper.find('.traffic-selection').exists()).toBe(false)
  expect(wrapper.get('.evidence-reader__summary').text()).toContain('清除选择')

  const requestPanel = wrapper.get('article[aria-label="请求阅读器"]')
  const responsePanel = wrapper.get('article[aria-label="响应阅读器"]')
  expect(requestPanel.getComponent(Editor).props('modelValue')).toContain('x'.repeat(70000))
  expect(requestPanel.findAll('button').some((button) => button.text() === '下一页')).toBe(false)
  expect(mocks.getTrafficProjection).not.toHaveBeenCalled()
  await requestPanel.findAll('button').find((button) => button.text() === '投影')!.trigger('click')
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
    expect(wrapper.getComponent(EvidenceReader).emitted('flowLoaded')?.at(-1)?.[0]).toMatchObject({ observations: [] })
    await wrapper.get('.traffic-toolbar').findAll('button').find((button) => button.text() === '刷新')!.trigger('click')
    await flushPromises()
  }
  expect(mocks.getTrafficFlow).toHaveBeenCalledTimes(detailCalls + 1)
  expect(mocks.getTrafficFlow).toHaveBeenLastCalledWith('project-a', 'pinned', expect.any(AbortSignal))
  await wrapper.get('.traffic-toolbar').findAll('button').find((button) => button.text() === '采集信息')!.trigger('click')
  await flushPromises()
  expect(overlay().get('.traffic-associations a').attributes('href')).toBe(router.resolve({ name: 'Findings', query: {
    vulnerabilityProjectId: 'project-a', vulnerabilityId: association.vulnerability_id, vulnerabilityObservationId: association.observation_id,
  } }).href)
  expect(overlay().get('.traffic-associations').text()).toContain('Observation observation-later')
  await overlay().get('[aria-label="关闭"]').trigger('click')
  await flushPromises()
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
  longRow.display_target = `${longRow.path}?x=one%20two&x=+`
  const first = { ...page(longRow), total: 101, total_pages: 3 }
  const second = { ...page(row('second')), page: 2, total: 101, total_pages: 3 }
  const third = { ...page(row('third')), page: 3, total: 101, total_pages: 3 }
  mocks.listTraffic.mockReset().mockImplementation(async (filters: TrafficFilters) => ({ ...(filters.page === 2 ? second : filters.page === 3 ? third : first), limit: filters.limit || 50, total_pages: Math.ceil(101 / (filters.limit || 50)) }))
  mocks.getTrafficScopes.mockResolvedValue({ items: [], has_more: false, next_cursor: null })
  const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/traffic', name: 'Traffic', component: TrafficWorkbench }] })
  await router.push('/traffic?flow=pinned&flow_project=project-a')
  await router.isReady()
  const wrapper = mount(TrafficWorkbench, { global: { plugins: [router], stubs: { EvidenceReader: Reader } } })
  unmount = () => wrapper.unmount()
  await flushPromises()
  const readerUid = wrapper.getComponent(Reader).vm.$.uid
  const button = (label: string) => wrapper.findAll('button').find((entry) => entry.text() === label || entry.attributes('aria-label') === label)!
  const position = () => wrapper.get('.pagination__position').text()
  expect(position()).toBe('第 1/3 页')
  const handle = wrapper.get('[aria-label="调整URL列宽"]')
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
  // 相邻状态码列触及最小宽度后，不继续扩大整表。
  expect(handle.attributes('aria-valuenow')).toBe('432')
  await handle.trigger('keydown', { key: 'Home' })
  await handle.trigger('keydown', { key: 'ArrowLeft' })
  expect(handle.attributes('aria-valuenow')).toBe('260')
  await handle.trigger('keydown', { key: 'ArrowRight', shiftKey: true })
  await handle.trigger('click')
  expect(handle.attributes('aria-valuenow')).toBe('300')
  expect(wrapper.findAll('col')[2]!.attributes('style')).toContain('width: 300px')
  expect(wrapper.get('.traffic-table__target').attributes('title')).toBe(longRow.display_target)
  expect(router.currentRoute.value.query.flow).toBe('pinned')

  await button('下一页').trigger('click')
  await flushPromises()
  expect(position()).toBe('第 2/3 页')
  expect(handle.attributes('aria-valuenow')).toBe('300')
  const failed = deferred<TrafficPage>()
  mocks.listTraffic.mockReturnValueOnce(failed.promise)
  await button('下一页').trigger('click')
  await flushPromises()
  expect(position()).toBe('第 2/3 页')
  // 失败请求不写入页序；先离开这个未完成请求，再检查成功访问记录恢复。
  router.back()
  await flushPromises()
  failed.resolve(third)
  await flushPromises()
  expect(position()).toBe('第 2/3 页')
  mocks.listTraffic.mockRejectedValueOnce(new Error('合成分页失败'))
  await button('下一页').trigger('click')
  await flushPromises()
  expect(position()).toBe('第 2/3 页')
  expect(wrapper.text()).toContain('刷新失败')
  await button('刷新').trigger('click')
  await flushPromises()
  expect(position()).toBe('第 3/3 页')
  router.back()
  await flushPromises()
  expect(position()).toBe('第 2/3 页')
  await button('采集信息').trigger('click')
  await vi.advanceTimersByTimeAsync(3000)
  await flushPromises()
  await overlay().get('[aria-label="关闭"]').trigger('click')
  await flushPromises()
  expect(position()).toBe('第 2/3 页')
  expect(wrapper.getComponent(Reader).vm.$.uid).toBe(readerUid)
  expect(handle.attributes('aria-valuenow')).toBe('300')
  expect(router.currentRoute.value.query.flow).toBe('pinned')
  await button('返回最新页').trigger('click')
  await flushPromises()
  expect(position()).toBe('第 1/3 页')
  expect(router.currentRoute.value.query.cursor).toBeUndefined()
  await button('下一页').trigger('click')
  await flushPromises()
  wrapper.findAllComponents(Select).find((select) => select.props('ariaLabel') === '流量分页每页条数')!.vm.$emit('update:modelValue', '100')
  await flushPromises()
  expect(position()).toBe('第 1/2 页')
  expect(mocks.listTraffic.mock.lastCall?.[0].limit).toBe(100)
  expect(handle.attributes('aria-valuenow')).toBe('300')

  const ordinary = mount(DataTable, { props: { rows: [longRow], columns: [{ key: 'path', label: '路径', width: 200, minWidth: 160, value: (value) => (value as TrafficSummary).path }], rowKey: (value) => (value as TrafficSummary).id, caption: '普通表格' } })
  expect(ordinary.find('colgroup').exists()).toBe(false)
  expect(ordinary.find('[role="separator"]').exists()).toBe(false)
  expect(ordinary.get('table').classes()).not.toContain('data-table--resizable')
  ordinary.unmount()
})

it('历史页后台仅探测新流量，不替换行、计数或选包；显式翻页读取当前集合', async () => {
  vi.useFakeTimers()
  vi.stubGlobal('ResizeObserver', class { observe() {} disconnect() {} })
  let newest = row('arrival-one')
  const historical = { ...page(row('historical')), page: 2, total: 101, total_pages: 3 }
  mocks.listTraffic.mockReset().mockImplementation(async (filters: TrafficFilters) => filters.limit === 1 ? { ...page(newest), limit: 1, total: 999, total_pages: 999 } : historical)
  mocks.getTrafficScopes.mockResolvedValue({ items: [], has_more: false, next_cursor: null })
  const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/traffic', name: 'Traffic', component: TrafficWorkbench }] })
  await router.push('/traffic?page=2&flow=pinned&flow_project=project-a')
  await router.isReady()
  const wrapper = mount(TrafficWorkbench, { global: { plugins: [router], stubs: { EvidenceReader: Reader } } })
  unmount = () => wrapper.unmount()
  await flushPromises()
  const readerUid = wrapper.getComponent(Reader).vm.$.uid
  await vi.advanceTimersByTimeAsync(3000)
  await flushPromises()
  expect(mocks.listTraffic.mock.lastCall?.[0]).toMatchObject({ page: 1, limit: 1 })
  newest = { ...row('arrival-two'), received_at: '2026-10-22T12:00:01Z' }
  await vi.advanceTimersByTimeAsync(3000)
  await flushPromises()
  expect(wrapper.get('tbody').text()).toContain('/historical')
  expect(wrapper.get('tbody').text()).not.toContain('/arrival')
  expect(wrapper.get('.pagination__summary').text()).toContain('共 101 条')
  expect(wrapper.get('.pagination__position').text()).toBe('第 2/3 页')
  expect(wrapper.text()).toContain('有新流量 · 返回最新页')
  expect(wrapper.getComponent(Reader).vm.$.uid).toBe(readerUid)
  const failed = deferred<TrafficPage>()
  mocks.listTraffic.mockReturnValueOnce(failed.promise)
  await wrapper.get('[aria-label="下一页"]').trigger('click')
  await flushPromises()
  expect(wrapper.get('.pagination__position').text()).toBe('第 2/3 页')
  failed.resolve({ ...page(row('current-third')), page: 3, total: 151, total_pages: 4 })
  await flushPromises()
  expect(wrapper.get('.pagination__position').text()).toBe('第 3/4 页')
  expect(wrapper.get('tbody').text()).toContain('/current-third')
  expect(wrapper.getComponent(Reader).vm.$.uid).toBe(readerUid)
})

it('后台探测不禁用跳页，显式翻页取消探测并拒绝迟到结果', async () => {
  vi.useFakeTimers()
  vi.stubGlobal('ResizeObserver', class { observe() {} disconnect() {} })
  const historical = { ...page(row('historical')), page: 2, total: 151, total_pages: 4 }
  const probe = deferred<TrafficPage>()
  const next = deferred<TrafficPage>()
  mocks.listTraffic.mockReset().mockResolvedValueOnce(historical).mockReturnValueOnce(probe.promise).mockReturnValueOnce(next.promise)
  mocks.getTrafficScopes.mockResolvedValue({ items: [], has_more: false, next_cursor: null })
  const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/traffic', name: 'Traffic', component: TrafficWorkbench }] })
  await router.push('/traffic?page=2&flow=pinned&flow_project=project-a')
  await router.isReady()
  const wrapper = mount(TrafficWorkbench, { attachTo: document.body, global: { plugins: [router], stubs: { EvidenceReader: Reader } } })
  unmount = () => wrapper.unmount()
  await flushPromises()
  const input = wrapper.get<HTMLInputElement>('.pagination__jump input')
  await input.setValue('3')
  input.element.focus()
  await vi.advanceTimersByTimeAsync(3000)
  await flushPromises()
  const probeSignal = mocks.listTraffic.mock.lastCall?.[2] as AbortSignal
  expect(input.element.disabled).toBe(false)
  expect(document.activeElement).toBe(input.element)
  expect(mocks.listTraffic.mock.lastCall?.[0]).toMatchObject({ page: 1, limit: 1 })
  await vi.advanceTimersByTimeAsync(6000)
  expect(mocks.listTraffic).toHaveBeenCalledTimes(2)
  await wrapper.get('.pagination__jump').trigger('submit')
  await flushPromises()
  expect(probeSignal.aborted).toBe(true)
  expect(mocks.listTraffic.mock.lastCall?.[0]).toMatchObject({ page: 3, limit: 50 })
  expect(input.element.disabled).toBe(true)
  expect(wrapper.get('.pagination__position').text()).toBe('第 2/4 页')
  next.resolve({ ...page(row('third')), page: 3, total: 151, total_pages: 4 })
  await flushPromises()
  probe.resolve({ ...page(row('late')), limit: 1, total: 999, total_pages: 999 })
  await flushPromises()
  expect(wrapper.get('.pagination__position').text()).toBe('第 3/4 页')
  expect(wrapper.get('tbody').text()).toContain('/third')
  expect(wrapper.get('.pagination__summary').text()).toContain('共 151 条')
  expect(wrapper.get('[data-testid="reader"]').text()).toContain('pinned')
})

it('旧摘要cursor明确失效；页码重挂载恢复、越界收敛和零结果不显示1/0', async () => {
  vi.useFakeTimers()
  vi.stubGlobal('ResizeObserver', class { observe() {} disconnect() {} })
  mocks.getTrafficScopes.mockResolvedValue({ items: [], has_more: false, next_cursor: null })
  mocks.listTraffic.mockReset().mockImplementation(async (filters: TrafficFilters) => ({ ...page(row('first')), page: Math.min(filters.page || 1, 2), total: 51, total_pages: 2 }))
  const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/traffic', name: 'Traffic', component: TrafficWorkbench }] })
  await router.push('/traffic?cursor=external&flow=pinned&flow_project=project-a')
  await router.isReady()
  const visible = ref(true)
  const host = defineComponent({ setup: () => () => visible.value ? h(TrafficWorkbench) : h('div') })
  const wrapper = mount(host, { global: { plugins: [router], stubs: { EvidenceReader: Reader } } })
  unmount = () => wrapper.unmount()
  await flushPromises()
  expect(wrapper.text()).toContain('原分页定位已失效，已返回第 1 页。')
  expect(router.currentRoute.value.query.cursor).toBeUndefined()
  expect(mocks.listTraffic).toHaveBeenCalledTimes(1)
  expect(wrapper.get('.pagination__position').text()).toBe('第 1/2 页')
  await wrapper.get('[aria-label="下一页"]').trigger('click')
  await flushPromises()
  expect(router.currentRoute.value.query.page).toBe('2')
  visible.value = false
  await flushPromises()
  visible.value = true
  await flushPromises()
  expect(wrapper.get('.pagination__position').text()).toBe('第 2/2 页')
  await router.push({ query: { ...router.currentRoute.value.query, page: '99' } })
  await flushPromises()
  expect(router.currentRoute.value.query.page).toBe('2')
  expect(wrapper.text()).toContain('结果页数已变化，已定位到第 2 页。')
  expect(wrapper.get('[data-testid="reader"]').text()).toContain('pinned')
  mocks.listTraffic.mockResolvedValue(page())
  await wrapper.get('.traffic-toolbar').findAll('button').find(button => button.text() === '刷新')!.trigger('click')
  await flushPromises()
  expect(router.currentRoute.value.query.page).toBeUndefined()
  expect(wrapper.get('.pagination').text()).toContain('共 0 条 · 共 0 页')
  expect(wrapper.get('.pagination').text()).not.toContain('1/0')
  expect(wrapper.get('[aria-label="下一页"]').attributes('disabled')).toBeDefined()
  expect(wrapper.get('[data-testid="reader"]').text()).toContain('pinned')
})

it('搜索第二页与查询上下文一起恢复；整页刷新丢失上下文时请求前回到最新页', async () => {
  vi.useFakeTimers()
  vi.stubGlobal('ResizeObserver', class { observe() {} disconnect() {} })
  mocks.listTraffic.mockReset().mockImplementation(async (filters: TrafficFilters) => ({ ...page(row(filters.page === 2 ? 'second' : 'first')), page: filters.page || 1, total: 101, total_pages: 3 }))
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
  const button = (label: string) => wrapper.findAll('button').find((entry) => entry.text() === label || entry.attributes('aria-label') === label)!
  // 从历史页切入新搜索时，页码与搜索身份同时回到首页。
  await button('下一页').trigger('click')
  await flushPromises()
  await wrapper.get('input[aria-label="搜索 URL、Host 或路径"]').setValue(search)
  await wrapper.get('form').trigger('submit')
  await flushPromises()
  await button('下一页').trigger('click')
  await flushPromises()
  const searchPage = router.currentRoute.value.query.page
  const readerUid = wrapper.getComponent(Reader).vm.$.uid
  await button('刷新').trigger('click')
  await flushPromises()
  expect(wrapper.getComponent(Reader).vm.$.uid).toBe(readerUid)
  expect(wrapper.get('.pagination__position').text()).toBe('第 2/3 页')
  visible.value = false
  await flushPromises()
  visible.value = true
  await flushPromises()
  expect(router.currentRoute.value.query.page).toBe(searchPage)
  expect(wrapper.get<HTMLInputElement>('input[aria-label="搜索 URL、Host 或路径"]').element.value).toBe(search)
  expect(wrapper.get('.pagination__position').text()).toBe('第 2/3 页')
  expect(wrapper.get('[data-testid="reader"]').text()).toContain('project-a / pinned')
  expect(mocks.listTraffic.mock.lastCall?.[0].url).toBe(search)
  const location = router.currentRoute.value.fullPath
  const history = { ...router.options.history.state }
  expect(location).not.toContain('secret')
  expect(JSON.stringify(history)).not.toContain('secret')
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
  expect(mocks.listTraffic.mock.calls[0]![0]).toMatchObject({ page: 1, url: undefined })
  expect(reloadedRouter.currentRoute.value.query.page).toBeUndefined()
  expect(reloadedRouter.currentRoute.value.query.search_context).toBeUndefined()
  expect(reloaded.text()).toContain('搜索上下文已失效，已返回第 1 页。')
  expect(reloaded.get('.pagination__position').text()).toBe('第 1/3 页')
  expect(reloaded.get('[data-testid="reader"]').text()).toContain('project-a / pinned')
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
  expect(wrapper.find('.traffic-scope-summary').exists()).toBe(false)
  await wrapper.findAll('button').find((button) => button.text() === '采集信息')!.trigger('click')
  await flushPromises()
  for (const text of ['正在采集', '等待终止确认', '采集已停止，正在同步已有流量', '同步完成', '同步失败，正在重试']) expect(overlay().get('.traffic-scope-summary').text()).toContain(text)
  const details = overlay().findAll('.traffic-metadata details')
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
