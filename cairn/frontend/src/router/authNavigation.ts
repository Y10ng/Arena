export interface AuthNavigationState {
  isAuthenticated: boolean
  isSuperAdmin: boolean
  mustChangePassword: boolean
}

export interface AuthNavigationTarget {
  name?: string | symbol | null
  fullPath: string
  isPublic: boolean
  requiresSuperAdmin: boolean
}

export type AuthNavigationResult = true | string | {
  name: 'Login' | 'ChangePassword' | 'Forbidden'
  query?: { redirect: string }
}

export function resolveAuthNavigation(
  target: AuthNavigationTarget,
  auth: AuthNavigationState,
): AuthNavigationResult {
  if (target.isPublic) {
    if (target.name === 'Login' && auth.isAuthenticated) {
      return auth.mustChangePassword ? { name: 'ChangePassword' } : '/'
    }
    return true
  }
  if (!auth.isAuthenticated) {
    return { name: 'Login', query: { redirect: target.fullPath } }
  }
  if (auth.mustChangePassword && target.name !== 'ChangePassword') {
    return { name: 'ChangePassword', query: { redirect: target.fullPath } }
  }
  if (!auth.mustChangePassword && target.name === 'ChangePassword') return '/'
  if (target.requiresSuperAdmin && !auth.isSuperAdmin) return { name: 'Forbidden' }
  return true
}
