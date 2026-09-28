import { describe, it, assert } from 'vitest';
import {
  buildDagreEdgeLabel,
  buildDagreLayoutEdges,
  COMPACT_GRAPH_NODE_THRESHOLD,
  GRAPH_LAYOUT_RANK_MODES,
  LARGE_GRAPH_NODE_THRESHOLD,
  resolveDagreLayoutOptions,
  useDagreLayout,
} from './useDagreLayout.js';
import type { GraphNode, GraphEdge } from './graphFilters.js';

function makeNodes(count: number): GraphNode[] {
  return Array.from({ length: count }, (_, index) => ({
    id: index === 0 ? 'origin' : `f${index}`,
    type: 'fact',
    data: {
      kind: index === 0 ? 'origin' : 'fact',
      label: `节点 ${index}`,
      createdAt: `2026-06-03T00:${String(index).padStart(2, '0')}:00Z`,
    },
    position: { x: 0, y: 0 },
  }));
}

function makeChainEdges(nodes: GraphNode[]): GraphEdge[] {
  return nodes.slice(1).map((node, index) => ({
    id: `edge-${index}`,
    source: nodes[index].id,
    target: node.id,
    type: 'intent',
    data: { state: 'concluded' },
  }));
}

describe('useDagreLayout', () => {
  it('resolveDagreLayoutOptions keeps small graphs compact', () => {
    const nodes = makeNodes(3);
    const edges = makeChainEdges(nodes);
    const { graphOptions } = resolveDagreLayoutOptions(nodes, edges, { dir: 'TB' });

    assert.equal(graphOptions.rankdir, 'TB');
    assert.equal(graphOptions.nodesep, 60);
    assert.equal(graphOptions.ranksep, 100);
    assert.equal(graphOptions.align, 'UL');
    assert.equal(graphOptions.acyclicer, 'greedy');
    assert.equal(graphOptions.ranker, 'network-simplex');
  });

  it('resolveDagreLayoutOptions expands spacing and uses tight-tree for large tree-like graphs', () => {
    const nodes = makeNodes(LARGE_GRAPH_NODE_THRESHOLD + 1);
    const edges = makeChainEdges(nodes);
    const { graphOptions } = resolveDagreLayoutOptions(nodes, edges);

    assert.ok(graphOptions.nodesep > 60);
    assert.ok(graphOptions.ranksep > 100);
    assert.equal(graphOptions.ranker, 'tight-tree');
  });

  it('critical path mode favors network-simplex and stronger critical edges', () => {
    const criticalEdge: GraphEdge = {
      id: 'edge-critical',
      source: 'origin',
      target: 'goal',
      data: { isCriticalPath: true },
    };
    const branchEdge: GraphEdge = {
      id: 'edge-branch',
      source: 'origin',
      target: 'pending:i1',
      data: {},
    };
    const { graphOptions } = resolveDagreLayoutOptions(makeNodes(40), [], {
      rankMode: GRAPH_LAYOUT_RANK_MODES.CRITICAL,
    });

    assert.equal(graphOptions.ranker, 'network-simplex');
    assert.deepEqual(
      buildDagreEdgeLabel(criticalEdge, { rankMode: GRAPH_LAYOUT_RANK_MODES.CRITICAL }),
      { minlen: 1, weight: 10 }
    );
    assert.deepEqual(
      buildDagreEdgeLabel(branchEdge, { rankMode: GRAPH_LAYOUT_RANK_MODES.CRITICAL }),
      { minlen: 2, weight: 1 }
    );
  });

  it('keeps dense graphs readable with wider spacing and bounded edge ranks', () => {
    const nodes = makeNodes(COMPACT_GRAPH_NODE_THRESHOLD);
    const edges = makeChainEdges(nodes);
    const { graphOptions, layoutOptions, edgeMinlenLimit } = resolveDagreLayoutOptions(nodes, edges);

    assert.ok(graphOptions.nodesep > 60);
    assert.ok(graphOptions.ranksep > 100);
    assert.ok(graphOptions.edgesep > 20);
    assert.equal(edgeMinlenLimit, 2);
    assert.deepEqual(layoutOptions.constraints, []);
  });

  it('caps time-rank edge minlen when the dense profile requests it', () => {
    const edge: GraphEdge = {
      id: 'edge-long-time-gap',
      source: 'origin',
      target: 'f7',
      data: {},
    };
    const nodeTimeRanks = new Map([
      ['origin', 0],
      ['f7', 7],
    ]);

    assert.deepEqual(
      buildDagreEdgeLabel(edge, { nodeTimeRanks, maxMinlen: 2 }),
      { minlen: 2, weight: 3 }
    );
  });

  it('uses one representative constraint edge for multi-source join intents', () => {
    const joinGroup = { id: 'join:i1' };
    const edges: GraphEdge[] = [
      { id: 'edge-f1', source: 'f1', target: 'f3', data: { joinGroup } },
      { id: 'edge-f2', source: 'f2', target: 'f3', data: { joinGroup } },
      { id: 'edge-single', source: 'f3', target: 'goal', data: {} },
    ];
    const layoutEdges = buildDagreLayoutEdges(edges, new Map([
      ['f1', 1],
      ['f2', 3],
      ['f3', 4],
    ]));

    assert.deepEqual(layoutEdges.map(edge => edge.id).sort(), ['edge-f2', 'edge-single']);
  });

  it('keeps Reason records outside causal density and Dagre edge compaction', () => {
    const causalNodes = makeNodes(3);
    const causalEdges = makeChainEdges(causalNodes);
    const reasonNode: GraphNode = {
      id: 'reason:run-1',
      type: 'reason',
      position: { x: 0, y: 0 },
      data: {
        kind: 'reason',
        label: '审计推理记录',
        createdAt: '2026-06-03T00:01:30Z',
      },
    };
    const auditEdges: GraphEdge[] = [
      {
        id: 'audit-input',
        source: 'origin',
        target: reasonNode.id,
        type: 'reason-audit',
        data: { joinGroup: { id: 'join:should-not-compact' } },
      },
      {
        id: 'audit-output',
        source: reasonNode.id,
        target: 'f1',
        type: 'reason-audit',
        data: { joinGroup: { id: 'join:should-not-compact' } },
      },
    ];

    const baseOptions = resolveDagreLayoutOptions(causalNodes, causalEdges);
    const withReasonOptions = resolveDagreLayoutOptions([...causalNodes, reasonNode], [...causalEdges, ...auditEdges]);
    assert.deepEqual(withReasonOptions.graphOptions, baseOptions.graphOptions);
    assert.deepEqual(buildDagreLayoutEdges(auditEdges).map(edge => edge.id), ['audit-input', 'audit-output']);
  });

  it('positions Reason sidecars deterministically without moving causal nodes', () => {
    const causalNodes = makeNodes(4);
    const causalEdges = makeChainEdges(causalNodes);
    const reasonNodes: GraphNode[] = [
      {
        id: 'reason:b',
        type: 'reason',
        position: { x: 0, y: 0 },
        data: { kind: 'reason', label: '第二条推理', createdAt: '2026-06-03T00:01:30Z' },
      },
      {
        id: 'reason:a',
        type: 'reason',
        position: { x: 0, y: 0 },
        data: { kind: 'reason', label: '第一条推理', createdAt: '2026-06-03T00:01:30Z' },
      },
    ];
    const auditEdges: GraphEdge[] = reasonNodes.flatMap(node => [
      { id: `${node.id}:in`, source: 'origin', target: node.id, type: 'reason-audit', data: {} },
      { id: `${node.id}:out`, source: node.id, target: 'f2', type: 'reason-audit', data: {} },
    ]);

    const base = useDagreLayout(causalNodes, causalEdges, 'LR');
    const combined = useDagreLayout(
      [...reasonNodes, ...causalNodes].reverse(),
      [...auditEdges, ...causalEdges].reverse(),
      'LR'
    );
    const combinedById = new Map(combined.map(node => [node.id, node]));

    causalNodes.forEach(node => {
      assert.deepEqual(combinedById.get(node.id)!.position, base.find(item => item.id === node.id)!.position);
    });
    assert.ok(combinedById.get('reason:a')!.position!.y < Math.min(...base.map(node => node.position!.y)));
    assert.ok(combinedById.get('reason:a')!.position!.x < combinedById.get('reason:b')!.position!.x);
  });

  it('produces the same coordinates for reordered causal input', () => {
    const nodes = makeNodes(6);
    const edges = makeChainEdges(nodes);
    const normal = useDagreLayout(nodes, edges, 'LR');
    const reordered = useDagreLayout([...nodes].reverse(), [...edges].reverse(), 'LR');
    const normalPositions = Object.fromEntries(normal.map(node => [node.id, node.position]));
    const reorderedPositions = Object.fromEntries(reordered.map(node => [node.id, node.position]));

    assert.deepEqual(reorderedPositions, normalPositions);
  });

  it('lays out a 100-node, 200-edge graph with finite deterministic coordinates', () => {
    const nodes = makeNodes(101);
    const chainEdges = makeChainEdges(nodes);
    const shortcutEdges: GraphEdge[] = nodes.slice(2).map((node, index) => ({
      id: `shortcut-${index}`,
      source: nodes[index].id,
      target: node.id,
      type: 'intent',
      data: { state: 'concluded' },
    }));
    const edges = [
      ...chainEdges,
      ...shortcutEdges,
      {
        id: 'shortcut-root-to-tail',
        source: 'origin',
        target: 'f100',
        type: 'intent' as const,
        data: { state: 'concluded' },
      },
    ];

    const normal = useDagreLayout(nodes, edges, 'LR');
    const reordered = useDagreLayout([...nodes].reverse(), [...edges].reverse(), 'LR');
    const normalPositions = Object.fromEntries(normal.map(node => [node.id, node.position]));
    const reorderedPositions = Object.fromEntries(reordered.map(node => [node.id, node.position]));

    assert.equal(normal.length, 101);
    assert.equal(edges.length, 200);
    normal.forEach(node => {
      assert.ok(Number.isFinite(node.position?.x));
      assert.ok(Number.isFinite(node.position?.y));
    });
    assert.deepEqual(reorderedPositions, normalPositions);
  });

  it('useDagreLayout accepts rank mode options and returns finite node positions', () => {
    const nodes = makeNodes(4);
    const edges = makeChainEdges(nodes);
    const laidOut = useDagreLayout(nodes, edges, {
      dir: 'LR',
      rankMode: GRAPH_LAYOUT_RANK_MODES.TIME,
    });

    assert.equal(laidOut.length, nodes.length);
    laidOut.forEach((node) => {
      assert.ok(Number.isFinite(node.position?.x));
      assert.ok(Number.isFinite(node.position?.y));
      assert.ok(node.data?.nodeWidth > 0);
      assert.ok(node.data?.nodeHeight > 0);
      assert.equal(node.data?.descriptionLines, 2);
    });
  });
});
