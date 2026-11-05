import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { onBeforeRouteLeave, onBeforeRouteUpdate } from 'vue-router'
import { updateProjectTestEgress } from '@/api/testEgress'
import TestEgressFields from '@/components/patterns/TestEgressFields.vue'
import { ConfirmAction } from '@/components/patterns'
import ProjectTestEgressDialog from './ProjectTestEgressDialog.vue'

vi.mock('vue-router', () => ({ onBeforeRouteLeave: vi.fn(), onBeforeRouteUpdate: vi.fn() }))
vi.mock('@/api/testEgress', () => ({ updateProjectTestEgress: vi.fn() }))

let wrapper: VueWrapper | undefined
function openDirtyDialog() {
  wrapper = mount(ProjectTestEgressDialog, {
    props: {
      projectId: 'project-a',
      value: {
        mode: 'http',
        host: 'proxy.example.test',
        port: 8080,
        auth_configured: true,
        revision: 2,
      },
    },
    global: {
      stubs: {
        UiDialog: { template: '<div><slot /><slot name="footer" /></div>' },
        TestEgressFields: {
          props: ['modelValue'],
          emits: ['update:modelValue'],
          template: '<div />',
        },
        ConfirmAction: { props: ['open'], emits: ['confirm', 'update:open'], template: '<div />' },
      },
    },
  })
  const fields = wrapper.getComponent(TestEgressFields)
  fields.vm.$emit('update:modelValue', { ...fields.props('modelValue'), host: 'new.example.test' })
  return wrapper
}
function registeredGuard(hook: typeof onBeforeRouteLeave) {
  return vi.mocked(hook).mock.calls[0]![0] as () => Promise<boolean>
}

afterEach(() => {
  wrapper?.unmount()
  wrapper = undefined
  vi.clearAllMocks()
})

describe('项目代理设置离开保护', () => {
  it('取消关闭确认保留输入，确认前与取消后均不提交或关闭', async () => {
    const dialog = openDirtyDialog()
    await dialog
      .findAll('button')
      .find((button) => button.text() === '关闭')!
      .trigger('click')
    const confirmation = dialog.getComponent(ConfirmAction)
    expect(confirmation.props('open')).toBe(true)
    expect(dialog.emitted('close')).toBeUndefined()
    confirmation.vm.$emit('update:open', false)
    await flushPromises()
    expect(dialog.emitted('close')).toBeUndefined()
    expect(dialog.getComponent(TestEgressFields).props('modelValue').host).toBe('new.example.test')
    expect(updateProjectTestEgress).not.toHaveBeenCalled()

    await dialog
      .findAll('button')
      .find((button) => button.text() === '关闭')!
      .trigger('click')
    confirmation.vm.$emit('confirm')
    await flushPromises()
    expect(dialog.emitted('close')).toHaveLength(1)
    expect(updateProjectTestEgress).not.toHaveBeenCalled()
  })

  it.each([onBeforeRouteLeave, onBeforeRouteUpdate])(
    '路由离开与更新均等待明确选择，重复请求不覆盖确认',
    async (hook) => {
      const dialog = openDirtyDialog()
      const guard = registeredGuard(hook)
      const pending = guard()
      let settled = false
      void pending.then(() => {
        settled = true
      })
      await flushPromises()
      expect(settled).toBe(false)
      await expect(guard()).resolves.toBe(false)
      dialog.getComponent(ConfirmAction).vm.$emit('update:open', false)
      await expect(pending).resolves.toBe(false)
      expect(dialog.emitted('close')).toBeUndefined()

      const confirmed = guard()
      dialog.getComponent(ConfirmAction).vm.$emit('confirm')
      await expect(confirmed).resolves.toBe(true)
      expect(dialog.emitted('close')).toHaveLength(1)
      expect(updateProjectTestEgress).not.toHaveBeenCalled()
    },
  )

  it('卸载取消未决确认，并保留和清理 beforeunload 保护', async () => {
    openDirtyDialog()
    const before = new Event('beforeunload', { cancelable: true })
    window.dispatchEvent(before)
    expect(before.defaultPrevented).toBe(true)
    const pending = registeredGuard(onBeforeRouteLeave)()
    wrapper!.unmount()
    wrapper = undefined
    await expect(pending).resolves.toBe(false)
    const after = new Event('beforeunload', { cancelable: true })
    window.dispatchEvent(after)
    expect(after.defaultPrevented).toBe(false)
  })
})
