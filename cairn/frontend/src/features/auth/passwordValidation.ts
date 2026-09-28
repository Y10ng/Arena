export const DEFAULT_PROVISIONED_PASSWORD = 'Arena@2026'

export interface PasswordChangeFields {
  currentPassword: string
  newPassword: string
  confirmPassword: string
}

export function validatePasswordChange(fields: PasswordChangeFields): string {
  if (!fields.currentPassword || !fields.newPassword || !fields.confirmPassword) {
    return '请填写当前密码、新密码和确认密码'
  }
  if (fields.newPassword !== fields.confirmPassword) {
    return '两次输入的新密码不一致'
  }
  if (fields.newPassword === DEFAULT_PROVISIONED_PASSWORD) {
    return '新密码不能使用默认密码 Arena@2026'
  }
  if (fields.newPassword === fields.currentPassword) {
    return '新密码不能与当前密码相同'
  }
  return ''
}

export function safeRedirect(value: unknown, fallback = '/'): string {
  if (typeof value !== 'string' || !value.startsWith('/') || value.startsWith('//')) return fallback
  if (value === '/change-password' || value.startsWith('/change-password?')) return fallback
  return value
}
