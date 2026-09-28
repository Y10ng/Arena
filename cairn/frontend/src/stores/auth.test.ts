import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { getCsrfToken } from '@/api/client'
import { useAuthStore } from './auth'

const jsonResponse = (status: number, data: unknown) => ({
  status,
  ok: status >= 200 && status < 300,
  headers: new Headers({ 'content-type': 'application/json' }),
  json: async () => data,
  text: async () => JSON.stringify(data),
}) as Response

describe('auth store session bootstrap', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    const storage = new Map<string, string>()
    vi.stubGlobal('window', {
      localStorage: {
        getItem: (key: string) => storage.get(key) || null,
        setItem: (key: string, value: string) => storage.set(key, value),
        removeItem: (key: string) => storage.delete(key),
      },
    })
  })

  afterEach(() => vi.unstubAllGlobals())

  it('reuses a CSRF token shared by another tab without rotating it', async () => {
    const sharedStorage = new Map<string, string>([['cairn:csrf-token', 'csrf-peer']])
    vi.stubGlobal('window', {
      localStorage: {
        getItem: (key: string) => sharedStorage.get(key) || null,
        setItem: (key: string, value: string) => sharedStorage.set(key, value),
        removeItem: (key: string) => sharedStorage.delete(key),
      },
    })
    const session = { user_id: 'usr_1', tenant_id: 'tenant_1', role: 'user' as const, must_change_password: false }
    const fetchMock = vi.fn().mockResolvedValueOnce(jsonResponse(200, session))
    vi.stubGlobal('fetch', fetchMock)

    const store = useAuthStore()

    await expect(store.initialize()).resolves.toBe(true)
    expect(fetchMock).toHaveBeenCalledTimes(1)
    expect(getCsrfToken()).toBe('csrf-peer')
  })

  it('rotates and stores CSRF when a valid session tab has no token', async () => {
    const session = { user_id: 'usr_1', tenant_id: 'tenant_1', role: 'user' as const, must_change_password: false }
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(jsonResponse(200, session))
      .mockResolvedValueOnce(jsonResponse(200, { ...session, csrf_token: 'csrf-new-tab' }))
    vi.stubGlobal('fetch', fetchMock)

    const store = useAuthStore()

    await expect(store.initialize()).resolves.toBe(true)
    expect(fetchMock).toHaveBeenNthCalledWith(1, '/auth/session', expect.objectContaining({ method: 'GET' }))
    expect(fetchMock).toHaveBeenNthCalledWith(2, '/auth/csrf', expect.objectContaining({
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
    }))
    expect(getCsrfToken()).toBe('csrf-new-tab')
    expect(store.isAuthenticated).toBe(true)
    expect(store.session?.user_id).toBe('usr_1')
  })

  it('does not overwrite a newer login when initialization finishes late', async () => {
    let resolveSession!: (response: Response) => void
    const deferredSession = new Promise<Response>((resolve) => { resolveSession = resolve })
    const newerSession = {
      user_id: 'usr_new', tenant_id: 'tenant_new', role: 'user' as const,
      must_change_password: true, csrf_token: 'csrf-new',
    }
    const fetchMock = vi.fn()
      .mockReturnValueOnce(deferredSession)
      .mockResolvedValueOnce(jsonResponse(200, newerSession))
    vi.stubGlobal('fetch', fetchMock)

    const store = useAuthStore()
    const initialization = store.initialize()
    await store.login('new-user', 'password')
    resolveSession(jsonResponse(401, { detail: 'expired' }))

    await expect(initialization).resolves.toBe(true)
    expect(store.session?.user_id).toBe('usr_new')
    expect(store.mustChangePassword).toBe(true)
    expect(getCsrfToken()).toBe('csrf-new')
  })

  it('updates the session after a required password change', async () => {
    const forcedSession = {
      user_id: 'usr_1', tenant_id: 'tenant_1', role: 'user' as const,
      must_change_password: true, csrf_token: 'csrf-login',
    }
    const changedSession = {
      ...forcedSession, must_change_password: false, csrf_token: 'csrf-changed',
    }
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(jsonResponse(200, forcedSession))
      .mockResolvedValueOnce(jsonResponse(200, changedSession))
    vi.stubGlobal('fetch', fetchMock)

    const store = useAuthStore()
    await store.login('user', 'Arena@2026')
    await store.updatePassword('Arena@2026', 'new-secret')

    expect(store.mustChangePassword).toBe(false)
    expect(store.session).toEqual(changedSession)
    expect(getCsrfToken()).toBe('csrf-changed')
    expect(fetchMock.mock.calls[1]?.[0]).toBe('/auth/password/change')
    expect(fetchMock.mock.calls[1]?.[1]).toEqual(expect.objectContaining({
      method: 'POST',
      body: JSON.stringify({
        current_password: 'Arena@2026',
        new_password: 'new-secret',
      }),
    }))
  })

  it('keeps the forced session when password change fails', async () => {
    const forcedSession = {
      user_id: 'usr_1', tenant_id: 'tenant_1', role: 'user' as const,
      must_change_password: true, csrf_token: 'csrf-login',
    }
    vi.stubGlobal('fetch', vi.fn()
      .mockResolvedValueOnce(jsonResponse(200, forcedSession))
      .mockResolvedValueOnce(jsonResponse(422, { detail: 'Invalid new password' })))

    const store = useAuthStore()
    await store.login('user', 'Arena@2026')

    await expect(store.updatePassword('Arena@2026', 'bad')).rejects.toMatchObject({ status: 422 })
    expect(store.isAuthenticated).toBe(true)
    expect(store.mustChangePassword).toBe(true)
  })

  it('clears authentication when CSRF rotation fails', async () => {
    const session = { user_id: 'usr_1', tenant_id: 'tenant_1', role: 'user' as const, must_change_password: false }
    vi.stubGlobal('fetch', vi.fn()
      .mockResolvedValueOnce(jsonResponse(200, session))
      .mockResolvedValueOnce(jsonResponse(403, { detail: 'Untrusted request origin' })))

    const store = useAuthStore()

    await expect(store.initialize()).resolves.toBe(false)
    expect(store.isAuthenticated).toBe(false)
    expect(getCsrfToken()).toBeNull()
  })
})
