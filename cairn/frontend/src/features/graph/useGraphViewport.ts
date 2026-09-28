import { computed, reactive, ref, type ComputedRef } from 'vue'
import type { GraphBounds, GraphNode } from './graphTypes'

const FIT_VIEWPORT_PADDING = 96
const MIN_ZOOM = 0.035
const DEFAULT_INTERACTION_MIN_ZOOM = 0.15
const MAX_ZOOM = 2.5
const MAX_FIT_ZOOM = 1.1

interface GraphViewportOptions {
  viewportElement: ComputedRef<HTMLElement | null>
  graphBounds: ComputedRef<GraphBounds>
  graphCanvasSize: ComputedRef<{ width: number; height: number }>
  laidOutNodes: ComputedRef<GraphNode[]>
}

export function useGraphViewport({
  viewportElement,
  graphBounds,
  graphCanvasSize,
  laidOutNodes,
}: GraphViewportOptions) {
  const pan = reactive({ x: 80, y: 100 })
  const zoom = ref(0.85)
  const viewportSize = reactive({ width: 800, height: 500 })
  const isPanning = ref(false)
  const isZooming = ref(false)
  const panStart = {
    clientX: 0,
    clientY: 0,
    panX: 0,
    panY: 0,
    pointerId: -1,
  }
  let zoomFrameId: number | null = null
  let zoomSettleTimer: number | null = null
  let pendingZoomEvent: { clientX: number; clientY: number; deltaY: number } | null = null

  const zoomPanStyle = computed(() => ({
    transform: `translate3d(${pan.x}px, ${pan.y}px, 0) scale(${zoom.value})`,
    width: `${graphCanvasSize.value.width}px`,
    height: `${graphCanvasSize.value.height}px`,
  }))

  const syncViewportSize = () => {
    viewportSize.width = viewportElement.value?.clientWidth || 800
    viewportSize.height = viewportElement.value?.clientHeight || 500
  }

  const startPan = (event: PointerEvent) => {
    if (event.button !== 0) return
    if (!canStartViewportPan(event.target)) return

    isPanning.value = true
    panStart.clientX = event.clientX
    panStart.clientY = event.clientY
    panStart.panX = pan.x
    panStart.panY = pan.y
    panStart.pointerId = event.pointerId
    viewportElement.value?.setPointerCapture(event.pointerId)
    event.preventDefault()
  }

  const onPan = (event: PointerEvent) => {
    if (!isPanning.value || event.pointerId !== panStart.pointerId) return
    pan.x = panStart.panX + event.clientX - panStart.clientX
    pan.y = panStart.panY + event.clientY - panStart.clientY
  }

  const endPan = (event?: PointerEvent) => {
    if (event && event.pointerId !== panStart.pointerId) return
    if (event && viewportElement.value?.hasPointerCapture(event.pointerId)) {
      viewportElement.value.releasePointerCapture(event.pointerId)
    }
    isPanning.value = false
    panStart.pointerId = -1
  }

  const onZoom = (event: WheelEvent) => {
    event.preventDefault()
    pendingZoomEvent = {
      clientX: event.clientX,
      clientY: event.clientY,
      deltaY: (pendingZoomEvent?.deltaY || 0) + event.deltaY,
    }
    isZooming.value = true
    if (zoomSettleTimer !== null) {
      window.clearTimeout(zoomSettleTimer)
    }
    zoomSettleTimer = window.setTimeout(() => {
      isZooming.value = false
      zoomSettleTimer = null
    }, 120)

    if (zoomFrameId === null) {
      zoomFrameId = window.requestAnimationFrame(applyQueuedZoom)
    }
  }

  const resetViewport = () => {
    fitViewport()
  }

  const fitViewport = () => {
    if (laidOutNodes.value.length === 0) return
    const bounds = graphBounds.value
    const graphWidth = bounds.width || 1
    const graphHeight = bounds.height || 1

    syncViewportSize()
    const viewportWidth = viewportSize.width
    const viewportHeight = viewportSize.height

    const padding = Math.min(
      FIT_VIEWPORT_PADDING,
      Math.max(32, Math.min(viewportWidth, viewportHeight) * 0.12),
    )
    const availableWidth = Math.max(1, viewportWidth - padding)
    const availableHeight = Math.max(1, viewportHeight - padding)
    const zoomX = availableWidth / graphWidth
    const zoomY = availableHeight / graphHeight
    const nextZoom = Math.max(MIN_ZOOM, Math.min(MAX_FIT_ZOOM, Math.min(zoomX, zoomY)))

    zoom.value = nextZoom
    pan.x = (viewportWidth - graphWidth * nextZoom) / 2 - bounds.minX * nextZoom
    pan.y = (viewportHeight - graphHeight * nextZoom) / 2 - bounds.minY * nextZoom
  }

  const centerWorldPoint = (worldX: number, worldY: number) => {
    syncViewportSize()
    pan.x = viewportSize.width / 2 - worldX * zoom.value
    pan.y = viewportSize.height / 2 - worldY * zoom.value
  }

  const setZoomAroundViewportCenter = (nextZoom: number) => {
    syncViewportSize()
    const currentZoom = zoom.value
    const clampedZoom = clampNumber(nextZoom, resolveInteractionMinZoom(), MAX_ZOOM)
    if (clampedZoom === currentZoom) return
    const centerX = viewportSize.width / 2
    const centerY = viewportSize.height / 2
    const worldX = (centerX - pan.x) / currentZoom
    const worldY = (centerY - pan.y) / currentZoom
    zoom.value = clampedZoom
    pan.x = centerX - worldX * clampedZoom
    pan.y = centerY - worldY * clampedZoom
  }

  const zoomIn = () => setZoomAroundViewportCenter(zoom.value * 1.2)
  const zoomOut = () => setZoomAroundViewportCenter(zoom.value / 1.2)

  const centerNodeInViewport = (node: GraphNode) => {
    if (!node.position) return
    const width = node.data?.nodeWidth || 180
    const height = node.data?.nodeHeight || 80
    centerWorldPoint(node.position.x + width / 2, node.position.y + height / 2)
  }

  const cleanupViewport = () => {
    if (zoomFrameId !== null) {
      window.cancelAnimationFrame(zoomFrameId)
      zoomFrameId = null
    }
    if (zoomSettleTimer !== null) {
      window.clearTimeout(zoomSettleTimer)
      zoomSettleTimer = null
    }
  }

  const applyQueuedZoom = () => {
    zoomFrameId = null
    const queuedEvent = pendingZoomEvent
    pendingZoomEvent = null
    if (!queuedEvent) return

    const viewport = viewportElement.value
    if (!viewport) return

    const rect = viewport.getBoundingClientRect()
    const pointerX = queuedEvent.clientX - rect.left
    const pointerY = queuedEvent.clientY - rect.top
    const currentZoom = zoom.value
    const wheelSteps = clampNumber(-queuedEvent.deltaY / 90, -8, 8)
    const nextZoom = clampNumber(
      currentZoom * Math.pow(1.08, wheelSteps),
      resolveInteractionMinZoom(),
      MAX_ZOOM,
    )
    if (nextZoom === currentZoom) return

    const worldX = (pointerX - pan.x) / currentZoom
    const worldY = (pointerY - pan.y) / currentZoom
    zoom.value = nextZoom
    pan.x = pointerX - worldX * nextZoom
    pan.y = pointerY - worldY * nextZoom
  }

  function canStartViewportPan(target: EventTarget | null) {
    if (!viewportElement.value || !(target instanceof HTMLElement || target instanceof SVGElement))
      return false
    if (!viewportElement.value.contains(target)) return false
    if (target.closest('.node-card, .edge-hit-area, button, a, input, textarea, select'))
      return false
    return (
      target === viewportElement.value ||
      target.classList.contains('zoom-pan-container') ||
      target.classList.contains('nodes-container') ||
      target.classList.contains('svg-edges-layer')
    )
  }

  function resolveInteractionMinZoom(): number {
    const viewport = viewportElement.value
    if (!viewport || graphBounds.value.width <= 0 || graphBounds.value.height <= 0) {
      return MIN_ZOOM
    }

    const fitZoom = Math.min(
      viewport.clientWidth / graphBounds.value.width,
      viewport.clientHeight / graphBounds.value.height,
    )
    return Math.max(MIN_ZOOM, Math.min(DEFAULT_INTERACTION_MIN_ZOOM, fitZoom * 0.75))
  }

  return {
    pan,
    zoom,
    viewportSize,
    isPanning,
    isZooming,
    zoomPanStyle,
    startPan,
    onPan,
    endPan,
    onZoom,
    resetViewport,
    fitViewport,
    zoomIn,
    zoomOut,
    centerWorldPoint,
    centerNodeInViewport,
    syncViewportSize,
    cleanupViewport,
  }
}

function clampNumber(value: number, min: number, max: number): number {
  return Math.min(max, Math.max(min, value))
}
