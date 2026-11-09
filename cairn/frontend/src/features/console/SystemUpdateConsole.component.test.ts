import { flushPromises, mount } from '@vue/test-utils'
import { ref } from 'vue'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { VueWrapper } from '@vue/test-utils'
import SystemUpdateConsole from './SystemUpdateConsole.vue'
import type { UpdateStatusResponse } from '@/api/systemUpdate'

const mocks = vi.hoisted(() => ({
  getStatus: vi.fn(),
  check: vi.fn(),
  prepare: vi.fn(),
  cancel: vi.fn(),
  apply: vi.fn(),
  refetch: vi.fn(),
}))

vi.mock('@/api/systemUpdate', () => ({
  getSystemUpdateStatus: mocks.getStatus,
  checkSystemUpdate: mocks.check,
  prepareSystemUpdate: mocks.prepare,
  cancelSystemUpdate: mocks.cancel,
  applySystemUpdate: mocks.apply,
}))

vi.mock('@tanstack/vue-query', () => ({
  useQuery: () => ({
    data: ref(statusData.value),
    isPending: ref(false),
    isError: ref(false),
    error: ref(null),
    refetch: mocks.refetch,
  }),
}))

const statusData = ref<UpdateStatusResponse | null>(null)
const wrappers: VueWrapper[] = []

function makeStatus(overrides: Partial<UpdateStatusResponse> = {}): UpdateStatusResponse {
  return {
    version: '0.2.5',
    install_type: 'zip',
    state: {
      phase: 'idle',
      error: null,
      prepare_progress: null,
      channel_version: null,
      channel: null,
      apply_log_path: null,
      previous_gateway_ref: null,
      updated_at: null,
    },
    channel_configured: true,
    gateway_image: 'yong29/arena-traffic-gateway:latest',
    running_task_count: 0,
    queued_task_count: 0,
    running_project_count: 0,
    ...overrides,
  }
}

function button(text: string) {
  const found = [...document.body.querySelectorAll<HTMLButtonElement>('button')].find(
    (item) => item.textContent?.trim() === text,
  )
  if (!found) throw new Error(`找不到按钮：${text}`)
  return found
}

async function mountPage() {
  const wrapper = mount(SystemUpdateConsole, { attachTo: document.body })
  wrappers.push(wrapper)
  await flushPromises()
  return wrapper
}

beforeEach(() => {
  statusData.value = makeStatus()
  mocks.getStatus.mockReset().mockResolvedValue(statusData.value)
  mocks.check
    .mockReset()
    .mockResolvedValue({
      phase: 'available',
      current_version: '0.2.5',
      channel_version: '0.2.6',
      update_available: true,
    })
  mocks.prepare.mockReset().mockResolvedValue({ phase: 'preparing' })
  mocks.cancel.mockReset().mockResolvedValue({ phase: 'available' })
  mocks.apply.mockReset().mockResolvedValue({ updater_pid: 12345 })
  mocks.refetch.mockReset().mockResolvedValue(undefined)
  vi.spyOn(window, 'confirm').mockReturnValue(true)
})

afterEach(() => {
  wrappers.splice(0).forEach((wrapper) => wrapper.unmount())
  document.body.innerHTML = ''
  vi.restoreAllMocks()
})

describe('SystemUpdateConsole', () => {
  it('显示当前版本与空闲状态', async () => {
    await mountPage()
    expect(document.body.textContent).toContain('0.2.5')
    expect(document.body.textContent).toContain('空闲')
  })

  it('git 安装显示指引而非一键更新', async () => {
    statusData.value = makeStatus({ install_type: 'git' })
    await mountPage()
    expect(document.body.textContent).toContain('源码安装')
    expect(document.body.textContent).toContain('git pull')
  })

  it('有新版本时显示下载按钮', async () => {
    statusData.value = makeStatus({
      state: {
        phase: 'available',
        channel_version: '0.2.6',
        channel: { format: 'arena-release-channel/v1', version: '0.2.6' },
      },
    })
    await mountPage()
    expect(document.body.textContent).toContain('0.2.6')
    expect(document.body.textContent).toContain('有新版本')
    expect(button('下载并准备更新')).toBeTruthy()
  })

  it('准备就绪时显示更新按钮', async () => {
    statusData.value = makeStatus({
      state: {
        phase: 'ready',
        channel_version: '0.2.6',
        channel: { format: 'arena-release-channel/v1', version: '0.2.6' },
      },
    })
    await mountPage()
    expect(button('立即更新并重启')).toBeTruthy()
  })

  it('准备中显示进度与取消按钮', async () => {
    statusData.value = makeStatus({
      state: { phase: 'preparing', prepare_progress: '正在下载…' },
    })
    await mountPage()
    expect(document.body.textContent).toContain('正在下载…')
    expect(button('取消准备')).toBeTruthy()
  })

  it('失败时显示错误信息', async () => {
    statusData.value = makeStatus({
      state: { phase: 'failed', error: 'sha256 校验失败' },
    })
    await mountPage()
    expect(document.body.textContent).toContain('sha256 校验失败')
  })

  it('点击检查更新调用 API', async () => {
    await mountPage()
    button('检查更新').click()
    await flushPromises()
    expect(mocks.check).toHaveBeenCalled()
  })

  it('点击下载调用 prepare API', async () => {
    statusData.value = makeStatus({
      state: {
        phase: 'available',
        channel_version: '0.2.6',
        channel: { format: 'arena-release-channel/v1', version: '0.2.6' },
      },
    })
    await mountPage()
    button('下载并准备更新').click()
    await flushPromises()
    expect(mocks.prepare).toHaveBeenCalled()
  })

  it('点击立即更新调用 apply API', async () => {
    statusData.value = makeStatus({
      state: {
        phase: 'ready',
        channel_version: '0.2.6',
        channel: { format: 'arena-release-channel/v1', version: '0.2.6' },
      },
    })
    await mountPage()
    button('立即更新并重启').click()
    await flushPromises()
    expect(mocks.apply).toHaveBeenCalled()
  })

  it('有运行中任务时显示警告', async () => {
    statusData.value = makeStatus({
      state: {
        phase: 'ready',
        channel_version: '0.2.6',
        channel: { format: 'arena-release-channel/v1', version: '0.2.6' },
      },
      running_task_count: 2,
      queued_task_count: 1,
    })
    await mountPage()
    expect(document.body.textContent).toContain('2 个运行中任务')
    expect(document.body.textContent).toContain('1 个排队任务')
  })
})
