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

beforeEach(() => {
  Object.keys(mocks.query).forEach((key) => delete mocks.query[key])
  mocks.replace.mockReset()
})

describe('FindingsPage 漏洞入口', () => {
  it('only mounts vulnerability management without business-switching tabs', () => {
    const wrapper = mount(FindingsPage)

    expect(wrapper.get('h1').text()).toBe('漏洞')
    expect(wrapper.find('[data-testid="vulnerability-management"]').exists()).toBe(true)
    expect(wrapper.find('[role="tablist"]').exists()).toBe(false)
    expect(wrapper.text()).not.toContain('报告管理')
  })

  it('does not interpret legacy view or alter composite vulnerability deep links', async () => {
    const query = {
      view: 'reports',
      vulnerabilityProjectId: 'project-a',
      vulnerabilityId: 'vulnerability-a',
      vulnerabilityQ: '保留筛选',
      keep: 'yes',
    }
    Object.assign(mocks.query, query)
    const wrapper = mount(FindingsPage)
    await flushPromises()

    expect(wrapper.find('[data-testid="vulnerability-management"]').exists()).toBe(true)
    expect(mocks.replace).not.toHaveBeenCalled()
    expect(mocks.query).toEqual(query)
  })
})
