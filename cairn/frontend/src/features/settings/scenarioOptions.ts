import type { Scenario, ScenarioOptions, ScenarioProfileOption } from '@/api/types'

export const BASE_CAPS = ['NET_RAW', 'NET_ADMIN', 'SYS_ADMIN', 'SYS_RAWIO']
export const DEFAULT_PROFILE_ID = 'default'
export const BASE_KNOWLEDGE_OPTIONS = [
  { value: 'knowledge_base', label: '标准知识库' },
]

interface ScenarioOptionInputs {
  scenarios: Scenario[]
  options?: ScenarioOptions
  selectedScenario?: Scenario | null
}

const addText = (values: Set<string>, value: unknown) => {
  const text = typeof value === 'string' ? value.trim() : ''
  if (text) values.add(text)
}

export const ensureDefaultScenario = (list: Scenario[]) => {
  const firstDefaultIndex = list.findIndex(s => s.is_default)
  if (firstDefaultIndex >= 0) {
    return list.map((scenario, index) => ({ ...scenario, is_default: index === firstDefaultIndex }))
  }
  if (list.length === 0) return list
  return list.map((scenario, index) => ({ ...scenario, is_default: index === 0 }))
}

export const buildAvailableImageOptions = ({
  scenarios,
  options,
  selectedScenario,
}: ScenarioOptionInputs) => {
  const values = new Set<string>()
  if (options) {
    for (const image of options.images || []) addText(values, image)
    return Array.from(values)
  }
  for (const scenario of scenarios) addText(values, scenario.image)
  addText(values, selectedScenario?.image)
  return Array.from(values)
}

export const buildAvailableCaps = ({
  scenarios,
  options,
  selectedScenario,
}: ScenarioOptionInputs) => {
  const values = new Set<string>()
  if (options) {
    for (const cap of options.cap_add || []) addText(values, cap)
    return Array.from(values)
  }
  for (const cap of BASE_CAPS) addText(values, cap)
  for (const scenario of scenarios) {
    for (const cap of scenario.cap_add || []) addText(values, cap)
  }
  for (const cap of selectedScenario?.cap_add || []) addText(values, cap)
  return Array.from(values)
}

export const buildPromptGroupOptions = ({
  scenarios,
  options,
  selectedScenario,
}: ScenarioOptionInputs) => {
  const values = new Set<string>()
  for (const group of options?.prompt_groups || ['default']) addText(values, group)
  for (const scenario of scenarios) addText(values, scenario.prompt_group)
  addText(values, selectedScenario?.prompt_group)
  return Array.from(values)
}

export const buildKnowledgeOptions = ({
  scenarios,
  options,
  selectedScenario,
}: ScenarioOptionInputs) => {
  const values = new Map(BASE_KNOWLEDGE_OPTIONS.map(option => [option.value, option.label]))
  for (const value of options?.knowledge || []) {
    const text = value.trim()
    if (text && !values.has(text)) values.set(text, text)
  }
  for (const scenario of scenarios) {
    const text = scenario.knowledge?.trim() || ''
    if (text && !values.has(text)) values.set(text, text)
  }
  const selected = selectedScenario?.knowledge?.trim()
  if (selected && !values.has(selected)) values.set(selected, selected)
  return Array.from(values, ([value, label]) => ({ value, label }))
}

export const buildAvailableProfileOptions = ({
  scenarios,
  options,
  selectedScenario,
}: ScenarioOptionInputs) => {
  const byId = new Map<string, ScenarioProfileOption>()
  byId.set(DEFAULT_PROFILE_ID, emptyProfile(DEFAULT_PROFILE_ID))
  for (const profile of options?.profiles || []) {
    const id = profile.id.trim()
    if (id) byId.set(id, profile)
  }
  for (const scenario of scenarios) addProfilePlaceholder(byId, scenario.profile_id)
  addProfilePlaceholder(byId, selectedScenario?.profile_id)
  return Array.from(byId.values())
}

export const profileSummary = (profile: ScenarioProfileOption) => {
  const parts = [
    profile.image || (profile.image_ref ? `image_ref=${profile.image_ref}` : ''),
    profile.network_policy ? `network=${profile.network_policy}` : '',
    profile.cap_add.length ? `cap=${profile.cap_add.join(',')}` : 'cap=none',
    profile.isolation || '',
  ].filter(Boolean)
  return parts.join(' · ') || '无额外画像'
}

export const profileLabel = (profile: ScenarioProfileOption) => {
  const label = profile.label?.trim()
  return label && label !== profile.id ? `${label} (${profile.id})` : profile.id
}

const addProfilePlaceholder = (values: Map<string, ScenarioProfileOption>, value: unknown) => {
  const id = typeof value === 'string' ? value.trim() : ''
  if (id && !values.has(id)) values.set(id, emptyProfile(id))
}

const emptyProfile = (id: string): ScenarioProfileOption => ({
  id,
  label: id,
  image_ref: '',
  image: '',
  network_policy: '',
  cap_add: [],
  isolation: '',
  completed_action: '',
})
