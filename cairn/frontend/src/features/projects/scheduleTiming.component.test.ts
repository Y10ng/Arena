import { describe, expect, it } from 'vitest'
import {
  localTimeCandidates,
  newScheduleDraft,
  scheduleTimesFromDraft,
  type ScheduleDraft,
} from './scheduleTiming'

function onceDraft(overrides: Partial<ScheduleDraft> = {}): ScheduleDraft {
  return {
    ...newScheduleDraft(),
    mode: 'once',
    timezone: 'America/New_York',
    startLocal: '2035-11-04T00:30',
    stopMode: 'specified',
    stopLocal: '2035-11-04T01:30',
    ...overrides,
  }
}

describe('single schedule time boundaries', () => {
  it('requires an explicit stop offset for a DST fold and rejects a DST gap', () => {
    const draft = onceDraft()
    const candidates = localTimeCandidates(draft.stopLocal, draft.timezone)
    expect(candidates.map((candidate) => candidate.value)).toEqual([
      '2035-11-04T01:30:00-04:00',
      '2035-11-04T01:30:00-05:00',
    ])
    expect(() => scheduleTimesFromDraft(draft)).toThrow('停止时间出现两次')
    for (const { value } of candidates)
      expect(scheduleTimesFromDraft({ ...draft, stopAt: value }).deadline).toBe(value)
    expect(() => scheduleTimesFromDraft(onceDraft({
      startLocal: '2035-03-11T00:30', stopLocal: '2035-03-11T02:30',
    }))).toThrow('停止时间无效，或该本地时刻因夏令时不存在')
  })

  it('preserves untouched seconds, precision and offsets and compares absolute instants', () => {
    const draft = onceDraft({
      startLocal: '2035-11-04T01:30',
      startAt: '2035-11-04T01:30:23.123-04:00',
      stopAt: '2035-11-04T01:30:37.456-05:00',
    })
    expect(scheduleTimesFromDraft(draft)).toMatchObject({
      rules: { start_at: draft.startAt }, deadline: draft.stopAt,
    })
    expect(() => scheduleTimesFromDraft({
      ...draft, startAt: '2035-11-04T01:30:38.000-05:00',
    })).toThrow('停止时间必须严格晚于启动时间')
  })

  it('does not validate or serialize hidden stop and recurrence drafts', () => {
    const draft = onceDraft({
      stopMode: 'unlimited', stopLocal: '', endDate: '2000-01-01', maxRuns: 'invalid',
    })
    expect(scheduleTimesFromDraft(draft)).toMatchObject({
      rules: { end_date: null, max_runs: null, weekdays: [], local_time: null }, deadline: null,
    })
    for (const mode of ['daily', 'weekly'] as const)
      expect(scheduleTimesFromDraft({
        ...draft, mode, endDate: '', maxRuns: '', stopMode: 'specified',
      }).deadline).toBeNull()
  })
})
