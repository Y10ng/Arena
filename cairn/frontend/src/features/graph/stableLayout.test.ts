import { describe, it, assert } from 'vitest';
import { separateGraphNodeOverlaps, stabilizeGraphNodePositions } from './stableLayout.js';
import type { GraphNode } from './graphFilters.js';

describe('stable layout', () => {
  it('stabilizeGraphNodePositions uses median alignment with bounded node inertia', () => {
    const previousNodes: GraphNode[] = [
      { id: 'origin', position: { x: 10, y: 20 }, data: {} },
      { id: 'f1', position: { x: 140, y: 20 }, data: {} },
    ];
    const nextNodes: GraphNode[] = [
      { id: 'origin', position: { x: 0, y: 0 }, data: {} },
      { id: 'f1', position: { x: 260, y: 80 }, data: {} },
      { id: 'f2', position: { x: 520, y: 120 }, data: {} },
    ];

    const stabilized = stabilizeGraphNodePositions(nextNodes, {
      previousNodes,
      previousContextKey: 'proj|LR|all|current',
      contextKey: 'proj|LR|all|current',
    });

    assert.deepEqual(stabilized.find(node => node.id === 'origin')!.position, { x: -55, y: -20 });
    assert.deepEqual(stabilized.find(node => node.id === 'f1')!.position, { x: 205, y: 60 });
    assert.deepEqual(stabilized.find(node => node.id === 'f2')!.position, { x: 465, y: 100 });
  });

  it('stabilizeGraphNodePositions allows a fresh layout after an explicit view change', () => {
    const stabilized = stabilizeGraphNodePositions(
      [{ id: 'origin', position: { x: 0, y: 0 }, data: {} }],
      {
        previousNodes: [{ id: 'origin', position: { x: 10, y: 20 }, data: {} }],
        previousContextKey: 'proj|LR|all|current',
        contextKey: 'proj|TB|all|current',
      }
    );

    assert.deepEqual(stabilized[0].position, { x: 0, y: 0 });
  });

  it('stabilizeGraphNodePositions ignores previous nodes without usable coordinates', () => {
    const stabilized = stabilizeGraphNodePositions(
      [{ id: 'origin', position: { x: 0, y: 0 }, data: {} }],
      {
        previousNodes: [{ id: 'origin', position: { x: Number.NaN, y: 20 }, data: {} }],
        previousContextKey: 'same',
        contextKey: 'same',
      }
    );

    assert.deepEqual(stabilized[0].position, { x: 0, y: 0 });
  });

  it('stabilizeGraphNodePositions resists outliers and preserves explicit pins', () => {
    const stabilized = stabilizeGraphNodePositions(
      [
        { id: 'a', position: { x: 0, y: 0 }, data: {} },
        { id: 'b', position: { x: 100, y: 0 }, data: {} },
        { id: 'outlier', position: { x: 10000, y: 0 }, data: {} },
        { id: 'new', position: { x: 300, y: 0 }, data: {} },
      ],
      {
        previousNodes: [
          { id: 'a', position: { x: 20, y: 10 }, data: {} },
          { id: 'b', position: { x: 120, y: 10 }, data: {} },
          { id: 'outlier', position: { x: 0, y: 10 }, data: {} },
        ],
        previousContextKey: 'proj|LR|time',
        contextKey: 'proj|LR|time',
        pinnedPositions: { b: { x: 777, y: 888 } },
      }
    );

    assert.deepEqual(stabilized.find(node => node.id === 'new')!.position, { x: 320, y: 10 });
    assert.deepEqual(stabilized.find(node => node.id === 'b')!.position, { x: 777, y: 888 });
    assert.equal(stabilized.find(node => node.id === 'outlier')!.position!.x, 72);
  });

  it('does not let moving Reason sidecars bias causal alignment', () => {
    const previousNodes: GraphNode[] = [
      { id: 'fact', position: { x: 10, y: 10 }, data: { kind: 'fact' } },
      { id: 'reason:a', position: { x: 0, y: 0 }, data: { kind: 'reason' } },
      { id: 'reason:b', position: { x: 0, y: 0 }, data: { kind: 'reason' } },
    ];
    const stabilized = stabilizeGraphNodePositions(
      [
        { id: 'fact', position: { x: 10, y: 10 }, data: { kind: 'fact' } },
        { id: 'reason:a', position: { x: 500, y: 500 }, data: { kind: 'reason' } },
        { id: 'reason:b', position: { x: 500, y: 500 }, data: { kind: 'reason' } },
      ],
      { previousNodes, previousContextKey: 'p|LR|time', contextKey: 'p|LR|time' }
    );

    assert.deepEqual(stabilized.find(node => node.id === 'fact')!.position, { x: 10, y: 10 });
  });

  it('does not use shared Reason nodes to align newly introduced causal nodes', () => {
    const stabilized = stabilizeGraphNodePositions(
      [
        { id: 'fact:new', position: { x: 100, y: 50 }, data: { kind: 'fact' } },
        { id: 'reason:a', position: { x: 500, y: 500 }, data: { kind: 'reason' } },
      ],
      {
        previousNodes: [
          { id: 'reason:a', position: { x: 0, y: 0 }, data: { kind: 'reason' } },
        ],
        previousContextKey: 'p|LR|time',
        contextKey: 'p|LR|time',
      }
    );

    assert.deepEqual(stabilized.find(node => node.id === 'fact:new')!.position, { x: 100, y: 50 });
  });

  it('keeps causal collision results unchanged when Reason overlaps the main chain', () => {
    const causalNodes: GraphNode[] = [
      { id: 'fact:a', position: { x: 100, y: 0 }, data: { kind: 'fact', nodeWidth: 240, nodeHeight: 150 } },
      { id: 'fact:b', position: { x: 100, y: 40 }, data: { kind: 'fact', nodeWidth: 240, nodeHeight: 150 } },
    ];
    const base = separateGraphNodeOverlaps(causalNodes, { dir: 'LR', nodeGap: 32 });
    const withReason = separateGraphNodeOverlaps([
      { id: 'reason:a', position: { x: 100, y: 0 }, data: { kind: 'reason', nodeWidth: 240, nodeHeight: 150 } },
      ...causalNodes,
    ], { dir: 'LR', nodeGap: 32 });
    const withReasonById = new Map(withReason.map(node => [node.id, node.position]));

    base.forEach(node => {
      assert.deepEqual(withReasonById.get(node.id), node.position);
    });
    assert.notDeepEqual(withReasonById.get('reason:a'), { x: 100, y: 0 });
  });

  it('separateGraphNodeOverlaps spreads nodes within the same LR rank', () => {
    const nodes: GraphNode[] = [
      { id: 'a', position: { x: 100, y: 0 }, data: { nodeWidth: 240, nodeHeight: 150 } },
      { id: 'b', position: { x: 100, y: 40 }, data: { nodeWidth: 240, nodeHeight: 150 } },
      { id: 'c', position: { x: 100, y: 80 }, data: { nodeWidth: 240, nodeHeight: 150 } },
    ];

    const separated = separateGraphNodeOverlaps(nodes, { dir: 'LR', nodeGap: 32 });
    const sorted = separated
      .map(node => ({
        id: node.id,
        start: node.position!.y,
        end: node.position!.y + Number(node.data?.nodeHeight),
      }))
      .sort((left, right) => left.start - right.start);

    assert.ok(sorted[1].start - sorted[0].end >= 31.5);
    assert.ok(sorted[2].start - sorted[1].end >= 31.5);
    assert.deepEqual(separated.map(node => node.position!.x), [100, 100, 100]);
  });

  it('separates dense coincident rectangles without a fixed-pass remainder', () => {
    const nodes: GraphNode[] = Array.from({ length: 8 }, (_, index) => ({
      id: `n${index}`,
      position: { x: 0, y: 0 },
      data: { nodeWidth: 240, nodeHeight: 150 },
    }));
    const separated = separateGraphNodeOverlaps(nodes, { dir: 'LR', nodeGap: 32 });
    const sorted = separated.slice().sort((left, right) => left.position!.y - right.position!.y);

    for (let index = 1; index < sorted.length; index += 1) {
      assert.ok(sorted[index].position!.y - (sorted[index - 1].position!.y + 150) >= 31.9);
    }
  });

  it('separateGraphNodeOverlaps spreads nodes horizontally for TB ranks', () => {
    const nodes: GraphNode[] = [
      { id: 'a', position: { x: 0, y: 100 }, data: { nodeWidth: 240, nodeHeight: 150 } },
      { id: 'b', position: { x: 60, y: 100 }, data: { nodeWidth: 240, nodeHeight: 150 } },
    ];

    const separated = separateGraphNodeOverlaps(nodes, { dir: 'TB', nodeGap: 40 });
    const sorted = separated
      .map(node => ({
        start: node.position!.x,
        end: node.position!.x + Number(node.data?.nodeWidth),
      }))
      .sort((left, right) => left.start - right.start);

    assert.ok(sorted[1].start - sorted[0].end >= 39.5);
    assert.deepEqual(separated.map(node => node.position!.y), [100, 100]);
  });

  it('separateGraphNodeOverlaps resolves global mixed-size rectangle collisions deterministically', () => {
    const nodes: GraphNode[] = [
      { id: 'wide', position: { x: 0, y: 0 }, data: { nodeWidth: 320, nodeHeight: 150 } },
      { id: 'tall', position: { x: 250, y: 110 }, data: { nodeWidth: 240, nodeHeight: 220 } },
      { id: 'small', position: { x: 470, y: 210 }, data: { nodeWidth: 180, nodeHeight: 100 } },
    ];

    const separated = separateGraphNodeOverlaps(nodes, { dir: 'LR', nodeGap: 24, rankTolerance: 40 });
    const reordered = separateGraphNodeOverlaps([...nodes].reverse(), { dir: 'LR', nodeGap: 24, rankTolerance: 40 });
    const positions = Object.fromEntries(separated.map(node => [node.id, node.position]));
    const reorderedPositions = Object.fromEntries(reordered.map(node => [node.id, node.position]));

    assert.deepEqual(reorderedPositions, positions);
    for (let leftIndex = 0; leftIndex < separated.length; leftIndex += 1) {
      for (let rightIndex = leftIndex + 1; rightIndex < separated.length; rightIndex += 1) {
        const left = separated[leftIndex];
        const right = separated[rightIndex];
        const overlapX = Math.min(left.position!.x + Number(left.data?.nodeWidth), right.position!.x + Number(right.data?.nodeWidth))
          - Math.max(left.position!.x, right.position!.x);
        const overlapY = Math.min(left.position!.y + Number(left.data?.nodeHeight), right.position!.y + Number(right.data?.nodeHeight))
          - Math.max(left.position!.y, right.position!.y);
        assert.ok(overlapX <= 0 || overlapY <= 0);
      }
    }
  });
});
