import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { DEFAULT_PROVISIONED_PASSWORD } from '@/features/auth/passwordValidation'
import UsersSettingsPage from './UsersSettingsPage.vue'
import type { AdminUser } from '@/api/types'

const mocks = vi.hoisted(() => ({
  createAdminUser: vi.fn(),
  listAdminUsers: vi.fn(),
  resetAdminUserPassword: vi.fn(),
  revokeAdminUserSessions: vi.fn(),
  updateAdminUserStatus: vi.fn(),
  authStore: {
    session: { user_id: 'self-user' } as { user_id: string } | null,
    clearSession: vi.fn(),
  },
  uiStore: { showToast: vi.fn() },
  router: { replace: vi.fn() },
}))

vi.mock('@/api/client', () => ({
  createAdminUser: mocks.createAdminUser,
  listAdminUsers: mocks.listAdminUsers,
  resetAdminUserPassword: mocks.resetAdminUserPassword,
  revokeAdminUserSessions: mocks.revokeAdminUserSessions,
  updateAdminUserStatus: mocks.updateAdminUserStatus,
}))

vi.mock('@/stores/auth', () => ({
  useAuthStore: () => mocks.authStore,
}))

vi.mock('@/stores/ui', () => ({
  useUIStore: () => mocks.uiStore,
}))

vi.mock('vue-router', () => ({
  useRouter: () => mocks.router,
}))

const selfUser: AdminUser = {
  id: 'self-user',
  login: 'current-admin',
  role: 'super_admin',
  disabled: false,
  must_change_password: false,
  tenant_id: 'tenant-self',
  active_session_count: 2,
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-08-01T00:00:00Z',
}

const otherUser: AdminUser = {
  id: 'other-user',
  login: 'other-admin',
  role: 'super_admin',
  disabled: false,
  must_change_password: true,
  tenant_id: 'tenant-other',
  active_session_count: 3,
  created_at: '2026-02-01T00:00:00Z',
  updated_at: '2026-08-02T00:00:00Z',
}

const disabledUser: AdminUser = {
  ...otherUser,
  id: 'disabled-user',
  login: 'disabled-admin',
  disabled: true,
  active_session_count: 0,
  updated_at: '2026-08-03T00:00:00Z',
}

const mountedWrappers: Array<ReturnType<typeof mount>> = []

function mountPage(users: AdminUser[] = [selfUser, otherUser, disabledUser]) {
  mocks.listAdminUsers.mockResolvedValue(users)
  const wrapper = mount(UsersSettingsPage, { attachTo: document.body })
  mountedWrappers.push(wrapper)
  return wrapper
}

function bodyButton(label: string) {
  const button = [...document.body.querySelectorAll<HTMLButtonElement>('button')].find(
    (candidate) => candidate.textContent?.trim() === label,
  )
  if (!button) throw new Error(`找不到按钮：${label}`)
  return button
}

function row(wrapper: ReturnType<typeof mount>, userId: string) {
  return wrapper.get(`[data-testid="user-row-${userId}"]`)
}

beforeEach(() => {
  mocks.createAdminUser.mockReset().mockResolvedValue(otherUser)
  mocks.listAdminUsers.mockReset()
  mocks.resetAdminUserPassword.mockReset().mockResolvedValue(otherUser)
  mocks.revokeAdminUserSessions.mockReset().mockResolvedValue(otherUser)
  mocks.updateAdminUserStatus.mockReset().mockResolvedValue(otherUser)
  mocks.authStore.session = { user_id: selfUser.id }
  mocks.authStore.clearSession.mockReset()
  mocks.uiStore.showToast.mockReset()
  mocks.router.replace.mockReset()
})

afterEach(() => {
  mountedWrappers.splice(0).forEach((wrapper) => wrapper.unmount())
  document.body.innerHTML = ''
})

describe('UsersSettingsPage', () => {
  it('shows an initial skeleton, preserves stale rows on refresh failure, and keeps row attributes', async () => {
    let resolveInitial: (users: AdminUser[]) => void = () => undefined
    mocks.listAdminUsers.mockImplementationOnce(
      () =>
        new Promise<AdminUser[]>((resolve) => {
          resolveInitial = resolve
        }),
    )
    const wrapper = mount(UsersSettingsPage, { attachTo: document.body })
    mountedWrappers.push(wrapper)
    expect(document.body.textContent).toContain('正在加载用户列表')

    resolveInitial([otherUser])
    await flushPromises()
    expect(row(wrapper, otherUser.id).attributes('data-user-id')).toBe(otherUser.id)

    mocks.listAdminUsers.mockRejectedValueOnce(new Error('refresh offline'))
    await bodyButton('刷新').click()
    await flushPromises()
    expect(document.body.textContent).toContain('刷新失败，保留上次用户列表')
    expect(document.body.textContent).toContain('other-admin')
  })

  it('keeps limit 50 and uses the loaded row count as the load-more offset', async () => {
    const firstPage = Array.from({ length: 50 }, (_, index) => ({
      ...otherUser,
      id: `user-${index}`,
      login: `user-${index}`,
    }))
    mocks.listAdminUsers
      .mockResolvedValueOnce(firstPage)
      .mockResolvedValueOnce([{ ...otherUser, id: 'user-50', login: 'user-50' }])
    const wrapper = mount(UsersSettingsPage, { attachTo: document.body })
    mountedWrappers.push(wrapper)
    await flushPromises()

    await bodyButton('加载更多').click()
    await flushPromises()
    expect(mocks.listAdminUsers).toHaveBeenNthCalledWith(1, 50, 0)
    expect(mocks.listAdminUsers).toHaveBeenNthCalledWith(2, 50, 50)
    expect(document.body.textContent).toContain('user-50')
  })

  it('creates with a login-only payload, shows the shared default password, and manages focus', async () => {
    const wrapper = mountPage()
    await flushPromises()

    const trigger = row(
      wrapper,
      otherUser.id,
    ).element.ownerDocument.querySelector<HTMLButtonElement>('.list-actions button')
    expect(trigger?.textContent).toContain('新增用户')
    trigger?.click()
    await flushPromises()

    const dialog = document.body.querySelector<HTMLElement>('[role="dialog"]')
    const input = dialog?.querySelector<HTMLInputElement>('input[autocomplete="username"]')
    expect(dialog?.getAttribute('aria-labelledby')).toBeTruthy()
    expect(dialog?.getAttribute('aria-describedby')).toBeTruthy()
    expect(dialog?.textContent).toContain(DEFAULT_PROVISIONED_PASSWORD)
    expect(document.activeElement).toBe(input)

    bodyButton('取消').click()
    await flushPromises()
    expect(document.activeElement).toBe(trigger)

    trigger?.click()
    await flushPromises()
    const reopenedInput = document.body.querySelector<HTMLInputElement>(
      '[role="dialog"] input[autocomplete="username"]',
    )
    expect(document.activeElement).toBe(reopenedInput)
    reopenedInput?.focus()
    reopenedInput?.dispatchEvent(new Event('focus'))
    if (reopenedInput) {
      reopenedInput.value = '  new-admin  '
      reopenedInput.dispatchEvent(new Event('input', { bubbles: true }))
    }
    document
      .querySelector<HTMLFormElement>('#create-user-form')
      ?.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }))
    await flushPromises()

    expect(mocks.createAdminUser).toHaveBeenCalledWith({ login: 'new-admin' })
    expect(Object.keys(mocks.createAdminUser.mock.calls[0]?.[0] ?? {})).toEqual(['login'])
    expect(mocks.uiStore.showToast).toHaveBeenCalledWith(
      `用户已创建，初始密码为 ${DEFAULT_PROVISIONED_PASSWORD}`,
      'success',
    )
    expect(document.body.querySelector('[role="dialog"]')).toBeNull()
  })

  it('resets another user password with optimistic concurrency and reloads', async () => {
    const wrapper = mountPage()
    await flushPromises()

    const resetTrigger = row(wrapper, otherUser.id).findAll('button')[1]
    await resetTrigger?.trigger('click')
    await flushPromises()

    const input = document.body.querySelector<HTMLInputElement>(
      '[role="dialog"] input[autocomplete="new-password"]',
    )
    expect(document.activeElement).toBe(input)
    await input?.focus()
    if (input) {
      input.value = 'replacement-secret'
      input.dispatchEvent(new Event('input', { bubbles: true }))
    }
    document
      .querySelector<HTMLFormElement>('#reset-password-form')
      ?.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }))
    await flushPromises()

    expect(mocks.resetAdminUserPassword).toHaveBeenCalledWith(otherUser.id, {
      password: 'replacement-secret',
      expected_updated_at: otherUser.updated_at,
    })
    expect(mocks.listAdminUsers).toHaveBeenCalledTimes(2)
    expect(mocks.uiStore.showToast).toHaveBeenCalledWith('密码已重置', 'success')
    expect(mocks.authStore.clearSession).not.toHaveBeenCalled()
    expect(mocks.router.replace).not.toHaveBeenCalled()
  })

  it('clears the local session and redirects after resetting the current account', async () => {
    const wrapper = mountPage()
    await flushPromises()

    await row(wrapper, selfUser.id).findAll('button')[1]?.trigger('click')
    await flushPromises()
    expect(document.body.querySelector('[role="alert"]')?.textContent).toContain(
      '当前会话会立即失效',
    )

    const input = document.body.querySelector<HTMLInputElement>(
      '[role="dialog"] input[autocomplete="new-password"]',
    )
    if (input) {
      input.value = 'new-self-secret'
      input.dispatchEvent(new Event('input', { bubbles: true }))
    }
    document
      .querySelector<HTMLFormElement>('#reset-password-form')
      ?.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }))
    await flushPromises()

    expect(mocks.resetAdminUserPassword).toHaveBeenCalledWith(selfUser.id, {
      password: 'new-self-secret',
      expected_updated_at: selfUser.updated_at,
    })
    expect(mocks.authStore.clearSession).toHaveBeenCalledOnce()
    expect(mocks.router.replace).toHaveBeenCalledWith({ name: 'Login' })
    expect(mocks.listAdminUsers).toHaveBeenCalledTimes(1)
    expect(mocks.uiStore.showToast).not.toHaveBeenCalledWith('密码已重置', 'success')
  })

  it('clears failed reset plaintext, keeps the dialog open, and locks dismissal while pending', async () => {
    let rejectReset: (error: Error) => void = () => undefined
    mocks.resetAdminUserPassword.mockImplementationOnce(
      () =>
        new Promise((_resolve, reject) => {
          rejectReset = reject
        }),
    )
    const wrapper = mountPage()
    await flushPromises()

    await row(wrapper, otherUser.id).findAll('button')[1]?.trigger('click')
    await flushPromises()
    const input = document.body.querySelector<HTMLInputElement>(
      '[role="dialog"] input[autocomplete="new-password"]',
    )
    if (input) {
      input.value = 'plaintext-to-clear'
      input.dispatchEvent(new Event('input', { bubbles: true }))
    }
    document
      .querySelector<HTMLFormElement>('#reset-password-form')
      ?.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }))
    await flushPromises()

    expect(bodyButton('取消').disabled).toBe(true)
    expect(document.body.querySelector('button[aria-label="关闭重置密码"]')).toBeNull()
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))
    await flushPromises()
    expect(document.body.querySelector('[role="dialog"]')).not.toBeNull()

    rejectReset(new Error('reset failed'))
    await flushPromises()

    expect(document.body.querySelector<HTMLInputElement>('#reset-password-form input')?.value).toBe(
      '',
    )
    expect(document.body.querySelector('[role="dialog"]')).not.toBeNull()
    expect(mocks.uiStore.showToast).toHaveBeenCalledWith('reset failed', 'danger')
  })

  it('confirms status changes and session revocation with preserved request payloads', async () => {
    const wrapper = mountPage()
    await flushPromises()

    await row(wrapper, otherUser.id).findAll('button')[0]?.trigger('click')
    await flushPromises()
    expect(document.body.querySelector('[role="alertdialog"]')?.textContent).toContain(
      '全部登录会话将立即失效',
    )
    bodyButton('确认禁用').click()
    await flushPromises()

    expect(mocks.updateAdminUserStatus).toHaveBeenCalledWith(otherUser.id, {
      disabled: true,
      expected_updated_at: otherUser.updated_at,
    })
    expect(mocks.uiStore.showToast).toHaveBeenCalledWith('用户已禁用', 'success')

    await row(wrapper, otherUser.id).findAll('button')[2]?.trigger('click')
    await flushPromises()
    expect(document.body.querySelector('[role="alertdialog"]')?.textContent).toContain(
      `撤销用户「${otherUser.login}」的全部会话`,
    )
    bodyButton('确认撤销').click()
    await flushPromises()

    expect(mocks.revokeAdminUserSessions).toHaveBeenCalledWith(otherUser.id)
    expect(mocks.uiStore.showToast).toHaveBeenCalledWith('用户会话已撤销', 'success')
  })

  it('protects current-account status and revoke actions while keeping password reset available', async () => {
    const wrapper = mountPage()
    await flushPromises()

    const buttons = row(wrapper, selfUser.id).findAll('button')
    expect(buttons[0]?.attributes('disabled')).toBeDefined()
    expect(buttons[1]?.attributes('disabled')).toBeUndefined()
    expect(buttons[2]?.attributes('disabled')).toBeDefined()

    await buttons[0]?.trigger('click')
    await buttons[2]?.trigger('click')
    await flushPromises()
    expect(document.body.querySelector('[role="alertdialog"]')).toBeNull()
    expect(mocks.updateAdminUserStatus).not.toHaveBeenCalled()
    expect(mocks.revokeAdminUserSessions).not.toHaveBeenCalled()

    await buttons[1]?.trigger('click')
    await flushPromises()
    expect(document.body.querySelector('[role="dialog"]')).not.toBeNull()
  })
})
