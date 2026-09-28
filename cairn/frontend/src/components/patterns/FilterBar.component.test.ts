import { mount } from '@vue/test-utils'
import { h } from 'vue'
import { describe, expect, it, vi } from 'vitest'
import FilterBar from './FilterBar.vue'

describe('FilterBar', () => {
  it('names the region and renders fields, status, and actions in semantic order', () => {
    const wrapper = mount(FilterBar, {
      props: { label: '成果筛选' },
      slots: {
        default: '<label data-test="field">严重度 <select><option>全部</option></select></label>',
        status: '<span data-test="status">同步中…</span>',
        actions: '<button data-test="action" type="button">刷新</button>',
      },
    })
    const region = wrapper.get('[role="region"]')

    expect(region.attributes('aria-label')).toBe('成果筛选')
    expect(region.findAll(':scope > div').map((element) => element.classes()[0])).toEqual([
      'filter-bar__fields',
      'filter-bar__status',
      'filter-bar__actions',
    ])
    expect(wrapper.get('[data-test="field"]').text()).toContain('严重度')
    expect(wrapper.get('.filter-bar__status').attributes()).toMatchObject({
      role: 'status',
      'aria-live': 'polite',
    })
    expect(wrapper.get('[data-test="status"]').text()).toBe('同步中…')
    expect(wrapper.get('[data-test="action"]').text()).toBe('刷新')
  })

  it('accepts inherited status sizing without changing the component API', () => {
    const wrapper = mount(FilterBar, {
      props: { label: '项目筛选' },
      attrs: {
        style:
          '--filter-bar-status-min-inline-size: 11em; --filter-bar-status-text-align: end; --filter-bar-status-white-space: nowrap;',
      },
      slots: {
        default: '<input data-test="field" />',
        status: '<span>正在获取最新项目数据…</span>',
      },
    })

    expect(wrapper.attributes('style')).toContain('--filter-bar-status-min-inline-size: 11em')
    expect(wrapper.get('.filter-bar__status').attributes()).toMatchObject({
      role: 'status',
      'aria-live': 'polite',
    })
  })

  it('keeps responsive layout hooks and omits unused optional regions', () => {
    const wrapper = mount(FilterBar, {
      props: { label: '项目筛选' },
      slots: { default: '<input data-test="field" />' },
    })

    expect(wrapper.classes()).toContain('filter-bar')
    expect(wrapper.get('.filter-bar__fields')).toBeDefined()
    expect(wrapper.find('.filter-bar__status').exists()).toBe(false)
    expect(wrapper.find('.filter-bar__actions').exists()).toBe(false)
  })

  it('does not swallow child control events', async () => {
    const onInput = vi.fn()
    const onRefresh = vi.fn()
    const wrapper = mount(FilterBar, {
      props: { label: '项目筛选' },
      slots: {
        default: () => h('input', { 'data-test': 'field', onInput }),
        actions: () =>
          h('button', { 'data-test': 'refresh', type: 'button', onClick: onRefresh }, '刷新'),
      },
    })

    await wrapper.get('[data-test="field"]').trigger('input')
    await wrapper.get('[data-test="refresh"]').trigger('click')

    expect(onInput).toHaveBeenCalledOnce()
    expect(onRefresh).toHaveBeenCalledOnce()
  })
})
