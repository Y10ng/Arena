import type { GraphEdge, GraphNode } from './graphTypes';

export function filterGraphElements(nodes: GraphNode[] = [], edges: GraphEdge[] = [], { status = 'all', query = '' } = {}) {
  const normalizedQuery = normalizeQuery(query);
  const statusFilteredEdgeIds = new Set<string>();
  const statusFilteredNodeIds = new Set<string>();

  edges.forEach(edge => {
    if (!matchesStatus(edge, status)) return;
    statusFilteredEdgeIds.add(edge.id);
    statusFilteredNodeIds.add(edge.source);
    statusFilteredNodeIds.add(edge.target);
  });
  nodes.forEach(node => {
    if (matchesNodeStatus(node, status)) {
      statusFilteredNodeIds.add(node.id);
    }
  });

  const statusNodes = status === 'all' ? nodes : nodes.filter(node => statusFilteredNodeIds.has(node.id));
  const statusEdges = status === 'all' ? edges : edges.filter(edge => statusFilteredEdgeIds.has(edge.id));

  if (!normalizedQuery) {
    return {
      nodes: statusNodes.map(node => ({ ...node, data: { ...node.data, isSearchMatch: false } })),
      edges: statusEdges,
      matchCount: 0
    };
  }

  const matchedNodeIds = new Set<string>();
  const matchedEdgeIds = new Set<string>();
  const directMatchedNodeIds = new Set<string>();

  statusNodes.forEach(node => {
    if (matchesQuery(nodeSearchText(node), normalizedQuery)) {
      matchedNodeIds.add(node.id);
      directMatchedNodeIds.add(node.id);
    }
  });
  statusEdges.forEach(edge => {
    if (matchesQuery(edgeSearchText(edge), normalizedQuery)) {
      matchedEdgeIds.add(edge.id);
      matchedNodeIds.add(edge.source);
      matchedNodeIds.add(edge.target);
    }
  });
  statusEdges.forEach(edge => {
    if (directMatchedNodeIds.has(edge.source) || directMatchedNodeIds.has(edge.target)) {
      matchedEdgeIds.add(edge.id);
      matchedNodeIds.add(edge.source);
      matchedNodeIds.add(edge.target);
    }
  });

  const filteredNodes = statusNodes
    .filter(node => matchedNodeIds.has(node.id))
    .map(node => ({
      ...node,
      data: {
        ...node.data,
        isSearchMatch: matchesQuery(nodeSearchText(node), normalizedQuery)
      }
    }));

  return {
    nodes: filteredNodes,
    edges: statusEdges.filter(edge => matchedEdgeIds.has(edge.id)),
    matchCount: filteredNodes.filter(node => node.data?.isSearchMatch).length + matchedEdgeIds.size
  };
}

function matchesStatus(edge: GraphEdge, status: string): boolean {
  if (status === 'all') return true;
  const state = edge.data?.state || '';
  if (edge.type === 'reason-audit') {
    if (status === 'open') return state === 'queued' || state === 'running';
    if (status === 'running') return state === 'running';
    if (status === 'completed') return Boolean(state) && state !== 'queued' && state !== 'running';
    if (status === 'critical') return false;
  }
  if (status === 'open') return state === 'unclaimed' || state === 'exploring';
  if (status === 'running') return state === 'exploring';
  if (status === 'completed') return state === 'concluded' || state === 'bootstrap';
  if (status === 'critical') return edge.data?.isCriticalPath === true;
  return true;
}

function matchesNodeStatus(node: GraphNode, status: string): boolean {
  if (status === 'all') return true;
  if (node.data?.kind === 'reason') {
    const runStatus = node.data?.runStatus || node.data?.status || '';
    if (status === 'open') return runStatus === 'queued' || runStatus === 'running';
    if (status === 'running') return runStatus === 'running';
    if (status === 'completed') return Boolean(runStatus) && runStatus !== 'queued' && runStatus !== 'running';
    if (status === 'critical') return false;
  }
  if (status === 'open') return node.data?.kind === 'pending' || node.data?.kind === 'pending_group';
  if (status === 'running') {
    return (node.data?.kind === 'pending' || node.data?.kind === 'pending_group') && node.data?.status === 'exploring';
  }
  if (status === 'completed') return node.data?.kind !== 'pending' && node.data?.kind !== 'pending_group';
  if (status === 'critical') return node.data?.isCriticalPath === true;
  return true;
}

function nodeSearchText(node: GraphNode): string {
  const data = node.data || {};
  const fact = data.fact || {};
  const intent = data.intent || {};
  return [
    node.id,
    data.label,
    fact.id,
    fact.description,
    intent.id,
    intent.description,
    data.worker,
    data.creator,
    data.taskRunId,
    data.pendingGroup?.sourceId,
    data.pendingGroup?.intentIds?.join(' '),
    data.pendingGroup?.intents?.map((intent: any) => intent.description).join(' '),
    data.reason?.outcome,
    data.reason?.rationale,
    data.reason?.inputNodeIds?.join(' '),
    data.reason?.outputNodeIds?.join(' '),
    data.pendingGroup ? '未完成 探索 分支 折叠' : '',
    data.reason ? 'Reason 决策 推理 审计' : ''
  ].filter(Boolean).join(' ');
}

function edgeSearchText(edge: GraphEdge): string {
  const intent = edge.data?.intent || {};
  const pendingGroup = edge.data?.pendingGroup || {};
  return [
    edge.id,
    edge.source,
    edge.target,
    edge.data?.label,
    edge.data?.state,
    edge.data?.worker,
    edge.data?.taskRunId,
    intent.id,
    intent.description,
    pendingGroup.sourceId,
    pendingGroup.intentIds?.join(' '),
    pendingGroup.intents?.map((item: any) => item.description).join(' ')
  ].filter(Boolean).join(' ');
}

function normalizeQuery(value: any): string {
  return String(value || '').trim().toLowerCase();
}

function matchesQuery(text: any, query: string): boolean {
  return String(text || '').toLowerCase().includes(query);
}
