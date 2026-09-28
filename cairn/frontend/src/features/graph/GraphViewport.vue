<template>
  <div
    class="viewport-container"
    :class="{ 'is-panning': isPanning, 'is-zooming': isZooming }"
    ref="viewportElement"
    @pointerdown="$emit('panStart', $event)"
    @pointermove="$emit('panMove', $event)"
    @pointerup="$emit('panEnd', $event)"
    @pointercancel="$emit('panEnd', $event)"
    @wheel="$emit('zoom', $event)"
  >
    <div class="zoom-pan-container" :style="zoomPanStyle">
      <GraphSvgEdges
        :canvas-size="graphCanvasSize"
        :edges="laidOutEdges"
        :edge-coords-map="edgeCoordsMap"
        :show-join-badges="shouldShowJoinBadges"
        :show-edge-labels="shouldShowEdgeLabels"
        :get-edge-group-classes="getEdgeGroupClasses"
        :get-edge-color="getEdgeColor"
        :get-edge-stroke-width="getEdgeStrokeWidth"
        :get-edge-marker="getEdgeMarker"
        :get-edge-join-badge-width="getEdgeJoinBadgeWidth"
        :get-edge-join-badge-text="getEdgeJoinBadgeText"
        :truncate-text="truncateText"
        @select-edge="$emit('selectEdge', $event)"
      />

      <div class="nodes-container">
        <GraphNodeCard
          v-for="node in laidOutNodes"
          :key="node.id"
          :node="node"
          :selected="selectedNodeId === node.id"
          :dragging="draggingNodeId === node.id"
          :is-collapsed="manuallyCollapsedNodeIds.has(node.id)"
          :get-node-type-label="getNodeTypeLabel"
          :truncate-text="truncateText"
          @select-click="$emit('selectNodeClick', $event, node)"
          @drag-start="$emit('nodeDragStart', $event, node)"
          @drag-move="$emit('nodeDragMove', $event, node)"
          @drag-end="$emit('nodeDragEnd', $event, node)"
          @drag-cancel="$emit('nodeDragCancel', $event, node)"
          @select="$emit('selectNode', $event)"
          @toggle-collapse="$emit('toggleNodeCollapse', $event)"
        />
      </div>
    </div>

    <aside class="graph-legend" aria-label="探索图谱图例">
      <div class="graph-legend-group">
        <span class="graph-legend-heading">节点</span>
        <span v-for="item in nodeLegend" :key="item.kind" class="graph-legend-item">
          <span class="graph-legend-mark" :class="`node-kind-${item.tone}`">
            <component :is="item.icon" :size="11" :stroke-width="2" aria-hidden="true" />
          </span>
          {{ item.shortLabel }}
        </span>
      </div>
      <div class="graph-legend-group">
        <span class="graph-legend-heading">连线</span>
        <span v-for="item in edgeLegend" :key="item.key" class="graph-legend-item">
          <span class="graph-legend-line" :class="item.key" />{{ item.label }}
        </span>
        <span class="graph-legend-item"><span class="graph-legend-line running" />运行流向</span>
        <span class="graph-legend-item"><span class="graph-legend-line critical" />关键路径</span>
      </div>
    </aside>

    <div class="graph-controls" aria-label="图谱缩放控件" @pointerdown.stop @wheel.stop>
      <IconButton label="放大图谱" variant="secondary" size="sm" @click="$emit('zoomIn')">
        <Plus :size="15" :stroke-width="2" />
      </IconButton>
      <span class="graph-zoom-value" aria-live="polite">{{ Math.round(zoom * 100) }}%</span>
      <IconButton label="缩小图谱" variant="secondary" size="sm" @click="$emit('zoomOut')">
        <Minus :size="15" :stroke-width="2" />
      </IconButton>
      <IconButton label="适应视口" variant="secondary" size="sm" @click="$emit('fitViewport')">
        <Scan :size="15" :stroke-width="2" />
      </IconButton>
    </div>

    <GraphMiniMap
      v-if="laidOutNodes.length"
      :nodes="laidOutNodes"
      :graph-bounds="graphBounds"
      :pan="pan"
      :zoom="zoom"
      :viewport-size="viewportSize"
      :selected-node-id="selectedNodeId"
      @center-world-point="(x, y) => $emit('centerWorldPoint', x, y)"
    />
  </div>
</template>

<script setup lang="ts">
import { nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { Minus, Plus, Scan } from '@lucide/vue'
import { IconButton } from '@/components/ui'
import GraphMiniMap from './GraphMiniMap.vue'
import GraphNodeCard from './GraphNodeCard.vue'
import GraphSvgEdges from './GraphSvgEdges.vue'
import {
  GRAPH_NODE_KINDS,
  getGraphNodePresentation,
  graphEdgePresentation,
} from './graphPresentation'
import type { EdgeRoute } from './edgeRouting'
import type { GraphBounds, GraphEdge, GraphNode, Position } from './graphTypes'

const props = defineProps<{
  isPanning: boolean
  isZooming: boolean
  zoomPanStyle: Record<string, string>
  graphCanvasSize: { width: number; height: number }
  graphBounds: GraphBounds
  pan: Position
  zoom: number
  viewportSize: { width: number; height: number }
  laidOutNodes: GraphNode[]
  laidOutEdges: GraphEdge[]
  edgeCoordsMap: Record<string, EdgeRoute>
  selectedNodeId: string | null
  draggingNodeId: string | null
  manuallyCollapsedNodeIds: Set<string>
  shouldShowJoinBadges: boolean
  shouldShowEdgeLabels: boolean
  getEdgeGroupClasses: (edge: GraphEdge) => Record<string, boolean>
  getEdgeColor: (edge: GraphEdge) => string
  getEdgeStrokeWidth: (edge: GraphEdge) => number
  getEdgeMarker: (edge: GraphEdge) => string
  getEdgeJoinBadgeWidth: (edge: GraphEdge) => number
  getEdgeJoinBadgeText: (edge: GraphEdge) => string
  getNodeTypeLabel: (node: GraphNode) => string
  truncateText: (text: string, length: number) => string
}>()

const emit = defineEmits<{
  (event: 'panStart', pointerEvent: PointerEvent): void
  (event: 'panMove', pointerEvent: PointerEvent): void
  (event: 'panEnd', pointerEvent: PointerEvent): void
  (event: 'zoom', wheelEvent: WheelEvent): void
  (event: 'zoomIn'): void
  (event: 'zoomOut'): void
  (event: 'fitViewport'): void
  (event: 'centerWorldPoint', x: number, y: number): void
  (event: 'selectEdge', edge: GraphEdge): void
  (event: 'selectNodeClick', pointerEvent: MouseEvent, node: GraphNode): void
  (event: 'nodeDragStart', pointerEvent: PointerEvent, node: GraphNode): void
  (event: 'nodeDragMove', pointerEvent: PointerEvent, node: GraphNode): void
  (event: 'nodeDragEnd', pointerEvent: PointerEvent, node: GraphNode): void
  (event: 'nodeDragCancel', pointerEvent: PointerEvent, node: GraphNode): void
  (event: 'selectNode', node: GraphNode): void
  (event: 'toggleNodeCollapse', nodeId: string): void
  (event: 'nodeSizesChanged', sizes: Record<string, { width: number; height: number }>): void
  (event: 'viewportSizeChanged'): void
}>()

const nodeLegend = GRAPH_NODE_KINDS.map((kind) => getGraphNodePresentation(kind))
const edgeLegend = [
  graphEdgePresentation.intent,
  graphEdgePresentation['reason-input'],
  graphEdgePresentation['reason-output'],
]
const viewportElement = ref<HTMLElement | null>(null)
const measuredSizes: Record<string, { width: number; height: number }> = {}
const observedElements = new Set<Element>()
let resizeObserver: ResizeObserver | null = null

const observeNodeCards = async () => {
  if (!resizeObserver) return
  await nextTick()
  const cards = viewportElement.value?.querySelectorAll<HTMLElement>('[data-node-id]') || []
  observedElements.forEach((element) => resizeObserver?.unobserve(element))
  observedElements.clear()
  cards.forEach((card) => {
    observedElements.add(card)
    resizeObserver?.observe(card)
  })
}

onMounted(() => {
  if (typeof ResizeObserver === 'undefined') return
  resizeObserver = new ResizeObserver((entries) => {
    let changed = false
    entries.forEach((entry) => {
      if (entry.target === viewportElement.value) {
        emit('viewportSizeChanged')
        return
      }
      const nodeId = (entry.target as HTMLElement).dataset.nodeId
      if (!nodeId) return
      // 节点使用 border-box 固定尺寸；contentRect 只包含内容区，会形成“测量后缩小”的反馈循环。
      const element = entry.target as HTMLElement
      const nextSize = {
        width: Math.round(element.offsetWidth || entry.contentRect.width),
        height: Math.round(element.offsetHeight || entry.contentRect.height),
      }
      const previous = measuredSizes[nodeId]
      if (
        previous &&
        Math.abs(previous.width - nextSize.width) <= 1 &&
        Math.abs(previous.height - nextSize.height) <= 1
      ) {
        return
      }
      measuredSizes[nodeId] = nextSize
      changed = true
    })
    if (changed) emit('nodeSizesChanged', { ...measuredSizes })
  })
  if (viewportElement.value) resizeObserver.observe(viewportElement.value)
  observeNodeCards()
})

watch(
  () => props.laidOutNodes.map((node) => node.id).join('|'),
  () => observeNodeCards(),
)

onBeforeUnmount(() => {
  resizeObserver?.disconnect()
  resizeObserver = null
  observedElements.clear()
})

defineExpose({
  viewportElement,
})
</script>
