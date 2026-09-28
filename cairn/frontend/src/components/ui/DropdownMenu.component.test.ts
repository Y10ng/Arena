import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, describe, expect, it } from 'vitest'
import DropdownMenu from './DropdownMenu.vue'

const mountedWrappers: Array<ReturnType<typeof mount>> = []

function mountMenu(props: Record<string, unknown> = {}) {
  const wrapper = mount(DropdownMenu, {
    attachTo: document.body,
    props: {
      items: [
        { value: 'edit', label: '编辑', shortcut: 'E' },
        { value: 'archive', label: '归档', disabled: true },
        { value: 'delete', label: '删除', separatorBefore: true, tone: 'danger' },
      ],
      ...props,
    },
    slots: {
      trigger: '<button type="button">项目工具</button>',
    },
  })
  mountedWrappers.push(wrapper)
  return wrapper
}

afterEach(() => {
  mountedWrappers.splice(0).forEach((wrapper) => wrapper.unmount())
  document.body.innerHTML = ''
})

describe('DropdownMenu', () => {
  it('renders menu items through a portal and emits the selected value', async () => {
    const wrapper = mountMenu({ open: true, label: '操作' })
    await flushPromises()

    expect(wrapper.find('[role="menu"]').exists()).toBe(false)
    expect(document.body.querySelector('[role="menu"]')).not.toBeNull()
    expect(document.body.textContent).toContain('操作')

    const items = Array.from(document.body.querySelectorAll<HTMLElement>('[role="menuitem"]'))
    expect(items.map((item) => item.textContent?.replace(/\s+/g, ' ').trim())).toEqual([
      '编辑E',
      '归档',
      '删除',
    ])
    expect(items[1]?.getAttribute('data-disabled')).not.toBeNull()

    items[2]?.click()
    await flushPromises()

    expect(wrapper.emitted('select')).toContainEqual(['delete'])
  })

  it('opens from the trigger and restores focus after Escape', async () => {
    mountMenu()
    const trigger = document.body.querySelector<HTMLButtonElement>('button')
    trigger?.focus()
    trigger?.click()
    await flushPromises()

    const menu = document.body.querySelector<HTMLElement>('[role="menu"]')
    expect(menu).not.toBeNull()

    menu?.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))
    await flushPromises()

    expect(document.body.querySelector('[role="menu"]')).toBeNull()
    expect(document.activeElement).toBe(trigger)
  })
})
