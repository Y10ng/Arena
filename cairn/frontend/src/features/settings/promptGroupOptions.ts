import type {
  PromptFile,
  PromptGroup,
  PromptGroupCapability,
  PromptGroupVisibility,
} from '@/api/types'

export const PROMPT_REQUIRED_FILE_ORDER = [
  'reason.md',
  'explore.md',
  'explore_conclude.md',
  'bootstrap.md',
  'bootstrap_conclude.md',
]

export const DEFAULT_PROMPT_REQUIRED_TOKENS: Record<string, string[]> = {
  'reason.md': [
    '{graph_yaml}',
    '{fact_ids}',
    '{open_intents}',
    '{max_intents}',
    '{knowledge_base}',
  ],
  'explore.md': ['{graph_yaml}', '{intent_id}', '{intent_description}', '{knowledge_base}'],
  'explore_conclude.md': [
    '{graph_yaml}',
    '{intent_id}',
    '{intent_description}',
    '{knowledge_base}',
  ],
  'bootstrap.md': ['{origin}', '{goal}', '{hints}', '{targets}', '{knowledge_base}'],
  'bootstrap_conclude.md': ['{origin}', '{goal}', '{hints}', '{targets}', '{knowledge_base}'],
}

const PROMPT_GROUP_CAPABILITIES: PromptGroupCapability[] = ['exploration', 'durable-report']
const PROMPT_GROUP_VISIBILITIES: PromptGroupVisibility[] = [
  'visible',
  'compatibility-only',
  'test-only',
]

export interface PromptGroupSelectOption {
  value: string
  label: string
  exists: boolean
  builtin: boolean
  managed: boolean
  overridesBuiltin: boolean
  valid: boolean
  compatible: boolean
  selectable: boolean
  visibility: PromptGroupVisibility | 'missing'
  capabilities: PromptGroupCapability[]
  fileCount: number
  referencedBy: string[]
}

const normalizedCapabilities = (group: PromptGroup) => {
  if (!Array.isArray(group.capabilities)) return ['exploration'] satisfies PromptGroupCapability[]
  return PROMPT_GROUP_CAPABILITIES.filter((capability) => group.capabilities.includes(capability))
}

const normalizedVisibility = (group: PromptGroup): PromptGroupVisibility =>
  PROMPT_GROUP_VISIBILITIES.includes(group.visibility) ? group.visibility : 'visible'

const addPromptGroup = (values: Map<string, PromptGroup>, group: PromptGroup) => {
  const key = group.key.trim()
  if (!key || values.has(key)) return
  values.set(key, {
    ...group,
    key,
    path: group.path || '',
    builtin: Boolean(group.builtin),
    managed: Boolean(group.managed),
    overrides_builtin: Boolean(group.overrides_builtin),
    capabilities: normalizedCapabilities(group),
    visibility: normalizedVisibility(group),
    valid: group.valid !== false,
    file_count: Number.isFinite(group.file_count) ? group.file_count : 0,
    referenced_by: Array.isArray(group.referenced_by) ? [...group.referenced_by] : [],
    warnings: Array.isArray(group.warnings) ? [...group.warnings] : [],
  })
}

export const promptGroupKindLabel = (
  group: Pick<PromptGroup, 'builtin' | 'managed' | 'overrides_builtin'>,
) => {
  if (group.overrides_builtin) return '覆盖内置'
  if (group.builtin) return '内置'
  if (group.managed) return '自定义'
  return '未知'
}

export const promptGroupRoleLabel = (group: Pick<PromptGroup, 'capabilities'>) => {
  const exploration = group.capabilities.includes('exploration')
  const report = group.capabilities.includes('durable-report')
  if (exploration && report) return '探索 + 报告'
  if (exploration) return '探索'
  if (report) return '仅报告'
  return '用途未知'
}

export const normalizePromptGroups = (groups: PromptGroup[] = []) => {
  const values = new Map<string, PromptGroup>()
  for (const group of groups) addPromptGroup(values, group)
  return Array.from(values.values()).sort((a, b) => {
    if (a.key === 'default') return -1
    if (b.key === 'default') return 1
    if (a.builtin !== b.builtin) return a.builtin ? -1 : 1
    return a.key.localeCompare(b.key)
  })
}

export const promptGroupSupportsCapability = (
  group: Pick<PromptGroup, 'capabilities'>,
  capability: PromptGroupCapability,
) => group.capabilities.includes(capability)

export const isPromptGroupVisibleInConsole = (
  group: Pick<PromptGroup, 'visibility' | 'managed' | 'builtin' | 'overrides_builtin'>,
) => group.visibility === 'visible' || (group.managed && !group.builtin && !group.overrides_builtin)

export const visiblePromptGroupsForConsole = (groups: PromptGroup[] = []) =>
  normalizePromptGroups(groups).filter(isPromptGroupVisibleInConsole)

export const buildPromptGroupSelectOptions = ({
  groups,
  selectedPromptGroup,
  capability,
}: {
  groups?: PromptGroup[]
  selectedPromptGroup?: string | null
  capability: PromptGroupCapability
}) => {
  const normalized = normalizePromptGroups(groups)
  const selected = selectedPromptGroup?.trim()
  const choices = normalized.filter(
    (group) => group.visibility === 'visible' && promptGroupSupportsCapability(group, capability),
  )
  const selectedGroup = selected ? normalized.find((group) => group.key === selected) : undefined
  if (selectedGroup && !choices.some((group) => group.key === selectedGroup.key))
    choices.push(selectedGroup)

  const options: PromptGroupSelectOption[] = choices.map((group) => {
    const compatible = promptGroupSupportsCapability(group, capability)
    const selectedHiddenGroup = group.key === selected && group.visibility !== 'visible'
    const visibilityLabel = selectedHiddenGroup
      ? group.visibility === 'compatibility-only'
        ? ' · 兼容保留（仅当前选择）'
        : ' · 测试保留（仅当前选择）'
      : group.visibility === 'test-only'
        ? ' · 仅测试'
        : group.visibility !== 'visible'
          ? ' · 已隐藏'
          : ''
    const compatibilityLabel = compatible
      ? ''
      : ` · 不支持${capability === 'exploration' ? '探索' : '报告'}`
    return {
      value: group.key,
      label: `${group.key} (${promptGroupKindLabel(group)} · ${promptGroupRoleLabel(group)})${visibilityLabel}${compatibilityLabel}`,
      exists: true,
      builtin: group.builtin,
      managed: group.managed,
      overridesBuiltin: group.overrides_builtin,
      valid: group.valid,
      compatible,
      selectable: group.visibility === 'visible' && compatible,
      visibility: group.visibility,
      capabilities: [...group.capabilities],
      fileCount: group.file_count,
      referencedBy: [...group.referenced_by],
    }
  })

  if (selected && !selectedGroup) {
    options.push({
      value: selected,
      label: `${selected} (不存在)`,
      exists: false,
      builtin: false,
      managed: false,
      overridesBuiltin: false,
      valid: false,
      compatible: false,
      selectable: false,
      visibility: 'missing',
      capabilities: [],
      fileCount: 0,
      referencedBy: [],
    })
  }

  return options
}

export const isPromptGroupReferenceValid = (
  key: string | null | undefined,
  groups: PromptGroup[] = [],
  capability: PromptGroupCapability,
  preservedVisibilities: PromptGroupVisibility[] = [],
) => {
  const text = key?.trim()
  if (!text) return false
  return normalizePromptGroups(groups).some((group) => {
    if (group.key !== text || !group.valid) return false
    if (preservedVisibilities.includes(group.visibility)) return true
    return group.visibility === 'visible' && promptGroupSupportsCapability(group, capability)
  })
}

export const normalizePromptFiles = (files: PromptFile[] = []) => {
  const byPath = new Map<string, PromptFile>()
  for (const file of files) {
    const path = file.path.trim()
    if (!path) continue
    byPath.set(path, {
      ...file,
      path,
      name: file.name || path,
      required_tokens: Array.isArray(file.required_tokens) ? [...file.required_tokens] : [],
      missing_tokens: Array.isArray(file.missing_tokens) ? [...file.missing_tokens] : [],
    })
  }
  return Array.from(byPath.values()).sort((a, b) => {
    const orderA = PROMPT_REQUIRED_FILE_ORDER.indexOf(a.path)
    const orderB = PROMPT_REQUIRED_FILE_ORDER.indexOf(b.path)
    if (orderA !== -1 || orderB !== -1) {
      return (orderA === -1 ? 99 : orderA) - (orderB === -1 ? 99 : orderB)
    }
    return a.path.localeCompare(b.path)
  })
}

export const missingPromptTokens = (content: string, requiredTokens: string[] = []) => {
  return requiredTokens.filter((token) => !content.includes(token))
}
