import { describe, expect, it } from 'vitest'
import type { Fact, FactEvidence, TaskRunResultEvent, TaskRunResultEventKind } from '@/api/types'
import {
  AUDIT_EVENT_FILTER_OPTIONS,
  AUDIT_EVENT_KINDS,
  AUDIT_EVENT_KIND_META,
  auditEventKindMeta,
  auditEventSummary,
  auditEvidenceStatus,
  buildAuditEvidenceRows,
  calculateAuditKpis,
  filterAndSortAuditEvents,
  flattenResultEventGroups,
  formatAuditConfidence,
  reconcileExpandedEventId,
} from './auditModel'
import type { AuditEvidenceLoadResult, AuditEvidenceRow } from './auditModel'

let eventSequence = 0

const event = (
  kind: TaskRunResultEventKind,
  payload: Record<string, unknown> = {},
  overrides: Partial<TaskRunResultEvent> = {},
): TaskRunResultEvent => {
  eventSequence += 1
  return {
    id: `event-${eventSequence}`,
    task_run_id: 'run-a',
    project_id: 'project-a',
    ts: '2026-08-21T00:00:00Z',
    kind,
    payload,
    ...overrides,
  }
}

const fact = (overrides: Partial<Fact> = {}): Fact => ({
  id: 'fact-a',
  project_id: 'project-a',
  description: '完整 Fact 描述',
  task_run_id: 'run-a',
  creator: 'worker',
  created_at: '2026-08-21T00:00:00Z',
  ...overrides,
})

const evidence = (overrides: Partial<FactEvidence> = {}): FactEvidence => ({
  project_id: 'project-a',
  fact_id: 'fact-a',
  artifact_id: 'artifact-a',
  pointer: 'lines 1-20',
  verifier_run_id: 'run-verifier',
  artifact_exists: true,
  verifier_run_exists: true,
  evidence_valid: true,
  created_at: '2026-08-21T00:00:00Z',
  ...overrides,
})

describe('audit event metadata and transforms', () => {
  it('exposes all event kinds, filters, labels, and supported Badge tones', () => {
    expect(AUDIT_EVENT_KINDS).toEqual([
      'validation_result',
      'protocol_write',
      'network_flow',
      'decision_result',
    ])
    expect(AUDIT_EVENT_FILTER_OPTIONS.map(({ value }) => value)).toEqual([
      'all',
      ...AUDIT_EVENT_KINDS,
    ])
    expect(AUDIT_EVENT_KIND_META).toEqual({
      validation_result: { label: '结果校验', tone: 'success' },
      protocol_write: { label: '协议写入', tone: 'info' },
      network_flow: { label: '网络流量', tone: 'warning' },
      decision_result: { label: '推理决策', tone: 'neutral' },
    })
    for (const kind of AUDIT_EVENT_KINDS)
      expect(auditEventKindMeta(kind)).toBe(AUDIT_EVENT_KIND_META[kind])
  })

  it('flattens grouped events in source order without cloning or changing payloads', () => {
    const secretPayload = Object.freeze({
      password: 'p@ss word',
      Cookie: 'session=top-secret',
      token: 'token\nsecond-line',
    })
    const first = Object.freeze(event('validation_result', secretPayload))
    const second = Object.freeze(event('network_flow', { status: 'observed' }))
    const groups: readonly { task_run_id: string; events: TaskRunResultEvent[] }[] = Object.freeze([
      Object.freeze({ task_run_id: 'run-a', events: [first] }),
      Object.freeze({ task_run_id: 'run-b', events: [second] }),
    ])

    const flattened = flattenResultEventGroups(groups)

    expect(flattened).toEqual([first, second])
    expect(flattened[0]).toBe(first)
    expect(flattened[0]?.payload).toBe(secretPayload)
    expect(groups[0]?.events).toEqual([first])
  })

  it('filters every kind and stably sorts valid timestamps newest-first', () => {
    const tiedFirst = event('network_flow', {}, { id: 'tied-first', ts: '2026-08-21T02:00:00Z' })
    const oldest = event('validation_result', {}, { id: 'oldest', ts: '2026-08-21T01:00:00Z' })
    const tiedSecond = event(
      'decision_result',
      {},
      { id: 'tied-second', ts: '2026-08-21T02:00:00Z' },
    )
    const newest = event('protocol_write', {}, { id: 'newest', ts: '2026-08-21T03:00:00Z' })
    const source = Object.freeze([tiedFirst, oldest, tiedSecond, newest])

    expect(filterAndSortAuditEvents(source, 'all').map(({ id }) => id)).toEqual([
      'newest',
      'tied-first',
      'tied-second',
      'oldest',
    ])
    for (const kind of AUDIT_EVENT_KINDS) {
      expect(filterAndSortAuditEvents(source, kind).every((item) => item.kind === kind)).toBe(true)
    }
    expect(filterAndSortAuditEvents(source, 'network_flow')).toEqual([tiedFirst])
    expect(source).toEqual([tiedFirst, oldest, tiedSecond, newest])
  })

  it('puts invalid timestamps after valid timestamps and preserves their source order', () => {
    const invalidFirst = event('validation_result', {}, { id: 'invalid-first', ts: 'not-a-date' })
    const valid = event('validation_result', {}, { id: 'valid', ts: '2026-08-21T00:00:00Z' })
    const invalidSecond = event('validation_result', {}, { id: 'invalid-second', ts: '' })

    expect(
      filterAndSortAuditEvents([invalidFirst, valid, invalidSecond], 'all').map(({ id }) => id),
    ).toEqual(['valid', 'invalid-first', 'invalid-second'])
  })

  it('retains an expanded event only while it remains in the supplied event snapshot', () => {
    const first = event('validation_result', {}, { id: 'first' })
    const second = event('network_flow', {}, { id: 'second' })

    expect(reconcileExpandedEventId('second', [first, second])).toBe('second')
    expect(reconcileExpandedEventId('deleted', [first, second])).toBeNull()
    expect(reconcileExpandedEventId('second', [first])).toBeNull()
    expect(reconcileExpandedEventId(null, [first, second])).toBeNull()
  })
})

describe('audit KPI and summary semantics', () => {
  it('counts canonical validation status, protocol status, and decision outcome with unknown buckets', () => {
    const events = [
      event('validation_result', { status: 'valid', outcome: 'invalid' }),
      event('validation_result', { status: 'invalid' }),
      event('validation_result', { status: 'rejected' }),
      event('validation_result', { status: 'future-value' }),
      event('validation_result', {}),
      event('protocol_write', { status: 'success', write_status: 'failed' }),
      event('protocol_write', { status: 'failed' }),
      event('protocol_write', { status: 'skipped' }),
      event('protocol_write', { write_status: 'success' }),
      event('decision_result', { outcome: 'complete' }),
      event('decision_result', { outcome: 'fact' }),
      event('decision_result', { outcome: 'intents' }),
      event('decision_result', { outcome: 'noop' }),
      event('decision_result', { outcome: 'future-value' }),
      event('network_flow', { status: 'blocked' }),
    ]

    expect(calculateAuditKpis(events)).toEqual({
      validation: { valid: 1, invalid: 1, rejected: 1, unknown: 2 },
      protocolWrite: { success: 1, failed: 1, skipped: 1, unknown: 1 },
      decision: { complete: 1, fact: 1, intents: 1, noop: 1, unknown: 1 },
    })
  })

  it('formats confidence zero, numeric strings, and both valid boundaries correctly', () => {
    expect(formatAuditConfidence(0)).toBe('0%')
    expect(formatAuditConfidence('0')).toBe('0%')
    expect(formatAuditConfidence(0.125)).toBe('13%')
    expect(formatAuditConfidence('0.999')).toBe('100%')
    expect(formatAuditConfidence(1)).toBe('100%')
  })

  it('reports missing, boolean, non-finite, malformed, and out-of-range confidence as unknown', () => {
    for (const value of [
      undefined,
      null,
      true,
      false,
      '',
      ' ',
      'invalid',
      NaN,
      Infinity,
      -0.01,
      1.01,
    ]) {
      expect(formatAuditConfidence(value)).toBe('未知')
    }
  })

  it('builds kind-specific summaries from canonical fields without truncating sensitive multiline text', () => {
    const rejection = '拒绝原因\npassword=hunter2\nCookie: session=secret'
    const response = `response-token=${'x'.repeat(240)}\nsecond line`
    const rationale = `完整理由\napi_key=sk-test-${'y'.repeat(240)}\n<script>alert(1)</script>`
    const networkError = `network error\nAuthorization: Bearer ${'z'.repeat(240)}`

    const validationSummary = auditEventSummary(
      event('validation_result', {
        status: 'rejected',
        outcome: 'fact',
        rejection_reason: rejection,
        error_message: 'validator-error-id-123',
      }),
    )
    const protocolSummary = auditEventSummary(
      event('protocol_write', {
        status: 'failed',
        write_status: 'success',
        fact_id: 'fact-sensitive-id',
        response_text: response,
      }),
    )
    const decisionSummary = auditEventSummary(
      event('decision_result', {
        outcome: 'noop',
        confidence: 0,
        rationale,
        completed_fact_id: 'fact-complete-sensitive-id',
        error_message: 'decision-error-full-text',
      }),
    )
    const networkSummary = auditEventSummary(
      event('network_flow', {
        status: 'failed',
        flow_id: 'flow-full-identifier',
        artifact_id: 'artifact-full-identifier',
        error_message: networkError,
      }),
    )

    expect(validationSummary).toContain('状态: rejected')
    expect(validationSummary).toContain(rejection)
    expect(validationSummary).toContain('validator-error-id-123')
    expect(protocolSummary).toContain('状态: failed')
    expect(protocolSummary).not.toContain('状态: success')
    expect(protocolSummary).toContain('fact-sensitive-id')
    expect(protocolSummary).toContain(response)
    expect(decisionSummary).toContain('置信度: 0%')
    expect(decisionSummary).toContain(rationale)
    expect(decisionSummary).toContain('fact-complete-sensitive-id')
    expect(decisionSummary).toContain('decision-error-full-text')
    expect(networkSummary).toContain('flow-full-identifier')
    expect(networkSummary).toContain('artifact-full-identifier')
    expect(networkSummary).toContain(networkError)
  })

  it('includes historical network-flow aliases in the auxiliary summary', () => {
    const summary = auditEventSummary(
      event('network_flow', {
        plane: 'target',
        transport: 'tcp',
        action: 'blocked',
        bytes: 128,
      }),
    )

    expect(summary).toContain('状态: blocked')
    expect(summary).toContain('网络平面: target')
    expect(summary).toContain('协议: tcp')
    expect(summary).toContain('字节数: 128')
  })
})

describe('audit evidence row model', () => {
  it('treats evidence_valid as authoritative and explains every invalid deletion state', () => {
    expect(
      auditEvidenceStatus({
        evidence_valid: true,
        artifact_exists: false,
        verifier_run_exists: false,
      }),
    ).toEqual({ label: '证据有效', tone: 'success' })
    expect(
      auditEvidenceStatus({
        evidence_valid: false,
        artifact_exists: false,
        verifier_run_exists: false,
      }),
    ).toEqual({ label: 'Artifact 与 verifier Run 均已删除，证据无效', tone: 'danger' })
    expect(
      auditEvidenceStatus({
        evidence_valid: false,
        artifact_exists: false,
        verifier_run_exists: true,
      }),
    ).toEqual({ label: 'Artifact 已删除，证据无效', tone: 'danger' })
    expect(
      auditEvidenceStatus({
        evidence_valid: false,
        artifact_exists: true,
        verifier_run_exists: false,
      }),
    ).toEqual({ label: 'Verifier Run 已删除，证据无效', tone: 'danger' })
    expect(
      auditEvidenceStatus({
        evidence_valid: false,
        artifact_exists: true,
        verifier_run_exists: null,
      }),
    ).toEqual({ label: '证据无效', tone: 'danger' })
  })

  it('creates one discriminated row per binding plus explicit none and error rows', () => {
    const firstFact = fact({ id: 'fact-bindings' })
    const noneFact = fact({ id: 'fact-none' })
    const errorFact = fact({ id: 'fact-error' })
    const firstEvidence = evidence({ fact_id: firstFact.id, artifact_id: 'artifact-one' })
    const secondEvidence = evidence({ fact_id: firstFact.id, artifact_id: 'artifact-two' })
    const requestError = new Error('credential-shaped request failure: token=secret')
    const results: AuditEvidenceLoadResult[] = [
      { status: 'success', fact: firstFact, evidence: [firstEvidence, secondEvidence] },
      { status: 'success', fact: noneFact, evidence: [] },
      { status: 'error', fact: errorFact, error: requestError },
    ]

    const rows = buildAuditEvidenceRows('project-a', results)

    expect(rows.map(({ kind }) => kind)).toEqual(['evidence', 'evidence', 'none', 'error'])
    expect(rows[0]).toMatchObject({ kind: 'evidence', fact: firstFact, evidence: firstEvidence })
    expect(rows[1]).toMatchObject({ kind: 'evidence', fact: firstFact, evidence: secondEvidence })
    expect(rows[2]).toMatchObject({ kind: 'none', fact: noneFact })
    expect(rows[3]).toMatchObject({ kind: 'error', fact: errorFact, error: requestError })
    expect((rows[3] as Extract<AuditEvidenceRow, { kind: 'error' }>).error).toBe(requestError)
  })

  it('preserves complete Fact descriptions, pointers, DTO identity, and source arrays', () => {
    const description = `Fact description\npassword=full-secret\n${'d'.repeat(300)}`
    const pointer = `$.credentials[0].token\nCookie=session-value\n${'p'.repeat(300)}`
    const sourceFact = Object.freeze(fact({ id: 'fact-sensitive', description }))
    const sourceEvidence = Object.freeze(
      evidence({ fact_id: sourceFact.id, artifact_id: 'artifact-sensitive', pointer }),
    )
    const binding = Object.freeze({
      status: 'success' as const,
      fact: sourceFact,
      evidence: Object.freeze([sourceEvidence]),
    })
    const results = Object.freeze([binding])

    const [row] = buildAuditEvidenceRows('project-a', results)

    expect(row?.kind).toBe('evidence')
    if (row?.kind !== 'evidence') throw new Error('expected evidence row')
    expect(row.fact).toBe(sourceFact)
    expect(row.fact.description).toBe(description)
    expect(row.evidence).toBe(sourceEvidence)
    expect(row.evidence.pointer).toBe(pointer)
    expect(results[0]?.evidence).toEqual([sourceEvidence])
  })

  it('uses project, Fact, kind, artifact, creation time, and pointer in stable collision-safe keys', () => {
    const sourceFact = fact({ id: 'fact-key' })
    const baseEvidence = evidence({
      fact_id: sourceFact.id,
      artifact_id: 'artifact-key',
      created_at: '2026-08-21T01:00:00Z',
      pointer: 'line:1',
    })
    const differentPointer = evidence({ ...baseEvidence, pointer: 'line:2' })
    const differentTime = evidence({ ...baseEvidence, created_at: '2026-08-21T02:00:00Z' })
    const success: AuditEvidenceLoadResult = {
      status: 'success',
      fact: sourceFact,
      evidence: [baseEvidence, differentPointer, differentTime],
    }

    const first = buildAuditEvidenceRows('project:a', [success])
    const second = buildAuditEvidenceRows('project:a', [success])
    const otherProject = buildAuditEvidenceRows('project:b', [success])
    const none = buildAuditEvidenceRows('project:a', [
      { status: 'success', fact: sourceFact, evidence: [] },
    ])
    const error = buildAuditEvidenceRows('project:a', [
      { status: 'error', fact: sourceFact, error: 'failed' },
    ])

    expect(first.map(({ key }) => key)).toEqual(second.map(({ key }) => key))
    expect(new Set(first.map(({ key }) => key).values()).size).toBe(3)
    expect(otherProject[0]?.key).not.toBe(first[0]?.key)
    expect(none[0]?.key).not.toBe(error[0]?.key)
  })
})
