import { ref, type Ref } from 'vue'
import type { GraphNode, NodeDragState, Position } from './graphTypes'

export function useGraphNodeDrag(userNodePositions: Ref<Record<string, Position>>, zoom: Ref<number>) {
  const draggingNodeId = ref<string | null>(null)
  const nodeDragState = ref<NodeDragState | null>(null)
  const suppressNodeClickId = ref<string | null>(null)

  const startNodeDrag = (event: PointerEvent, node: GraphNode) => {
    if (event.button !== 0) return
    const target = event.target
    if (target instanceof Element && target.closest('button, a, input, textarea, select')) return

    const startPosition = node.position || { x: 0, y: 0 }
    nodeDragState.value = {
      nodeId: node.id,
      pointerId: event.pointerId,
      startClientX: event.clientX,
      startClientY: event.clientY,
      startX: startPosition.x,
      startY: startPosition.y,
      moved: false,
    }
    const currentTarget = event.currentTarget as HTMLElement
    currentTarget.setPointerCapture(event.pointerId)
    event.stopPropagation()
  }

  const onNodeDrag = (event: PointerEvent, node: GraphNode) => {
    const dragState = nodeDragState.value
    if (!dragState || dragState.nodeId !== node.id || dragState.pointerId !== event.pointerId) return

    const screenDx = event.clientX - dragState.startClientX
    const screenDy = event.clientY - dragState.startClientY
    if (!dragState.moved && Math.hypot(screenDx, screenDy) > 3) {
      dragState.moved = true
      draggingNodeId.value = node.id
    }
    if (!dragState.moved) return

    userNodePositions.value = {
      ...userNodePositions.value,
      [node.id]: {
        x: dragState.startX + screenDx / zoom.value,
        y: dragState.startY + screenDy / zoom.value,
      },
    }
    event.preventDefault()
  }

  const endNodeDrag = (event: PointerEvent, node: GraphNode) => {
    const dragState = nodeDragState.value
    if (!dragState || dragState.nodeId !== node.id || dragState.pointerId !== event.pointerId) return

    const currentTarget = event.currentTarget as HTMLElement
    if (currentTarget.hasPointerCapture(event.pointerId)) {
      currentTarget.releasePointerCapture(event.pointerId)
    }
    if (dragState.moved) {
      suppressNodeClickId.value = node.id
      window.setTimeout(() => {
        if (suppressNodeClickId.value === node.id) {
          suppressNodeClickId.value = null
        }
      }, 0)
    }
    nodeDragState.value = null
    draggingNodeId.value = null
  }

  const cancelNodeDrag = (event: PointerEvent, node: GraphNode) => {
    if (nodeDragState.value?.nodeId !== node.id) return
    const currentTarget = event.currentTarget as HTMLElement
    if (currentTarget.hasPointerCapture(event.pointerId)) {
      currentTarget.releasePointerCapture(event.pointerId)
    }
    nodeDragState.value = null
    draggingNodeId.value = null
  }

  const resetNodeDrag = () => {
    nodeDragState.value = null
    draggingNodeId.value = null
    suppressNodeClickId.value = null
  }

  return {
    draggingNodeId,
    suppressNodeClickId,
    startNodeDrag,
    onNodeDrag,
    endNodeDrag,
    cancelNodeDrag,
    resetNodeDrag,
  }
}
