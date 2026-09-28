import { describe, expect, it } from 'vitest'

import type { Scenario } from '@/api/types'
import { normalizeScenarioForm, scenarioPayloadFromForm } from './scenarioConsoleModel'

const scenario = (overrides: Partial<Scenario> = {}): Scenario => ({
  key: 'default',
  label: 'Default',
  is_default: true,
  profile_id: 'default',
  image: '',
  prompt_group: 'default',
  knowledge: null,
  origin_template: null,
  goal_template: null,
  cap_add: [],
  features: {},
  ...overrides,
})

describe('scenario creation defaults', () => {
  it('loads null templates as empty editor values and serializes empty editors to null', () => {
    const draft = normalizeScenarioForm(scenario())

    expect(draft.origin_template).toBe('')
    expect(draft.goal_template).toBe('')
    expect(scenarioPayloadFromForm(draft).origin_template).toBeNull()
    expect(scenarioPayloadFromForm(draft).goal_template).toBeNull()
  })

  it('preserves template whitespace and sensitive strings verbatim', () => {
    const origin = '  username=admin\npassword=allowed-secret\n'
    const goal = '\nConfirm the exact configured scope.  '
    const payload = scenarioPayloadFromForm(scenario({
      origin_template: origin,
      goal_template: goal,
    }))

    expect(payload.origin_template).toBe(origin)
    expect(payload.goal_template).toBe(goal)
  })
})
