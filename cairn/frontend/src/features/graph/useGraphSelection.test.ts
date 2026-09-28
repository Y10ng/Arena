import { computed, ref } from 'vue'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const { apiMock } = vi.hoisted(() => ({ apiMock: vi.fn() }))
vi.mock('@/api/client', () => ({ api: apiMock }))

import { useGraphSelection } from './useGraphSelection'
import type { GraphEdge, GraphNode } from './graphTypes'

const factNode = { id: 'f001', data: { kind: 'fact' } } as GraphNode

describe('useGraphSelection', () => {
  beforeEach(() => {
    apiMock.mockReset()
    apiMock.mockResolvedValue([{ artifact_id: 'a001' }])
  })

  it('does not request Fact evidence for Reason nodes', async () => {
    const reasonNode = { id: 'reason:r001', data: { kind: 'reason' } } as GraphNode
    const selection = useGraphSelection({
      currentProjectId: computed(() => 'p1'),
      laidOutNodes: computed(() => [reasonNode]),
      laidOutEdges: computed(() => []),
      suppressNodeClickId: ref(null),
    })

    await selection.openNode(reasonNode)

    expect(apiMock).not.toHaveBeenCalled()
    expect(selection.selectedNodeId.value).toBe('reason:r001')
    expect(selection.evidenceList.value).toEqual([])
  })

  it('keeps a selected Intent resolved after narrative projection replaces it', () => {
    const directEdge: GraphEdge = {
      id: 'intent:i1',
      source: 'f1',
      target: 'pending:i1',
      type: 'intent',
    }
    const narrativeEdge: GraphEdge = {
      id: 'reason-output:r1:i1',
      source: 'reason:r1',
      target: 'pending:i1',
      type: 'reason-audit',
      data: {
        auditDirection: 'output',
        presentation: {
          role: 'reason-output-intent',
          reasonRunId: 'r1',
          representedIntentEdgeIds: ['intent:i1'],
        },
      },
    }
    const edges = ref<GraphEdge[]>([directEdge])
    const selection = useGraphSelection({
      currentProjectId: computed(() => 'p1'),
      laidOutNodes: computed(() => []),
      laidOutEdges: computed(() => edges.value),
      suppressNodeClickId: ref(null),
    })

    selection.selectEdge(directEdge)
    edges.value = [narrativeEdge]

    expect(selection.selectedEdgeId.value).toBe('intent:i1')
    expect(selection.selectedEdge.value).toStrictEqual(narrativeEdge)
    expect(selection.showEdgeDrawer.value).toBe(true)

    selection.selectEdge(narrativeEdge)

    expect(selection.selectedEdgeId.value).toBeNull()
    expect(selection.showEdgeDrawer.value).toBe(false)
  })

  it('keeps a projected selection resolved when filtering restores the canonical Intent', () => {
    const directEdge: GraphEdge = {
      id: 'intent:i1',
      source: 'f1',
      target: 'pending:i1',
      type: 'intent',
    }
    const narrativeEdge: GraphEdge = {
      id: 'reason-output:r1:i1',
      source: 'reason:r1',
      target: 'pending:i1',
      type: 'reason-audit',
      data: {
        auditDirection: 'output',
        presentation: {
          role: 'reason-output-intent',
          reasonRunId: 'r1',
          representedIntentEdgeIds: ['intent:i1'],
        },
      },
    }
    const edges = ref<GraphEdge[]>([narrativeEdge])
    const selection = useGraphSelection({
      currentProjectId: computed(() => 'p1'),
      laidOutNodes: computed(() => []),
      laidOutEdges: computed(() => edges.value),
      suppressNodeClickId: ref(null),
    })

    selection.selectEdge(narrativeEdge)
    expect(selection.selectedEdgeId.value).toBe('intent:i1')
    expect(selection.selectedEdge.value).toStrictEqual(narrativeEdge)

    edges.value = [directEdge]
    expect(selection.selectedEdge.value).toStrictEqual(directEdge)
    expect(selection.showEdgeDrawer.value).toBe(true)

    edges.value = [narrativeEdge]
    expect(selection.selectedEdge.value).toStrictEqual(narrativeEdge)
    expect(selection.showEdgeDrawer.value).toBe(true)
  })

  it('opens a route node idempotently and keeps click toggle behavior', async () => {
    const selection = useGraphSelection({
      currentProjectId: computed(() => 'p1'),
      laidOutNodes: computed(() => [factNode]),
      laidOutEdges: computed(() => []),
      suppressNodeClickId: ref(null),
    })

    await selection.openNode(factNode)
    await selection.openNode(factNode)
    expect(selection.selectedNodeId.value).toBe('f001')
    expect(selection.showNodeDrawer.value).toBe(true)
    expect(selection.evidenceList.value).toEqual([{ artifact_id: 'a001' }])

    await selection.selectNode(factNode)
    expect(selection.selectedNodeId.value).toBeNull()
    expect(selection.showNodeDrawer.value).toBe(false)
  })
})
