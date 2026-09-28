import type { GraphNode } from './graphFilters';

export const GRAPH_NODE_DEFAULT_WIDTH = 240;
export const GRAPH_NODE_MIN_HEIGHT = 150;
export const GRAPH_NODE_DECISION_OVERLAY_EXTRA_HEIGHT = 62;
export const GRAPH_NODE_DESCRIPTION_LINES = 2;
export const GRAPH_NODE_CONCLUSION_LINES = 1;

interface WidthStep {
  minLength: number;
  width: number;
  charsPerLine: number;
}

const WIDTH_STEPS: WidthStep[] = [
  { minLength: 80, width: 320, charsPerLine: 34 },
  { minLength: 56, width: 280, charsPerLine: 30 },
  { minLength: 0, width: GRAPH_NODE_DEFAULT_WIDTH, charsPerLine: 25 },
];

function displayLength(value: any): number {
  return Array.from(String(value || '').trim()).reduce((sum, char) => {
    return sum + (/[\u3040-\u30ff\u3400-\u9fff\uf900-\ufaff]/u.test(char) ? 2 : 1);
  }, 0);
}

function normalizeInlineText(value: any): string {
  return String(value || '').replace(/\s+/g, ' ').trim();
}

function minPositive(values: number[]): number {
  return values.filter(value => value >= 0).reduce((min, value) => Math.min(min, value), Number.POSITIVE_INFINITY);
}

function measuredSlice(value: string, maxLength: number): number {
  let currentLength = 0;
  let index = 0;
  for (const char of Array.from(value)) {
    const nextLength = currentLength + displayLength(char);
    if (nextLength > maxLength) break;
    currentLength = nextLength;
    index += char.length;
  }
  return index;
}

function fallbackSummaryCutIndex(value: string): number {
  const maxConclusionLength = 34;
  if (displayLength(value) <= maxConclusionLength + 12) return -1;

  const softMarks = ['，', ',', '；', ';', '、', ':', '：'];
  const candidates = softMarks
    .map(mark => value.indexOf(mark))
    .filter(index => index > 0 && displayLength(value.slice(0, index + 1)) <= maxConclusionLength + 8);
  if (candidates.length > 0) {
    return Math.max(...candidates) + 1;
  }

  return measuredSlice(value, maxConclusionLength);
}

export interface NodeSummary {
  conclusion: string;
  summary: string;
}

export function splitGraphNodeSummary(value: any): NodeSummary {
  const rawText = String(value || '').trim();
  if (!rawText) {
    return { conclusion: '', summary: '' };
  }

  const sentenceMatch = /[。！？!?\.]/u.exec(rawText);
  const newlineIndex = rawText.search(/\r?\n/u);
  const sentenceCutIndex = sentenceMatch ? sentenceMatch.index + sentenceMatch[0].length : -1;
  let cutIndex = minPositive([sentenceCutIndex, newlineIndex]);

  if (!Number.isFinite(cutIndex)) {
    cutIndex = fallbackSummaryCutIndex(rawText);
  }

  if (cutIndex <= 0 || cutIndex >= rawText.length) {
    return {
      conclusion: normalizeInlineText(rawText),
      summary: '',
    };
  }

  return {
    conclusion: normalizeInlineText(rawText.slice(0, cutIndex)),
    summary: normalizeInlineText(rawText.slice(cutIndex)),
  };
}

function estimateMetaChipCount(data: any): number {
  let count = 1; // evidence/live chip is always rendered as the trailing audit signal.
  if (data.worker || data.creator) count += 1;
  if (data.taskRunId) count += 1;
  return count;
}

export interface EstimatedNodeSize {
  width: number;
  height: number;
  descriptionLines: number;
}

export interface MeasuredNodeSize {
  width: number;
  height: number;
}

export function resolveGraphNodeSize(
  node: GraphNode,
  measuredSize?: MeasuredNodeSize | null
): EstimatedNodeSize {
  const estimated = estimateGraphNodeSize(node);
  const measuredWidth = Number(measuredSize?.width);
  const measuredHeight = Number(measuredSize?.height);
  return {
    ...estimated,
    width: Number.isFinite(measuredWidth) && measuredWidth > 0 ? measuredWidth : estimated.width,
    height: Number.isFinite(measuredHeight) && measuredHeight > 0 ? measuredHeight : estimated.height,
  };
}

export function estimateGraphNodeSize(node: GraphNode): EstimatedNodeSize {
  const data = node?.data || {};
  const labelLength = displayLength(data.label);
  const step = WIDTH_STEPS.find(item => labelLength >= item.minLength) || WIDTH_STEPS[WIDTH_STEPS.length - 1];
  const summary = splitGraphNodeSummary(data.label);
  const descriptionLines = GRAPH_NODE_DESCRIPTION_LINES;
  const conclusionLines = summary.conclusion ? GRAPH_NODE_CONCLUSION_LINES : 0;
  const visibleSummaryLines = summary.summary ? descriptionLines : 0;
  const metaChipCount = estimateMetaChipCount(data);
  const metaRows = data.kind === 'pending' && metaChipCount >= 3 ? 2 : 1;
  const liveExtra = data.kind === 'pending' && (data.liveState || data.elapsedTaskSeconds !== undefined) ? 6 : 0;
  const decisionOverlayExtra = data.decisionOverlay ? GRAPH_NODE_DECISION_OVERLAY_EXTRA_HEIGHT : 0;
  const pendingGroupExtra = data.kind === 'pending_group' ? (data.pendingGroup?.hasCriticalPath ? 44 : 24) : 0;
  const contentHeight = 72 + conclusionLines * 20 + visibleSummaryLines * 17 + metaRows * 24 + liveExtra + decisionOverlayExtra + pendingGroupExtra;
  const minimumHeight = GRAPH_NODE_MIN_HEIGHT + liveExtra + decisionOverlayExtra + pendingGroupExtra;
  const height = Math.max(
    GRAPH_NODE_MIN_HEIGHT,
    minimumHeight,
    contentHeight,
  );

  return {
    width: step.width,
    height,
    descriptionLines,
  };
}
