import { flushPromises, mount } from '@vue/test-utils'
import { h } from 'vue'
import { afterEach, describe, expect, it } from 'vitest'
import Tabs from './Tabs.vue'

const mountedWrappers: Array<ReturnType<typeof mount>> = []

function mountTabs(props: Record<string, unknown> = {}) {
  const wrapper = mount(Tabs, {
    attachTo: document.body,
    props: {
      modelValue: 'form',
      options: [
        { value: 'form', label: '表单编辑', count: 2 },
        { value: 'yaml', label: '原始 YAML' },
        { value: 'disabled', label: '不可用', disabled: true },
      ],
      ...props,
    },
    slots: {
      panel: ({ option }: { option: { label: string } }) => h('p', `${option.label}内容`),
    },
  })
  mountedWrappers.push(wrapper)
  return wrapper
}

afterEach(() => {
  mountedWrappers.splice(0).forEach((wrapper) => wrapper.unmount())
  document.body.innerHTML = ''
})

describe('Tabs', () => {
  it('renders tab semantics, count, disabled state and the active panel', async () => {
    mountTabs()
    await flushPromises()

    const tabs = Array.from(document.body.querySelectorAll<HTMLButtonElement>('[role="tab"]'))
    expect(tabs.map((tab) => tab.textContent?.replace(/\s+/g, ' ').trim())).toEqual([
      '表单编辑2',
      '原始 YAML',
      '不可用',
    ])
    expect(tabs[0]?.getAttribute('aria-selected')).toBe('true')
    expect(tabs[2]?.disabled).toBe(true)

    const panel = document.body.querySelector<HTMLElement>('[role="tabpanel"]')
    expect(panel?.textContent).toContain('表单编辑内容')
    expect(tabs[0]?.getAttribute('aria-controls')).toBe(panel?.id)
  })

  it('emits a string value when an enabled tab is selected', async () => {
    const wrapper = mountTabs()
    await flushPromises()

    const tabs = document.body.querySelectorAll<HTMLButtonElement>('[role="tab"]')
    tabs[1]?.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, button: 0 }))
    await flushPromises()

    expect(wrapper.emitted('update:modelValue')).toContainEqual(['yaml'])
  })

  it('keeps arrow-key navigation non-committing in manual mode', async () => {
    const wrapper = mountTabs({ activationMode: 'manual' })
    await flushPromises()

    const tabs = document.body.querySelectorAll<HTMLButtonElement>('[role="tab"]')
    tabs[0]?.focus()
    tabs[0]?.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowRight', bubbles: true }))
    await flushPromises()

    expect(document.activeElement).toBe(tabs[1])
    expect(wrapper.emitted('update:modelValue')).toBeUndefined()

    tabs[1]?.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }))
    await flushPromises()
    expect(wrapper.emitted('update:modelValue')).toContainEqual(['yaml'])
  })
})
