import { assert, describe, it } from 'vitest'

import type { KnowledgeFile, KnowledgeSet } from '@/api/types'
import {
  BUILTIN_KNOWLEDGE_KEY,
  buildKnowledgeSelectOptions,
  isKnowledgeReferenceValid,
  normalizeKnowledgeFiles,
  normalizeKnowledgeSets,
} from './knowledgeOptions'

const makeSet = (overrides: Partial<KnowledgeSet> = {}): KnowledgeSet => ({
  key: 'playbooks',
  path: '/knowledge/playbooks',
  builtin: false,
  file_count: 1,
  referenced_by: [],
  ...overrides,
})

const makeFile = (overrides: Partial<KnowledgeFile> = {}): KnowledgeFile => ({
  name: 'README.md',
  path: 'README.md',
  size: 10,
  updated_at: null,
  warnings: [],
  ...overrides,
})

describe('knowledge option helpers', () => {
  it('always includes the builtin knowledge set first', () => {
    const sets = normalizeKnowledgeSets([
      makeSet({ key: 'web_standards' }),
      makeSet({ key: BUILTIN_KNOWLEDGE_KEY, file_count: 3 }),
      makeSet({ key: 'coding_rules' }),
    ])

    assert.deepEqual(sets.map(set => set.key), [
      BUILTIN_KNOWLEDGE_KEY,
      'coding_rules',
      'web_standards',
    ])
    assert.equal(sets[0].builtin, true)
    assert.equal(sets[0].file_count, 0)
  })

  it('accepts an explicit no-knowledge selection', () => {
    const options = buildKnowledgeSelectOptions({
      sets: [makeSet({ key: 'playbooks' })],
      selectedKnowledge: null,
    })

    assert.equal(options[0].value, null)
    assert.equal(options[0].label, '无知识库（场景独立运行）')
    assert.equal(isKnowledgeReferenceValid(null, [makeSet({ key: 'playbooks' })]), true)
  })

  it('marks missing selected knowledge as invalid but keeps it visible', () => {
    const options = buildKnowledgeSelectOptions({
      sets: [makeSet({ key: 'playbooks' })],
      selectedKnowledge: 'missing_set',
    })

    const missing = options.find(option => option.value === 'missing_set')
    assert.equal(missing?.exists, false)
    assert.equal(isKnowledgeReferenceValid('missing_set', [makeSet({ key: 'playbooks' })]), false)
    assert.equal(isKnowledgeReferenceValid('playbooks', [makeSet({ key: 'playbooks' })]), true)
  })

  it('sorts knowledge files and drops empty paths', () => {
    const files = normalizeKnowledgeFiles([
      makeFile({ path: 'zeta.md' }),
      makeFile({ path: '' }),
      makeFile({ path: 'alpha/README.md' }),
    ])

    assert.deepEqual(files.map(file => file.path), ['alpha/README.md', 'zeta.md'])
  })
})
