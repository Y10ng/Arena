<template>
  <div
    class="node-card"
    :class="[
      `node-kind-${presentation.tone}`,
      {
        selected,
        'search-match': node.data?.isSearchMatch,
        critical: node.data?.isCriticalPath,
        pending: node.data?.kind === 'pending',
        'pending-group': node.data?.kind === 'pending_group',
        origin: node.data?.kind === 'origin',
        goal: node.data?.kind === 'goal',
        reason: node.data?.kind === 'reason',
        dragging,
        'is-running': status.isRunning,
      },
    ]"
    :style="{
      left: `${node.position?.x}px`,
      top: `${node.position?.y}px`,
      width: `${node.data?.nodeWidth || 180}px`,
      height: `${node.data?.nodeHeight || 80}px`,
    }"
    :data-node-id="node.id"
    role="button"
    tabindex="0"
    :aria-label="`${getNodeTypeLabel(node)}：${node.data?.conclusion || node.data?.label || node.id}，状态 ${status.label}`"
    :aria-pressed="selected"
    @click="$emit('selectClick', $event, node)"
    @pointerdown="$emit('dragStart', $event, node)"
    @pointermove="$emit('dragMove', $event, node)"
    @pointerup="$emit('dragEnd', $event, node)"
    @pointercancel="$emit('dragCancel', $event, node)"
    @keydown.enter.prevent="$emit('select', node)"
    @keydown.space.prevent="$emit('select', node)"
  >
    <span class="node-handle node-handle-input" aria-hidden="true" />
    <span class="node-handle node-handle-output" aria-hidden="true" />

    <div class="node-header">
      <div class="node-heading">
        <span class="node-kind-icon" aria-hidden="true">
          <component :is="presentation.icon" :size="15" :stroke-width="2" />
        </span>
        <div class="node-heading-copy">
          <span class="node-type-label">{{ presentation.label }}</span>
          <span v-if="status.isRunning" class="node-running-indicator">
            <span class="node-pulse-dot" />运行中
          </span>
        </div>
      </div>
      <div class="node-icons">
        <span v-if="node.data?.evidenceCount > 0" class="evidence-badge" title="关联的证据产物">
          <Paperclip :size="11" :stroke-width="2.2" aria-hidden="true" />
          {{ node.data?.evidenceCount }}
        </span>
        <button
          v-if="!['pending', 'pending_group', 'reason'].includes(node.data?.kind || '')"
          class="btn-collapse"
          type="button"
          :aria-label="isCollapsed ? '展开下游' : '折叠下游'"
          :title="isCollapsed ? '展开下游' : '折叠下游'"
          @click.stop="$emit('toggleCollapse', node.id)"
          @keydown.enter.stop
          @keydown.space.stop
        >
          <ChevronDown v-if="isCollapsed" :size="13" :stroke-width="2.2" aria-hidden="true" />
          <ChevronUp v-else :size="13" :stroke-width="2.2" aria-hidden="true" />
        </button>
      </div>
    </div>

    <div class="node-body">
      <div class="node-conclusion">{{ node.data?.conclusion || node.id }}</div>
      <div class="node-description">
        {{ truncateText(node.data?.summary || node.data?.label || '', 90) }}
      </div>
    </div>

    <div class="node-footer">
      <div class="node-footer-meta">
        <span class="node-status-pill" :class="`tone-${status.tone}`">{{ status.label }}</span>
        <span v-if="node.data?.worker || node.data?.creator" class="node-executor mono">
          {{ node.data.worker || node.data.creator }}
        </span>
      </div>
      <span class="node-id mono" :title="node.id">{{ node.id }}</span>
    </div>

    <div v-if="isCollapsed && node.data?.collapsedDescendantsCount" class="collapsed-badge">
      +{{ node.data.collapsedDescendantsCount }} 折叠
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { ChevronDown, ChevronUp, Paperclip } from '@lucide/vue'
import { getGraphNodePresentation, resolveGraphNodeStatus } from './graphPresentation'
import type { GraphNode } from './graphTypes'

const props = defineProps<{
  node: GraphNode
  selected: boolean
  dragging: boolean
  isCollapsed: boolean
  getNodeTypeLabel: (node: GraphNode) => string
  truncateText: (text: string, length: number) => string
}>()

const presentation = computed(() => getGraphNodePresentation(props.node))
const status = computed(() => resolveGraphNodeStatus(props.node))

defineEmits<{
  (event: 'selectClick', pointerEvent: MouseEvent, node: GraphNode): void
  (event: 'dragStart', pointerEvent: PointerEvent, node: GraphNode): void
  (event: 'dragMove', pointerEvent: PointerEvent, node: GraphNode): void
  (event: 'dragEnd', pointerEvent: PointerEvent, node: GraphNode): void
  (event: 'dragCancel', pointerEvent: PointerEvent, node: GraphNode): void
  (event: 'select', node: GraphNode): void
  (event: 'toggleCollapse', nodeId: string): void
}>()
</script>
