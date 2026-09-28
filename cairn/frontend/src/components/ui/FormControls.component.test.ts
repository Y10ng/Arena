import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'
import FormField from './FormField.vue'
import Input from './Input.vue'

describe('Input', () => {
  it('supports string v-model and forwards input attributes', async () => {
    const wrapper = mount(Input, {
      props: { modelValue: '' },
      attrs: {
        name: 'username',
        placeholder: '用户名',
        autocomplete: 'username',
        'data-test': 'username',
      },
    })

    await wrapper.get('input').setValue('alice')

    expect(wrapper.emitted('update:modelValue')).toEqual([['alice']])
    expect(wrapper.attributes()).toMatchObject({
      name: 'username',
      placeholder: '用户名',
      autocomplete: 'username',
      'data-test': 'username',
    })
  })

  it('supports numeric v-model including an empty value', async () => {
    const wrapper = mount(Input, { props: { modelValue: 1, type: 'number' } })

    expect(wrapper.props('type')).toBe('number')
    expect(wrapper.attributes('type')).toBe('number')
    await wrapper.get('input').setValue('42.5')
    await wrapper.get('input').setValue('')

    expect(wrapper.emitted('update:modelValue')).toEqual([[42.5], [null]])
  })

  it('exposes invalid state accessibly', () => {
    const wrapper = mount(Input, { props: { modelValue: 'bad', invalid: true } })

    expect(wrapper.classes()).toContain('ui-input--invalid')
    expect(wrapper.attributes('aria-invalid')).toBe('true')
  })
})

describe('FormField', () => {
  it('connects explicit control, hint, and error IDs', () => {
    const wrapper = mount(FormField, {
      props: {
        for: 'email',
        label: '邮箱',
        hint: '仅用于通知',
        error: '邮箱无效',
        required: true,
      },
      slots: {
        default: `<template #default="{ controlAttrs }"><input v-bind="controlAttrs" /></template>`,
      },
    })

    expect(wrapper.get('label').attributes('for')).toBe('email')
    expect(wrapper.get('input').attributes()).toMatchObject({
      id: 'email',
      'aria-describedby': 'email-hint email-error',
      'aria-invalid': 'true',
      required: '',
    })
    expect(wrapper.get('#email-hint').text()).toBe('仅用于通知')
    expect(wrapper.get('#email-error').text()).toBe('邮箱无效')
  })

  it('generates IDs and renders scoped label and error slots', () => {
    const wrapper = mount(FormField, {
      slots: {
        label: `<template #label="{ id }"><span data-test="label">Label for {{ id }}</span></template>`,
        default: `<template #default="{ id, describedBy, invalid }"><input :id="id" :aria-describedby="describedBy" :data-invalid="invalid" /></template>`,
        error: '<strong data-test="error">自定义错误</strong>',
      },
    })
    const input = wrapper.get('input')
    const id = input.attributes('id')

    expect(id).toMatch(/^field-/)
    expect(wrapper.get('label').attributes('for')).toBe(id)
    expect(wrapper.get('[data-test="label"]').text()).toContain(id)
    expect(input.attributes('aria-describedby')).toBe(`${id}-error`)
    expect(input.attributes('data-invalid')).toBe('true')
    expect(wrapper.get('.ui-form-field__error').attributes('id')).toBe(`${id}-error`)
    expect(wrapper.get('[data-test="error"]').text()).toBe('自定义错误')
  })
})
