import { flushPromises, mount } from '@vue/test-utils'
import { reactive } from 'vue'
import { createMemoryHistory, createRouter } from 'vue-router'
import { QueryClient, VueQueryPlugin } from '@tanstack/vue-query'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { VueWrapper } from '@vue/test-utils'
import type { OverviewResponse } from '@/api/types/overview'

const mocks = vi.hoisted(() => ({ api: vi.fn(), auth: null as any }))
vi.mock('@/api/client', () => ({ api: mocks.api }))
vi.mock('@/stores/auth', () => ({ useAuthStore: () => mocks.auth }))

import Overview from './Overview.vue'

const empty: OverviewResponse = {
  generated_at: '2026-10-27T02:03:00Z',
  projects: { total: 0, by_status: {} },
  vulnerabilities: { total: 0, by_severity: {}, by_status: {} },
  project_previews: [],
  vulnerability_previews: [],
}
const loaded: OverviewResponse = {
  ...empty,
  projects: { total: 1, by_status: { active: 1 } },
  vulnerabilities: {
    total: 3,
    by_severity: { high: 2, info: 1 },
    by_status: { new: 2, remediated: 1 },
  },
  project_previews: [
    { id: 'p1', title: '合成项目', status: 'active', created_at: empty.generated_at },
  ],
  vulnerability_previews: [
    {
      id: 'v1',
      project_id: 'p1',
      project_title: '合成项目',
      title: '合成漏洞',
      severity: 'high',
      status: 'remediated',
      created_at: empty.generated_at,
    },
  ],
}
const wrappers: VueWrapper[] = []
const clients: QueryClient[] = []

async function mountOverview() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: Infinity } },
  })
  clients.push(queryClient)
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/', name: 'Overview', component: Overview },
      ...[
        ['/projects', 'Projects'],
        ['/reports', 'Reports'],
        ['/findings', 'Findings'],
        ['/projects/:id', 'WorkspaceGraph'],
      ].map(([path, name]) => ({ path, name, component: { template: '<div />' } })),
    ],
  })
  await router.push('/')
  await router.isReady()
  const wrapper = mount(Overview, {
    attachTo: document.body,
    global: {
      plugins: [router, [VueQueryPlugin, { queryClient }]],
      stubs: { Tooltip: { template: '<slot />' } },
    },
  })
  wrappers.push(wrapper)
  await flushPromises()
  return { wrapper, queryClient }
}

beforeEach(() => {
  mocks.api.mockReset()
  mocks.auth = reactive({
    isAuthenticated: true,
    session: { user_id: 'u1', tenant_id: 't1', role: 'user', must_change_password: false },
  })
  vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('visible')
})

afterEach(() => {
  wrappers.splice(0).forEach((wrapper) => wrapper.unmount())
  clients.splice(0).forEach((client) => client.clear())
  vi.restoreAllMocks()
  document.body.innerHTML = ''
})

describe('Overview', () => {
  it('distinguishes initial loading, failure and a successful real zero; requests only /overview', async () => {
    let reject!: (reason: Error) => void
    mocks.api.mockReturnValueOnce(
      new Promise((_resolve, fail) => {
        reject = fail
      }),
    )
    const { wrapper } = await mountOverview()
    expect(wrapper.text()).toContain('正在加载仪表盘')
    expect(wrapper.find('.overview-stats').exists()).toBe(false)
    reject(new Error('合成读取失败'))
    await flushPromises()
    expect(wrapper.text()).toContain('仪表盘加载失败')
    expect(wrapper.text()).not.toContain('暂无项目')
    mocks.api.mockResolvedValueOnce(empty)
    await wrapper
      .findAll('button')
      .find((button) => button.text() === '重试')!
      .trigger('click')
    await flushPromises()
    expect(wrapper.findAll('.overview-stats dd').map((node) => node.text())).toEqual([
      '0',
      '0',
      '0',
      '0',
    ])
    expect(wrapper.text()).toContain('暂无项目')
    expect(wrapper.text()).toContain('暂无漏洞记录')
    expect(wrapper.findAll('.overview-severity-table tbody tr')).toHaveLength(5)
    expect(
      mocks.api.mock.calls.every(([method, path]) => method === 'GET' && path === '/overview'),
    ).toBe(true)
  })

  it('uses shared status labels, numeric bars and existing keyboard-accessible deep links', async () => {
    mocks.api.mockResolvedValueOnce(loaded)
    const { wrapper } = await mountOverview()
    expect(wrapper.findAll('.overview-stats dd').map((node) => node.text())).toEqual([
      '1',
      '0',
      '3',
      '2',
    ])
    expect(wrapper.text()).toContain('进行中')
    expect(wrapper.text()).toContain('已修复')
    expect(wrapper.get('.overview-projects a').attributes('href')).toBe('/projects/p1')
    expect(wrapper.get('.overview-findings a').attributes('href')).toBe(
      '/findings?vulnerabilityProjectId=p1&vulnerabilityId=v1',
    )
    const rows = wrapper.findAll('.overview-severity-table tbody tr')
    expect(rows[1].get('a').attributes('href')).toBe('/findings?severity=high')
    expect(rows[1].get('a').attributes('aria-label')).toContain('高危：2 条，占 66.7%')
    expect(rows[1].get('.overview-bar').attributes('style')).toContain('width: 100%')
    expect(rows[4].get('.overview-bar').attributes('style')).toContain('width: 50%')
    expect(rows[0].get('.overview-bar').attributes('style')).toContain('width: 0%')
    ;(rows[1].get('a').element as HTMLAnchorElement).focus()
    expect(document.activeElement).toBe(rows[1].get('a').element)
    expect(wrapper.text()).not.toMatch(/TaskRuns|任务服务|等待队列|最近失败/)
    expect(wrapper.find('svg polyline').exists()).toBe(false)
  })

  it('retains data and time during background loading and failure, with a working retry', async () => {
    mocks.api.mockResolvedValueOnce(loaded)
    const { wrapper } = await mountOverview()
    let reject!: (reason: Error) => void
    mocks.api.mockReturnValueOnce(
      new Promise((_resolve, fail) => {
        reject = fail
      }),
    )
    await wrapper
      .findAll('button')
      .find((button) => button.text() === '刷新')!
      .trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('合成漏洞')
    expect(wrapper.find('.overview-loading').exists()).toBe(false)
    reject(new Error('更新失败'))
    await flushPromises()
    expect(wrapper.text()).toContain('更新失败，显示上次数据')
    expect(wrapper.get('.overview-freshness time').attributes('datetime')).toBe(loaded.generated_at)
    expect(wrapper.findAll('.overview-stats dd').map((node) => node.text())).toEqual([
      '1',
      '0',
      '3',
      '2',
    ])
    mocks.api.mockResolvedValueOnce(empty)
    await wrapper
      .findAll('button')
      .find((button) => button.text() === '刷新')!
      .trigger('click')
    await flushPromises()
    expect(wrapper.text()).not.toContain('更新失败，显示上次数据')
    expect(wrapper.text()).toContain('暂无漏洞记录')
  })

  it('does not carry data across users, tenants or administrator scope and disables hidden-page reads', async () => {
    mocks.api.mockResolvedValueOnce(loaded)
    const { wrapper, queryClient } = await mountOverview()
    mocks.api.mockReturnValue(new Promise(() => {}))
    mocks.auth.session = {
      ...mocks.auth.session,
      user_id: 'u2',
      tenant_id: 't2',
      role: 'super_admin',
    }
    await flushPromises()
    expect(wrapper.text()).not.toContain('合成漏洞')
    expect(wrapper.text()).toContain('正在加载仪表盘')
    expect(
      queryClient
        .getQueryCache()
        .getAll()
        .map((query) => query.queryKey),
    ).toEqual([
      ['overview', 'u1', 't1', 'user'],
      ['overview', 'u2', 't2', 'super_admin'],
    ])
    vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('hidden')
    document.dispatchEvent(new Event('visibilitychange'))
    await flushPromises()
    const current = queryClient
      .getQueryCache()
      .find({ queryKey: ['overview', 'u2', 't2', 'super_admin'] })!
    expect(current.isActive()).toBe(false)
  })
})
