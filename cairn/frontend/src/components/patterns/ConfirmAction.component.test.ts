import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, describe, expect, it } from 'vitest'
import ConfirmAction from './ConfirmAction.vue'

const wrappers: Array<ReturnType<typeof mount>> = []

afterEach(() => {
  wrappers.splice(0).forEach((wrapper) => wrapper.unmount())
  document.body.innerHTML = ''
})

const mountConfirm = (props: InstanceType<typeof ConfirmAction>['$props']) => {
  const wrapper = mount(ConfirmAction, {
    props,
    attachTo: document.body,
  })
  wrappers.push(wrapper)
  return wrapper
}

describe('ConfirmAction', () => {
  it('focuses cancel and emits confirm without implicitly closing', async () => {
    const wrapper = mountConfirm({
      open: true,
      title: '撤销全部会话',
      description: '用户需要重新登录。',
      confirmLabel: '撤销会话',
      cancelLabel: '保留会话',
      tone: 'danger',
    })
    await flushPromises()

    const cancel = Array.from(document.body.querySelectorAll<HTMLButtonElement>('button')).find(
      (button) => button.textContent?.trim() === '保留会话',
    )
    const confirm = Array.from(document.body.querySelectorAll<HTMLButtonElement>('button')).find(
      (button) => button.textContent?.trim() === '撤销会话',
    )

    expect(document.activeElement).toBe(cancel)
    expect(confirm?.classList.contains('ui-button--danger')).toBe(true)
    confirm?.click()
    await flushPromises()

    expect(wrapper.emitted('confirm')).toHaveLength(1)
    expect(wrapper.emitted('update:open')).toBeUndefined()
    expect(document.body.querySelector('[role="alertdialog"]')).not.toBeNull()
  })

  it('keeps loading confirmations open and disables every dismissal path', async () => {
    const wrapper = mountConfirm({
      open: true,
      title: '正在删除',
      description: '请等待删除完成。',
      loading: true,
    })
    await flushPromises()

    const buttons = Array.from(document.body.querySelectorAll<HTMLButtonElement>('button'))
    expect(buttons).toHaveLength(2)
    expect(buttons.every((button) => button.disabled)).toBe(true)
    expect(buttons.find((button) => button.getAttribute('aria-busy') === 'true')).toBeTruthy()

    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))
    await flushPromises()

    expect(wrapper.emitted('confirm')).toBeUndefined()
    expect(wrapper.emitted('update:open')).toBeUndefined()
    expect(document.body.querySelector('[role="alertdialog"]')).not.toBeNull()
  })

  it('derives a warning button and respects explicit confirmation disabling', async () => {
    mountConfirm({
      open: true,
      title: '重新启用用户',
      description: '该用户将恢复访问权限。',
      tone: 'warning',
      confirmDisabled: true,
    })
    await flushPromises()

    const confirm = Array.from(document.body.querySelectorAll<HTMLButtonElement>('button')).find(
      (button) => button.textContent?.trim() === '确认',
    )
    expect(confirm?.classList.contains('ui-button--warning')).toBe(true)
    expect(confirm?.disabled).toBe(true)
  })
})
