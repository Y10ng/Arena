import type { MonitoringDispatcherSummary, MonitoringTaskRun } from '@/api/types'

export interface ActiveMonitoringTaskRun extends MonitoringTaskRun {
  is_long_running: boolean
}

export const monitoringTaskKey = (task: Pick<MonitoringTaskRun, 'project_id' | 'id'>): string => {
  return `${task.project_id}/${task.id}`
}

export const activeMonitoringTasks = ({
  running,
  longRunning,
  queued,
}: {
  running: MonitoringTaskRun[]
  longRunning: MonitoringTaskRun[]
  queued: MonitoringTaskRun[]
}): ActiveMonitoringTaskRun[] => {
  const longRunningKeys = new Set(longRunning.map(monitoringTaskKey))
  const activeTasks = new Map<string, ActiveMonitoringTaskRun>()

  running.forEach((task) => {
    activeTasks.set(monitoringTaskKey(task), {
      ...task,
      is_long_running: longRunningKeys.has(monitoringTaskKey(task)),
    })
  })

  queued.forEach((task) => {
    const key = monitoringTaskKey(task)
    if (!activeTasks.has(key)) {
      activeTasks.set(key, { ...task, is_long_running: false })
    }
  })

  return [...activeTasks.values()]
}

export const formatMonitoringBytes = (bytes: number): string => {
  if (!bytes) return '0 字节'
  const k = 1024
  const sizes = ['字节', 'KB', 'MB', 'GB', 'TB', 'PB']
  const index = Math.min(Math.floor(Math.log(bytes) / Math.log(k)), sizes.length - 1)
  return `${parseFloat((bytes / Math.pow(k, index)).toFixed(2))} ${sizes[index]}`
}

export const dispatcherEffectiveStatus = (
  dispatcher: Pick<MonitoringDispatcherSummary, 'effective_status' | 'last_status'> | null,
): string => {
  return dispatcher?.effective_status || dispatcher?.last_status || 'unknown'
}
