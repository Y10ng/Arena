import { describe, expect, it } from 'vitest'
import { buildProvenanceTimeline } from './provenanceModel'
import type { TaskRunHookEvent } from '@/features/runs/runTypes'

const hook = (overrides: Partial<TaskRunHookEvent>): TaskRunHookEvent => ({
  id: 'event-1',
  task_run_id: 'run-1',
  project_id: 'project-1',
  ts: '2026-08-25T00:00:00Z',
  kind: 'hook_operation',
  session_id: 'session-1',
  hook_event_name: 'PreToolUse',
  tool_name: 'Bash',
  operation_id: 'operation-1',
  status: 'planned',
  recorded_at: '2026-08-25T00:00:00Z',
  phase: 'explore_execute',
  cwd: '/workspace/private',
  tool_input: { command: "curl -H 'Authorization: Bearer test-secret' https://target.test" },
  tool_response: {},
  command: "curl -H 'Authorization: Bearer test-secret' https://target.test",
  command_kind: 'curl',
  file_path: null,
  file_path_digest: null,
  query: null,
  url: null,
  exit_code: null,
  duration_ms: null,
  artifact_id: 'artifact-hook',
  risk_level: 'high',
  summary: 'explore_execute / PreToolUse / Bash',
  ...overrides,
})

describe('buildProvenanceTimeline', () => {
  it('merges matching Hook pre/post events and keeps unmasked operation details', () => {
    const timeline = buildProvenanceTimeline({
      hooks: [
        hook({}),
        hook({
          id: 'event-2',
          ts: '2026-08-25T00:00:02Z',
          recorded_at: '2026-08-25T00:00:02Z',
          hook_event_name: 'PostToolUse',
          status: 'succeeded',
          tool_response: { exit_code: 0 },
        }),
      ],
    })

    expect(timeline).toHaveLength(1)
    expect(timeline[0]).toMatchObject({ kind: 'hook', status: 'succeeded', incomplete: false })
    expect(timeline[0].summary).toContain('Bearer test-secret')
    expect(timeline[0].payload.tool_input).toEqual({
      command: "curl -H 'Authorization: Bearer test-secret' https://target.test",
    })
  })

  it('marks unmatched Hook operations incomplete and sorts all records oldest first', () => {
    const timeline = buildProvenanceTimeline({
      hooks: [hook({ ts: '2026-08-25T00:00:03Z', recorded_at: null })],
      results: [
        {
          id: 'result-1',
          task_run_id: 'run-1',
          project_id: 'project-1',
          ts: '2026-08-25T00:00:01Z',
          kind: 'validation_result',
          payload: { status: 'valid' },
        },
      ],
    })

    expect(timeline.map((item) => item.kind)).toEqual(['result', 'hook'])
    expect(timeline[1].incomplete).toBe(true)
  })
})
