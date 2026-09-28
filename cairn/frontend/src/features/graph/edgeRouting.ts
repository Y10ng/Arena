import type { GraphEdge, GraphNode } from './graphFilters'
import type { IntentNarrativeDescriptor } from './graphTypes'

export type EdgeLayoutDir = 'LR' | 'TB'

export interface EdgeRoute {
  sx: number
  sy: number
  tx: number
  ty: number
  labelX: number
  labelY: number
}

export interface CompactEdgesOptions {
  dir?: EdgeLayoutDir
}

interface NodeRect {
  x: number
  y: number
  width: number
  height: number
  centerX: number
  centerY: number
}

const DEFAULT_NODE_WIDTH = 180
const DEFAULT_NODE_HEIGHT = 80
const PORT_PADDING = 18
const MAX_PORT_STEP = 22

export function projectVisibleNarrativeEdges(
  edges: GraphEdge[] = [],
  nodes: GraphNode[] = [],
): GraphEdge[] {
  const visibleNodeIds = new Set(nodes.map((node) => node.id))
  const reasonRunByNodeId = new Map(
    nodes
      .filter((node) => node.data?.kind === 'reason')
      .map((node) => [node.id, String(node.data?.reason?.runId || node.data?.taskRunId || '')]),
  )
  const intentEdgesById = new Map<string, GraphEdge[]>()
  const reasonInputs = edges.filter(
    (edge) => edge.type === 'reason-audit' && edge.data?.auditDirection === 'input',
  )
  const reasonOutputs = edges.filter(
    (edge) => edge.type === 'reason-audit' && edge.data?.auditDirection === 'output',
  )

  edges.forEach((edge) => {
    if (edge.type !== 'intent') return
    const intentId = getIntentId(edge)
    if (!intentId) return
    if (!intentEdgesById.has(intentId)) intentEdgesById.set(intentId, [])
    intentEdgesById.get(intentId)!.push(edge)
  })

  const suppressedEdgeIds = new Set<string>()
  const narrativesByOutputEdgeId = new Map<
    string,
    { descriptors: IntentNarrativeDescriptor[]; edgeIds: string[] }
  >()

  intentEdgesById.forEach((intentEdges, intentId) => {
    const sortedIntentEdges = [...intentEdges].sort(compareEdgesForRendering)
    if (!isProjectableIntentGroup(sortedIntentEdges)) return

    const expectedSourceIds = getExpectedSourceIds(sortedIntentEdges)
    const visibleSourceIds = [...new Set(sortedIntentEdges.map((edge) => edge.source))].sort()
    if (
      expectedSourceIds.length !== visibleSourceIds.length ||
      expectedSourceIds.some((sourceId, index) => sourceId !== visibleSourceIds[index]) ||
      expectedSourceIds.some((sourceId) => !visibleNodeIds.has(sourceId))
    ) {
      return
    }

    const targetIds = [...new Set(sortedIntentEdges.map((edge) => edge.target))]
    if (targetIds.length !== 1) return
    const targetId = targetIds[0]
    if (!visibleNodeIds.has(targetId)) return
    const candidates = reasonOutputs.filter((edge) => {
      if (edge.target !== targetId || !visibleNodeIds.has(edge.source)) return false
      if (!edgeCarriesIntent(edge, intentId)) return false
      const runId = getReasonRunId(edge, reasonRunByNodeId)
      return sortedIntentEdges.every((intentEdge) =>
        intentAssociatesReason(intentEdge, edge.source, runId),
      )
    })

    const completeCandidates = candidates.filter((outputEdge) =>
      expectedSourceIds.every((sourceId) =>
        reasonInputs.some(
          (inputEdge) => inputEdge.source === sourceId && inputEdge.target === outputEdge.source,
        ),
      ),
    )
    if (completeCandidates.length !== 1) return

    const outputEdge = completeCandidates[0]
    const descriptor = buildNarrativeDescriptor(intentId, sortedIntentEdges)
    const existing = narrativesByOutputEdgeId.get(outputEdge.id) || { descriptors: [], edgeIds: [] }
    existing.descriptors.push(descriptor)
    existing.edgeIds.push(...sortedIntentEdges.map((edge) => edge.id))
    narrativesByOutputEdgeId.set(outputEdge.id, existing)
    sortedIntentEdges.forEach((edge) => suppressedEdgeIds.add(edge.id))
  })

  return edges
    .filter((edge) => !suppressedEdgeIds.has(edge.id))
    .map((edge) => {
      const narrative = narrativesByOutputEdgeId.get(edge.id)
      if (!narrative) return edge
      const narratives = [...narrative.descriptors].sort((left, right) =>
        left.intentId.localeCompare(right.intentId),
      )
      const representedIntentEdgeIds = [...new Set(narrative.edgeIds)].sort()
      return {
        ...edge,
        data: {
          ...edge.data,
          label:
            narratives.length === 1
              ? narratives[0].label
              : `已派发 ${narratives.length} 个 Explore`,
          presentation: {
            role: 'reason-output-intent' as const,
            reasonRunId: getReasonRunId(edge, reasonRunByNodeId),
            representedIntentEdgeIds,
            narratives,
          },
        },
      }
    })
    .sort(compareEdgesForRendering)
}

export function compactGraphEdgesForRendering(
  edges: GraphEdge[] = [],
  nodes: GraphNode[] = [],
  { dir = 'LR' }: CompactEdgesOptions = {},
): GraphEdge[] {
  if (!Array.isArray(edges) || edges.length <= 1) return edges

  const rectById = new Map(nodes.map((node) => [node.id, getNodeRect(node)]))
  const compacted: GraphEdge[] = []
  const joinEdgesByGroupId = new Map<string, GraphEdge[]>()
  const reasonInputsByNodeId = new Map<string, GraphEdge[]>()

  edges.forEach((edge) => {
    if (edge.type === 'reason-audit') {
      if (edge.data?.auditDirection !== 'input') {
        compacted.push(edge)
        return
      }
      const groupId = `${edge.target}:${edge.data?.reasonRunId || ''}`
      if (!reasonInputsByNodeId.has(groupId)) reasonInputsByNodeId.set(groupId, [])
      reasonInputsByNodeId.get(groupId)!.push(edge)
      return
    }

    const joinGroupId = edge.data?.joinGroup?.id
    if (!joinGroupId) {
      compacted.push(edge)
      return
    }

    if (!joinEdgesByGroupId.has(joinGroupId)) {
      joinEdgesByGroupId.set(joinGroupId, [])
    }
    joinEdgesByGroupId.get(joinGroupId)!.push(edge)
  })

  reasonInputsByNodeId.forEach((groupEdges) => {
    if (groupEdges.length <= 1) {
      compacted.push(...groupEdges)
      return
    }
    compacted.push(buildRepresentativeReasonInput(groupEdges, rectById, dir))
  })

  joinEdgesByGroupId.forEach((groupEdges) => {
    compacted.push(buildRepresentativeJoinEdge(groupEdges, rectById, dir))
  })

  return compacted.sort(compareEdgesForRendering)
}

function isProjectableIntentGroup(edges: GraphEdge[]): boolean {
  if (edges.length === 0) return false
  return edges.every(
    (edge) =>
      !edge.data?.pendingGroup &&
      Number(edge.data?.mergedCount || 1) === 1 &&
      Boolean(getIntentId(edge)),
  )
}

function getExpectedSourceIds(edges: GraphEdge[]): string[] {
  const joinSources = edges.flatMap((edge) => {
    const sourceIds = edge.data?.joinGroup?.sourceIds
    return Array.isArray(sourceIds) ? sourceIds.map(String) : []
  })
  return [
    ...new Set(joinSources.length > 0 ? joinSources : edges.map((edge) => edge.source)),
  ].sort()
}

function getIntentId(edge: GraphEdge): string {
  return String(
    edge.data?.intentId || edge.data?.intent?.id || edge.data?.joinGroup?.intentId || '',
  ).trim()
}

function edgeCarriesIntent(edge: GraphEdge, intentId: string): boolean {
  return (
    String(edge.data?.intentId || '') === intentId ||
    (Array.isArray(edge.data?.intentIds) && edge.data!.intentIds!.map(String).includes(intentId))
  )
}

function intentAssociatesReason(
  edge: GraphEdge,
  reasonNodeId: string,
  reasonRunId: string,
): boolean {
  const runIds = [edge.data?.reasonRunId, ...(edge.data?.reasonRunIds || [])]
    .map((value) => String(value || ''))
    .filter(Boolean)
  return (
    edge.data?.reasonNodeId === reasonNodeId || (reasonRunId !== '' && runIds.includes(reasonRunId))
  )
}

function getReasonRunId(edge: GraphEdge, reasonRunByNodeId: Map<string, string>): string {
  return String(
    edge.data?.reasonRunId ||
      reasonRunByNodeId.get(edge.data?.auditDirection === 'output' ? edge.source : edge.target) ||
      '',
  )
}

function buildNarrativeDescriptor(intentId: string, edges: GraphEdge[]): IntentNarrativeDescriptor {
  const representative = edges[0]
  const intent = representative.data?.intent
  return {
    intentId,
    canonicalEdgeIds: edges.map((edge) => edge.id).sort(),
    label: String(representative.data?.label || intent?.description || intentId),
    state: String(representative.data?.state || ''),
    worker: String(representative.data?.worker || intent?.worker || ''),
    creator: String(representative.data?.creator || intent?.creator || ''),
    taskRunId: String(representative.data?.taskRunId || intent?.task_run_id || ''),
    isCriticalPath: edges.some((edge) => edge.data?.isCriticalPath === true),
    intent,
  }
}

function buildRepresentativeReasonInput(
  groupEdges: GraphEdge[],
  rectById: Map<string, NodeRect>,
  dir: EdgeLayoutDir,
): GraphEdge {
  const representative = chooseReasonInputRepresentative(groupEdges, rectById, dir)
  const hiddenEdges = groupEdges
    .filter((edge) => edge.id !== representative.id)
    .sort(compareEdgesForRendering)
  const inputNodeIds = [...new Set(groupEdges.map((edge) => edge.source))].sort()
  return {
    ...representative,
    data: {
      ...representative.data,
      renderedSourceCount: inputNodeIds.length,
      hiddenEdgeIds: hiddenEdges.map((edge) => edge.id),
      presentation: {
        role: 'reason-input-summary' as const,
        reasonRunId: String(representative.data?.reasonRunId || ''),
        inputNodeIds,
        hiddenInputEdgeIds: hiddenEdges.map((edge) => edge.id),
      },
    },
  }
}

function chooseReasonInputRepresentative(
  edges: GraphEdge[],
  rectById: Map<string, NodeRect>,
  dir: EdgeLayoutDir,
): GraphEdge {
  return [...edges].sort((left, right) => {
    const leftRect = rectById.get(left.source)
    const rightRect = rectById.get(right.source)
    const leftProgress = dir === 'TB' ? leftRect?.y || 0 : leftRect?.x || 0
    const rightProgress = dir === 'TB' ? rightRect?.y || 0 : rightRect?.x || 0
    if (leftProgress !== rightProgress) return rightProgress - leftProgress
    const scoreDiff =
      representativeEdgeScore(left, rectById, dir) - representativeEdgeScore(right, rectById, dir)
    if (scoreDiff !== 0) return scoreDiff
    return compareEdgesForRendering(left, right)
  })[0]
}

function buildRepresentativeJoinEdge(
  groupEdges: GraphEdge[],
  rectById: Map<string, NodeRect>,
  dir: EdgeLayoutDir,
): GraphEdge {
  const representative = chooseRepresentativeJoinEdge(groupEdges, rectById, dir)
  const joinGroup = representative.data?.joinGroup || groupEdges[0]?.data?.joinGroup || {}
  const sourceIds = joinGroup.sourceIds || groupEdges.map((edge) => edge.source).sort()
  const hiddenEdges = groupEdges
    .filter((edge) => edge.id !== representative.id)
    .sort(compareEdgesForRendering)

  return {
    ...representative,
    data: {
      ...representative.data,
      joinGroup: {
        ...joinGroup,
        markerEdgeId: representative.id,
        sourceIds,
        sourceCount: sourceIds.length || groupEdges.length,
      },
      renderedSourceCount: sourceIds.length || groupEdges.length,
      hiddenEdgeIds: hiddenEdges.map((edge) => edge.id),
    },
  }
}

function chooseRepresentativeJoinEdge(
  edges: GraphEdge[],
  rectById: Map<string, NodeRect>,
  dir: EdgeLayoutDir,
): GraphEdge {
  if (edges.length <= 1) return edges[0]

  return [...edges].sort((left, right) => {
    const leftScore = representativeEdgeScore(left, rectById, dir)
    const rightScore = representativeEdgeScore(right, rectById, dir)
    if (leftScore !== rightScore) return leftScore - rightScore

    const leftMarker = left.data?.joinGroup?.markerEdgeId === left.id ? 0 : 1
    const rightMarker = right.data?.joinGroup?.markerEdgeId === right.id ? 0 : 1
    if (leftMarker !== rightMarker) return leftMarker - rightMarker

    return String(left.id).localeCompare(String(right.id))
  })[0]
}

function representativeEdgeScore(
  edge: GraphEdge,
  rectById: Map<string, NodeRect>,
  dir: EdgeLayoutDir,
): number {
  const sourceRect = rectById.get(edge.source)
  const targetRect = rectById.get(edge.target)
  if (!sourceRect || !targetRect) return Number.POSITIVE_INFINITY

  if (dir === 'TB') {
    return Math.abs(targetRect.y - (sourceRect.y + sourceRect.height))
  }
  return Math.abs(targetRect.x - (sourceRect.x + sourceRect.width))
}

function compareEdgesForRendering(left: GraphEdge, right: GraphEdge): number {
  const leftIntent = left.data?.intent?.id || left.data?.intentId || ''
  const rightIntent = right.data?.intent?.id || right.data?.intentId || ''
  const intentDiff = String(leftIntent).localeCompare(String(rightIntent))
  if (intentDiff !== 0) return intentDiff
  return String(left.id).localeCompare(String(right.id))
}

export function buildEdgeRoutes(
  nodes: GraphNode[] = [],
  edges: GraphEdge[] = [],
  dir: EdgeLayoutDir = 'LR',
): Record<string, EdgeRoute> {
  const rectById = new Map(nodes.map((node) => [node.id, getNodeRect(node)]))
  const sourcePortOffsets = buildPortOffsets(edges, rectById, dir, 'source')
  const targetPortOffsets = buildPortOffsets(edges, rectById, dir, 'target')
  const routes: Record<string, EdgeRoute> = {}

  edges.forEach((edge) => {
    const sourceRect = rectById.get(edge.source)
    const targetRect = rectById.get(edge.target)
    if (!edge.id || !sourceRect || !targetRect) return

    const sourceOffset = sourcePortOffsets.get(edge.id) || 0
    const targetOffset = targetPortOffsets.get(edge.id) || 0
    const route =
      dir === 'TB'
        ? {
            sx: sourceRect.centerX + sourceOffset,
            sy: sourceRect.y + sourceRect.height,
            tx: targetRect.centerX + targetOffset,
            ty: targetRect.y,
          }
        : {
            sx: sourceRect.x + sourceRect.width,
            sy: sourceRect.centerY + sourceOffset,
            tx: targetRect.x,
            ty: targetRect.centerY + targetOffset,
          }

    routes[edge.id] = {
      ...route,
      labelX: (route.sx + route.tx) / 2,
      labelY: (route.sy + route.ty) / 2 - 8,
    }
  })

  return routes
}

function buildPortOffsets(
  edges: GraphEdge[],
  rectById: Map<string, NodeRect>,
  dir: EdgeLayoutDir,
  side: 'source' | 'target',
): Map<string, number> {
  const edgesByNodeId = new Map<string, GraphEdge[]>()
  edges.forEach((edge) => {
    const nodeId = side === 'source' ? edge.source : edge.target
    if (!rectById.has(nodeId)) return
    if (!edgesByNodeId.has(nodeId)) edgesByNodeId.set(nodeId, [])
    edgesByNodeId.get(nodeId)!.push(edge)
  })

  const offsets = new Map<string, number>()
  edgesByNodeId.forEach((nodeEdges, nodeId) => {
    const nodeRect = rectById.get(nodeId)
    if (!nodeRect) return

    const span = dir === 'TB' ? nodeRect.width : nodeRect.height
    const step = resolvePortStep(span, nodeEdges.length)
    const middle = (nodeEdges.length - 1) / 2

    sortEdgesForPort(nodeEdges, rectById, dir, side).forEach((edge, index) => {
      offsets.set(edge.id, (index - middle) * step)
    })
  })

  return offsets
}

function sortEdgesForPort(
  edges: GraphEdge[],
  rectById: Map<string, NodeRect>,
  dir: EdgeLayoutDir,
  side: 'source' | 'target',
): GraphEdge[] {
  const oppositeKey = side === 'source' ? 'target' : 'source'
  return [...edges].sort((left, right) => {
    const leftRect = rectById.get(left[oppositeKey])
    const rightRect = rectById.get(right[oppositeKey])
    const secondaryDiff = getSecondaryCenter(leftRect, dir) - getSecondaryCenter(rightRect, dir)
    if (secondaryDiff !== 0) return secondaryDiff

    const oppositeDiff = String(left[oppositeKey]).localeCompare(String(right[oppositeKey]))
    if (oppositeDiff !== 0) return oppositeDiff

    return String(left.id).localeCompare(String(right.id))
  })
}

function getNodeRect(node: GraphNode): NodeRect {
  const width = Number(node.data?.nodeWidth) || DEFAULT_NODE_WIDTH
  const height = Number(node.data?.nodeHeight) || DEFAULT_NODE_HEIGHT
  const x = Number(node.position?.x) || 0
  const y = Number(node.position?.y) || 0
  return {
    x,
    y,
    width,
    height,
    centerX: x + width / 2,
    centerY: y + height / 2,
  }
}

function getSecondaryCenter(rect: NodeRect | undefined, dir: EdgeLayoutDir): number {
  if (!rect) return 0
  return dir === 'TB' ? rect.centerX : rect.centerY
}

function resolvePortStep(span: number, edgeCount: number): number {
  if (edgeCount <= 1) return 0
  const usableSpan = Math.max(0, span - PORT_PADDING * 2)
  if (usableSpan <= 0) return 0
  return Math.min(MAX_PORT_STEP, usableSpan / Math.max(1, edgeCount - 1))
}
