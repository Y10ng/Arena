import type { GraphBounds, GraphNode, Position } from './graphTypes'

export const MIN_GRAPH_CANVAS_SIZE = 4000

export function measureGraphBounds(nodes: GraphNode[]): GraphBounds {
  if (!nodes.length) {
    return {
      minX: 0,
      minY: 0,
      maxX: MIN_GRAPH_CANVAS_SIZE,
      maxY: MIN_GRAPH_CANVAS_SIZE,
      width: MIN_GRAPH_CANVAS_SIZE,
      height: MIN_GRAPH_CANVAS_SIZE,
    }
  }

  let minX = Infinity
  let maxX = -Infinity
  let minY = Infinity
  let maxY = -Infinity
  nodes.forEach((node) => {
    const x = isFinitePosition(node.position) ? node.position.x : 0
    const y = isFinitePosition(node.position) ? node.position.y : 0
    const w = Number(node.data?.nodeWidth) || 180
    const h = Number(node.data?.nodeHeight) || 80
    minX = Math.min(minX, x)
    minY = Math.min(minY, y)
    maxX = Math.max(maxX, x + w)
    maxY = Math.max(maxY, y + h)
  })

  if (![minX, minY, maxX, maxY].every(Number.isFinite)) {
    return {
      minX: 0,
      minY: 0,
      maxX: MIN_GRAPH_CANVAS_SIZE,
      maxY: MIN_GRAPH_CANVAS_SIZE,
      width: MIN_GRAPH_CANVAS_SIZE,
      height: MIN_GRAPH_CANVAS_SIZE,
    }
  }

  return {
    minX,
    minY,
    maxX,
    maxY,
    width: Math.max(1, maxX - minX),
    height: Math.max(1, maxY - minY),
  }
}

export function isFinitePosition(position: any): position is Position {
  return Boolean(
    position
    && Number.isFinite(Number(position.x))
    && Number.isFinite(Number(position.y))
  )
}

export function clonePosition(position: Position): Position {
  return {
    x: Number(position.x),
    y: Number(position.y),
  }
}

export function cloneNodesForLayout(nodes: GraphNode[]): GraphNode[] {
  return nodes.map(node => ({
    ...node,
    position: node.position ? clonePosition(node.position) : node.position,
  }))
}
