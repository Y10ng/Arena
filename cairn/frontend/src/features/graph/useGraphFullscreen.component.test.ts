import { flushPromises, mount } from '@vue/test-utils'
import { defineComponent, h, ref } from 'vue'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { useGraphFullscreen } from './useGraphFullscreen'

const wrappers: Array<ReturnType<typeof mount>> = []

function pressEscape() {
  document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))
}

afterEach(() => {
  wrappers.splice(0).forEach((wrapper) => wrapper.unmount())
  document.body.innerHTML = ''
})

describe('useGraphFullscreen', () => {
  it('keeps fixed fullscreen active until inline overlays are closed', async () => {
    const fitViewport = vi.fn()
    let controls!: ReturnType<typeof useGraphFullscreen>
    const Host = defineComponent({
      setup() {
        const graphPageElement = ref<HTMLElement | null>(null)
        controls = useGraphFullscreen(graphPageElement, fitViewport)
        return () => h('section', { ref: graphPageElement })
      },
    })
    const wrapper = mount(Host, { attachTo: document.body })
    wrappers.push(wrapper)
    await flushPromises()

    await controls.toggleFullscreen()
    expect(controls.isGraphFullscreen.value).toBe(true)

    const dialog = document.createElement('div')
    dialog.setAttribute('role', 'dialog')
    wrapper.element.appendChild(dialog)
    pressEscape()
    await flushPromises()
    expect(controls.isGraphFullscreen.value).toBe(true)

    dialog.remove()
    pressEscape()
    await flushPromises()
    expect(controls.isGraphFullscreen.value).toBe(false)
  })
})
