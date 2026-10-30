import { flushPromises, mount } from '@vue/test-utils'
import { QueryClient, VueQueryPlugin } from '@tanstack/vue-query'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiError } from '@/api/client'
import type { RuntimeSettingsDocument } from '@/api/runtimeSettings'
import RuntimeSettingsPanel from './RuntimeSettingsPanel.vue'

const mocks = vi.hoisted(() => ({ read: vi.fn(), patch: vi.fn() }))
vi.mock('@/api/runtimeSettings', () => ({
  readRuntimeSettings: mocks.read,
  patchRuntimeSettings: mocks.patch,
}))
const documentValue = (): RuntimeSettingsDocument => ({
  revision: 'original',
  runtime: {
    max_workers: 8,
    max_project_workers: 4,
    max_running_projects: 3,
    interval: 3,
    healthcheck_timeout: 30,
  },
  tasks: {
    bootstrap: { timeout: 600, conclude_timeout: 60 },
    reason: { timeout: 300 },
    explore: { timeout: 900, conclude_timeout: 60 },
    report: { timeout: 600 },
  },
  load_status: { state: 'loaded', hard_cap: 10, updated_at: null, message: '' },
})
const wrappers: ReturnType<typeof mount>[] = []
const clients: QueryClient[] = []
async function setup() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  clients.push(client)
  const wrapper = mount(RuntimeSettingsPanel, {
    global: { plugins: [[VueQueryPlugin, { queryClient: client }]] },
  })
  wrappers.push(wrapper)
  await flushPromises()
  return wrapper
}
beforeEach(() => {
  mocks.read.mockReset().mockResolvedValue(documentValue())
  mocks.patch.mockReset().mockResolvedValue(documentValue())
})
afterEach(() => {
  wrappers.splice(0).forEach((wrapper) => wrapper.unmount())
  clients.splice(0).forEach((client) => client.clear())
})

describe('运行设置窄字段保存', () => {
  it.each([
    ['loaded', 'Dispatcher 已加载当前配置', 'Dispatcher 已加载。', false],
    ['pending', '等待下一次上报', '等待 Dispatcher 加载。', true],
    ['failed', '配置读取失败', 'Dispatcher 加载失败。', true],
    ['unknown', '没有有效上报', 'Dispatcher 加载状态未知。', true],
  ] as const)(
    '加载状态 %s 只省略正常成功的重复说明',
    async (state, message, label, showMessage) => {
      const value = documentValue()
      value.load_status = { ...value.load_status, state, message }
      mocks.read.mockResolvedValue(value)
      const wrapper = await setup()
      const status = wrapper.get('[role="status"]').text()
      expect(status).toContain(label)
      expect(status.includes(message)).toBe(showMessage)
    },
  )

  it('仅提交改动字段和原始revision，保存与加载状态分开', async () => {
    const pending = documentValue()
    pending.revision = 'saved'
    pending.runtime.max_workers = 9
    pending.load_status.state = 'pending'
    mocks.patch.mockResolvedValue(pending)
    const wrapper = await setup()
    mocks.read.mockResolvedValue(pending)
    await wrapper.get('input[aria-label="总 Worker 并发"]').setValue('9')
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    expect(mocks.patch).toHaveBeenCalledExactlyOnceWith({
      revision: 'original',
      runtime: { max_workers: 9 },
    })
    expect(wrapper.text()).toContain('配置已保存')
    expect(wrapper.text()).toContain('等待 Dispatcher 加载')
    expect(wrapper.text()).not.toContain('Dispatcher 已加载')
  })

  it('冲突保留草稿且不自动重试，核对后才使用新revision', async () => {
    mocks.patch.mockRejectedValueOnce(new ApiError('revision conflict', 409, null))
    const wrapper = await setup()
    await wrapper.get('input[aria-label="探索执行时限（秒）"]').setValue('1200')
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    expect(wrapper.text()).toContain('草稿已保留')
    expect(wrapper.get('input[aria-label="探索执行时限（秒）"]').element).toHaveProperty(
      'value',
      '1200',
    )
    await wrapper.get('form').trigger('submit')
    expect(mocks.patch).toHaveBeenCalledTimes(1)
    const current = documentValue()
    current.revision = 'updated'
    current.tasks.explore.timeout = 1000
    mocks.read.mockResolvedValue(current)
    await wrapper
      .findAll('button')
      .find((button) => button.text() === '读取当前配置')!
      .trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('1000')
    await wrapper
      .findAll('button')
      .find((button) => button.text() === '已核对，继续编辑')!
      .trigger('click')
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    expect(mocks.patch).toHaveBeenLastCalledWith({
      revision: 'updated',
      tasks: { explore: { timeout: 1200 } },
    })
  })

  it('非法容量不提交，网络不确定时先核对而不显示成功', async () => {
    const wrapper = await setup()
    await wrapper.get('input[aria-label="单项目 Worker 并发"]').setValue('9')
    await wrapper.get('form').trigger('submit')
    expect(mocks.patch).not.toHaveBeenCalled()
    expect(wrapper.text()).toContain('不能超过总 Worker 并发')
    await wrapper.get('input[aria-label="单项目 Worker 并发"]').setValue('5')
    mocks.patch.mockRejectedValueOnce(new TypeError('network'))
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    expect(wrapper.text()).toContain('保存结果未确认')
    expect(wrapper.text()).not.toContain('配置已保存')
    await wrapper.get('form').trigger('submit')
    expect(mocks.patch).toHaveBeenCalledTimes(1)
  })
})
