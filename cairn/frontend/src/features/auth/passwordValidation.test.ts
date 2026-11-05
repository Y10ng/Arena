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
  it.each(['/', '/projects/one/runs?runId=run-a#output', '/reports?reportId=report-a'])(
    'preserves local application path %s', (path) => {
      expect(safeRedirect(path)).toBe(path)
    },
  )

  it.each([
    undefined, null, [], '', 'projects', '//example.test', 'https://example.test',
    '/change-password', '/change-password?redirect=/',
  ].map((value) => ({ value })))('falls back to projects for invalid or looping redirect $value', ({ value }) => {
    expect(safeRedirect(value)).toBe('/projects')
  })

  it('honors an explicit fallback without replacing a valid deep link', () => {
    expect(safeRedirect('//example.test', '/reports')).toBe('/reports')
    expect(safeRedirect('/change-password', '/reports')).toBe('/reports')
    expect(safeRedirect('/projects/one', '/reports')).toBe('/projects/one')
  })
})
