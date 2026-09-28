<template>
  <div class="graph-page" :class="{ 'is-fullscreen': isGraphFullscreen }" ref="graphPageElement">
    <GraphToolbar
      v-model:search-query="searchQuery"
      v-model:active-status-filter="activeStatusFilter"
      v-model:is-branch-collapse-enabled="isBranchCollapseEnabled"
      :search-match-count="searchMatchCount"
      :status-filters="statusFilters"
      :is-fullscreen="isGraphFullscreen"
      @run-search="runSearch"
      @reset-viewport="resetViewport"
      @reset-node-positions="resetUserNodePositions"
      @toggle-fullscreen="toggleFullscreen"
      @add-hint="openHintDialog"
    />

    <GraphViewport
      ref="viewportComponent"
      :is-panning="isPanning"
      :is-zooming="isZooming"
      :zoom-pan-style="zoomPanStyle"
      :graph-canvas-size="graphCanvasSize"
      :graph-bounds="graphBounds"
      :pan="pan"
      :zoom="zoom"
      :viewport-size="viewportSize"
      :laid-out-nodes="laidOutNodes"
      :laid-out-edges="laidOutEdges"
      :edge-coords-map="edgeCoordsMap"
      :selected-node-id="selectedNodeId"
      :dragging-node-id="draggingNodeId"
      :manually-collapsed-node-ids="manuallyCollapsedNodeIds"
      :should-show-join-badges="shouldShowJoinBadges"
      :should-show-edge-labels="shouldShowEdgeLabels"
      :get-edge-group-classes="getEdgeGroupClasses"
      :get-edge-color="getEdgeColor"
      :get-edge-stroke-width="getEdgeStrokeWidth"
      :get-edge-marker="getEdgeMarker"
      :get-edge-join-badge-width="getEdgeJoinBadgeWidth"
      :get-edge-join-badge-text="getEdgeJoinBadgeText"
      :get-node-type-label="getNodeTypeLabel"
      :truncate-text="truncateText"
      @pan-start="startPan"
      @pan-move="onPan"
      @pan-end="endPan"
      @zoom="onZoom"
      @zoom-in="zoomIn"
      @zoom-out="zoomOut"
      @fit-viewport="fitViewport"
      @center-world-point="centerWorldPoint"
      @select-edge="selectEdge"
      @select-node-click="selectNodeFromClick"
      @node-drag-start="startNodeDrag"
      @node-drag-move="onNodeDrag"
      @node-drag-end="endNodeDrag"
      @node-drag-cancel="cancelNodeDrag"
      @select-node="selectNode"
      @toggle-node-collapse="toggleNodeCollapse"
      @node-sizes-changed="measuredNodeSizes = $event"
      @viewport-size-changed="syncViewportSize"
    />

    <GraphPlaybackControls
      v-model:playback-index="playbackIndex"
      :is-playing="isPlaying"
      :event-count="playbackEvents.length"
      :playback-time-label="playbackTimeLabel"
      :playback-event-desc="playbackEventDesc"
      @toggle-playback="togglePlayback"
      @slider-input="onPlaybackSliderInput"
    />

    <GraphDetailsDrawer
      :show-node-drawer="showNodeDrawer"
      :show-edge-drawer="showEdgeDrawer"
      :selected-node="selectedNode"
      :selected-edge="selectedEdge"
      :evidence-list="evidenceList"
      :decision-result="decisionResult"
      :get-node-type-label="getNodeTypeLabel"
      @close="closeDrawer"
      @go-to-run="goToRun"
      @preview-artifact="previewArtifact"
      @retry-from-fact="openRetryDialog"
    />

    <Dialog
      :open="retryDialogOpen"
      :title="`换方向重试（from ${retryFromFactId}）`"
      description="从当前 Fact 创建新的探索 intent，并由 dispatcher 继续调度。"
      size="sm"
      portal-disabled
      close-label="关闭换方向重试"
      @update:open="retryDialogOpen = $event"
      @open-auto-focus="handleRetryDialogOpenAutoFocus"
      @escape-key-down="handleDialogEscapeKeyDown"
    >
      <Textarea
        ref="retryTextarea"
        v-model="retryDescription"
        aria-label="新的探索角度或策略"
        placeholder="描述新的探索角度/策略（如：换角色重放、换参数、换工具、换绕过方式）..."
        :rows="4"
        @keydown.meta.enter="submitRetry"
        @keydown.ctrl.enter="submitRetry"
      />

      <template #footer>
        <Button variant="secondary" @click="retryDialogOpen = false">取消</Button>
        <Button
          :loading="retrySubmitting"
          :disabled="!retryDescription.trim()"
          @click="submitRetry"
        >
          创建重试
        </Button>
      </template>
    </Dialog>

    <Dialog
      :open="hintDialogOpen"
      title="添加项目提示"
      description="添加项目级提示，并在下一轮 reason 时注入探索上下文。"
      size="sm"
      portal-disabled
      close-label="关闭添加项目提示"
      @update:open="hintDialogOpen = $event"
      @open-auto-focus="handleHintDialogOpenAutoFocus"
      @escape-key-down="handleDialogEscapeKeyDown"
    >
      <Textarea
        ref="hintTextarea"
        v-model="hintDialogContent"
        aria-label="项目提示内容"
        placeholder="输入提示内容... 该提示会随图谱快照注入下一轮 reason，引导探索方向"
        :rows="4"
        @keydown.meta.enter="submitHintDialog"
        @keydown.ctrl.enter="submitHintDialog"
      />

      <template #footer>
        <Button variant="secondary" @click="hintDialogOpen = false">取消</Button>
        <Button
          :loading="hintSubmitting"
          :disabled="!hintDialogContent.trim()"
          @click="submitHintDialog"
        >
          提交提示
        </Button>
      </template>
    </Dialog>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, watch, nextTick, onMounted, onUnmounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useWorkspaceStore } from '@/stores/workspace'
import { useUIStore } from '@/stores/ui'
import {
  api,
  listProjectTaskRunResultEvents,
  listProjectTaskRuns,
  type TaskRunSummary,
} from '@/api/client'
import { Button, Dialog, Textarea } from '@/components/ui'
import GraphDetailsDrawer from './GraphDetailsDrawer.vue'
import GraphPlaybackControls from './GraphPlaybackControls.vue'
import GraphToolbar from './GraphToolbar.vue'
import GraphViewport from './GraphViewport.vue'
import { convertToGraph, buildGraphPlaybackEvents } from './useGraphModel'
import {
  getNodeLayoutKey,
  layoutSemanticTimeline,
  type TimelinePlacement,
} from './semanticTimelineLayout'
import { collapsePendingBranches, filterGraphElements, applyManualCollapse } from './graphFilters'
import {
  buildEdgeRoutes,
  compactGraphEdgesForRendering,
  projectVisibleNarrativeEdges,
} from './edgeRouting'
import {
  MIN_GRAPH_CANVAS_SIZE,
  clonePosition,
  isFinitePosition,
  measureGraphBounds,
} from './graphGeometry'
import { useGraphFullscreen } from './useGraphFullscreen'
import { useGraphNodeDrag } from './useGraphNodeDrag'
import { useGraphPlayback } from './useGraphPlayback'
import { useGraphSelection } from './useGraphSelection'
import type { ResultEvent } from './panels/decisionModel'
import { useGraphViewport } from './useGraphViewport'
import {
  getGraphEdgePresentation,
  getGraphNodeTypeLabel,
  isGraphEdgeRunning,
} from './graphPresentation'
import type { GraphBounds, GraphEdge, GraphNode, Position } from './graphTypes'
import type { EdgeRoute } from './edgeRouting'

const router = useRouter()
const route = useRoute()
const workspaceStore = useWorkspaceStore()
const uiStore = useUIStore()

// 全局提示弹窗：从工具栏触发，提交项目级提示（不绑定节点），下一轮 reason 注入。
const hintDialogOpen = ref(false)
const hintDialogContent = ref('')
const hintSubmitting = ref(false)

// 换方向重试弹窗：从 fact 节点触发，创建新 intent，dispatcher 选不同 worker。
const retryDialogOpen = ref(false)
const retryFromFactId = ref('')
const retryDescription = ref('')
const retrySubmitting = ref(false)
const retryTextarea = ref<{ focus: (options?: FocusOptions) => void } | null>(null)
const hintTextarea = ref<{ focus: (options?: FocusOptions) => void } | null>(null)

const openHintDialog = () => {
  hintDialogContent.value = ''
  hintDialogOpen.value = true
}

const openRetryDialog = (factId: string) => {
  retryFromFactId.value = factId
  retryDescription.value = ''
  retryDialogOpen.value = true
}

const handleRetryDialogOpenAutoFocus = (event: Event) => {
  event.preventDefault()
  nextTick(() => retryTextarea.value?.focus())
}

const handleHintDialogOpenAutoFocus = (event: Event) => {
  event.preventDefault()
  nextTick(() => hintTextarea.value?.focus())
}

const handleDialogEscapeKeyDown = (event: KeyboardEvent) => {
  event.stopImmediatePropagation()
}

const submitRetry = async () => {
  const pid = workspaceStore.currentProjectId
  const desc = retryDescription.value.trim()
  if (!pid || !desc || retrySubmitting.value) return
  retrySubmitting.value = true
  try {
    await api('POST', `/projects/${pid}/intents`, {
      from: [retryFromFactId.value],
      description: desc,
    })
    uiStore.showToast('已创建重试 intent，dispatcher 将选不同 worker 探索', 'success')
    retryDialogOpen.value = false
    workspaceStore.loadProject(pid)
  } catch (err: any) {
    uiStore.showToast(err.message || '创建重试失败', 'danger')
  } finally {
    retrySubmitting.value = false
  }
}

const submitHintDialog = async () => {
  const pid = workspaceStore.currentProjectId
  const content = hintDialogContent.value.trim()
  if (!pid || !content || hintSubmitting.value) return
  hintSubmitting.value = true
  try {
    await api('POST', `/projects/${pid}/hints`, {
      content,
    })
    uiStore.showToast('已添加项目提示，下一轮 reason 将注入', 'success')
    hintDialogOpen.value = false
    workspaceStore.loadProject(pid)
  } catch (err: any) {
    uiStore.showToast(err.message || '添加提示失败', 'danger')
  } finally {
    hintSubmitting.value = false
  }
}

// State
const searchQuery = ref('')
const activeStatusFilter = ref('all')
const isBranchCollapseEnabled = ref(false)

const layoutDir = 'LR' as const

const GRAPH_CANVAS_PADDING = 800
const graphPageElement = ref<HTMLElement | null>(null)
const viewportComponent = ref<{ viewportElement: HTMLElement | null } | null>(null)
const viewportElement = computed(() => viewportComponent.value?.viewportElement ?? null)
const userNodePositions = ref<Record<string, Position>>({})
const currentProjectId = computed(() => workspaceStore.currentProjectId)

const resetUserNodePositions = () => {
  userNodePositions.value = {}
}

// Status Filter choices
const statusFilters = [
  { value: 'all', label: '全部' },
  { value: 'open', label: '未完成' },
  { value: 'running', label: '探索中' },
  { value: 'completed', label: '已完成' },
  { value: 'critical', label: '关键路径' },
]

// Manual collapse node ids
const manuallyCollapsedNodeIds = ref<Set<string>>(new Set())
const baseLaidOutNodes = ref<GraphNode[]>([])
const measuredNodeSizes = ref<Record<string, { width: number; height: number }>>({})
const timelinePlacements = ref<Record<string, TimelinePlacement>>({})
const previousNodeIdByLayoutKey = new Map<string, string>()
let timelinePlacementProjectId = ''
const hasInitialFit = ref(false)

// TaskRun 丰富主图元数据，Reason result-events 只补充审计语义，失败时不阻断主图。
const taskRuns = ref<TaskRunSummary[]>([])
const resultEventsByRun = ref<Record<string, ResultEvent[]>>({})
let taskRunsRequestId = 0
let taskRunsAbortController: AbortController | null = null

const chunkIds = (ids: string[], size: number) => {
  const chunks: string[][] = []
  for (let index = 0; index < ids.length; index += size) {
    chunks.push(ids.slice(index, index + size))
  }
  return chunks
}

const fetchTaskRuns = async () => {
  const projectId = workspaceStore.currentProjectId
  const requestId = ++taskRunsRequestId
  taskRunsAbortController?.abort()
  taskRunsAbortController = null
  if (!projectId) {
    taskRuns.value = []
    resultEventsByRun.value = {}
    return
  }

  const controller = new AbortController()
  taskRunsAbortController = controller
  try {
    const runs = await listProjectTaskRuns(projectId, controller.signal)
    if (requestId !== taskRunsRequestId || workspaceStore.currentProjectId !== projectId) return
    taskRuns.value = runs
    resultEventsByRun.value = {}

    const reasonRunIds = runs.filter((run) => run.task_type === 'reason').map((run) => run.id)
    try {
      const eventGroups = (
        await Promise.all(
          chunkIds(reasonRunIds, 50).map((runIds) =>
            listProjectTaskRunResultEvents(projectId, runIds, controller.signal),
          ),
        )
      ).flat()
      if (requestId !== taskRunsRequestId || workspaceStore.currentProjectId !== projectId) return
      resultEventsByRun.value = Object.fromEntries(
        eventGroups.map((group) => [group.task_run_id, group.events as ResultEvent[]]),
      )
    } catch (error) {
      if (controller.signal.aborted || requestId !== taskRunsRequestId) return
      resultEventsByRun.value = {}
    }
  } catch (error) {
    if (controller.signal.aborted || requestId !== taskRunsRequestId) return
    taskRuns.value = []
    resultEventsByRun.value = {}
  } finally {
    if (taskRunsAbortController === controller) taskRunsAbortController = null
  }
}

const playbackEvents = computed(() => {
  if (
    !workspaceStore.projectDetail ||
    workspaceStore.projectDetail.project?.id !== workspaceStore.currentProjectId
  )
    return []
  return buildGraphPlaybackEvents(workspaceStore.projectDetail)
})

const {
  playbackIndex,
  isPlaying,
  cutoffTime,
  playbackTimeLabel,
  playbackEventDesc,
  togglePlayback,
  resetPlayback,
  onPlaybackSliderInput,
} = useGraphPlayback(playbackEvents)

// Convert model to graph elements
const rawGraphElements = computed(() => {
  if (
    !workspaceStore.projectDetail ||
    workspaceStore.projectDetail.project?.id !== workspaceStore.currentProjectId
  ) {
    return { nodes: [], edges: [] }
  }
  return convertToGraph(workspaceStore.projectDetail, {
    taskRuns: taskRuns.value,
    resultEventsByRun: resultEventsByRun.value,
    cutoffTime: cutoffTime.value,
  })
})

// 折叠会改变拓扑；搜索和状态过滤只改变可见性，不能触发重新布局。
const structuralGraphElements = computed(() => {
  const { nodes, edges } = rawGraphElements.value
  const collapsed = collapsePendingBranches(nodes, edges, {
    enabled: isBranchCollapseEnabled.value,
  })
  return applyManualCollapse(collapsed.nodes, collapsed.edges, manuallyCollapsedNodeIds.value)
})

// 语义时间轴先固定节点槽位；搜索、筛选和尺寸校正都不能重排已有节点。
watch(
  [structuralGraphElements, measuredNodeSizes],
  ([elements, sizes]) => {
    const projectId = currentProjectId.value || ''
    if (timelinePlacementProjectId !== projectId) {
      timelinePlacementProjectId = projectId
      timelinePlacements.value = {}
      previousNodeIdByLayoutKey.clear()
    }
    const result = layoutSemanticTimeline(elements.nodes, elements.edges, {
      measuredNodeSizes: sizes,
      previousPlacements: timelinePlacements.value,
    })
    timelinePlacements.value = result.placements
    baseLaidOutNodes.value = result.nodes
  },
  { immediate: true },
)

watch(baseLaidOutNodes, (nodes) => {
  const nextPositions = { ...userNodePositions.value }
  let changed = false
  nodes.forEach((node) => {
    const layoutKey = getNodeLayoutKey(node)
    const previousNodeId = previousNodeIdByLayoutKey.get(layoutKey)
    if (
      previousNodeId &&
      previousNodeId !== node.id &&
      isFinitePosition(nextPositions[previousNodeId]) &&
      !isFinitePosition(nextPositions[node.id])
    ) {
      nextPositions[node.id] = clonePosition(nextPositions[previousNodeId])
      delete nextPositions[previousNodeId]
      changed = true
    }
    previousNodeIdByLayoutKey.set(layoutKey, node.id)
  })
  if (changed) userNodePositions.value = nextPositions
})

const positionedStructuralNodes = computed<GraphNode[]>(() => {
  return baseLaidOutNodes.value.map((node) => {
    const userPosition = userNodePositions.value[node.id]
    if (!isFinitePosition(userPosition)) return node
    return {
      ...node,
      position: clonePosition(userPosition),
    }
  })
})

const visibleGraphElements = computed(() =>
  filterGraphElements(positionedStructuralNodes.value, structuralGraphElements.value.edges, {
    status: activeStatusFilter.value,
    query: searchQuery.value,
  }),
)

const laidOutNodes = computed<GraphNode[]>(() => visibleGraphElements.value.nodes)
const searchMatchCount = computed(() => visibleGraphElements.value.matchCount)

const graphBounds = computed<GraphBounds>(() => measureGraphBounds(laidOutNodes.value))

const graphCanvasSize = computed(() => {
  const bounds = graphBounds.value
  return {
    width: Math.ceil(
      Math.max(
        MIN_GRAPH_CANVAS_SIZE,
        bounds.maxX + GRAPH_CANVAS_PADDING,
        bounds.width + GRAPH_CANVAS_PADDING,
      ),
    ),
    height: Math.ceil(
      Math.max(
        MIN_GRAPH_CANVAS_SIZE,
        bounds.maxY + GRAPH_CANVAS_PADDING,
        bounds.height + GRAPH_CANVAS_PADDING,
      ),
    ),
  }
})

const {
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
} = useGraphViewport({
  viewportElement,
  graphBounds,
  graphCanvasSize,
  laidOutNodes,
})

const {
  draggingNodeId,
  suppressNodeClickId,
  startNodeDrag,
  onNodeDrag,
  endNodeDrag,
  cancelNodeDrag,
  resetNodeDrag,
} = useGraphNodeDrag(userNodePositions, zoom)

const shouldShowEdgeLabels = computed(() => {
  return !isZooming.value && zoom.value >= 0.32 && laidOutEdges.value.length <= 24
})

const isDenseEdgeView = computed(() => laidOutEdges.value.length > 24)

const shouldShowJoinBadges = computed(() => {
  return (
    !isZooming.value &&
    zoom.value >= 0.28 &&
    laidOutEdges.value.some((edge) => Number(edge.data?.renderedSourceCount || 0) > 1) &&
    laidOutEdges.value.length <= 36
  )
})

const laidOutEdges = computed<GraphEdge[]>(() => {
  const narrativeEdges = projectVisibleNarrativeEdges(
    visibleGraphElements.value.edges,
    laidOutNodes.value,
  )
  return compactGraphEdgesForRendering(narrativeEdges, laidOutNodes.value, {
    dir: layoutDir,
  })
})

const {
  selectedNodeId,
  selectedEdgeId,
  showNodeDrawer,
  showEdgeDrawer,
  evidenceList,
  selectedNode,
  selectedEdge,
  decisionResult,
  selectNodeFromClick,
  openNode,
  selectNode,
  selectNodeForSearch,
  selectEdge,
  closeDrawer,
  resetSelection,
} = useGraphSelection({
  currentProjectId,
  laidOutNodes,
  laidOutEdges,
  suppressNodeClickId,
})

watch(
  currentProjectId,
  (newVal) => {
    taskRunsAbortController?.abort()
    taskRunsAbortController = null
    taskRuns.value = []
    resultEventsByRun.value = {}
    userNodePositions.value = {}
    timelinePlacements.value = {}
    timelinePlacementProjectId = newVal || ''
    previousNodeIdByLayoutKey.clear()
    baseLaidOutNodes.value = []
    measuredNodeSizes.value = {}
    hasInitialFit.value = false
    taskRunsRequestId++
    manuallyCollapsedNodeIds.value = new Set()
    resetSelection()
    resetNodeDrag()
    resetPlayback()

    if (newVal) {
      fetchTaskRuns()
    }
  },
  { immediate: true },
)

// Fit viewport on load
watch(
  laidOutNodes,
  (nodes) => {
    if (nodes.length > 0 && !hasInitialFit.value) {
      hasInitialFit.value = true
      nextTick(() => {
        fitViewport()
      })
    }
  },
  { immediate: true },
)

const consumedRouteNodeKey = ref('')
watch(
  [() => route.query.nodeId, laidOutNodes, currentProjectId],
  async ([nodeId, nodes, projectId]) => {
    if (typeof nodeId !== 'string' || !nodeId || !projectId) {
      consumedRouteNodeKey.value = ''
      return
    }
    const routeKey = `${projectId}:${nodeId}`
    if (consumedRouteNodeKey.value === routeKey) return
    const node = nodes.find((item) => item.id === nodeId)
    if (!node) return
    consumedRouteNodeKey.value = routeKey
    await openNode(node)
    await nextTick()
    centerNodeInViewport(node)
  },
  { immediate: true },
)

const edgeCoordsMap = computed<Record<string, EdgeRoute>>(() => {
  return buildEdgeRoutes(laidOutNodes.value, laidOutEdges.value, layoutDir)
})

const isEdgeSelected = (edge: GraphEdge) =>
  selectedEdgeId.value === edge.id ||
  edge.data?.presentation?.representedIntentEdgeIds?.includes(selectedEdgeId.value || '') === true

const getEdgeGroupClasses = (edge: GraphEdge) => ({
  critical: edge.data?.isCriticalPath === true,
  selected: isEdgeSelected(edge),
  'is-dense': isDenseEdgeView.value,
  'is-join': Number(edge.data?.renderedSourceCount || 0) > 1,
  'is-active': isGraphEdgeRunning(edge, laidOutNodes.value),
  'is-reason-audit': edge.type === 'reason-audit',
  'is-reason-input': edge.data?.auditDirection === 'input',
  'is-reason-output': edge.data?.auditDirection === 'output',
  'is-muted': isMutedEdge(edge),
})

const isMutedEdge = (edge: GraphEdge) => {
  if (!isDenseEdgeView.value) return false
  if (isEdgeSelected(edge)) return false
  if (edge.data?.isCriticalPath) return false
  if (edge.data?.state === 'exploring') return false
  return true
}

const getEdgeStrokeWidth = (edge: GraphEdge) => {
  if (edge.data?.isCriticalPath || isEdgeSelected(edge)) return 2.2
  if (edge.type === 'reason-audit') return 1.35
  if (edge.data?.state === 'exploring') return 2
  if (isDenseEdgeView.value) return Number(edge.data?.renderedSourceCount || 0) > 1 ? 1.2 : 1
  return 1.5
}

const getEdgeJoinBadgeText = (edge: GraphEdge) => {
  const count = edge.data?.renderedSourceCount || edge.data?.joinGroup?.sourceCount || 1
  return edge.data?.presentation?.role === 'reason-input-summary' ? `+${count} inputs` : `+${count}`
}

const getEdgeJoinBadgeWidth = (edge: GraphEdge) => {
  return Math.max(24, getEdgeJoinBadgeText(edge).length * 7 + 10)
}

const getEdgeColor = (edge: GraphEdge) => {
  if (edge.data?.isCriticalPath) return 'var(--graph-edge-critical)'
  if (isEdgeSelected(edge)) return 'var(--graph-edge-selected)'
  if (isGraphEdgeRunning(edge, laidOutNodes.value)) return 'var(--graph-edge-active)'
  if (edge.type === 'reason-audit') {
    return edge.data?.auditDirection === 'input'
      ? 'var(--graph-edge-reason-input)'
      : 'var(--graph-edge-reason-output)'
  }
  if (Number(edge.data?.renderedSourceCount || 0) > 1 || edge.data?.joinGroup) {
    return 'var(--graph-edge-join)'
  }
  const state = edge.data?.state || 'unclaimed'
  if (state === 'concluded' || state === 'bootstrap') return 'var(--graph-edge-trunk)'
  return 'var(--graph-edge-muted)'
}

const getEdgeMarker = (edge: GraphEdge) => {
  if (edge.data?.isCriticalPath) return 'url(#arrow-critical)'
  if (isEdgeSelected(edge)) return 'url(#arrow-selected)'
  if (isGraphEdgeRunning(edge, laidOutNodes.value)) return 'url(#arrow-active)'
  if (edge.type === 'reason-audit') {
    return `url(#${getGraphEdgePresentation(edge).markerId})`
  }
  if (edge.data?.state === 'concluded' || edge.data?.state === 'bootstrap') {
    return 'url(#arrow-intent)'
  }
  return 'url(#arrow-normal)'
}

const { isGraphFullscreen, toggleFullscreen } = useGraphFullscreen(graphPageElement, fitViewport)

const onWindowResize = () => {
  syncViewportSize()
  if (isGraphFullscreen.value) fitViewport()
}

// Layout handlers
const toggleNodeCollapse = (nodeId: string) => {
  if (manuallyCollapsedNodeIds.value.has(nodeId)) {
    manuallyCollapsedNodeIds.value.delete(nodeId)
  } else {
    manuallyCollapsedNodeIds.value.add(nodeId)
  }
  manuallyCollapsedNodeIds.value = new Set(manuallyCollapsedNodeIds.value)
}

// Search
const searchMatchNodeIds = computed(() => {
  return laidOutNodes.value.filter((node) => node.data?.isSearchMatch).map((node) => node.id)
})

const runSearch = () => {
  if (!searchQuery.value.trim() || searchMatchNodeIds.value.length === 0) return
  const currentIndex = selectedNodeId.value
    ? searchMatchNodeIds.value.indexOf(selectedNodeId.value)
    : -1
  const nextId = searchMatchNodeIds.value[(currentIndex + 1) % searchMatchNodeIds.value.length]
  const nextNode = laidOutNodes.value.find((node) => node.id === nextId)
  if (!nextNode) return

  selectNodeForSearch(nextNode)
  centerNodeInViewport(nextNode)
}

const getNodeTypeLabel = getGraphNodeTypeLabel

// Route movements jump
const goToRun = (runId: string) => {
  if (!workspaceStore.currentProjectId) return
  closeDrawer()
  router.push(`/projects/${workspaceStore.currentProjectId}/runs?runId=${runId}`)
}

const previewArtifact = (artifactId: string) => {
  if (!workspaceStore.currentProjectId) return
  closeDrawer()
  router.push(`/projects/${workspaceStore.currentProjectId}/artifacts?artifactId=${artifactId}`)
}

onMounted(() => {
  window.addEventListener('resize', onWindowResize)
})

onUnmounted(() => {
  taskRunsAbortController?.abort()
  cleanupViewport()
  window.removeEventListener('resize', onWindowResize)
})

// Text utils
const truncateText = (text: string, len: number) => {
  if (!text) return ''
  return text.length > len ? text.slice(0, len) + '...' : text
}
</script>

<style src="./GraphPage.css"></style>
