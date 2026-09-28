import { assert, describe, it } from 'vitest'

import type { ScenarioFeatureSchemaItem } from '@/api/types'
import {
  featureDraftFromScenario,
  featurePayloadFromDraft,
  invalidCaps,
  isKnownImage,
  validateFeaturesAgainstSchema,
} from './featuresSchema'

const schema: ScenarioFeatureSchemaItem[] = [
  {
    key: 'network_flow',
    label: 'Network Flow',
    value_type: 'boolean',
    default: false,
    options: [],
    runtime_driver: true,
    description: '',
  },
  {
    key: 'isolation',
    label: 'Isolation',
    value_type: 'enum',
    default: 'container',
    options: ['container'],
    runtime_driver: false,
    description: '',
  },
]

describe('features schema helpers', () => {
  it('hydrates a schema-shaped draft with defaults', () => {
    const draft = featureDraftFromScenario({ network_flow: true }, schema)

    assert.deepEqual(draft, {
      network_flow: true,
      isolation: 'container',
    })
  })

  it('serializes only schema keys', () => {
    const payload = featurePayloadFromDraft(
      { network_flow: true, isolation: 'container', unknown: true },
      schema,
    )

    assert.deepEqual(payload, {
      network_flow: true,
      isolation: 'container',
    })
  })

  it('rejects unknown keys and invalid value types', () => {
    const result = validateFeaturesAgainstSchema(
      { network_flow: 'yes', isolation: 'process', unknown: true },
      schema,
    )

    assert.isFalse(result.valid)
    assert.include(result.errors.join('; '), 'network_flow')
    assert.include(result.errors.join('; '), 'isolation')
    assert.include(result.errors.join('; '), 'unknown')
  })

  it('checks image and cap legality against backend options', () => {
    assert.isTrue(isKnownImage('', ['cairn/default:latest']))
    assert.isTrue(isKnownImage('cairn/default:latest', ['cairn/default:latest']))
    assert.isFalse(isKnownImage('cairn/custom:latest', ['cairn/default:latest']))
    assert.deepEqual(invalidCaps(['NET_RAW', 'SYS_ADMIN'], ['NET_RAW']), ['SYS_ADMIN'])
  })
})
