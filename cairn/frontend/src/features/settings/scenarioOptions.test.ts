import { assert, describe, it } from 'vitest'

import type { Scenario, ScenarioOptions } from '@/api/types'
import {
  DEFAULT_PROFILE_ID,
  buildAvailableProfileOptions,
  buildAvailableCaps,
  buildAvailableImageOptions,
  buildKnowledgeOptions,
  buildPromptGroupOptions,
  ensureDefaultScenario,
  profileSummary,
} from './scenarioOptions'

const makeScenario = (overrides: Partial<Scenario> = {}): Scenario => ({
  key: 'default',
  label: 'Default',
  is_default: false,
  profile_id: DEFAULT_PROFILE_ID,
  image: '',
  prompt_group: 'default',
  knowledge: 'knowledge_base',
  cap_add: [],
  features: {},
  ...overrides,
})

const makeOptions = (overrides: Partial<ScenarioOptions> = {}): ScenarioOptions => ({
  profiles: [],
  prompt_groups: [],
  knowledge: [],
  images: [],
  cap_add: [],
  cap_add_source: 'dispatcher_report',
  features: [],
  project_counts: {},
  ...overrides,
})

describe('scenario option helpers', () => {
  it('keeps exactly one default scenario', () => {
    const scenarios = ensureDefaultScenario([
      makeScenario({ key: 'alpha', is_default: false }),
      makeScenario({ key: 'beta', is_default: true }),
      makeScenario({ key: 'gamma', is_default: true }),
    ])

    assert.deepEqual(
      scenarios.map(scenario => [scenario.key, scenario.is_default]),
      [
        ['alpha', false],
        ['beta', true],
        ['gamma', false],
      ],
    )
  })

  it('falls back to the first scenario when none is marked default', () => {
    const scenarios = ensureDefaultScenario([
      makeScenario({ key: 'alpha' }),
      makeScenario({ key: 'beta' }),
    ])

    assert.deepEqual(
      scenarios.map(scenario => [scenario.key, scenario.is_default]),
      [
        ['alpha', true],
        ['beta', false],
      ],
    )
  })

  it('merges execution profile options from defaults, backend options, and drafts', () => {
    const profiles = buildAvailableProfileOptions({
      scenarios: [makeScenario({ profile_id: 'stored' })],
      options: makeOptions({ profiles: [{ id: 'network', label: 'network', image_ref: 'default', image: 'cairn/default:latest', network_policy: 'host', cap_add: [], isolation: 'shared_container', completed_action: 'stop' }] }),
      selectedScenario: makeScenario({ profile_id: 'draft' }),
    })

    assert.deepEqual(profiles.map(profile => profile.id), [DEFAULT_PROFILE_ID, 'network', 'stored', 'draft'])
    assert.equal(profileSummary(profiles[1]), 'cairn/default:latest · network=host · cap=none · shared_container')
  })

  it('keeps prompt and knowledge references visible even when backend options do not include them', () => {
    const scenarios = [
      makeScenario({
        image: 'cairn/stored:latest',
        prompt_group: 'archived_group',
        knowledge: 'web_standards',
        cap_add: ['NET_RAW'],
      }),
    ]
    const selectedScenario = makeScenario({
      image: 'cairn/draft:latest',
      prompt_group: 'draft_group',
      knowledge: 'draft_knowledge',
      cap_add: ['SYS_ADMIN'],
    })
    const options = makeOptions({
      images: ['cairn/backend:latest'],
      prompt_groups: ['default'],
      knowledge: ['knowledge_base'],
      cap_add: ['NET_ADMIN'],
    })

    assert.deepEqual(
      buildPromptGroupOptions({ scenarios, options, selectedScenario }),
      ['default', 'archived_group', 'draft_group'],
    )
    assert.deepEqual(
      buildKnowledgeOptions({ scenarios, options, selectedScenario }).map(option => option.value),
      ['knowledge_base', 'web_standards', 'draft_knowledge'],
    )
  })

  it('uses backend image and cap lists as the selectable runtime policy when present', () => {
    const scenarios = [makeScenario({ image: 'cairn/stored:latest', cap_add: ['NET_RAW'] })]
    const selectedScenario = makeScenario({ image: 'cairn/draft:latest', cap_add: ['SYS_ADMIN'] })
    const options = makeOptions({
      images: ['cairn/backend:latest'],
      cap_add: ['NET_ADMIN'],
      cap_add_source: 'dispatcher_report',
    })

    assert.deepEqual(
      buildAvailableImageOptions({ scenarios, options, selectedScenario }),
      ['cairn/backend:latest'],
    )
    assert.deepEqual(
      buildAvailableCaps({ scenarios, options, selectedScenario }),
      ['NET_ADMIN'],
    )
  })
})
