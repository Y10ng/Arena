import { describe, expect, it } from 'vitest'
import { safeRedirect, validatePasswordChange } from './passwordValidation'

describe('password change validation', () => {
  it('requires all password fields', () => {
    expect(validatePasswordChange({
      currentPassword: '', newPassword: 'next', confirmPassword: 'next',
    })).toBe('请填写当前密码、新密码和确认密码')
  })

  it('rejects mismatched and provisioned passwords', () => {
    expect(validatePasswordChange({
      currentPassword: 'old', newPassword: 'next', confirmPassword: 'other',
    })).toBe('两次输入的新密码不一致')
    expect(validatePasswordChange({
      currentPassword: 'old', newPassword: 'Arena@2026', confirmPassword: 'Arena@2026',
    })).toBe('新密码不能使用默认密码 Arena@2026')
  })

  it('rejects current password reuse', () => {
    expect(validatePasswordChange({
      currentPassword: 'same', newPassword: 'same', confirmPassword: 'same',
    })).toBe('新密码不能与当前密码相同')
  })
})

describe('safe redirect', () => {
  it('accepts local application paths', () => {
    expect(safeRedirect('/projects/one?tab=runs')).toBe('/projects/one?tab=runs')
  })

  it('rejects external and password-page loops', () => {
    expect(safeRedirect('//example.test')).toBe('/')
    expect(safeRedirect('https://example.test')).toBe('/')
    expect(safeRedirect('/change-password?redirect=/')).toBe('/')
  })
})
