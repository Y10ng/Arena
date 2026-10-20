import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { QueryClient, VueQueryPlugin, useQuery } from '@tanstack/vue-query'
import { defineComponent } from 'vue'
import { afterEach, describe, expect, it, vi } from 'vitest'
import FrontLayout from './FrontLayout.vue'

const mocks = vi.hoisted(() => ({
  response: vi.fn(),
  ui: {
    isSidebarCollapsed: false,
    isCommandPaletteOpen: false,
    theme: 'dark',
    triggerGlobalRefresh: vi.fn(),
    showToast: vi.fn(),
  },
}))
vi.mock('@/stores/ui', () => ({ useUIStore: () => mocks.ui }))
vi.mock('@/stores/auth', () => ({
  useAuthStore: () => ({ isSuperAdmin: false, displayName: '合成用户' }),
}))
vi.mock('@/stores/workspace', () => ({
  useWorkspaceStore: () => ({ projectDetail: null, clearProject: vi.fn() }),
}))
vi.mock('vue-router', () => ({
  useRoute: () => ({ name: 'Projects', matched: [], params: {} }),
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
}))

let wrapper: VueWrapper | undefined
let queryClient: QueryClient | undefined

afterEach(() => {
  wrapper?.unmount()
  queryClient?.clear()
  document.body.innerHTML = ''
  vi.clearAllMocks()
})

describe('global refresh with a real QueryClient', () => {
  it('does not promise success after invalidate and still reports actual refetch failures', async () => {
    mocks.response.mockResolvedValue('合成响应')
    queryClient = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: Infinity } } })
    const probe = defineComponent({
      setup() {
        const query = useQuery({ queryKey: ['refresh-probe'], queryFn: mocks.response })
        return { data: query.data, failed: query.isError }
      },
      template: '<div>{{ failed ? "数据查询失败" : data }}</div>',
    })
    wrapper = mount(FrontLayout, {
      attachTo: document.body,
      global: {
        plugins: [[VueQueryPlugin, { queryClient }]],
        stubs: {
          AppSidebar: true,
          RouterLink: { template: '<a><slot /></a>' },
          RouterView: probe,
        },
      },
    })
    await flushPromises()
    const trigger = wrapper.get('button[title="刷新页面数据"]')
    await trigger.trigger('click')
    await flushPromises()
    expect(mocks.response).toHaveBeenCalledTimes(2)
    expect(mocks.ui.triggerGlobalRefresh).toHaveBeenCalledTimes(1)
    expect(mocks.ui.showToast).not.toHaveBeenCalled()

    mocks.response.mockRejectedValue(new Error('private upstream error'))
    await trigger.trigger('click')
    await flushPromises()
    expect(mocks.response).toHaveBeenCalledTimes(3)
    expect(wrapper.text()).toContain('数据查询失败')
    expect(mocks.ui.showToast.mock.calls).toEqual([['刷新失败', 'danger']])
    expect(wrapper.text()).not.toContain('private upstream error')
  })
})
