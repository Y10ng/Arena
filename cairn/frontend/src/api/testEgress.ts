import { api } from './client'

export type TestEgressMode = 'direct' | 'http' | 'socks5'
export interface TestEgressPublic {
  mode: TestEgressMode
  host: string | null
  port: number | null
  auth_configured: boolean
  revision: number
}
export interface TestEgressInput {
  mode: TestEgressMode
  host?: string | null
  port?: number | null
  auth_action?: 'keep' | 'replace' | 'clear'
  username?: string
  password?: string
}
export const updateProjectTestEgress = (projectId: string, input: TestEgressInput) =>
  api<TestEgressPublic>('PUT', `/projects/${encodeURIComponent(projectId)}/test-egress`, input)
