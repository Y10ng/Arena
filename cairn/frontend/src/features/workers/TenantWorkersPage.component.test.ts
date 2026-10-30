import { flushPromises, mount } from '@vue/test-utils'
import { ref } from 'vue'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { TenantWorker } from '@/api/types'
import { Select, Tabs } from '@/components/ui'
import TenantWorkersPage from './TenantWorkersPage.vue'

const mocks = vi.hoisted(() => ({
  workers: [] as TenantWorker[],
  isSuperAdmin: false,
  startCreate: vi.fn(),
  requestLeave: vi.fn().mockResolvedValue(true),
  runtimeRead: vi.fn(),
  updateTenantWorker: vi.fn(),
  workersRefetch: vi.fn(),
  showToast: vi.fn(),
  setQueryData: vi.fn(),
}))
vi.mock('@/api/client', () => ({
  isRevisionConflict: (error: unknown) =>
    error instanceof Error && error.message === 'revision_conflict',
  listTenantModels: vi.fn(),
  listTenantWorkers: vi.fn(),
  updateTenantWorker: mocks.updateTenantWorker,
}))
vi.mock('vue-router', () => ({ useRoute: () => ({ query: {} }), onBeforeRouteLeave: vi.fn() }))
vi.mock('@/stores/auth', () => ({ useAuthStore: () => ({ isSuperAdmin: mocks.isSuperAdmin }) }))
vi.mock('@/api/runtimeSettings', () => ({
  readRuntimeSettings: mocks.runtimeRead,
  patchRuntimeSettings: vi.fn(),
}))
vi.mock('@/features/models/TenantModelsPanel.vue', () => ({
  default: {
    template: '<section>模型清单</section>',
    setup: (_props: unknown, { expose }: { expose: (methods: unknown) => void }) => {
      expose({ startCreate: mocks.startCreate, requestLeave: mocks.requestLeave })
      return {}
    },
  },
}))
vi.mock('@/stores/ui', () => ({ useUIStore: () => ({ showToast: mocks.showToast }) }))
vi.mock('@tanstack/vue-query', () => ({
  useQuery: ({ queryKey }: { queryKey: string[] }) => ({
    data: ref(
      queryKey[0] === 'tenant-workers'
        ? mocks.workers
        : queryKey[0] === 'tenant-models'
          ? []
          : null,
    ),
    error: ref(null),
    isPending: ref(false),
    isFetching: ref(false),
    refetch: mocks.workersRefetch,
  }),
  useQueryClient: () => ({ setQueryData: mocks.setQueryData }),
}))
const worker = (role: TenantWorker['task_types'][number]): TenantWorker => ({
  id: role,
  display_name: role,
  model_id: null,
  model_display_name: '',
  model_name: '',
  engine: 'pi',
  task_types: [role],
  max_running: role === 'explore' ? 3 : 1,
  priority: 50,
  enabled: role !== 'reason',
  available: false,
  unavailable_reason: 'model_unconfigured',
  executor_available: true,
  revision: 7,
  created_at: '',
  updated_at: '',
})
const wrappers: ReturnType<typeof mount>[] = []
function mountPage() {
  const wrapper = mount(TenantWorkersPage, {
    attachTo: document.body,
    global: { stubs: { RouterLink: { template: '<a><slot /></a>' } } },
  })
  wrappers.push(wrapper)
  return wrapper
}
beforeEach(() => {
  mocks.isSuperAdmin = false
  mocks.startCreate.mockClear()
  mocks.runtimeRead.mockClear()
  mocks.workers = [worker('explore'), worker('reason'), worker('bootstrap')]
  mocks.updateTenantWorker.mockReset().mockImplementation(async (id, body) => ({
    ...mocks.workers.find((item) => item.id === id),
    ...body,
    revision: 8,
  }))
  mocks.workersRefetch.mockReset().mockResolvedValue({ data: mocks.workers, error: null })
  mocks.showToast.mockReset()
  mocks.setQueryData.mockReset()
})
afterEach(() => {
  wrappers.splice(0).forEach((wrapper) => wrapper.unmount())
  document.body.innerHTML = ''
})

describe('固定 Worker 配置', () => {
  it('普通用户同页新增模型，不显示运行设置或调用管理员 API', async () => {
    const wrapper = mountPage()
    expect(wrapper.findComponent(Tabs).exists()).toBe(false)
    expect(wrapper.text()).toContain('模型清单')
    await wrapper
      .findAll('button')
      .find((button) => button.text() === '新增模型')!
      .trigger('click')
    expect(mocks.startCreate).toHaveBeenCalledTimes(1)
    expect(mocks.runtimeRead).not.toHaveBeenCalled()
  })

  it('管理员标签关联实际内容，运行区懒挂载且切换保留 Worker 草稿', async () => {
    mocks.isSuperAdmin = true
    const wrapper = mountPage()
    await flushPromises()
    const workerTab = wrapper.get('[role="tab"][data-state="active"]')
    const workerPanel = wrapper.get(`#${workerTab.attributes('aria-controls')}`)
    expect(workerPanel.text()).toContain('模型清单')
    expect(workerPanel.find('.worker-table').exists()).toBe(true)
    expect(wrapper.find('.runtime-settings-panel').exists()).toBe(false)
    await workerPanel.get('input[aria-label="explore 并发"]').setValue('6')
    await wrapper.findAll('[role="tab"]')[1]!.trigger('mousedown', { button: 0, ctrlKey: false })
    await flushPromises()
    const runtimeTab = wrapper.get('[role="tab"][data-state="active"]')
    const runtimePanel = wrapper.get(`#${runtimeTab.attributes('aria-controls')}`)
    const runtimeElement = runtimePanel.get('.runtime-settings-panel').element
    expect(workerPanel.isVisible()).toBe(false)
    await workerTab.trigger('mousedown', { button: 0, ctrlKey: false })
    await flushPromises()
    expect(wrapper.get('.runtime-settings-panel').element).toBe(runtimeElement)
    expect(workerPanel.get('input[aria-label="explore 并发"]').element).toHaveProperty('value', '6')
  })

  it('按固定次序展示真实三行，未配置模型也能保存并保留禁用及优先级', async () => {
    const wrapper = mountPage()
    expect(wrapper.findAll('tbody th').map((cell) => cell.text())).toEqual([
      '初始化bootstrap',
      '决策reason',
      '探索explore',
    ])
    expect(wrapper.text()).toContain('未配置模型')
    expect(wrapper.text()).not.toMatch(/新增 Worker|删除|任务类型|默认组合/)
    expect(wrapper.get('input[aria-label="explore 并发"]').element).toHaveProperty('value', '3')
    await wrapper.get('button[aria-label="保存 reason"]').trigger('click')
    await flushPromises()
    expect(mocks.updateTenantWorker).toHaveBeenCalledExactlyOnceWith('reason', {
      model_id: null,
      max_running: 1,
      priority: 50,
      enabled: false,
      expected_revision: 7,
    })
  })

  it('只提交可编辑字段，保存中阻止重复操作，其他行草稿不丢失', async () => {
    let resolve!: (value: TenantWorker) => void
    mocks.updateTenantWorker.mockReturnValueOnce(
      new Promise<TenantWorker>((done) => {
        resolve = done
      }),
    )
    const wrapper = mountPage()
    wrapper.findAllComponents(Select)[0]!.vm.$emit('update:modelValue', 'model-a')
    await wrapper.get('input[aria-label="bootstrap 并发"]').setValue('4')
    await wrapper.get('input[aria-label="explore 并发"]').setValue('6')
    await wrapper.get('button[aria-label="保存 bootstrap"]').trigger('click')
    await wrapper.get('button[aria-label="保存 bootstrap"]').trigger('click')
    expect(mocks.updateTenantWorker).toHaveBeenCalledExactlyOnceWith('bootstrap', {
      model_id: 'model-a',
      max_running: 4,
      priority: 50,
      enabled: true,
      expected_revision: 7,
    })
    resolve({ ...worker('bootstrap'), model_id: 'model-a', max_running: 4, revision: 8 })
    await flushPromises()
    expect(wrapper.get('input[aria-label="explore 并发"]').element).toHaveProperty('value', '6')
    expect(mocks.showToast).toHaveBeenCalledWith('Worker 配置已保存', 'success')
  })

  it('冲突及刷新失败保留草稿，用户重试后使用最新版本', async () => {
    mocks.updateTenantWorker.mockRejectedValueOnce(new Error('revision_conflict'))
    const wrapper = mountPage()
    await wrapper.get('input[aria-label="bootstrap 并发"]').setValue('5')
    await wrapper.get('button[aria-label="保存 bootstrap"]').trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('草稿已保留')
    mocks.workersRefetch.mockResolvedValueOnce({ error: new Error('读取失败') })
    await wrapper.get('button[aria-label="保存 bootstrap"]').trigger('click')
    await flushPromises()
    expect(mocks.updateTenantWorker).toHaveBeenCalledTimes(1)
    expect(wrapper.get('input[aria-label="bootstrap 并发"]').element).toHaveProperty('value', '5')
    mocks.workersRefetch.mockResolvedValueOnce({
      data: [{ ...worker('bootstrap'), revision: 9 }],
      error: null,
    })
    await wrapper.get('button[aria-label="保存 bootstrap"]').trigger('click')
    await flushPromises()
    expect(mocks.updateTenantWorker).toHaveBeenLastCalledWith('bootstrap', {
      model_id: null,
      max_running: 5,
      priority: 50,
      enabled: true,
      expected_revision: 9,
    })
  })
})
