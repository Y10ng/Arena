import type { ScenarioFeatureSchemaItem } from '@/api/types'

export const DEFAULT_FEATURE_SCHEMA: ScenarioFeatureSchemaItem[] = [
  {
    key: 'sandbox',
    label: 'Sandbox',
    value_type: 'boolean',
    default: false,
    options: [],
    runtime_driver: false,
    description: 'Metadata flag for audit context.',
  },
  {
    key: 'network_flow',
    label: 'Network Flow',
    value_type: 'boolean',
    default: false,
    options: [],
    runtime_driver: true,
    description: 'Controls whether the scenario uses the dispatcher network mode.',
  },
  {
    key: 'isolation',
    label: 'Isolation',
    value_type: 'enum',
    default: 'container',
    options: ['container'],
    runtime_driver: false,
    description: 'Metadata label for execution isolation.',
  },
  {
    key: 'assessment_protocol',
    label: 'Assessment Protocol',
    value_type: 'enum',
    default: 'legacy',
    options: ['legacy', 'default-copy-v1'],
    runtime_driver: false,
    description: 'Versioned structured assessment contract.',
  },
  {
    key: 'outcome_contract_version',
    label: '结果证据契约',
    value_type: 'enum',
    default: '1',
    options: ['1', '2'],
    runtime_driver: false,
    description: '冻结到项目的结构化 Coverage 证据质量规则版本。',
  },
  {
    key: 'blocker_cooldown_version',
    label: 'Blocker 冷却版本',
    value_type: 'enum',
    default: '0',
    options: ['0', '1'],
    runtime_driver: false,
    description: '冻结到项目的目标阻断去重、策略识别与冷却规则版本。',
  },
  {
    key: 'surface_manifest_version',
    label: 'Surface Manifest 版本',
    value_type: 'enum',
    default: '0',
    options: ['0', '1'],
    runtime_driver: false,
    description: '冻结到项目的确定性攻击面身份与追加式发现清单版本。',
  },
  {
    key: 'minimum_coverage_version',
    label: 'Minimum Coverage 版本',
    value_type: 'enum',
    default: '0',
    options: ['0', '1'],
    runtime_driver: false,
    description: '冻结到项目的规则化 required/optional Coverage 派生版本。',
  },
  {
    key: 'quality_gate_version',
    label: 'Quality Snapshot 版本',
    value_type: 'enum',
    default: '0',
    options: ['0', '1'],
    runtime_driver: false,
    description: '冻结到项目的 Evidence-aware 质量快照版本；v1 仅 shadow 观测，不改变完成门。',
  },
  {
    key: 'verifier_contract_version',
    label: 'Independent Verifier 版本',
    value_type: 'enum',
    default: '0',
    options: ['0', '1'],
    runtime_driver: false,
    description: '冻结到项目的逐 Observation 独立漏洞复现契约版本。',
  },
  {
    key: 'report_policy_version',
    label: 'Manual Report Policy Version',
    value_type: 'enum',
    default: '0',
    options: ['0', '1'],
    runtime_driver: false,
    description: '仅控制项目完成后的手动报告能力，不改变探索与完成逻辑。',
  },
  {
    key: 'report_prompt_group',
    label: 'Report Prompt Group',
    value_type: 'string',
    default: '',
    options: [],
    runtime_driver: false,
    description: '手动 durable 报告使用的独立 Prompt Group。',
  },
]

export interface ValidationResult {
  valid: boolean
  errors: string[]
}

export const effectiveFeatureSchema = (schema?: ScenarioFeatureSchemaItem[]) => {
  return schema && schema.length > 0 ? schema : DEFAULT_FEATURE_SCHEMA
}

export const featureDefaults = (schema?: ScenarioFeatureSchemaItem[]): Record<string, boolean | string> => {
  const values: Record<string, boolean | string> = {}
  for (const item of effectiveFeatureSchema(schema)) {
    values[item.key] = item.default
  }
  return values
}

export const featureDraftFromScenario = (
  features: Record<string, unknown> = {},
  schema?: ScenarioFeatureSchemaItem[],
): Record<string, boolean | string> => {
  const items = effectiveFeatureSchema(schema)
  const values = featureDefaults(items)
  for (const item of items) {
    const value = features[item.key]
    if (item.value_type === 'boolean' && typeof value === 'boolean') {
      values[item.key] = value
    } else if (
      (item.value_type === 'enum' || item.value_type === 'string') &&
      typeof value === 'string'
    ) {
      values[item.key] = value
    }
  }
  return values
}

export const featurePayloadFromDraft = (
  draft: Record<string, unknown>,
  schema?: ScenarioFeatureSchemaItem[],
): Record<string, boolean | string> => {
  const payload: Record<string, boolean | string> = {}
  for (const item of effectiveFeatureSchema(schema)) {
    const value = draft[item.key]
    if (item.value_type === 'boolean') {
      payload[item.key] = typeof value === 'boolean' ? value : Boolean(item.default)
    } else if (item.value_type === 'enum' || item.value_type === 'string') {
      payload[item.key] = typeof value === 'string' ? value : String(item.default)
    }
  }
  return payload
}

export const validateFeaturesAgainstSchema = (
  features: Record<string, unknown>,
  schema?: ScenarioFeatureSchemaItem[],
): ValidationResult => {
  const items = effectiveFeatureSchema(schema)
  const byKey = new Map(items.map(item => [item.key, item]))
  const errors: string[] = []

  for (const key of Object.keys(features)) {
    if (!byKey.has(key)) errors.push(`未知 feature: ${key}`)
  }

  for (const item of items) {
    const value = features[item.key]
    if (value === undefined) continue
    if (item.value_type === 'boolean' && typeof value !== 'boolean') {
      errors.push(`${item.key} 必须是布尔值`)
    }
    if (item.value_type === 'enum') {
      if (typeof value !== 'string' || !item.options.includes(value)) {
        errors.push(`${item.key} 必须是: ${item.options.join(', ')}`)
      }
    }
    if (item.value_type === 'string' && typeof value !== 'string') {
      errors.push(`${item.key} 必须是字符串`)
    }
  }

  return { valid: errors.length === 0, errors }
}

export const isKnownImage = (image: string, allowedImages: string[] = []) => {
  const text = image.trim()
  return !text || allowedImages.includes(text)
}

export const invalidCaps = (caps: string[] = [], allowedCaps: string[] = []) => {
  const allowed = new Set(allowedCaps)
  return caps.filter(cap => !allowed.has(cap))
}
