import type { ScenarioCatalogEntry } from '@/api/types'

export interface ScenarioTemplateFieldState {
  value: string
  userTouched: boolean
  lastAutoApplied: string | null
}

export interface ScenarioTemplateState {
  selectedKey: string | null
  origin: ScenarioTemplateFieldState
  goal: ScenarioTemplateFieldState
}

const applyFieldTemplate = (
  field: ScenarioTemplateFieldState,
  template: string | null,
): ScenarioTemplateFieldState => {
  if (field.userTouched) return field

  const isPristine = field.value === '' && field.lastAutoApplied === null
  const isAutoApplied = (
    field.lastAutoApplied !== null
    && field.value === field.lastAutoApplied
  )
  if (!isPristine && !isAutoApplied) return field

  if (template === null) {
    return isAutoApplied
      ? { value: '', userTouched: false, lastAutoApplied: null }
      : field
  }

  return {
    value: template,
    userTouched: false,
    lastAutoApplied: template,
  }
}

export const applyScenarioTemplates = (
  state: ScenarioTemplateState,
  scenario: ScenarioCatalogEntry,
): ScenarioTemplateState => {
  if (state.selectedKey === scenario.key) return state

  return {
    selectedKey: scenario.key,
    origin: applyFieldTemplate(state.origin, scenario.origin_template ?? null),
    goal: applyFieldTemplate(state.goal, scenario.goal_template ?? null),
  }
}
