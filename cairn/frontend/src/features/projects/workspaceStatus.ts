export type ProjectStatusTone = 'neutral' | 'info' | 'success'

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
