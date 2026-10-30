import type { TestEgressInput, TestEgressPublic } from '@/api/testEgress'

// 凭据仅属于当前表单内存；公共读取与浏览器恢复缓存均不得承载它们。
export interface TestEgressDraft {
  mode: TestEgressInput['mode']
  host: string
  port: number | null
  auth_action: 'keep' | 'replace' | 'clear'
  username: string
  password: string
}
export function testEgressDraft(value?: TestEgressPublic): TestEgressDraft {
  return { mode: value?.mode || 'direct', host: value?.host || '', port: value?.port ?? null, auth_action: 'keep', username: '', password: '' }
}
export function testEgressError(value: TestEgressDraft): string {
  if (value.mode === 'direct') return ''
  if (!value.host.trim() || /[\s/@?#]/.test(value.host) || value.host.includes('://')) return '请填写代理主机地址，不含协议、路径或认证信息。'
  if (value.port === null || !Number.isInteger(value.port) || value.port < 1 || value.port > 65535) return '代理端口须为 1–65535 的整数。'
  if (value.auth_action === 'replace' && (!value.username || !value.password)) return '替换认证时请同时填写用户名和密码。'
  return ''
}
export function testEgressInput(value: TestEgressDraft): TestEgressInput {
  if (value.mode === 'direct') return { mode: 'direct', auth_action: 'clear' }
  return {
    mode: value.mode, host: value.host.trim(), port: value.port, auth_action: value.auth_action,
    ...(value.auth_action === 'replace' ? { username: value.username, password: value.password } : {}),
  }
}
