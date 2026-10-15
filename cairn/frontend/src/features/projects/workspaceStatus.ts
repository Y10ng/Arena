import type { ProjectDetail, ProjectRuntime, ProjectSummary } from '@/api/types'
import { isSupportedProjectRuntime } from './projectRuntime'

export type WorkspaceStatusCode = string
export type WorkspaceStatusTone = 'neutral' | 'info' | 'warning' | 'success' | 'danger'

export interface WorkspaceStatusView {
  code: WorkspaceStatusCode
  label: string
  detail: string
  tone: WorkspaceStatusTone
  workerNames: string[]
  degraded: boolean
  degradedMessage?: string
  observedAt?: string
}

interface RuntimeSnapshot {
  runtime?: ProjectRuntime | null
  runtimeUnavailable?: boolean
}

const states: Record<string, { label: string; tone: WorkspaceStatusTone }> = {
  pending: { label: '待启动', tone: 'info' },
  unknown: { label: '尚未观测', tone: 'neutral' },
  blocked: { label: '配置阻塞', tone: 'warning' },
  waiting: { label: '等待调度条件', tone: 'info' },
  capacity: { label: '等待容量', tone: 'warning' },
  cooldown: { label: '退避中', tone: 'warning' },
  starting: { label: '启动中', tone: 'info' },
  running: { label: '执行已领取', tone: 'info' },
  pausing: { label: '正在暂停', tone: 'warning' },
  paused: { label: '已暂停', tone: 'neutral' },
  stopping: { label: '正在结束', tone: 'warning' },
  stopped: { label: '已结束', tone: 'neutral' },
  completed: { label: '已完成', tone: 'success' },
  failed: { label: '执行失败', tone: 'danger' },
}

export function resolveRuntimeStatus(snapshot: RuntimeSnapshot): WorkspaceStatusView {
  const runtime = snapshot.runtime
  if (
    snapshot.runtimeUnavailable ||
    !isSupportedProjectRuntime(runtime) ||
    !runtime.dispatcher ||
    !Array.isArray(runtime.active_runs)
  )
    return {
      code: 'unknown',
      label: '状态暂未更新',
      detail: snapshot.runtimeUnavailable
        ? '状态查询失败或已过期，请刷新后查看；项目操作仍由服务端确认。'
        : '尚未取得项目运行状态。',
      tone: 'neutral',
      workerNames: [],
      degraded: true,
    }
  // Dispatcher 心跳过期不改变 Server 已确认的项目暂停或结束状态。
  const dispatcherNeeded = !['paused', 'stopped', 'completed'].includes(runtime.state)
  const degradedMessage = dispatcherNeeded
    ? runtime.dispatcher.freshness === 'stale'
      ? 'Dispatcher 连接未及时更新，执行状态待确认。'
      : runtime.dispatcher.freshness === 'unknown'
        ? '尚未取得 Dispatcher 观测，执行状态待确认。'
        : undefined
    : undefined
  let state = states[runtime.state] || states.unknown!
  if (
    runtime.state === 'blocked' &&
    [
      'project_authorization_scope_upgrade_required',
      'project_authorization_scope_changed',
      'project_authorization_expired',
      'project_reauthorization_required',
    ].includes(runtime.code)
  )
    state = { label: '授权阻断', tone: 'warning' }
  if (runtime.state === 'waiting' && /capacity/.test(runtime.code)) state = states.capacity!
  if (runtime.state === 'waiting' && /cooldown|backoff/.test(runtime.code)) state = states.cooldown!
  // running/心跳只证明已领取；只有执行里程碑才能说明进程已启动。
  if (
    runtime.state === 'running' &&
    runtime.active_runs.some((run) => run.execution_stage === 'running')
  ) {
    state = { label: '进程已启动', tone: 'info' }
  }
  return {
    code: runtime.state,
    label: state.label,
    detail: runtime.message,
    tone: state.tone,
    workerNames: [...new Set(runtime.active_runs.map((run) => run.worker_name).filter(Boolean))],
    degraded: Boolean(degradedMessage),
    degradedMessage,
    observedAt: runtime.observed_at,
  }
}

function pendingStatus(snapshot: RuntimeSnapshot, projectId: string): WorkspaceStatusView {
  return {
    code: 'pending',
    label: '待启动',
    tone: 'info',
    workerNames: [],
    degraded: false,
    detail:
      !snapshot.runtimeUnavailable &&
      isSupportedProjectRuntime(snapshot.runtime) &&
      snapshot.runtime.project_id === projectId &&
      snapshot.runtime.project_status === 'pending'
        ? snapshot.runtime.message
        : '尚未进入执行调度，请在启动计划中查看时间、原因或管理预约。',
  }
}
// pending 是 Server 已确认的项目状态，不依赖 Dispatcher 观测推断。
export const resolveProjectDetailStatus = (
  snapshot: RuntimeSnapshot & { detail: ProjectDetail },
) =>
  snapshot.detail.project.status === 'pending'
    ? pendingStatus(snapshot, snapshot.detail.project.id)
    : resolveRuntimeStatus({
        ...snapshot,
        runtime:
          snapshot.runtime?.project_id === snapshot.detail.project.id
            ? snapshot.runtime
            : undefined,
      })

export const resolveProjectSummaryStatus = (
  snapshot: RuntimeSnapshot & { project: ProjectSummary },
) =>
  snapshot.project.status === 'pending'
    ? pendingStatus(snapshot, snapshot.project.id)
    : resolveRuntimeStatus({
        ...snapshot,
        runtime:
          snapshot.runtime?.project_id === snapshot.project.id ? snapshot.runtime : undefined,
      })

export const resolveWorkspaceStatus = resolveProjectDetailStatus
