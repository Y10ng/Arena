import { describe, it, assert } from 'vitest';
import type { GraphNode } from './graphFilters.js';
import {
  estimateGraphNodeSize,
  GRAPH_NODE_DECISION_OVERLAY_EXTRA_HEIGHT,
  GRAPH_NODE_DESCRIPTION_LINES,
  GRAPH_NODE_DEFAULT_WIDTH,
  GRAPH_NODE_MIN_HEIGHT,
  resolveGraphNodeSize,
  splitGraphNodeSummary
} from './nodeSizing.js';

describe('node sizing', () => {
  it('estimateGraphNodeSize keeps short graph nodes compact', () => {
    const size = estimateGraphNodeSize({
      id: 'n1',
      data: {
        label: '确认登录入口',
        kind: 'fact',
        worker: 'codex',
        taskRunId: 'tr001',
      },
    });

    assert.equal(size.width, GRAPH_NODE_DEFAULT_WIDTH);
    assert.equal(size.height, GRAPH_NODE_MIN_HEIGHT);
    assert.equal(size.descriptionLines, 2);
  });

  it('estimateGraphNodeSize clamps long descriptions to stable two-line cards', () => {
    const size = estimateGraphNodeSize({
      id: 'n2',
      data: {
        label: '确认登录入口存在并继续核对表单字段、错误提示、重定向目标、会话持久化行为和后续需要验证的边界条件',
        kind: 'fact',
        worker: 'claude-code',
        taskRunId: 'tr_long_description_001',
      },
    });

    assert.equal(size.width, 320);
    assert.equal(size.descriptionLines, GRAPH_NODE_DESCRIPTION_LINES);
    assert.equal(size.height, GRAPH_NODE_MIN_HEIGHT);
  });

  it('splitGraphNodeSummary separates conclusion sentence from supporting summary', () => {
    const result = splitGraphNodeSummary('确认登录接口存在。后续摘要说明包含表单字段、错误提示和重定向目标。');

    assert.equal(result.conclusion, '确认登录接口存在。');
    assert.equal(result.summary, '后续摘要说明包含表单字段、错误提示和重定向目标。');
  });

  it('resolveGraphNodeSize prefers valid measurements and rejects invalid dimensions', () => {
    const node: GraphNode = { id: 'measured', data: { label: '测量节点', kind: 'fact' } };

    assert.deepEqual(resolveGraphNodeSize(node, { width: 301, height: 181 }), {
      width: 301,
      height: 181,
      descriptionLines: GRAPH_NODE_DESCRIPTION_LINES,
    });
    assert.deepEqual(resolveGraphNodeSize(node, { width: Number.NaN, height: -1 }), {
      width: GRAPH_NODE_DEFAULT_WIDTH,
      height: GRAPH_NODE_MIN_HEIGHT,
      descriptionLines: GRAPH_NODE_DESCRIPTION_LINES,
    });
  });

  it('estimateGraphNodeSize leaves extra room for live pending nodes', () => {
    const size = estimateGraphNodeSize({
      id: 'n3',
      data: {
        label: '验证表单行为',
        kind: 'pending',
        worker: 'pi',
        taskRunId: 'tr_followup',
        liveState: 'heartbeat',
        elapsedTaskSeconds: 80,
      },
    });

    assert.equal(size.width, GRAPH_NODE_DEFAULT_WIDTH);
    assert.ok(size.height > GRAPH_NODE_MIN_HEIGHT);
  });

  it('estimateGraphNodeSize leaves extra room for decision overlays', () => {
    const base = estimateGraphNodeSize({
      id: 'n4',
      data: {
        label: '确认登录入口',
        kind: 'fact',
        worker: 'codex',
        taskRunId: 'tr001',
      },
    });
    const withOverlay = estimateGraphNodeSize({
      id: 'n5',
      data: {
        label: '确认登录入口',
        kind: 'fact',
        worker: 'codex',
        taskRunId: 'tr001',
        decisionOverlay: {
          title: '为什么分叉',
          summary: '已有证据不足以完成目标，需要继续验证。',
        },
      },
    });

    assert.equal(withOverlay.width, GRAPH_NODE_DEFAULT_WIDTH);
    assert.equal(withOverlay.height, base.height + GRAPH_NODE_DECISION_OVERLAY_EXTRA_HEIGHT);
  });

  it('estimateGraphNodeSize leaves extra room for pending_group nodes', () => {
    const normalSize = estimateGraphNodeSize({
      id: 'n6',
      data: {
        label: '测试折叠',
        kind: 'fact',
        worker: 'codex',
        taskRunId: 'tr001',
      },
    });

    const pendingGroupSizeNormal = estimateGraphNodeSize({
      id: 'n7',
      data: {
        label: '测试折叠',
        kind: 'pending_group',
        pendingGroup: {
          hasCriticalPath: false,
        },
      },
    });

    const pendingGroupSizeCritical = estimateGraphNodeSize({
      id: 'n8',
      data: {
        label: '测试折叠',
        kind: 'pending_group',
        pendingGroup: {
          hasCriticalPath: true,
        },
      },
    });

    assert.equal(pendingGroupSizeNormal.height, normalSize.height + 24);
    assert.equal(pendingGroupSizeCritical.height, normalSize.height + 44);
  });
});
