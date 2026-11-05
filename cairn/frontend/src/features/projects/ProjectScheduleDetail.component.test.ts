import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { QueryClient, VueQueryPlugin } from '@tanstack/vue-query'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { onBeforeRouteLeave, onBeforeRouteUpdate } from 'vue-router'
import { api } from '@/api/client'
import { Dialog } from '@/components/ui'
import TestEgressFields from '@/components/patterns/TestEgressFields.vue'
import ProjectScheduleDetail from './ProjectScheduleDetail.vue'

vi.mock('@/api/client', () => ({ api: vi.fn() }))
vi.mock('vue-router', () => ({
  onBeforeRouteLeave: vi.fn(),
  onBeforeRouteUpdate: vi.fn(),
  useRoute: () => ({ params: { id: 'schedule-a' } }),
}))

let wrapper: VueWrapper | undefined
let client: QueryClient | undefined
const egress = {
  mode: 'http',
  host: 'proxy.example.test',
  port: 8080,
  auth_configured: true,
  revision: 2,
}

async function openDirtyReschedule() {
  const schedule = {
    id: 'schedule-a',
    version: 4,
    status: 'active',
    mode: 'once',
    timezone: 'Asia/Shanghai',
    start_at: '2099-06-01T09:00:00+08:00',
    next_run_at: '2099-06-01T09:00:00+08:00',
    local_time: null,
    weekdays: [],
    end_date: null,
    max_runs: null,
    project_id: 'project-a',
    project: null,
    test_egress: egress,
    resources: [],
    library_bindings: [],
    generated_count: 0,
  }
  vi.mocked(api).mockImplementation(async (_method, path) =>
    path.includes('/history?') ? { items: [], total: 0 } : schedule,
  )
  client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } })
  wrapper = mount(ProjectScheduleDetail, {
    attachTo: document.body,
    global: {
      plugins: [[VueQueryPlugin, { queryClient: client }]],
      stubs: {
        NewProject: true,
        ScheduleTiming: true,
        RouterLink: { template: '<a><slot /></a>' },
      },
    },
  })
  await flushPromises()
  await clickButton('改期与代理设置')
  const fields = wrapper.getComponent(TestEgressFields)
  fields.vm.$emit('update:modelValue', {
    ...fields.props('modelValue'),
    host: 'edited.example.test',
  })
  await flushPromises()
  return wrapper
}

async function clickButton(label: string) {
  const button = [...document.querySelectorAll('button')].find(
    (item) => item.textContent?.trim() === label,
  )
  expect(button, `找不到按钮：${label}`).toBeTruthy()
  button!.click()
  await flushPromises()
}

function expectNoWrites() {
  expect(vi.mocked(api).mock.calls.every(([method]) => method === 'GET')).toBe(true)
}

afterEach(() => {
  wrapper?.unmount()
  client?.clear()
  document.body.innerHTML = ''
  vi.clearAllMocks()
})

describe('单次计划资料详情', () => {
  it.each([
    {
      label: '显示关联项目的实际资料', projectId: 'project-a', available: true,
      resources: [{ id: 'resource-a', path: 'inputs/brief.txt', kind: 'note', size_bytes: 13, digest_sha256: 'digest-a', source_name: null }],
      bindings: [{ library_key: 'notes', library_name: '计划资料', version: 1, file_count: 1, digest_sha256: 'digest-b' }],
      expected: 'inputs/brief.txt', also: '固定 v1 · 1 文件', absent: '未绑定固定资料',
    },
    {
      label: '旧服务不被误报为空选择', projectId: 'project-a', available: undefined,
      resources: [], bindings: [], expected: '当前服务未提供关联项目的资料信息',
      also: '请进入项目查看', absent: '未绑定固定资料',
    },
    {
      label: '关联项目删除后显示不可用', projectId: null, available: false,
      resources: [], bindings: [], expected: '关联项目已不可用',
      also: '无法查看其资料', absent: '未绑定固定资料',
    },
    {
      label: '新服务确认没有资料', projectId: 'project-a', available: true,
      resources: [], bindings: [], expected: '未绑定固定资料',
      also: '关联项目资料', absent: '当前服务未提供',
    },
  ])('$label', async ({ projectId, available, resources, bindings, expected, also, absent }) => {
    const schedule = {
      id: 'schedule-a', version: 1, status: 'active', mode: 'once', timezone: 'UTC',
      start_at: '2099-06-01T09:00:00Z', next_run_at: '2099-06-01T09:00:00Z',
      local_time: null, weekdays: [], end_date: null, max_runs: null,
      project_id: projectId, project: null, test_egress: egress,
      once_project_materials_available: available,
      resources, library_bindings: bindings, generated_count: 0,
    }
    vi.mocked(api).mockImplementation(async (_method, path) =>
      path.includes('/history?') ? { items: [], total: 0 } : schedule,
    )
    client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } })
    wrapper = mount(ProjectScheduleDetail, {
      attachTo: document.body,
      global: {
        plugins: [[VueQueryPlugin, { queryClient: client }]],
        stubs: { NewProject: true, ScheduleTiming: true, RouterLink: { template: '<a><slot /></a>' } },
      },
    })
    await flushPromises()
    const details = wrapper.get('.schedule-inputs')
    expect(details.text()).toContain(expected)
    expect(details.text()).toContain(also)
    expect(details.text()).not.toContain(absent)
    expectNoWrites()
  })
})

describe('计划详情改期取消保护', () => {
  it('取消异步确认保留表单与草稿，明确放弃才关闭且不保存', async () => {
    const page = await openDirtyReschedule()
    await clickButton('取消')
    expect(document.body.textContent).toContain('放弃未保存的代理设置？')
    await clickButton('继续编辑')
    expect(page.getComponent(Dialog).props('open')).toBe(true)
    expect(page.getComponent(TestEgressFields).props('modelValue').host).toBe('edited.example.test')
    expectNoWrites()

    await clickButton('取消')
    await clickButton('放弃修改')
    expect(page.getComponent(Dialog).props('open')).toBe(false)
    expectNoWrites()
  })

  it('路由离开和参数更新等待确认，取消返回false，页面卸载保护仍生效', async () => {
    const page = await openDirtyReschedule()
    const leave = vi.mocked(onBeforeRouteLeave).mock.calls[0]![0] as () =>
      boolean | Promise<boolean>
    const update = vi.mocked(onBeforeRouteUpdate).mock.calls[0]![0] as () =>
      boolean | Promise<boolean>
    const pending = leave()
    expect(pending).toBeInstanceOf(Promise)
    expect(update()).toBe(pending)
    let resolved = false
    void Promise.resolve(pending).then(() => {
      resolved = true
    })
    await flushPromises()
    expect(resolved).toBe(false)
    await clickButton('继续编辑')
    expect(await pending).toBe(false)
    expect(page.getComponent(Dialog).props('open')).toBe(true)
    expect(page.getComponent(TestEgressFields).props('modelValue').host).toBe('edited.example.test')

    const unload = new Event('beforeunload', { cancelable: true })
    window.dispatchEvent(unload)
    expect(unload.defaultPrevented).toBe(true)

    const confirmed = update()
    await flushPromises()
    await clickButton('放弃修改')
    expect(await confirmed).toBe(true)
    expectNoWrites()
  })
})
