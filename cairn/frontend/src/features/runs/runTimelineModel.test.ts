import { describe, expect, it } from 'vitest'
import type { Fact, Intent, ReportInventoryItem } from '@/api/types'
import type { TaskRun, TaskRunHookEvent } from './runTypes'
import { buildWorkTimeline, pairToolCalls, resultsForRun } from './runTimelineModel'

const run = (id: string, overrides: Partial<TaskRun> = {}): TaskRun =>
  ({
    id,
    project_id: 'p',
    intent_id: 'i',
    task_type: 'explore',
    status: 'running',
    started_at: '2026-01-01T00:00:00Z',
    ended_at: null,
    runtime_context: {},
    ...overrides,
  }) as TaskRun
const event = (
  id: string,
  operation: string | null,
  overrides: Partial<TaskRunHookEvent> = {},
): TaskRunHookEvent =>
  ({
    id,
    task_run_id: 'r',
    project_id: 'p',
    phase: 'execute',
    session_id: 's',
    operation_id: operation,
    hook_event_name: 'tool_execution_start',
    tool_name: 'read',
    status: 'started',
    recorded_at: '2026-01-01T00:00:01Z',
    ts: '2026-01-02T00:00:00Z',
    artifact_id: null,
    ...overrides,
  }) as TaskRunHookEvent

describe('工作进展的来源与调用身份', () => {
  it('保留同工作失败尝试与筛选，只给接受结果的生产 Run 归属；无 Intent 不猜合并', () => {
    const failed = run('old', { status: 'failed', error_message: 'private failure' })
    const accepted = run('new', { status: 'succeeded', started_at: '2026-01-03T00:00:00Z' })
    const intent = { id: 'i', description: '同一工作', to: 'f', conclusion: 'completed' } as Intent
    const fact = { id: 'f', description: '完整已接受结果', task_run_id: 'new' } as Fact
    const all = [
      failed,
      accepted,
      run('reason-a', { intent_id: null }),
      run('reason-b', { intent_id: null }),
    ]
    const work = buildWorkTimeline(all, [intent], [fact], new Set(['old']))[0]!
    expect(work.runs.map((item) => item.id)).toEqual(['new', 'old'])
    expect(work.matchedRuns.map((item) => item.id)).toEqual(['old'])
    expect(work.startedAt).toBe(failed.started_at)
    expect(work.status).toBe('failed')
    expect(work.results).toEqual([
      { key: 'fact:f', runId: 'new', label: '已接受事实', text: fact.description },
    ])
    expect(resultsForRun(failed, [fact], [intent])).toEqual([])
    expect(failed.status).toBe('failed')
    expect(
      buildWorkTimeline(all, [intent], [fact], new Set(all.map((item) => item.id))),
    ).toHaveLength(3)
    expect(buildWorkTimeline([accepted], [], [], new Set(['new']))[0]!.status).toBe('succeeded')
    expect(
      buildWorkTimeline(
        [accepted],
        [{ ...intent, conclusion: 'failed' }],
        [fact],
        new Set(['new']),
      )[0]!.status,
    ).toBe('succeeded')
  })

  it('以 Run、phase、session、operation 配对并行同名工具，终态不保留虚假执行中', () => {
    const events = [
      event('1', 'a'),
      event('2', 'b'),
      event('duplicate', 'a'),
      event('3', 'a', {
        hook_event_name: 'tool_execution_end',
        status: 'succeeded',
        recorded_at: '2026-01-01T00:00:03Z',
      }),
      event('4', 'a', { phase: 'conclude' }),
      event('5', 'a', { session_id: 'other' }),
      event('6', null, { recorded_at: null }),
      event('7', null),
      event('unrelated', 'c', { task_run_id: 'other' }),
      event('source', null, { hook_event_name: 'observation_saved', artifact_id: 'raw' }),
    ]
    const calls = pairToolCalls(run('r'), events)
    expect(calls).toHaveLength(6)
    expect(calls.filter((call) => call.active)).toHaveLength(5)
    expect(calls.find((call) => call.endedAt)).toMatchObject({
      durationMs: 2000,
      status: '已完成',
      artifactId: 'raw',
    })
    expect(
      calls.find((call) => call.operationId === null && call.startedAt === null)?.startedAt,
    ).toBeNull()
    const terminal = pairToolCalls(run('r', { status: 'failed' }), events)
    expect(terminal.some((call) => call.active)).toBe(false)
    expect(terminal.filter((call) => call.status === '结束状态未记录')).toHaveLength(5)
  })

  it('分别投影开始输入和结束响应，保留权威字段且不制造 pi 空正文', () => {
    const events = [
      event('start', 'a', {
        command: 'npm test',
        file_path: '/tmp/input',
        query: '查询原文',
        url: 'https://example.invalid',
        tool_input: { command: 'npm test', content: '完整输入' },
        tool_response: { ignored: '开始事件不是响应来源' },
      }),
      event('end', 'a', {
        hook_event_name: 'tool_execution_end',
        status: 'succeeded',
        recorded_at: '2026-01-01T00:00:03Z',
        duration_ms: 1250,
        exit_code: 0,
        summary: '完成摘要',
        tool_input: { ignored: '开始输入优先' },
        tool_response: { output: '完整响应' },
      }),
    ]
    const [call] = pairToolCalls(run('r', { status: 'succeeded' }), events)
    expect(call).toMatchObject({
      command: 'npm test',
      filePath: '/tmp/input',
      query: '查询原文',
      url: 'https://example.invalid',
      summary: '完成摘要',
      exitCode: 0,
      durationMs: 1250,
      toolInput: { command: 'npm test', content: '完整输入' },
      toolResponse: { output: '完整响应' },
    })
    expect(call!.toolInput).not.toHaveProperty('ignored')
    expect(call!.toolInput).not.toHaveProperty('output')
    expect(call!.toolResponse).not.toHaveProperty('command')

    const piEvents = events.map((item) => ({
      ...item,
      tool_input: {},
      tool_response: {},
    }))
    const [piCall] = pairToolCalls(run('r', { status: 'succeeded' }), piEvents)
    expect(piCall?.toolInput).toBeNull()
    expect(piCall?.toolResponse).toBeNull()
  })

  it('无 Intent 的正式 Fact、创建工作和已接受报告保留各自来源', () => {
    const source = run('reason', { intent_id: null, status: 'succeeded' })
    const results = resultsForRun(
      source,
      [{ id: 'depth', task_run_id: 'reason', description: '正式事实' } as Fact],
      [{ id: 'created', task_run_id: 'reason', to: 'goal', description: '完成决策原文' } as Intent],
      [{ artifact_id: 'report', task_run_id: 'reason' } as ReportInventoryItem],
    )
    expect(results.map((result) => result.label)).toEqual([
      '已接受事实',
      '已接受完成决策',
      '已接受报告',
    ])
    expect(results.every((result) => result.runId === 'reason')).toBe(true)
  })
})
