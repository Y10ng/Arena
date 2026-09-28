import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'
import Button from './Button.vue'

const variants = ['primary', 'secondary', 'success', 'danger', 'warning', 'ghost', 'link'] as const

describe('Button', () => {
  it.each(variants)('renders the %s variant', (variant) => {
    const wrapper = mount(Button, { props: { variant } })

    expect(wrapper.classes()).toContain(`ui-button--${variant}`)
  })

  it('defaults to a non-submitting button and renders its slots', () => {
    const wrapper = mount(Button, {
      slots: {
        icon: '<svg data-test="icon" />',
        default: '保存',
      },
    })

    expect(wrapper.attributes('type')).toBe('button')
    expect(wrapper.classes()).toEqual(
      expect.arrayContaining(['ui-button--primary', 'ui-button--md']),
    )
    expect(wrapper.get('[data-test="icon"]')).toBeDefined()
    expect(wrapper.get('.ui-button__icon').attributes('aria-hidden')).toBe('true')
    expect(wrapper.get('.ui-button__content').text()).toBe('保存')
  })

  it('disables the button while loading or when explicitly disabled', async () => {
    const wrapper = mount(Button, { props: { loading: true }, slots: { default: '提交' } })

    expect(wrapper.attributes('disabled')).toBeDefined()
    expect(wrapper.attributes('aria-busy')).toBe('true')
    expect(wrapper.get('.ui-spinner').attributes('aria-hidden')).toBe('true')
    expect(wrapper.find('.ui-button__icon').exists()).toBe(false)

    await wrapper.setProps({ loading: false, disabled: true })

    expect(wrapper.attributes('disabled')).toBeDefined()
    expect(wrapper.attributes('aria-busy')).toBeUndefined()
    expect(wrapper.find('.ui-spinner').exists()).toBe(false)
  })
})
