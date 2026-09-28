import { computed, ref, type ComputedRef, type Ref } from 'vue'
import { api } from '@/api/client'
import type { GraphEdge, GraphEvidenceRecord, GraphNode } from './graphTypes'

interface GraphSelectionOptions {
  currentProjectId: ComputedRef<string | null | undefined>
  laidOutNodes: ComputedRef<GraphNode[]>
  laidOutEdges: ComputedRef<GraphEdge[]>
  suppressNodeClickId: Ref<string | null>
}

export function useGraphSelection({
  currentProjectId,
  laidOutNodes,
  laidOutEdges,
  suppressNodeClickId,
}: GraphSelectionOptions) {
  const selectedNodeId = ref<string | null>(null)
  const selectedEdgeId = ref<string | null>(null)
  const showNodeDrawer = ref(false)
  const showEdgeDrawer = ref(false)
  const evidenceList = ref<GraphEvidenceRecord[]>([])
  let evidenceRequestId = 0

  const selectedNode = computed(() => {
    if (!selectedNodeId.value) return null
    return laidOutNodes.value.find((node) => node.id === selectedNodeId.value) || null
  })

  const selectedEdge = computed(() => {
    if (!selectedEdgeId.value) return null
    return (
      laidOutEdges.value.find((edge) => edge.id === selectedEdgeId.value) ||
      laidOutEdges.value.find((edge) =>
        edge.data?.presentation?.representedIntentEdgeIds?.includes(selectedEdgeId.value || ''),
      ) ||
      null
    )
  })

  const selectNodeFromClick = (event: MouseEvent, node: GraphNode) => {
    if (suppressNodeClickId.value === node.id) {
      suppressNodeClickId.value = null
      event.preventDefault()
      return
    }
    selectNode(node)
  }

  const openNode = async (node: GraphNode) => {
    const requestId = ++evidenceRequestId
    evidenceList.value = []
    selectedEdgeId.value = null
    showEdgeDrawer.value = false
    selectedNodeId.value = node.id
    showNodeDrawer.value = true

    const projectId = currentProjectId.value
    const evidenceKinds = new Set(['fact', 'origin', 'goal'])
    if (projectId && evidenceKinds.has(node.data?.kind || 'fact')) {
      try {
        const evidence = await api<GraphEvidenceRecord[]>(
          'GET',
          `/projects/${projectId}/facts/${node.id}/evidence`,
        )
        if (
          requestId === evidenceRequestId &&
          currentProjectId.value === projectId &&
          selectedNodeId.value === node.id
        ) {
          evidenceList.value = evidence
        }
      } catch {
        if (requestId === evidenceRequestId) evidenceList.value = []
      }
    }
  }

  const selectNode = async (node: GraphNode) => {
    if (selectedNodeId.value === node.id && showNodeDrawer.value) {
      evidenceRequestId++
      evidenceList.value = []
      selectedNodeId.value = null
      showNodeDrawer.value = false
      return
    }
    await openNode(node)
  }

  const selectNodeForSearch = (node: GraphNode) => {
    evidenceRequestId++
    evidenceList.value = []
    selectedEdgeId.value = null
    selectedNodeId.value = node.id
    showNodeDrawer.value = false
    showEdgeDrawer.value = false
  }

  const selectEdge = (edge: GraphEdge) => {
    evidenceRequestId++
    evidenceList.value = []
    selectedNodeId.value = null
    showNodeDrawer.value = false

    const representsSelectedEdge =
      selectedEdgeId.value !== null &&
      edge.data?.presentation?.representedIntentEdgeIds?.includes(selectedEdgeId.value) === true
    if (selectedEdgeId.value === edge.id || representsSelectedEdge) {
      selectedEdgeId.value = null
      showEdgeDrawer.value = false
      return
    }
    selectedEdgeId.value = edge.data?.presentation?.representedIntentEdgeIds?.[0] || edge.id
    showEdgeDrawer.value = true
  }

  const closeDrawer = () => {
    evidenceRequestId++
    evidenceList.value = []
    selectedNodeId.value = null
    selectedEdgeId.value = null
    showNodeDrawer.value = false
    showEdgeDrawer.value = false
  }

  const resetSelection = () => {
    evidenceRequestId++
    evidenceList.value = []
    selectedNodeId.value = null
    selectedEdgeId.value = null
    showNodeDrawer.value = false
    showEdgeDrawer.value = false
  }

  const decisionResult = computed(() => {
    return selectedEdge.value?.data?.decisionResult || null
  })

  return {
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
  }
}
