import { describe, it, assert } from 'vitest'
import {
  GRAPH_STATUS_FILTERS,
  collapsePendingBranches,
  filterGraphElements,
  applyManualCollapse,
} from './graphFilters.js'
import type { GraphNode, GraphEdge } from './graphFilters.js'

const nodes: GraphNode[] = [
  { id: 'origin', data: { kind: 'origin', label: '起点' } },
  { id: 'f1', data: { kind: 'fact', label: '登录接口', worker: 'codex', taskRunId: 'tr1' } },
  { id: 'goal', data: { kind: 'goal', label: '目标', isCriticalPath: true } },
  {
    id: 'pending:i2',
    data: { kind: 'pending', status: 'exploring', label: '验证表单行为', worker: 'pi' },
  },
]

const edges: GraphEdge[] = [
  {
    id: 'edge-i1',
    source: 'origin',
    target: 'f1',
    data: {
      state: 'concluded',
      label: '枚举登录接口',
      intent: { id: 'i1', description: '枚举登录接口' },
    },
  },
  {
    id: 'edge-i2',
    source: 'f1',
    target: 'pending:i2',
    data: {
      state: 'exploring',
      label: '验证表单行为',
      intent: { id: 'i2', description: '验证表单行为' },
    },
  },
  {
    id: 'edge-i3',
    source: 'f1',
    target: 'goal',
    data: {
      state: 'concluded',
      label: '完成目标',
      isCriticalPath: true,
      intent: { id: 'i3', description: '完成目标' },
    },
  },
]

describe('graph filters', () => {
  it('GRAPH_STATUS_FILTERS derives visible labels from shared status text', () => {
    assert.deepEqual(GRAPH_STATUS_FILTERS, [
      { value: 'all', label: '全部' },
      { value: 'open', label: '未完成' },
      { value: 'running', label: '探索中' },
      { value: 'completed', label: '已结论' },
      { value: 'critical', label: '关键路径' },
    ])
  })

  it('filterGraphElements filters running graph branches', () => {
    const result = filterGraphElements(nodes, edges, { status: 'running' })
    assert.deepEqual(result.nodes.map((node) => node.id).sort(), ['f1', 'pending:i2'])
    assert.deepEqual(
      result.edges.map((edge) => edge.id),
      ['edge-i2'],
    )
  })

  it('filterGraphElements searches intent text and keeps connected endpoint nodes', () => {
    const result = filterGraphElements(nodes, edges, { query: '表单' })
    assert.deepEqual(result.nodes.map((node) => node.id).sort(), ['f1', 'pending:i2'])
    assert.deepEqual(
      result.edges.map((edge) => edge.id),
      ['edge-i2'],
    )
    assert.equal(result.matchCount, 2)
  })

  it('filterGraphElements can isolate critical path edges', () => {
    const result = filterGraphElements(nodes, edges, { status: 'critical' })
    assert.deepEqual(result.nodes.map((node) => node.id).sort(), ['f1', 'goal'])
    assert.deepEqual(
      result.edges.map((edge) => edge.id),
      ['edge-i3'],
    )
  })

  it('collapsePendingBranches folds same-source open pending branches', () => {
    const branchNodes: GraphNode[] = [
      { id: 'f1', type: 'fact', data: { kind: 'fact', label: '入口事实' } },
      {
        id: 'pending:i2',
        type: 'fact',
        data: { kind: 'pending', status: 'exploring', label: '验证表单行为' },
      },
      {
        id: 'pending:i3',
        type: 'fact',
        data: { kind: 'pending', status: 'unclaimed', label: '检查错误提示' },
      },
    ]
    const branchEdges: GraphEdge[] = [
      {
        id: 'edge-i2',
        source: 'f1',
        target: 'pending:i2',
        type: 'intent',
        data: { state: 'exploring', intent: { id: 'i2', description: '验证表单行为' } },
      },
      {
        id: 'edge-i3',
        source: 'f1',
        target: 'pending:i3',
        type: 'intent',
        data: { state: 'unclaimed', intent: { id: 'i3', description: '检查错误提示' } },
      },
    ]

    const result = collapsePendingBranches(branchNodes, branchEdges, { enabled: true })
    assert.equal(result.collapsedCount, 2)
    assert.equal(result.groupCount, 1)
    assert.deepEqual(
      result.nodes.map((node) => node.id),
      ['f1', 'pending-group:f1'],
    )
    assert.deepEqual(
      result.edges.map((edge) => edge.id),
      ['edge-pending-group-f1'],
    )

    const groupNode = result.nodes.find((node) => node.id === 'pending-group:f1')!
    assert.equal(groupNode.data?.kind, 'pending_group')
    assert.equal(groupNode.data?.status, 'exploring')
    assert.equal(groupNode.data?.pendingGroup.count, 2)
    assert.equal(groupNode.data?.pendingGroup.runningCount, 1)
    assert.deepEqual(groupNode.data?.pendingGroup.intentIds, ['i2', 'i3'])

    const groupEdge = result.edges[0]
    assert.equal(groupEdge.source, 'f1')
    assert.equal(groupEdge.target, 'pending-group:f1')
    assert.equal(groupEdge.data?.state, 'exploring')
  })

  it('collapsePendingBranches leaves graph untouched when disabled', () => {
    const result = collapsePendingBranches(nodes, edges, { enabled: false })
    assert.strictEqual(result.nodes, nodes)
    assert.strictEqual(result.edges, edges)
    assert.equal(result.collapsedCount, 0)
    assert.equal(result.groupCount, 0)
  })

  it('collapsePendingBranches does not fold multi-source pending intents', () => {
    const multiSourceNodes: GraphNode[] = [
      { id: 'f1', type: 'fact', data: { kind: 'fact', label: '事实一' } },
      { id: 'f2', type: 'fact', data: { kind: 'fact', label: '事实二' } },
      {
        id: 'pending:i4',
        type: 'fact',
        data: { kind: 'pending', status: 'unclaimed', label: '需要双来源判断' },
      },
    ]
    const multiSourceEdges: GraphEdge[] = [
      {
        id: 'edge-i4-f1',
        source: 'f1',
        target: 'pending:i4',
        type: 'intent',
        data: { state: 'unclaimed', intent: { id: 'i4', description: '需要双来源判断' } },
      },
      {
        id: 'edge-i4-f2',
        source: 'f2',
        target: 'pending:i4',
        type: 'intent',
        data: { state: 'unclaimed', intent: { id: 'i4', description: '需要双来源判断' } },
      },
    ]

    const result = collapsePendingBranches(multiSourceNodes, multiSourceEdges, { enabled: true })
    assert.strictEqual(result.nodes, multiSourceNodes)
    assert.strictEqual(result.edges, multiSourceEdges)
    assert.equal(result.collapsedCount, 0)
    assert.equal(result.groupCount, 0)
  })

  it('filterGraphElements can search and status-filter folded pending groups', () => {
    const branchNodes: GraphNode[] = [
      { id: 'f1', type: 'fact', data: { kind: 'fact', label: '入口事实' } },
      {
        id: 'pending:i5',
        type: 'fact',
        data: { kind: 'pending', status: 'exploring', label: '追踪配置依赖' },
      },
      {
        id: 'pending:i6',
        type: 'fact',
        data: { kind: 'pending', status: 'unclaimed', label: '确认边界条件' },
      },
    ]
    const branchEdges: GraphEdge[] = [
      {
        id: 'edge-i5',
        source: 'f1',
        target: 'pending:i5',
        type: 'intent',
        data: { state: 'exploring', intent: { id: 'i5', description: '追踪配置依赖' } },
      },
      {
        id: 'edge-i6',
        source: 'f1',
        target: 'pending:i6',
        type: 'intent',
        data: { state: 'unclaimed', intent: { id: 'i6', description: '确认边界条件' } },
      },
    ]
    const collapsed = collapsePendingBranches(branchNodes, branchEdges, { enabled: true })

    const searchResult = filterGraphElements(collapsed.nodes, collapsed.edges, {
      query: '配置依赖',
    })
    assert.deepEqual(searchResult.nodes.map((node) => node.id).sort(), ['f1', 'pending-group:f1'])
    assert.deepEqual(
      searchResult.edges.map((edge) => edge.id),
      ['edge-pending-group-f1'],
    )
    assert.equal(searchResult.matchCount, 2)

    const runningResult = filterGraphElements(collapsed.nodes, collapsed.edges, {
      status: 'running',
    })
    assert.deepEqual(runningResult.nodes.map((node) => node.id).sort(), ['f1', 'pending-group:f1'])
    assert.deepEqual(
      runningResult.edges.map((edge) => edge.id),
      ['edge-pending-group-f1'],
    )
  })

  it('collapsePendingBranches folds completed non-critical branch subtrees', () => {
    const nodes: GraphNode[] = [
      { id: 'f1', type: 'fact', data: { kind: 'fact', label: '事实一', isCriticalPath: true } },
      { id: 'f2', type: 'fact', data: { kind: 'fact', label: '事实二', isCriticalPath: false } },
      { id: 'f3', type: 'fact', data: { kind: 'fact', label: '事实三', isCriticalPath: false } },
      { id: 'f4', type: 'fact', data: { kind: 'fact', label: '事实四', isCriticalPath: false } },
    ]
    const edges: GraphEdge[] = [
      {
        id: 'e-f1-f2',
        source: 'f1',
        target: 'f2',
        data: { state: 'concluded', intent: { id: 'i2' } },
      },
      {
        id: 'e-f2-f3',
        source: 'f2',
        target: 'f3',
        data: { state: 'concluded', intent: { id: 'i3' } },
      },
      {
        id: 'e-f1-f4',
        source: 'f1',
        target: 'f4',
        data: { state: 'concluded', intent: { id: 'i4' } },
      },
    ]

    const result = collapsePendingBranches(nodes, edges, { enabled: true })
    assert.equal(result.collapsedCount, 3)
    assert.equal(result.groupCount, 1)
    assert.deepEqual(result.nodes.map((n) => n.id).sort(), ['f1', 'pending-group:f1'])
  })

  it('collapsePendingBranches folds a single long non-critical tail branch', () => {
    const nodes: GraphNode[] = [
      { id: 'f1', type: 'fact', data: { kind: 'fact', label: '保留主节点', isCriticalPath: true } },
      { id: 'f2', type: 'fact', data: { kind: 'fact', label: '长尾二' } },
      { id: 'f3', type: 'fact', data: { kind: 'fact', label: '长尾三' } },
      { id: 'f4', type: 'fact', data: { kind: 'fact', label: '长尾四' } },
    ]
    const edges: GraphEdge[] = [
      {
        id: 'e-f1-f2',
        source: 'f1',
        target: 'f2',
        data: { state: 'concluded', intent: { id: 'i2' } },
      },
      {
        id: 'e-f2-f3',
        source: 'f2',
        target: 'f3',
        data: { state: 'concluded', intent: { id: 'i3' } },
      },
      {
        id: 'e-f3-f4',
        source: 'f3',
        target: 'f4',
        data: { state: 'concluded', intent: { id: 'i4' } },
      },
    ]

    const result = collapsePendingBranches(nodes, edges, { enabled: true })
    assert.equal(result.collapsedCount, 3)
    assert.equal(result.groupCount, 1)
    assert.deepEqual(result.nodes.map((n) => n.id).sort(), ['f1', 'pending-group:f1'])
    assert.ok(
      result.nodes
        .find((n) => n.id === 'pending-group:f1')
        ?.data?.label?.includes('已收拢 1 个分支'),
    )
  })

  it('collapsePendingBranches keeps auto groups non-overlapping by preferring deeper tails', () => {
    const nodes: GraphNode[] = [
      { id: 'origin', type: 'fact', data: { kind: 'origin', label: '起点' } },
      { id: 'f1', type: 'fact', data: { kind: 'fact', label: '一级分支' } },
      { id: 'f2', type: 'fact', data: { kind: 'fact', label: '二级分支' } },
      { id: 'f3', type: 'fact', data: { kind: 'fact', label: '三级分支' } },
      { id: 'f4', type: 'fact', data: { kind: 'fact', label: '四级分支' } },
      { id: 'f5', type: 'fact', data: { kind: 'fact', label: '五级分支' } },
    ]
    const edges: GraphEdge[] = [
      {
        id: 'e-origin-f1',
        source: 'origin',
        target: 'f1',
        data: { state: 'concluded', intent: { id: 'i1' } },
      },
      {
        id: 'e-f1-f2',
        source: 'f1',
        target: 'f2',
        data: { state: 'concluded', intent: { id: 'i2' } },
      },
      {
        id: 'e-f2-f3',
        source: 'f2',
        target: 'f3',
        data: { state: 'concluded', intent: { id: 'i3' } },
      },
      {
        id: 'e-f3-f4',
        source: 'f3',
        target: 'f4',
        data: { state: 'concluded', intent: { id: 'i4' } },
      },
      {
        id: 'e-f4-f5',
        source: 'f4',
        target: 'f5',
        data: { state: 'concluded', intent: { id: 'i5' } },
      },
    ]

    const result = collapsePendingBranches(nodes, edges, { enabled: true })
    assert.equal(result.groupCount, 1)
    assert.deepEqual(result.nodes.map((n) => n.id).sort(), [
      'f1',
      'f2',
      'origin',
      'pending-group:f2',
    ])
    assert.equal(result.nodes.filter((n) => n.data?.kind === 'pending_group').length, 1)
  })

  it('collapsePendingBranches groups multi-source branches and redirects incoming edges', () => {
    const nodes: GraphNode[] = [
      { id: 'f1', type: 'fact', data: { kind: 'fact', label: '事实一' } },
      { id: 'f2', type: 'fact', data: { kind: 'fact', label: '事实二' } },
      { id: 'pending:i1', type: 'fact', data: { kind: 'pending', label: 'pending一' } },
      { id: 'pending:i2', type: 'fact', data: { kind: 'pending', label: 'pending二' } },
    ]
    const edges: GraphEdge[] = [
      {
        id: 'e-f1-pi1',
        source: 'f1',
        target: 'pending:i1',
        data: { state: 'unclaimed', intent: { id: 'i1' } },
      },
      {
        id: 'e-f1-pi2',
        source: 'f1',
        target: 'pending:i2',
        data: { state: 'unclaimed', intent: { id: 'i2' } },
      },
      {
        id: 'e-f2-pi2',
        source: 'f2',
        target: 'pending:i2',
        data: { state: 'unclaimed', intent: { id: 'i2' } },
      },
    ]

    const result = collapsePendingBranches(nodes, edges, { enabled: true })
    assert.equal(result.collapsedCount, 2)
    assert.equal(result.groupCount, 1)
    assert.deepEqual(result.nodes.map((n) => n.id).sort(), ['f1', 'f2', 'pending-group:f1'])

    const redirectedEdge = result.edges.find((e) => e.id === 'e-f2-pi2')!
    assert.ok(redirectedEdge)
    assert.equal(redirectedEdge.source, 'f2')
    assert.equal(redirectedEdge.target, 'pending-group:f1')
  })

  it('applyManualCollapse hides downstream subtrees and filters edges', () => {
    const nodes: GraphNode[] = [
      { id: 'f1', type: 'fact', data: { kind: 'origin' } },
      { id: 'f2', type: 'fact', data: { kind: 'fact' } },
      { id: 'f3', type: 'fact', data: { kind: 'fact' } },
    ]
    const edges: GraphEdge[] = [
      { id: 'e-f1-f2', source: 'f1', target: 'f2' },
      { id: 'e-f2-f3', source: 'f2', target: 'f3' },
    ]

    const manuallyCollapsed = new Set(['f2'])
    const result = applyManualCollapse(nodes, edges, manuallyCollapsed)
    assert.deepEqual(result.nodes.map((n) => n.id).sort(), ['f1', 'f2'])
    assert.deepEqual(
      result.edges.map((e) => e.id),
      ['e-f1-f2'],
    )
  })

  it('collapsePendingBranches computes detailed statistics, depth, and critical warning', () => {
    const nodes: GraphNode[] = [
      { id: 'f1', type: 'fact', data: { kind: 'fact', label: '事实一', isCriticalPath: true } },
      {
        id: 'f2',
        type: 'fact',
        data: { kind: 'pending', status: 'exploring', liveState: 'running', label: '待探索二' },
      },
      { id: 'f3', type: 'fact', data: { kind: 'pending', status: 'unclaimed', label: '待探索三' } },
      { id: 'f4', type: 'fact', data: { kind: 'fact', label: '事实四', isCriticalPath: true } },
    ]
    const edges: GraphEdge[] = [
      {
        id: 'e-f1-f2',
        source: 'f1',
        target: 'f2',
        type: 'intent',
        data: { state: 'exploring', intent: { id: 'i2' } },
      },
      {
        id: 'e-f1-f3',
        source: 'f1',
        target: 'f3',
        type: 'intent',
        data: { state: 'unclaimed', intent: { id: 'i3' } },
      },
      {
        id: 'e-f2-f4',
        source: 'f2',
        target: 'f4',
        type: 'intent',
        data: { state: 'concluded', isCriticalPath: true, intent: { id: 'i4' } },
      },
    ]

    const result = collapsePendingBranches(nodes, edges, { enabled: true })
    const groupNode = result.nodes.find((n) => n.id === 'pending-group:f1')!
    assert.ok(groupNode)
    assert.equal(groupNode.data?.pendingGroup.collapsedCount, 2)
    assert.equal(groupNode.data?.pendingGroup.runningCount, 1)
    assert.equal(groupNode.data?.pendingGroup.unclaimedCount, 1)
    assert.equal(groupNode.data?.pendingGroup.maxDepth, 1)
    assert.equal(groupNode.data?.pendingGroup.hasCriticalPath, true)
    assert.ok(groupNode.data?.label?.includes('警告: 包含关键路径!'))

    const groupEdge = result.edges.find((e) => e.id === 'edge-pending-group-f1')!
    assert.ok(groupEdge)
    assert.equal(groupEdge.data?.mergedCount, 2)
  })

  it('keeps Reason audit edges out of automatic branch collapse topology', () => {
    const branchNodes: GraphNode[] = [
      { id: 'f1', type: 'fact', data: { kind: 'fact', label: '入口事实' } },
      { id: 'pending:i1', type: 'fact', data: { kind: 'pending', label: '检查权限' } },
      { id: 'pending:i2', type: 'fact', data: { kind: 'pending', label: '检查会话' } },
      { id: 'reason:r1', type: 'reason', data: { kind: 'reason', label: '选择下一步' } },
    ]
    const branchEdges: GraphEdge[] = [
      { id: 'e-i1', source: 'f1', target: 'pending:i1', type: 'intent' },
      { id: 'e-i2', source: 'f1', target: 'pending:i2', type: 'intent' },
      {
        id: 'e-reason-input',
        source: 'f1',
        target: 'reason:r1',
        type: 'reason-audit',
        data: { auditDirection: 'input', state: 'succeeded' },
      },
      {
        id: 'e-reason-output',
        source: 'reason:r1',
        target: 'pending:i1',
        type: 'reason-audit',
        data: { auditDirection: 'output', state: 'succeeded' },
      },
    ]

    const result = collapsePendingBranches(branchNodes, branchEdges, { enabled: true })

    assert.equal(result.collapsedCount, 2)
    assert.equal(result.groupCount, 1)
    assert.ok(result.nodes.some((node) => node.id === 'reason:r1'))
    assert.ok(result.edges.some((edge) => edge.id === 'e-reason-input'))
    const remappedOutput = result.edges.find((edge) => edge.id === 'e-reason-output')
    assert.ok(remappedOutput)
    assert.equal(remappedOutput.target, 'pending-group:f1')
    assert.equal(remappedOutput.data?.collapsedEndpointCount, 1)
  })

  it('keeps Reason audit edges out of manual collapse traversal', () => {
    const manualNodes: GraphNode[] = [
      { id: 'f1', type: 'fact', data: { kind: 'origin' } },
      { id: 'f2', type: 'fact', data: { kind: 'fact' } },
      { id: 'f3', type: 'fact', data: { kind: 'fact' } },
      { id: 'reason:r1', type: 'reason', data: { kind: 'reason' } },
    ]
    const manualEdges: GraphEdge[] = [
      { id: 'e-f1-f2', source: 'f1', target: 'f2', type: 'intent' },
      { id: 'e-f2-f3', source: 'f2', target: 'f3', type: 'intent' },
      { id: 'e-f2-reason', source: 'f2', target: 'reason:r1', type: 'reason-audit' },
      { id: 'e-f3-reason', source: 'f3', target: 'reason:r1', type: 'reason-audit' },
    ]

    const result = applyManualCollapse(manualNodes, manualEdges, new Set(['f2']))

    assert.deepEqual(result.nodes.map((node) => node.id).sort(), ['f1', 'f2', 'reason:r1'])
    assert.ok(result.edges.some((edge) => edge.id === 'e-f2-reason'))
    assert.notOk(result.edges.some((edge) => edge.id === 'e-f3-reason'))
    assert.equal(result.nodes.find((node) => node.id === 'f2')?.data?.collapsedDescendantsCount, 1)
  })

  it('redirects a hidden Reason output to the manual collapse anchor', () => {
    const manualNodes: GraphNode[] = [
      { id: 'origin', type: 'fact', data: { kind: 'origin' } },
      { id: 'f1', type: 'fact', data: { kind: 'fact' } },
      { id: 'f2', type: 'fact', data: { kind: 'fact' } },
      { id: 'reason:r1', type: 'reason', data: { kind: 'reason' } },
    ]
    const manualEdges: GraphEdge[] = [
      { id: 'e-origin-f1', source: 'origin', target: 'f1', type: 'intent' },
      { id: 'e-f1-f2', source: 'f1', target: 'f2', type: 'intent' },
      {
        id: 'e-reason-input',
        source: 'origin',
        target: 'reason:r1',
        type: 'reason-audit',
        data: { auditDirection: 'input', reasonRunId: 'r1' },
      },
      {
        id: 'e-reason-output',
        source: 'reason:r1',
        target: 'f2',
        type: 'reason-audit',
        data: { auditDirection: 'output', reasonRunId: 'r1' },
      },
    ]

    const result = applyManualCollapse(manualNodes, manualEdges, new Set(['f1']))

    assert.deepEqual(result.nodes.map((node) => node.id).sort(), ['f1', 'origin', 'reason:r1'])
    const outputEdge = result.edges.find((edge) => edge.id === 'e-reason-output')
    assert.equal(outputEdge?.source, 'reason:r1')
    assert.equal(outputEdge?.target, 'f1')
    assert.equal(outputEdge?.data?.collapsedEndpointCount, 1)
  })

  it('searches and status-filters Reason decisions without marking them critical', () => {
    const reasonNode: GraphNode = {
      id: 'reason:r1',
      type: 'reason',
      data: {
        kind: 'reason',
        label: '选择认证绕过路径',
        runStatus: 'running',
        reason: {
          runId: 'r1',
          worker: 'reason-worker',
          status: 'running',
          startedAt: '2026-08-23T10:00:00Z',
          endedAt: '',
          outcome: 'intents',
          rationale: '优先验证会话固定风险',
          confidence: 0.84,
          blockedBy: [],
          dependsOnTargets: [],
          rejectedOptions: [],
          inputNodeIds: ['f1'],
          outputNodeIds: ['pending:i1'],
          auditAvailable: true,
          errorType: '',
          errorMessage: '',
        },
      },
    }
    const reasonEdges: GraphEdge[] = [
      {
        id: 'e-reason-input',
        source: 'f1',
        target: 'reason:r1',
        type: 'reason-audit',
        data: { state: 'running', auditDirection: 'input' },
      },
    ]
    const reasonNodes = [nodes[1], reasonNode]

    const searchResult = filterGraphElements(reasonNodes, reasonEdges, { query: '会话固定' })
    assert.deepEqual(searchResult.nodes.map((node) => node.id).sort(), ['f1', 'reason:r1'])
    assert.deepEqual(
      searchResult.edges.map((edge) => edge.id),
      ['e-reason-input'],
    )

    const runningResult = filterGraphElements(reasonNodes, reasonEdges, { status: 'running' })
    assert.deepEqual(runningResult.nodes.map((node) => node.id).sort(), ['f1', 'reason:r1'])
    assert.deepEqual(
      runningResult.edges.map((edge) => edge.id),
      ['e-reason-input'],
    )

    const criticalResult = filterGraphElements(reasonNodes, reasonEdges, { status: 'critical' })
    assert.notOk(criticalResult.nodes.some((node) => node.id === 'reason:r1'))
    assert.notOk(criticalResult.edges.some((edge) => edge.id === 'e-reason-input'))
  })

  it('applyManualCollapse computes collapsedDescendantsCount for hidden descendants', () => {
    const nodes: GraphNode[] = [
      { id: 'f1', type: 'fact', data: { kind: 'origin' } },
      { id: 'f2', type: 'fact', data: { kind: 'fact' } },
      { id: 'f3', type: 'fact', data: { kind: 'fact' } },
      { id: 'f4', type: 'fact', data: { kind: 'fact' } },
    ]
    const edges: GraphEdge[] = [
      { id: 'e-f1-f2', source: 'f1', target: 'f2' },
      { id: 'e-f2-f3', source: 'f2', target: 'f3' },
      { id: 'e-f3-f4', source: 'f3', target: 'f4' },
      { id: 'e-f1-f4', source: 'f1', target: 'f4' },
    ]

    const manuallyCollapsed = new Set(['f2'])
    const result = applyManualCollapse(nodes, edges, manuallyCollapsed)

    const collapsedNode = result.nodes.find((n) => n.id === 'f2')!
    assert.ok(collapsedNode)
    assert.equal(collapsedNode.data?.collapsedDescendantsCount, 1)
  })
})
