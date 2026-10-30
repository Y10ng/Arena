import { api } from './client'

export interface RuntimeSettingsValues {
  runtime: {
    max_workers: number
    max_running_projects: number
    max_project_workers: number
    interval: number
    healthcheck_timeout: number
  }
  tasks: {
    bootstrap: { timeout: number; conclude_timeout: number }
    reason: { timeout: number }
    explore: { timeout: number; conclude_timeout: number }
    report: { timeout: number }
  }
}

export interface RuntimeSettingsDocument extends RuntimeSettingsValues {
  revision: string
  load_status: {
    state: 'loaded' | 'pending' | 'failed' | 'unknown'
    hard_cap: number | null
    updated_at: string | null
    message: string
  }
}

export interface RuntimeSettingsPatch {
  revision: string
  runtime?: Partial<RuntimeSettingsValues['runtime']>
  tasks?: {
    [K in keyof RuntimeSettingsValues['tasks']]?: Partial<RuntimeSettingsValues['tasks'][K]>
  }
}

export const readRuntimeSettings = () =>
  api<RuntimeSettingsDocument>('GET', '/dispatcher/runtime-settings')
export const patchRuntimeSettings = (body: RuntimeSettingsPatch) =>
  api<RuntimeSettingsDocument>('PATCH', '/dispatcher/runtime-settings', body)
