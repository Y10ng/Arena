export const EMPTY = '—';

export function known(values: any[], separator = ' / '): string {
  return values
    .filter((value) => value !== null && value !== undefined && value !== '')
    .join(separator);
}

export function auditValue(value: any): string {
  if (value === null || value === undefined || value === '') return '';
  if (typeof value === 'boolean') return value ? '是' : '否';
  if (typeof value === 'object') {
    try {
      return JSON.stringify(value);
    } catch {
      return String(value);
    }
  }
  return String(value);
}

export function dateTime(value: any): string {
  if (!value) return '';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return date.toLocaleString('zh-CN', {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  });
}

export function timeOnly(value: any): string {
  if (!value) return '';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return date.toLocaleTimeString('zh-CN', {
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  });
}

export function duration(seconds: any): string {
  if (seconds === null || seconds === undefined || seconds === '') return '';
  const totalSeconds = Math.max(0, Math.round(Number(seconds || 0)));
  const hours = Math.floor(totalSeconds / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  const remainingSeconds = totalSeconds % 60;
  if (hours > 0) return `${hours}小时${minutes}分`;
  if (minutes > 0) return `${minutes}分${remainingSeconds}秒`;
  return `${remainingSeconds}秒`;
}

export function milliseconds(value: any): string {
  if (value === null || value === undefined || value === '') return '';
  const ms = Math.max(0, Number(value || 0));
  if (ms < 1000) return `${Math.round(ms)}毫秒`;
  return `${(ms / 1000).toFixed(ms >= 10000 ? 1 : 2)}秒`;
}

export function bytes(value: any): string {
  const size = Math.max(0, Number(value || 0));
  if (size < 1024) return `${size} B`;
  const units = ['KB', 'MB', 'GB', 'TB'];
  let scaled = size / 1024;
  let unitIndex = 0;
  while (scaled >= 1024 && unitIndex < units.length - 1) {
    scaled /= 1024;
    unitIndex += 1;
  }
  return `${scaled.toFixed(scaled >= 10 ? 0 : 1)} ${units[unitIndex]}`;
}

export function countMap(counts: Record<string, any> = {}, separator = ' / '): string {
  if (!counts || typeof counts !== 'object' || Array.isArray(counts)) return '';
  return Object.entries(counts)
    .filter(([, count]) => Number(count || 0) > 0)
    .map(([key, count]) => `${key}=${count}`)
    .join(separator);
}

export const AUDIT_COUNT_LABELS: Record<string, string> = Object.freeze({
  valid: '有效',
  invalid: '无效',
  rejected: '拒绝',
  complete: '完成',
  fact: '事实',
  intents: '新方向',
  noop: '无操作',
  success: '成功',
  failed: '失败',
  skipped: '跳过',
  observed: '已观察',
  allowed: '允许',
  blocked: '阻塞',
  unknown: '未知',
  started: '开始',
  submitted: '已提交',
  planned: '计划',
  denied: '拒绝',
  succeeded: '成功',
  stopped: '停止',
  ended: '结束',
  low: '低',
  medium: '中',
  high: '高',
  high_risk: '高风险',
  low_confidence: '低置信',
  target_dependency: 'Target 依赖',
  created_intent: '提出方向',
  concluded_created_intent: '已结论',
  goal_path_created_intent: '进入完成链路',
  open_created_intent: '仍打开',
  missing_created_intent: '引用缺失',
});

export function auditCountLabel(key: string): string {
  return AUDIT_COUNT_LABELS[key] || key;
}

export const RISK_LEVEL_LABELS: Record<string, string> = Object.freeze({
  low: '低',
  medium: '中',
  high: '高',
  unknown: '未知',
});

export function riskLevelText(value: any): string {
  if (value === null || value === undefined || value === '') return '';
  const key = String(value || 'unknown').toLowerCase();
  return RISK_LEVEL_LABELS[key] || String(value);
}

export function formatAuditCounts(counts: Record<string, any> = {}, keys: string[] = [], separator = ' / '): string {
  if (!counts || typeof counts !== 'object' || Array.isArray(counts)) return '';
  return keys
    .filter((key) => Number(counts?.[key] || 0) > 0)
    .map((key) => `${auditCountLabel(key)}=${counts[key]}`)
    .join(separator);
}

export const PROJECT_FILTER_OPTIONS = Object.freeze([
  { value: 'all', label: '全部' },
  { value: 'active', label: '活跃' },
  { value: 'paused', label: '暂停' },
  { value: 'stopped', label: '结束' },
  { value: 'completed', label: '完成' },
]);

export const TASK_TYPE_LABELS: Record<string, string> = Object.freeze({
  bootstrap: '启动',
  reason: '推理',
  explore: '探索',
  verify: '验证',
  conclude: '收敛',
});

export function taskTypeText(value: any): string {
  return TASK_TYPE_LABELS[value] || value || '';
}

export interface Artifact {
  id?: string;
  project_id?: string;
  kind?: string;
  kind_deprecated?: boolean;
  deprecation_message?: string;
}

export function artifactKindLabel(artifact: Artifact): string {
  const kind = artifact?.kind || '';
  if (!kind) return '';
  return artifact?.kind_deprecated ? `${kind}（冻结兼容）` : kind;
}

export function artifactDeprecationText(artifact: Artifact): string {
  if (!artifact?.kind_deprecated) return '';
  return artifact?.deprecation_message || '冻结兼容：当前阶段不再新建或采集此类产物，仅保留历史读取。';
}

export function evidencePointerTypeText(pointer = ''): string {
  const text = String(pointer || '').trim();
  if (!text) return '未指定';
  const lower = text.toLowerCase();
  if (
    lower === 'stdout' ||
    lower === 'stderr' ||
    lower.startsWith('stdout:') ||
    lower.startsWith('stderr:') ||
    lower.startsWith('tail:')
  ) {
    return '输出片段';
  }
  if (
    lower.startsWith('line:') ||
    lower.startsWith('lines:') ||
    /^l\d+([:-]l?\d+)?$/i.test(text)
  ) {
    return '行号';
  }
  if (lower.startsWith('json:') || lower.startsWith('jsonpath:') || text === '$' || text.startsWith('$.')) {
    return 'JSON Path';
  }
  if (lower.startsWith('bbox:') || lower.startsWith('xywh=') || lower.startsWith('region:')) {
    return '截图区域';
  }
  if (text.includes('#')) return '报告锚点';
  return '自定义指针';
}

export function isArtifactImagePreviewable(artifact: Artifact): boolean {
  return artifact?.kind === 'screenshot';
}

export function artifactContentUrl(artifact: Artifact, projectId = ''): string {
  const resolvedProjectId = projectId || artifact?.project_id || '';
  if (!resolvedProjectId || !artifact?.id || !isArtifactImagePreviewable(artifact)) return '';
  return `/projects/${encodeURIComponent(resolvedProjectId)}/artifacts/${encodeURIComponent(artifact.id)}/content`;
}

export function diffLineKind(line = ''): string {
  if (line.startsWith('+++') || line.startsWith('---')) return 'meta';
  if (line.startsWith('@@')) return 'hunk';
  if (line.startsWith('+')) return 'add';
  if (line.startsWith('-')) return 'remove';
  return 'context';
}

export function diffPreviewLines(content = ''): any[] {
  return String(content || '').split(/\r?\n/).map((text, index) => ({
    key: `${index}:${text}`,
    text,
    kind: diffLineKind(text),
  }));
}

export interface StatusMeta {
  text: string;
  tone: string;
}

export const STATUS_DEFINITIONS: Record<string, StatusMeta> = Object.freeze({
  active: { text: '活跃', tone: 'info' },
  paused: { text: '已暂停', tone: 'neutral' },
  pausing: { text: '正在暂停', tone: 'warn' },
  stopping: { text: '正在结束', tone: 'warn' },
  starting: { text: '启动中', tone: 'info' },
  stopped: { text: '已结束', tone: 'neutral' },
  completed: { text: '已完成', tone: 'good' },
  queued: { text: '排队中', tone: 'neutral' },
  running: { text: '运行中', tone: 'info' },
  bootstrap: { text: '启动', tone: 'info' },
  started: { text: '已开始', tone: 'info' },
  submitted: { text: '已提交', tone: 'info' },
  planned: { text: '计划', tone: 'info' },
  exploring: { text: '探索中', tone: 'info' },
  unclaimed: { text: '未领取', tone: 'neutral' },
  unfinished: { text: '未完成', tone: 'warn' },
  concluded: { text: '已结论', tone: 'good' },
  confirmed: { text: '已确认', tone: 'good' },
  normal: { text: '已确认', tone: 'good' },
  critical_path: { text: '关键路径', tone: 'good' },
  seed: { text: '种子', tone: 'good' },
  discovered: { text: '发现', tone: 'info' },
  approved: { text: '批准', tone: 'good' },
  succeeded: { text: '成功', tone: 'good' },
  success: { text: '成功', tone: 'good' },
  healthy: { text: '健康', tone: 'good' },
  allowed: { text: '允许', tone: 'good' },
  valid: { text: '有效', tone: 'good' },
  evidence_valid: { text: '证据有效', tone: 'good' },
  failed: { text: '失败', tone: 'bad' },
  unhealthy: { text: '异常', tone: 'bad' },
  invalid: { text: '无效', tone: 'bad' },
  evidence_broken: { text: '证据断链', tone: 'bad' },
  rejected: { text: '拒绝', tone: 'bad' },
  parse_error: { text: '解析失败', tone: 'bad' },
  timed_out: { text: '超时', tone: 'warn' },
  stale: { text: '过期', tone: 'warn' },
  denied: { text: '拒绝', tone: 'warn' },
  cancelled: { text: '已取消', tone: 'warn' },
  blocked: { text: '阻塞', tone: 'warn' },
  cooldown: { text: '冷却', tone: 'warn' },
  capacity_full: { text: '容量已满', tone: 'warn' },
  rejected_backoff: { text: '拒绝退避', tone: 'bad' },
  schedulable: { text: '可调度', tone: 'good' },
  idle: { text: '空闲', tone: 'neutral' },
  fixed_node: { text: '固定节点', tone: 'neutral' },
  missing: { text: '缺失', tone: 'neutral' },
  not_tracked: { text: '未跟踪', tone: 'neutral' },
  skipped: { text: '跳过', tone: 'neutral' },
  unknown: { text: '未知', tone: 'neutral' },
  ended: { text: '已结束', tone: 'neutral' },
});

export function statusMeta(status: any): StatusMeta {
  if (status === null || status === undefined || status === '') {
    return { text: EMPTY, tone: 'neutral' };
  }
  const value = String(status || 'unknown').toLowerCase();
  const meta = STATUS_DEFINITIONS[value];
  if (meta) return meta;
  return { text: status || EMPTY, tone: 'neutral' };
}

export function statusText(status: any): string {
  return statusMeta(status).text;
}

export function badgeTone(status: any): string {
  return statusMeta(status).tone;
}

export const MONITOR_STATE_FILTER_OPTIONS = Object.freeze([
  { value: 'all', label: '全部' },
  { value: 'running', label: statusText('running') },
  { value: 'queued', label: statusText('queued') },
  { value: 'failed', label: statusText('failed') },
]);

export function intentStatusKey(intent: any): string {
  if (intent?.to) return 'concluded';
  if (intent?.worker) return 'exploring';
  return 'unclaimed';
}

export function intentStatusMeta(intent: any): StatusMeta {
  return statusMeta(intentStatusKey(intent));
}

export function formatRelativeTime(value: any): string {
  if (!value) return '';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  
  const now = new Date();
  const diffMs = now.getTime() - date.getTime();
  const diffSec = Math.floor(diffMs / 1000);
  
  if (diffSec < 10) return '刚刚';
  if (diffSec < 60) return `${diffSec}秒前`;
  
  const diffMin = Math.floor(diffSec / 60);
  if (diffMin < 60) return `${diffMin}分钟前`;
  
  const diffHour = Math.floor(diffMin / 60);
  if (diffHour < 24) return `${diffHour}小时前`;
  
  const diffDay = Math.floor(diffHour / 24);
  if (diffDay < 30) return `${diffDay}天前`;
  
  return date.toLocaleDateString('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit'
  });
}
