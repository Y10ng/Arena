import type { GraphEdge, GraphNode } from './graphTypes'

export function applyManualCollapse(
  nodes: GraphNode[] = [],
  edges: GraphEdge[] = [],
  manuallyCollapsedNodeIds: Set<string> = new Set(),
) {
  if (!manuallyCollapsedNodeIds || manuallyCollapsedNodeIds.size === 0) {
    return { nodes, edges }
  }

  const adj = new Map<string, GraphEdge[]>()
  const incomingCount = new Map<string, number>()

  nodes.forEach((node) => {
    adj.set(node.id, [])
    incomingCount.set(node.id, 0)
  })

  edges.forEach((edge) => {
    if (edge.type === 'reason-audit') return
    if (incomingCount.has(edge.source) && incomingCount.has(edge.target)) {
      adj.get(edge.source)!.push(edge)
      incomingCount.set(edge.target, incomingCount.get(edge.target)! + 1)
    }
  })

  const roots: string[] = []
  nodes.forEach((node) => {
    if (node.data?.kind !== 'reason' && (incomingCount.get(node.id) || 0) === 0) {
      roots.push(node.id)
    }
  })

  if (roots.length === 0 && nodes.length > 0) {
    nodes.forEach((node) => roots.push(node.id))
  }

  const visibleNodeIds = new Set<string>()
  const queue = [...roots]

  while (queue.length > 0) {
    const currId = queue.shift()!
    if (visibleNodeIds.has(currId)) continue
    visibleNodeIds.add(currId)

    if (manuallyCollapsedNodeIds.has(currId)) continue

    const outbound = adj.get(currId) || []
    outbound.forEach((edge) => {
      queue.push(edge.target)
    })
  }

  // 隐藏端点映射到最近的手动收拢锚点，Reason 审计链路因此仍可见。
  const collapsedAnchorByNodeId = new Map<string, string>()
  const visibleCollapsedAnchorIds = [...manuallyCollapsedNodeIds]
    .filter((nodeId) => visibleNodeIds.has(nodeId))
    .sort()

  visibleCollapsedAnchorIds.forEach((anchorId) => {
    const visited = new Set([anchorId])
    const descendants = [anchorId]
    while (descendants.length > 0) {
      const currId = descendants.shift()!
      const outbound = adj.get(currId) || []
      outbound.forEach((edge) => {
        if (visited.has(edge.target)) return
        visited.add(edge.target)
        descendants.push(edge.target)
        if (!visibleNodeIds.has(edge.target) && !collapsedAnchorByNodeId.has(edge.target)) {
          collapsedAnchorByNodeId.set(edge.target, anchorId)
        }
      })
    }
  })

  const resolveVisibleNodeId = (nodeId: string) => {
    if (visibleNodeIds.has(nodeId)) return nodeId
    return collapsedAnchorByNodeId.get(nodeId)
  }

  // Reason 不参与因果遍历，只在重映射后仍有可见输入和输出时恢复。
  nodes
    .filter((node) => node.data?.kind === 'reason')
    .forEach((reasonNode) => {
      const auditEdges = edges.filter(
        (edge) =>
          edge.type === 'reason-audit' &&
          (edge.source === reasonNode.id || edge.target === reasonNode.id),
      )
      const inputEdges = auditEdges.filter((edge) => edge.target === reasonNode.id)
      const outputEdges = auditEdges.filter((edge) => edge.source === reasonNode.id)
      const hasVisibleInput =
        inputEdges.length === 0 ||
        inputEdges.some((edge) => Boolean(resolveVisibleNodeId(edge.source)))
      const hasVisibleOutput =
        outputEdges.length === 0 ||
        outputEdges.some((edge) => Boolean(resolveVisibleNodeId(edge.target)))
      if (hasVisibleInput && hasVisibleOutput) visibleNodeIds.add(reasonNode.id)
    })

  const nextNodes = nodes.filter((node) => visibleNodeIds.has(node.id))
  const nextEdges: GraphEdge[] = []
  const reasonAuditKeys = new Set<string>()

  edges.forEach((edge) => {
    if (edge.type !== 'reason-audit') {
      if (
        visibleNodeIds.has(edge.source) &&
        visibleNodeIds.has(edge.target) &&
        !manuallyCollapsedNodeIds.has(edge.source)
      ) {
        nextEdges.push(edge)
      }
      return
    }

    const source = resolveVisibleNodeId(edge.source)
    const target = resolveVisibleNodeId(edge.target)
    if (!source || !target || source === target) return
    if (!visibleNodeIds.has(source) || !visibleNodeIds.has(target)) return

    const reasonId =
      edge.data?.reasonRunId ||
      edge.data?.taskRunId ||
      edge.data?.reasonNodeId ||
      (source.startsWith('reason:') ? source : target.startsWith('reason:') ? target : '')
    const dedupeKey = [reasonId, edge.data?.auditDirection || '', source, target].join(':')
    if (reasonAuditKeys.has(dedupeKey)) return
    reasonAuditKeys.add(dedupeKey)

    const collapsedEndpointCount = Number(source !== edge.source) + Number(target !== edge.target)
    nextEdges.push(
      collapsedEndpointCount === 0
        ? edge
        : {
            ...edge,
            source,
            target,
            data: {
              ...edge.data,
              collapsedEndpointCount:
                Number(edge.data?.collapsedEndpointCount || 0) + collapsedEndpointCount,
            },
          },
    )
  })

  const hiddenNodeIds = new Set(
    nodes.map((node) => node.id).filter((id) => !visibleNodeIds.has(id)),
  )

  const resultNodes = nextNodes.map((node) => {
    if (manuallyCollapsedNodeIds.has(node.id)) {
      const visited = new Set<string>()
      const descendants = [node.id]
      visited.add(node.id)
      while (descendants.length > 0) {
        const curr = descendants.shift()!
        const outbound = adj.get(curr) || []
        outbound.forEach((edge) => {
          if (!visited.has(edge.target)) {
            visited.add(edge.target)
            descendants.push(edge.target)
          }
        })
      }
      visited.delete(node.id)

      let actuallyHiddenCount = 0
      visited.forEach((descendantId) => {
        if (hiddenNodeIds.has(descendantId)) actuallyHiddenCount += 1
      })

      return {
        ...node,
        data: {
          ...node.data,
          collapsedDescendantsCount: actuallyHiddenCount,
        },
      }
    }
    return node
  })

  return {
    nodes: resultNodes,
    edges: nextEdges,
  }
}
