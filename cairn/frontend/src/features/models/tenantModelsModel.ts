import type { ModelPricing, ModelProtocol, TenantModel, TenantModelConfig } from '@/api/types'

export type TenantModelDraft = TenantModelConfig & { pricing: ModelPricing }

export const pricingFields = [
  { key: 'input', label: '非缓存输入' }, { key: 'output', label: '输出' },
  { key: 'cache_read', label: '缓存读取' }, { key: 'cache_write', label: '缓存写入' },
] as const

export function pricingError(pricing: ModelPricing = {}) {
  if (pricing.currency && !/^[A-Z]{3}$/.test(pricing.currency)) return '币种需为三位大写代码，如 USD、CNY'
  if (pricingFields.some(({ key }) => pricing[key] != null && !/^\d{1,12}(?:\.\d{1,12})?$/.test(pricing[key]!))) {
    return '单价需为非负有限十进制，整数和小数各最多 12 位；留空表示未定价'
  }
  return ''
}

export const emptyTenantModelDraft = (): TenantModelDraft => ({
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

export const tenantModelDraft = (model: TenantModel): TenantModelDraft => ({
  display_name: model.display_name,
  protocol: model.protocol,
  base_url: model.base_url,
  auth_header: model.auth_header,
  custom_headers: model.custom_headers.map(header => ({ ...header })),
  models_endpoint: model.models_endpoint,
  inference_endpoint: model.inference_endpoint,
  model_name: model.model_name,
  enabled: model.enabled,
  pricing: { ...model.pricing },
})

const headerName = /^[!#$%&'*+.^_`|~0-9A-Za-z-]+$/
const reservedHeaders = new Set([
  'authorization', 'x-api-key', 'host', 'content-length',
  'transfer-encoding', 'connection',
])

export const customHeaderError = (draft: TenantModelDraft): string => {
  if (draft.custom_headers.length > 32) return '自定义 Headers 最多 32 项'
  const seen = new Set<string>()
  let totalBytes = 0
  for (const [index, header] of draft.custom_headers.entries()) {
    if (!headerName.test(header.name)) return `第 ${index + 1} 个 Header 名称无效`
    const folded = header.name.toLocaleLowerCase('en-US')
    if (seen.has(folded)) return `第 ${index + 1} 个 Header 名称重复`
    if (reservedHeaders.has(folded)) return `Header ${header.name} 由系统管理，不能自定义`
    if ([...header.value].some(character => {
      const code = character.charCodeAt(0)
      return code < 32 || code === 127
    })) return `Header ${header.name} 的值包含控制字符`
    if ([...header.value].some(character => character.codePointAt(0)! > 255)) {
      return `Header ${header.name} 的值包含 HTTP 无法发送的字符`
    }
    seen.add(folded)
    totalBytes += new TextEncoder().encode(header.name).length
      + new TextEncoder().encode(header.value).length
  }
  if (totalBytes > 16 * 1024) return '自定义 Headers 总大小超过限制'
  return ''
}

export const tenantModelDraftIsValid = (draft: TenantModelDraft) => Boolean(
  draft.display_name.trim()
  && draft.base_url.trim()
  && draft.model_name.trim()
  && !customHeaderError(draft)
  && !pricingError(draft.pricing),
)

export const protocolDefaultEndpoints = (protocol: ModelProtocol) => ({
  models: '/v1/models',
  inference: protocol === 'anthropic-messages'
    ? '/v1/messages'
    : protocol === 'openai-chat-completions'
      ? '/v1/chat/completions'
      : '/v1/responses',
})
