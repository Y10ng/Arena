import { splitGraphNodeSummary } from './nodeSizing'
import {
  buildReasonGraphRecords,
  type ReasonGraphRecord,
  type ResultEvent,
} from './panels/decisionModel'
import type { GraphNode, GraphEdge } from './graphFilters'

export { buildGraphPlaybackEvents } from './graphModelEvents'

const JOIN_EDGE_COLORS = ['#6f8ea3', '#7f8aa3', '#8b82a3', '#9a8780', '#789585', '#8b8f75']

export interface ProjectDetail {
  project?: {
    id: string
    title: string
    status: string
    [key: string]: any
  }
  facts?: any[]
  intents?: any[]
  [key: string]: any
}

export interface ConvertOptions {
  taskRuns?: any[]
  resultEventsByRun?: Record<string, ResultEvent[]>
  factEvidenceById?: Record<string, { items?: any[]; [key: string]: any }>
  cutoffTime?: string | null
  nowTime?: string | number | null
}

/**
 * 将 ProjectDetail 转换为 nodes 和 edges 结构。
 */
export function convertToGraph(
  projectDetail: ProjectDetail,
  options: ConvertOptions = {},
): { nodes: GraphNode[]; edges: GraphEdge[] } {
  if (!projectDetail) return { nodes: [], edges: [] }

  const {
    taskRuns = [],
    resultEventsByRun = {},
    factEvidenceById = {},
    cutoffTime = null,
    nowTime = null,
  } = options
  const nowMs = Number.isFinite(toTime(nowTime)) ? toTime(nowTime) : Date.now()

  const facts = projectDetail.facts || []
  const intents = projectDetail.intents || []
  const isProjectCompleted = projectDetail.project?.status === 'completed'
  const factById = new Map(facts.map((fact) => [fact.id, fact]))
  const taskRunById = new Map<string, any>(taskRuns.map((run) => [run.id, run]))
  const taskRunsByIntentId = new Map<string, any[]>()

  taskRuns.forEach((run) => {
    if (!run.intent_id) return
    if (!taskRunsByIntentId.has(run.intent_id)) {
      taskRunsByIntentId.set(run.intent_id, [])
    }
    taskRunsByIntentId.get(run.intent_id)!.push(run)
  })

  const reasonRecords = buildReasonGraphRecords(projectDetail, taskRuns, resultEventsByRun).filter(
    (record) => isReasonRunVisible(record.run, cutoffTime),
  )
  const reasonAssociationsByIntentId = buildReasonAssociationsByIntentId(reasonRecords)

  const nodes: GraphNode[] = []
  const edges: GraphEdge[] = []
  const addedNodeIds = new Set<string>()
  const visibleFactIds = new Set<string>()

  // 1. 处理真实 Fact 节点
  facts
    .filter((fact) => isFactVisible(fact, cutoffTime))
    .forEach((fact) => {
      let kind: 'fact' | 'origin' | 'goal' = 'fact'
      if (fact.id === 'origin') {
        kind = 'origin'
      } else if (fact.id === 'goal') {
        kind = 'goal'
      }

      visibleFactIds.add(fact.id)
      const meta = buildFactMeta(
        fact,
        intents,
        taskRunById,
        taskRunsByIntentId,
        factEvidenceById,
        nowMs,
      )

      nodes.push({
        id: fact.id,
        layoutKey: resolveFactLayoutKey(fact, intents, factById, cutoffTime),
        type: 'fact',
        data: {
          fact,
          label: fact.description,
          kind,
          status:
            fact.id === 'origin' || (fact.id === 'goal' && isProjectCompleted)
              ? 'confirmed'
              : 'normal',
          ...meta,
        },
        position: { x: 0, y: 0 },
      })
      addedNodeIds.add(fact.id)
    })

  // 2. 遍历 Intent，提取边及虚构节点（未领取/探索中）
  intents
    .filter((intent) => isIntentVisible(intent, cutoffTime))
    .forEach((intent) => {
      const concludedAtCutoff = isIntentConcludedAtCutoff(intent, factById, cutoffTime)

      // 确定边状态
      let state = 'unclaimed'
      if (intent.to && concludedAtCutoff) {
        if (intent.creator === 'dispatcher.bootstrap' || intent.description === 'bootstrap') {
          state = 'bootstrap'
        } else {
          state = 'concluded'
        }
      } else if (intent.worker) {
        state = 'exploring'
      }

      const fromIds = intent.from || []
      const visibleFromIds = fromIds.filter((fromId: string) => visibleFactIds.has(fromId))
      const toId = concludedAtCutoff ? intent.to : null
      const intentMeta = buildIntentMeta(intent, taskRunById, taskRunsByIntentId, nowMs)

      if (!toId) {
        // 探索中/未领取：生成占位虚构目标节点
        const virtualNodeId = `pending:${intent.id}`
        if (!addedNodeIds.has(virtualNodeId)) {
          nodes.push({
            id: virtualNodeId,
            layoutKey: `intent:${intent.id}`,
            type: 'fact',
            data: {
              intent,
              label: intent.description,
              kind: 'pending',
              status: state === 'exploring' ? 'exploring' : 'unclaimed',
              ...intentMeta,
              evidenceCount: 0,
            },
            position: { x: 0, y: 0 },
          })
          addedNodeIds.add(virtualNodeId)
        }

        addIntentEdges(edges, {
          intent,
          fromIds: visibleFromIds,
          targetId: virtualNodeId,
          state,
          intentMeta,
          reasonAssociation: reasonAssociationsByIntentId.get(intent.id) || null,
        })
      } else {
        // 已结论：从所有 from_i 指向 toId
        if (!visibleFactIds.has(toId)) return

        addIntentEdges(edges, {
          intent,
          fromIds: visibleFromIds,
          targetId: toId,
          state,
          intentMeta,
          reasonAssociation: reasonAssociationsByIntentId.get(intent.id) || null,
        })
      }
    })

  // 3. Reason TaskRun 作为审计旁路节点，不改变 Fact/Intent 主因果拓扑。
  const visibleNodeIdByLayoutKey = new Map<string, string>()
  nodes.forEach((node) => {
    const layoutKey = String(node.layoutKey || node.data?.layoutKey || '').trim()
    if (layoutKey) visibleNodeIdByLayoutKey.set(layoutKey, node.id)
  })

  reasonRecords.forEach((record) => {
    const visibleInputIds = record.inputNodeIds.filter((nodeId) => addedNodeIds.has(nodeId))
    const visibleOutputDescriptors = record.outputDescriptors.flatMap((output) => {
      const nodeId = addedNodeIds.has(output.nodeId)
        ? output.nodeId
        : visibleNodeIdByLayoutKey.get(output.layoutKey)
      return nodeId ? [{ ...output, nodeId }] : []
    })
    const visibleOutputIds = [...new Set(visibleOutputDescriptors.map((output) => output.nodeId))]
    const associatedIntentIds = [
      ...new Set(
        visibleOutputDescriptors
          .map((output) => output.intentId)
          .filter((id): id is string => Boolean(id)),
      ),
    ].sort(compareStableIds)
    const reasonData = {
      ...record.audit,
      inputNodeIds: visibleInputIds,
      outputNodeIds: visibleOutputIds,
      outputDescriptors: visibleOutputDescriptors,
    }

    nodes.push({
      id: record.nodeId,
      layoutKey: `reason:${record.run.id}`,
      type: 'reason',
      data: {
        kind: 'reason',
        status: record.audit.status,
        label: record.audit.rationale || `Reason 决策 ${record.run.id}`,
        reason: reasonData,
        taskRun: record.run,
        taskRunId: record.run.id,
        worker: record.audit.worker,
        runStatus: record.audit.status,
        createdAt: record.audit.startedAt,
        evidenceCount: 0,
      },
      position: { x: 0, y: 0 },
    })
    addedNodeIds.add(record.nodeId)

    visibleInputIds.forEach((sourceId) => {
      edges.push({
        id: `reason-input:${record.run.id}:${sourceId}`,
        source: sourceId,
        target: record.nodeId,
        type: 'reason-audit',
        data: {
          auditDirection: 'input',
          state: record.audit.status,
          label: 'Reason 输入',
          taskRunId: record.run.id,
          worker: record.audit.worker,
          reasonRunId: record.run.id,
          reasonNodeId: record.nodeId,
          intentIds: associatedIntentIds,
          ...(associatedIntentIds.length === 1 ? { intentId: associatedIntentIds[0] } : {}),
        },
      })
    })
    visibleOutputIds.forEach((targetId, index) => {
      const intentIds = [
        ...new Set(
          visibleOutputDescriptors
            .filter((output) => output.nodeId === targetId && output.intentId)
            .map((output) => output.intentId as string),
        ),
      ].sort(compareStableIds)
      edges.push({
        id: `reason-output:${record.run.id}:${targetId}:${index}`,
        source: record.nodeId,
        target: targetId,
        type: 'reason-audit',
        data: {
          auditDirection: 'output',
          state: record.audit.status,
          label: 'Reason 输出',
          taskRunId: record.run.id,
          worker: record.audit.worker,
          reasonRunId: record.run.id,
          reasonNodeId: record.nodeId,
          outputOrder: index,
          intentIds,
          ...(intentIds.length === 1 ? { intentId: intentIds[0] } : {}),
        },
      })
    })
  })

  annotateGraphTopology(nodes, edges)

  if (isProjectCompleted) {
    markCriticalPath(nodes, edges)
  }

  annotateGraphVisualSemantics(nodes, edges)

  return { nodes, edges }
}

function isReasonRunVisible(run: any, cutoffTime: string | null): boolean {
  if (!cutoffTime) return true
  const startedAt = run.started_at || run.created_at || run.ended_at
  return compareTime(startedAt, cutoffTime) <= 0
}

interface ReasonIntentAssociation {
  intentId: string
  reasonRunId: string
  reasonRunIds: string[]
  reasonNodeId: string
}

function buildReasonAssociationsByIntentId(
  records: ReasonGraphRecord[],
): Map<string, ReasonIntentAssociation> {
  const runIdsByIntentId = new Map<string, Set<string>>()
  records.forEach((record) => {
    record.outputDescriptors.forEach((output) => {
      if (!output.intentId) return
      if (!runIdsByIntentId.has(output.intentId)) runIdsByIntentId.set(output.intentId, new Set())
      runIdsByIntentId.get(output.intentId)!.add(record.run.id)
    })
  })

  const associations = new Map<string, ReasonIntentAssociation>()
  runIdsByIntentId.forEach((runIds, intentId) => {
    const reasonRunIds = [...runIds].sort(compareStableIds)
    const reasonRunId = reasonRunIds[0]
    associations.set(intentId, {
      intentId,
      reasonRunId,
      reasonRunIds,
      reasonNodeId: `reason:${reasonRunId}`,
    })
  })
  return associations
}

function isFactVisible(fact: any, cutoffTime: string | null): boolean {
  if (fact.id === 'origin' || fact.id === 'goal') return true
  if (!cutoffTime) return true
  return compareTime(fact.created_at, cutoffTime) <= 0
}

function isIntentVisible(intent: any, cutoffTime: string | null): boolean {
  if (!cutoffTime) return true
  return compareTime(intent.created_at, cutoffTime) <= 0
}

function isIntentConcludedAtCutoff(
  intent: any,
  factById: Map<string, any>,
  cutoffTime: string | null,
): boolean {
  if (!intent.to) return false
  if (!cutoffTime) return true

  const targetFact = factById.get(intent.to)
  const conclusionTime = intent.concluded_at || targetFact?.created_at || intent.created_at
  return compareTime(conclusionTime, cutoffTime) <= 0
}

function resolveFactLayoutKey(
  fact: any,
  intents: any[],
  factById: Map<string, any>,
  cutoffTime: string | null,
): string {
  const inboundIntentIds = [
    ...new Set(
      intents
        .filter(
          (intent) =>
            intent.id &&
            intent.to === fact.id &&
            isIntentVisible(intent, cutoffTime) &&
            isIntentConcludedAtCutoff(intent, factById, cutoffTime),
        )
        .map((intent) => String(intent.id)),
    ),
  ].sort(compareStableIds)
  return inboundIntentIds.length === 1 ? `intent:${inboundIntentIds[0]}` : `node:${fact.id}`
}

function buildFactMeta(
  fact: any,
  intents: any[],
  taskRunById: Map<string, any>,
  taskRunsByIntentId: Map<string, any[]>,
  factEvidenceById: any,
  nowMs: number,
) {
  const inboundIntent = intents.find((intent) => intent.to === fact.id) || null
  const taskRun = resolveTaskRun(fact.task_run_id, inboundIntent, taskRunById, taskRunsByIntentId)
  const evidenceCount = factEvidenceById[fact.id]?.items?.length || 0

  return {
    creator: fact.creator || inboundIntent?.creator || '',
    createdAt: fact.created_at || '',
    taskRun,
    taskRunId: fact.task_run_id || inboundIntent?.task_run_id || taskRun?.id || '',
    worker: taskRun?.worker_name || inboundIntent?.worker || '',
    runStatus: taskRun?.status || '',
    ...buildLiveMeta(inboundIntent, taskRun, nowMs),
    evidenceCount,
  }
}

function buildIntentMeta(
  intent: any,
  taskRunById: Map<string, any>,
  taskRunsByIntentId: Map<string, any[]>,
  nowMs: number,
) {
  const taskRun = resolveTaskRun(intent.task_run_id, intent, taskRunById, taskRunsByIntentId)

  return {
    creator: intent.creator || '',
    createdAt: intent.created_at || '',
    taskRun,
    taskRunId: intent.task_run_id || taskRun?.id || '',
    worker: intent.worker || taskRun?.worker_name || latestWorkerFromHistory(intent) || '',
    runStatus: taskRun?.status || '',
    ...buildLiveMeta(intent, taskRun, nowMs),
  }
}

function buildLiveMeta(intent: any, taskRun: any, nowMs: number) {
  const heartbeatAt = intent?.last_heartbeat_at || ''
  const startedAt = taskRun?.started_at || intent?.created_at || ''
  const heartbeatAgeSeconds = elapsedSeconds(heartbeatAt, nowMs)
  const elapsedTaskSeconds = elapsedSeconds(startedAt, nowMs)
  const runStatus = taskRun?.status || ''
  const isOpenIntent = Boolean(intent) && !intent.concluded_at && !intent.to
  const isRunning = runStatus === 'running' || (isOpenIntent && Boolean(intent?.worker))
  let liveState = ''
  if (isRunning) {
    liveState = heartbeatAt ? 'heartbeat' : 'running'
  }
  if (isRunning && heartbeatAgeSeconds !== null && heartbeatAgeSeconds > 120) {
    liveState = 'stale'
  }
  return {
    heartbeatAt,
    heartbeatAgeSeconds,
    elapsedTaskSeconds,
    liveState,
  }
}

function elapsedSeconds(startTime: any, nowMs: number): number | null {
  const startMs = toTime(startTime)
  if (!Number.isFinite(startMs) || !Number.isFinite(nowMs)) return null
  return Math.max(0, Math.round((nowMs - startMs) / 1000))
}

function resolveTaskRun(
  taskRunId: string | undefined,
  intent: any,
  taskRunById: Map<string, any>,
  taskRunsByIntentId: Map<string, any[]>,
) {
  if (taskRunId && taskRunById.has(taskRunId)) {
    return taskRunById.get(taskRunId)
  }
  const intentRuns = intent?.id ? taskRunsByIntentId.get(intent.id) || [] : []
  if (intentRuns.length === 0) return null
  return [...intentRuns].sort((a, b) => {
    const timeA = toTime(a.started_at || a.ended_at || '')
    const timeB = toTime(b.started_at || b.ended_at || '')
    return timeB - timeA
  })[0]
}

function latestWorkerFromHistory(intent: any): string {
  const history = intent.worker_history || []
  if (history.length === 0) return ''
  const latest = [...history].sort((a, b) => compareTime(b.cleared_at, a.cleared_at))[0]
  return latest?.worker || ''
}

function addIntentEdges(
  edges: GraphEdge[],
  {
    intent,
    fromIds,
    targetId,
    state,
    intentMeta,
    reasonAssociation,
  }: {
    intent: any
    fromIds: string[]
    targetId: string
    state: string
    intentMeta: any
    reasonAssociation: ReasonIntentAssociation | null
  },
) {
  const edgeIds = fromIds.map((fromId, index) => `edge-${intent.id}-${fromId}-${targetId}-${index}`)
  const joinGroup =
    fromIds.length > 1
      ? {
          id: `join:${intent.id}`,
          intentId: intent.id,
          sourceIds: [...fromIds],
          sourceCount: fromIds.length,
          targetId,
          color: stableJoinColor(intent.id),
          markerEdgeId: edgeIds[0],
        }
      : null

  fromIds.forEach((fromId, index) => {
    edges.push({
      id: edgeIds[index],
      source: fromId,
      target: targetId,
      type: 'intent',
      data: {
        intent,
        intentId: intent.id,
        state,
        label: intent.description,
        ...intentMeta,
        ...(reasonAssociation
          ? {
              reasonRunId: reasonAssociation.reasonRunId,
              reasonRunIds: reasonAssociation.reasonRunIds,
              reasonNodeId: reasonAssociation.reasonNodeId,
            }
          : {}),
        ...(joinGroup ? { joinGroup } : {}),
      },
    })
  })
}

function annotateGraphTopology(nodes: GraphNode[], edges: GraphEdge[]) {
  const incomingByTarget = new Map<string, Set<string>>()
  const outgoingIntentIdsBySource = new Map<string, Set<string>>()
  const incomingJoinGroupsByTarget = new Map<string, Map<string, any>>()

  edges.forEach((edge) => {
    if (edge.type !== 'intent') return
    if (!incomingByTarget.has(edge.target)) incomingByTarget.set(edge.target, new Set())
    incomingByTarget.get(edge.target)!.add(edge.id)

    const outgoingIntentId = edge.data?.intent?.id || edge.id
    if (!outgoingIntentIdsBySource.has(edge.source))
      outgoingIntentIdsBySource.set(edge.source, new Set())
    outgoingIntentIdsBySource.get(edge.source)!.add(outgoingIntentId)

    const joinGroup = edge.data?.joinGroup
    if (joinGroup?.targetId) {
      if (!incomingJoinGroupsByTarget.has(joinGroup.targetId))
        incomingJoinGroupsByTarget.set(joinGroup.targetId, new Map())
      incomingJoinGroupsByTarget.get(joinGroup.targetId)!.set(joinGroup.id, joinGroup)
    }
  })

  nodes.forEach((node) => {
    const inDegree = incomingByTarget.get(node.id)?.size || 0
    const outDegree = outgoingIntentIdsBySource.get(node.id)?.size || 0
    const joinGroups = [...(incomingJoinGroupsByTarget.get(node.id)?.values() || [])]
    node.data = {
      ...node.data,
      inDegree,
      outDegree,
      isBranchPoint: outDegree > 1,
      isJoinPoint: inDegree > 1,
      joinGroups,
      joinAccentColor: joinGroups[0]?.color || '',
      joinAccentColors: joinGroups
        .slice(0, 3)
        .map((group) => group.color)
        .filter(Boolean),
    }
  })
}

function stableJoinColor(value: any): string {
  const key = String(value || '')
  let hash = 0
  for (let index = 0; index < key.length; index += 1) {
    hash = (hash * 31 + key.charCodeAt(index)) >>> 0
  }
  return JOIN_EDGE_COLORS[hash % JOIN_EDGE_COLORS.length]
}

function annotateGraphVisualSemantics(nodes: GraphNode[], edges: GraphEdge[]) {
  const hasCriticalPath = edges.some((edge) => edge.data?.isCriticalPath === true)

  edges.forEach((edge) => {
    edge.data = {
      ...edge.data,
      edgeTier: resolveEdgeTier(edge),
      edgeTone: resolveEdgeTone(edge),
      hasCriticalPath,
    }
  })

  nodes.forEach((node) => {
    node.data = {
      ...node.data,
      ...buildNodeSummaryData(node),
    }
  })
}

function resolveEdgeTier(edge: GraphEdge): string {
  if (edge.type === 'reason-audit') return 'audit'
  if (edge.data?.isCriticalPath === true) return 'critical'
  const state = edge.data?.state || 'unclaimed'
  if (state === 'concluded' || state === 'bootstrap') return 'trunk'
  return 'exploratory'
}

function resolveEdgeTone(edge: GraphEdge): string {
  if (edge.type === 'reason-audit') {
    return edge.data?.auditDirection === 'input' ? 'reason-input' : 'reason-output'
  }
  if (edge.data?.isCriticalPath === true) return 'critical'
  if (edge.data?.joinGroup?.color) return 'join'
  const state = edge.data?.state || 'unclaimed'
  if (state === 'bootstrap') return 'bootstrap'
  if (state === 'concluded') return 'concluded'
  if (state === 'exploring') return 'exploring'
  return 'unclaimed'
}

function buildNodeSummaryData(node: GraphNode) {
  const data = node.data || {}
  const sourceText =
    data.reason?.rationale ||
    data.label ||
    data.fact?.description ||
    data.intent?.description ||
    node.id
  const summaryParts = splitGraphNodeSummary(sourceText)

  return {
    conclusion: summaryParts.conclusion,
    summary: summaryParts.summary,
    metaTags: buildNodeMetaTags(node, data),
  }
}

function buildNodeMetaTags(_node: GraphNode, data: any = {}) {
  const tags: any[] = []
  const kind = data.kind || 'fact'

  if (data.isCriticalPath === true) {
    tags.push(metaTag('关键路径', '', 'critical'))
  }

  if (kind === 'origin') {
    tags.push(metaTag('起点', '', 'fixed'))
  } else if (kind === 'goal') {
    tags.push(metaTag(data.status === 'confirmed' ? 'Goal 已确认' : 'Goal', '', 'goal'))
  } else if (kind === 'pending') {
    tags.push(
      metaTag(
        data.status === 'exploring' ? '探索中' : '未领取',
        '',
        data.status === 'exploring' ? 'live' : 'muted',
      ),
    )
  } else if (kind === 'pending_group') {
    const count = data.pendingGroup?.count || data.pendingCount || 0
    tags.push(metaTag('折叠分支', count ? String(count) : '', 'muted'))
  } else if (kind === 'reason') {
    tags.push(metaTag('Reason 决策', data.reason?.outcome || '', 'reason'))
    if (!data.reason?.auditAvailable) {
      tags.push(metaTag('审计', '不可用', 'muted'))
    }
  } else {
    tags.push(metaTag('Fact', '', 'fact'))
  }

  if ((data.inDegree || 0) > 1) {
    tags.push(metaTag('汇聚', String(data.inDegree), 'join', { color: data.joinAccentColor || '' }))
  }
  if ((data.outDegree || 0) > 1) {
    tags.push(metaTag('分叉', String(data.outDegree), 'branch'))
  }

  const liveState = data.liveState || ''
  if (liveState === 'stale') {
    tags.push(metaTag('心跳', '过期', 'warn'))
  } else if (liveState === 'heartbeat' || liveState === 'running') {
    tags.push(metaTag('心跳', '运行中', 'live'))
  }

  if (data.worker || data.creator) {
    tags.push(metaTag('执行器', data.worker || data.creator, 'worker'))
  }

  if (
    Number.isFinite(data.evidenceCount) &&
    !['pending', 'pending_group', 'reason'].includes(kind)
  ) {
    tags.push(metaTag('Evidence', String(data.evidenceCount), 'evidence'))
  }

  if (data.taskRunId) {
    tags.push(metaTag('Run', data.taskRunId, 'run'))
  }

  return tags.slice(0, 6)
}

function metaTag(label: string, value = '', tone = 'muted', options: any = {}) {
  return {
    label,
    value,
    tone,
    color: options.color || '',
    title: value ? `${label} ${value}` : label,
  }
}

function markCriticalPath(nodes: GraphNode[], edges: GraphEdge[]) {
  const incoming = new Map<string, GraphEdge[]>()
  edges.forEach((edge) => {
    if (edge.type !== 'intent') return
    if (!['concluded', 'bootstrap'].includes(edge.data?.state || '')) return
    if (!incoming.has(edge.target)) {
      incoming.set(edge.target, [])
    }
    incoming.get(edge.target)!.push(edge)
  })

  const nodeIds = new Set<string>()
  const edgeIds = new Set<string>()
  const stack = ['goal']
  const visited = new Set<string>()

  while (stack.length > 0) {
    const nodeId = stack.pop()
    if (!nodeId || visited.has(nodeId)) continue
    visited.add(nodeId)
    nodeIds.add(nodeId)
    const inboundEdges = incoming.get(nodeId) || []
    inboundEdges.forEach((edge) => {
      edgeIds.add(edge.id)
      nodeIds.add(edge.source)
      stack.push(edge.source)
    })
  }

  if (!nodeIds.has('origin')) return

  nodes.forEach((node) => {
    if (!nodeIds.has(node.id)) return
    node.data = { ...node.data, isCriticalPath: true }
  })
  edges.forEach((edge) => {
    if (!edgeIds.has(edge.id)) return
    edge.data = { ...edge.data, isCriticalPath: true }
  })
}

function compareTime(left: any, right: any): number {
  return toTime(left) - toTime(right)
}

function compareStableIds(left: string, right: string): number {
  return left < right ? -1 : left > right ? 1 : 0
}

function toTime(value: any): number {
  if (!value) return Number.POSITIVE_INFINITY
  const parsed = Date.parse(value)
  return Number.isFinite(parsed) ? parsed : Number.POSITIVE_INFINITY
}
