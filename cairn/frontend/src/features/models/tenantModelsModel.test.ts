import { describe, expect, it } from 'vitest'
import type { TenantModel } from '@/api/types'
import {
  customHeaderError,
  emptyTenantModelDraft,
  protocolDefaultEndpoints,
  pricingError,
  tenantModelDraft,
  tenantModelDraftIsValid,
} from './tenantModelsModel'

const model = (id: string): TenantModel => ({
  id,
  display_name: 'Vendor A Model',
  protocol: 'openai-responses',
  base_url: 'https://api.example.test/v1',
  auth_header: 'x-api-key',
  custom_headers: [{ name: 'X-Gateway', value: 'café-route' }],
  models_endpoint: 'catalog?scope=all',
  inference_endpoint: null,
  model_name: 'model-a',
  enabled: true,
  revision: 2,
  credential_configured: true,
  credential_version: 1,
  credential_key_version: 'key-v1',
  created_at: '2026-08-13T00:00:00Z',
  updated_at: '2026-08-13T00:00:00Z',
})

describe('tenant Models state helpers', () => {
  it('creates drafts without credential material and copies persisted Headers', () => {
    expect(emptyTenantModelDraft()).toEqual({
      display_name: '',
      protocol: 'anthropic-messages',
      base_url: '',
      auth_header: 'authorization-bearer',
      custom_headers: [],
      models_endpoint: null,
      inference_endpoint: null,
      model_name: '',
      enabled: true,
      pricing: {},
    })
    const draft = tenantModelDraft(model('m1'))
    expect(draft).toEqual({
      display_name: 'Vendor A Model',
      protocol: 'openai-responses',
      base_url: 'https://api.example.test/v1',
      auth_header: 'x-api-key',
      custom_headers: [{ name: 'X-Gateway', value: 'café-route' }],
      models_endpoint: 'catalog?scope=all',
      inference_endpoint: null,
      model_name: 'model-a',
      enabled: true,
      pricing: {},
    })
    draft.custom_headers[0]!.value = 'changed'
    expect(model('m1').custom_headers[0]!.value).toBe('café-route')
    expect(JSON.stringify(draft)).not.toContain('credential')
  })

  it('validates Header boundaries and required Model fields', () => {
    const draft = tenantModelDraft(model('m1'))
    expect(tenantModelDraftIsValid(draft)).toBe(true)
    expect(customHeaderError({
      ...draft,
      custom_headers: [{ name: 'X-Route', value: 'a' }, { name: 'x-route', value: 'b' }],
    })).toContain('名称重复')
    expect(customHeaderError({
      ...draft,
      custom_headers: [{ name: 'Authorization', value: 'custom' }],
    })).toContain('由系统管理')
    expect(customHeaderError({
      ...draft,
      custom_headers: [{ name: 'X-Route', value: 'line\nbreak' }],
    })).toContain('控制字符')
    expect(customHeaderError({
      ...draft,
      custom_headers: [{ name: 'X-Route', value: '无法发送' }],
    })).toContain('HTTP 无法发送')
    expect(tenantModelDraftIsValid({ ...draft, model_name: ' ' })).toBe(false)
  })

  it('preserves decimal prices and distinguishes explicit zero from unpriced', () => {
    const persisted = { ...model('m1'), pricing: { currency: 'USD', input: '0', output: '0.000000000001', cache_read: null } }
    const draft = tenantModelDraft(persisted)
    expect(draft.pricing).toEqual(persisted.pricing)
    draft.pricing.input = null
    expect(persisted.pricing.input).toBe('0')
    expect(pricingError(draft.pricing)).toBe('')
    expect(pricingError({ input: '0' })).toBe('')
    for (const input of ['-1', 'NaN', 'Infinity', '1e3', '']) expect(pricingError({ input })).not.toBe('')
    expect(pricingError({ currency: 'usd' })).not.toBe('')
  })

  it('returns the protocol-specific default endpoints', () => {
    expect(protocolDefaultEndpoints('anthropic-messages')).toEqual({
      models: '/v1/models', inference: '/v1/messages',
    })
    expect(protocolDefaultEndpoints('openai-chat-completions')).toEqual({
      models: '/v1/models', inference: '/v1/chat/completions',
    })
    expect(protocolDefaultEndpoints('openai-responses')).toEqual({
      models: '/v1/models', inference: '/v1/responses',
    })
  })
})
