import { flushPromises, mount } from '@vue/test-utils'
import { defineComponent } from 'vue'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import FindingsPage from './FindingsPage.vue'

const mocks = vi.hoisted(() => ({
  query: {} as Record<string, string | undefined>,
  replace: vi.fn(),
}))

vi.mock('vue-router', () => ({
  useRoute: () => ({ query: mocks.query }),
  useRouter: () => ({ replace: mocks.replace }),
}))

vi.mock('./VulnerabilityManagement.vue', () => ({
  default: defineComponent({
    template: '<div data-testid="vulnerability-management">漏洞管理内容</div>',
  }),
}))

vi.mock('./ReportManagement.vue', () => ({
  default: defineComponent({ template: '<div data-testid="report-management">报告管理内容</div>' }),
}))

beforeEach(() => {
  Object.keys(mocks.query).forEach((key) => delete mocks.query[key])
  mocks.replace
    .mockReset()
    .mockImplementation(({ query }: { query: Record<string, string | undefined> }) => {
      Object.keys(mocks.query).forEach((key) => delete mocks.query[key])
      Object.assign(mocks.query, query)
      return Promise.resolve()
    })
})

describe('FindingsPage 成果中心 shell', () => {
  it('defaults to vulnerability management and exposes URL-backed tabs', () => {
    const wrapper = mount(FindingsPage)

    expect(wrapper.text()).not.toContain('成果中心')
    expect(wrapper.text()).not.toContain('ENTERPRISE DELIVERABLES')
    expect(wrapper.find('.findings-route-tabs').exists()).toBe(true)
    expect(wrapper.text()).toContain('漏洞管理')
    expect(wrapper.text()).toContain('报告管理')
    expect(wrapper.find('[data-testid="vulnerability-management"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="report-management"]').exists()).toBe(false)
  })

  it('opens report management from the URL and preserves unrelated query values on tab changes', async () => {
    mocks.query.view = 'reports'
    mocks.query.keep = 'yes'
    mocks.query.reportQ = 'old-filter'
    mocks.query.reportId = 'old-report'
    mocks.query.reportProjectId = 'old-project'
    const wrapper = mount(FindingsPage)

    expect(wrapper.find('[data-testid="report-management"]').exists()).toBe(true)
    wrapper.findComponent({ name: 'UiTabs' }).vm.$emit('update:modelValue', 'vulnerabilities')
    await flushPromises()

    expect(mocks.replace).toHaveBeenLastCalledWith({ query: { keep: 'yes' } })
  })

  it('normalizes an invalid view without dropping unrelated query values', async () => {
    mocks.query.view = 'unknown'
    mocks.query.keep = 'yes'
    mount(FindingsPage)
    await flushPromises()

    expect(mocks.replace).toHaveBeenCalledWith({ query: { keep: 'yes' } })
  })
})
