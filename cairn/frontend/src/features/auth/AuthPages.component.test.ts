import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import ChangePasswordPage from './ChangePasswordPage.vue'
import LoginPage from './LoginPage.vue'

const mocks = vi.hoisted(() => ({
  authStore: {
    login: vi.fn(),
    logout: vi.fn(),
    updatePassword: vi.fn(),
    mustChangePassword: false,
  },
  uiStore: { theme: 'light', toggleTheme: vi.fn() },
  route: { query: { redirect: '/projects' } },
  router: { replace: vi.fn() },
}))

vi.mock('@/stores/auth', () => ({
  useAuthStore: () => mocks.authStore,
}))

vi.mock('@/stores/ui', () => ({ useUIStore: () => mocks.uiStore }))

vi.mock('vue-router', () => ({
  useRoute: () => mocks.route,
  useRouter: () => mocks.router,
}))

describe('authentication pilot pages', () => {
  beforeEach(() => {
    mocks.authStore.login.mockReset()
    mocks.authStore.logout.mockReset()
    mocks.authStore.updatePassword.mockReset()
    mocks.authStore.mustChangePassword = false
    mocks.router.replace.mockReset()
    mocks.route.query.redirect = '/projects'
  })

  it('preserves login control semantics and redirect behavior', async () => {
    const wrapper = mount(LoginPage)
    const inputs = wrapper.findAll('input')

    expect(wrapper.get('.auth-brand').text()).toContain('Arena')
    expect(wrapper.get('.auth-mark').text()).toBe('A')
    expect(wrapper.get('h1').text()).toBe('登录 Arena')
    expect(inputs).toHaveLength(2)
    expect(inputs[0]?.attributes()).toMatchObject({
      autocomplete: 'username',
      autofocus: '',
      required: '',
    })
    expect(inputs[1]?.attributes()).toMatchObject({
      autocomplete: 'current-password',
      required: '',
      type: 'password',
    })

    await inputs[0]?.setValue('  alice  ')
    await inputs[1]?.setValue('secret')
    await wrapper.get('form').trigger('submit')
    await flushPromises()

    expect(mocks.authStore.login).toHaveBeenCalledWith('alice', 'secret')
    expect(mocks.router.replace).toHaveBeenCalledWith('/projects')
  })

  it('renders login failures as an inline alert', async () => {
    mocks.authStore.login.mockRejectedValueOnce(new Error('凭据无效'))
    const wrapper = mount(LoginPage)
    const inputs = wrapper.findAll('input')

    await inputs[0]?.setValue('alice')
    await inputs[1]?.setValue('bad-password')
    await wrapper.get('form').trigger('submit')
    await flushPromises()

    expect(wrapper.get('[role="alert"]').text()).toContain('凭据无效')
    expect((inputs[0]?.element as HTMLInputElement).value).toBe('alice')
    expect((inputs[1]?.element as HTMLInputElement).value).toBe('bad-password')
  })

  it('preserves password hints, native semantics, and logout navigation', async () => {
    const wrapper = mount(ChangePasswordPage)
    const inputs = wrapper.findAll('input')

    expect(wrapper.get('.auth-brand').text()).toContain('Arena')
    expect(wrapper.get('.auth-mark').text()).toBe('A')
    expect(inputs).toHaveLength(3)
    expect(inputs[0]?.attributes()).toMatchObject({
      autocomplete: 'current-password',
      autofocus: '',
      required: '',
    })
    expect(inputs[1]?.attributes('autocomplete')).toBe('new-password')
    expect(inputs[2]?.attributes('autocomplete')).toBe('new-password')
    const hintId = inputs[1]?.attributes('aria-describedby')
    expect(hintId).toBeTruthy()
    expect(wrapper.get(`[id="${hintId}"]`).text()).toContain('Arena@2026')

    await wrapper
      .findAll('button')
      .find((button) => button.text() === '退出登录')
      ?.trigger('click')
    await flushPromises()

    expect(mocks.authStore.logout).toHaveBeenCalledOnce()
    expect(mocks.router.replace).toHaveBeenCalledWith({ name: 'Login' })
  })
})
