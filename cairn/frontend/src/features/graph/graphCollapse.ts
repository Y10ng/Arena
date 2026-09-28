import type { GraphEdge, GraphNode } from './graphTypes'

const AUTO_COLLAPSE_MIN_BRANCHES = 2
const AUTO_COLLAPSE_MIN_COLLAPSED_NODES = 3
const AUTO_COLLAPSE_ROOT_KINDS = new Set(['origin', 'goal'])

function getNonCriticalDescendants(
  startNodeId: string,
  nodes: GraphNode[],
  edges: GraphEdge[],
): Set<string> {
  const descendants = new Set<string>()
  const queue = [startNodeId]
  const nodeMap = new Map(nodes.map((n) => [n.id, n]))

  const adj = new Map<string, string[]>()
  edges.forEach((edge) => {
    if (!adj.has(edge.source)) adj.set(edge.source, [])
    adj.get(edge.source)!.push(edge.target)
  })

  while (queue.length > 0) {
    const curr = queue.shift()!
    const targets = adj.get(curr) || []
    targets.forEach((targetId) => {
      const targetNode = nodeMap.get(targetId)
      if (targetNode && !descendants.has(targetId)) {
        const kind = targetNode.data?.kind
        if (kind !== 'origin' && kind !== 'goal' && !targetNode.data?.isCriticalPath) {
          descendants.add(targetId)
          queue.push(targetId)
        }
      }
    })
  }
  return descendants
}

function selectUsefulCollapseGroups(
  potentialGroups: Map<string, any[]>,
  nodeMap: Map<string, GraphNode>,
  edges: GraphEdge[],
): [string, any[]][] {
  const depths = buildGraphDepths(nodeMap, edges)
  const candidates = [...potentialGroups.entries()]
    .map(([sourceId, items]) => {
      const sourceNode = nodeMap.get(sourceId)
      if (!sourceNode || AUTO_COLLAPSE_ROOT_KINDS.has(sourceNode.data?.kind || '')) return null

      const groupCollapsedNodeIds = new Set<string>()
      items.forEach((item) => {
        groupCollapsedNodeIds.add(item.node.id)
        item.descendants.forEach((dId: string) => {
          groupCollapsedNodeIds.add(dId)
        })
      })

      const branchCount = items.length
      const collapsedCount = groupCollapsedNodeIds.size
      const hasUsefulReduction =
        branchCount >= AUTO_COLLAPSE_MIN_BRANCHES ||
        collapsedCount >= AUTO_COLLAPSE_MIN_COLLAPSED_NODES
      if (!hasUsefulReduction) return null

      return {
        sourceId,
        items,
        groupCollapsedNodeIds,
        sourceDepth: depths.get(sourceId) ?? 0,
        collapsedCount,
        branchCount,
      }
    })
    .filter(Boolean) as Array<{
    sourceId: string
    items: any[]
    groupCollapsedNodeIds: Set<string>
    sourceDepth: number
    collapsedCount: number
    branchCount: number
  }>

  candidates.sort((left, right) => {
    const depthDiff = right.sourceDepth - left.sourceDepth
    if (depthDiff !== 0) return depthDiff
    const collapsedDiff = right.collapsedCount - left.collapsedCount
    if (collapsedDiff !== 0) return collapsedDiff
    const branchDiff = right.branchCount - left.branchCount
    if (branchDiff !== 0) return branchDiff
    return left.sourceId.localeCompare(right.sourceId)
  })

  const reservedNodeIds = new Set<string>()
  const selected: [string, any[]][] = []
  candidates.forEach((candidate) => {
    if (reservedNodeIds.has(candidate.sourceId)) return
    const overlapsExistingGroup = [...candidate.groupCollapsedNodeIds].some((nodeId) =>
      reservedNodeIds.has(nodeId),
    )
    if (overlapsExistingGroup) return

    selected.push([candidate.sourceId, candidate.items])
    candidate.groupCollapsedNodeIds.forEach((nodeId) => {
      reservedNodeIds.add(nodeId)
    })
  })

  return selected
}

function buildGraphDepths(
  nodeMap: Map<string, GraphNode>,
  edges: GraphEdge[],
): Map<string, number> {
  const incomingCount = new Map<string, number>()
  const adj = new Map<string, string[]>()
  nodeMap.forEach((_node, nodeId) => {
    incomingCount.set(nodeId, 0)
    adj.set(nodeId, [])
  })

  edges.forEach((edge) => {
    if (!nodeMap.has(edge.source) || !nodeMap.has(edge.target)) return
    adj.get(edge.source)!.push(edge.target)
    incomingCount.set(edge.target, (incomingCount.get(edge.target) || 0) + 1)
  })

  const roots = [...nodeMap.values()]
    .filter((node) => node.data?.kind === 'origin')
    .map((node) => node.id)
  if (roots.length === 0) {
    incomingCount.forEach((count, nodeId) => {
      if (count === 0) roots.push(nodeId)
    })
  }

  const depths = new Map<string, number>()
  const queue = roots.map((nodeId) => ({ nodeId, depth: 0 }))
  while (queue.length > 0) {
    const { nodeId, depth } = queue.shift()!
    const previousDepth = depths.get(nodeId)
    if (previousDepth !== undefined && previousDepth <= depth) continue
    depths.set(nodeId, depth)
    ;(adj.get(nodeId) || []).forEach((targetId) => {
      queue.push({ nodeId: targetId, depth: depth + 1 })
    })
  }

  return depths
}

export function collapsePendingBranches(
  nodes: GraphNode[] = [],
  edges: GraphEdge[] = [],
  { enabled = false } = {},
) {
  if (!enabled) {
    return { nodes, edges, collapsedCount: 0, groupCount: 0 }
  }

  const nodeMap = new Map(nodes.map((n) => [n.id, n]))
  const causalEdges = edges.filter((edge) => edge.type !== 'reason-audit')

  const candidateNodeById = new Map(
    nodes
      .filter((node) => {
        const kind = node.data?.kind
        if (kind === 'origin' || kind === 'goal' || kind === 'reason') return false
        if (kind === 'pending') return true
        return !node.data?.isCriticalPath
      })
      .map((node) => [node.id, node]),
  )

  const potentialGroups = new Map<string, any[]>()

  causalEdges.forEach((edge) => {
    const candidateNode = candidateNodeById.get(edge.target)
    if (!candidateNode) return

    const descendants = getNonCriticalDescendants(edge.target, nodes, causalEdges)

    if (!potentialGroups.has(edge.source)) {
      potentialGroups.set(edge.source, [])
    }
    potentialGroups.get(edge.source)!.push({
      edge,
      node: candidateNode,
      descendants,
      isPending: candidateNode.data?.kind === 'pending',
    })
  })

  const activeGroups = selectUsefulCollapseGroups(potentialGroups, nodeMap, causalEdges)
  if (activeGroups.length === 0) {
    return { nodes, edges, collapsedCount: 0, groupCount: 0 }
  }

  const groupedBySource = new Map(activeGroups)
  const collapsedNodeIds = new Set<string>()
  const nodeIdToGroupId = new Map<string, string>()
  const groupNodesBySource = new Map<string, GraphNode>()
  const groupEdges: GraphEdge[] = []

  activeGroups.forEach(([sourceId, items]) => {
    const intents = items
      .map((item) => item.edge.data?.intent || item.node.data?.intent || {})
      .filter(Boolean)

    const groupCollapsedNodeIds = new Set<string>()
    items.forEach((item) => {
      groupCollapsedNodeIds.add(item.node.id)
      item.descendants.forEach((dId: string) => {
        groupCollapsedNodeIds.add(dId)
      })
    })

    let groupRunning = 0
    let groupStale = 0
    let groupConcluded = 0
    let groupUnclaimed = 0

    groupCollapsedNodeIds.forEach((nodeId) => {
      const node = nodeMap.get(nodeId)
      if (!node) return
      if (node.data?.liveState === 'stale') {
        groupStale++
      } else if (
        node.data?.liveState === 'running' ||
        node.data?.liveState === 'heartbeat' ||
        node.data?.status === 'exploring' ||
        node.data?.runStatus === 'running'
      ) {
        groupRunning++
      } else if (node.data?.kind !== 'pending' && node.data?.kind !== 'pending_group') {
        groupConcluded++
      } else {
        groupUnclaimed++
      }
    })

    const localAdj = new Map<string, string[]>()
    causalEdges.forEach((edge) => {
      const isSourceInGroup = groupCollapsedNodeIds.has(edge.source) || edge.source === sourceId
      const isTargetInGroup = groupCollapsedNodeIds.has(edge.target)
      if (isSourceInGroup && isTargetInGroup) {
        if (!localAdj.has(edge.source)) {
          localAdj.set(edge.source, [])
        }
        localAdj.get(edge.source)!.push(edge.target)
      }
    })

    const depths = new Map<string, number>()
    depths.set(sourceId, 0)
    const queue = [sourceId]
    let maxDepth = 0
    while (queue.length > 0) {
      const curr = queue.shift()!
      const currDepth = depths.get(curr)!
      maxDepth = Math.max(maxDepth, currDepth)
      const targets = localAdj.get(curr) || []
      targets.forEach((targetId) => {
        if (!depths.has(targetId)) {
          depths.set(targetId, currDepth + 1)
          queue.push(targetId)
        }
      })
    }

    const hasCriticalPath = causalEdges.some(
      (edge) =>
        edge.data?.isCriticalPath === true &&
        (groupCollapsedNodeIds.has(edge.source) || groupCollapsedNodeIds.has(edge.target)),
    )

    let label = `已收拢 ${items.length} 个分支 (共 ${groupCollapsedNodeIds.size} 个节点, 最大深度 ${maxDepth})`
    const statusParts = []
    if (groupRunning > 0) statusParts.push(`${groupRunning} 运行中`)
    if (groupStale > 0) statusParts.push(`${groupStale} 已过期`)
    if (groupConcluded > 0) statusParts.push(`${groupConcluded} 已完成`)
    if (groupUnclaimed > 0) statusParts.push(`${groupUnclaimed} 未领用`)
    if (statusParts.length > 0) {
      label += `\n分布: ${statusParts.join(', ')}`
    }
    if (hasCriticalPath) {
      label += `\n⚠️ 警告: 包含关键路径!`
    }

    const groupId = `pending-group:${sourceId}`
    const pendingGroup = {
      sourceId,
      count: items.length,
      collapsedCount: groupCollapsedNodeIds.size,
      runningCount: groupRunning,
      staleCount: groupStale,
      concludedCount: groupConcluded,
      unclaimedCount: groupUnclaimed,
      maxDepth,
      hasCriticalPath,
      intentIds: intents.map((intent) => intent.id).filter(Boolean),
      intents,
    }

    items.forEach((item) => {
      collapsedNodeIds.add(item.node.id)
      nodeIdToGroupId.set(item.node.id, groupId)
      item.descendants.forEach((dId: string) => {
        collapsedNodeIds.add(dId)
        nodeIdToGroupId.set(dId, groupId)
      })
    })

    groupNodesBySource.set(sourceId, {
      id: groupId,
      type: 'fact',
      data: {
        kind: 'pending_group',
        status: groupRunning > 0 ? 'exploring' : 'unclaimed',
        label,
        pendingCount: items.length,
        runningCount: groupRunning,
        sourceFactId: sourceId,
        layoutKey: groupId,
        pendingGroup,
      },
      position: { x: 0, y: 0 },
    })

    groupEdges.push({
      id: `edge-pending-group-${sourceId}`,
      source: sourceId,
      target: groupId,
      type: 'intent',
      data: {
        state: groupRunning > 0 ? 'exploring' : 'unclaimed',
        label,
        pendingGroup,
        mergedCount: items.length,
      },
    })
  })

  const collapsedEdgeIds = new Set<string>()
  activeGroups.forEach(([_sourceId, items]) => {
    items.forEach((item) => {
      collapsedEdgeIds.add(item.edge.id)
    })
  })
  edges.forEach((edge) => {
    if (collapsedNodeIds.has(edge.source) && collapsedNodeIds.has(edge.target)) {
      collapsedEdgeIds.add(edge.id)
    }
  })

  const nextNodes: GraphNode[] = []
  const insertedGroupIds = new Set<string>()
  nodes.forEach((node) => {
    if (collapsedNodeIds.has(node.id)) return
    nextNodes.push(node)
    if (groupedBySource.has(node.id)) {
      const groupNode = groupNodesBySource.get(node.id)!
      nextNodes.push(groupNode)
      insertedGroupIds.add(groupNode.id)
    }
  })
  groupNodesBySource.forEach((groupNode) => {
    if (!insertedGroupIds.has(groupNode.id)) nextNodes.push(groupNode)
  })

  const mappedEdges = edges
    .filter((edge) => {
      if (collapsedEdgeIds.has(edge.id)) return false
      if (edge.type !== 'reason-audit' && collapsedNodeIds.has(edge.source)) return false
      return true
    })
    .map((edge) => {
      const source = collapsedNodeIds.has(edge.source)
        ? nodeIdToGroupId.get(edge.source)
        : edge.source
      const target = collapsedNodeIds.has(edge.target)
        ? nodeIdToGroupId.get(edge.target)
        : edge.target
      if (!source || !target || source === target) return null
      if (source === edge.source && target === edge.target) return edge
      return {
        ...edge,
        source,
        target,
        data: {
          ...edge.data,
          collapsedEndpointCount: Number(edge.data?.collapsedEndpointCount || 0) + 1,
        },
      }
    })
    .filter((edge): edge is GraphEdge => Boolean(edge))
    .concat(groupEdges)

  const mergedEdgesMap = new Map<string, GraphEdge>()
  mappedEdges.forEach((edge) => {
    const key =
      edge.type === 'reason-audit'
        ? [
            'reason-audit',
            edge.data?.reasonRunId || edge.data?.taskRunId || edge.id,
            edge.data?.auditDirection || '',
            edge.source,
            edge.target,
          ].join(':')
        : `intent:${edge.source}->${edge.target}`
    if (mergedEdgesMap.has(key)) {
      const existing = mergedEdgesMap.get(key)!
      const existingCount = existing.data?.mergedCount || 1
      const currentCount = edge.data?.mergedCount || 1
      existing.data = {
        ...existing.data,
        mergedCount: existingCount + currentCount,
      }
      if (edge.data?.isCriticalPath) {
        existing.data.isCriticalPath = true
      }
      const existingState = existing.data.state
      const currentState = edge.data?.state
      if (currentState === 'exploring' || existingState === 'exploring') {
        existing.data.state = 'exploring'
      } else if (currentState === 'concluded' || existingState === 'concluded') {
        existing.data.state = 'concluded'
      } else if (currentState === 'bootstrap' || existingState === 'bootstrap') {
        existing.data.state = 'bootstrap'
      }
    } else {
      mergedEdgesMap.set(key, {
        ...edge,
        data: {
          ...edge.data,
          mergedCount: edge.data?.mergedCount || 1,
        },
      })
    }
  })

  const nextEdges = Array.from(mergedEdgesMap.values())

  return {
    nodes: nextNodes,
    edges: nextEdges,
    collapsedCount: collapsedNodeIds.size,
    groupCount: activeGroups.length,
  }
}
