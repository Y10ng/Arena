import { describe, it, assert } from 'vitest'
import { buildGraphPlaybackEvents, convertToGraph } from './useGraphModel.js'
import type { ProjectDetail } from './useGraphModel.js'

const projectDetail: ProjectDetail = {
  project: {
    id: 'proj_001',
    title: '图谱回放测试',
    status: 'active',
    created_at: '2026-06-03T00:00:00Z',
  },
  facts: [
    { id: 'origin', description: '起点', creator: 'system', created_at: '2026-06-03T00:00:00Z' },
    { id: 'goal', description: '目标', creator: 'system', created_at: '2026-06-03T00:00:00Z' },
    {
      id: 'f1',
      description: '确认登录接口存在。响应里包含标准表单入口和会话字段。',
      task_run_id: 'tr_explore',
      creator: 'codex',
      created_at: '2026-06-03T00:03:00Z',
    },
  ],
  intents: [
    {
      id: 'i1',
      from: ['origin'],
      to: 'f1',
      description: '枚举登录接口',
      creator: 'dispatcher.reason',
      task_run_id: 'tr_reason',
      worker: null,
      worker_history: [
        { worker: 'codex', reason: 'completed', cleared_at: '2026-06-03T00:03:10Z' },
      ],
      created_at: '2026-06-03T00:01:00Z',
      concluded_at: '2026-06-03T00:03:00Z',
    },
    {
      id: 'i2',
      from: ['f1'],
      to: null,
      description: '验证表单行为',
      creator: 'dispatcher.reason',
      task_run_id: 'tr_followup',
      worker: 'pi',
      worker_history: [],
      last_heartbeat_at: '2026-06-03T00:04:40Z',
      created_at: '2026-06-03T00:04:00Z',
      concluded_at: null,
    },
  ],
}

const taskRuns = [
  {
    id: 'tr_explore',
    intent_id: 'i1',
    task_type: 'explore',
    worker_name: 'codex',
    worker_type: 'codex',
    status: 'succeeded',
    started_at: '2026-06-03T00:02:00Z',
  },
  {
    id: 'tr_followup',
    intent_id: 'i2',
    task_type: 'explore',
    worker_name: 'pi',
    worker_type: 'pi',
    status: 'running',
    started_at: '2026-06-03T00:04:10Z',
  },
]

const factEvidenceById = {
  f1: {
    loading: false,
    items: [
      { artifact_id: 'a1', pointer: 'stdout:1', created_at: '2026-06-03T00:03:10Z' },
      { artifact_id: 'a2', pointer: 'report#login', created_at: '2026-06-03T00:03:20Z' },
    ],
  },
}

describe('useGraphModel', () => {
  it('convertToGraph builds the complete fact/intent graph with rich node metadata', () => {
    const graph = convertToGraph(projectDetail, {
      taskRuns,
      factEvidenceById,
      nowTime: '2026-06-03T00:05:00Z',
    })

    assert.deepEqual(graph.nodes.map((node) => node.id).sort(), [
      'f1',
      'goal',
      'origin',
      'pending:i2',
    ])

    const factNode = graph.nodes.find((node) => node.id === 'f1')!
    assert.equal(graph.nodes.find((node) => node.id === 'origin')!.layoutKey, 'node:origin')
    assert.equal(factNode.layoutKey, 'intent:i1')
    assert.equal(factNode.data?.conclusion, '确认登录接口存在。')
    assert.equal(factNode.data?.summary, '响应里包含标准表单入口和会话字段。')
    assert.ok(
      factNode.data?.metaTags.some((tag: any) => tag.label === '执行器' && tag.value === 'codex'),
    )
    assert.ok(
      factNode.data?.metaTags.some((tag: any) => tag.label === 'Evidence' && tag.value === '2'),
    )
    assert.equal(factNode.data?.worker, 'codex')
    assert.equal(factNode.data?.taskRunId, 'tr_explore')
    assert.equal(factNode.data?.runStatus, 'succeeded')
    assert.equal(factNode.data?.evidenceCount, 2)

    const concludedEdge = graph.edges.find((edge) => edge.data?.intent.id === 'i1')!
    assert.equal(concludedEdge.target, 'f1')
    assert.equal(concludedEdge.data?.state, 'concluded')
    assert.equal(concludedEdge.data?.edgeTier, 'trunk')
    assert.equal(concludedEdge.data?.hasCriticalPath, false)

    const pendingNode = graph.nodes.find((node) => node.id === 'pending:i2')!
    assert.equal(pendingNode.layoutKey, 'intent:i2')
    assert.equal(pendingNode.data?.worker, 'pi')
    assert.equal(pendingNode.data?.runStatus, 'running')
    assert.equal(pendingNode.data?.heartbeatAt, '2026-06-03T00:04:40Z')
    assert.equal(pendingNode.data?.heartbeatAgeSeconds, 20)
    assert.equal(pendingNode.data?.elapsedTaskSeconds, 50)
    assert.equal(pendingNode.data?.liveState, 'heartbeat')
    assert.ok(
      pendingNode.data?.metaTags.some((tag: any) => tag.label === '心跳' && tag.value === '运行中'),
    )

    const pendingEdge = graph.edges.find((edge) => edge.data?.intent.id === 'i2')!
    assert.equal(pendingEdge.data?.edgeTier, 'exploratory')
  })

  it('convertToGraph shows a pending node before an intent conclusion exists at the cutoff', () => {
    const graph = convertToGraph(projectDetail, {
      taskRuns,
      factEvidenceById,
      cutoffTime: '2026-06-03T00:02:00Z',
    })

    assert.ok(!graph.nodes.some((node) => node.id === 'f1'))
    assert.equal(graph.nodes.find((node) => node.id === 'pending:i1')!.layoutKey, 'intent:i1')

    const edge = graph.edges.find((item) => item.data?.intent.id === 'i1')!
    assert.equal(edge.source, 'origin')
    assert.equal(edge.target, 'pending:i1')
  })

  it('convertToGraph switches to the concluded fact edge once the cutoff reaches conclusion time', () => {
    const graph = convertToGraph(projectDetail, {
      taskRuns,
      factEvidenceById,
      cutoffTime: '2026-06-03T00:03:00Z',
    })

    assert.equal(graph.nodes.find((node) => node.id === 'f1')!.layoutKey, 'intent:i1')
    assert.ok(!graph.nodes.some((node) => node.id === 'pending:i1'))

    const edge = graph.edges.find((item) => item.data?.intent.id === 'i1')!
    assert.equal(edge.target, 'f1')
    assert.equal(edge.data?.state, 'concluded')
  })

  it('convertToGraph adds Reason audit nodes without changing causal topology', () => {
    const reasonRun = {
      id: 'tr_reason',
      task_type: 'reason',
      worker_name: 'reason-worker',
      status: 'succeeded',
      started_at: '2026-06-03T00:00:30Z',
      ended_at: '2026-06-03T00:01:00Z',
    }
    const graph = convertToGraph(projectDetail, {
      taskRuns: [...taskRuns, reasonRun],
      resultEventsByRun: {
        tr_reason: [
          {
            kind: 'decision_result',
            payload: {
              outcome: 'intents',
              rationale: '登录入口是当前最稳定的下一步。',
              confidence: 0.82,
              created_intent_ids: ['i1'],
              from_ids: ['origin'],
            },
          },
        ],
      },
    })

    const reasonNode = graph.nodes.find((node) => node.id === 'reason:tr_reason')!
    assert.equal(reasonNode.layoutKey, 'reason:tr_reason')
    assert.equal(reasonNode.data?.kind, 'reason')
    assert.equal(reasonNode.data?.reason?.outcome, 'intents')
    assert.equal(reasonNode.data?.reason?.confidence, 0.82)
    assert.deepEqual(reasonNode.data?.reason?.inputNodeIds, ['origin'])
    assert.deepEqual(reasonNode.data?.reason?.outputNodeIds, ['f1'])
    assert.deepEqual(reasonNode.data?.reason?.outputDescriptors, [
      { nodeId: 'f1', layoutKey: 'intent:i1', intentId: 'i1' },
    ])

    const directEdge = graph.edges.find(
      (edge) => edge.type === 'intent' && edge.data?.intentId === 'i1',
    )!
    assert.equal(directEdge.data?.reasonRunId, 'tr_reason')
    assert.equal(directEdge.data?.reasonNodeId, 'reason:tr_reason')
    assert.deepEqual(directEdge.data?.reasonRunIds, ['tr_reason'])

    const auditEdges = graph.edges.filter((edge) => edge.type === 'reason-audit')
    assert.deepEqual(
      auditEdges.map((edge) => [edge.data?.auditDirection, edge.source, edge.target]),
      [
        ['input', 'origin', 'reason:tr_reason'],
        ['output', 'reason:tr_reason', 'f1'],
      ],
    )
    assert.equal(
      auditEdges.every((edge) => edge.data?.reasonRunId === 'tr_reason'),
      true,
    )
    assert.equal(
      auditEdges.every((edge) => edge.data?.reasonNodeId === 'reason:tr_reason'),
      true,
    )
    assert.equal(
      auditEdges.every((edge) => edge.data?.intentId === 'i1'),
      true,
    )
    assert.equal(
      auditEdges.find((edge) => edge.data?.auditDirection === 'output')?.data?.outputOrder,
      0,
    )
    assert.equal(graph.nodes.find((node) => node.id === 'origin')!.data?.outDegree, 1)
    assert.equal(graph.nodes.find((node) => node.id === 'f1')!.data?.inDegree, 1)
    assert.equal(
      auditEdges.every((edge) => edge.data?.edgeTier === 'audit'),
      true,
    )
  })

  it('keeps the Reason output linked to the Pending node during playback', () => {
    const graph = convertToGraph(projectDetail, {
      taskRuns: [
        ...taskRuns,
        {
          id: 'tr_reason',
          task_type: 'reason',
          worker_name: 'reason-worker',
          status: 'succeeded',
          started_at: '2026-06-03T00:00:30Z',
          ended_at: '2026-06-03T00:01:00Z',
        },
      ],
      resultEventsByRun: {
        tr_reason: [
          {
            kind: 'decision_result',
            payload: {
              outcome: 'intents',
              created_intent_ids: ['i1'],
              from_ids: ['origin'],
            },
          },
        ],
      },
      cutoffTime: '2026-06-03T00:02:00Z',
    })

    const outputEdge = graph.edges.find(
      (edge) => edge.type === 'reason-audit' && edge.data?.auditDirection === 'output',
    )
    assert.equal(outputEdge?.source, 'reason:tr_reason')
    assert.equal(outputEdge?.target, 'pending:i1')
    assert.deepEqual(
      graph.nodes.find((node) => node.id === 'reason:tr_reason')?.data?.reason?.outputDescriptors,
      [{ nodeId: 'pending:i1', layoutKey: 'intent:i1', intentId: 'i1' }],
    )
  })

  it('convertToGraph keeps a Reason run when decision audit events are unavailable', () => {
    const graph = convertToGraph(projectDetail, {
      taskRuns: [
        ...taskRuns,
        {
          id: 'tr_reason',
          task_type: 'reason',
          worker_name: 'reason-worker',
          status: 'failed',
          started_at: '2026-06-03T00:00:30Z',
          error_type: 'worker_error',
          error_message: 'Reason 执行失败',
        },
      ],
    })

    const reasonNode = graph.nodes.find((node) => node.id === 'reason:tr_reason')!
    assert.equal(reasonNode.data?.reason?.auditAvailable, false)
    assert.equal(reasonNode.data?.reason?.errorType, 'worker_error')
    assert.deepEqual(reasonNode.data?.reason?.inputNodeIds, ['origin'])
    assert.deepEqual(reasonNode.data?.reason?.outputNodeIds, ['f1'])
  })

  it('buildGraphPlaybackEvents returns discrete graph growth events in time order', () => {
    const events = buildGraphPlaybackEvents(projectDetail)

    assert.deepEqual(
      events.map((event) => event.id),
      ['intent-created:i1', 'fact:f1', 'intent-concluded:i1', 'intent-created:i2'],
    )
    assert.equal(events[0].label, '提出 Intent i1')
    assert.equal(events[2].label, '完成 Intent i1 → f1')
  })

  it('convertToGraph marks the completed goal chain as critical path', () => {
    const completedProject: ProjectDetail = {
      ...projectDetail,
      project: { ...projectDetail.project!, status: 'completed' },
      intents: [
        ...(projectDetail.intents || []),
        {
          id: 'i3',
          from: ['f1'],
          to: 'goal',
          description: '确认目标完成',
          creator: 'dispatcher.reason',
          task_run_id: 'tr_reason_goal',
          worker: null,
          worker_history: [],
          created_at: '2026-06-03T00:05:00Z',
          concluded_at: '2026-06-03T00:05:30Z',
        },
      ],
    }

    const graph = convertToGraph(completedProject, { taskRuns, factEvidenceById })
    const criticalNodes = graph.nodes
      .filter((node) => node.data?.isCriticalPath)
      .map((node) => node.id)
      .sort()
    const criticalIntentIds = graph.edges
      .filter((edge) => edge.data?.isCriticalPath)
      .map((edge) => edge.data?.intent.id)
      .sort()

    assert.deepEqual(criticalNodes, ['f1', 'goal', 'origin'])
    assert.deepEqual(criticalIntentIds, ['i1', 'i3'])
    assert.equal(
      graph.nodes.find((node) => node.id === 'pending:i2')!.data?.isCriticalPath,
      undefined,
    )

    const criticalEdge = graph.edges.find((edge) => edge.data?.intent.id === 'i3')!
    const nonCriticalEdge = graph.edges.find((edge) => edge.data?.intent.id === 'i2')!
    assert.equal(criticalEdge.data?.edgeTier, 'critical')
    assert.equal(criticalEdge.data?.hasCriticalPath, true)
    assert.equal(nonCriticalEdge.data?.edgeTier, 'exploratory')
    assert.equal(nonCriticalEdge.data?.hasCriticalPath, true)
  })

  it('convertToGraph marks stale pending heartbeat without inventing backend state', () => {
    const graph = convertToGraph(projectDetail, {
      taskRuns,
      factEvidenceById,
      nowTime: '2026-06-03T00:07:00Z',
    })

    const pendingNode = graph.nodes.find((node) => node.id === 'pending:i2')!
    assert.equal(pendingNode.data?.heartbeatAgeSeconds, 140)
    assert.equal(pendingNode.data?.liveState, 'stale')
  })

  it('convertToGraph does not mark concluded heartbeat as stale', () => {
    const concludedProject: ProjectDetail = {
      ...projectDetail,
      intents: (projectDetail.intents || []).map((intent) =>
        intent.id === 'i1'
          ? {
              ...intent,
              worker: 'codex',
              last_heartbeat_at: '2026-06-03T00:03:00Z',
              concluded_at: '2026-06-03T00:03:00Z',
            }
          : intent,
      ),
    }
    const graph = convertToGraph(concludedProject, {
      taskRuns,
      factEvidenceById,
      nowTime: '2026-06-03T00:07:00Z',
    })

    const factNode = graph.nodes.find((node) => node.id === 'f1')!
    assert.equal(factNode.data?.heartbeatAgeSeconds, 240)
    assert.equal(factNode.data?.liveState, '')
  })

  it('convertToGraph annotates branch points and multi-source join intent edges', () => {
    const branchingProject: ProjectDetail = {
      project: {
        id: 'proj_branching',
        title: '分支汇聚测试',
        status: 'active',
        created_at: '2026-06-03T00:00:00Z',
      },
      facts: [
        {
          id: 'origin',
          description: '起点',
          creator: 'system',
          created_at: '2026-06-03T00:00:00Z',
        },
        { id: 'f1', description: '入口事实', creator: 'codex', created_at: '2026-06-03T00:01:00Z' },
        { id: 'f2', description: '并行事实', creator: 'codex', created_at: '2026-06-03T00:02:00Z' },
        { id: 'f3', description: '汇聚事实', creator: 'codex', created_at: '2026-06-03T00:03:00Z' },
        { id: 'goal', description: '目标', creator: 'system', created_at: '2026-06-03T00:00:00Z' },
      ],
      intents: [
        {
          id: 'i1',
          from: ['origin'],
          to: 'f1',
          description: '建立入口事实',
          creator: 'dispatcher.reason',
          created_at: '2026-06-03T00:00:30Z',
          concluded_at: '2026-06-03T00:01:00Z',
        },
        {
          id: 'i2',
          from: ['f1'],
          to: 'f2',
          description: '展开并行方向',
          creator: 'dispatcher.reason',
          created_at: '2026-06-03T00:01:30Z',
          concluded_at: '2026-06-03T00:02:00Z',
        },
        {
          id: 'i3',
          from: ['f1', 'f2'],
          to: 'f3',
          description: '合并两个来源',
          creator: 'dispatcher.reason',
          created_at: '2026-06-03T00:02:30Z',
          concluded_at: '2026-06-03T00:03:00Z',
        },
      ],
    }

    const graph = convertToGraph(branchingProject)
    const branchNode = graph.nodes.find((node) => node.id === 'f1')!
    const joinNode = graph.nodes.find((node) => node.id === 'f3')!
    const joinEdges = graph.edges.filter((edge) => edge.data?.intent.id === 'i3')

    assert.equal(branchNode.data?.outDegree, 2)
    assert.equal(branchNode.data?.isBranchPoint, true)
    assert.equal(branchNode.data?.inDegree, 1)
    assert.equal(joinNode.data?.inDegree, 2)
    assert.equal(joinNode.data?.isJoinPoint, true)
    assert.equal(joinNode.data?.joinGroups.length, 1)
    assert.equal(joinNode.data?.joinGroups[0].id, 'join:i3')
    assert.equal(joinNode.data?.joinGroups[0].sourceCount, 2)

    assert.equal(joinEdges.length, 2)
    const firstJoinEdge = joinEdges[0]!
    const secondJoinEdge = joinEdges[1]!
    assert.equal(firstJoinEdge.data!.joinGroup!.id, 'join:i3')
    assert.equal(secondJoinEdge.data!.joinGroup!.id, 'join:i3')
    assert.equal(firstJoinEdge.data!.joinGroup!.color, secondJoinEdge.data!.joinGroup!.color)
    assert.equal(firstJoinEdge.data!.joinGroup!.markerEdgeId, firstJoinEdge.id)
    assert.equal(secondJoinEdge.data!.joinGroup!.markerEdgeId, firstJoinEdge.id)
  })
})
