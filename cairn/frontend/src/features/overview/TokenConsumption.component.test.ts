import { mount } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

beforeEach(() => vi.stubGlobal('ResizeObserver', class { observe() {} disconnect() {} }))
afterEach(() => vi.unstubAllGlobals())
import type { ConsumptionOverview, ConsumptionSummary } from '@/api/types/consumption'
import TokenConsumption from './TokenConsumption.vue'

const empty: ConsumptionSummary = {
  tokens: 0, responses: 0, partial_responses: 0, unpriced_responses: 0, amounts: [],
  usage: { input_tokens: null, output_tokens: null, cache_read_tokens: null, cache_write_tokens: null },
}
const measured: ConsumptionSummary = {
  ...empty, tokens: 15, responses: 1, partial_responses: 1, unpriced_responses: 1,
  usage: { ...empty.usage, input_tokens: 10, output_tokens: 5 },
  amounts: [{ currency: 'USD', amount: '0.000000000001' }, { currency: 'CNY', amount: '0.0200' }],
}
function data(): ConsumptionOverview {
  return {
    scope: 'tenant', timezone: 'Asia/Shanghai', enabled_at: '2026-11-04T00:00:00Z',
    generated_at: '2026-11-04T10:00:00Z', start: '2026-10-21T16:00:00Z', end: '2026-11-04T16:00:00Z',
    coverage: 'recorded_only', total: measured, today: { ...measured, date: '2026-11-04', coverage: 'partial' },
    undated: { ...empty, responses: 2 },
    days: [{ ...empty, date: '2026-11-03', coverage: 'unavailable' }, { ...measured, date: '2026-11-04', coverage: 'partial' }],
    models: [{ ...measured, model: { display_name: '合成模型', model_revision: 2 }, source: 'agent_chat' }],
  }
}
describe('TokenConsumption', () => {
  it('keeps missing days unknown and exposes identical keyboard, hover and table values', async () => {
    const wrapper = mount(TokenConsumption, { props: { data: data() } })
    expect(wrapper.findAll('.token-dot')).toHaveLength(1)
    expect(wrapper.get('.token-line').attributes('d')).not.toContain('L')
    const targets = wrapper.findAll('g[tabindex]')
    await targets[1]!.trigger('focus')
    const keyboard = wrapper.get('.token-tooltip').text()
    expect(keyboard).toContain('15 Token')
    expect(keyboard).toContain('USD 0.000000000001 · CNY 0.02')
    await targets[1]!.trigger('blur')
    await targets[1]!.trigger('pointermove')
    expect(wrapper.get('.token-tooltip').text()).toBe(keyboard)
    await wrapper.get('button').trigger('click')
    expect(wrapper.findAll('table')).toHaveLength(1)
    expect(wrapper.find('.model-usage').exists()).toBe(true)
    expect(wrapper.findAll('table')[0]!.text()).toContain('未记录')
    expect(wrapper.get('.coverage-note summary').text()).toContain('2 条历史汇总无发生日期')
    await wrapper.get('.coverage-note summary').trigger('click')
    expect(wrapper.get('.coverage-note').attributes('open')).toBeDefined()
    expect(wrapper.get('.coverage-note').text()).toContain('无日期历史汇总仅计入总量')
    expect(wrapper.get('.coverage-note').text()).toContain('非完整账单')
    expect(wrapper.text()).toContain('ChatBot')
    expect(wrapper.get('.model-usage').text()).toContain('1 次部分计量')
    expect(wrapper.get('.model-usage').text()).toContain('1 次未定价')
    wrapper.unmount()
  })
  it('sorts model rows by recorded tokens without changing the input or converting amounts', async () => {
    const source = data()
    source.models.push({ ...measured, tokens: 25, model: { display_name: '另一合成模型' }, source: 'model_test' })
    const wrapper = mount(TokenConsumption, { props: { data: source } })
    expect(wrapper.find('.model-usage').exists()).toBe(false)
    await wrapper.get('button').trigger('click')
    expect(wrapper.get('.model-usage li').text()).toContain('另一合成模型')
    await wrapper.get('button[aria-label="按 Token 用量升序排列"]').trigger('click')
    expect(wrapper.get('.model-usage li').text()).toContain('合成模型ChatBot')
    expect(wrapper.find('button[aria-label="按 Token 用量降序排列"]').exists()).toBe(true)
    expect(source.models[0]!.tokens).toBe(15)
    expect(wrapper.get('.model-usage').text()).toContain('USD 0.000000000001 · CNY 0.02')
    wrapper.unmount()
  })
  it('does not draw an invented zero trend for an empty dataset', () => {
    const source = data()
    source.days = source.days.map(day => ({ ...empty, date: day.date, coverage: 'partial' }))
    source.models = []
    const wrapper = mount(TokenConsumption, { props: { data: source } })
    expect(wrapper.find('svg[role="group"]').exists()).toBe(false)
    expect(wrapper.text()).toContain('这 14 天尚无可绘制的用量记录')
    wrapper.unmount()
  })
})
