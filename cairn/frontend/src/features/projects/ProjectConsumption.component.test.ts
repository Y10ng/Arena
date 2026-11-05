import { flushPromises, mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'
import type { ConsumptionSummary } from '@/api/types/consumption'
import ProjectConsumption from './ProjectConsumption.vue'

const summary = (changes: Partial<ConsumptionSummary> = {}): ConsumptionSummary => ({
  tokens: 0, responses: 0, partial_responses: 0, unpriced_responses: 0, amounts: [],
  usage: { input_tokens: null, output_tokens: null, cache_read_tokens: null, cache_write_tokens: null },
  ...changes,
})

describe('ProjectConsumption', () => {
  it('区分未加载、没有记录、全未知响应与已知零', async () => {
    const wrapper = mount(ProjectConsumption)
    expect(wrapper.text()).toBe('未加载')
    await wrapper.setProps({ consumption: summary() })
    expect(wrapper.text()).toBe('未记录')
    expect(wrapper.get('.project-consumption').attributes('aria-label')).toContain('无响应记录')
    await wrapper.setProps({ consumption: summary({ responses: 2, partial_responses: 2 }) })
    expect(wrapper.text()).toBe('未记录')
    expect(wrapper.get('.project-consumption').attributes('aria-label')).toContain('响应计量全未知')
    await wrapper.setProps({ consumption: summary({ responses: 1, usage: { input_tokens: 0, output_tokens: 0, cache_read_tokens: 0, cache_write_tokens: 0 } }) })
    expect(wrapper.text()).toBe('0')
    expect(wrapper.get('.project-consumption').attributes('aria-label')).toBe('累计 Token：0 Token')
    wrapper.unmount()
  })

  it('部分小计紧凑显示，完整数字与计量缺口通过键盘焦点可达', async () => {
    const wrapper = mount(ProjectConsumption, { attachTo: document.body, props: { consumption: summary({
      tokens: 1_234_567, responses: 3, partial_responses: 2, unobserved_bindings: 1,
      usage: { input_tokens: 1_234_567, output_tokens: null, cache_read_tokens: null, cache_write_tokens: null },
    }) } })
    expect(wrapper.text()).toContain('部分')
    expect(wrapper.get('.project-consumption').attributes('tabindex')).toBe('0')
    const trigger = wrapper.get('.project-consumption')
    ;(trigger.element as HTMLElement).focus()
    await flushPromises()
    expect(document.activeElement).toBe(trigger.element)
    expect(wrapper.get('.project-consumption').attributes('aria-label')).toBe('累计 Token：1,234,567 Token；2 条响应部分计量；1 个绑定尚无回执')
    expect(document.querySelector('.ui-tooltip')?.textContent).toContain('1,234,567 Token')
    expect(document.querySelector('.ui-tooltip')?.textContent).toContain('1 个绑定尚无回执')
    wrapper.unmount()
  })
})
