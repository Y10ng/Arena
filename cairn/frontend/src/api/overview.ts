import { api } from './client'
import type { AuthSession } from './auth'
import type { OverviewResponse } from './types/overview'

export const overviewKey = (session: AuthSession | null) =>
  ['overview', session?.user_id, session?.tenant_id, session?.role] as const

export const getOverview = (signal?: AbortSignal) =>
  api<OverviewResponse>('GET', '/overview', undefined, signal)
