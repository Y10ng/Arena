import type {
  Fact,
  FactEvidence,
  TaskRunResultEvent,
  TaskRunResultEventGroup,
  TaskRunResultEventKind,
} from '@/api/types'
import type { BadgeTone } from '@/components/ui'

export type AuditEventFilter = 'all' | TaskRunResultEventKind

export interface AuditEventKindMeta {
  label: string
  tone: BadgeTone
}

export interface AuditEventFilterOption {
  value: AuditEventFilter
  label: string
}

export interface AuditKpis {
  validation: Record<'valid' | 'invalid' | 'rejected' | 'unknown', number>
  protocolWrite: Record<'success' | 'failed' | 'skipped' | 'unknown', number>
  decision: Record<'complete' | 'fact' | 'intents' | 'noop' | 'unknown', number>
}

export interface EvidenceValidityMeta {
  label: string
  tone: BadgeTone
}

export type AuditEvidenceLoadResult =
  | {
      status: 'success'
      fact: Fact
      evidence: readonly FactEvidence[]
    }
  | {
      status: 'error'
      fact: Fact
      error: unknown
    }

interface EvidenceRowBase {
  key: string
  projectId: string
  fact: Fact
}

export interface EvidenceBindingRow extends EvidenceRowBase {
  kind: 'evidence'
  evidence: FactEvidence
  validity: EvidenceValidityMeta
}

export interface EvidenceNoneRow extends EvidenceRowBase {
  kind: 'none'
}

export interface EvidenceErrorRow extends EvidenceRowBase {
  kind: 'error'
  error: unknown
}

export type AuditEvidenceRow = EvidenceBindingRow | EvidenceNoneRow | EvidenceErrorRow

export const AUDIT_EVENT_KINDS = [
  'validation_result',
  'protocol_write',
  'network_flow',
  'decision_result',
] as const satisfies readonly TaskRunResultEventKind[]

export const AUDIT_EVENT_KIND_META = {
  validation_result: { label: '结果校验', tone: 'success' },
  protocol_write: { label: '协议写入', tone: 'info' },
  network_flow: { label: '网络流量', tone: 'warning' },
  decision_result: { label: '推理决策', tone: 'neutral' },
} as const satisfies Record<TaskRunResultEventKind, AuditEventKindMeta>

export const AUDIT_EVENT_FILTER_OPTIONS: readonly AuditEventFilterOption[] = [
  { value: 'all', label: '全部事件' },
  ...AUDIT_EVENT_KINDS.map((kind) => ({
    value: kind,
    label: AUDIT_EVENT_KIND_META[kind].label,
  })),
]

const timestampValue = (value: string): number | null => {
  const timestamp = Date.parse(value)
  return Number.isFinite(timestamp) ? timestamp : null
}

const payloadText = (value: unknown, fallback = '未知'): string => {
  if (typeof value === 'string') return value
  if (typeof value === 'number' && Number.isFinite(value)) return String(value)
  if (typeof value === 'boolean') return String(value)
  if (value === null || value === undefined) return fallback

  try {
    return JSON.stringify(value)
  } catch {
    return String(value)
  }
}

const appendPayloadPart = (
  parts: string[],
  label: string,
  payload: Record<string, unknown>,
  key: string,
): void => {
  if (payload[key] !== undefined && payload[key] !== null) {
    parts.push(`${label}: ${payloadText(payload[key])}`)
  }
}

const appendPayloadValue = (parts: string[], label: string, value: unknown): void => {
  if (value !== undefined && value !== null) {
    parts.push(`${label}: ${payloadText(value)}`)
  }
}

const summaryWithParts = (title: string, parts: readonly string[]): string =>
  parts.length > 0 ? `${title}: ${parts.join(' · ')}` : title

export const auditEventKindMeta = (kind: TaskRunResultEventKind): AuditEventKindMeta =>
  AUDIT_EVENT_KIND_META[kind]

export const flattenResultEventGroups = (
  groups: readonly TaskRunResultEventGroup[],
): TaskRunResultEvent[] => groups.flatMap((group) => group.events)

export const filterAndSortAuditEvents = (
  events: readonly TaskRunResultEvent[],
  filter: AuditEventFilter,
): TaskRunResultEvent[] =>
  events
    .map((event, sourceIndex) => ({ event, sourceIndex, timestamp: timestampValue(event.ts) }))
    .filter(({ event }) => filter === 'all' || event.kind === filter)
    .sort((left, right) => {
      if (left.timestamp !== null && right.timestamp !== null) {
        return right.timestamp - left.timestamp || left.sourceIndex - right.sourceIndex
      }
      if (left.timestamp !== null) return -1
      if (right.timestamp !== null) return 1
      return left.sourceIndex - right.sourceIndex
    })
    .map(({ event }) => event)

export const reconcileExpandedEventId = (
  expandedEventId: string | null,
  events: readonly TaskRunResultEvent[],
): string | null =>
  expandedEventId !== null && events.some((event) => event.id === expandedEventId)
    ? expandedEventId
    : null

export const formatAuditConfidence = (value: unknown): string => {
  if (typeof value === 'boolean' || value === null || value === undefined) return '未知'
  if (typeof value !== 'number' && typeof value !== 'string') return '未知'
  if (typeof value === 'string' && value.trim() === '') return '未知'

  const confidence = typeof value === 'number' ? value : Number(value)
  if (!Number.isFinite(confidence) || confidence < 0 || confidence > 1) return '未知'
  return `${Math.round(confidence * 100)}%`
}

export const auditEventSummary = (event: TaskRunResultEvent): string => {
  const { payload } = event

  if (event.kind === 'validation_result') {
    const parts = [`状态: ${payloadText(payload.status)}`]
    appendPayloadPart(parts, '阶段', payload, 'phase')
    appendPayloadPart(parts, '结果', payload, 'outcome')
    appendPayloadPart(parts, '结果类型', payload, 'result_kind')
    appendPayloadPart(parts, '拒绝原因', payload, 'rejection_reason')
    appendPayloadPart(parts, '错误', payload, 'error_message')
    appendPayloadPart(parts, '规范化', payload, 'canonicalization')
    appendPayloadPart(parts, '警告', payload, 'warnings')
    return summaryWithParts(AUDIT_EVENT_KIND_META[event.kind].label, parts)
  }

  if (event.kind === 'protocol_write') {
    const parts = [`状态: ${payloadText(payload.status)}`]
    appendPayloadPart(parts, '操作', payload, 'operation')
    appendPayloadPart(parts, '阶段', payload, 'phase')
    appendPayloadPart(parts, 'Fact', payload, 'fact_id')
    appendPayloadPart(parts, 'Intent', payload, 'intent_id')
    appendPayloadPart(parts, '来源', payload, 'from_ids')
    appendPayloadPart(parts, '新建 ID', payload, 'created_id')
    appendPayloadPart(parts, '响应状态码', payload, 'status_code')
    appendPayloadPart(parts, '响应', payload, 'response_text')
    return summaryWithParts(AUDIT_EVENT_KIND_META[event.kind].label, parts)
  }

  if (event.kind === 'decision_result') {
    const parts = [`结果: ${payloadText(payload.outcome)}`]
    appendPayloadPart(parts, '阶段', payload, 'phase')
    parts.push(`置信度: ${formatAuditConfidence(payload.confidence)}`)
    appendPayloadPart(parts, '理由', payload, 'rationale')
    appendPayloadPart(parts, '来源', payload, 'from_ids')
    appendPayloadPart(parts, '新建 Intents', payload, 'created_intent_ids')
    appendPayloadPart(parts, '完成 Fact', payload, 'completed_fact_id')
    appendPayloadPart(parts, '拒绝选项', payload, 'rejected_options')
    appendPayloadPart(parts, '阻塞项', payload, 'blocked_by')
    appendPayloadPart(parts, '依赖目标', payload, 'depends_on_targets')
    appendPayloadPart(parts, '写入状态', payload, 'write_status')
    appendPayloadPart(parts, '错误', payload, 'error_message')
    return summaryWithParts(AUDIT_EVENT_KIND_META[event.kind].label, parts)
  }

  const parts: string[] = []
  appendPayloadValue(parts, '状态', payload.status ?? payload.action ?? payload.outcome)
  appendPayloadPart(parts, '阶段', payload, 'phase')
  appendPayloadValue(parts, '网络平面', payload.network_plane ?? payload.plane)
  appendPayloadValue(parts, '协议', payload.protocol ?? payload.transport ?? payload.scheme)
  appendPayloadPart(parts, 'Flow ID', payload, 'flow_id')
  appendPayloadPart(parts, 'Artifact', payload, 'artifact_id')
  appendPayloadPart(parts, 'Artifact 角色', payload, 'artifact_role')
  appendPayloadPart(parts, '接收字节', payload, 'bytes_received')
  appendPayloadValue(parts, '字节数', payload.byte_count ?? payload.bytes)
  appendPayloadPart(parts, '错误', payload, 'error_message')
  return summaryWithParts(AUDIT_EVENT_KIND_META[event.kind].label, parts)
}

export const calculateAuditKpis = (events: readonly TaskRunResultEvent[]): AuditKpis => {
  const counts: AuditKpis = {
    validation: { valid: 0, invalid: 0, rejected: 0, unknown: 0 },
    protocolWrite: { success: 0, failed: 0, skipped: 0, unknown: 0 },
    decision: { complete: 0, fact: 0, intents: 0, noop: 0, unknown: 0 },
  }

  for (const event of events) {
    if (event.kind === 'validation_result') {
      const status = event.payload.status
      if (status === 'valid' || status === 'invalid' || status === 'rejected') {
        counts.validation[status] += 1
      } else {
        counts.validation.unknown += 1
      }
    } else if (event.kind === 'protocol_write') {
      const status = event.payload.status
      if (status === 'success' || status === 'failed' || status === 'skipped') {
        counts.protocolWrite[status] += 1
      } else {
        counts.protocolWrite.unknown += 1
      }
    } else if (event.kind === 'decision_result') {
      const outcome = event.payload.outcome
      if (
        outcome === 'complete' ||
        outcome === 'fact' ||
        outcome === 'intents' ||
        outcome === 'noop'
      ) {
        counts.decision[outcome] += 1
      } else {
        counts.decision.unknown += 1
      }
    }
  }

  return counts
}

export const auditEvidenceStatus = (
  evidence: Pick<FactEvidence, 'evidence_valid' | 'artifact_exists' | 'verifier_run_exists'>,
): EvidenceValidityMeta => {
  if (evidence.evidence_valid) return { label: '证据有效', tone: 'success' }
  if (!evidence.artifact_exists && evidence.verifier_run_exists === false) {
    return { label: 'Artifact 与 verifier Run 均已删除，证据无效', tone: 'danger' }
  }
  if (!evidence.artifact_exists) {
    return { label: 'Artifact 已删除，证据无效', tone: 'danger' }
  }
  if (evidence.verifier_run_exists === false) {
    return { label: 'Verifier Run 已删除，证据无效', tone: 'danger' }
  }
  return { label: '证据无效', tone: 'danger' }
}

const evidenceRowKey = (
  projectId: string,
  factId: string,
  kind: AuditEvidenceRow['kind'],
  artifactId = '',
  createdAt = '',
  pointer = '',
): string => JSON.stringify([projectId, factId, kind, artifactId, createdAt, pointer])

export const buildAuditEvidenceRows = (
  projectId: string,
  results: readonly AuditEvidenceLoadResult[],
): AuditEvidenceRow[] =>
  results.flatMap((result): AuditEvidenceRow[] => {
    if (result.status === 'error') {
      return [
        {
          kind: 'error',
          key: evidenceRowKey(projectId, result.fact.id, 'error'),
          projectId,
          fact: result.fact,
          error: result.error,
        },
      ]
    }

    if (result.evidence.length === 0) {
      return [
        {
          kind: 'none',
          key: evidenceRowKey(projectId, result.fact.id, 'none'),
          projectId,
          fact: result.fact,
        },
      ]
    }

    return result.evidence.map((evidence) => ({
      kind: 'evidence',
      key: evidenceRowKey(
        projectId,
        result.fact.id,
        'evidence',
        evidence.artifact_id,
        evidence.created_at,
        evidence.pointer,
      ),
      projectId,
      fact: result.fact,
      evidence,
      validity: auditEvidenceStatus(evidence),
    }))
  })
