import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, describe, expect, it } from 'vitest'
import Popover from './Popover.vue'

const mountedWrappers: Array<ReturnType<typeof mount>> = []

function mountPopover(props: Record<string, unknown> = {}) {
  const wrapper = mount(Popover, {
    attachTo: document.body,
    props,
    slots: {
      trigger: '<button type="button">打开筛选</button>',
      default: '<div data-testid="popover-body">筛选内容</div>',
    },
  })
  mountedWrappers.push(wrapper)
  return wrapper
}

afterEach(() => {
  mountedWrappers.splice(0).forEach((wrapper) => wrapper.unmount())
  document.body.innerHTML = ''
})

describe('Popover', () => {
  it('renders interactive content through a portal', async () => {
    const wrapper = mountPopover({ open: true, size: 'lg' })
    await flushPromises()

    expect(wrapper.find('[data-testid="popover-body"]').exists()).toBe(false)
    const content = document.body.querySelector<HTMLElement>('[data-testid="popover-body"]')
    expect(content?.textContent).toBe('筛选内容')
    expect(content?.parentElement?.classList.contains('ui-popover__content--lg')).toBe(true)
  })

  it('requests dismissal on Escape when dismissible', async () => {
    const wrapper = mountPopover({ open: true })
    await flushPromises()

    const content = document.body.querySelector<HTMLElement>(
      '[data-testid="popover-body"]',
    )?.parentElement
    content?.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))
    await flushPromises()

    expect(wrapper.emitted('update:open')).toContainEqual([false])
  })

  it('blocks Escape dismissal when dismissible is false', async () => {
    const wrapper = mountPopover({ open: true, dismissible: false })
    await flushPromises()

    const content = document.body.querySelector<HTMLElement>(
      '[data-testid="popover-body"]',
    )?.parentElement
    content?.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))
    await flushPromises()

    expect(wrapper.emitted('update:open')).toBeUndefined()
    expect(document.body.querySelector('[data-testid="popover-body"]')).not.toBeNull()
  })
})
