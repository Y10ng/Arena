import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'
import Card from './Card.vue'
import EmptyState from './EmptyState.vue'

describe('Card', () => {
  it('defaults to a section with medium padding', () => {
    const wrapper = mount(Card, { slots: { default: '内容' } })

    expect(wrapper.element.tagName).toBe('SECTION')
    expect(wrapper.classes()).toContain('ui-card--padding-md')
    expect(wrapper.text()).toBe('内容')
  })

  it.each(['none', 'sm', 'lg'] as const)('renders %s padding', (padding) => {
    const wrapper = mount(Card, { props: { padding } })

    expect(wrapper.classes()).toContain(`ui-card--padding-${padding}`)
  })

  it('supports a polymorphic root and forwards attributes', () => {
    const wrapper = mount(Card, {
      props: { as: 'article' },
      attrs: { id: 'summary', 'aria-label': '摘要' },
    })

    expect(wrapper.element.tagName).toBe('ARTICLE')
    expect(wrapper.attributes()).toMatchObject({ id: 'summary', 'aria-label': '摘要' })
  })
})

describe('EmptyState', () => {
  it('renders and connects all content slots', () => {
    const wrapper = mount(EmptyState, {
      slots: {
        icon: '<svg data-test="icon" />',
        title: '<span>暂无项目</span>',
        description: '<span>创建第一个项目</span>',
        default: '<p data-test="content">可从模板开始</p>',
        actions: '<button data-test="action">新建项目</button>',
      },
    })
    const title = wrapper.get('.ui-empty-state__title')

    expect(wrapper.attributes('aria-labelledby')).toBe(title.attributes('id'))
    expect(wrapper.get('.ui-empty-state__icon').attributes('aria-hidden')).toBe('true')
    expect(title.text()).toBe('暂无项目')
    expect(wrapper.get('.ui-empty-state__description').text()).toBe('创建第一个项目')
    expect(wrapper.get('[data-test="content"]').text()).toBe('可从模板开始')
    expect(wrapper.get('[data-test="action"]').text()).toBe('新建项目')
  })

  it('omits optional regions when no props or slots are provided', () => {
    const wrapper = mount(EmptyState)

    expect(wrapper.attributes('aria-labelledby')).toBeUndefined()
    expect(wrapper.find('.ui-empty-state__title').exists()).toBe(false)
    expect(wrapper.find('.ui-empty-state__description').exists()).toBe(false)
    expect(wrapper.find('.ui-empty-state__content').exists()).toBe(false)
    expect(wrapper.find('.ui-empty-state__actions').exists()).toBe(false)
  })
})
