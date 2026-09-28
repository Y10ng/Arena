import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { flushPromises, mount } from '@vue/test-utils'
import { defineComponent, ref } from 'vue'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import UiDialog from './Dialog.vue'
import UiSelect from './Select.vue'

const options = [
  { value: 'model-a', label: 'Anthropic Claude A' },
  { value: 'model-b', label: 'Anthropic Claude B' },
  { value: 'model-disabled', label: '不可用 Model', disabled: true },
]

const mountedWrappers: Array<ReturnType<typeof mount>> = []
const tokensCss = readFileSync(resolve(process.cwd(), 'src/design-system/tokens.css'), 'utf8')

function mountSelect(props: Record<string, unknown>) {
  const wrapper = mount(UiSelect, {
    attachTo: document.body,
    props: {
      modelValue: '',
      options,
      ...props,
    },
  })
  mountedWrappers.push(wrapper)
  return wrapper
}

beforeEach(() => {
  HTMLElement.prototype.hasPointerCapture = () => false
  HTMLElement.prototype.setPointerCapture = () => undefined
  HTMLElement.prototype.releasePointerCapture = () => undefined
})

afterEach(() => {
  mountedWrappers.splice(0).forEach((wrapper) => wrapper.unmount())
  document.body.innerHTML = ''
})

describe('Select', () => {
  it('renders placeholder and invalid semantics on the trigger', async () => {
    mountSelect({
      placeholder: '选择 Model',
      invalid: true,
      ariaLabel: 'Worker Model',
    })
    await flushPromises()

    const trigger = document.body.querySelector<HTMLButtonElement>('[role="combobox"]')
    expect(trigger?.textContent).toContain('选择 Model')
    expect(trigger?.getAttribute('aria-invalid')).toBe('true')
    expect(trigger?.getAttribute('aria-label')).toBe('Worker Model')
  })

  it('renders options through a portal and emits the selected string value', async () => {
    const wrapper = mountSelect({ open: true })
    await flushPromises()

    expect(wrapper.find('[role="listbox"]').exists()).toBe(false)
    const items = Array.from(document.body.querySelectorAll<HTMLElement>('[role="option"]'))
    expect(items.map((item) => item.textContent?.trim())).toEqual([
      'Anthropic Claude A',
      'Anthropic Claude B',
      '不可用 Model',
    ])
    expect(items[2]?.getAttribute('data-disabled')).not.toBeNull()

    items[1]?.dispatchEvent(new PointerEvent('pointerup', { bubbles: true }))
    items[1]?.click()
    await flushPromises()

    expect(wrapper.emitted('update:modelValue')).toContainEqual(['model-b'])
  })

  it('does not open when disabled', async () => {
    const wrapper = mountSelect({ disabled: true })
    await flushPromises()

    const trigger = document.body.querySelector<HTMLButtonElement>('[role="combobox"]')
    expect(trigger?.disabled).toBe(true)
    trigger?.click()
    await flushPromises()

    expect(document.body.querySelector('[role="listbox"]')).toBeNull()
    expect(wrapper.emitted('update:open')).toBeUndefined()
  })

  it.each(['md', 'fullscreen'] as const)(
    'selects a portalled option inside a %s dialog',
    async (size) => {
      const Host = defineComponent({
        components: { UiDialog, UiSelect },
        setup() {
          return { options, selected: ref('model-a'), size }
        },
        template: `
          <UiDialog :open="true" title="选择模型" description="测试模态框内下拉" :size="size">
            <UiSelect v-model="selected" :options="options" />
          </UiDialog>
        `,
      })
      const wrapper = mount(Host, { attachTo: document.body })
      mountedWrappers.push(wrapper)
      await flushPromises()

      const trigger = document.body.querySelector<HTMLButtonElement>('[role="combobox"]')
      trigger?.dispatchEvent(
        new PointerEvent('pointerdown', { bubbles: true, button: 0, pointerType: 'mouse' }),
      )
      trigger?.click()
      await flushPromises()

      const items = Array.from(document.body.querySelectorAll<HTMLElement>('[role="option"]'))
      expect(items[1]?.textContent).toContain('Anthropic Claude B')
      items[1]?.dispatchEvent(new PointerEvent('pointerup', { bubbles: true }))
      items[1]?.click()
      await flushPromises()

      expect(
        document.body.querySelector<HTMLButtonElement>('[role="combobox"]')?.textContent,
      ).toContain('Anthropic Claude B')
    },
  )

  it('keeps portalled child overlays above modal and fullscreen layers', () => {
    expect(tokensCss).toContain('--z-modal: 50;')
    expect(tokensCss).toContain('--z-fullscreen: 80;')
    expect(tokensCss).toContain('--z-overlay-child: 90;')
  })
})
