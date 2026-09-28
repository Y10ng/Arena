import { describe, expect, it } from 'vitest'
import type { TenantWorker } from '@/api/types'
import {
  defaultTenantWorkerDraft,
  selectAfterWorkerListChange,
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
  it('creates independent default and persisted drafts', () => {
    const first = defaultTenantWorkerDraft('model-1')
    const second = defaultTenantWorkerDraft('model-2')
    first.task_types.pop()

    expect(first.model_id).toBe('model-1')
    expect(second).toEqual({
      display_name: '',
      model_id: 'model-2',
      task_types: ['reason', 'bootstrap', 'explore'],
      max_running: 1,
      priority: 100,
      enabled: true,
    })
    expect(tenantWorkerDraft(worker('w1'))).toEqual({
      display_name: 'Worker w1',
      model_id: 'model-1',
      task_types: ['reason', 'explore'],
      max_running: 2,
      priority: 50,
      enabled: true,
    })
  })

  it('validates required integer ranges before submitting', () => {
    const draft = tenantWorkerDraft(worker('w1'))
    expect(tenantWorkerDraftIsValid(draft)).toBe(true)
    expect(tenantWorkerDraftIsValid({ ...draft, max_running: 0 })).toBe(false)
    expect(tenantWorkerDraftIsValid({ ...draft, max_running: 1.5 })).toBe(false)
    expect(tenantWorkerDraftIsValid({ ...draft, priority: -10_001 })).toBe(false)
    expect(tenantWorkerDraftIsValid({ ...draft, priority: Number.NaN })).toBe(false)
  })

  it('keeps selection when present and falls back after deletion', () => {
    expect(selectAfterWorkerListChange([worker('w1'), worker('w2')], 'w2')).toBe('w2')
    expect(selectAfterWorkerListChange([worker('w1')], 'w2')).toBe('w1')
    expect(selectAfterWorkerListChange([], 'w2')).toBe('')
  })

  it('maps known unavailable reasons and safely handles unknown values', () => {
    expect(tenantWorkerUnavailableLabel('engine_unsupported')).toBe('历史引擎不受支持 · 请新建 pi Worker')
    expect(tenantWorkerUnavailableLabel('worker_disabled')).toBe('已禁用')
    expect(tenantWorkerUnavailableLabel('model_disabled')).toBe('Model 已禁用')
    expect(tenantWorkerUnavailableLabel('model_protocol_unsupported')).toBe('Model 协议不受支持')
    expect(tenantWorkerUnavailableLabel('model_incomplete')).toBe('Model 未完成')
    expect(tenantWorkerUnavailableLabel('credential_missing')).toBe('缺少 Key')
    expect(tenantWorkerUnavailableLabel('future_reason')).toBe('不可用')
    expect(tenantWorkerUnavailableLabel(null)).toBe('不可用')
  })

  it('separates configuration readiness from Dispatcher runtime status', () => {
    expect(tenantWorkerStatusLabel(worker('ready'))).toBe('可用')
    expect(tenantWorkerStatusLabel(worker('offline', {
      executor_available: false,
    }))).toBe('配置就绪 · Dispatcher 不可用')
    expect(tenantWorkerStatusLabel(worker('missing-key', {
      available: false,
      unavailable_reason: 'credential_missing',
      executor_available: false,
    }))).toBe('缺少 Key')
  })
})
