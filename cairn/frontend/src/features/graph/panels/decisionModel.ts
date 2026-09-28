import type { ReasonAuditData, ReasonOutputDescriptor } from '../graphTypes';
import type { ProjectDetail } from '../useGraphModel';

export interface TaskRun {
  id: string;
  started_at?: string | null;
  ended_at?: string | null;
  created_at?: string;
  task_type?: string;
  worker_name?: string;
  intent_id?: string | null;
  status?: string;
  error_type?: string | null;
  error_message?: string | null;
  [key: string]: any;
}

export interface ResultEvent {
  kind: string;
  payload?: {
    operation?: string;
    fact_id?: string;
    completed_fact_id?: string;
    created_id?: string;
    created_intent_ids?: string[];
    outcome?: string;
    write_status?: string;
    rationale?: string;
    rejected_options?: any[];
    confidence?: number;
    blocked_by?: any[];
    depends_on_targets?: any[];
    from_ids?: string[];
    [key: string]: any;
  };
  [key: string]: any;
}

export function sortTaskRunsByTime(taskRuns: TaskRun[] = []): TaskRun[] {
  return [...taskRuns].sort((a, b) => {
    const timeA = a.started_at || a.created_at || '';
    const timeB = b.started_at || b.created_at || '';
    return timeA.localeCompare(timeB);
  });
}

export function getRunEvents(resultEventsByRun: Record<string, ResultEvent[]> = {}, run: TaskRun): ResultEvent[] {
  return resultEventsByRun[run.id] || [];
}

export function getDecisionEvent(events: ResultEvent[] = []): ResultEvent | null {
  return [...events].reverse().find(event => event.kind === 'decision_result') || null;
}

export function getProtocolWrites(events: ResultEvent[] = [], operation: string | null = null): ResultEvent[] {
  return events.filter(event => {
    if (event.kind !== 'protocol_write') return false;
    return operation ? event.payload?.operation === operation : true;
  });
}

export function getCreatedFacts(project: ProjectDetail, run: TaskRun, events: ResultEvent[] = []): any[] {
  const direct = (project.facts || []).filter(fact => fact.task_run_id === run.id);
  if (direct.length > 0) return direct;

  const factIds = new Set<string>();
  getProtocolWrites(events).forEach(event => {
    if (event.payload?.fact_id) factIds.add(event.payload.fact_id);
  });
  const decision = getDecisionEvent(events);
  if (decision?.payload?.completed_fact_id) {
    factIds.add(decision.payload.completed_fact_id);
  }

  return (project.facts || []).filter(fact => factIds.has(fact.id));
}

export function getCreatedIntents(project: ProjectDetail, run: TaskRun, events: ResultEvent[] = []): any[] {
  const direct = (project.intents || []).filter(intent => intent.task_run_id === run.id);
  if (direct.length > 0) return direct;

  const intentIds = new Set<string>();
  getProtocolWrites(events, 'create_intent').forEach(event => {
    if (event.payload?.created_id) intentIds.add(event.payload.created_id);
  });
  const decision = getDecisionEvent(events);
  if (Array.isArray(decision?.payload?.created_intent_ids)) {
    decision.payload.created_intent_ids.forEach(id => intentIds.add(id));
  }

  return (project.intents || []).filter(intent => intentIds.has(intent.id));
}

export interface ReasonGraphRecord {
  nodeId: string;
  run: TaskRun;
  inputNodeIds: string[];
  outputNodeIds: string[];
  outputDescriptors: ReasonOutputDescriptor[];
  audit: ReasonAuditData;
}

export function buildReasonGraphRecords(
  project: ProjectDetail,
  taskRuns: TaskRun[] = [],
  resultEventsByRun: Record<string, ResultEvent[]> = {},
): ReasonGraphRecord[] {
  return sortTaskRunsByTime(taskRuns)
    .filter(run => run.task_type === 'reason')
    .map(run => {
      const events = getRunEvents(resultEventsByRun, run);
      const decision = getDecisionEvent(events);
      const payload = decision?.payload || {};
      const createdIntents = getCreatedIntents(project, run, events);
      const createdFacts = getCreatedFacts(project, run, events);
      const inputNodeIds = new Set<string>();
      const outputDescriptors: ReasonOutputDescriptor[] = [];

      createdIntents.forEach(intent => {
        (intent.from || []).forEach((id: string) => inputNodeIds.add(id));
        outputDescriptors.push({
          nodeId: intent.to || `pending:${intent.id}`,
          layoutKey: `intent:${intent.id}`,
          intentId: intent.id,
        });
      });
      if (inputNodeIds.size === 0 && Array.isArray(payload.from_ids)) {
        payload.from_ids.forEach(id => inputNodeIds.add(id));
      }

      if (payload.outcome === 'fact') {
        createdFacts.forEach(fact => outputDescriptors.push({
          nodeId: fact.id,
          layoutKey: resolveOutputLayoutKey(project, fact.id),
          intentId: resolveUniqueInboundIntentId(project, fact.id),
        }));
      } else if (payload.outcome === 'complete') {
        const nodeId = payload.completed_fact_id || 'goal';
        outputDescriptors.push({
          nodeId,
          layoutKey: resolveOutputLayoutKey(project, nodeId),
          intentId: resolveUniqueInboundIntentId(project, nodeId),
        });
      }

      const orderedOutputDescriptors = uniqueOutputDescriptors(outputDescriptors)
        .sort(compareReasonOutputDescriptors);
      const orderedInputNodeIds = [...inputNodeIds].sort(compareStableIds);
      const orderedOutputNodeIds = [...new Set(orderedOutputDescriptors.map(output => output.nodeId))];

      const audit: ReasonAuditData = {
        runId: run.id,
        worker: run.worker_name || '',
        status: run.status || '',
        startedAt: run.started_at || run.created_at || '',
        endedAt: run.ended_at || '',
        outcome: payload.outcome || '',
        rationale: payload.rationale || '',
        confidence: typeof payload.confidence === 'number' ? payload.confidence : null,
        blockedBy: Array.isArray(payload.blocked_by) ? payload.blocked_by : [],
        dependsOnTargets: Array.isArray(payload.depends_on_targets)
          ? payload.depends_on_targets
          : [],
        rejectedOptions: Array.isArray(payload.rejected_options) ? payload.rejected_options : [],
        inputNodeIds: orderedInputNodeIds,
        outputNodeIds: orderedOutputNodeIds,
        outputDescriptors: orderedOutputDescriptors,
        auditAvailable: Boolean(decision),
        errorType: run.error_type || '',
        errorMessage: run.error_message || '',
      };

      return {
        nodeId: `reason:${run.id}`,
        run,
        inputNodeIds: audit.inputNodeIds,
        outputNodeIds: audit.outputNodeIds,
        outputDescriptors: orderedOutputDescriptors,
        audit,
      };
    });
}

function resolveUniqueInboundIntentId(project: ProjectDetail, nodeId: string): string | null {
  const intentIds = [...new Set(
    (project.intents || [])
      .filter(intent => intent.to === nodeId && intent.id)
      .map(intent => String(intent.id))
  )].sort(compareStableIds);
  return intentIds.length === 1 ? intentIds[0] : null;
}

function resolveOutputLayoutKey(project: ProjectDetail, nodeId: string): string {
  const intentId = resolveUniqueInboundIntentId(project, nodeId);
  return intentId ? `intent:${intentId}` : `node:${nodeId}`;
}

function uniqueOutputDescriptors(outputs: ReasonOutputDescriptor[]): ReasonOutputDescriptor[] {
  const byAssociation = new Map<string, ReasonOutputDescriptor>();
  outputs.forEach(output => {
    const key = `${output.nodeId}::${output.intentId || ''}`;
    if (!byAssociation.has(key)) byAssociation.set(key, output);
  });
  return [...byAssociation.values()];
}

function compareReasonOutputDescriptors(left: ReasonOutputDescriptor, right: ReasonOutputDescriptor): number {
  return compareStableIds(left.layoutKey, right.layoutKey)
    || compareStableIds(left.nodeId, right.nodeId)
    || compareStableIds(left.intentId || '', right.intentId || '');
}

function compareStableIds(left: string, right: string): number {
  return left < right ? -1 : left > right ? 1 : 0;
}

export function getDecisionText(events: ResultEvent[] = []): string {
  const event = getDecisionEvent(events);
  if (!event) return '';
  const payload = event.payload || {};
  const labels: Record<string, string> = {
    complete: '判定目标完成',
    fact: '写入新 Fact',
    intents: '提出新意图',
    noop: '本轮不变更图谱'
  };
  const label = labels[payload.outcome || ''] || payload.outcome || '决策结果';
  const status = payload.write_status ? `写入: ${payload.write_status}` : '';
  const rationale = payload.rationale ? `理由: ${payload.rationale}` : '';
  const rejected = Array.isArray(payload.rejected_options) && payload.rejected_options.length > 0
    ? `否决 ${payload.rejected_options.length} 项`
    : '';
  const confidence = typeof payload.confidence === 'number'
    ? `置信度: ${Math.round(payload.confidence * 100)}%`
    : '';
  const blocked = Array.isArray(payload.blocked_by) && payload.blocked_by.length > 0
    ? `阻塞 ${payload.blocked_by.length} 项`
    : '';
  const targets = Array.isArray(payload.depends_on_targets) && payload.depends_on_targets.length > 0
    ? `依赖 Target ${payload.depends_on_targets.length} 个`
    : '';
  return [label, status, rationale, confidence, blocked, targets, rejected].filter(Boolean).join(' · ');
}

const DECISION_OVERLAY_TITLE: Record<string, string> = {
  complete: '为什么完成',
  fact: '为什么写入',
  intents: '为什么分叉',
  noop: '为什么不变更',
};

const DECISION_OVERLAY_SUMMARY_LIMIT = 120;

export interface DecisionOverlay {
  nodeId: string;
  runId: string;
  taskType: string;
  workerName: string;
  outcome: string;
  title: string;
  summary: string;
}

export function buildDecisionOverlayMap(project: ProjectDetail, taskRuns: TaskRun[] = [], resultEventsByRun: Record<string, ResultEvent[]> = {}): Record<string, DecisionOverlay> {
  const overlays: Record<string, DecisionOverlay> = {};
  for (const run of sortTaskRunsByTime(taskRuns)) {
    const events = getRunEvents(resultEventsByRun, run);
    const decision = getDecisionEvent(events);
    if (!decision) continue;
    const targets = getDecisionOverlayTargets(project, run, events);
    if (targets.length === 0) continue;
    const payload = decision.payload || {};
    const summary = decisionOverlaySummary(events);
    if (!summary) continue;
    for (const nodeId of targets) {
      overlays[nodeId] = {
        nodeId,
        runId: run.id,
        taskType: run.task_type || '',
        workerName: run.worker_name || '',
        outcome: payload.outcome || 'unknown',
        title: DECISION_OVERLAY_TITLE[payload.outcome || ''] || '决策解释',
        summary,
      };
    }
  }
  return overlays;
}

export function getDecisionOverlayTargets(project: ProjectDetail, run: TaskRun, events: ResultEvent[] = []): string[] {
  const decision = getDecisionEvent(events);
  const payload = decision?.payload || {};
  if (!payload.outcome) return [];

  if (payload.outcome === 'intents') {
    const sourceIds = new Set<string>();
    getCreatedIntents(project, run, events).forEach(intent => {
      (intent.from || []).forEach((id: string) => sourceIds.add(id));
    });
    if (sourceIds.size === 0 && Array.isArray(payload.from_ids)) {
      payload.from_ids.forEach((id: string) => sourceIds.add(id));
    }
    return factNodeIds(project, sourceIds);
  }

  if (payload.outcome === 'complete') {
    return factNodeIds(project, new Set([payload.completed_fact_id || 'goal']));
  }

  if (payload.outcome === 'fact') {
    const factIds = new Set<string>(getCreatedFacts(project, run, events).map(fact => fact.id));
    if (payload.completed_fact_id) factIds.add(payload.completed_fact_id);
    return factNodeIds(project, factIds);
  }

  if (payload.outcome === 'noop' && Array.isArray(payload.from_ids)) {
    return factNodeIds(project, new Set(payload.from_ids));
  }

  return [];
}

function factNodeIds(project: ProjectDetail, ids: Set<string>): string[] {
  const existingIds = new Set((project.facts || []).map(fact => fact.id));
  return [...ids].filter(id => existingIds.has(id));
}

function decisionOverlaySummary(events: ResultEvent[] = []): string {
  const decision = getDecisionEvent(events);
  const payload = decision?.payload || {};
  const fallback = getDecisionText(events);
  const value = payload.rationale || fallback;
  if (!value) return '';
  const text = String(value).replace(/\s+/g, ' ').trim();
  if (text.length <= DECISION_OVERLAY_SUMMARY_LIMIT) return text;
  return `${text.slice(0, DECISION_OVERLAY_SUMMARY_LIMIT)}...`;
}

export function getWritebackText(project: ProjectDetail, run: TaskRun, events: ResultEvent[] = []): string {
  const createdFacts = getCreatedFacts(project, run, events);
  const createdIntents = getCreatedIntents(project, run, events);

  const parts: string[] = [];
  if (createdFacts.length > 0) {
    const ids = createdFacts.map(fact => fact.id).join(', ');
    parts.push(`写入 Fact [${ids}]`);
  }
  if (createdIntents.length > 0) {
    parts.push(`产出 ${createdIntents.length} 个 Intent`);
  }

  if (run.task_type === 'explore' && run.intent_id) {
    const intent = (project.intents || []).find(item => item.id === run.intent_id);
    if (intent && intent.to) {
      parts.push(`完成意图 → 写入 Fact [${intent.to}]`);
    } else {
      parts.push(`执行意图 [${run.intent_id}]`);
    }
  }

  return parts.join(' · ') || '';
}

export function getHighlightElementIds(project: ProjectDetail, run: TaskRun, events: ResultEvent[] = []): string[] {
  const highlightIds: string[] = [];

  getCreatedFacts(project, run, events).forEach(fact => highlightIds.push(fact.id));

  if (run.intent_id) {
    highlightIds.push(`intent-${run.intent_id}`);
    const intent = (project.intents || []).find(item => item.id === run.intent_id);
    if (intent?.to) {
      highlightIds.push(intent.to);
    }
  }

  getCreatedIntents(project, run, events).forEach(intent => {
    highlightIds.push(`intent-${intent.id}`);
  });

  return [...new Set(highlightIds)];
}
