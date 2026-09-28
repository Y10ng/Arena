import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { nextTick } from 'vue'
import ToastHost from './ToastHost.vue'
import { useUIStore, type ToastMessage } from '@/stores/ui'

const toast = (
  id: string,
  type: ToastMessage['type'],
  text = `${type} notification`,
): ToastMessage => ({
  id,
  type,
  text,
})

describe('ToastHost', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    localStorage.clear()
  })

  afterEach(() => {
    vi.restoreAllMocks()
    vi.useRealTimers()
  })

  it('renders every tone with appropriate live semantics', () => {
    const store = useUIStore()
    store.toasts = [
      toast('info', 'info'),
      toast('success', 'success'),
      toast('warn', 'warn'),
      toast('danger', 'danger'),
    ]

    const wrapper = mount(ToastHost)

    expect(wrapper.get('[role="region"]').attributes('aria-label')).toBe('通知')
    expect(wrapper.get('.toast-card--info').attributes()).toMatchObject({
      role: 'status',
      'aria-live': 'polite',
      'aria-atomic': 'true',
    })
    expect(wrapper.get('.toast-card--success').attributes('role')).toBe('status')
    expect(wrapper.get('.toast-card--warn').attributes()).toMatchObject({
      role: 'alert',
      'aria-live': 'assertive',
    })
    expect(wrapper.get('.toast-card--danger').attributes('role')).toBe('alert')
  })

  it('removes a toast from its accessible close button', async () => {
    const store = useUIStore()
    store.toasts = [toast('dismiss-me', 'info', '可以关闭的通知')]
    const wrapper = mount(ToastHost)

    await wrapper.get('button[aria-label="关闭通知"]').trigger('click')
    await nextTick()

    expect(store.toasts).toHaveLength(0)
    expect(wrapper.text()).not.toContain('可以关闭的通知')
  })

  it('renders long unbroken and multiline text without truncating content', () => {
    const longText = `${'very-long-path-segment'.repeat(24)}\n第二行通知内容`
    const store = useUIStore()
    store.toasts = [toast('long', 'danger', longText)]

    const text = mount(ToastHost).get('.toast-text')

    expect(text.text()).toContain('very-long-path-segment'.repeat(24))
    expect(text.text()).toContain('第二行通知内容')
  })

  it('does not let a cleared timeout remove a later toast with the same id', () => {
    vi.useFakeTimers()
    vi.spyOn(Math, 'random').mockReturnValue(0.25)
    const store = useUIStore()

    store.showToast('first', 'info', 100)
    const firstId = store.toasts[0]?.id
    if (!firstId) throw new Error('toast id was not created')
    store.removeToast(firstId)
    store.showToast('second', 'success', 200)

    vi.advanceTimersByTime(100)
    expect(store.toasts.map((item) => item.text)).toEqual(['second'])

    vi.advanceTimersByTime(100)
    expect(store.toasts).toHaveLength(0)
  })
})
