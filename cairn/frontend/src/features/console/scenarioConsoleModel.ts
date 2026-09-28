import type { Scenario } from '@/api/types'
import { clonePlain } from './consoleCommon'

const isRecord = (value: unknown): value is Record<string, unknown> => {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
}

export const normalizeScenarioForm = (scenario: Scenario): Scenario => {
  return {
    ...scenario,
    profile_id: scenario.profile_id || '',
    image: scenario.image || '',
    origin_template: scenario.origin_template ?? '',
    goal_template: scenario.goal_template ?? '',
    cap_add: Array.isArray(scenario.cap_add) ? [...scenario.cap_add] : [],
    features: isRecord(scenario.features) ? clonePlain(scenario.features) : {},
  }
}

export const scenarioPayloadFromForm = (scenario: Scenario): Scenario => {
  return {
    ...normalizeScenarioForm(scenario),
    origin_template: scenario.origin_template === '' ? null : scenario.origin_template,
    goal_template: scenario.goal_template === '' ? null : scenario.goal_template,
  }
}

export const scenarioSnapshot = (scenario: Scenario | null, featureDraft: Record<string, unknown>) => {
  if (!scenario) return ''
  return JSON.stringify({
    scenario: clonePlain(scenario),
    featureDraft: clonePlain(featureDraft),
  })
}
