import { flushPromises, mount } from '@vue/test-utils'
import { reactive, ref } from 'vue'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { InjectionPayloadListItem } from '@/api/types'
import PayloadsPage from './PayloadsPage.vue'

const mocks = vi.hoisted(() => ({
  listCategories: vi.fn(),
  listPayloads: vi.fn(),
  refetch: vi.fn(),
  replace: vi.fn(),
  push: vi.fn(),
  route: undefined as unknown as { params: { id: string }; query: Record<string, string> },
  queryOptions: [] as Array<Record<string, unknown>>,
}))

vi.mock('@/api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/client')>()
  return {
    ...actual,
    listProjectInjectionPayloadCategories: mocks.listCategories,
    listProjectInjectionPayloads: mocks.listPayloads,
  }
})

vi.mock('vue-router', () => {
  mocks.route = reactive({ params: { id: 'project-a' }, query: {} })
  return {
    useRoute: () => mocks.route,
    useRouter: () => ({ replace: mocks.replace, push: mocks.push }),
  }
})

vi.mock('@tanstack/vue-query', () => ({
  useQuery: (options: Record<string, unknown>) => {
    mocks.queryOptions.push(options)
    const key = (options.queryKey as { value: readonly unknown[] }).value
    if (key.includes('categories')) {
      return {
        data: ref({
          projectId: 'project-a',
          visitGeneration: 0,
          items: [{ injection_type: '未知 Agent 总线 / 消息体', count: 1 }],
        }),
      }
    }
    return {
      data: ref({
        projectId: 'project-a',
        injectionType: '',
        responseStatus: '',
        search: '',
        offset: 0,
        visitGeneration: 0,
        page: { items: [payload], total: 1, limit: 50, offset: 0 },
      }),
      error: ref(null),
      isError: ref(false),
      isFetching: ref(false),
      isLoading: ref(false),
      refetch: mocks.refetch,
    }
  },
}))

vi.mock('./PayloadDetailDrawer.vue', () => ({
  default: {
    name: 'PayloadDetailDrawer',
    props: ['projectId', 'payloadId', 'visitGeneration', 'open'],
    emits: ['close'],
    template: '<div data-testid="payload-drawer" />',
  },
}))

const payload: InjectionPayloadListItem = {
  id: 'p001',
  project_id: 'project-a',
  task_run_id: 'tr001',
  phase: 'explore_execute',
  capture_id: 'pc_001',
  injection_type: '未知 Agent 总线 / 消息体',
  payload_preview: '<script>alert(1)</script>\nnext',
  original_format: '自定义 JSON message.content',
  response_status: 'returned',
  response_preview: '<img src=x onerror=alert(2)>',
  response_format: 'Agent 聊天文本',
  created_at: '2026-09-03T00:00:00Z',
  updated_at: '2026-09-03T00:01:00Z',
}

beforeEach(() => {
  mocks.route.params.id = 'project-a'
  mocks.route.query = {}
  mocks.queryOptions.length = 0
  mocks.listCategories.mockReset().mockResolvedValue([])
  mocks.listPayloads.mockReset().mockResolvedValue({ items: [], total: 0, limit: 50, offset: 0 })
  mocks.refetch.mockReset()
  mocks.replace.mockReset().mockResolvedValue(undefined)
  mocks.push.mockReset().mockResolvedValue(undefined)
})

afterEach(() => {
  document.body.innerHTML = ''
})

describe('PayloadsPage', () => {
  it('shows dynamic unknown categories and renders Payload previews as text', () => {
    const wrapper = mount(PayloadsPage, { attachTo: document.body })

    expect(wrapper.text()).toContain('未知 Agent 总线 / 消息体')
    expect(wrapper.text()).toContain('<script>alert(1)</script> ↵ next')
    expect(wrapper.find('script').exists()).toBe(false)
  })

  it('uses immutable project/filter/pagination snapshots with AbortSignal', async () => {
    mount(PayloadsPage)
    const listOptions = mocks.queryOptions.find((options) =>
      (options.queryKey as { value: readonly unknown[] }).value.includes('list'),
    ) as {
      queryKey: { value: readonly unknown[] }
      queryFn: (context: { queryKey: readonly unknown[]; signal: AbortSignal }) => Promise<unknown>
    }
    const controller = new AbortController()

    await listOptions.queryFn({ queryKey: listOptions.queryKey.value, signal: controller.signal })

    expect(mocks.listPayloads).toHaveBeenCalledWith(
      'project-a',
      {
        injectionType: undefined,
        responseStatus: undefined,
        search: undefined,
        limit: 50,
        offset: 0,
      },
      controller.signal,
    )
  })

  it('opens details through payloadId and clears selection on project changes', async () => {
    const wrapper = mount(PayloadsPage)
    const detailButton = wrapper.findAll('button').find((button) => button.text().trim() === '详情')
    await detailButton?.trigger('click')
    expect(mocks.replace).toHaveBeenCalledWith({ query: { payloadId: 'p001' } })

    mocks.route.query = { payloadId: 'p001' }
    mocks.route.params.id = 'project-b'
    await flushPromises()

    expect(mocks.replace).toHaveBeenLastCalledWith({ query: {} })
    expect(wrapper.getComponent({ name: 'PayloadDetailDrawer' }).props('projectId')).toBe(
      'project-b',
    )
  })
})
