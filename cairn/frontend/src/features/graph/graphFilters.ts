import { statusText } from '@/lib/format';

export type { GraphEdge, GraphNode } from './graphTypes';
export { collapsePendingBranches } from './graphCollapse';
export { applyManualCollapse } from './graphManualCollapse';
export { filterGraphElements } from './graphSearchFilters';

export const GRAPH_STATUS_FILTERS = [
  { value: 'all', label: '全部' },
  { value: 'open', label: statusText('unfinished') },
  { value: 'running', label: statusText('exploring') },
  { value: 'completed', label: statusText('concluded') },
  { value: 'critical', label: statusText('critical_path') },
];
