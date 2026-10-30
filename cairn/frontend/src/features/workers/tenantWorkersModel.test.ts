import { describe, expect, it } from 'vitest'
import type { TenantWorker } from '@/api/types'
import {
  tenantWorkerDraft,
  tenantWorkerDraftIsValid,
  tenantWorkerStatusLabel,
  tenantWorkerUnavailableLabel,
} from './tenantWorkersModel'

const worker = (id: string, overrides: Partial<TenantWorker> = {}): TenantWorker => ({
  id,
  display_name: `Worker ${id}`,
  model_id: 'model-1',
  model_display_name: 'Primary Claude',
  model_name: 'claude-model',
  engine: 'pi',
  task_types: ['reason', 'explore'],
  max_running: 2,
  priority: 50,
  enabled: true,
  available: true,
  unavailable_reason: null,
  executor_available: true,
  revision: 3,
  created_at: '2026-08-13T00:00:00Z',
  updated_at: '2026-08-13T00:00:00Z',
  ...overrides,
})

describe('tenant Worker state helpers', () => {
  it('creates only editable fields and accepts an unconfigured model', () => {
    const draft = tenantWorkerDraft(worker('w1', { model_id: null, enabled: false }))
    expect(draft).toEqual({ model_id: null, max_running: 2, priority: 50, enabled: false, expected_revision: 3 })
    expect(tenantWorkerDraftIsValid(draft)).toBe(true)
    expect(tenantWorkerDraftIsValid({ ...draft, model_id: '' })).toBe(false)
  })

  it('validates required integer ranges before submitting', () => {
    const draft = tenantWorkerDraft(worker('w1'))
    expect(tenantWorkerDraftIsValid(draft)).toBe(true)
    expect(tenantWorkerDraftIsValid({ ...draft, max_running: 0 })).toBe(false)
    expect(tenantWorkerDraftIsValid({ ...draft, max_running: 1.5 })).toBe(false)
    expect(tenantWorkerDraftIsValid({ ...draft, priority: -10_001 })).toBe(false)
    expect(tenantWorkerDraftIsValid({ ...draft, priority: Number.NaN })).toBe(false)
  })

  it('maps known unavailable reasons and safely handles unknown values', () => {
    expect(tenantWorkerUnavailableLabel('engine_unsupported')).toBe('引擎不受支持')
    expect(tenantWorkerUnavailableLabel('model_unconfigured')).toBe('未配置模型')
    expect(tenantWorkerUnavailableLabel('worker_disabled')).toBe('已禁用')
    expect(tenantWorkerUnavailableLabel('model_disabled')).toBe('模型已禁用')
    expect(tenantWorkerUnavailableLabel('model_protocol_unsupported')).toBe('模型协议不受支持')
    expect(tenantWorkerUnavailableLabel('model_incomplete')).toBe('模型配置未完成')
    expect(tenantWorkerUnavailableLabel('credential_missing')).toBe('缺少模型凭据')
    expect(tenantWorkerUnavailableLabel('future_reason')).toBe('不可用')
    expect(tenantWorkerUnavailableLabel(null)).toBe('不可用')
  })

  it('separates configuration readiness from Dispatcher runtime status', () => {
    expect(tenantWorkerStatusLabel(worker('ready'))).toBe('配置就绪')
    expect(tenantWorkerStatusLabel(worker('offline', {
      executor_available: false,
    }))).toBe('配置就绪 · Dispatcher 不可用')
    expect(tenantWorkerStatusLabel(worker('missing-key', {
      available: false,
      unavailable_reason: 'credential_missing',
      executor_available: false,
    }))).toBe('缺少模型凭据')
  })
})
