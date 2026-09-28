import { describe, expect, it } from 'vitest'

import { getNodeLayoutKey, layoutSemanticTimeline } from './semanticTimelineLayout'
import type { GraphEdge, GraphNode, GraphNodeKind, ReasonAuditData } from './graphTypes'

function node(id: string, kind: GraphNodeKind, createdAt: string): GraphNode {
  return {
    id,
    type: kind,
    data: {
      kind,
      label: id,
      createdAt,
      layoutKey: kind === 'reason' ? id : `node:${id}`,
    },
  }
}

const baseNodes: GraphNode[] = [
  node('origin', 'origin', '2026-01-01T00:00:00Z'),
  {
    ...node('reason:r1', 'reason', '2026-01-01T00:00:01Z'),
    data: {
      ...node('reason:r1', 'reason', '2026-01-01T00:00:01Z').data,
      reason: { runId: 'r1' } as ReasonAuditData,
    },
  },
  {
    ...node('pending:i1', 'pending', '2026-01-01T00:00:02Z'),
    data: {
      ...node('pending:i1', 'pending', '2026-01-01T00:00:02Z').data,
      layoutKey: 'intent:i1',
      intent: { id: 'i1', created_at: '2026-01-01T00:00:02Z' },
      outputOrder: 0,
    },
  },
]

const baseEdges: GraphEdge[] = [
  {
    id: 'reason-input:r1:origin',
    source: 'origin',
    target: 'reason:r1',
    type: 'reason-audit',
    data: { auditDirection: 'input' },
  },
  {
    id: 'reason-output:r1:i1',
    source: 'reason:r1',
    target: 'pending:i1',
    type: 'reason-audit',
    data: { auditDirection: 'output', outputOrder: 0 },
  },
  {
    id: 'intent:i1',
    source: 'origin',
    target: 'pending:i1',
    type: 'intent',
    data: { intent: { id: 'i1' } },
  },
]

describe('semantic timeline layout', () => {
  it('places Fact, Reason, and outputs in fixed left-to-right columns', () => {
    const result = layoutSemanticTimeline(baseNodes, baseEdges)
    const byId = new Map(result.nodes.map((item) => [item.id, item]))

    expect(byId.get('origin')!.position!.x).toBeLessThan(byId.get('reason:r1')!.position!.x)
    expect(byId.get('reason:r1')!.position!.x).toBeLessThan(byId.get('pending:i1')!.position!.x)
    expect(byId.get('reason:r1')!.position!.y).toBe(byId.get('pending:i1')!.position!.y)
  })

  it('keeps committed nodes fixed and appends a new Reason output below its siblings', () => {
    const first = layoutSemanticTimeline(baseNodes, baseEdges)
    const nextNode: GraphNode = {
      ...node('pending:i2', 'pending', '2026-01-01T00:00:03Z'),
      data: {
        ...node('pending:i2', 'pending', '2026-01-01T00:00:03Z').data,
        layoutKey: 'intent:i2',
        intent: { id: 'i2', created_at: '2026-01-01T00:00:03Z' },
        outputOrder: 1,
      },
    }
    const nextEdges: GraphEdge[] = [
      {
        id: 'reason-output:r1:i2',
        source: 'reason:r1',
        target: 'pending:i2',
        type: 'reason-audit',
        data: { auditDirection: 'output', outputOrder: 1 },
      },
      {
        id: 'intent:i2',
        source: 'origin',
        target: 'pending:i2',
        type: 'intent',
        data: { intent: { id: 'i2' } },
      },
    ]
    const second = layoutSemanticTimeline([...baseNodes, nextNode], [...baseEdges, ...nextEdges], {
      previousPlacements: first.placements,
    })

    for (const existing of baseNodes) {
      expect(second.placements[getNodeLayoutKey(existing)]).toEqual(
        first.placements[getNodeLayoutKey(existing)],
      )
    }
    expect(second.placements['intent:i2'].y).toBeGreaterThan(second.placements['intent:i1'].y)
  })

  it('preserves the exact position when a Pending output becomes a Fact', () => {
    const first = layoutSemanticTimeline(baseNodes, baseEdges)
    const fact: GraphNode = {
      id: 'fact:f1',
      layoutKey: 'intent:i1',
      type: 'fact',
      data: {
        kind: 'fact',
        label: 'result',
        fact: { id: 'fact:f1', created_at: '2026-01-01T00:00:04Z' },
      },
    }
    const nodes = baseNodes.map((item) => (item.id === 'pending:i1' ? fact : item))
    const edges = baseEdges.map((edge) => ({
      ...edge,
      target: edge.target === 'pending:i1' ? 'fact:f1' : edge.target,
    }))
    const second = layoutSemanticTimeline(nodes, edges, {
      previousPlacements: first.placements,
      measuredNodeSizes: { 'fact:f1': { width: 320, height: 220 } },
    })

    expect(second.placements['intent:i1']).toEqual(first.placements['intent:i1'])
    expect(second.nodes.find((item) => item.id === 'fact:f1')!.position).toEqual(
      first.nodes.find((item) => item.id === 'pending:i1')!.position,
    )
    expect(second.nodes.find((item) => item.id === 'fact:f1')!.data?.nodeWidth).toBe(320)
  })

  it('uses only Intent edges for causal coordinates', () => {
    const withoutAudit = layoutSemanticTimeline(
      baseNodes.filter((item) => item.data?.kind !== 'reason'),
      baseEdges.filter((edge) => edge.type === 'intent'),
    )
    const withAudit = layoutSemanticTimeline(baseNodes, baseEdges)

    for (const nodeId of ['origin', 'pending:i1']) {
      expect(withAudit.nodes.find((item) => item.id === nodeId)?.position).toEqual(
        withoutAudit.nodes.find((item) => item.id === nodeId)?.position,
      )
    }
    expect(withAudit.placements['node:origin'].rank).toBe(0)
    expect(withAudit.placements['reason:r1'].rank).toBe(1)
    expect(withAudit.placements['intent:i1'].rank).toBe(2)
    expect(withAudit.placements['intent:i1'].x - withAudit.placements['node:origin'].x).toBe(840)
  })

  it('places a multi-input target after its deepest causal source', () => {
    const causalNodes: GraphNode[] = [
      node('origin', 'origin', '2026-01-01T00:00:00Z'),
      node('fact:a', 'fact', '2026-01-01T00:00:01Z'),
      node('fact:b', 'fact', '2026-01-01T00:00:02Z'),
      node('fact:join', 'fact', '2026-01-01T00:00:03Z'),
    ]
    const causalEdges: GraphEdge[] = [
      { id: 'i1', source: 'origin', target: 'fact:a', type: 'intent' },
      { id: 'i2', source: 'fact:a', target: 'fact:b', type: 'intent' },
      { id: 'i3a', source: 'origin', target: 'fact:join', type: 'intent' },
      { id: 'i3b', source: 'fact:b', target: 'fact:join', type: 'intent' },
    ]

    const result = layoutSemanticTimeline(causalNodes, causalEdges)

    expect(result.placements['node:fact:a'].rank).toBe(2)
    expect(result.placements['node:fact:b'].rank).toBe(4)
    expect(result.placements['node:fact:join'].rank).toBe(6)
  })

  it('rejects a previous placement when the authoritative causal rank changes', () => {
    const first = layoutSemanticTimeline(baseNodes, baseEdges)
    const chainedEdges = baseEdges.map((edge) =>
      edge.type === 'intent' ? { ...edge, source: 'fact:parent' } : edge,
    )
    const parent = node('fact:parent', 'fact', '2026-01-01T00:00:01Z')
    const parentEdge: GraphEdge = {
      id: 'intent:parent',
      source: 'origin',
      target: 'fact:parent',
      type: 'intent',
    }
    const second = layoutSemanticTimeline([...baseNodes, parent], [...chainedEdges, parentEdge], {
      previousPlacements: first.placements,
    })

    expect(first.placements['intent:i1'].rank).toBe(2)
    expect(second.placements['intent:i1'].rank).toBe(4)
    expect(second.placements['intent:i1'].x).not.toBe(first.placements['intent:i1'].x)
  })

  it('places every output beside its Reason even when canonical depths differ', () => {
    const nodes: GraphNode[] = [
      node('origin', 'origin', '2026-01-01T00:00:00Z'),
      node('fact:early', 'fact', '2026-01-01T00:00:01Z'),
      node('fact:deep', 'fact', '2026-01-01T00:00:02Z'),
      {
        ...node('reason:late', 'reason', '2026-01-01T00:00:03Z'),
        data: {
          ...node('reason:late', 'reason', '2026-01-01T00:00:03Z').data,
          reason: {
            runId: 'late',
            startedAt: '2026-01-01T00:00:03Z',
            endedAt: '2026-01-01T00:00:04Z',
          } as ReasonAuditData,
        },
      },
      node('pending:shallow', 'pending', '2026-01-01T00:00:04Z'),
      node('pending:deep', 'pending', '2026-01-01T00:00:04Z'),
    ]
    const edges: GraphEdge[] = [
      { id: 'i1', source: 'origin', target: 'fact:early', type: 'intent' },
      { id: 'i2', source: 'fact:early', target: 'fact:deep', type: 'intent' },
      { id: 'i3', source: 'fact:early', target: 'pending:shallow', type: 'intent' },
      { id: 'i4', source: 'fact:deep', target: 'pending:deep', type: 'intent' },
      {
        id: 'in:early',
        source: 'fact:early',
        target: 'reason:late',
        type: 'reason-audit',
        data: { auditDirection: 'input' },
      },
      {
        id: 'in:deep',
        source: 'fact:deep',
        target: 'reason:late',
        type: 'reason-audit',
        data: { auditDirection: 'input' },
      },
      {
        id: 'out:shallow',
        source: 'reason:late',
        target: 'pending:shallow',
        type: 'reason-audit',
        data: { auditDirection: 'output', outputOrder: 0 },
      },
      {
        id: 'out:deep',
        source: 'reason:late',
        target: 'pending:deep',
        type: 'reason-audit',
        data: { auditDirection: 'output', outputOrder: 1 },
      },
    ]

    const result = layoutSemanticTimeline(nodes, edges)
    const reasonRank = result.placements['reason:late'].rank

    expect(result.placements['node:pending:shallow'].rank).toBe(reasonRank + 1)
    expect(result.placements['node:pending:deep'].rank).toBe(reasonRank + 1)
    expect(result.placements['node:fact:early'].rank).toBeLessThan(reasonRank)
    expect(result.placements['node:fact:deep'].rank).toBeLessThan(reasonRank)
  })

  it('keeps duplicate audit edges from one Reason bound to the same output column', () => {
    const reason: GraphNode = {
      ...node('reason:r1', 'reason', '2026-01-01T00:00:03Z'),
      data: {
        ...node('reason:r1', 'reason', '2026-01-01T00:00:03Z').data,
        reason: {
          runId: 'r1',
          startedAt: '2026-01-01T00:00:03Z',
          endedAt: '2026-01-01T00:00:04Z',
        } as ReasonAuditData,
      },
    }
    const nodes = [
      node('origin', 'origin', '2026-01-01T00:00:00Z'),
      node('fact:a', 'fact', '2026-01-01T00:00:01Z'),
      node('fact:b', 'fact', '2026-01-01T00:00:02Z'),
      reason,
      node('pending:i1', 'pending', '2026-01-01T00:00:04Z'),
    ]
    const edges: GraphEdge[] = [
      { id: 'i1', source: 'origin', target: 'fact:a', type: 'intent' },
      { id: 'i2', source: 'fact:a', target: 'fact:b', type: 'intent' },
      {
        id: 'input:r1',
        source: 'fact:b',
        target: 'reason:r1',
        type: 'reason-audit',
        data: { auditDirection: 'input' },
      },
      {
        id: 'output:r1:a',
        source: 'reason:r1',
        target: 'pending:i1',
        type: 'reason-audit',
        data: { auditDirection: 'output' },
      },
      {
        id: 'output:r1:b',
        source: 'reason:r1',
        target: 'pending:i1',
        type: 'reason-audit',
        data: { auditDirection: 'output' },
      },
    ]

    const result = layoutSemanticTimeline(nodes, edges)

    expect(result.placements['node:pending:i1'].rank).toBe(result.placements['reason:r1'].rank + 1)
  })

  it('shares a wave for overlapping independent Reasons and advances non-overlapping runs', () => {
    const reasonNode = (id: string, startedAt: string, endedAt: string): GraphNode => ({
      ...node(`reason:${id}`, 'reason', startedAt),
      data: {
        ...node(`reason:${id}`, 'reason', startedAt).data,
        reason: { runId: id, startedAt, endedAt } as ReasonAuditData,
      },
    })
    const nodes: GraphNode[] = [
      node('origin', 'origin', '2026-01-01T00:00:00Z'),
      reasonNode('a', '2026-01-01T00:00:01Z', '2026-01-01T00:00:05Z'),
      reasonNode('b', '2026-01-01T00:00:02Z', '2026-01-01T00:00:04Z'),
      reasonNode('c', '2026-01-01T00:00:06Z', '2026-01-01T00:00:07Z'),
      node('pending:a', 'pending', '2026-01-01T00:00:02Z'),
      node('pending:b', 'pending', '2026-01-01T00:00:03Z'),
      node('pending:c', 'pending', '2026-01-01T00:00:07Z'),
    ]
    const edges: GraphEdge[] = ['a', 'b', 'c'].flatMap((id) => [
      {
        id: `input:${id}`,
        source: 'origin',
        target: `reason:${id}`,
        type: 'reason-audit' as const,
        data: { auditDirection: 'input' as const },
      },
      {
        id: `output:${id}`,
        source: `reason:${id}`,
        target: `pending:${id}`,
        type: 'reason-audit' as const,
        data: { auditDirection: 'output' as const },
      },
      { id: `intent:${id}`, source: 'origin', target: `pending:${id}`, type: 'intent' as const },
    ])

    const result = layoutSemanticTimeline(nodes, edges)

    expect(result.placements['reason:a'].rank).toBe(result.placements['reason:b'].rank)
    expect(result.placements['reason:c'].rank).toBeGreaterThan(result.placements['reason:a'].rank)
  })

  it('keeps later Reason waves to the right of earlier waves after dependency shifts', () => {
    const reasonNode = (id: string, startedAt: string, endedAt: string): GraphNode => ({
      ...node(`reason:${id}`, 'reason', startedAt),
      data: {
        ...node(`reason:${id}`, 'reason', startedAt).data,
        reason: { runId: id, startedAt, endedAt } as ReasonAuditData,
      },
    })
    const nodes: GraphNode[] = [
      node('origin', 'origin', '2026-01-01T00:00:00Z'),
      node('fact:a', 'fact', '2026-01-01T00:00:01Z'),
      node('fact:b', 'fact', '2026-01-01T00:00:02Z'),
      node('fact:deep', 'fact', '2026-01-01T00:00:03Z'),
      reasonNode('early', '2026-01-01T00:00:04Z', '2026-01-01T00:00:05Z'),
      reasonNode('late', '2026-01-01T00:00:06Z', '2026-01-01T00:00:07Z'),
    ]
    const edges: GraphEdge[] = [
      { id: 'i1', source: 'origin', target: 'fact:a', type: 'intent' },
      { id: 'i2', source: 'fact:a', target: 'fact:b', type: 'intent' },
      { id: 'i3', source: 'fact:b', target: 'fact:deep', type: 'intent' },
      {
        id: 'input:early',
        source: 'fact:deep',
        target: 'reason:early',
        type: 'reason-audit',
        data: { auditDirection: 'input' },
      },
      {
        id: 'input:late',
        source: 'origin',
        target: 'reason:late',
        type: 'reason-audit',
        data: { auditDirection: 'input' },
      },
    ]

    const result = layoutSemanticTimeline(nodes, edges)

    expect(result.placements['reason:early'].rank).toBe(7)
    expect(result.placements['reason:late'].rank).toBeGreaterThan(
      result.placements['reason:early'].rank,
    )
  })

  it('keeps an unfinished Goal in the terminal column', () => {
    const nodes = [
      node('origin', 'origin', '2026-01-01T00:00:00Z'),
      node('fact:a', 'fact', '2026-01-01T00:00:01Z'),
      node('goal', 'goal', '2026-01-01T00:00:00Z'),
    ]
    const result = layoutSemanticTimeline(nodes, [
      { id: 'intent:a', source: 'origin', target: 'fact:a', type: 'intent' },
    ])

    expect(result.placements['node:goal'].rank).toBeGreaterThan(
      result.placements['node:fact:a'].rank,
    )
  })

  it('does not move an unfinished Goal past a Reason that consumes it', () => {
    const reason: GraphNode = {
      ...node('reason:r1', 'reason', '2026-01-01T00:00:01Z'),
      data: {
        ...node('reason:r1', 'reason', '2026-01-01T00:00:01Z').data,
        reason: {
          runId: 'r1',
          startedAt: '2026-01-01T00:00:01Z',
          endedAt: '2026-01-01T00:00:02Z',
        } as ReasonAuditData,
      },
    }
    const nodes = [
      node('origin', 'origin', '2026-01-01T00:00:00Z'),
      node('goal', 'goal', '2026-01-01T00:00:00Z'),
      reason,
      node('pending:i1', 'pending', '2026-01-01T00:00:02Z'),
    ]
    const edges: GraphEdge[] = [
      {
        id: 'input:goal',
        source: 'goal',
        target: 'reason:r1',
        type: 'reason-audit',
        data: { auditDirection: 'input' },
      },
      {
        id: 'output:i1',
        source: 'reason:r1',
        target: 'pending:i1',
        type: 'reason-audit',
        data: { auditDirection: 'output' },
      },
    ]

    const result = layoutSemanticTimeline(nodes, edges)

    expect(result.placements['node:goal'].rank).toBeLessThan(result.placements['reason:r1'].rank)
    expect(result.placements['reason:r1'].rank).toBeLessThan(
      result.placements['node:pending:i1'].rank,
    )
  })

  it('reduces crossings between adjacent causal ranks deterministically', () => {
    const nodes = [
      node('source:a', 'fact', '2026-01-01T00:00:01Z'),
      node('source:b', 'fact', '2026-01-01T00:00:02Z'),
      node('target:b', 'fact', '2026-01-01T00:00:03Z'),
      node('target:a', 'fact', '2026-01-01T00:00:04Z'),
    ]
    const edges: GraphEdge[] = [
      { id: 'a', source: 'source:a', target: 'target:a', type: 'intent' },
      { id: 'b', source: 'source:b', target: 'target:b', type: 'intent' },
    ]

    const forward = layoutSemanticTimeline(nodes, edges)
    const reversed = layoutSemanticTimeline([...nodes].reverse(), [...edges].reverse())
    const byId = new Map(forward.nodes.map((item) => [item.id, item]))

    expect(byId.get('source:a')!.position!.y).toBeLessThan(byId.get('source:b')!.position!.y)
    expect(byId.get('target:a')!.position!.y).toBeLessThan(byId.get('target:b')!.position!.y)
    expect(Object.fromEntries(reversed.nodes.map((item) => [item.id, item.position]))).toEqual(
      Object.fromEntries(forward.nodes.map((item) => [item.id, item.position])),
    )
  })

  it('centers a multi-input join from all parent centers', () => {
    const nodes = [
      node('parent:a', 'fact', '2026-01-01T00:00:01Z'),
      node('parent:b', 'fact', '2026-01-01T00:00:02Z'),
      node('join', 'fact', '2026-01-01T00:00:03Z'),
    ]
    const edges: GraphEdge[] = [
      { id: 'a', source: 'parent:a', target: 'join', type: 'intent' },
      { id: 'b', source: 'parent:b', target: 'join', type: 'intent' },
    ]

    const result = layoutSemanticTimeline(nodes, edges)
    const byId = new Map(result.nodes.map((item) => [item.id, item]))
    const parentCenters = ['parent:a', 'parent:b'].map((id) => {
      const item = byId.get(id)!
      return item.position!.y + item.data!.nodeHeight! / 2
    })
    const join = byId.get('join')!
    const joinCenter = join.position!.y + join.data!.nodeHeight! / 2

    expect(joinCenter).toBe((parentCenters[0] + parentCenters[1]) / 2)
  })

  it('inserts a new node into the nearest legal gap without moving anchors', () => {
    const nodes = [
      node('origin', 'origin', '2026-01-01T00:00:00Z'),
      node('child:a', 'fact', '2026-01-01T00:00:01Z'),
      node('child:b', 'fact', '2026-01-01T00:00:02Z'),
    ]
    const edges: GraphEdge[] = [
      { id: 'a', source: 'origin', target: 'child:a', type: 'intent' },
      { id: 'b', source: 'origin', target: 'child:b', type: 'intent' },
    ]
    const first = layoutSemanticTimeline(nodes, edges)
    first.placements['node:child:b'] = {
      ...first.placements['node:child:b'],
      slot: 500,
      y: 500,
    }
    const added = node('child:new', 'fact', '2026-01-01T00:00:03Z')
    const second = layoutSemanticTimeline(
      [...nodes, added],
      [...edges, { id: 'new', source: 'origin', target: 'child:new', type: 'intent' }],
      { previousPlacements: first.placements },
    )

    expect(second.placements['node:child:a']).toEqual(first.placements['node:child:a'])
    expect(second.placements['node:child:b']).toEqual(first.placements['node:child:b'])
    expect(second.placements['node:child:new'].y).toBeGreaterThan(
      second.placements['node:child:a'].y,
    )
    expect(second.placements['node:child:new'].y).toBeLessThan(second.placements['node:child:b'].y)
  })

  it('repairs only the overlapping suffix after a measured height grows', () => {
    const nodes = [
      node('origin', 'origin', '2026-01-01T00:00:00Z'),
      node('child:a', 'fact', '2026-01-01T00:00:01Z'),
      node('child:b', 'fact', '2026-01-01T00:00:02Z'),
    ]
    const edges: GraphEdge[] = [
      { id: 'a', source: 'origin', target: 'child:a', type: 'intent' },
      { id: 'b', source: 'origin', target: 'child:b', type: 'intent' },
    ]
    const first = layoutSemanticTimeline(nodes, edges)
    const second = layoutSemanticTimeline(nodes, edges, {
      previousPlacements: first.placements,
      measuredNodeSizes: { 'child:a': { width: 240, height: 220 } },
    })
    const third = layoutSemanticTimeline(nodes, edges, {
      previousPlacements: second.placements,
      measuredNodeSizes: { 'child:a': { width: 240, height: 220 } },
    })

    expect(second.placements['node:child:a']).toEqual(first.placements['node:child:a'])
    expect(second.placements['node:child:b'].y).toBe(second.placements['node:child:a'].y + 220 + 36)
    expect(third.placements).toEqual(second.placements)
  })

  it('lays out a dense active graph with finite stable non-overlapping columns', () => {
    const nodes = Array.from({ length: 100 }, (_, index) =>
      node(
        `fact:${index}`,
        index === 0 ? 'origin' : 'fact',
        `2026-01-01T00:${String(Math.floor(index / 60)).padStart(2, '0')}:${String(index % 60).padStart(2, '0')}Z`,
      ),
    )
    const edges: GraphEdge[] = []
    for (let index = 1; index < nodes.length; index += 1) {
      edges.push({
        id: `chain:${index}`,
        source: `fact:${Math.max(0, index - 10)}`,
        target: `fact:${index}`,
        type: 'intent',
      })
      if (index >= 20) {
        edges.push({
          id: `join:${index}`,
          source: `fact:${index - 20}`,
          target: `fact:${index}`,
          type: 'intent',
        })
      }
    }

    const first = layoutSemanticTimeline(nodes, edges)
    const reversed = layoutSemanticTimeline([...nodes].reverse(), [...edges].reverse())
    const second = layoutSemanticTimeline(nodes, edges, { previousPlacements: first.placements })
    const positions = Object.fromEntries(first.nodes.map((item) => [item.id, item.position]))

    expect(
      first.nodes.every(
        (item) => Number.isFinite(item.position?.x) && Number.isFinite(item.position?.y),
      ),
    ).toBe(true)
    expect(Object.fromEntries(reversed.nodes.map((item) => [item.id, item.position]))).toEqual(
      positions,
    )
    expect(Object.fromEntries(second.nodes.map((item) => [item.id, item.position]))).toEqual(
      positions,
    )

    const byRank = new Map<number, GraphNode[]>()
    first.nodes.forEach((item) => {
      const rank = first.placements[getNodeLayoutKey(item)].rank
      if (!byRank.has(rank)) byRank.set(rank, [])
      byRank.get(rank)!.push(item)
    })
    byRank.forEach((rankNodes) => {
      const ordered = [...rankNodes].sort((left, right) => left.position!.y - right.position!.y)
      for (let index = 1; index < ordered.length; index += 1) {
        const previous = ordered[index - 1]
        expect(ordered[index].position!.y).toBeGreaterThanOrEqual(
          previous.position!.y + previous.data!.nodeHeight! + 36,
        )
      }
    })
  })

  it('produces identical initial coordinates for reversed input arrays', () => {
    const forward = layoutSemanticTimeline(baseNodes, baseEdges)
    const reversed = layoutSemanticTimeline([...baseNodes].reverse(), [...baseEdges].reverse())
    const positions = Object.fromEntries(forward.nodes.map((item) => [item.id, item.position]))
    const reversedPositions = Object.fromEntries(
      reversed.nodes.map((item) => [item.id, item.position]),
    )

    expect(reversedPositions).toEqual(positions)
  })
})
