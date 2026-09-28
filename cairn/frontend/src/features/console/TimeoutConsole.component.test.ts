import { mount } from '@vue/test-utils'
import { computed, ref } from 'vue'
import { afterEach, describe, expect, it, vi } from 'vitest'

describe('TimeoutConsole pilot migration', () => {
  afterEach(() => {
    vi.doUnmock('./useTimeoutConsole')
    vi.resetModules()
  })

  it('keeps numeric v-model validation and save behavior on the real controls', async () => {
    const timeoutForm = ref({ intent_timeout: 30, reason_timeout: 60 })
    const saveTimeouts = vi.fn()
    const timeoutErrors = computed(() => ({
      intent: timeoutForm.value.intent_timeout >= 5 ? '' : '不能小于 5 秒',
      reason: timeoutForm.value.reason_timeout >= 5 ? '' : '不能小于 5 秒',
    }))

    vi.doMock('./useTimeoutConsole', () => ({
      useTimeoutConsole: () => ({
        formatError: (error: unknown) => String(error),
        isLoadingTimeouts: ref(false),
        isSavingTimeouts: ref(false),
        isTimeoutsError: ref(false),
        isTimeoutsValid: computed(() => !timeoutErrors.value.intent && !timeoutErrors.value.reason),
        saveTimeouts,
        timeoutErrors,
        timeoutForm,
        timeoutsError: ref(null),
      }),
    }))

    const { default: TimeoutConsole } = await import('./TimeoutConsole.vue')
    const wrapper = mount(TimeoutConsole)
    const inputs = wrapper.findAll('input')
    const saveButton = wrapper.get('button')

    expect(inputs).toHaveLength(2)
    expect(inputs[0]?.attributes()).toMatchObject({
      min: '5',
      required: '',
      step: '1',
      type: 'number',
    })

    await inputs[0]?.setValue('4')

    expect(timeoutForm.value.intent_timeout).toBe(4)
    expect(wrapper.text()).toContain('不能小于 5 秒')
    expect(saveButton.attributes('disabled')).toBeDefined()

    await inputs[0]?.setValue('45')
    await saveButton.trigger('click')

    expect(timeoutForm.value.intent_timeout).toBe(45)
    expect(saveTimeouts).toHaveBeenCalledOnce()
  })
})
