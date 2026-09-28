import { resolveGraphNodeSize } from './nodeSizing'
import type { GraphEdge, GraphNode, Position } from './graphTypes'

export interface TimelinePlacement extends Position {
  layoutKey: string
  nodeId: string
  rank: number
  slot: number
  groupKey: string
  order: number
}

export interface SemanticTimelineLayoutOptions {
  measuredNodeSizes?: Record<string, { width: number; height: number }> | null
  previousPlacements?: Record<string, TimelinePlacement> | null
}

export interface SemanticTimelineLayoutResult {
  nodes: GraphNode[]
  placements: Record<string, TimelinePlacement>
}

const CANVAS_OFFSET_X = 80
const CANVAS_OFFSET_Y = 80
const COLUMN_PITCH = 420
const NODE_GAP = 36
const LARGE_GRAPH_NODE_COUNT = 200
const LARGE_GRAPH_EDGE_COUNT = 600

interface LayoutSize {
  width: number
  height: number
  descriptionLines: number
}

interface LayoutContext {
  nodes: GraphNode[]
  edges: GraphEdge[]
  ranks: Map<string, number>
  nodeById: Map<string, GraphNode>
  sizeByNodeId: Map<string, LayoutSize>
  nodesByRank: Map<number, GraphNode[]>
  incomingByNodeId: Map<string, string[]>
  outgoingByNodeId: Map<string, string[]>
  producerReasonByNodeId: Map<string, string>
  outputOrderByNodeId: Map<string, number>
}

interface OrderedBlock {
  key: string
  nodes: GraphNode[]
  seedOrder: number
}

interface OccupiedRow {
  key: string
  start: number
  end: number
}

export function layoutSemanticTimeline(
  nodes: GraphNode[] = [],
  edges: GraphEdge[] = [],
  options: SemanticTimelineLayoutOptions = {},
): SemanticTimelineLayoutResult {
  const sortedNodes = [...nodes].sort(compareNodes)
  const ranks = buildSemanticRanks(sortedNodes, edges)
  const context = buildLayoutContext(sortedNodes, edges, ranks, options.measuredNodeSizes)
  const previousPlacements = options.previousPlacements || {}
  const placements = retainStablePlacements(context, previousPlacements)
  repairRetainedCollisions(context, placements)

  const orderedByRank = buildOrderedRanks(context)
  if (Object.keys(placements).length === 0) {
    placeInitialRanks(context, orderedByRank, placements)
  } else {
    placeIncrementalRanks(context, orderedByRank, placements, previousPlacements)
  }

  const laidOutNodes = nodes.map((node) => {
    const layoutKey = getNodeLayoutKey(node)
    const placement = placements[layoutKey]
    const size = context.sizeByNodeId.get(node.id)!
    if (!placement) return node
    return {
      ...node,
      data: {
        ...node.data,
        layoutKey,
        nodeWidth: size.width,
        nodeHeight: size.height,
        descriptionLines: size.descriptionLines,
      },
      position: { x: placement.x, y: placement.y },
    }
  })

  return { nodes: laidOutNodes, placements }
}

export function getNodeLayoutKey(node: GraphNode): string {
  const explicit = String(node.layoutKey || node.data?.layoutKey || '').trim()
  if (explicit) return explicit
  if (node.data?.kind === 'reason') {
    return `reason:${node.data?.reason?.runId || node.data?.taskRunId || node.id.replace(/^reason:/, '')}`
  }
  const intentId = node.data?.intent?.id
  if (intentId) return `intent:${intentId}`
  return `node:${node.id}`
}

function buildSemanticRanks(nodes: GraphNode[], edges: GraphEdge[]): Map<string, number> {
  const ranks = new Map<string, number>()
  const causalNodeIds = new Set(
    nodes.filter((node) => node.data?.kind !== 'reason').map((node) => node.id),
  )
  const reasonNodes = nodes.filter((node) => node.data?.kind === 'reason').sort(compareReasonNodes)
  causalNodeIds.forEach((nodeId) => ranks.set(nodeId, 0))

  const intentEdges = edges
    .filter(
      (edge) =>
        edge.type === 'intent' && causalNodeIds.has(edge.source) && causalNodeIds.has(edge.target),
    )
    .sort(compareEdges)
  relaxIntentRanks(ranks, intentEdges, Math.max(1, causalNodeIds.size))

  const reasonOutputEdges = edges
    .filter(
      (edge) =>
        edge.type === 'reason-audit' &&
        edge.data?.auditDirection === 'output' &&
        causalNodeIds.has(edge.target),
    )
    .sort(compareEdges)
  const producerIdsByTarget = new Map<string, Set<string>>()
  reasonOutputEdges.forEach((edge) => {
    if (!producerIdsByTarget.has(edge.target)) producerIdsByTarget.set(edge.target, new Set())
    producerIdsByTarget.get(edge.target)!.add(edge.source)
  })
  const boundOutputEdges = reasonOutputEdges.filter(
    (edge) => producerIdsByTarget.get(edge.target)?.size === 1,
  )
  const chronologicalWaveByReasonId = buildChronologicalWaves(reasonNodes)
  const reasonNodesByWave = new Map<number, GraphNode[]>()
  reasonNodes.forEach((reason) => {
    const wave = chronologicalWaveByReasonId.get(reason.id) || 0
    if (!reasonNodesByWave.has(wave)) reasonNodesByWave.set(wave, [])
    reasonNodesByWave.get(wave)!.push(reason)
  })

  const passLimit = Math.max(1, nodes.length + edges.length)
  for (let pass = 0; pass < passLimit; pass += 1) {
    let changed = relaxIntentRanks(ranks, intentEdges, 1)
    let previousWaveRank = -1

    ;[...reasonNodesByWave.entries()]
      .sort(([left], [right]) => left - right)
      .forEach(([wave, waveReasons]) => {
        const requirements = waveReasons.flatMap((reason) => {
          const inputRanks = edges
            .filter(
              (edge) =>
                edge.type === 'reason-audit' &&
                edge.data?.auditDirection === 'input' &&
                edge.target === reason.id,
            )
            .map((edge) => ranks.get(edge.source))
            .filter((rank): rank is number => Number.isFinite(rank))
          const outputRanks = boundOutputEdges
            .filter((edge) => edge.source === reason.id)
            .map((edge) => ranks.get(edge.target))
            .filter((rank): rank is number => Number.isFinite(rank))
          return [
            ranks.get(reason.id) || 0,
            inputRanks.length > 0 ? Math.max(...inputRanks) + 1 : 1,
            outputRanks.length > 0 ? Math.max(...outputRanks) - 1 : 1,
          ]
        })
        const waveRank = toOddRank(Math.max(1 + 2 * wave, previousWaveRank + 2, ...requirements))
        previousWaveRank = waveRank

        waveReasons.forEach((reason) => {
          if (ranks.get(reason.id) !== waveRank) {
            ranks.set(reason.id, waveRank)
            changed = true
          }
          const outputRank = waveRank + 1
          boundOutputEdges
            .filter((edge) => edge.source === reason.id)
            .forEach((edge) => {
              if (outputRank > (ranks.get(edge.target) || 0)) {
                ranks.set(edge.target, outputRank)
                changed = true
              }
            })
        })
      })

    if (!changed) break
  }

  const goalNodes = nodes.filter((node) => node.data?.kind === 'goal')
  goalNodes.forEach((goal) => {
    const hasInboundIntent = intentEdges.some((edge) => edge.target === goal.id)
    const hasReasonProducer = boundOutputEdges.some((edge) => edge.target === goal.id)
    const hasOutboundDependency = edges.some(
      (edge) =>
        edge.source === goal.id &&
        (edge.type === 'intent' ||
          (edge.type === 'reason-audit' && edge.data?.auditDirection === 'input')),
    )
    if (hasInboundIntent || hasReasonProducer || hasOutboundDependency) return
    const maxNonGoalRank = Math.max(
      0,
      ...nodes.filter((node) => node.id !== goal.id).map((node) => ranks.get(node.id) || 0),
    )
    ranks.set(goal.id, toEvenRank(maxNonGoalRank + 1))
  })

  return ranks
}

function relaxIntentRanks(
  ranks: Map<string, number>,
  intentEdges: GraphEdge[],
  passLimit: number,
): boolean {
  let anyChanged = false
  for (let pass = 0; pass < passLimit; pass += 1) {
    let changed = false
    intentEdges.forEach((edge) => {
      const candidate = (ranks.get(edge.source) || 0) + 2
      if (candidate > (ranks.get(edge.target) || 0)) {
        ranks.set(edge.target, candidate)
        changed = true
        anyChanged = true
      }
    })
    if (!changed) break
  }
  return anyChanged
}

function buildChronologicalWaves(reasonNodes: GraphNode[]): Map<string, number> {
  const waves = new Map<string, number>()
  let currentWave = -1
  let currentWaveEnd = Number.NEGATIVE_INFINITY

  reasonNodes.forEach((reason) => {
    const start = reasonStart(reason)
    const end = reasonEnd(reason, start)
    if (currentWave < 0) {
      currentWave = 0
    } else if (
      Number.isFinite(start) &&
      Number.isFinite(currentWaveEnd) &&
      start > currentWaveEnd
    ) {
      currentWave += 1
    }
    waves.set(reason.id, currentWave)
    currentWaveEnd = Math.max(currentWaveEnd, end)
  })

  return waves
}

function toOddRank(rank: number): number {
  const safe = Math.max(1, Math.floor(rank))
  return safe % 2 === 1 ? safe : safe + 1
}

function toEvenRank(rank: number): number {
  const safe = Math.max(0, Math.ceil(rank))
  return safe % 2 === 0 ? safe : safe + 1
}

function buildLayoutContext(
  nodes: GraphNode[],
  edges: GraphEdge[],
  ranks: Map<string, number>,
  measuredNodeSizes?: Record<string, { width: number; height: number }> | null,
): LayoutContext {
  const nodeById = new Map(nodes.map((node) => [node.id, node]))
  const sizeByNodeId = new Map(
    nodes.map((node) => [node.id, resolveGraphNodeSize(node, measuredNodeSizes?.[node.id])]),
  )
  const nodesByRank = new Map<number, GraphNode[]>()
  nodes.forEach((node) => {
    const rank = ranks.get(node.id) || 0
    if (!nodesByRank.has(rank)) nodesByRank.set(rank, [])
    nodesByRank.get(rank)!.push(node)
  })

  const incomingByNodeId = new Map<string, Set<string>>()
  const outgoingByNodeId = new Map<string, Set<string>>()
  const producersByNodeId = new Map<string, Set<string>>()
  const outputOrderByNodeId = new Map<string, number>()
  edges
    .filter(isLayoutEdge)
    .sort(compareEdges)
    .forEach((edge) => {
      if (!nodeById.has(edge.source) || !nodeById.has(edge.target)) return
      addToSetMap(incomingByNodeId, edge.target, edge.source)
      addToSetMap(outgoingByNodeId, edge.source, edge.target)
      if (edge.type === 'reason-audit' && edge.data?.auditDirection === 'output') {
        addToSetMap(producersByNodeId, edge.target, edge.source)
        const order = Number(edge.data?.outputOrder)
        if (Number.isFinite(order)) {
          outputOrderByNodeId.set(
            edge.target,
            Math.min(outputOrderByNodeId.get(edge.target) ?? Number.MAX_SAFE_INTEGER, order),
          )
        }
      }
    })

  const producerReasonByNodeId = new Map<string, string>()
  producersByNodeId.forEach((producers, nodeId) => {
    if (producers.size === 1) producerReasonByNodeId.set(nodeId, [...producers][0])
  })

  return {
    nodes,
    edges,
    ranks,
    nodeById,
    sizeByNodeId,
    nodesByRank,
    incomingByNodeId: toSortedArrayMap(incomingByNodeId),
    outgoingByNodeId: toSortedArrayMap(outgoingByNodeId),
    producerReasonByNodeId,
    outputOrderByNodeId,
  }
}

function isLayoutEdge(edge: GraphEdge): boolean {
  return (
    edge.type === 'intent' ||
    (edge.type === 'reason-audit' &&
      (edge.data?.auditDirection === 'input' || edge.data?.auditDirection === 'output'))
  )
}

function addToSetMap(map: Map<string, Set<string>>, key: string, value: string) {
  if (!map.has(key)) map.set(key, new Set())
  map.get(key)!.add(value)
}

function toSortedArrayMap(map: Map<string, Set<string>>): Map<string, string[]> {
  return new Map([...map.entries()].map(([key, values]) => [key, [...values].sort(compareStrings)]))
}

function retainStablePlacements(
  context: LayoutContext,
  previousPlacements: Record<string, TimelinePlacement>,
): Record<string, TimelinePlacement> {
  return Object.fromEntries(
    context.nodes.flatMap((node) => {
      const layoutKey = getNodeLayoutKey(node)
      const previous = previousPlacements[layoutKey]
      const rank = context.ranks.get(node.id)
      return previous && previous.rank === rank ? [[layoutKey, { ...previous }]] : []
    }),
  ) as Record<string, TimelinePlacement>
}

function repairRetainedCollisions(
  context: LayoutContext,
  placements: Record<string, TimelinePlacement>,
) {
  const retainedByRank = new Map<number, Array<{ node: GraphNode; placement: TimelinePlacement }>>()
  context.nodes.forEach((node) => {
    const placement = placements[getNodeLayoutKey(node)]
    if (!placement) return
    if (!retainedByRank.has(placement.rank)) retainedByRank.set(placement.rank, [])
    retainedByRank.get(placement.rank)!.push({ node, placement })
  })

  retainedByRank.forEach((entries) => {
    entries.sort((left, right) => {
      const yDiff = left.placement.y - right.placement.y
      return yDiff || compareStrings(left.placement.layoutKey, right.placement.layoutKey)
    })
    let nextY = CANVAS_OFFSET_Y
    entries.forEach(({ node, placement }) => {
      const y = Math.max(placement.y, nextY)
      placement.y = y
      placement.slot = y
      nextY = y + context.sizeByNodeId.get(node.id)!.height + NODE_GAP
    })
  })
}

function buildOrderedRanks(context: LayoutContext): Map<number, GraphNode[]> {
  const blocksByRank = new Map<number, OrderedBlock[]>()
  context.nodesByRank.forEach((nodes, rank) => {
    const blockByKey = new Map<string, OrderedBlock>()
    ;[...nodes]
      .sort((left, right) => compareNodesWithinRank(left, right, context))
      .forEach((node, seedOrder) => {
        const producer = context.producerReasonByNodeId.get(node.id)
        const key = producer ? `producer:${producer}` : `node:${getNodeLayoutKey(node)}`
        if (!blockByKey.has(key)) blockByKey.set(key, { key, nodes: [], seedOrder })
        blockByKey.get(key)!.nodes.push(node)
      })
    blockByKey.forEach((block) => {
      block.nodes.sort((left, right) => compareNodesWithinRank(left, right, context))
    })
    blocksByRank.set(
      rank,
      [...blockByKey.values()].sort(
        (left, right) => left.seedOrder - right.seedOrder || compareStrings(left.key, right.key),
      ),
    )
  })

  const ranks = [...blocksByRank.keys()].sort((left, right) => left - right)
  const sweepCount =
    context.nodes.length > LARGE_GRAPH_NODE_COUNT || context.edges.length > LARGE_GRAPH_EDGE_COUNT
      ? 2
      : 4
  for (let sweep = 0; sweep < sweepCount; sweep += 1) {
    const forward = sweep % 2 === 0
    const traversal = forward ? ranks : [...ranks].reverse()
    const ordinalByNodeId = buildNodeOrdinals(blocksByRank, ranks)
    traversal.forEach((rank) => {
      const blocks = blocksByRank.get(rank) || []
      const scored = blocks.map((block, currentOrder) => ({
        block,
        currentOrder,
        score: blockNeighborMedian(block, context, ordinalByNodeId, forward),
      }))
      scored.sort((left, right) => {
        const leftHasScore = Number.isFinite(left.score)
        const rightHasScore = Number.isFinite(right.score)
        if (leftHasScore && rightHasScore && left.score !== right.score) {
          return left.score - right.score
        }
        if (leftHasScore !== rightHasScore) return leftHasScore ? -1 : 1
        return (
          left.currentOrder - right.currentOrder || compareStrings(left.block.key, right.block.key)
        )
      })
      blocksByRank.set(
        rank,
        scored.map(({ block }) => block),
      )
    })
  }

  return new Map(
    ranks.map((rank) => [rank, (blocksByRank.get(rank) || []).flatMap((block) => block.nodes)]),
  )
}

function buildNodeOrdinals(
  blocksByRank: Map<number, OrderedBlock[]>,
  ranks: number[],
): Map<string, number> {
  const ordinals = new Map<string, number>()
  ranks.forEach((rank) => {
    let ordinal = 0
    ;(blocksByRank.get(rank) || []).forEach((block) => {
      block.nodes.forEach((node) => {
        ordinals.set(node.id, ordinal)
        ordinal += 1
      })
    })
  })
  return ordinals
}

function blockNeighborMedian(
  block: OrderedBlock,
  context: LayoutContext,
  ordinalByNodeId: Map<string, number>,
  forward: boolean,
): number {
  const values = block.nodes.flatMap((node) => {
    const neighbors = forward
      ? context.incomingByNodeId.get(node.id) || []
      : context.outgoingByNodeId.get(node.id) || []
    const rank = context.ranks.get(node.id) || 0
    return neighbors
      .filter((neighborId) => {
        const neighborRank = context.ranks.get(neighborId)
        return neighborRank !== undefined && (forward ? neighborRank < rank : neighborRank > rank)
      })
      .map((neighborId) => ordinalByNodeId.get(neighborId))
      .filter((value): value is number => Number.isFinite(value))
  })
  return median(values)
}

function placeInitialRanks(
  context: LayoutContext,
  orderedByRank: Map<number, GraphNode[]>,
  placements: Record<string, TimelinePlacement>,
) {
  ;[...orderedByRank.keys()]
    .sort((left, right) => left - right)
    .forEach((rank) => {
      const orderedNodes = orderedByRank.get(rank) || []
      const preferredTopByNodeId = buildPreferredTops(context, orderedNodes, placements)
      const yByNodeId = packInitialColumn(context, orderedNodes, preferredTopByNodeId)
      orderedNodes.forEach((node, order) => {
        const y = yByNodeId.get(node.id) ?? CANVAS_OFFSET_Y
        placements[getNodeLayoutKey(node)] = createPlacement(context, node, rank, y, order)
      })
    })
}

function placeIncrementalRanks(
  context: LayoutContext,
  orderedByRank: Map<number, GraphNode[]>,
  placements: Record<string, TimelinePlacement>,
  previousPlacements: Record<string, TimelinePlacement>,
) {
  ;[...orderedByRank.keys()]
    .sort((left, right) => left - right)
    .forEach((rank) => {
      const orderedNodes = orderedByRank.get(rank) || []
      const occupied = buildOccupiedRows(context, orderedNodes, placements)
      orderedNodes.forEach((node, order) => {
        const layoutKey = getNodeLayoutKey(node)
        if (placements[layoutKey]) return
        const size = context.sizeByNodeId.get(node.id)!
        const historicalY = previousPlacements[layoutKey]?.y
        const preferredY = Number.isFinite(historicalY)
          ? historicalY
          : preferredTop(context, node, placements)
        const y = findBestAvailableY(occupied, preferredY, size.height)
        placements[layoutKey] = createPlacement(context, node, rank, y, order)
        insertOccupiedRow(occupied, { key: layoutKey, start: y, end: y + size.height })
      })
    })
}

function buildPreferredTops(
  context: LayoutContext,
  orderedNodes: GraphNode[],
  placements: Record<string, TimelinePlacement>,
): Map<string, number> {
  const preferred = new Map<string, number>()
  const producerGroups = new Map<string, GraphNode[]>()
  orderedNodes.forEach((node) => {
    const producer = context.producerReasonByNodeId.get(node.id)
    if (!producer) return
    if (!producerGroups.has(producer)) producerGroups.set(producer, [])
    producerGroups.get(producer)!.push(node)
  })

  producerGroups.forEach((group, producerId) => {
    const producer = context.nodeById.get(producerId)
    const producerPlacement = producer ? placements[getNodeLayoutKey(producer)] : undefined
    if (!producer || !producerPlacement) return
    const totalHeight =
      group.reduce((sum, node) => sum + context.sizeByNodeId.get(node.id)!.height, 0) +
      NODE_GAP * Math.max(0, group.length - 1)
    const producerSize = context.sizeByNodeId.get(producer.id)!
    let y = producerPlacement.y + producerSize.height / 2 - totalHeight / 2
    group.forEach((node) => {
      preferred.set(node.id, y)
      y += context.sizeByNodeId.get(node.id)!.height + NODE_GAP
    })
  })

  orderedNodes.forEach((node) => {
    if (!preferred.has(node.id)) preferred.set(node.id, preferredTop(context, node, placements))
  })
  return preferred
}

function preferredTop(
  context: LayoutContext,
  node: GraphNode,
  placements: Record<string, TimelinePlacement>,
): number {
  const size = context.sizeByNodeId.get(node.id)!
  const producerId = context.producerReasonByNodeId.get(node.id)
  const neighborIds = producerId ? [producerId] : context.incomingByNodeId.get(node.id) || []
  const centers = neighborIds
    .map((neighborId) => {
      const neighbor = context.nodeById.get(neighborId)
      const placement = neighbor ? placements[getNodeLayoutKey(neighbor)] : undefined
      const neighborSize = neighbor ? context.sizeByNodeId.get(neighbor.id) : undefined
      return placement && neighborSize ? placement.y + neighborSize.height / 2 : Number.NaN
    })
    .filter(Number.isFinite)
  return centers.length > 0 ? mean(centers) - size.height / 2 : CANVAS_OFFSET_Y
}

function packInitialColumn(
  context: LayoutContext,
  orderedNodes: GraphNode[],
  preferredTopByNodeId: Map<string, number>,
): Map<string, number> {
  const positions = new Map<string, number>()
  let nextY = CANVAS_OFFSET_Y
  orderedNodes.forEach((node) => {
    const desired = preferredTopByNodeId.get(node.id) ?? CANVAS_OFFSET_Y
    const y = Math.max(desired, nextY)
    positions.set(node.id, y)
    nextY = y + context.sizeByNodeId.get(node.id)!.height + NODE_GAP
  })

  for (let index = orderedNodes.length - 2; index >= 0; index -= 1) {
    const node = orderedNodes[index]
    const nextNode = orderedNodes[index + 1]
    const maxY = positions.get(nextNode.id)! - context.sizeByNodeId.get(node.id)!.height - NODE_GAP
    positions.set(node.id, Math.min(positions.get(node.id)!, maxY))
  }

  const minY = Math.min(CANVAS_OFFSET_Y, ...positions.values())
  if (minY < CANVAS_OFFSET_Y) {
    const offset = CANVAS_OFFSET_Y - minY
    positions.forEach((y, nodeId) => positions.set(nodeId, y + offset))
  }
  return positions
}

function buildOccupiedRows(
  context: LayoutContext,
  orderedNodes: GraphNode[],
  placements: Record<string, TimelinePlacement>,
): OccupiedRow[] {
  return orderedNodes
    .flatMap((node) => {
      const layoutKey = getNodeLayoutKey(node)
      const placement = placements[layoutKey]
      if (!placement) return []
      return [
        {
          key: layoutKey,
          start: placement.y,
          end: placement.y + context.sizeByNodeId.get(node.id)!.height,
        },
      ]
    })
    .sort((left, right) => left.start - right.start || compareStrings(left.key, right.key))
}

function findBestAvailableY(rows: OccupiedRow[], preferredY: number, height: number): number {
  const candidates = [CANVAS_OFFSET_Y, ...rows.map((row) => row.end + NODE_GAP)]
  const legal = candidates.filter((candidate) =>
    rows.every(
      (row) => candidate + height + NODE_GAP <= row.start || candidate >= row.end + NODE_GAP,
    ),
  )
  const preferredCenter = preferredY + height / 2
  return (
    legal.sort((left, right) => {
      const distance =
        Math.abs(left + height / 2 - preferredCenter) -
        Math.abs(right + height / 2 - preferredCenter)
      return distance || left - right
    })[0] ?? CANVAS_OFFSET_Y
  )
}

function insertOccupiedRow(rows: OccupiedRow[], row: OccupiedRow) {
  rows.push(row)
  rows.sort((left, right) => left.start - right.start || compareStrings(left.key, right.key))
}

function createPlacement(
  context: LayoutContext,
  node: GraphNode,
  rank: number,
  y: number,
  order: number,
): TimelinePlacement {
  const layoutKey = getNodeLayoutKey(node)
  const producer = context.producerReasonByNodeId.get(node.id)
  return {
    layoutKey,
    nodeId: node.id,
    rank,
    slot: y,
    groupKey: node.data?.kind === 'reason' ? layoutKey : producer || `rank:${rank}`,
    order,
    x: CANVAS_OFFSET_X + rank * COLUMN_PITCH,
    y,
  }
}

function compareNodesWithinRank(left: GraphNode, right: GraphNode, context: LayoutContext): number {
  const leftProducer = context.producerReasonByNodeId.get(left.id) || ''
  const rightProducer = context.producerReasonByNodeId.get(right.id) || ''
  const producerDiff = compareStrings(leftProducer, rightProducer)
  if (producerDiff !== 0) return producerDiff
  const leftOrder = context.outputOrderByNodeId.get(left.id) ?? Number.MAX_SAFE_INTEGER
  const rightOrder = context.outputOrderByNodeId.get(right.id) ?? Number.MAX_SAFE_INTEGER
  if (leftOrder !== rightOrder) return leftOrder - rightOrder
  return compareNodes(left, right)
}

function median(values: number[]): number {
  if (values.length === 0) return Number.NaN
  const sorted = [...values].sort((left, right) => left - right)
  const middle = Math.floor(sorted.length / 2)
  return sorted.length % 2 === 0 ? (sorted[middle - 1] + sorted[middle]) / 2 : sorted[middle]
}

function mean(values: number[]): number {
  return values.reduce((sum, value) => sum + value, 0) / values.length
}

function compareReasonNodes(left: GraphNode, right: GraphNode): number {
  const startDiff = reasonStart(left) - reasonStart(right)
  if (Number.isFinite(startDiff) && startDiff !== 0) return startDiff
  const endDiff = reasonEnd(left, reasonStart(left)) - reasonEnd(right, reasonStart(right))
  if (Number.isFinite(endDiff) && endDiff !== 0) return endDiff
  return compareStrings(left.id, right.id)
}

function compareNodes(left: GraphNode, right: GraphNode): number {
  const timeDiff = nodeTime(left) - nodeTime(right)
  if (timeDiff !== 0) return timeDiff
  const kindDiff = nodeKindOrder(left) - nodeKindOrder(right)
  if (kindDiff !== 0) return kindDiff
  return compareStrings(left.id, right.id)
}

function compareEdges(left: GraphEdge, right: GraphEdge): number {
  const leftOrder = Number(left.data?.outputOrder)
  const rightOrder = Number(right.data?.outputOrder)
  if (Number.isFinite(leftOrder) && Number.isFinite(rightOrder) && leftOrder !== rightOrder) {
    return leftOrder - rightOrder
  }
  const sourceDiff = compareStrings(left.source, right.source)
  if (sourceDiff !== 0) return sourceDiff
  const targetDiff = compareStrings(left.target, right.target)
  if (targetDiff !== 0) return targetDiff
  return compareStrings(left.id, right.id)
}

function reasonStart(node: GraphNode): number {
  const value = Date.parse(String(node.data?.reason?.startedAt || node.data?.createdAt || ''))
  return Number.isFinite(value) ? value : Number.MAX_SAFE_INTEGER
}

function reasonEnd(node: GraphNode, start: number): number {
  const value = Date.parse(String(node.data?.reason?.endedAt || ''))
  if (Number.isFinite(value)) return Math.max(start, value)
  return Number.isFinite(start) ? start : Number.MAX_SAFE_INTEGER
}

function nodeTime(node: GraphNode): number {
  if (node.data?.kind === 'origin' || node.id === 'origin') return Number.NEGATIVE_INFINITY
  const candidates = [
    node.data?.createdAt,
    node.data?.fact?.created_at,
    node.data?.intent?.created_at,
    node.data?.reason?.startedAt,
  ]
    .map((value) => Date.parse(String(value || '')))
    .filter(Number.isFinite)
  return candidates.length > 0 ? Math.min(...candidates) : Number.MAX_SAFE_INTEGER
}

function nodeKindOrder(node: GraphNode): number {
  const order: Record<string, number> = {
    origin: 0,
    fact: 1,
    reason: 2,
    pending_group: 3,
    pending: 4,
    goal: 5,
  }
  return order[String(node.data?.kind || '')] ?? 6
}

function compareStrings(left: string, right: string): number {
  return left < right ? -1 : left > right ? 1 : 0
}
