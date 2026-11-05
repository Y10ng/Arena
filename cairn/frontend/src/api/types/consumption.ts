export interface ConsumptionSummary {
  unobserved_bindings?: number
  tokens: number
  responses: number
  partial_responses: number
  unpriced_responses: number
  amounts: { currency: string; amount: string }[]
  usage: { input_tokens: number | null; output_tokens: number | null; cache_read_tokens: number | null; cache_write_tokens: number | null }
}
export interface ConsumptionDay extends ConsumptionSummary {
  date: string
  coverage: 'unavailable' | 'partial'
}
export interface ConsumptionOverview {
  scope: 'global' | 'tenant'
  timezone: string
  enabled_at: string
  generated_at: string
  start: string
  end: string
  coverage: 'recorded_only'
  unobserved_bindings?: number
  total: ConsumptionSummary
  today: ConsumptionDay
  undated: ConsumptionSummary
  days: ConsumptionDay[]
  models: (ConsumptionSummary & {
    model: { model_id?: string; model_revision?: string | number; model_name?: string; display_name?: string; protocol?: string }
    source: string
  })[]
}
