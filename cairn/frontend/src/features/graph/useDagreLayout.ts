import dagre from '@dagrejs/dagre';
import { estimateGraphNodeSize, resolveGraphNodeSize } from './nodeSizing';
import type { GraphNode, GraphEdge } from './graphFilters';

export const GRAPH_LAYOUT_RANK_MODES = {
  TIME: 'time',
  CRITICAL: 'critical',
} as const;

export type RankMode = typeof GRAPH_LAYOUT_RANK_MODES[keyof typeof GRAPH_LAYOUT_RANK_MODES];

export const LARGE_GRAPH_NODE_THRESHOLD = 30;
export const COMPACT_GRAPH_NODE_THRESHOLD = 55;
export const COMPACT_GRAPH_EDGE_THRESHOLD = 120;

const DEFAULT_LAYOUT_DIR = 'LR';
const DEFAULT_RANK_MODE = GRAPH_LAYOUT_RANK_MODES.TIME;
const MAX_ORDER_CONSTRAINTS = 180;
const DEFAULT_EDGE_MINLEN_LIMIT = 3;
const COMPACT_EDGE_MINLEN_LIMIT = 2;
const VALID_LAYOUT_DIRS = new Set(['LR', 'RL', 'TB', 'BT']);
const VALID_RANK_MODES = new Set<string>(Object.values(GRAPH_LAYOUT_RANK_MODES));
const BASE_GRAPH_SPACING = {
  nodesep: 60,
  ranksep: 100,
  edgesep: 20,
};

export interface LayoutOptions {
  dir?: string;
  rankMode?: RankMode;
  measuredNodeSizes?: Record<string, { width: number; height: number }> | null;
}

/**
 * 使用 dagre 对节点和边进行自动布局排序
 */
export function useDagreLayout(
  nodes: GraphNode[],
  edges: GraphEdge[],
  dirOrOptions: string | LayoutOptions = DEFAULT_LAYOUT_DIR
): GraphNode[] {
  const options = normalizeLayoutOptions(dirOrOptions);
  const causalNodes = nodes.filter(node => !isReasonNode(node));
  const reasonNodes = nodes.filter(isReasonNode);
  const causalNodeIds = new Set(causalNodes.map(node => node.id));
  const causalEdges = edges.filter(edge => (
    !isReasonAuditEdge(edge)
    && causalNodeIds.has(edge.source)
    && causalNodeIds.has(edge.target)
  ));
  const { graphOptions, layoutOptions, nodeTimeRanks, edgeMinlenLimit } = resolveDagreLayoutOptions(causalNodes, causalEdges, options);
  const g = new dagre.graphlib.Graph({ multigraph: true });
  const nodeSizes = new Map<string, { width: number; height: number; descriptionLines?: number }>();
  const layoutEdges = buildDagreLayoutEdges(causalEdges, nodeTimeRanks);

  g.setGraph(graphOptions);
  g.setDefaultEdgeLabel(() => ({}));

  sortGraphNodesForLayout(causalNodes, options.rankMode).forEach((node) => {
    const size = resolveGraphNodeSize(node, options.measuredNodeSizes?.[node.id]);
    nodeSizes.set(node.id, size);
    g.setNode(node.id, { width: size.width, height: size.height });
  });
  reasonNodes.forEach(node => {
    nodeSizes.set(node.id, resolveGraphNodeSize(node, options.measuredNodeSizes?.[node.id]));
  });

  layoutEdges.forEach((edge, index) => {
    if (!nodeSizes.has(edge.source) || !nodeSizes.has(edge.target)) return;
    g.setEdge(
      edge.source,
      edge.target,
      buildDagreEdgeLabel(edge, { rankMode: options.rankMode, nodeTimeRanks, maxMinlen: edgeMinlenLimit }),
      edge.id || `${edge.source}->${edge.target}:${index}`
    );
  });

  if (causalNodes.length > 0) {
    dagre.layout(g, layoutOptions);
  }

  const positions = new Map<string, { x: number; y: number }>();
  causalNodes.forEach(node => {
    const coords = g.node(node.id);
    const size = nodeSizes.get(node.id)!;
    if (!coords) return;
    positions.set(node.id, {
      x: coords.x - size.width / 2,
      y: coords.y - size.height / 2,
    });
  });
  positionReasonSidecars(reasonNodes, edges, positions, nodeSizes, options.dir).forEach((position, nodeId) => {
    positions.set(nodeId, position);
  });

  return nodes.map((node) => {
    const size = nodeSizes.get(node.id) || estimateGraphNodeSize(node);
    const position = positions.get(node.id);
    if (!position) return node;

    return {
      ...node,
      data: {
        ...node.data,
        nodeWidth: size.width,
        nodeHeight: size.height,
        descriptionLines: size.descriptionLines,
      },
      position,
    };
  });
}

export function resolveDagreLayoutOptions(
  nodes: GraphNode[] = [],
  edges: GraphEdge[] = [],
  options: LayoutOptions = {}
) {
  const normalized = normalizeLayoutOptions(options);
  const causalNodes = nodes.filter(node => !isReasonNode(node));
  const causalNodeIds = new Set(causalNodes.map(node => node.id));
  const causalEdges = edges.filter(edge => (
    !isReasonAuditEdge(edge)
    && causalNodeIds.has(edge.source)
    && causalNodeIds.has(edge.target)
  ));
  const nodeCount = causalNodes.length;
  const edgeCount = causalEdges.length;
  const isLargeGraph = nodeCount > LARGE_GRAPH_NODE_THRESHOLD;
  const isDenseGraph = shouldUseCompactLayout(nodeCount, edgeCount);
  const density = edgeCount / Math.max(1, nodeCount);
  const densityScale = isLargeGraph ? Math.min(0.55, Math.max(0, density - 1) * 0.22) : 0;
  const nodeScale = isDenseGraph
    ? 1.28 + Math.min(0.9, Math.max(0, nodeCount - COMPACT_GRAPH_NODE_THRESHOLD) / 95) + densityScale
    : isLargeGraph ? 1 + Math.min(1.2, (nodeCount - LARGE_GRAPH_NODE_THRESHOLD) / 60) + densityScale : 1;
  const rankScale = isDenseGraph
    ? 1.45 + Math.min(1.1, Math.max(0, nodeCount - COMPACT_GRAPH_NODE_THRESHOLD) / 80) + densityScale
    : isLargeGraph ? 1 + Math.min(1.5, (nodeCount - LARGE_GRAPH_NODE_THRESHOLD) / 50) + densityScale : 1;
  const ranker = chooseDagreRanker(nodeCount, edgeCount, normalized.rankMode);
  const nodeTimeRanks = buildNodeTimeRanks(causalNodes);
  const sortedNodes = sortGraphNodesForLayout(causalNodes, normalized.rankMode);
  const constraints = isDenseGraph ? [] : buildLayoutOrderConstraints(sortedNodes);

  return {
    graphOptions: {
      rankdir: normalized.dir,
      nodesep: Math.round(BASE_GRAPH_SPACING.nodesep * (nodeScale + densityScale)),
      ranksep: Math.round(BASE_GRAPH_SPACING.ranksep * rankScale),
      edgesep: Math.round(BASE_GRAPH_SPACING.edgesep * (isDenseGraph ? 1.8 : isLargeGraph ? 1.35 : 1)),
      align: 'UL',
      acyclicer: 'greedy',
      ranker,
      marginx: isDenseGraph ? 56 : isLargeGraph ? 40 : 20,
      marginy: isDenseGraph ? 56 : isLargeGraph ? 40 : 20,
    },
    layoutOptions: {
      constraints,
    },
    nodeTimeRanks,
    edgeMinlenLimit: isDenseGraph ? COMPACT_EDGE_MINLEN_LIMIT : DEFAULT_EDGE_MINLEN_LIMIT,
  };
}

export function buildDagreLayoutEdges(edges: GraphEdge[] = [], nodeTimeRanks: Map<string, number> = new Map()): GraphEdge[] {
  const standalone: GraphEdge[] = [];
  const edgeGroups = new Map<string, GraphEdge[]>();

  [...edges].sort(compareGraphEdges).forEach(edge => {
    const groupId = edge.data?.joinGroup?.id;
    // 审计边永远独立保留，不能被意图汇聚压缩吞掉。
    if (isReasonAuditEdge(edge) || !groupId) {
      standalone.push(edge);
      return;
    }
    if (!edgeGroups.has(groupId)) edgeGroups.set(groupId, []);
    edgeGroups.get(groupId)!.push(edge);
  });

  const representatives = [...edgeGroups.entries()]
    .sort(([left], [right]) => compareStrings(left, right))
    .map(([, groupEdges]) => [...groupEdges].sort((left, right) => {
      const rankDiff = edgeSourceRank(right, nodeTimeRanks) - edgeSourceRank(left, nodeTimeRanks);
      if (rankDiff !== 0) return rankDiff;
      const criticalDiff = Number(right.data?.isCriticalPath === true) - Number(left.data?.isCriticalPath === true);
      if (criticalDiff !== 0) return criticalDiff;
      return compareGraphEdges(left, right);
    })[0]);

  return [...standalone, ...representatives].sort(compareGraphEdges);
}

export function sortGraphNodesForLayout(nodes: GraphNode[] = [], rankMode: RankMode = DEFAULT_RANK_MODE): GraphNode[] {
  const normalizedRankMode = normalizeRankMode(rankMode);
  return [...nodes].sort((left, right) => {
    if (normalizedRankMode === GRAPH_LAYOUT_RANK_MODES.CRITICAL) {
      const criticalDiff = criticalWeight(right) - criticalWeight(left);
      if (criticalDiff !== 0) return criticalDiff;
    }

    const timeDiff = nodeTimeSortValue(left) - nodeTimeSortValue(right);
    if (timeDiff !== 0) return timeDiff;

    const kindDiff = nodeKindOrder(left) - nodeKindOrder(right);
    if (kindDiff !== 0) return kindDiff;

    return compareStrings(String(left.id), String(right.id));
  });
}

export function buildLayoutOrderConstraints(sortedNodes: GraphNode[] = [], maxConstraints = MAX_ORDER_CONSTRAINTS) {
  const constraints = [];
  const orderedIds = sortedNodes.map(node => node.id).filter(Boolean);
  const limit = Math.min(maxConstraints, Math.max(0, orderedIds.length - 1));
  for (let index = 0; index < limit; index += 1) {
    constraints.push({ left: orderedIds[index], right: orderedIds[index + 1] });
  }
  return constraints;
}

export function buildDagreEdgeLabel(
  edge: GraphEdge,
  {
    rankMode = DEFAULT_RANK_MODE,
    nodeTimeRanks = new Map<string, number>(),
    maxMinlen = DEFAULT_EDGE_MINLEN_LIMIT,
  }: { rankMode?: RankMode; nodeTimeRanks?: Map<string, number>; maxMinlen?: number } = {}
) {
  const normalizedRankMode = normalizeRankMode(rankMode);
  const isCriticalEdge = edge?.data?.isCriticalPath === true;
  const minlenLimit = clamp(Number(maxMinlen) || DEFAULT_EDGE_MINLEN_LIMIT, 1, DEFAULT_EDGE_MINLEN_LIMIT);
  const label: any = {
    minlen: 1,
    weight: isCriticalEdge ? 6 : 2,
  };

  if (normalizedRankMode === GRAPH_LAYOUT_RANK_MODES.TIME) {
    const sourceRank = nodeTimeRanks.get(edge.source);
    const targetRank = nodeTimeRanks.get(edge.target);
    const rankGap = Number.isFinite(sourceRank) && Number.isFinite(targetRank)
      ? targetRank! - sourceRank!
      : 1;
    label.minlen = clamp(Math.abs(rankGap), 1, minlenLimit);
    label.weight = isCriticalEdge ? 8 : Math.max(2, 5 - label.minlen);
  }

  if (normalizedRankMode === GRAPH_LAYOUT_RANK_MODES.CRITICAL) {
    label.minlen = isCriticalEdge ? 1 : 2;
    label.weight = isCriticalEdge ? 10 : 1;
  }

  return label;
}

export function buildNodeTimeRanks(nodes: GraphNode[] = []): Map<string, number> {
  const sortedNodes = sortGraphNodesForLayout(nodes, GRAPH_LAYOUT_RANK_MODES.TIME);
  const ranks = new Map<string, number>();
  let currentRank = -1;
  let previousTime: number | null = null;

  sortedNodes.forEach((node) => {
    const time = nodeTimeSortValue(node);
    if (previousTime === null || time !== previousTime) {
      currentRank += 1;
      previousTime = time;
    }
    ranks.set(node.id, currentRank);
  });

  return ranks;
}

function normalizeLayoutOptions(dirOrOptions: string | LayoutOptions = DEFAULT_LAYOUT_DIR): LayoutOptions & { dir: string; rankMode: RankMode } {
  const rawOptions = typeof dirOrOptions === 'object' && dirOrOptions !== null
    ? dirOrOptions
    : { dir: dirOrOptions };
  const dir = VALID_LAYOUT_DIRS.has(rawOptions.dir || '') ? rawOptions.dir! : DEFAULT_LAYOUT_DIR;
  return {
    dir,
    rankMode: normalizeRankMode(rawOptions.rankMode),
    measuredNodeSizes: rawOptions.measuredNodeSizes || null,
  };
}

function normalizeRankMode(rankMode: any): RankMode {
  return VALID_RANK_MODES.has(rankMode) ? rankMode : DEFAULT_RANK_MODE;
}

function chooseDagreRanker(nodeCount: number, edgeCount: number, rankMode: RankMode): string {
  if (rankMode === GRAPH_LAYOUT_RANK_MODES.CRITICAL) return 'network-simplex';
  if (nodeCount <= LARGE_GRAPH_NODE_THRESHOLD) return 'network-simplex';
  const isTreeLike = edgeCount <= Math.max(1, nodeCount * 1.25);
  return isTreeLike ? 'tight-tree' : 'network-simplex';
}

function shouldUseCompactLayout(nodeCount: number, edgeCount: number): boolean {
  return nodeCount >= COMPACT_GRAPH_NODE_THRESHOLD || edgeCount >= COMPACT_GRAPH_EDGE_THRESHOLD;
}

function nodeTimeSortValue(node: GraphNode): number {
  const data = node?.data || {};
  if (data.kind === 'origin' || node?.id === 'origin') return Number.NEGATIVE_INFINITY;
  if (data.kind === 'goal' || node?.id === 'goal') return Number.POSITIVE_INFINITY;

  const candidates = [
    data.createdAt,
    data.fact?.created_at,
    data.intent?.created_at,
    data.intent?.concluded_at,
    ...(data.pendingGroup?.intents || []).flatMap((intent: any) => [intent.created_at, intent.concluded_at]),
  ];
  const times = candidates
    .map(toTime)
    .filter(time => Number.isFinite(time));

  return times.length > 0 ? Math.min(...times) : Number.MAX_SAFE_INTEGER;
}

function nodeKindOrder(node: GraphNode): number {
  const kind = node?.data?.kind || '';
  if (kind === 'origin') return 0;
  if (kind === 'fact') return 1;
  if (kind === 'pending_group') return 2;
  if (kind === 'pending') return 3;
  if (kind === 'goal') return 4;
  if (kind === 'reason') return 5;
  return 6;
}

function criticalWeight(node: GraphNode): number {
  return node?.data?.isCriticalPath === true ? 1 : 0;
}

function positionReasonSidecars(
  reasonNodes: GraphNode[],
  edges: GraphEdge[],
  causalPositions: Map<string, { x: number; y: number }>,
  nodeSizes: Map<string, { width: number; height: number }>,
  dir: string
): Map<string, { x: number; y: number }> {
  const result = new Map<string, { x: number; y: number }>();
  if (reasonNodes.length === 0) return result;

  const isTopBottom = dir === 'TB' || dir === 'BT';
  const causalRects = [...causalPositions.entries()].map(([id, position]) => {
    const size = nodeSizes.get(id) || { width: 240, height: 150 };
    return {
      id,
      x: position.x,
      y: position.y,
      width: size.width,
      height: size.height,
      centerX: position.x + size.width / 2,
      centerY: position.y + size.height / 2,
    };
  });
  const minX = causalRects.length > 0 ? Math.min(...causalRects.map(rect => rect.x)) : 20;
  const minY = causalRects.length > 0 ? Math.min(...causalRects.map(rect => rect.y)) : 20;
  const graphCenterX = causalRects.length > 0
    ? (minX + Math.max(...causalRects.map(rect => rect.x + rect.width))) / 2
    : 140;
  const graphCenterY = causalRects.length > 0
    ? (minY + Math.max(...causalRects.map(rect => rect.y + rect.height))) / 2
    : 95;
  const rectById = new Map(causalRects.map(rect => [rect.id, rect]));
  const sidecarGap = 48;
  const itemGap = 28;

  const records = reasonNodes.map(node => {
    const endpointIds = reasonEndpointIds(node, edges).filter(id => rectById.has(id));
    const endpointCenters = endpointIds.map(id => rectById.get(id)!);
    const desiredPrimary = endpointCenters.length > 0
      ? median(endpointCenters.map(rect => isTopBottom ? rect.centerY : rect.centerX))
      : (isTopBottom ? graphCenterY : graphCenterX);
    return {
      node,
      size: nodeSizes.get(node.id) || { width: 240, height: 150 },
      desiredPrimary,
    };
  }).sort((left, right) => {
    const primaryDiff = left.desiredPrimary - right.desiredPrimary;
    if (primaryDiff !== 0) return primaryDiff;
    const timeDiff = nodeTimeSortValue(left.node) - nodeTimeSortValue(right.node);
    if (timeDiff !== 0) return timeDiff;
    return compareStrings(left.node.id, right.node.id);
  });

  let cursor = Number.NEGATIVE_INFINITY;
  records.forEach(record => {
    const primarySize = isTopBottom ? record.size.height : record.size.width;
    const desiredStart = record.desiredPrimary - primarySize / 2;
    const primaryStart = Number.isFinite(cursor) ? Math.max(desiredStart, cursor + itemGap) : desiredStart;
    cursor = primaryStart + primarySize;

    if (isTopBottom) {
      result.set(record.node.id, {
        x: minX - sidecarGap - record.size.width,
        y: primaryStart,
      });
    } else {
      result.set(record.node.id, {
        x: primaryStart,
        y: minY - sidecarGap - record.size.height,
      });
    }
  });

  return result;
}

function reasonEndpointIds(node: GraphNode, edges: GraphEdge[]): string[] {
  const endpointIds = new Set<string>();
  edges.forEach(edge => {
    if (!isReasonAuditEdge(edge)) return;
    if (edge.source === node.id && edge.target !== node.id) endpointIds.add(edge.target);
    if (edge.target === node.id && edge.source !== node.id) endpointIds.add(edge.source);
  });

  const reasonData: any = node.data?.reason || {};
  [...(reasonData.inputNodeIds || []), ...(reasonData.outputNodeIds || [])].forEach((id: any) => {
    if (id) endpointIds.add(String(id));
  });
  return [...endpointIds].sort(compareStrings);
}

function isReasonNode(node: GraphNode): boolean {
  return node?.data?.kind === 'reason';
}

function isReasonAuditEdge(edge: GraphEdge): boolean {
  return edge?.type === 'reason-audit';
}

function compareGraphEdges(left: GraphEdge, right: GraphEdge): number {
  const typeDiff = Number(isReasonAuditEdge(left)) - Number(isReasonAuditEdge(right));
  if (typeDiff !== 0) return typeDiff;
  const sourceDiff = compareStrings(String(left.source || ''), String(right.source || ''));
  if (sourceDiff !== 0) return sourceDiff;
  const targetDiff = compareStrings(String(left.target || ''), String(right.target || ''));
  if (targetDiff !== 0) return targetDiff;
  const groupDiff = compareStrings(String(left.data?.joinGroup?.id || ''), String(right.data?.joinGroup?.id || ''));
  if (groupDiff !== 0) return groupDiff;
  return compareStrings(String(left.id || ''), String(right.id || ''));
}

function compareStrings(left: string, right: string): number {
  return left < right ? -1 : left > right ? 1 : 0;
}

function median(values: number[]): number {
  if (values.length === 0) return 0;
  const sorted = [...values].sort((left, right) => left - right);
  const middle = Math.floor(sorted.length / 2);
  return sorted.length % 2 === 0
    ? (sorted[middle - 1] + sorted[middle]) / 2
    : sorted[middle];
}

function edgeSourceRank(edge: GraphEdge, nodeTimeRanks: Map<string, number>): number {
  const rank = nodeTimeRanks.get(edge.source);
  return Number.isFinite(rank) ? rank! : Number.NEGATIVE_INFINITY;
}

function toTime(value: any): number {
  if (!value) return Number.NaN;
  const parsed = Date.parse(value);
  return Number.isFinite(parsed) ? parsed : Number.NaN;
}

function clamp(value: number, min: number, max: number): number {
  return Math.min(max, Math.max(min, value));
}
