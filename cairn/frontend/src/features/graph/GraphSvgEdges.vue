<template>
  <svg
    class="svg-edges-layer"
    :width="canvasSize.width"
    :height="canvasSize.height"
    :viewBox="`0 0 ${canvasSize.width} ${canvasSize.height}`"
  >
    <defs>
      <marker
        v-for="marker in markers"
        :id="marker.id"
        :key="marker.id"
        viewBox="0 0 10 10"
        refX="6"
        refY="5"
        markerWidth="5"
        markerHeight="5"
        orient="auto-start-reverse"
      >
        <path d="M 0 1.5 L 8 5 L 0 8.5 z" :fill="marker.color" />
      </marker>
    </defs>

    <g
      v-for="edge in edges"
      :key="edge.id"
      class="edge-group"
      :class="[getEdgeGroupClasses(edge), `edge-semantic-${getGraphEdgePresentation(edge).key}`]"
    >
      <template v-if="edgeCoordsMap[edge.id]">
        <line
          class="edge-hit-area"
          :x1="edgeCoordsMap[edge.id]!.sx"
          :y1="edgeCoordsMap[edge.id]!.sy"
          :x2="edgeCoordsMap[edge.id]!.tx"
          :y2="edgeCoordsMap[edge.id]!.ty"
          stroke="transparent"
          stroke-width="12"
          pointer-events="stroke"
          role="button"
          tabindex="0"
          :aria-label="`查看${getGraphEdgePresentation(edge).label}边 ${edge.id}`"
          @click="$emit('selectEdge', edge)"
          @keydown.enter.prevent="$emit('selectEdge', edge)"
          @keydown.space.prevent="$emit('selectEdge', edge)"
        />
        <line
          class="edge-visible-path"
          :x1="edgeCoordsMap[edge.id]!.sx"
          :y1="edgeCoordsMap[edge.id]!.sy"
          :x2="edgeCoordsMap[edge.id]!.tx"
          :y2="edgeCoordsMap[edge.id]!.ty"
          :stroke="getEdgeColor(edge)"
          :stroke-dasharray="getGraphEdgePresentation(edge).dashArray"
          :class="{ 'critical-edge': edge.data?.isCriticalPath }"
          :stroke-width="getEdgeStrokeWidth(edge)"
          :marker-end="getEdgeMarker(edge)"
        />
        <g
          v-if="showJoinBadges && Number(edge.data?.renderedSourceCount || 0) > 1"
          class="edge-join-badge"
          :transform="`translate(${edgeCoordsMap[edge.id]!.labelX}, ${edgeCoordsMap[edge.id]!.labelY})`"
        >
          <rect
            :x="-(getEdgeJoinBadgeWidth(edge) / 2)"
            y="-9"
            :width="getEdgeJoinBadgeWidth(edge)"
            height="16"
            rx="4"
          />
          <text y="2" text-anchor="middle">{{ getEdgeJoinBadgeText(edge) }}</text>
        </g>
        <template v-if="showEdgeLabels && edge.data?.label">
          <text
            :x="edgeCoordsMap[edge.id]!.labelX"
            :y="edgeCoordsMap[edge.id]!.labelY"
            class="edge-text edge-text-halo"
            text-anchor="middle"
            aria-hidden="true"
          >
            {{ truncateText(edge.data.label, 20) }}
          </text>
          <text
            :x="edgeCoordsMap[edge.id]!.labelX"
            :y="edgeCoordsMap[edge.id]!.labelY"
            class="edge-text"
            text-anchor="middle"
          >
            {{ truncateText(edge.data.label, 20) }}
          </text>
        </template>
      </template>
    </g>
  </svg>
</template>

<script setup lang="ts">
import { getGraphEdgePresentation } from './graphPresentation'
import type { EdgeRoute } from './edgeRouting'
import type { GraphEdge } from './graphTypes'

const markers = [
  { id: 'arrow-normal', color: 'var(--graph-edge-muted)' },
  { id: 'arrow-critical', color: 'var(--graph-edge-critical)' },
  { id: 'arrow-selected', color: 'var(--graph-edge-selected)' },
  { id: 'arrow-active', color: 'var(--graph-edge-active)' },
  { id: 'arrow-intent', color: 'var(--graph-edge-trunk)' },
  { id: 'arrow-reason-input', color: 'var(--graph-edge-reason-input)' },
  { id: 'arrow-reason-output', color: 'var(--graph-edge-reason-output)' },
]

defineProps<{
  canvasSize: { width: number; height: number }
  edges: GraphEdge[]
  edgeCoordsMap: Record<string, EdgeRoute>
  showJoinBadges: boolean
  showEdgeLabels: boolean
  getEdgeGroupClasses: (edge: GraphEdge) => Record<string, boolean>
  getEdgeColor: (edge: GraphEdge) => string
  getEdgeStrokeWidth: (edge: GraphEdge) => number
  getEdgeMarker: (edge: GraphEdge) => string
  getEdgeJoinBadgeWidth: (edge: GraphEdge) => number
  getEdgeJoinBadgeText: (edge: GraphEdge) => string
  truncateText: (text: string, length: number) => string
}>()

defineEmits<{
  (event: 'selectEdge', edge: GraphEdge): void
}>()
</script>
