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

  it('preserves deep-link queries through login and forced password changes', () => {
    const destination = target({
      name: 'WorkspaceRuns',
      fullPath: '/projects/project-a/runs?runId=run-a#output',
    })
    expect(resolveAuthNavigation(destination, auth({ isAuthenticated: false }))).toEqual({
      name: 'Login', query: { redirect: destination.fullPath },
    })
    expect(resolveAuthNavigation(destination, auth({ mustChangePassword: true }))).toEqual({
      name: 'ChangePassword', query: { redirect: destination.fullPath },
    })
    expect(resolveAuthNavigation(destination, auth())).toBe(true)
  })

  it.each([false, true])('lands authenticated login visitors on projects (admin: %s)', (isSuperAdmin) => {
    expect(resolveAuthNavigation(
      target({ name: 'Login', fullPath: '/login', isPublic: true }),
      auth({ isSuperAdmin }),
    )).toBe('/projects')
  })

  it('still allows anonymous visitors to reach login and authenticated visitors to reach overview', () => {
    expect(resolveAuthNavigation(
      target({ name: 'Login', fullPath: '/login', isPublic: true }),
      auth({ isAuthenticated: false }),
    )).toBe(true)
    expect(resolveAuthNavigation(target({ name: 'Overview', fullPath: '/' }), auth())).toBe(true)
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
    )).toBe('/projects')
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
