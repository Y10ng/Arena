import { mount } from '@vue/test-utils'
import { ref, unref } from 'vue'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { VueWrapper } from '@vue/test-utils'

const mocks = vi.hoisted(() => ({
  api: vi.fn(),
  isSuperAdmin: false,
  queryOptions: [] as Array<{ queryKey: unknown; queryFn?: () => unknown }>,
  projects: [{ id: 'project-a', status: 'active', title: '项目 A' }],
  monitoring: {
    generated_at: '2026-08-23T00:00:00Z',
    long_running_threshold_seconds: 300,
    project_count: 1,
    active_project_count: 1,
    dispatcher: { status: 'running', stale: false, updated_at: null },
    queued_tasks: [],
    running_tasks: [],
    long_running_tasks: [],
    recent_failures: [],
    vulnerabilities: {
      total: 10,
      by_severity: { critical: 4, high: 3, medium: 2, info: 1 },
      by_status: {},
      per_project: [],
    },
  } as any,
  router: { push: vi.fn() },
}))

vi.mock('@/api/client', () => ({ api: mocks.api }))

vi.mock('@/stores/auth', () => ({
  useAuthStore: () => mocks,
}))

vi.mock('vue-router', () => ({
  useRouter: () => mocks.router,
}))

vi.mock('@tanstack/vue-query', () => ({
  useQuery: (options: { queryKey: unknown; queryFn?: () => unknown }) => {
    mocks.queryOptions.push(options)
    const key = unref(options.queryKey)
    if (Array.isArray(key) && key[0] === 'projects') {
      return { data: ref(mocks.projects), isLoading: ref(false) }
    }
    return { data: ref(mocks.monitoring), isLoading: ref(false) }
  },
}))

import Overview from './Overview.vue'

const wrappers: VueWrapper[] = []

function mountOverview() {
  const wrapper = mount(Overview, {
    attachTo: document.body,
    global: {
      mocks: { $router: mocks.router },
      stubs: {
        Alert: { template: '<div><slot /></div>' },
        Badge: { template: '<span><slot /></span>' },
        Button: { template: '<button type="button"><slot name="icon" /><slot /></button>' },
        Card: {
          props: ['as'],
          template: '<component :is="as || \'div\'"><slot /></component>',
        },
        EmptyState: { template: '<div />' },
        Spinner: { template: '<span />' },
        StatusBadge: { template: '<span><slot /></span>' },
      },
    },
  })
  wrappers.push(wrapper)
  return wrapper
}

beforeEach(() => {
  mocks.router.push.mockReset()
  mocks.api.mockReset().mockResolvedValue(undefined)
  mocks.isSuperAdmin = false
  mocks.queryOptions.length = 0
  mocks.monitoring.dispatcher = { status: 'running', stale: false, updated_at: null }
  mocks.monitoring.vulnerabilities = {
    total: 10,
    by_severity: { critical: 4, high: 3, medium: 2, info: 1 },
    by_status: {},
    per_project: [],
  }
})

afterEach(() => {
  wrappers.splice(0).forEach((wrapper) => wrapper.unmount())
  document.body.innerHTML = ''
})

function monitoringQueryOptions() {
  const options = mocks.queryOptions.find((item) => {
    const key = unref(item.queryKey)
    return Array.isArray(key) && key[0] === 'monitoringSummary'
  })
  if (!options?.queryFn) throw new Error('找不到概览监控查询')
  return options as { queryKey: unknown; queryFn: () => unknown }
}

describe('Overview monitoring scope', () => {
  it('uses the Tenant monitoring endpoint for ordinary users', async () => {
    mountOverview()
    const options = monitoringQueryOptions()

    expect(unref(options.queryKey)).toEqual(['monitoringSummary', 'overview', 'tenant'])
    await options.queryFn()
    expect(mocks.api).toHaveBeenCalledWith('GET', '/monitoring/summary')
  })

  it('uses and normalizes the global overview endpoint for super-admin', async () => {
    mocks.isSuperAdmin = true
    mocks.monitoring.dispatcher = {
      effective_status: 'error',
      heartbeat_stale: true,
      updated_at: null,
    }
    mocks.monitoring.vulnerabilities = {
      total: 12,
      by_severity: { critical: 5, high: 4, medium: 2, low: 1 },
      by_status: {},
      per_project: [],
    }

    const wrapper = mountOverview()
    const options = monitoringQueryOptions()

    expect(unref(options.queryKey)).toEqual(['monitoringSummary', 'overview', 'admin'])
    await options.queryFn()
    expect(mocks.api).toHaveBeenCalledWith('GET', '/admin/monitoring?profile=overview')
    expect(wrapper.text()).toContain('状态过期')
    expect(wrapper.get('.vuln-chart-center').text()).toContain('12漏洞总数')
    expect(wrapper.get('#vuln-chart-desc').text()).toContain('共 12 个漏洞')
  })
})

describe('Overview vulnerability severity donut', () => {
  it('renders dynamic segments with complete keyboard-readable values', () => {
    const wrapper = mountOverview()
    const segments = wrapper.findAll('.vuln-segment')

    expect(segments).toHaveLength(4)
    expect(segments.map((segment) => segment.attributes('aria-label'))).toEqual([
      '严重：4 个，占 40.0%',
      '高危：3 个，占 30.0%',
      '中危：2 个，占 20.0%',
      '信息：1 个，占 10.0%',
    ])
    expect(segments.every((segment) => segment.attributes('tabindex') === '0')).toBe(true)
    expect(wrapper.get('.vuln-chart-center').text()).toContain('10漏洞总数')
    expect(wrapper.get('#vuln-chart-desc').text()).toContain('共 10 个漏洞')
  })

  it('previews on hover and focus, pins on click, and clears on Escape', async () => {
    const wrapper = mountOverview()
    const critical = wrapper.get('.vuln-segment-critical')
    const high = wrapper.get('.vuln-segment-high')

    await critical.trigger('mouseenter')
    expect(wrapper.get('.vuln-chart-tooltip').text()).toContain('4 个严重 · 40.0%')
    expect(wrapper.get('.vuln-chart-center').text()).toContain('4严重')
    expect(critical.classes()).toContain('is-active')
    expect(critical.attributes('style')).toContain('translate(')
    expect(critical.attributes('style')).toContain('scale(1.025)')

    await wrapper.get('.vuln-chart-graphic').trigger('mouseleave')
    expect(wrapper.find('.vuln-chart-tooltip').exists()).toBe(false)
    expect(wrapper.get('.vuln-chart-center').text()).toContain('10漏洞总数')

    await high.trigger('focus')
    await high.trigger('click')
    await high.trigger('blur')
    expect(high.attributes('aria-pressed')).toBe('true')
    expect(wrapper.get('.vuln-chart-tooltip').text()).toContain('3 个高危 · 30.0%')

    await critical.trigger('mouseenter')
    expect(wrapper.get('.vuln-chart-tooltip').text()).toContain('4 个严重 · 40.0%')
    await wrapper.get('.vuln-chart-graphic').trigger('mouseleave')
    expect(wrapper.get('.vuln-chart-tooltip').text()).toContain('3 个高危 · 30.0%')

    await wrapper.get('.vuln-chart').trigger('keydown', { key: 'Escape' })
    expect(wrapper.find('.vuln-chart-tooltip').exists()).toBe(false)
    expect(high.attributes('aria-pressed')).toBe('false')
    expect(wrapper.get('.vuln-chart-center').text()).toContain('10漏洞总数')
  })

  it('supports keyboard pinning and mirrors interaction in the detail legend', async () => {
    const wrapper = mountOverview()
    const mediumSegment = wrapper.get('.vuln-segment-medium')
    const infoLegend = wrapper.findAll('.vuln-legend-row').at(3)

    await mediumSegment.trigger('keydown', { key: 'Enter' })
    expect(mediumSegment.attributes('aria-pressed')).toBe('true')
    expect(wrapper.get('.vuln-chart-tooltip').attributes('style')).toContain('left:')

    if (!infoLegend) throw new Error('找不到信息严重度图例')
    await infoLegend.trigger('focus')
    expect(wrapper.get('.vuln-chart-center').text()).toContain('1信息')
    await infoLegend.trigger('click')
    await infoLegend.trigger('blur')
    expect(infoLegend.attributes('aria-pressed')).toBe('true')
    expect(mediumSegment.attributes('aria-pressed')).toBe('false')
  })
})
