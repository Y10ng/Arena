import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'
import Checkbox from './Checkbox.vue'

describe('Checkbox', () => {
  it('supports v-model through the native checkbox control', async () => {
    const wrapper = mount(Checkbox, { props: { modelValue: false, label: '选择目标' } })
    const input = wrapper.get<HTMLInputElement>('input')

    await input.setValue(true)

    expect(wrapper.emitted('update:modelValue')).toEqual([[true]])
    expect(input.element.type).toBe('checkbox')
    expect(wrapper.get('label').text()).toBe('选择目标')
  })

  it('forwards an accessible name and native disabled and required states', () => {
    const wrapper = mount(Checkbox, {
      props: { modelValue: true, disabled: true, required: true },
      attrs: { 'aria-label': '选择全部目标', name: 'targets' },
    })
    const input = wrapper.get<HTMLInputElement>('input')

    expect(input.attributes()).toMatchObject({
      'aria-label': '选择全部目标',
      disabled: '',
      name: 'targets',
      required: '',
    })
    expect(input.element.checked).toBe(true)
  })

  it('exposes the native indeterminate state as mixed', async () => {
    const wrapper = mount(Checkbox, {
      props: { modelValue: false, indeterminate: true, label: '部分选择' },
    })
    const input = wrapper.get<HTMLInputElement>('input')

    expect(input.element.indeterminate).toBe(true)
    expect(input.attributes('aria-checked')).toBe('mixed')

    // 模拟浏览器在用户点击时先清除原生 indeterminate，再派发 change。
    input.element.indeterminate = false
    await input.setValue(true)
    expect(wrapper.emitted('update:modelValue')).toEqual([[true]])
    expect(input.element.indeterminate).toBe(true)
    expect(input.attributes('aria-checked')).toBe('mixed')

    await wrapper.setProps({ indeterminate: false })
    expect(input.element.indeterminate).toBe(false)
    expect(input.attributes('aria-checked')).toBeUndefined()
  })

  it('keeps native focus and Space keyboard behavior available', async () => {
    const wrapper = mount(Checkbox, {
      attachTo: document.body,
      props: { modelValue: false, label: '键盘选择' },
    })
    const input = wrapper.get<HTMLInputElement>('input')

    input.element.focus()
    const keydown = new KeyboardEvent('keydown', { key: ' ', bubbles: true, cancelable: true })
    input.element.dispatchEvent(keydown)

    expect(document.activeElement).toBe(input.element)
    expect(keydown.defaultPrevented).toBe(false)
    wrapper.unmount()
  })
})
