import { describe, expect, it } from 'vitest'
import { resolveAuthNavigation } from './authNavigation'

const target = (overrides: Partial<Parameters<typeof resolveAuthNavigation>[0]> = {}) => ({
  name: 'Projects',
  fullPath: '/projects',
  isPublic: false,
  requiresSuperAdmin: false,
  ...overrides,
})

const auth = (overrides: Partial<Parameters<typeof resolveAuthNavigation>[1]> = {}) => ({
  isAuthenticated: true,
  isSuperAdmin: false,
  mustChangePassword: false,
  ...overrides,
})

describe('auth navigation guard', () => {
  it('sends anonymous users to login with their destination', () => {
    expect(resolveAuthNavigation(target(), auth({ isAuthenticated: false }))).toEqual({
      name: 'Login', query: { redirect: '/projects' },
    })
  })

  it('gives forced password changes precedence over authorization', () => {
    expect(resolveAuthNavigation(
      target({ name: 'ConsoleUsers', fullPath: '/console/users', requiresSuperAdmin: true }),
      auth({ mustChangePassword: true }),
    )).toEqual({ name: 'ChangePassword', query: { redirect: '/console/users' } })
  })

  it('allows forced users to reach the password page', () => {
    expect(resolveAuthNavigation(
      target({ name: 'ChangePassword', fullPath: '/change-password' }),
      auth({ mustChangePassword: true }),
    )).toBe(true)
  })

  it('redirects users who completed the password lifecycle away from the password page', () => {
    expect(resolveAuthNavigation(
      target({ name: 'ChangePassword', fullPath: '/change-password' }),
      auth(),
    )).toBe('/')
  })

  it('redirects an authenticated forced user away from login', () => {
    expect(resolveAuthNavigation(
      target({ name: 'Login', fullPath: '/login', isPublic: true }),
      auth({ mustChangePassword: true }),
    )).toEqual({ name: 'ChangePassword' })
  })

  it('enforces super administrator access after password lifecycle checks', () => {
    expect(resolveAuthNavigation(
      target({ requiresSuperAdmin: true }),
      auth(),
    )).toEqual({ name: 'Forbidden' })
  })

  it('allows super administrators to reach privileged routes', () => {
    expect(resolveAuthNavigation(
      target({ requiresSuperAdmin: true }),
      auth({ isSuperAdmin: true }),
    )).toBe(true)
  })

  it('allows authenticated users to reach the non-privileged Forbidden page', () => {
    expect(resolveAuthNavigation(
      target({ name: 'Forbidden', fullPath: '/forbidden', requiresSuperAdmin: false }),
      auth(),
    )).toBe(true)
  })
})
