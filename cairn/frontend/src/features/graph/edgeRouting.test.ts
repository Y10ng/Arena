import { describe, it, assert } from 'vitest'
import {
  buildEdgeRoutes,
  compactGraphEdgesForRendering,
  projectVisibleNarrativeEdges,
} from './edgeRouting.js'
import type { EdgeRoute } from './edgeRouting.js'
import type { GraphEdge, GraphNode } from './graphFilters.js'
import type { ReasonAuditData } from './graphTypes.js'

const nodes: GraphNode[] = [
  { id: 'origin', position: { x: 0, y: 120 }, data: { nodeWidth: 240, nodeHeight: 150 } },
  { id: 'f1', position: { x: 420, y: 0 }, data: { nodeWidth: 240, nodeHeight: 150 } },
  { id: 'f2', position: { x: 420, y: 220 }, data: { nodeWidth: 240, nodeHeight: 150 } },
  { id: 'goal', position: { x: 840, y: 120 }, data: { nodeWidth: 240, nodeHeight: 150 } },
]

function assertDirectRoute(route: EdgeRoute): void {
  assert.deepEqual(Object.keys(route).sort(), ['labelX', 'labelY', 'sx', 'sy', 'tx', 'ty'])
  assert.ok(Object.values(route).every(Number.isFinite))
  assert.equal(route.labelX, (route.sx + route.tx) / 2)
  assert.equal(route.labelY, (route.sy + route.ty) / 2 - 8)
}

describe('edge routing', () => {
  it('fans out LR ports and remains deterministic when arrays are reordered', () => {
    const edges: GraphEdge[] = [
      { id: 'e1', source: 'origin', target: 'f1' },
      { id: 'e2', source: 'origin', target: 'f2' },
      { id: 'e3', source: 'f1', target: 'goal' },
      { id: 'e4', source: 'f2', target: 'goal' },
    ]
    const routes = buildEdgeRoutes(nodes, edges, 'LR')
    const reordered = buildEdgeRoutes([...nodes].reverse(), [...edges].reverse(), 'LR')

    assert.notEqual(routes.e1.sy, routes.e2.sy)
    assert.notEqual(routes.e3.ty, routes.e4.ty)
    edges.forEach((edge) => {
      assert.deepEqual(reordered[edge.id], routes[edge.id])
      assertDirectRoute(routes[edge.id])
    })
  })

  it('keeps parallel direct lines deterministic without routing lanes', () => {
    const edges: GraphEdge[] = [
      { id: 'parallel-b', source: 'origin', target: 'goal' },
      { id: 'parallel-a', source: 'origin', target: 'goal' },
    ]
    const routes = buildEdgeRoutes(nodes, edges, 'LR')
    const reordered = buildEdgeRoutes(nodes, [...edges].reverse(), 'LR')

    assert.notEqual(routes['parallel-a'].sy, routes['parallel-b'].sy)
    assert.notEqual(routes['parallel-a'].ty, routes['parallel-b'].ty)
    assert.deepEqual(reordered, routes)
    assertDirectRoute(routes['parallel-a'])
    assertDirectRoute(routes['parallel-b'])
  })

  it('fans out TB ports and returns direct finite endpoints in both directions', () => {
    const tbNodes: GraphNode[] = [
      { id: 's', position: { x: 220, y: 0 }, data: { nodeWidth: 240, nodeHeight: 150 } },
      { id: 't1', position: { x: 0, y: 360 }, data: { nodeWidth: 240, nodeHeight: 150 } },
      { id: 't2', position: { x: 440, y: 360 }, data: { nodeWidth: 240, nodeHeight: 150 } },
    ]
    const edges: GraphEdge[] = [
      { id: 'down-left', source: 's', target: 't1' },
      { id: 'down-right', source: 's', target: 't2' },
      { id: 'up', source: 't1', target: 's' },
    ]
    const routes = buildEdgeRoutes(tbNodes, edges, 'TB')

    assert.notEqual(routes['down-left'].sx, routes['down-right'].sx)
    assert.equal(routes['down-left'].sy, routes['down-right'].sy)
    edges.forEach((edge) => assertDirectRoute(routes[edge.id]))
  })

  it('produces a finite direct LR route for backward edges', () => {
    const edge: GraphEdge = { id: 'backward', source: 'goal', target: 'origin' }
    const route = buildEdgeRoutes(nodes, [edge], 'LR').backward

    assert.ok(route.tx < route.sx)
    assertDirectRoute(route)
  })

  it('compacts multi-source join edges into one stable representative render edge', () => {
    const compactNodes: GraphNode[] = [
      { id: 'origin', position: { x: 0, y: 120 }, data: { nodeWidth: 240, nodeHeight: 150 } },
      { id: 'f1', position: { x: 420, y: 0 }, data: { nodeWidth: 240, nodeHeight: 150 } },
      { id: 'f2', position: { x: 620, y: 220 }, data: { nodeWidth: 240, nodeHeight: 150 } },
      { id: 'goal', position: { x: 980, y: 120 }, data: { nodeWidth: 240, nodeHeight: 150 } },
    ]
    const joinGroup = {
      id: 'join:i10',
      intentId: 'i10',
      sourceIds: ['origin', 'f1', 'f2'],
      sourceCount: 3,
      targetId: 'goal',
      markerEdgeId: 'join-origin',
    }
    const joinEdges: GraphEdge[] = [
      {
        id: 'join-origin',
        source: 'origin',
        target: 'goal',
        data: { joinGroup, intent: { id: 'i10' } },
      },
      { id: 'join-f1', source: 'f1', target: 'goal', data: { joinGroup, intent: { id: 'i10' } } },
      { id: 'join-f2', source: 'f2', target: 'goal', data: { joinGroup, intent: { id: 'i10' } } },
      { id: 'single', source: 'origin', target: 'f1', data: { intent: { id: 'i01' } } },
    ]
    const compacted = compactGraphEdgesForRendering(joinEdges, compactNodes, { dir: 'LR' })
    const reordered = compactGraphEdgesForRendering(
      [...joinEdges].reverse(),
      [...compactNodes].reverse(),
      { dir: 'LR' },
    )
    const compactedJoin = compacted.find((edge) => edge.data?.joinGroup?.id === 'join:i10')!

    assert.deepEqual(
      compacted.map((edge) => edge.id),
      ['single', 'join-f2'],
    )
    assert.deepEqual(reordered, compacted)
    assert.equal(compactedJoin.data?.renderedSourceCount, 3)
    assert.deepEqual(compactedJoin.data?.hiddenEdgeIds.sort(), ['join-f1', 'join-origin'])
  })

  it('never compacts reason-audit edges even when they carry join metadata', () => {
    const joinGroup = { id: 'join:audit-safe', sourceIds: ['origin', 'f1'] }
    const edges: GraphEdge[] = [
      { id: 'intent-a', source: 'origin', target: 'goal', type: 'intent', data: { joinGroup } },
      { id: 'intent-b', source: 'f1', target: 'goal', type: 'intent', data: { joinGroup } },
      { id: 'audit-a', source: 'origin', target: 'f1', type: 'reason-audit', data: { joinGroup } },
      { id: 'audit-b', source: 'f1', target: 'goal', type: 'reason-audit', data: { joinGroup } },
    ]
    const compacted = compactGraphEdgesForRendering(edges, nodes, { dir: 'LR' })

    assert.equal(compacted.length, 3)
    assert.deepEqual(
      compacted
        .filter((edge) => edge.type === 'reason-audit')
        .map((edge) => edge.id)
        .sort(),
      ['audit-a', 'audit-b'],
    )
  })

  it('keeps a Reason-associated Intent as the same direct line', () => {
    const edge: GraphEdge = {
      id: 'reason-intent',
      source: 'origin',
      target: 'goal',
      type: 'intent',
      data: { reasonRunId: 'r1', reasonNodeId: 'reason:r1' },
    }
    const plainEdge: GraphEdge = { ...edge, data: {} }

    assert.deepEqual(
      buildEdgeRoutes(nodes, [edge], 'LR')[edge.id],
      buildEdgeRoutes(nodes, [plainEdge], 'LR')[plainEdge.id],
    )
  })

  it('projects a complete Reason chain and preserves Intent narrative metadata', () => {
    const narrativeNodes: GraphNode[] = [
      { id: 'source', data: { kind: 'fact' } },
      { id: 'reason:r1', data: { kind: 'reason', reason: { runId: 'r1' } as ReasonAuditData } },
      { id: 'pending:i1', data: { kind: 'pending' } },
    ]
    const narrativeEdges: GraphEdge[] = [
      {
        id: 'intent:i1:source',
        source: 'source',
        target: 'pending:i1',
        type: 'intent',
        data: {
          intentId: 'i1',
          intent: { id: 'i1', description: '验证新入口' },
          reasonRunId: 'r1',
          reasonNodeId: 'reason:r1',
          state: 'exploring',
        },
      },
      {
        id: 'reason-input:r1:source',
        source: 'source',
        target: 'reason:r1',
        type: 'reason-audit',
        data: { auditDirection: 'input', reasonRunId: 'r1' },
      },
      {
        id: 'reason-output:r1:i1',
        source: 'reason:r1',
        target: 'pending:i1',
        type: 'reason-audit',
        data: { auditDirection: 'output', reasonRunId: 'r1', intentId: 'i1' },
      },
    ]

    const projected = projectVisibleNarrativeEdges(narrativeEdges, narrativeNodes)

    assert.deepEqual(
      projected.map((edge) => edge.id),
      ['reason-input:r1:source', 'reason-output:r1:i1'],
    )
    const output = projected.find((edge) => edge.id === 'reason-output:r1:i1')!
    assert.equal(output.data?.label, '验证新入口')
    assert.deepEqual(output.data?.presentation?.representedIntentEdgeIds, ['intent:i1:source'])
    assert.equal(output.data?.presentation?.narratives?.[0].intentId, 'i1')
  })

  it('retains the canonical Intent when the visible Reason chain is incomplete', () => {
    const incompleteNodes: GraphNode[] = [
      { id: 'source', data: { kind: 'fact' } },
      { id: 'reason:r1', data: { kind: 'reason', reason: { runId: 'r1' } as ReasonAuditData } },
      { id: 'pending:i1', data: { kind: 'pending' } },
    ]
    const incompleteEdges: GraphEdge[] = [
      {
        id: 'intent:i1:source',
        source: 'source',
        target: 'pending:i1',
        type: 'intent',
        data: { intentId: 'i1', reasonRunId: 'r1', reasonNodeId: 'reason:r1' },
      },
      {
        id: 'reason-output:r1:i1',
        source: 'reason:r1',
        target: 'pending:i1',
        type: 'reason-audit',
        data: { auditDirection: 'output', reasonRunId: 'r1', intentId: 'i1' },
      },
    ]

    const projected = projectVisibleNarrativeEdges(incompleteEdges, incompleteNodes)

    assert.ok(projected.some((edge) => edge.id === 'intent:i1:source'))
    assert.equal(
      projected.find((edge) => edge.id === 'reason-output:r1:i1')?.data?.presentation,
      undefined,
    )
  })

  it('retains the canonical Intent when a Reason input endpoint is not visible', () => {
    const visibleNodes: GraphNode[] = [
      { id: 'reason:r1', data: { kind: 'reason', reason: { runId: 'r1' } as ReasonAuditData } },
      { id: 'pending:i1', data: { kind: 'pending' } },
    ]
    const edges: GraphEdge[] = [
      {
        id: 'intent:i1:hidden',
        source: 'hidden-source',
        target: 'pending:i1',
        type: 'intent',
        data: {
          intentId: 'i1',
          reasonRunId: 'r1',
          reasonNodeId: 'reason:r1',
        },
      },
      {
        id: 'reason-input:r1:hidden',
        source: 'hidden-source',
        target: 'reason:r1',
        type: 'reason-audit',
        data: { auditDirection: 'input', reasonRunId: 'r1' },
      },
      {
        id: 'reason-output:r1:i1',
        source: 'reason:r1',
        target: 'pending:i1',
        type: 'reason-audit',
        data: { auditDirection: 'output', reasonRunId: 'r1', intentId: 'i1' },
      },
    ]

    const projected = projectVisibleNarrativeEdges(edges, visibleNodes)

    assert.ok(projected.some((edge) => edge.id === 'intent:i1:hidden'))
    assert.equal(
      projected.find((edge) => edge.id === 'reason-output:r1:i1')?.data?.presentation,
      undefined,
    )
  })

  it('compacts Reason inputs to the rightmost real source and keeps all input IDs', () => {
    const compactNodes: GraphNode[] = [
      { id: 'old', position: { x: 0, y: 0 }, data: { nodeWidth: 240, nodeHeight: 150 } },
      { id: 'recent', position: { x: 840, y: 220 }, data: { nodeWidth: 240, nodeHeight: 150 } },
      {
        id: 'reason:r1',
        position: { x: 1260, y: 120 },
        data: { kind: 'reason', nodeWidth: 240, nodeHeight: 150 },
      },
    ]
    const inputEdges: GraphEdge[] = [
      {
        id: 'input:old',
        source: 'old',
        target: 'reason:r1',
        type: 'reason-audit',
        data: { auditDirection: 'input', reasonRunId: 'r1' },
      },
      {
        id: 'input:recent',
        source: 'recent',
        target: 'reason:r1',
        type: 'reason-audit',
        data: { auditDirection: 'input', reasonRunId: 'r1' },
      },
    ]

    const compacted = compactGraphEdgesForRendering(inputEdges, compactNodes)

    assert.equal(compacted.length, 1)
    assert.equal(compacted[0].id, 'input:recent')
    assert.equal(compacted[0].data?.renderedSourceCount, 2)
    assert.deepEqual(compacted[0].data?.presentation?.inputNodeIds, ['old', 'recent'])
    assert.deepEqual(compacted[0].data?.presentation?.hiddenInputEdgeIds, ['input:old'])
  })

  it('keeps direct endpoints independent of unrelated blocker nodes', () => {
    const endpoints: GraphNode[] = [
      { id: 'source', position: { x: 0, y: 0 }, data: { nodeWidth: 240, nodeHeight: 150 } },
      { id: 'target', position: { x: 720, y: 0 }, data: { nodeWidth: 240, nodeHeight: 150 } },
    ]
    const blocker: GraphNode = {
      id: 'blocker',
      position: { x: 360, y: 0 },
      data: { kind: 'reason', nodeWidth: 240, nodeHeight: 150 },
    }
    const edge: GraphEdge = {
      id: 'crossing',
      source: 'source',
      target: 'target',
      type: 'intent',
      data: {},
    }
    const route = buildEdgeRoutes(endpoints, [edge], 'LR').crossing
    const blockedRoute = buildEdgeRoutes([blocker, ...endpoints].reverse(), [edge], 'LR').crossing

    assert.deepEqual(blockedRoute, route)
    assertDirectRoute(route)
  })
})
