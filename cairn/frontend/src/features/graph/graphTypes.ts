export type LayoutDir = 'LR' | 'TB'

export interface Position {
  x: number
  y: number
}

export interface GraphBounds {
  minX: number
  minY: number
  maxX: number
  maxY: number
  width: number
  height: number
}

export interface NodeDragState {
  nodeId: string
  pointerId: number
  startClientX: number
  startClientY: number
  startX: number
  startY: number
  moved: boolean
}

export type GraphNodeKind = 'origin' | 'goal' | 'fact' | 'pending' | 'pending_group' | 'reason'

export type GraphEdgeType = 'intent' | 'reason-audit'
export type ReasonAuditDirection = 'input' | 'output'

export interface ReasonOutputDescriptor {
  nodeId: string
  layoutKey: string
  intentId: string | null
}

export interface ReasonAuditData {
  runId: string
  worker: string
  status: string
  startedAt: string
  endedAt: string
  outcome: string
  rationale: string
  confidence: number | null
  blockedBy: unknown[]
  dependsOnTargets: unknown[]
  rejectedOptions: unknown[]
  inputNodeIds: string[]
  outputNodeIds: string[]
  outputDescriptors?: ReasonOutputDescriptor[]
  auditAvailable: boolean
  errorType: string
  errorMessage: string
}

export interface GraphNode {
  id: string
  layoutKey?: string
  type?: string
  data?: {
    kind?: GraphNodeKind
    status?: string
    label?: string
    isCriticalPath?: boolean
    liveState?: string
    runStatus?: string
    fact?: any
    intent?: any
    worker?: string
    creator?: string
    taskRunId?: string
    isSearchMatch?: boolean
    pendingGroup?: any
    collapsedDescendantsCount?: number
    reason?: ReasonAuditData
    [key: string]: any
  }
  position?: Position
}

export interface IntentNarrativeDescriptor {
  intentId: string
  canonicalEdgeIds: string[]
  label: string
  state: string
  worker: string
  creator: string
  taskRunId: string
  isCriticalPath: boolean
  intent?: any
}

export interface GraphEdgePresentationData {
  role: 'reason-output-intent' | 'reason-input-summary'
  reasonRunId: string
  representedIntentEdgeIds?: string[]
  narratives?: IntentNarrativeDescriptor[]
  inputNodeIds?: string[]
  hiddenInputEdgeIds?: string[]
}

export interface GraphEvidenceRecord {
  artifact_id: string
  artifact?: {
    uri?: string | null
  } | null
  created_at?: string | null
  [key: string]: unknown
}

export interface GraphEdge {
  id: string
  source: string
  target: string
  type?: GraphEdgeType
  data?: {
    state?: string
    auditDirection?: ReasonAuditDirection
    label?: string
    worker?: string
    taskRunId?: string
    intentId?: string
    intentIds?: string[]
    reasonRunId?: string
    reasonRunIds?: string[]
    reasonNodeId?: string
    isCriticalPath?: boolean
    intent?: any
    pendingGroup?: any
    mergedCount?: number
    renderedSourceCount?: number
    joinGroup?: {
      sourceCount?: number
      [key: string]: any
    }
    presentation?: GraphEdgePresentationData
    [key: string]: any
  }
}
