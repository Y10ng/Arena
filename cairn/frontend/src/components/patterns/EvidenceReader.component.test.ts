import { flushPromises, mount } from '@vue/test-utils'
import { defineComponent, h, ref } from 'vue'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import EvidenceReader from './EvidenceReader.vue'
import { DropdownMenu, Select } from '@/components/ui'
import type { EvidenceItem, TrafficFlowDetail, TrafficProjection, TrafficReadable, TrafficSide } from '@/api/types'

const api = vi.hoisted(() => ({ getTrafficFlow: vi.fn(), getTrafficReadable: vi.fn(), getTrafficProjection: vi.fn(), getTrafficBodyPreview: vi.fn() }))
vi.mock('@/api/client', async (original) => ({ ...await original<typeof import('@/api/client')>(), ...api }))
const Editor = defineComponent({
  props: ['modelValue', 'readingState', 'readingView', 'variant'],
  setup(_props, { expose }) {
    const textarea = ref<HTMLTextAreaElement>()
    const selectTextRange = vi.fn((_start: number, _end: number) => { textarea.value!.scrollTop = 0 })
    expose({ selectTextRange })
    return { textarea, selectTextRange }
  },
  render() {
    return h('div', { class: 'monaco-reader' }, h('textarea', { ref: 'textarea', readonly: true, value: this.modelValue }))
  },
})
const scope = { id: 'scope', project_id: 'project', task_run_id: 'run', generation: 1, container_id: 'container' }
const detail = (): TrafficFlowDetail => ({
  id: 'flow', project_id: 'project', scope, revision: 1, state: 'complete', source_state: 'complete',
  header_text: { request: 'GET /synthetic HTTP/1.1\r\n\r\n', response: 'HTTP/1.1 200 OK\r\n\r\n' },
  sync: { bodies: {
    request: { status: 'available', artifact_id: 'request', revision: 1, accepted_bytes: 0, captured_bytes: 0 },
    response: { status: 'available', artifact_id: 'response', revision: 1, accepted_bytes: 18, captured_bytes: 18 },
  } },
})
function projection(side: TrafficSide, offset = 0): TrafficProjection {
  const text = side === 'request' ? 'GET /synthetic HTTP/1.1\r\nHost: synthetic.test\r\n\r\n' : 'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n\r\n{"synthetic":true}'
  return { project_id: 'project', flow_id: 'flow', scope, revision: 1, state: 'complete', side, representation: 'http-evidence-v1', sha256: 'a'.repeat(64), body_sha256: 'b'.repeat(64), body_artifact_id: side, body_display: 'utf8', header_size: 48, size: text.length, offset, next_offset: text.length, has_more: false, text }
}
function readable(side: TrafficSide): TrafficReadable {
  const raw = projection(side)
  const end = raw.text.indexOf('\r\n\r\n') + 4
  const body = raw.text.slice(end)
  return { project_id: raw.project_id, flow_id: raw.flow_id, side, revision: raw.revision,
    representation: 'http-readable', excerpt_eligible: false, header_text: raw.text.slice(0, end), body_text: body,
    read_mode: 'auto', deferred_reason: null, body_bytes: body.length,
    decode_status: 'complete', decoded: false, text_escaped: false, display_truncated: false, content_encodings: [], decoded_body_bytes: body.length, body_limit: 10485760,
    body_artifact_id: raw.body_artifact_id, body_sha256: raw.body_sha256 }
}
const flowItem: EvidenceItem = { id: 'flow-item', kind: 'http', flow_id: 'flow', title: '合成主证据', role: 'primary', source: 'capture' }
function inlineItem(id: string, role: 'primary' | 'supporting' = 'primary'): EvidenceItem {
  return { id, kind: 'http', title: id === 'first' ? '基线' : '对照', role, source: 'manual', request_text: 'GET /synthetic HTTP/1.1\r\n\r\n', response_text: 'HTTP/1.1 200 OK\r\n\r\n' }
}
function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>((done) => { resolve = done })
  return { promise, resolve }
}
let wrapper: ReturnType<typeof mount<typeof EvidenceReader>> | undefined
let notifyResize: () => void
const scrollIntoView = vi.fn()
beforeEach(() => {
  vi.stubGlobal('ResizeObserver', class {
    callback: () => void
    constructor(callback: () => void) { this.callback = callback }
    observe(element: HTMLElement) { if (element.classList.contains('evidence-reader')) notifyResize = this.callback }
    unobserve() {}
    disconnect() {}
  })
  HTMLElement.prototype.scrollIntoView = scrollIntoView
  api.getTrafficFlow.mockResolvedValue(detail())
  api.getTrafficReadable.mockImplementation(async (_project: string, _flow: string, side: TrafficSide) => readable(side))
  api.getTrafficProjection.mockImplementation(async (_project: string, _flow: string, side: TrafficSide, offset: number) => projection(side, offset))
})
afterEach(() => {
  wrapper?.unmount(); wrapper = undefined
  vi.restoreAllMocks(); vi.clearAllMocks(); vi.unstubAllGlobals()
})
function render(items: EvidenceItem[] = [flowItem], slots = {}) {
  wrapper = mount(EvidenceReader, {
    attachTo: document.body,
    props: { entityKey: 'entity', projectId: 'project', items, autoRefresh: false, loadPreview: vi.fn(), originalUrl: () => null },
    slots,
    global: { stubs: { MonacoEditor: Editor } },
  })
  return wrapper
}

it('成对放大通知父工作区，已处理的 Esc 不收起，收起保留编辑器并回焦', async () => {
  const reader = render()
  await reader.setProps({ fillHeight: true })
  await flushPromises()
  const editor = reader.getComponent(Editor).vm
  editor.$emit('overflow-change', true)
  await flushPromises()
  await reader.get('[aria-label="放大成对报文"]').trigger('click')
  expect(reader.emitted('expandedChange')?.at(-1)).toEqual([true])
  const handled = new KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true })
  handled.preventDefault()
  reader.element.dispatchEvent(handled)
  await flushPromises()
  expect(reader.find('[aria-label="收起成对报文"]').exists()).toBe(true)
  editor.$emit('overflow-change', false)
  await flushPromises()
  await reader.trigger('keydown', { key: 'Escape' })
  await flushPromises()
  expect(reader.emitted('expandedChange')?.at(-1)).toEqual([false])
  expect(reader.getComponent(Editor).vm.selectTextRange).toBe(editor.selectTextRange)
  expect(document.activeElement).toBe(reader.get('[aria-label="放大成对报文"]').element)
})

it('公共头整合父插槽、采集状态和可聚焦重建说明，诊断不重复占据正文', async () => {
  const reader = render([flowItem], { 'header-context': '<span>当前选择 · 页外</span>', 'header-actions': '<button>详情</button>' })
  await flushPromises()
  expect(reader.findAll('.evidence-reader__summary')).toHaveLength(1)
  expect(reader.get('.evidence-reader__summary').text()).toContain('当前选择 · 页外')
  expect(reader.get('.evidence-reader__summary').text()).toContain('完整')
  expect(reader.get('.evidence-reader__summary').text()).not.toContain('HTTP 证据')
  expect(reader.findAll('.evidence-reader__notice')).toHaveLength(0)
  expect(reader.text()).not.toContain('revision')
  expect(reader.text()).not.toContain('SHA-256')
  expect(reader.findAllComponents(Editor).every((editor) => editor.props('variant') === 'http')).toBe(true)
  const info = reader.get('button[aria-label="结构化重建说明"]')
  ;(info.element as HTMLButtonElement).focus()
  expect(document.activeElement).toBe(info.element)
  await info.trigger('click')
  await flushPromises()
  expect(document.body.textContent).toContain('结构化重建，非原始字节流。')
  expect(document.body.textContent?.match(/请求阅读来源/g)).toHaveLength(1)
  expect(document.body.textContent?.match(/响应阅读来源/g)).toHaveLength(1)
  expect(reader.findAll('.evidence-reader__content').every((body) => !body.text().includes('SHA-256'))).toBe(true)
})

it('非HTTP和空选择也保留公共头插槽，普通文本使用原件阅读器', async () => {
  const reader = render([{ id: 'text', kind: 'text', title: '记录', role: 'primary', source: 'manual', text: '合成记录' }], {
    'header-context': '<span>证据上下文</span>', 'header-actions': '<button>返回步骤</button>',
  })
  await reader.setProps({ presentation: 'step' })
  await flushPromises()
  expect(reader.get('.evidence-reader__header').text()).toBe('证据上下文返回步骤')
  expect(reader.getComponent(Editor).props('variant')).toBe('packet')
  await reader.setProps({ items: [] })
  expect(reader.get('.evidence-reader__header').text()).toContain('返回步骤')
  expect(reader.text()).toContain('未记录关键证据')
})

it('多交互显示角色标题和位置，前后导航与选择菜单使用同一选择事件', async () => {
  const reader = render([inlineItem('first'), inlineItem('second', 'supporting')])
  await flushPromises()
  expect(reader.get('.evidence-reader__position').text()).toBe('1/2')
  expect(reader.getComponent(Select).props('options')).toEqual([{ value: 'first', label: '主证据 · 基线' }, { value: 'second', label: '补充 · 对照' }])
  expect(reader.get('[aria-label="上一交互"]').attributes('disabled')).toBeDefined()
  await reader.get('[aria-label="下一交互"]').trigger('click')
  expect(reader.get('.evidence-reader__position').text()).toBe('2/2')
  expect(reader.emitted('update:selectedItemId')?.at(-1)).toEqual(['second'])
  reader.getComponent(Select).vm.$emit('update:modelValue', 'first')
  await flushPromises()
  expect(reader.emitted('update:selectedItemId')?.at(-1)).toEqual(['first'])
  expect(reader.get('[aria-label="上一交互"]').attributes('disabled')).toBeDefined()
})

it('正文定位等待异步数据及编辑器就绪，聚焦正文而非外层标题且不重读', async () => {
  const request = deferred<TrafficReadable>()
  const response = deferred<TrafficReadable>()
  api.getTrafficReadable.mockImplementation((_project: string, _flow: string, side: TrafficSide) => side === 'request' ? request.promise : response.promise)
  const reader = render()
  reader.vm.revealBody('flow-item')
  await flushPromises()
  expect(scrollIntoView).not.toHaveBeenCalled()
  request.resolve(readable('request'))
  await flushPromises()
  expect(scrollIntoView).not.toHaveBeenCalled()
  response.resolve(readable('response'))
  await flushPromises()
  expect(scrollIntoView).toHaveBeenCalledExactlyOnceWith({ block: 'start', behavior: 'auto' })
  expect(document.activeElement).toBe(reader.get('[data-evidence-body="request"]').element)
  for (const editor of reader.findAllComponents(Editor)) expect(editor.vm.selectTextRange).toHaveBeenCalledExactlyOnceWith(0, 0)
  expect(api.getTrafficReadable).toHaveBeenCalledTimes(2)
  expect(reader.getComponent(Editor).props('readingView')).toBe('readable')
})

it.each(['raw', 'pretty', 'hex'] as const)('返回后重开同一证据双侧回首，保留 %s、分页和实例且只定位外层一次', async (mode) => {
  api.getTrafficProjection.mockImplementation(async (_project: string, _flow: string, side: TrafficSide, offset: number) => ({ ...projection(side, offset), next_offset: offset + 64, size: 256, has_more: true }))
  api.getTrafficBodyPreview.mockImplementation(async (_project: string, _flow: string, side: TrafficSide, offset: number) => ({ side, revision: 1, offset, next_offset: offset + 64, accepted_bytes: 256, has_more: true, sealed: true, content_base64: btoa('synthetic body') }))
  const reader = render()
  await flushPromises()
  const responsePanel = reader.get('article[aria-label="响应阅读器"]')
  responsePanel.getComponent(DropdownMenu).vm.$emit('select', 'raw')
  await flushPromises()
  responsePanel.getComponent(DropdownMenu).vm.$emit('select', mode)
  await flushPromises()
  await responsePanel.findAll('button').find((button) => button.text() === '下一页')!.trigger('click')
  await flushPromises()
  const editors = reader.findAllComponents(Editor)
  const readingStates = editors.map((editor) => editor.props('readingState'))
  const bodies = reader.findAll<HTMLElement>('[data-evidence-body]')
  const focus = vi.spyOn(bodies[0]!.element, 'focus')
  reader.vm.revealBody('flow-item')
  await flushPromises()
  const projectionReads = api.getTrafficProjection.mock.calls.length
  const bodyReads = api.getTrafficBodyPreview.mock.calls.length
  editors.forEach((editor, index) => { editor.get('textarea').element.scrollTop = (index + 1) * 100 })
  bodies.forEach((body) => { body.element.scrollTop = 80 })
  reader.vm.cancelReveal()
  await reader.vm.refreshFlow()
  await flushPromises()
  expect(editors.map((editor) => editor.get('textarea').element.scrollTop)).toEqual([100, 200])
  expect(scrollIntoView).toHaveBeenCalledTimes(1)
  expect(focus).toHaveBeenCalledTimes(1)
  reader.vm.revealBody('flow-item')
  await flushPromises()
  expect(editors.map((editor) => editor.get('textarea').element.scrollTop)).toEqual([0, 0])
  expect(bodies.map((body) => body.element.scrollTop)).toEqual([0, 0])
  expect(scrollIntoView).toHaveBeenCalledTimes(2)
  expect(focus).toHaveBeenCalledTimes(2)
  expect(focus).toHaveBeenLastCalledWith({ preventScroll: true })
  expect(document.activeElement).toBe(bodies[0]!.element)
  reader.findAllComponents(Editor).forEach((editor, index) => {
    expect(editor.vm.$ === editors[index]!.vm.$).toBe(true)
    expect(editor.props('readingState')).toBe(readingStates[index])
    expect(editor.vm.selectTextRange.mock.calls).toEqual([[0, 0], [0, 0]])
  })
  expect(responsePanel.getComponent(Editor).props('readingView')).toBe(mode === 'pretty' ? 'raw-pretty' : mode)
  expect(responsePanel.get('.evidence-reader__pagination').text()).toContain(mode === 'hex' ? '正文 64–128' : 'UTF-8 字节 64–128')
  expect(api.getTrafficProjection).toHaveBeenCalledTimes(projectionReads)
  expect(api.getTrafficBodyPreview).toHaveBeenCalledTimes(bodyReads)
})

it.each(['absent', 'error'] as const)('响应 %s 时仍定位已有请求，不等待不存在的编辑器', async (state) => {
  if (state === 'absent') {
    const missing = detail()
    missing.sync.bodies.response.status = 'absent'
    api.getTrafficFlow.mockResolvedValue(missing)
  } else {
    api.getTrafficReadable.mockImplementation(async (_project: string, _flow: string, side: TrafficSide) => {
      if (side === 'response') throw new Error('合成响应读取失败')
      return readable(side)
    })
  }
  const reader = render()
  reader.vm.revealBody('flow-item')
  await flushPromises()
  expect(reader.findAllComponents(Editor)).toHaveLength(1)
  expect(reader.getComponent(Editor).vm.selectTextRange).toHaveBeenCalledExactlyOnceWith(0, 0)
  expect(scrollIntoView).toHaveBeenCalledExactlyOnceWith({ block: 'start', behavior: 'auto' })
  expect(reader.text()).toContain(state === 'absent' ? '未记录响应' : '合成响应读取失败')
})

it('返回步骤取消待定位但不取消正文加载，迟到编辑器不会重新滚动', async () => {
  const pending = deferred<TrafficReadable>()
  api.getTrafficReadable.mockImplementation(async (_project: string, _flow: string, side: TrafficSide) => { await pending.promise; return readable(side) })
  const reader = render()
  reader.vm.revealBody('flow-item')
  await flushPromises()
  reader.vm.cancelReveal()
  expect(api.getTrafficReadable.mock.calls[0]?.[3].aborted).toBe(false)
  pending.resolve(readable('request'))
  await flushPromises()
  expect(scrollIntoView).not.toHaveBeenCalled()
  expect(reader.findAllComponents(Editor)).toHaveLength(2)
})

it('切换交互取消迟到定位，不让旧流量抢走新正文和焦点', async () => {
  const old = deferred<TrafficReadable>()
  api.getTrafficReadable.mockReturnValue(old.promise)
  const reader = render([flowItem, inlineItem('second')])
  reader.vm.revealBody('flow-item')
  await flushPromises()
  await reader.setProps({ selectedItemId: 'second' })
  old.resolve(readable('request'))
  await flushPromises()
  expect(scrollIntoView).not.toHaveBeenCalled()
  expect(reader.get('textarea').element.value).toContain('/synthetic')
  reader.vm.revealBody('second')
  await flushPromises()
  expect(scrollIntoView).toHaveBeenCalledTimes(1)
  expect(api.getTrafficReadable).toHaveBeenCalledTimes(2)
})

it('来源身份切换取消同名证据的待定位，迟到正文不污染新来源', async () => {
  const old = deferred<TrafficReadable>()
  api.getTrafficReadable.mockReturnValue(old.promise)
  const reader = render()
  reader.vm.revealBody('flow-item')
  await flushPromises()
  await reader.setProps({ entityKey: 'other-observation', items: [inlineItem('flow-item')] })
  old.resolve(readable('response'))
  await flushPromises()
  expect(scrollIntoView).not.toHaveBeenCalled()
  expect(reader.text()).toContain('人工记录')
  for (const editor of reader.findAllComponents(Editor)) expect(editor.vm.selectTextRange).not.toHaveBeenCalled()
  reader.vm.revealBody('flow-item')
  await flushPromises()
  for (const editor of reader.findAllComponents(Editor)) expect(editor.vm.selectTextRange).toHaveBeenCalledExactlyOnceWith(0, 0)
  expect(api.getTrafficReadable).toHaveBeenCalledTimes(2)
})

it('按真实分栏宽度收纳模式到更多，保留Pretty、Hex、换行和复制原文', async () => {
  const writeText = vi.fn().mockResolvedValue(undefined)
  vi.stubGlobal('navigator', { clipboard: { writeText } })
  const reader = render()
  await flushPromises()
  const panelContainer = reader.get('.evidence-reader__panels').element
  vi.spyOn(panelContainer, 'getBoundingClientRect').mockReturnValue({ width: 720, top: 0 } as DOMRect)
  notifyResize()
  await flushPromises()
  const responsePanel = reader.get('article[aria-label="响应阅读器"]')
  expect(responsePanel.findAll('button').some((button) => button.text() === 'Pretty')).toBe(false)
  const menu = responsePanel.getComponent(DropdownMenu)
  expect(menu.props('items').map((item) => item.value)).toEqual(['readable', 'raw', 'pretty', 'hex', 'wrap'])
  menu.vm.$emit('select', 'pretty')
  await flushPromises()
  expect(responsePanel.get('.evidence-reader__current-mode').text()).toBe('Pretty')
  await responsePanel.get('button[aria-label="响应复制正文视图"]').trigger('click')
  await flushPromises()
  expect(writeText).toHaveBeenCalledExactlyOnceWith(projection('response').text)
  menu.vm.$emit('select', 'wrap')
  await flushPromises()
  expect(responsePanel.getComponent(Editor).props('readingState').wrap).toBe(false)
  expect(api.getTrafficReadable).toHaveBeenCalledTimes(2)
  vi.spyOn(panelContainer, 'getBoundingClientRect').mockReturnValue({ width: 1100, top: 0 } as DOMRect)
  notifyResize()
  await flushPromises()
  expect(responsePanel.findAll('button').some((button) => button.text() === 'Pretty')).toBe(true)
  expect(responsePanel.getComponent(Editor).props('readingView')).toBe('readable-pretty')
})

it('小gzip默认可读并紧凑标注已解压，Pretty保留原头且已有选段切回v1投影', async () => {
  const response = { ...readable('response'), decoded: true, content_encodings: ['gzip'],
    header_text: 'HTTP/1.1 200 OK\r\nContent-Encoding: gzip\r\nContent-Length: 38\r\n\r\n' }
  api.getTrafficReadable.mockImplementation(async (_project, _flow, side) => side === 'response' ? response : readable(side))
  const excerpt = { side: 'response' as const, pointer: 'utf8-bytes=0:4', representation: 'http-evidence-v1' as const, revision: 1, sha256: 'a'.repeat(64) }
  const reader = render([{ ...flowItem, excerpts: [excerpt] } as EvidenceItem])
  await flushPromises()
  const panel = reader.get('article[aria-label="响应阅读器"]')
  expect(panel.text()).toContain('已解压')
  expect(panel.getComponent(Editor).props('modelValue')).toBe(response.header_text + response.body_text)
  expect(panel.findAll('.evidence-reader__notice')).toHaveLength(0)
  expect(api.getTrafficProjection).not.toHaveBeenCalled()
  panel.getComponent(DropdownMenu).vm.$emit('select', 'pretty')
  await flushPromises()
  expect(panel.getComponent(Editor).props('modelValue')).toBe(response.header_text + '{\n  "synthetic": true\n}')
  await panel.get('[aria-label="响应已保存选段"] button').trigger('click')
  await flushPromises()
  expect(panel.getComponent(Editor).props('readingView')).toBe('raw')
  expect(panel.getComponent(Editor).props('modelValue')).toBe(projection('response').text)
  expect(api.getTrafficProjection).toHaveBeenCalledWith('project', 'flow', 'response', 0, 65536, expect.any(AbortSignal))
  expect(panel.getComponent(Editor).vm.selectTextRange).toHaveBeenCalledWith(0, 4)
  expect(reader.emitted()).not.toHaveProperty('excerpt')
  panel.getComponent(DropdownMenu).vm.$emit('select', 'readable')
  await flushPromises()
  expect(panel.getComponent(Editor).props('modelValue')).toBe(response.header_text + response.body_text)
})

it('大正文默认只有头部，搜索和模式不取正文，显式加载10 MiB后同版刷新不重读，新来源重新延期', async () => {
  const large = detail()
  large.sync.bodies.response.accepted_bytes = 10485760
  large.sync.bodies.response.captured_bytes = 10485760
  api.getTrafficFlow.mockResolvedValue(large)
  const response = { ...readable('response'), body_text: 'x'.repeat(10485760), decoded_body_bytes: 10485760 }
  api.getTrafficReadable.mockImplementation(async (_project, _flow, side) => side === 'response' ? response : readable(side))
  const reader = render()
  await flushPromises()
  const panel = reader.get('article[aria-label="响应阅读器"]')
  expect(api.getTrafficReadable).toHaveBeenCalledTimes(1)
  expect(panel.getComponent(Editor).props('modelValue')).toBe(large.header_text.response)
  expect(panel.text()).toContain('已保存 10.00 MiB')
  expect(panel.find('[aria-label="响应复制已载入头部"]').exists()).toBe(true)
  await panel.get('[aria-label="响应搜索"]').trigger('click')
  panel.getComponent(DropdownMenu).vm.$emit('select', 'raw')
  await flushPromises()
  expect(api.getTrafficProjection).not.toHaveBeenCalled()
  expect(api.getTrafficReadable).toHaveBeenCalledTimes(1)
  await panel.findAll('button').find((button) => button.text() === '加载响应正文')!.trigger('click')
  await flushPromises()
  const editor = panel.getComponent(Editor)
  expect(editor.props('modelValue').length).toBe(response.header_text.length + 10485760)
  expect(api.getTrafficReadable).toHaveBeenLastCalledWith('project', 'flow', 'response', expect.any(AbortSignal), 'manual')
  await reader.vm.refreshFlow()
  await flushPromises()
  expect(panel.getComponent(Editor).vm.$).toBe(editor.vm.$)
  expect(api.getTrafficReadable).toHaveBeenCalledTimes(2)
  await reader.setProps({ entityKey: 'other-observation' })
  await flushPromises()
  expect(reader.get('article[aria-label="响应阅读器"]').text()).toContain('加载响应正文')
  expect(api.getTrafficReadable.mock.calls.filter((call) => call[2] === 'response')).toHaveLength(1)
})

it('小原件自动探测膨胀后延期，点击失败可重试且取消后迟到响应不覆盖头部', async () => {
  api.getTrafficReadable.mockImplementation(async (_project, _flow, side) => side === 'response' ? { ...readable(side), body_text: '', decode_status: 'deferred', deferred_reason: 'display-size' } : readable(side))
  const reader = render()
  await flushPromises()
  const panel = reader.get('article[aria-label="响应阅读器"]')
  expect(panel.text()).toContain('加载响应正文')
  expect(api.getTrafficReadable).toHaveBeenLastCalledWith('project', 'flow', 'response', expect.any(AbortSignal), 'auto')
  api.getTrafficReadable.mockRejectedValueOnce(new Error('合成读取失败'))
  await panel.findAll('button').find((button) => button.text() === '加载响应正文')!.trigger('click')
  await flushPromises()
  expect(panel.text()).toContain('合成读取失败')
  const pending = deferred<TrafficReadable>()
  api.getTrafficReadable.mockReturnValueOnce(pending.promise)
  await panel.findAll('button').find((button) => button.text() === '重试')!.trigger('click')
  const signal = api.getTrafficReadable.mock.calls.at(-1)![3] as AbortSignal
  await panel.findAll('button').find((button) => button.text() === '取消读取')!.trigger('click')
  expect(signal.aborted).toBe(true)
  pending.resolve(readable('response'))
  await flushPromises()
  expect(panel.text()).toContain('加载响应正文')
  expect(panel.getComponent(Editor).props('modelValue')).toBe(detail().header_text.response)
})

it('未知或大Artifact不预取，显式读取仍走调用方专用权限并在卸载取消', async () => {
  const loadPreview = vi.fn((_reference: unknown, _signal: AbortSignal) => new Promise<never>(() => {}))
  const reader = render([{ id: 'artifact-item', kind: 'http', title: '专用材料', role: 'primary', source: 'capture', request: { artifact_id: 'private-request' } }])
  await reader.setProps({ loadPreview, artifactSize: () => 1024 * 1024 })
  await flushPromises()
  expect(loadPreview).not.toHaveBeenCalled()
  const panel = reader.get('article[aria-label="请求阅读器"]')
  expect(panel.findComponent(Editor).exists()).toBe(false)
  await panel.findAll('button').find((button) => button.text() === '加载请求正文')!.trigger('click')
  const signal = loadPreview.mock.calls[0]![1] as AbortSignal
  reader.unmount(); wrapper = undefined
  expect(signal.aborted).toBe(true)
})

it.each(['invalid-encoding', 'unsupported-encoding', 'non-text'] as const)('%s明确可见而不是误报大小限制，Raw/Hex及下载入口保留', async (decode_status) => {
  api.getTrafficReadable.mockImplementation(async (_project, _flow, side) => ({ ...readable(side), decode_status, body_text: '' }))
  const reader = render()
  await flushPromises()
  const panel = reader.get('article[aria-label="响应阅读器"]')
  expect(panel.find('.evidence-reader__notice').exists()).toBe(true)
  expect(panel.text()).not.toContain('超过 10 MiB')
  expect(panel.findAll('button').map((button) => button.text())).toContain('投影')
  expect(panel.findAll('button').map((button) => button.text())).toContain('Hex')
  expect(panel.findAll('button').map((button) => button.text())).not.toContain('Pretty')
  expect(panel.find('a[download]').exists()).toBe(false)
  await panel.get('button[aria-label="响应更多"]').trigger('click')
  await flushPromises()
  expect(document.body.querySelector('a[download]')?.getAttribute('href')).toContain('/bodies/response')
})

it('异常字节保留有效正文并明确复制转义文本，不向Pretty传递假JSON', async () => {
  const response = { ...readable('response'), text_escaped: true, body_text: '{"text":"有效正文\\xff\\u0000"}' }
  api.getTrafficReadable.mockImplementation(async (_project, _flow, side) => side === 'response' ? response : readable(side))
  const writeText = vi.fn().mockResolvedValue(undefined)
  vi.stubGlobal('navigator', { clipboard: { writeText } })
  const reader = render()
  await flushPromises()
  const panel = reader.get('article[aria-label="响应阅读器"]')
  expect(panel.getComponent(Editor).props('modelValue')).toBe(response.header_text + response.body_text)
  expect(panel.text()).toContain('转义')
  expect(panel.text()).not.toContain('安全转义视图')
  expect(panel.findAll('.evidence-reader__notice')).toHaveLength(0)
  expect(panel.findAll('button').some((button) => button.text() === 'Pretty')).toBe(false)
  await panel.get('[aria-label="响应复制转义文本"]').trigger('click')
  await flushPromises()
  expect(writeText).toHaveBeenCalledWith(response.header_text + response.body_text)
  expect(panel.text()).toContain('已复制转义文本')
})

it('failed、未同步、缺侧及截断均直接可见，不隐藏到结构化说明', async () => {
  const failed = detail()
  failed.state = 'failed'; failed.source_state = 'failed'
  failed.sync.bodies.request.status = 'pending'; failed.sync.bodies.response.status = 'absent'
  api.getTrafficFlow.mockResolvedValue(failed)
  api.getTrafficReadable.mockResolvedValue({ ...readable('request'), decode_status: 'pending' })
  const reader = render()
  await flushPromises()
  expect(reader.get('.evidence-reader__summary').text()).toContain('采集失败')
  expect(reader.get('.evidence-reader__summary').text()).not.toContain('预览受限')
  expect(reader.text()).toContain('正文尚未封存')
  expect(reader.text()).toContain('请求正文尚未同步')
  expect(reader.text()).toContain('未记录响应')
})
