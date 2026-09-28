import { api } from './client'

export interface AuthSession {
  user_id: string
  tenant_id: string
  role: 'user' | 'super_admin'
  must_change_password: boolean
  csrf_token?: string | null
}

export interface ChangePasswordRequest {
  current_password: string
  new_password: string
}

export const loginSession = (login: string, password: string) =>
  api<AuthSession>('POST', '/auth/login', { login, password })

export const getSession = () => api<AuthSession>('GET', '/auth/session')

export const refreshCsrfToken = () => api<AuthSession>('POST', '/auth/csrf')

export const changePassword = (body: ChangePasswordRequest) =>
  api<AuthSession>('POST', '/auth/password/change', body)

export const logoutSession = () => api<void>('POST', '/auth/logout')
