import { mount } from '@vue/test-utils'
import { afterEach, describe, expect, it, vi } from 'vitest'
import IconButton from './IconButton.vue'
import Skeleton from './Skeleton.vue'
import Textarea from './Textarea.vue'
import Tooltip from './Tooltip.vue'

const buttonVariants = [
  'primary',
  'secondary',
  'success',
  'danger',
  'warning',
  'ghost',
  'link',
] as const

afterEach(() => {
  document.body.innerHTML = ''
})

describe('IconButton', () => {
  it.each(buttonVariants)('supports the Button %s variant', (variant) => {
    const wrapper = mount(IconButton, {
      props: { label: '编辑', variant },
      slots: { default: '<svg data-test="icon" />' },
    })

    expect(wrapper.classes()).toContain(`ui-icon-button--${variant}`)
    expect(wrapper.attributes('aria-label')).toBe('编辑')
    expect(wrapper.get('[data-test="icon"]')).toBeDefined()
  })

  it('requires a nonempty label and forwards native button attributes', () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => undefined)
    const wrapper = mount(IconButton, {
      props: { label: ' ' },
      attrs: { name: 'refresh', form: 'toolbar', 'data-test': 'refresh' },
    })

    expect(warn.mock.calls[0]?.[0]).toContain('custom validator check failed')
    expect(wrapper.attributes()).toMatchObject({
      name: 'refresh',
      form: 'toolbar',
      'data-test': 'refresh',
      type: 'button',
    })
    warn.mockRestore()
  })

  it('disables itself and announces loading state', () => {
    const wrapper = mount(IconButton, { props: { label: '刷新', loading: true } })

    expect(wrapper.attributes('disabled')).toBeDefined()
    expect(wrapper.attributes('aria-busy')).toBe('true')
    expect(wrapper.get('.ui-spinner').attributes('aria-hidden')).toBe('true')
  })
})

describe('Textarea', () => {
  it('supports string v-model, rows, invalid state, resize, and native attributes', async () => {
    const wrapper = mount(Textarea, {
      props: { modelValue: '', rows: 6, invalid: true, resize: 'none' },
      attrs: { name: 'notes', placeholder: '补充说明' },
    })

    await wrapper.get('textarea').setValue('details')

    expect(wrapper.emitted('update:modelValue')).toEqual([['details']])
    expect(wrapper.attributes()).toMatchObject({
      rows: '6',
      name: 'notes',
      placeholder: '补充说明',
      'aria-invalid': 'true',
    })
    expect(wrapper.classes()).toContain('ui-textarea--resize-none')
  })

  it('exposes focus', () => {
    const wrapper = mount(Textarea, {
      props: { modelValue: '' },
      attachTo: document.body,
    })

    wrapper.vm.focus()

    expect(document.activeElement).toBe(wrapper.element)
    wrapper.unmount()
  })
})

describe('Skeleton', () => {
  it.each(['text', 'rect', 'circle'] as const)('renders the %s shape decoratively', (shape) => {
    const wrapper = mount(Skeleton, { props: { shape } })

    expect(wrapper.classes()).toContain(`ui-skeleton--${shape}`)
    expect(wrapper.attributes('aria-hidden')).toBe('true')
    expect(wrapper.attributes('role')).toBeUndefined()
  })

  it('announces nondecorative loading content', () => {
    const wrapper = mount(Skeleton, {
      props: { decorative: false, label: '正在加载项目' },
    })

    expect(wrapper.attributes('role')).toBe('status')
    expect(wrapper.attributes('aria-label')).toBe('正在加载项目')
    expect(wrapper.attributes('aria-hidden')).toBeUndefined()
  })
})

describe('Tooltip', () => {
  it('keeps its trigger keyboard-focusable and opens content on focus', async () => {
    const wrapper = mount(Tooltip, {
      props: { content: '刷新数据', side: 'right', delay: 0 },
      slots: { default: '<button type="button">刷新</button>' },
      attachTo: document.body,
    })
    const trigger = wrapper.get('button')

    expect(trigger.attributes('tabindex')).toBeUndefined()
    await trigger.trigger('focus')
    await new Promise((resolve) => window.setTimeout(resolve, 0))

    const content = document.body.querySelector('.ui-tooltip')
    expect(content?.textContent).toContain('刷新数据')
    expect(content?.getAttribute('data-side')).toBe('right')
    wrapper.unmount()
  })

  it('does not open when disabled and supports the content slot', async () => {
    const wrapper = mount(Tooltip, {
      props: { disabled: true, delay: 0 },
      slots: {
        default: '<button type="button">删除</button>',
        content: '<strong>不可删除</strong>',
      },
      attachTo: document.body,
    })

    await wrapper.get('button').trigger('focus')
    await new Promise((resolve) => window.setTimeout(resolve, 0))

    expect(document.body.querySelector('.ui-tooltip')).toBeNull()
    wrapper.unmount()
  })
})
