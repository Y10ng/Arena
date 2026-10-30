import type { TrafficScopeStatus } from '@/api/traffic'

export function trafficScopeLabel(scope: Pick<TrafficScopeStatus, 'status'>): string {
  const status = scope.status
  if (!status) return '采集状态未上报'
  const stopped = status.control_stopped === true && status.test_stopped === true && status.collector_stopped === true
  if (status.code === 'status_persist_failed' && status.upload_complete !== true) {
    return stopped ? '同步失败，上传已停止' : '同步失败，上传已停止；等待终止确认'
  }
  if (stopped) {
    if (status.upload_complete === true) return '同步完成'
    if (status.state === 'blocked' || status.state === 'retrying') return status.state === 'retrying' ? '同步失败，正在重试' : '同步失败'
    return '采集已停止，正在同步已有流量'
  }
  if (['stopping', 'stopped', 'blocked'].includes(status.state)) return '等待终止确认'
  if (status.collector_stopped === true) return '采集已停止，等待终止确认'
  return ({ starting: '采集启动中', ready: '采集已就绪', running: '正在采集', draining: '正在同步已有流量', retrying: '同步失败，正在重试', watching: '采集状态待确认' } as Record<string, string>)[status.state] || '采集状态待确认'
}
