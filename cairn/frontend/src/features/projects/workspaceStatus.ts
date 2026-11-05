import type { ProjectExecutionControl, ProjectRuntime } from '@/api/types'
import {
  actionReasonText,
  executionControlConfirmed,
  isSupportedProjectRuntime,
  runtimeSnapshotExpired,
} from './projectRuntime'

export type ProjectStatusTone = 'neutral' | 'info' | 'success' | 'warning' | 'danger'
type ProjectControlSubject = {
  id: string
  status: string
  execution_control?: ProjectExecutionControl
}

export interface ProjectStatusView {
  label: string
  tone: ProjectStatusTone
}

const projectStatuses: Record<string, ProjectStatusView> = {
  pending: { label: '待启动', tone: 'info' },
  active: { label: '进行中', tone: 'info' },
  paused: { label: '已暂停', tone: 'neutral' },
  stopped: { label: '已结束', tone: 'neutral' },
  completed: { label: '已完成', tone: 'success' },
}

// 项目状态只表达元数据事实；运行健康和动作资格由各自消费者处理。
export const resolveProjectStatus = (status: string): ProjectStatusView =>
  projectStatuses[status] || { label: '未知状态', tone: 'neutral' }

export function resolveExecutionControl(project: ProjectControlSubject) {
  const control = project.execution_control
  if (!control || (!control.action && !control.state)) return undefined
  const actions = {
    pause: { label: '暂停', state: 'paused' as const, confirmed: '物理暂停已确认' },
    resume: { label: '恢复', state: 'running' as const, confirmed: '原地恢复已确认' },
    finish: { label: '结束', state: 'stopped' as const, confirmed: '所属执行终止已确认' },
  }
  const action = control.action && actions[control.action]
  const resources = Array.isArray(control.resources) ? control.resources : []
  const base = {
    control,
    resources,
    actionLabel: action?.label || '执行控制',
    confirmed: Boolean(action && executionControlConfirmed(control, action.state)),
  }
  if (!action)
    return {
      ...base,
      label: '执行状态待确认',
      notice: '执行控制回执无法识别',
      stateText: '未确认',
      tone: 'warning' as const,
    }
  if (control.state === 'lost')
    return {
      ...base,
      label: `${action.label}失败`,
      notice: actionReasonText('execution_scene_lost'),
      stateText: '原现场未核验，物理状态未确认',
      tone: 'danger' as const,
    }
  if (control.state === 'failed')
    return {
      ...base,
      label: `${action.label}失败`,
      notice: actionReasonText(control.error || 'execution_control_failed'),
      stateText: '控制失败，物理状态未确认',
      tone: 'danger' as const,
    }
  if (control.state === 'requested')
    return {
      ...base,
      label: `正在${action.label}`,
      notice: control.error
        ? actionReasonText(control.error)
        : `物理${action.label === '结束' ? '终止' : action.label}待确认`,
      stateText: '请求已受理，物理状态待确认',
      tone: 'warning' as const,
    }
  if (!base.confirmed)
    return {
      ...base,
      label: `${action.label}待确认`,
      notice: '尚无有效的物理控制回执',
      stateText: '物理状态未确认',
      tone: 'warning' as const,
    }
  return {
    ...base,
    ...resolveProjectStatus(project.status),
    notice: '',
    // 没有执行资源的控制只改变派发，不声称冻结或恢复了某个进程。
    stateText: resources.length ? action.confirmed : `${action.label}已确认，当前无所属执行现场`,
  }
}

export function resolveRuntimeObservation(
  project: ProjectControlSubject,
  runtime: ProjectRuntime | undefined,
  reading: { failed: boolean; fetching: boolean; dataUpdatedAt: number },
  now: number,
) {
  // 不展示其他项目或无法解释的协议；同项目旧观测只作历史阅读，不参与动作判断。
  const snapshot =
    isSupportedProjectRuntime(runtime) && runtime.project_id === project.id ? runtime : undefined
  const control = resolveExecutionControl(project)
  const observation = (notice: string, historical: boolean, tone: 'warning' | 'danger') => ({
    snapshot,
    control,
    historical,
    notice: [control?.notice, notice].filter(Boolean).join('；'),
    tone: control?.tone === 'danger' ? ('danger' as const) : tone,
  })
  let notice = ''
  let historical = false
  if (reading.failed) {
    notice = snapshot ? '观测未更新' : '观测读取失败'
    historical = true
  } else if (runtime && !isSupportedProjectRuntime(runtime)) {
    notice = '观测版本不支持'
  } else if (runtime && !snapshot) {
    notice = '观测身份不一致'
  } else if (!snapshot) {
    notice = reading.fetching ? '观测加载中' : '暂无运行观测'
  } else if (control?.confirmed && (
    snapshot.execution_control?.request_id !== control.control.request_id ||
    snapshot.execution_control?.state === 'requested'
  )) {
    notice = '动作资格观测未更新'
    historical = true
  } else if (snapshot.project_status !== project.status) {
    notice = '观测与项目状态不一致'
    historical = true
  } else if (runtimeSnapshotExpired(reading.dataUpdatedAt, now)) {
    notice = '观测已失效'
    historical = true
  }

  if (notice) return observation(notice, historical, 'warning')
  if (
    snapshot!.state === 'pausing' ||
    ['project_pause_draining', 'project_finish_draining', 'project_pause_unconfirmed'].includes(
      snapshot!.code,
    )
  ) {
    // 热控制回执优先于旧运行投影中的 stop/drain 推导。
    if (!control)
      notice = snapshot!.code === 'project_finish_draining' ? '物理终止待确认' : '物理暂停待确认'
  } else if (snapshot!.code === 'cleanup_pending') notice = '资源清理待确认'
  else if (snapshot!.state === 'blocked') notice = '运行受阻'
  else if (snapshot!.state === 'failed') notice = '运行失败'
  else if (snapshot!.state === 'unknown' || snapshot!.dispatcher.freshness !== 'fresh')
    notice = '运行观测未知'
  return observation(notice, false, snapshot!.state === 'failed' ? 'danger' : 'warning')
}
