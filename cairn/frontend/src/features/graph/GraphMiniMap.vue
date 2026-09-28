<template>
  <div class="graph-minimap" aria-label="探索图谱缩略图" @pointerdown.stop @wheel.stop>
    <svg
      :viewBox="viewBox"
      role="button"
      tabindex="0"
      aria-label="点击缩略图移动视口"
      @click="handleClick"
      @keydown.enter.prevent="centerGraph"
      @keydown.space.prevent="centerGraph"
    >
      <rect
        v-for="node in nodes"
        :key="node.id"
        class="graph-minimap-node"
        :class="[
          `node-kind-${getGraphNodePresentation(node).tone}`,
          { selected: node.id === selectedNodeId },
        ]"
        :x="node.position?.x || 0"
        :y="node.position?.y || 0"
        :width="node.data?.nodeWidth || 180"
        :height="node.data?.nodeHeight || 80"
        rx="12"
      />
      <rect
        class="graph-minimap-viewport"
        :x="visibleWorld.x"
        :y="visibleWorld.y"
        :width="visibleWorld.width"
        :height="visibleWorld.height"
        rx="8"
      />
    </svg>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { getGraphNodePresentation } from './graphPresentation'
import type { GraphBounds, GraphNode, Position } from './graphTypes'

const props = defineProps<{
  nodes: GraphNode[]
  graphBounds: GraphBounds
  pan: Position
  zoom: number
  viewportSize: { width: number; height: number }
  selectedNodeId: string | null
}>()

const emit = defineEmits<{
  (event: 'centerWorldPoint', x: number, y: number): void
}>()

const padding = computed(() =>
  Math.max(48, Math.min(props.graphBounds.width, props.graphBounds.height) * 0.06),
)
const viewBox = computed(() => {
  const paddingValue = padding.value
  return `${props.graphBounds.minX - paddingValue} ${props.graphBounds.minY - paddingValue} ${Math.max(1, props.graphBounds.width + paddingValue * 2)} ${Math.max(1, props.graphBounds.height + paddingValue * 2)}`
})

const visibleWorld = computed(() => ({
  x: -props.pan.x / props.zoom,
  y: -props.pan.y / props.zoom,
  width: props.viewportSize.width / props.zoom,
  height: props.viewportSize.height / props.zoom,
}))

function handleClick(event: MouseEvent) {
  const svg = event.currentTarget as SVGSVGElement
  const matrix = svg.getScreenCTM()
  if (!matrix) return
  const point = svg.createSVGPoint()
  point.x = event.clientX
  point.y = event.clientY
  const worldPoint = point.matrixTransform(matrix.inverse())
  emit('centerWorldPoint', worldPoint.x, worldPoint.y)
}

function centerGraph() {
  emit(
    'centerWorldPoint',
    props.graphBounds.minX + props.graphBounds.width / 2,
    props.graphBounds.minY + props.graphBounds.height / 2,
  )
}
</script>
