import { describe, it, assert } from 'vitest';
import {
  buildDecisionOverlayMap,
  buildReasonGraphRecords,
  getCreatedFacts,
  getCreatedIntents,
  getDecisionText,
  getDecisionOverlayTargets,
  getHighlightElementIds,
  getWritebackText,
  sortTaskRunsByTime,
} from './decisionModel.js';
import type { TaskRun, ResultEvent } from './decisionModel.js';
import type { ProjectDetail } from '../useGraphModel.js';

const project: ProjectDetail = {
  facts: [
    { id: 'origin', description: '起点' },
    { id: 'f1', description: '探索得到的事实' },
    { id: 'goal', description: '目标' }
  ],
  intents: [
    { id: 'i1', from: ['origin'], to: 'f1', description: '探索入口' },
    { id: 'i2', from: ['f1'], to: null, description: '继续验证' }
  ]
};

describe('decisionModel', () => {
  it('sortTaskRunsByTime orders task runs by started or created time', () => {
    const runs: TaskRun[] = [
      { id: 'tr2', created_at: '2026-06-03T00:02:00Z' },
      { id: 'tr1', started_at: '2026-06-03T00:01:00Z' }
    ];

    assert.deepEqual(sortTaskRunsByTime(runs).map(run => run.id), ['tr1', 'tr2']);
  });

  it('decision events can identify created facts when direct provenance is absent', () => {
    const run: TaskRun = { id: 'tr_explore', task_type: 'explore', intent_id: 'i1' };
    const events: ResultEvent[] = [
      {
        kind: 'decision_result',
        payload: {
          phase: 'explore_execute',
          outcome: 'fact',
          from_ids: ['origin'],
          completed_fact_id: 'f1',
          write_status: 'success'
        }
      }
    ];

    assert.deepEqual(getCreatedFacts(project, run, events).map(fact => fact.id), ['f1']);
    assert.equal(getDecisionText(events), '写入新 Fact · 写入: success');
    assert.equal(getWritebackText(project, run, events), '写入 Fact [f1] · 完成意图 → 写入 Fact [f1]');
    assert.deepEqual(getHighlightElementIds(project, run, events), ['f1', 'intent-i1']);
  });

  it('decision events can identify created intents from reason output', () => {
    const run: TaskRun = { id: 'tr_reason', task_type: 'reason' };
    const events: ResultEvent[] = [
      {
        kind: 'decision_result',
        payload: {
          phase: 'reason',
          outcome: 'intents',
          rationale: '需要继续验证',
          confidence: 0.68,
          blocked_by: ['等待 target 审批'],
          depends_on_targets: ['target_001'],
          rejected_options: [{ description: '直接完成', reason: '证据不足' }],
          created_intent_ids: ['i2'],
          write_status: 'success'
        }
      }
    ];

    assert.deepEqual(getCreatedIntents(project, run, events).map(intent => intent.id), ['i2']);
    assert.equal(
      getDecisionText(events),
      '提出新意图 · 写入: success · 理由: 需要继续验证 · 置信度: 68% · 阻塞 1 项 · 依赖 Target 1 个 · 否决 1 项'
    );
    assert.deepEqual(getHighlightElementIds(project, run, events), ['intent-i2']);
  });

  it('Reason graph outputs expose deterministic layout descriptors', () => {
    const reasonProject: ProjectDetail = {
      facts: project.facts,
      intents: [
        { id: 'intent-z', from: ['f1', 'origin'], to: null, description: '后续 Z' },
        { id: 'intent-a', from: ['origin'], to: null, description: '后续 A' },
      ],
    };
    const run: TaskRun = { id: 'tr_reason', task_type: 'reason' };
    const events: ResultEvent[] = [{
      kind: 'decision_result',
      payload: {
        outcome: 'intents',
        created_intent_ids: ['intent-z', 'intent-a'],
      },
    }];

    const [record] = buildReasonGraphRecords(reasonProject, [run], { tr_reason: events });

    assert.deepEqual(record.inputNodeIds, ['f1', 'origin']);
    assert.deepEqual(record.outputNodeIds, ['pending:intent-a', 'pending:intent-z']);
    assert.deepEqual(record.outputDescriptors, [
      { nodeId: 'pending:intent-a', layoutKey: 'intent:intent-a', intentId: 'intent-a' },
      { nodeId: 'pending:intent-z', layoutKey: 'intent:intent-z', intentId: 'intent-z' },
    ]);
  });

  it('protocol write fallback still matches facts and intents', () => {
    const run: TaskRun = { id: 'tr_legacy', task_type: 'reason' };
    const events: ResultEvent[] = [
      { kind: 'protocol_write', payload: { operation: 'conclude', fact_id: 'f1' } },
      { kind: 'protocol_write', payload: { operation: 'create_intent', created_id: 'i2' } }
    ];

    assert.deepEqual(getCreatedFacts(project, run, events).map(fact => fact.id), ['f1']);
    assert.deepEqual(getCreatedIntents(project, run, events).map(intent => intent.id), ['i2']);
  });

  it('decision overlays attach reason-created intents to their source fact', () => {
    const run: TaskRun = {
      id: 'tr_reason',
      task_type: 'reason',
      worker_name: 'mock-worker',
    };
    const events: ResultEvent[] = [
      {
        kind: 'decision_result',
        payload: {
          outcome: 'intents',
          rationale: '从 f1 继续分叉验证，因为已有事实不足以完成目标。',
          created_intent_ids: ['i2'],
          write_status: 'success',
        },
      },
    ];

    assert.deepEqual(getDecisionOverlayTargets(project, run, events), ['f1']);

    const overlays = buildDecisionOverlayMap(project, [run], { tr_reason: events });
    assert.equal(overlays.f1.title, '为什么分叉');
    assert.equal(overlays.f1.runId, 'tr_reason');
    assert.equal(overlays.f1.workerName, 'mock-worker');
    assert.equal(overlays.f1.summary, '从 f1 继续分叉验证，因为已有事实不足以完成目标。');
  });

  it('decision overlays attach fact and complete outcomes to produced fact nodes', () => {
    const runs: TaskRun[] = [
      { id: 'tr_explore', task_type: 'explore', intent_id: 'i1', worker_name: 'codex' },
      { id: 'tr_complete', task_type: 'reason', worker_name: 'claude' },
    ];
    const resultEventsByRun: Record<string, ResultEvent[]> = {
      tr_explore: [
        {
          kind: 'decision_result',
          payload: {
            outcome: 'fact',
            completed_fact_id: 'f1',
            rationale: '探索输出已验证。',
          },
        },
      ],
      tr_complete: [
        {
          kind: 'decision_result',
          payload: {
            outcome: 'complete',
            completed_fact_id: 'goal',
            rationale: '目标条件已经满足。',
          },
        },
      ],
    };

    assert.deepEqual(getDecisionOverlayTargets(project, runs[0], resultEventsByRun.tr_explore), ['f1']);
    assert.deepEqual(getDecisionOverlayTargets(project, runs[1], resultEventsByRun.tr_complete), ['goal']);

    const overlays = buildDecisionOverlayMap(project, runs, resultEventsByRun);
    assert.equal(overlays.f1.title, '为什么写入');
    assert.equal(overlays.goal.title, '为什么完成');
  });
});
