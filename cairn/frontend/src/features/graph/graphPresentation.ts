import {
  CircleDot,
  Compass,
  FileCheck2,
  Layers3,
  Lightbulb,
  Target,
  type LucideIcon,
} from '@lucide/vue'
import { statusMeta } from '@/lib/format'
import type { GraphEdge, GraphEdgeType, GraphNode, GraphNodeKind } from './graphTypes'

export interface GraphNodePresentation {
  kind: GraphNodeKind
  label: string
  shortLabel: string
  icon: LucideIcon
  tone: string
}

export interface GraphStatusPresentation {
  key: string
  label: string
  tone: string
  isRunning: boolean
}

export interface GraphEdgePresentation {
  key: 'intent' | 'reason-input' | 'reason-output'
  label: string
  tone: string
  dashArray: string
  markerId: string
}

export const GRAPH_NODE_KINDS: readonly GraphNodeKind[] = [
  'origin',
  'goal',
  'fact',
  'pending',
  'pending_group',
  'reason',
]

export const graphNodePresentation: Record<GraphNodeKind, GraphNodePresentation> = {
  origin: { kind: 'origin', label: '起点', shortLabel: '起点', icon: CircleDot, tone: 'origin' },
  goal: { kind: 'goal', label: '目标', shortLabel: '目标', icon: Target, tone: 'goal' },
  fact: { kind: 'fact', label: 'Fact 事实', shortLabel: 'Fact', icon: FileCheck2, tone: 'fact' },
  pending: {
    kind: 'pending',
    label: '探索意图',
    shortLabel: '探索意图',
    icon: Compass,
    tone: 'pending',
  },
  pending_group: {
    kind: 'pending_group',
    label: '待定分支',
    shortLabel: '待定分支',
    icon: Layers3,
    tone: 'pending-group',
  },
  reason: {
    kind: 'reason',
    label: 'Reason 决策',
    shortLabel: 'Reason',
    icon: Lightbulb,
    tone: 'reason',
  },
}

export const graphEdgePresentation: Record<
  'intent' | 'reason-input' | 'reason-output',
  GraphEdgePresentation
> = {
  intent: {
    key: 'intent',
    label: 'Intent',
    tone: 'intent',
    dashArray: 'none',
    markerId: 'arrow-intent',
  },
  'reason-input': {
    key: 'reason-input',
    label: 'Reason 输入',
    tone: 'reason-input',
    dashArray: '3,5',
    markerId: 'arrow-reason-input',
  },
  'reason-output': {
    key: 'reason-output',
    label: 'Reason 输出',
    tone: 'reason-output',
    dashArray: '9,5',
    markerId: 'arrow-reason-output',
  },
}

export function getGraphNodePresentation(
  nodeOrKind: GraphNode | GraphNodeKind,
): GraphNodePresentation {
  const kind = typeof nodeOrKind === 'string' ? nodeOrKind : nodeOrKind.data?.kind || 'fact'
  return graphNodePresentation[kind]
}

export function getGraphNodeTypeLabel(node: GraphNode): string {
  return getGraphNodePresentation(node).label
}

export function resolveGraphNodeStatus(node: GraphNode): GraphStatusPresentation {
  const liveState = normalizeStatus(node.data?.liveState)
  const runStatus = normalizeStatus(node.data?.runStatus)
  const nodeStatus = normalizeStatus(node.data?.status)

  if (liveState === 'stale') return buildStatus('stale', false)
  if (['failed', 'timed_out'].includes(runStatus)) return buildStatus(runStatus, false)
  if (runStatus === 'cancelled') return buildStatus(runStatus, false)
  if (liveState === 'running' || liveState === 'heartbeat' || runStatus === 'running') {
    return buildStatus(liveState === 'heartbeat' ? 'heartbeat' : 'running', true)
  }
  if (runStatus) return buildStatus(runStatus, false)
  if (nodeStatus) return buildStatus(nodeStatus, nodeStatus === 'exploring')
  return buildStatus('unknown', false)
}

export function getGraphEdgePresentation(edge: GraphEdge): GraphEdgePresentation {
  if (edge.type !== ('reason-audit' satisfies GraphEdgeType)) return graphEdgePresentation.intent
  return edge.data?.auditDirection === 'input'
    ? graphEdgePresentation['reason-input']
    : graphEdgePresentation['reason-output']
}

export function isGraphEdgeRunning(edge: GraphEdge, nodes: readonly GraphNode[] = []): boolean {
  if (edge.data?.state === 'exploring' || edge.data?.state === 'running') return true
  const target = nodes.find((node) => node.id === edge.target)
  return target ? resolveGraphNodeStatus(target).isRunning : false
}

function normalizeStatus(value: unknown): string {
  return typeof value === 'string' ? value.trim().toLowerCase() : ''
}

function buildStatus(key: string, isRunning: boolean): GraphStatusPresentation {
  const meta = statusMeta(key)
  return {
    key,
    label: key === 'heartbeat' ? '心跳正常' : meta.text,
    tone: key === 'heartbeat' ? 'good' : meta.tone,
    isRunning,
  }
}
