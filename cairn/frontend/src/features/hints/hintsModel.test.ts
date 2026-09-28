import { describe, expect, it } from 'vitest'
import type { Fact, Hint } from '@/api/types'
import {
  createHintPayload,
  hintAnchorMeta,
  hintCreatorMeta,
  mergeCreatedHint,
  middleHintFacts,
  sortHintsNewestFirst,
} from './hintsModel'

const fact = (id: string): Fact => ({
  id,
  project_id: 'project-a',
  description: id,
  creator: 'system',
  created_at: '2026-08-21T00:00:00Z',
})

const hint = (id: string, overrides: Partial<Hint> = {}): Hint => ({
  id,
  content: id,
  creator: 'usr-1',
  fact_id: null,
  intent_id: null,
  created_at: '2026-08-21T00:00:00Z',
  ...overrides,
})

describe('hintsModel', () => {
  it('keeps only middle facts without mutating the source', () => {
    const facts = [fact('origin'), fact('f001'), fact('goal'), fact('f002')]
    const snapshot = [...facts]

    expect(middleHintFacts(facts).map((item) => item.id)).toEqual(['f001', 'f002'])
    expect(facts).toEqual(snapshot)
  })

  it('preserves creator identities and maps only the system creator', () => {
    expect(hintCreatorMeta('system')).toEqual({ label: 'AGENT 自治系统', tone: 'info' })
    expect(hintCreatorMeta('usr-sensitive-owner')).toEqual({
      label: 'usr-sensitive-owner',
      tone: 'neutral',
    })
  })

  it('uses intent, fact, and project anchor metadata in priority order', () => {
    expect(hintAnchorMeta({ intent_id: 'i001', fact_id: 'f001' })).toEqual({
      kind: 'intent',
      label: 'intent i001',
      tone: 'info',
    })
    expect(hintAnchorMeta({ intent_id: null, fact_id: 'f001' })).toEqual({
      kind: 'fact',
      label: 'fact f001',
      tone: 'success',
    })
    expect(hintAnchorMeta({ intent_id: null, fact_id: null })).toEqual({
      kind: 'project',
      label: '项目级',
      tone: 'neutral',
    })
  })

  it('sorts newest first and preserves source order for equal timestamps', () => {
    const firstEqual = hint('first-equal', { created_at: '2026-08-21T01:00:00Z' })
    const older = hint('older', { created_at: '2026-08-20T23:00:00Z' })
    const secondEqual = hint('second-equal', { created_at: '2026-08-21T01:00:00Z' })
    const source = [firstEqual, older, secondEqual]

    expect(sortHintsNewestFirst(source).map((item) => item.id)).toEqual([
      'first-equal',
      'second-equal',
      'older',
    ])
    expect(source).toEqual([firstEqual, older, secondEqual])
  })

  it('builds exact project, fact, and intent payloads with the existing trim contract', () => {
    const content = '  Authorization: Bearer allowed-secret\npassword=credential  '

    expect(createHintPayload(content, '', '')).toEqual({
      content: 'Authorization: Bearer allowed-secret\npassword=credential',
    })
    expect(createHintPayload(content, '', 'f001')).toEqual({
      content: 'Authorization: Bearer allowed-secret\npassword=credential',
      fact_id: 'f001',
    })
    expect(createHintPayload(content, 'i001', 'stale-fact')).toEqual({
      content: 'Authorization: Bearer allowed-secret\npassword=credential',
      intent_id: 'i001',
    })
  })

  it('merges returned hints by identity without duplicate rows or source mutation', () => {
    const original = hint('h001', { content: 'old', created_at: '2026-08-20T00:00:00Z' })
    const source = [original]
    const replacement = hint('h001', {
      content: 'token=allowed-secret',
      created_at: '2026-08-21T02:00:00Z',
    })
    const created = hint('h002', { created_at: '2026-08-21T03:00:00Z' })

    expect(mergeCreatedHint(source, replacement)).toEqual([replacement])
    expect(mergeCreatedHint(source, created).map((item) => item.id)).toEqual(['h002', 'h001'])
    expect(source).toEqual([original])
  })
})
