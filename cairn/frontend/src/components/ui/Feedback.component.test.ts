import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'
import Alert from './Alert.vue'
import Badge from './Badge.vue'
import Spinner from './Spinner.vue'
import StatusBadge from './StatusBadge.vue'

describe('Badge', () => {
  it('normalizes warning tone and renders size, dot, and content', () => {
    const wrapper = mount(Badge, {
      props: { tone: 'warn', size: 'lg', dot: true },
      slots: { default: '需关注' },
    })

    expect(wrapper.classes()).toEqual(expect.arrayContaining(['ui-badge--warning', 'ui-badge--lg']))
    expect(wrapper.get('.ui-badge__dot').attributes('aria-hidden')).toBe('true')
    expect(wrapper.text()).toBe('需关注')
  })
})

describe('StatusBadge', () => {
  it.each([
    ['healthy', 'success', '健康'],
    ['failed', 'danger', '失败'],
    ['blocked', 'warning', '阻塞'],
    ['running', 'info', '运行中'],
    ['unknown', 'neutral', '未知'],
  ])('maps %s to the %s badge tone', (status, tone, text) => {
    const wrapper = mount(StatusBadge, { props: { status } })

    expect(wrapper.get('.ui-badge').classes()).toContain(`ui-badge--${tone}`)
    expect(wrapper.text()).toBe(text)
  })

  it('provides status metadata to its slot', () => {
    const wrapper = mount(StatusBadge, {
      props: { status: 'succeeded' },
      slots: {
        default: `<template #default="{ meta }">{{ meta.text }} / {{ meta.tone }}</template>`,
      },
    })

    expect(wrapper.text()).toBe('成功 / good')
  })
})

describe('Alert', () => {
  it.each([
    ['info', 'status'],
    ['success', 'status'],
    ['warning', 'alert'],
    ['danger', 'alert'],
  ] as const)('uses the expected role for %s alerts', (tone, role) => {
    const wrapper = mount(Alert, { props: { tone } })

    expect(wrapper.attributes('role')).toBe(role)
  })

  it('renders its slots and emits dismiss', async () => {
    const wrapper = mount(Alert, {
      props: { title: '提示', dismissible: true },
      slots: {
        icon: '<span data-test="icon">!</span>',
        default: '正文',
        actions: '<button data-test="action">重试</button>',
      },
    })

    expect(wrapper.get('.ui-alert__title').text()).toBe('提示')
    expect(wrapper.get('.ui-alert__icon').attributes('aria-hidden')).toBe('true')
    expect(wrapper.get('.ui-alert__content').text()).toBe('正文')
    expect(wrapper.get('[data-test="action"]').text()).toBe('重试')

    await wrapper.get('button[aria-label="关闭提示"]').trigger('click')
    expect(wrapper.emitted('dismiss')).toHaveLength(1)
  })
})

describe('Spinner', () => {
  it('announces loading by default and supports a custom label', () => {
    const wrapper = mount(Spinner, { props: { label: '正在保存', size: 'lg' } })

    expect(wrapper.attributes('role')).toBe('status')
    expect(wrapper.attributes('aria-label')).toBe('正在保存')
    expect(wrapper.classes()).toContain('ui-spinner--lg')
  })

  it('is hidden from assistive technology when decorative', () => {
    const wrapper = mount(Spinner, { props: { decorative: true } })

    expect(wrapper.attributes('role')).toBeUndefined()
    expect(wrapper.attributes('aria-label')).toBeUndefined()
    expect(wrapper.attributes('aria-hidden')).toBe('true')
  })
})
