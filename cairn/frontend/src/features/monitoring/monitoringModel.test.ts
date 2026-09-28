import { describe, expect, it } from 'vitest'
import type { MonitoringTaskRun } from '@/api/types'
import {
  activeMonitoringTasks,
  dispatcherEffectiveStatus,
  formatMonitoringBytes,
  monitoringTaskKey,
} from './monitoringModel'

const taskRun = (overrides: Partial<MonitoringTaskRun>): MonitoringTaskRun => ({
  id: 'tr001',
  project_id: 'p1',
  project_title: 'Project',
  project_status: 'active',
  task_type: 'explore',
  worker_name: 'worker-a',
  worker_type: 'mock',
  status: 'running',
  artifact_count: 0,
  heartbeat_source: 'none',
  heartbeat_status: 'not_tracked',
  ...overrides,
})

describe('monitoringModel', () => {
  it('uses project id and run id as the stable task key', () => {
    expect(monitoringTaskKey(taskRun({ project_id: 'p1', id: 'tr001' }))).toBe('p1/tr001')
  })

  it('keeps active tasks with colliding run ids across projects', () => {
    const tasks = activeMonitoringTasks({
      running: [
        taskRun({ project_id: 'p1', id: 'tr001', status: 'running' }),
        taskRun({ project_id: 'p2', id: 'tr001', status: 'running' }),
      ],
      longRunning: [taskRun({ project_id: 'p1', id: 'tr001', status: 'running' })],
      queued: [taskRun({ project_id: 'p3', id: 'tr001', status: 'queued' })],
    })

    expect(tasks.map(monitoringTaskKey)).toEqual(['p1/tr001', 'p2/tr001', 'p3/tr001'])
    expect(tasks.find((task) => monitoringTaskKey(task) === 'p1/tr001')?.is_long_running).toBe(true)
    expect(tasks.find((task) => monitoringTaskKey(task) === 'p2/tr001')?.is_long_running).toBe(
      false,
    )
  })

  it('does not let queued duplicates overwrite running tasks', () => {
    const tasks = activeMonitoringTasks({
      running: [taskRun({ project_id: 'p1', id: 'tr001', status: 'running' })],
      longRunning: [],
      queued: [taskRun({ project_id: 'p1', id: 'tr001', status: 'queued' })],
    })

    expect(tasks).toHaveLength(1)
    expect(tasks[0].status).toBe('running')
  })

  it('formats byte counts beyond GB', () => {
    expect(formatMonitoringBytes(1024 ** 4)).toBe('1 TB')
  })

  it('prefers dispatcher effective status when present', () => {
    expect(
      dispatcherEffectiveStatus({ last_status: 'running', effective_status: 'stale' }),
    ).toBe('stale')
  })
})
