import { describe, it, assert } from 'vitest';

import {
  artifactContentUrl,
  artifactDeprecationText,
  artifactKindLabel,
  auditCountLabel,
  badgeTone,
  diffLineKind,
  diffPreviewLines,
  evidencePointerTypeText,
  formatAuditCounts,
  intentStatusKey,
  intentStatusMeta,
  isArtifactImagePreviewable,
  MONITOR_STATE_FILTER_OPTIONS,
  PROJECT_FILTER_OPTIONS,
  riskLevelText,
  statusMeta,
  statusText,
  taskTypeText,
  formatRelativeTime,
} from './format.js';

describe('status meta', () => {
  it('keeps task status text and tone in one mapping', () => {
    assert.equal(statusText('running'), '运行中');
    assert.equal(badgeTone('running'), 'info');
    assert.deepEqual(statusMeta('failed'), { text: '失败', tone: 'bad' });
  });

  it('covers intent and audit statuses used by graph and monitoring panels', () => {
    assert.deepEqual(statusMeta('bootstrap'), { text: '启动', tone: 'info' });
    assert.deepEqual(statusMeta('exploring'), { text: '探索中', tone: 'info' });
    assert.deepEqual(statusMeta('unclaimed'), { text: '未领取', tone: 'neutral' });
    assert.deepEqual(statusMeta('unfinished'), { text: '未完成', tone: 'warn' });
    assert.deepEqual(statusMeta('concluded'), { text: '已结论', tone: 'good' });
    assert.deepEqual(statusMeta('confirmed'), { text: '已确认', tone: 'good' });
    assert.deepEqual(statusMeta('critical_path'), { text: '关键路径', tone: 'good' });
    assert.deepEqual(statusMeta('fixed_node'), { text: '固定节点', tone: 'neutral' });
    assert.deepEqual(statusMeta('parse_error'), { text: '解析失败', tone: 'bad' });
    assert.deepEqual(statusMeta('cooldown'), { text: '冷却', tone: 'warn' });
    assert.deepEqual(statusMeta('capacity_full'), { text: '容量已满', tone: 'warn' });
    assert.deepEqual(statusMeta('rejected_backoff'), { text: '拒绝退避', tone: 'bad' });
    assert.deepEqual(statusMeta('schedulable'), { text: '可调度', tone: 'good' });
    assert.deepEqual(statusMeta('evidence_valid'), { text: '证据有效', tone: 'good' });
    assert.deepEqual(statusMeta('evidence_broken'), { text: '证据断链', tone: 'bad' });
  });

  it('covers target statuses used by project detail panels', () => {
    assert.deepEqual(statusMeta('seed'), { text: '种子', tone: 'good' });
    assert.deepEqual(statusMeta('discovered'), { text: '发现', tone: 'info' });
    assert.deepEqual(statusMeta('approved'), { text: '批准', tone: 'good' });
    assert.deepEqual(statusMeta('rejected'), { text: '拒绝', tone: 'bad' });
  });

  it('derives intent status meta from one shared mapping', () => {
    assert.equal(intentStatusKey({ to: 'f001', worker: 'mock' }), 'concluded');
    assert.deepEqual(intentStatusMeta({ to: 'f001' }), { text: '已结论', tone: 'good' });
    assert.deepEqual(intentStatusMeta({ worker: 'mock' }), { text: '探索中', tone: 'info' });
    assert.deepEqual(intentStatusMeta({}), { text: '未领取', tone: 'neutral' });
  });

  it('falls back without inventing a color for unknown statuses', () => {
    assert.deepEqual(statusMeta('custom_state'), { text: 'custom_state', tone: 'neutral' });
  });

  it('preserves the empty status placeholder', () => {
    assert.equal(statusText(undefined), '—');
    assert.equal(badgeTone(undefined), 'neutral');
  });

  it('formats audit count labels from the shared mapping', () => {
    assert.equal(auditCountLabel('created_intent'), '提出方向');
    assert.equal(auditCountLabel('high_risk'), '高风险');
    assert.equal(auditCountLabel('custom_metric'), 'custom_metric');
    assert.equal(
      formatAuditCounts(
        {
          valid: 2,
          invalid: 0,
          created_intent: 1,
          goal_path_created_intent: 1,
        },
        ['valid', 'invalid', 'created_intent', 'goal_path_created_intent'],
      ),
      '有效=2 / 提出方向=1 / 进入完成链路=1',
    );
  });

  it('formats hook risk levels from the shared mapping', () => {
    assert.equal(riskLevelText('low'), '低');
    assert.equal(riskLevelText('medium'), '中');
    assert.equal(riskLevelText('high'), '高');
    assert.equal(riskLevelText('unknown'), '未知');
    assert.equal(riskLevelText('custom_risk'), 'custom_risk');
    assert.equal(riskLevelText(undefined), '');
  });

  it('formats task type labels from the shared mapping', () => {
    assert.equal(taskTypeText('bootstrap'), '启动');
    assert.equal(taskTypeText('reason'), '推理');
    assert.equal(taskTypeText('explore'), '探索');
    assert.equal(taskTypeText('custom_task'), 'custom_task');
    assert.equal(taskTypeText(''), '');
  });

  it('keeps project filter labels in the shared mapping', () => {
    assert.deepEqual(PROJECT_FILTER_OPTIONS, [
      { value: 'all', label: '全部' },
      { value: 'active', label: '活跃' },
      { value: 'paused', label: '暂停' },
      { value: 'stopped', label: '结束' },
      { value: 'completed', label: '完成' },
    ]);
  });

  it('keeps monitor state filter labels in the shared mapping', () => {
    assert.deepEqual(MONITOR_STATE_FILTER_OPTIONS, [
      { value: 'all', label: '全部' },
      { value: 'running', label: '运行中' },
      { value: 'queued', label: '排队中' },
      { value: 'failed', label: '失败' },
    ]);
  });
});

describe('artifact labels', () => {
  it('keeps active artifact kinds unchanged', () => {
    assert.equal(artifactKindLabel({ kind: 'report' }), 'report');
    assert.equal(artifactDeprecationText({ kind: 'report' }), '');
  });

  it('marks deprecated artifact kinds as frozen compatible', () => {
    const artifact = {
      kind: 'har',
      kind_deprecated: true,
      deprecation_message: '冻结兼容：当前阶段不再新建或采集此类产物，仅保留历史读取。',
    };

    assert.equal(artifactKindLabel(artifact), 'har（冻结兼容）');
    assert.equal(artifactDeprecationText(artifact), artifact.deprecation_message);
  });

  it('falls back to the frozen compatibility message', () => {
    assert.equal(
      artifactDeprecationText({ kind: 'pcap', kind_deprecated: true }),
      '冻结兼容：当前阶段不再新建或采集此类产物，仅保留历史读取。',
    );
  });

  it('builds inline screenshot content URLs only for image-previewable artifacts', () => {
    assert.equal(isArtifactImagePreviewable({ kind: 'screenshot' }), true);
    assert.equal(isArtifactImagePreviewable({ kind: 'report' }), false);
    assert.equal(
      artifactContentUrl({ id: 'a 001', project_id: 'proj 001', kind: 'screenshot' }),
      '/projects/proj%20001/artifacts/a%20001/content',
    );
    assert.equal(artifactContentUrl({ id: 'a001', project_id: 'proj_001', kind: 'report' }), '');
  });

  it('classifies patch preview lines for diff highlighting', () => {
    assert.equal(diffLineKind('--- a/file.txt'), 'meta');
    assert.equal(diffLineKind('+++ b/file.txt'), 'meta');
    assert.equal(diffLineKind('@@ -1 +1 @@'), 'hunk');
    assert.equal(diffLineKind('+new'), 'add');
    assert.equal(diffLineKind('-old'), 'remove');
    assert.equal(diffLineKind(' context'), 'context');
    assert.deepEqual(diffPreviewLines('-old\n+new').map((line) => line.kind), ['remove', 'add']);
  });
});

describe('evidence pointer labels', () => {
  it('classifies common evidence pointer shapes without changing raw pointers', () => {
    assert.equal(evidencePointerTypeText(''), '未指定');
    assert.equal(evidencePointerTypeText('stdout'), '输出片段');
    assert.equal(evidencePointerTypeText('stderr:tail'), '输出片段');
    assert.equal(evidencePointerTypeText('line:42'), '行号');
    assert.equal(evidencePointerTypeText('L12-L18'), '行号');
    assert.equal(evidencePointerTypeText('json:$.items[0]'), 'JSON Path');
    assert.equal(evidencePointerTypeText('$.items[0]'), 'JSON Path');
    assert.equal(evidencePointerTypeText('bbox:10,20,30,40'), '截图区域');
    assert.equal(evidencePointerTypeText('report.md#finding-1'), '报告锚点');
    assert.equal(evidencePointerTypeText('/tmp/report.txt'), '自定义指针');
  });
});

describe('formatRelativeTime', () => {
  it('formats dates relative to now', () => {
    const now = new Date();
    assert.equal(formatRelativeTime(''), '');
    assert.equal(formatRelativeTime(new Date(now.getTime() - 2 * 1000)), '刚刚');
    assert.equal(formatRelativeTime(new Date(now.getTime() - 25 * 1000)), '25秒前');
    assert.equal(formatRelativeTime(new Date(now.getTime() - 5 * 60 * 1000)), '5分钟前');
    assert.equal(formatRelativeTime(new Date(now.getTime() - 3 * 3600 * 1000)), '3小时前');
    assert.equal(formatRelativeTime(new Date(now.getTime() - 4 * 24 * 3600 * 1000)), '4天前');
  });
});
