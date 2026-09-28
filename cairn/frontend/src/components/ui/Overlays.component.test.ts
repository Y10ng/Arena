import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, describe, expect, it } from 'vitest'
import AlertDialog from './AlertDialog.vue'
import Dialog from './Dialog.vue'
import Drawer from './Drawer.vue'

const mountedWrappers: Array<ReturnType<typeof mount>> = []

const mountToBody = (
  component: Parameters<typeof mount>[0],
  options: Parameters<typeof mount>[1],
) => {
  const wrapper = mount(component, { ...options, attachTo: document.body })
  mountedWrappers.push(wrapper)
  return wrapper
}

const pressEscape = () => {
  document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))
}

afterEach(() => {
  mountedWrappers.splice(0).forEach((wrapper) => wrapper.unmount())
  document.body.innerHTML = ''
})

describe('Dialog', () => {
  it('renders a named and described dialog through a portal', async () => {
    const wrapper = mountToBody(Dialog, {
      props: {
        open: true,
        title: '编辑项目',
        description: '更新项目的基础信息。',
        closeLabel: '关闭编辑项目',
      },
      slots: { default: '<p>表单正文</p>' },
    })
    await flushPromises()

    expect(wrapper.find('[role="dialog"]').exists()).toBe(false)
    const dialog = document.body.querySelector<HTMLElement>('[role="dialog"]')
    expect(dialog).not.toBeNull()
    expect(dialog?.getAttribute('aria-labelledby')).toBeTruthy()
    expect(dialog?.getAttribute('aria-describedby')).toBeTruthy()
    expect(
      document.getElementById(dialog?.getAttribute('aria-labelledby') ?? '')?.textContent,
    ).toBe('编辑项目')
    expect(
      document.getElementById(dialog?.getAttribute('aria-describedby') ?? '')?.textContent,
    ).toBe('更新项目的基础信息。')
    expect(document.body.querySelector('button[aria-label="关闭编辑项目"]')).not.toBeNull()
  })

  it('handles Escape according to dismissible and forwards focus events', async () => {
    const dismissible = mountToBody(Dialog, {
      props: { open: true, title: '可关闭', description: '允许取消。' },
    })
    await flushPromises()

    expect(dismissible.emitted('openAutoFocus')).toHaveLength(1)
    pressEscape()
    await flushPromises()
    expect(dismissible.emitted('escapeKeyDown')).toHaveLength(1)
    expect(dismissible.emitted('escapeKeyDown')?.[0]?.[0]).toBeInstanceOf(KeyboardEvent)
    expect(dismissible.emitted('update:open')).toContainEqual([false])

    dismissible.unmount()
    document.body.innerHTML = ''

    const locked = mountToBody(Dialog, {
      props: {
        open: true,
        title: '处理中',
        description: '操作完成前不可关闭。',
        dismissible: false,
      },
    })
    await flushPromises()

    expect(document.body.querySelector('button[aria-label="关闭"]')).toBeNull()
    pressEscape()
    await flushPromises()
    expect(locked.emitted('escapeKeyDown')).toHaveLength(1)
    expect(locked.emitted('update:open')).toBeUndefined()
  })

  it('keeps the default portal behavior and supports inline rendering', async () => {
    const portalled = mountToBody(Dialog, {
      props: {
        open: true,
        title: '默认 Portal',
        description: '内容挂载到 document.body。',
      },
    })
    await flushPromises()

    expect(portalled.find('[role="dialog"]').exists()).toBe(false)
    portalled.unmount()
    document.body.innerHTML = ''

    const host = document.createElement('section')
    document.body.appendChild(host)
    const inline = mount(Dialog, {
      attachTo: host,
      props: {
        open: true,
        title: '内联 Dialog',
        description: '内容保留在全屏宿主中。',
        portalDisabled: true,
      },
    })
    mountedWrappers.push(inline)
    await flushPromises()

    expect(inline.find('[role="dialog"]').exists()).toBe(true)
    expect(host.querySelector('[role="dialog"]')).not.toBeNull()
    expect(host.querySelector('.ui-dialog__overlay')).not.toBeNull()
    expect(document.body.querySelectorAll('[role="dialog"]')).toHaveLength(1)
  })

  it('supports fullscreen workspaces without outside dismissal', async () => {
    const opener = document.createElement('button')
    opener.textContent = '打开编辑器'
    document.body.appendChild(opener)
    opener.focus()

    const wrapper = mount(Dialog, {
      attachTo: document.body,
      props: {
        open: false,
        title: '全屏编辑内容',
        description: '使用 Markdown 编辑项目资料。',
        size: 'fullscreen',
        closeOnOutside: false,
        showClose: false,
      },
      slots: {
        'header-actions': '<button type="button">源码模式</button>',
        default: '<div>编辑器正文</div>',
      },
    })
    mountedWrappers.push(wrapper)

    await wrapper.setProps({ open: true })
    await flushPromises()

    const dialog = document.body.querySelector<HTMLElement>('[role="dialog"]')
    expect(dialog?.classList.contains('ui-dialog__content--fullscreen')).toBe(true)
    expect(document.body.querySelector('.ui-dialog__overlay--fullscreen')).not.toBeNull()
    expect(document.body.textContent).toContain('源码模式')
    expect(dialog?.getAttribute('aria-labelledby')).toBeTruthy()
    expect(dialog?.getAttribute('aria-describedby')).toBeTruthy()

    document.body.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true }))
    document.body.dispatchEvent(new PointerEvent('pointerup', { bubbles: true }))
    await flushPromises()
    expect(wrapper.emitted('update:open')).toBeUndefined()

    pressEscape()
    await flushPromises()
    expect(wrapper.emitted('update:open')).toContainEqual([false])

    await wrapper.setProps({ open: false })
    await flushPromises()
    expect(document.activeElement).toBe(opener)
  })
})

describe('AlertDialog', () => {
  it('uses alertdialog semantics, focuses cancel, and blocks outside dismissal', async () => {
    const wrapper = mountToBody(AlertDialog, {
      props: {
        open: true,
        title: '删除用户',
        description: '该操作无法撤销。',
        tone: 'danger',
      },
      slots: {
        cancel: '<button>保留用户</button>',
        action: '<button>删除用户</button>',
      },
    })
    await flushPromises()

    const alertDialog = document.body.querySelector<HTMLElement>('[role="alertdialog"]')
    expect(alertDialog).not.toBeNull()
    expect(alertDialog?.getAttribute('aria-labelledby')).toBeTruthy()
    expect(alertDialog?.getAttribute('aria-describedby')).toBeTruthy()
    expect(document.activeElement?.textContent).toBe('保留用户')

    document.body.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true }))
    document.body.dispatchEvent(new PointerEvent('pointerup', { bubbles: true }))
    await flushPromises()
    expect(wrapper.emitted('update:open')).toBeUndefined()
  })

  it('only allows Escape cancellation when dismissible', async () => {
    const wrapper = mountToBody(AlertDialog, {
      props: {
        open: true,
        title: '正在删除',
        description: '请等待操作完成。',
        dismissible: false,
      },
    })
    await flushPromises()

    pressEscape()
    await flushPromises()
    expect(wrapper.emitted('update:open')).toBeUndefined()
  })
})

describe('Drawer', () => {
  it('applies side and size contracts and forwards close event details', async () => {
    const wrapper = mountToBody(Drawer, {
      props: {
        open: true,
        title: '漏洞详情',
        description: '查看漏洞证据与修复建议。',
        side: 'left',
        size: 'sm',
        closeLabel: '关闭漏洞详情',
      },
    })
    await flushPromises()

    const drawer = document.body.querySelector<HTMLElement>('[role="dialog"]')
    expect(drawer?.classList.contains('ui-drawer__content--left')).toBe(true)
    expect(drawer?.classList.contains('ui-drawer__content--sm')).toBe(true)

    const close = document.body.querySelector<HTMLButtonElement>(
      'button[aria-label="关闭漏洞详情"]',
    )
    close?.click()
    await flushPromises()

    expect(wrapper.emitted('update:open')?.[0]?.[0]).toBe(false)
    expect(wrapper.emitted('update:open')?.[0]?.[1]).toMatchObject({ reason: 'close-press' })
  })

  it('supports the large horizontal Drawer size contract', async () => {
    mountToBody(Drawer, {
      props: {
        open: true,
        title: '宽幅证据详情',
        description: '展示请求、响应和产物内容。',
        side: 'right',
        size: 'lg',
      },
    })
    await flushPromises()

    const drawer = document.body.querySelector<HTMLElement>('[role="dialog"]')
    expect(drawer?.classList.contains('ui-drawer__content--right')).toBe(true)
    expect(drawer?.classList.contains('ui-drawer__content--lg')).toBe(true)
  })

  it('keeps the default portal behavior and supports inline rendering', async () => {
    const portalled = mountToBody(Drawer, {
      props: {
        open: true,
        title: '默认 Portal',
        description: '内容挂载到 document.body。',
      },
    })
    await flushPromises()

    expect(portalled.find('[role="dialog"]').exists()).toBe(false)
    portalled.unmount()
    document.body.innerHTML = ''

    const host = document.createElement('section')
    document.body.appendChild(host)
    const inline = mount(Drawer, {
      attachTo: host,
      props: {
        open: true,
        title: '内联 Drawer',
        description: '内容保留在全屏宿主中。',
        portalDisabled: true,
      },
    })
    mountedWrappers.push(inline)
    await flushPromises()

    expect(inline.find('[role="dialog"]').exists()).toBe(true)
    expect(host.querySelector('[role="dialog"]')).not.toBeNull()
    expect(host.querySelector('.ui-drawer__overlay')).not.toBeNull()
  })

  it('forwards Escape while preserving the dismissible close contract', async () => {
    const wrapper = mountToBody(Drawer, {
      props: {
        open: true,
        title: 'Escape Drawer',
        description: '允许调用方处理同一键盘事件。',
      },
    })
    await flushPromises()

    pressEscape()
    await flushPromises()

    expect(wrapper.emitted('escapeKeyDown')).toHaveLength(1)
    expect(wrapper.emitted('escapeKeyDown')?.[0]?.[0]).toBeInstanceOf(KeyboardEvent)
    expect(wrapper.emitted('update:open')?.[0]?.[0]).toBe(false)
    expect(wrapper.emitted('update:open')?.[0]?.[1]).toMatchObject({ reason: 'escape-key' })
  })

  it('blocks dismiss events when nondismissible', async () => {
    const wrapper = mountToBody(Drawer, {
      props: {
        open: true,
        title: '保存中',
        description: '保存完成前不可关闭。',
        side: 'bottom',
        size: 'md',
        dismissible: false,
      },
    })
    await flushPromises()

    const drawer = document.body.querySelector<HTMLElement>('[role="dialog"]')
    expect(drawer?.classList.contains('ui-drawer__content--bottom')).toBe(true)
    expect(drawer?.classList.contains('ui-drawer__content--md')).toBe(true)
    expect(document.body.querySelector('button[aria-label="关闭"]')).toBeNull()

    pressEscape()
    document.body.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true }))
    document.body.dispatchEvent(new PointerEvent('pointerup', { bubbles: true }))
    await flushPromises()
    expect(wrapper.emitted('update:open')).toBeUndefined()
  })
})
