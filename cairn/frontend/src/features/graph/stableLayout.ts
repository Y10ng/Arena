import type { GraphNode } from './graphFilters';

interface Position {
  x: number;
  y: number;
}

function isFinitePosition(position: any): position is Position {
  return (
    position
    && Number.isFinite(Number(position.x))
    && Number.isFinite(Number(position.y))
  );
}

function clonePosition(position: Position): Position {
  return {
    x: Number(position.x),
    y: Number(position.y),
  };
}

export interface StabilizeOptions {
  previousNodes?: GraphNode[];
  previousContextKey?: string;
  contextKey?: string;
  pinnedPositions?: Record<string, Position> | null;
  maxAlignmentShift?: number;
  maxNodeMovement?: number;
}

export interface SeparateOverlapsOptions {
  dir?: string;
  nodeGap?: number;
  rankTolerance?: number;
}

interface MutableSizedNode {
  node: GraphNode;
  width: number;
  height: number;
  x: number;
  y: number;
}

const DEFAULT_NODE_GAP = 32;
const DEFAULT_RANK_TOLERANCE = 120;
const DEFAULT_NODE_WIDTH = 180;
const DEFAULT_NODE_HEIGHT = 80;
const DEFAULT_MAX_ALIGNMENT_SHIFT = 180;
const DEFAULT_MAX_NODE_MOVEMENT = 72;

export function stabilizeGraphNodePositions(
  nodes: GraphNode[],
  {
    previousNodes = [],
    previousContextKey = '',
    contextKey = '',
    pinnedPositions = null,
    maxAlignmentShift = DEFAULT_MAX_ALIGNMENT_SHIFT,
    maxNodeMovement = DEFAULT_MAX_NODE_MOVEMENT,
  }: StabilizeOptions = {}
): GraphNode[] {
  if (!previousContextKey || !contextKey) return applyPinnedPositions(nodes, pinnedPositions);

  const prevParts = previousContextKey.split('|');
  const currParts = contextKey.split('|');

  // 项目、布局方向或排序模式变化时必须使用全新布局。
  if (prevParts[0] !== currParts[0] || prevParts[1] !== currParts[1] || prevParts[2] !== currParts[2]) {
    return applyPinnedPositions(nodes, pinnedPositions);
  }

  const previousById = new Map<string, Position>(
    (previousNodes || [])
      .filter(node => node?.id && isFinitePosition(node.position))
      .map(node => [node.id, clonePosition(node.position!)])
  );

  if (previousById.size === 0) return applyPinnedPositions(nodes, pinnedPositions);

  const causalSharedNodes = (nodes || []).filter(node => (
    node.data?.kind !== 'reason'
    && previousById.has(node.id)
    && isFinitePosition(node.position)
  ));
  const dxValues = causalSharedNodes.map(node => previousById.get(node.id)!.x - Number(node.position!.x));
  const dyValues = causalSharedNodes.map(node => previousById.get(node.id)!.y - Number(node.position!.y));

  const alignment = {
    x: clamp(median(dxValues), -Math.max(0, maxAlignmentShift), Math.max(0, maxAlignmentShift)),
    y: clamp(median(dyValues), -Math.max(0, maxAlignmentShift), Math.max(0, maxAlignmentShift)),
  };
  const movementLimit = Math.max(0, maxNodeMovement);

  const stabilized = (nodes || []).map(node => {
    if (!isFinitePosition(node.position)) return node;

    const aligned = {
      x: Number(node.position.x) + alignment.x,
      y: Number(node.position.y) + alignment.y,
    };
    const previous = previousById.get(node.id);
    if (!previous) {
      return withPosition(node, aligned);
    }

    // 共享节点以旧位置为惯性基准，但允许新布局在有限范围内消除累积拥挤。
    return withPosition(node, {
      x: previous.x + clamp(aligned.x - previous.x, -movementLimit, movementLimit),
      y: previous.y + clamp(aligned.y - previous.y, -movementLimit, movementLimit),
    });
  });

  return applyPinnedPositions(stabilized, pinnedPositions);
}

export function stableLayout(
  nodes: GraphNode[],
  previousCoords: Record<string, { x: number; y: number }>
): GraphNode[] {
  if (!previousCoords || Object.keys(previousCoords).length === 0) return nodes;

  const previousNodes = Object.entries(previousCoords).map(([id, position]) => ({
    id,
    position,
    data: {},
  })) as GraphNode[];

  return stabilizeGraphNodePositions(nodes, {
    previousNodes,
    previousContextKey: 'legacy|same|same',
    contextKey: 'legacy|same|same',
  });
}

export function separateGraphNodeOverlaps(
  nodes: GraphNode[],
  {
    dir = 'LR',
    nodeGap = DEFAULT_NODE_GAP,
    rankTolerance = DEFAULT_RANK_TOLERANCE,
  }: SeparateOverlapsOptions = {}
): GraphNode[] {
  if (!Array.isArray(nodes) || nodes.length <= 1) return nodes;

  const isTopBottom = dir === 'TB' || dir === 'BT';
  const gap = Math.max(0, nodeGap);
  const tolerance = Math.max(0, rankTolerance);
  const mutable = nodes
    .filter(node => isFinitePosition(node.position))
    .map(node => ({
      node,
      width: positiveSize(node.data?.nodeWidth, DEFAULT_NODE_WIDTH),
      height: positiveSize(node.data?.nodeHeight, DEFAULT_NODE_HEIGHT),
      x: Number(node.position!.x),
      y: Number(node.position!.y),
    }));

  if (mutable.length <= 1) return nodes;

  const causalOrdered = mutable
    .filter(item => item.node.data?.kind !== 'reason')
    .sort((left, right) => compareSizedNodes(left, right, isTopBottom));
  const reasonOrdered = mutable
    .filter(item => item.node.data?.kind === 'reason')
    .sort((left, right) => compareSizedNodes(left, right, isTopBottom));

  placeNodesWithoutOverlap(causalOrdered, [], isTopBottom, gap, tolerance);
  placeNodesWithoutOverlap(reasonOrdered, causalOrdered, isTopBottom, gap, tolerance);

  const positionById = new Map(
    [...causalOrdered, ...reasonOrdered].map(item => [item.node.id, { x: item.x, y: item.y }])
  );
  return nodes.map(node => {
    const position = positionById.get(node.id);
    if (!position || !isFinitePosition(node.position)) return node;
    if (nearlyEqual(position.x, Number(node.position.x)) && nearlyEqual(position.y, Number(node.position.y))) return node;
    return withPosition(node, position);
  });
}

function applyPinnedPositions(
  nodes: GraphNode[],
  pinnedPositions: Record<string, Position> | null | undefined
): GraphNode[] {
  if (!pinnedPositions) return nodes;
  return nodes.map(node => {
    const pinned = pinnedPositions[node.id];
    return isFinitePosition(pinned) ? withPosition(node, clonePosition(pinned)) : node;
  });
}

function withPosition(node: GraphNode, position: Position): GraphNode {
  if (
    isFinitePosition(node.position)
    && nearlyEqual(Number(node.position.x), position.x)
    && nearlyEqual(Number(node.position.y), position.y)
  ) {
    return node;
  }
  return {
    ...node,
    position,
  };
}

function placeNodesWithoutOverlap(
  nodes: MutableSizedNode[],
  fixedNodes: MutableSizedNode[],
  isTopBottom: boolean,
  gap: number,
  tolerance: number
): void {
  nodes.forEach((current, index) => {
    const placed = [...fixedNodes, ...nodes.slice(0, index)];
    for (let attempt = 0; attempt < Math.max(1, placed.length * 2); attempt += 1) {
      const blocker = placed.find(other => (
        requiredSeparation(current.x, current.width, other.x, other.width, gap) > 0
        && requiredSeparation(current.y, current.height, other.y, other.height, gap) > 0
      ));
      if (!blocker) break;

      // 因果节点先固定，Reason 只向外侧审计通道移动，不能反向推挤主链。
      if (isTopBottom) {
        current.x = blocker.x + blocker.width + gap + Math.min(0.01, tolerance / 10000);
      } else {
        current.y = blocker.y + blocker.height + gap + Math.min(0.01, tolerance / 10000);
      }
    }
  });
}

function compareSizedNodes(left: MutableSizedNode, right: MutableSizedNode, isTopBottom: boolean): number {
  const leftPrimary = isTopBottom ? center(left.y, left.height) : center(left.x, left.width);
  const rightPrimary = isTopBottom ? center(right.y, right.height) : center(right.x, right.width);
  if (!nearlyEqual(leftPrimary, rightPrimary)) return leftPrimary - rightPrimary;

  const leftSecondary = isTopBottom ? center(left.x, left.width) : center(left.y, left.height);
  const rightSecondary = isTopBottom ? center(right.x, right.width) : center(right.y, right.height);
  if (!nearlyEqual(leftSecondary, rightSecondary)) return leftSecondary - rightSecondary;
  return compareIds(left.node.id, right.node.id);
}

function requiredSeparation(
  firstStart: number,
  firstSize: number,
  secondStart: number,
  secondSize: number,
  gap: number
): number {
  return (firstSize + secondSize) / 2 + gap
    - Math.abs(center(firstStart, firstSize) - center(secondStart, secondSize));
}

function positiveSize(value: any, fallback: number): number {
  const parsed = Number(value);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : fallback;
}

function center(start: number, size: number): number {
  return start + size / 2;
}

function median(values: number[]): number {
  if (values.length === 0) return 0;
  const sorted = [...values].sort((left, right) => left - right);
  const middle = Math.floor(sorted.length / 2);
  return sorted.length % 2 === 0
    ? (sorted[middle - 1] + sorted[middle]) / 2
    : sorted[middle];
}

function compareIds(left: string, right: string): number {
  return left < right ? -1 : left > right ? 1 : 0;
}

function nearlyEqual(left: number, right: number): boolean {
  return Math.abs(left - right) < 0.001;
}

function clamp(value: number, min: number, max: number): number {
  return Math.min(max, Math.max(min, value));
}
