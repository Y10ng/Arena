import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import GraphToolbar from './GraphToolbar.vue'

const statusFilters = [
  { value: 'all', label: '全部' },
  { value: 'running', label: '探索中' },
]

function mountToolbar(overrides: Record<string, unknown> = {}) {
  return mount(GraphToolbar, {
    props: {
      searchQuery: '',
      searchMatchCount: 2,
      activeStatusFilter: 'all',
      statusFilters,
      isBranchCollapseEnabled: false,
      isFullscreen: false,
      ...overrides,
    },
  })
}

function buttonByText(wrapper: ReturnType<typeof mountToolbar>, text: string) {
  const button = wrapper.findAll('button').find((candidate) => candidate.text().includes(text))
  if (!button) throw new Error(`找不到按钮：${text}`)
  return button
}

describe('GraphToolbar visual controls', () => {
  it('preserves search updates, Enter search, match count, and status filtering', async () => {
    const wrapper = mountToolbar()
    const search = wrapper.get('input[aria-label="搜索节点内容"]')

    await search.setValue('越权')
    await search.trigger('keyup', { key: 'Enter' })
    await buttonByText(wrapper, '探索中').trigger('click')

    expect(wrapper.get('.search-matches').text()).toBe('2 个匹配')
    expect(wrapper.emitted('update:searchQuery')).toEqual([['越权']])
    expect(wrapper.emitted('runSearch')).toHaveLength(1)
    expect(wrapper.emitted('update:activeStatusFilter')).toEqual([['running']])
    expect(buttonByText(wrapper, '全部').attributes('aria-pressed')).toBe('true')
    expect(wrapper.findAll('.ui-button')).toHaveLength(statusFilters.length + 4)
    expect(wrapper.findAll('.ui-input')).toHaveLength(1)
    expect(wrapper.findAll('.ui-checkbox')).toHaveLength(1)
    expect(wrapper.find('.btn-secondary').exists()).toBe(false)
    expect(wrapper.find('.form-input').exists()).toBe(false)
  })

  it('preserves collapse and toolbar action events through shared controls', async () => {
    const wrapper = mountToolbar()

    await wrapper.get('input[type="checkbox"]').setValue(true)
    await buttonByText(wrapper, '添加提示').trigger('click')
    await buttonByText(wrapper, '适应视口').trigger('click')
    await buttonByText(wrapper, '重置节点').trigger('click')
    await buttonByText(wrapper, '全屏').trigger('click')

    expect(wrapper.emitted('update:isBranchCollapseEnabled')).toEqual([[true]])
    expect(wrapper.emitted('addHint')).toHaveLength(1)
    expect(wrapper.emitted('resetViewport')).toHaveLength(1)
    expect(wrapper.emitted('resetNodePositions')).toHaveLength(1)
    expect(wrapper.emitted('toggleFullscreen')).toHaveLength(1)
  })

  it('reflects fullscreen, collapse, and zero-match states', () => {
    const wrapper = mountToolbar({
      searchMatchCount: 0,
      isFullscreen: true,
      isBranchCollapseEnabled: true,
    })

    expect(wrapper.find('.search-matches').exists()).toBe(false)
    expect(wrapper.text()).not.toContain('布局:')
    expect(buttonByText(wrapper, '退出全屏').exists()).toBe(true)
    expect(wrapper.get<HTMLInputElement>('input[type="checkbox"]').element.checked).toBe(true)
  })
})
