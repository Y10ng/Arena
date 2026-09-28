import { describe, expect, it } from 'vitest'
import {
  buildTerminalOutputs,
  isConcludeRecovery,
  runDurationSeconds,
  runDurationText,
  runQueuedAt,
  runReferenceTime,
  selectDefaultTerminalOutput,
  selectRetainedTerminalOutput,
  type RunArtifactLike,
} from './runTerminalModel'
import type { TaskRun } from './runTypes'

const artifact = (
  id: string,
  phase: string,
  stream: 'stdout' | 'stderr' | 'diagnostic' = 'stdout',
  metadata: Record<string, unknown> = {},
): RunArtifactLike => ({
  id,
  kind: 'raw_output',
  uri: `artifact://runs/${id}-${phase}-${stream}.log`,
  size: 128,
  metadata: { phase, stream, ...metadata },
})

const run = (overrides: Partial<TaskRun> = {}): TaskRun => ({
  id: 'run-a',
  project_id: 'project-a',
  intent_id: null,
  task_type: 'explore',
  worker_name: 'claude-code',
  worker_type: 'claude_code',
  status: 'succeeded',
  session_id: null,
  prompt_group: null,
  prompt_version: null,
  execution_profile: null,
  started_at: '2026-08-20T10:00:00.000Z',
  ended_at: '2026-08-20T10:01:30.000Z',
  exit_code: 0,
  error_type: null,
  error_message: null,
  assessment_attempt_id: null,
  runtime_context: {},
  ...overrides,
})

describe('run terminal output selection', () => {
  it('classifies stdout, stderr, and diagnostic outputs', () => {
    const outputs = buildTerminalOutputs([
      artifact('stdout', 'explore_execute'),
      artifact('stderr', 'explore_execute', 'stderr'),
      artifact('diagnostic', 'explore_execute', 'diagnostic'),
    ])

    expect(outputs.map((output) => [output.id, output.stream])).toEqual([
      ['stdout', 'stdout'],
      ['stderr', 'stderr'],
      ['diagnostic', 'diagnostic'],
    ])
    expect(outputs.map((output) => output.label)).toEqual([
      'Explore 执行 · 标准输出',
      'Explore 执行 · 标准错误',
      'Explore 执行 · 诊断',
    ])
  })

  it('defaults to successful stdout, then stdout, diagnostic, and stderr', () => {
    const successful = buildTerminalOutputs([
      artifact('stderr', 'explore_execute', 'stderr'),
      artifact('diagnostic', 'explore_execute', 'diagnostic'),
      artifact('failed-stdout', 'explore_execute', 'stdout', { returncode: 1 }),
      artifact('successful-stdout', 'explore_conclude', 'stdout', { returncode: 0 }),
    ])
    expect(selectDefaultTerminalOutput(successful)?.id).toBe('successful-stdout')

    const diagnosticOnly = buildTerminalOutputs([
      artifact('stderr', 'explore_execute', 'stderr'),
      artifact('diagnostic', 'explore_execute', 'diagnostic'),
    ])
    expect(selectDefaultTerminalOutput(diagnosticOnly)?.id).toBe('diagnostic')

    const stderrOnly = buildTerminalOutputs([artifact('stderr', 'explore_execute', 'stderr')])
    expect(selectDefaultTerminalOutput(stderrOnly)?.id).toBe('stderr')
  })

  it('retains a manual selection until that artifact disappears', () => {
    const outputs = buildTerminalOutputs([
      artifact('execute', 'explore_execute', 'stdout', { returncode: 0 }),
      artifact('conclude', 'explore_conclude', 'stdout', { returncode: 0 }),
    ])

    expect(selectRetainedTerminalOutput(outputs, 'execute')?.id).toBe('execute')
    expect(selectRetainedTerminalOutput(outputs.slice(1), 'execute')?.id).toBe('conclude')
  })

  it('detects conclude recovery only from stdout output', () => {
    const recovered = buildTerminalOutputs([
      artifact('execute', 'explore_execute', 'stdout', { returncode: 124, timed_out: true }),
      artifact('conclude', 'explore_conclude', 'stdout', { returncode: 0 }),
    ])
    expect(isConcludeRecovery(recovered)).toBe(true)

    const stderrConclude = buildTerminalOutputs([
      artifact('execute', 'explore_execute', 'stdout', { returncode: 124, timed_out: true }),
      artifact('conclude-error', 'explore_conclude', 'stderr', { returncode: 0 }),
    ])
    expect(isConcludeRecovery(stderrConclude)).toBe(false)
  })

  it('reads truncation metadata safely and supports legacy URI inference', () => {
    const outputs = buildTerminalOutputs([
      {
        id: 'legacy-stderr',
        kind: 'raw_output',
        uri: 'artifact://runs/explore-stderr.log',
        size: 64,
        metadata: {
          phase: 'explore_execute',
          truncated: true,
          original_size: 4096,
        },
      },
      {
        id: 'malformed',
        kind: 'raw_output',
        uri: 'artifact://runs/report-diagnostic.log',
        size: -1,
        metadata: {
          original_size: '4096',
          truncated: 'yes',
        },
      },
    ])

    expect(outputs[0]).toMatchObject({
      stream: 'stderr',
      captureTruncated: true,
      originalSize: 4096,
      storedSize: 64,
    })
    expect(outputs[1]).toMatchObject({
      stream: 'diagnostic',
      captureTruncated: false,
      originalSize: null,
      storedSize: null,
    })
  })

  it('does not infer terminal streams from unrelated artifact filenames', () => {
    const outputs = buildTerminalOutputs([
      {
        id: 'screenshot',
        kind: 'screenshot',
        uri: 'artifact://evidence/diagnostic.png',
      },
      {
        id: 'binary-file',
        kind: 'file',
        uri: 'artifact://evidence/stdout.bin',
      },
    ])

    expect(outputs).toEqual([])
  })
})

describe('run timing', () => {
  it('derives completed duration from started and ended timestamps', () => {
    const completed = run()
    expect(runDurationSeconds(completed)).toBe(90)
    expect(runDurationText(completed)).toBe('1分30秒')
    expect(runReferenceTime(completed)).toBe(completed.started_at)
  })

  it('supports zero-second completion and clamps reversed timestamps', () => {
    expect(
      runDurationText(
        run({
          started_at: '2026-08-20T10:00:00.000Z',
          ended_at: '2026-08-20T10:00:00.000Z',
        }),
      ),
    ).toBe('0秒')
    expect(
      runDurationSeconds(
        run({
          started_at: '2026-08-20T10:01:00.000Z',
          ended_at: '2026-08-20T10:00:00.000Z',
        }),
      ),
    ).toBe(0)
  })

  it('uses queued runtime context and never labels terminal runs as executing', () => {
    const queued = run({
      status: 'queued',
      started_at: null,
      ended_at: null,
      runtime_context: { queued_at: '2026-08-20T09:59:00.000Z' },
    })
    expect(runQueuedAt(queued)).toBe('2026-08-20T09:59:00.000Z')
    expect(runReferenceTime(queued)).toBe('2026-08-20T09:59:00.000Z')
    expect(runDurationText(queued)).toBe('排队中')

    expect(runDurationText(run({ status: 'running', ended_at: null }))).toBe('执行中')
    expect(runDurationText(run({ status: 'failed', ended_at: null }))).toBe('已结束')
    expect(
      runDurationText(run({ status: 'timed_out', started_at: 'invalid', ended_at: null })),
    ).toBe('已结束')
  })
})
