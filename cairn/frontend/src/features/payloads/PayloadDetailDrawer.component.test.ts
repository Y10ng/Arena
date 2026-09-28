import { flushPromises, mount } from '@vue/test-utils'
import { ref } from 'vue'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { InjectionPayloadRecord } from '@/api/types'
import PayloadDetailDrawer from './PayloadDetailDrawer.vue'

const mocks = vi.hoisted(() => ({
  clipboardWrite: vi.fn(),
  push: vi.fn(),
  refetch: vi.fn(),
  showToast: vi.fn(),
  queryData: null as unknown,
  queryError: null as Error | null,
  queryIsError: false,
}))

vi.mock('@tanstack/vue-query', () => ({
  useQuery: () => ({
    data: ref(mocks.queryData),
    error: ref(mocks.queryError),
    isError: ref(mocks.queryIsError),
    refetch: mocks.refetch,
  }),
}))

vi.mock('vue-router', () => ({
  useRouter: () => ({ push: mocks.push }),
}))

vi.mock('@/stores/ui', () => ({
  useUIStore: () => ({ showToast: mocks.showToast }),
}))

vi.mock('@/components/MonacoEditor.vue', () => ({
  default: {
    name: 'MonacoEditor',
    props: ['modelValue', 'language', 'readOnly', 'ariaLabel'],
    template: '<pre class="monaco-stub" :aria-label="ariaLabel">{{ modelValue }}</pre>',
  },
}))

vi.mock('@/components/ui/Drawer.vue', () => ({
  default: {
    name: 'Drawer',
    props: ['open', 'title', 'description'],
    emits: ['update:open'],
    template:
      '<section v-if="open"><header><slot name="title" /></header><slot /><footer><slot name="footer" /></footer></section>',
  },
}))

const record: InjectionPayloadRecord = {
  id: 'p001',
  project_id: 'project-a',
  task_run_id: 'tr001',
  phase: 'explore_execute',
  capture_id: 'pc_001',
  injection_type: '自定义 Agent 总线 / 消息体',
  payload_text: '  <script>alert("payload")</script>\n\n',
  original_format: 'Base64 解码后写入 message.content',
  response_status: 'returned',
  response_text: '\n<img src=x onerror=alert("response")>  ',
  response_format: 'Agent 聊天文本',
  created_at: '2026-09-03T00:00:00Z',
  updated_at: '2026-09-03T00:01:00Z',
}

beforeEach(() => {
  mocks.queryData = {
    projectId: 'project-a',
    payloadId: 'p001',
    visitGeneration: 0,
    record,
  }
  mocks.queryError = null
  mocks.queryIsError = false
  mocks.clipboardWrite.mockReset().mockResolvedValue(undefined)
  mocks.push.mockReset().mockResolvedValue(undefined)
  mocks.refetch.mockReset()
  mocks.showToast.mockReset()
  Object.defineProperty(navigator, 'clipboard', {
    configurable: true,
    value: { writeText: mocks.clipboardWrite },
  })
})

afterEach(() => {
  document.body.innerHTML = ''
})

describe('PayloadDetailDrawer', () => {
  it('renders HTML-shaped Payload and response as inert exact text', () => {
    const wrapper = mount(PayloadDetailDrawer, {
      props: { projectId: 'project-a', payloadId: 'p001', visitGeneration: 0, open: true },
      attachTo: document.body,
    })

    const editors = wrapper.findAllComponents({ name: 'MonacoEditor' })
    expect(editors).toHaveLength(2)
    expect(editors[0]?.props('modelValue')).toBe(record.payload_text)
    expect(editors[1]?.props('modelValue')).toBe(record.response_text)
    expect(wrapper.find('script').exists()).toBe(false)
    expect(wrapper.find('img').exists()).toBe(false)
  })

  it('distinguishes an explicit empty response from a pending response', () => {
    mocks.queryData = {
      projectId: 'project-a',
      payloadId: 'p001',
      visitGeneration: 0,
      record: { ...record, response_status: 'empty', response_text: '' },
    }
    const wrapper = mount(PayloadDetailDrawer, {
      props: { projectId: 'project-a', payloadId: 'p001', visitGeneration: 0, open: true },
    })

    expect(wrapper.findAllComponents({ name: 'MonacoEditor' })).toHaveLength(1)
    expect(wrapper.text()).toContain('目标明确返回了空响应')
    expect(wrapper.text()).not.toContain('目标响应尚未完成')
  })

  it('hides stale detail immediately when the project identity changes', async () => {
    const wrapper = mount(PayloadDetailDrawer, {
      props: { projectId: 'project-a', payloadId: 'p001', visitGeneration: 0, open: true },
    })
    expect(wrapper.findAllComponents({ name: 'MonacoEditor' })).toHaveLength(2)

    await wrapper.setProps({ projectId: 'project-b', visitGeneration: 1 })

    expect(wrapper.findAllComponents({ name: 'MonacoEditor' })).toHaveLength(0)
    expect(wrapper.text()).not.toContain('<script>alert("payload")</script>')
    expect(wrapper.text()).toContain('正在加载 Payload 详情')
  })

  it('copies exact text and links to the owning TaskRun', async () => {
    const wrapper = mount(PayloadDetailDrawer, {
      props: { projectId: 'project-a', payloadId: 'p001', visitGeneration: 0, open: true },
    })

    const buttons = wrapper.findAll('button')
    await buttons.find((button) => button.text().includes('复制 Payload'))?.trigger('click')
    await buttons.find((button) => button.text().includes('tr001'))?.trigger('click')
    await flushPromises()

    expect(mocks.clipboardWrite).toHaveBeenCalledWith(record.payload_text)
    expect(mocks.showToast).toHaveBeenCalledWith('Payload 已复制', 'success')
    expect(mocks.push).toHaveBeenCalledWith({
      name: 'WorkspaceRuns',
      params: { id: 'project-a' },
      query: { runId: 'tr001' },
    })
  })
})
