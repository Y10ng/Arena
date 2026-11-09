import type { UpdatePhase } from '@/api/systemUpdate'

export const phaseLabel = (phase: UpdatePhase): string => {
  const labels: Record<UpdatePhase, string> = {
    idle: '空闲',
    checking: '检查中',
    available: '有新版本',
    up_to_date: '已是最新',
    preparing: '准备中',
    ready: '就绪',
    applying: '更新中',
    done: '完成',
    failed: '失败',
    interrupted: '已中断',
  }
  return labels[phase] || phase
}

export const pollInterval = (phase: UpdatePhase): number | null => {
  if (phase === 'preparing' || phase === 'applying') return 2000
  return null
}

export const isTerminal = (phase: UpdatePhase): boolean =>
  ['done', 'failed', 'interrupted', 'up_to_date'].includes(phase)

export const canCheck = (phase: UpdatePhase): boolean =>
  ['idle', 'up_to_date', 'done', 'failed', 'interrupted', 'available'].includes(phase)

export const canPrepare = (phase: UpdatePhase): boolean => ['available'].includes(phase)

export const canCancel = (phase: UpdatePhase): boolean => ['preparing'].includes(phase)

export const canApply = (phase: UpdatePhase): boolean => ['ready'].includes(phase)

export const showProgress = (phase: UpdatePhase): boolean =>
  ['checking', 'preparing', 'applying'].includes(phase)

export const versionMatrix = (
  current: string,
  channel: string | null | undefined,
): { current: string; latest: string; updateAvailable: boolean } => ({
  current,
  latest: channel || '—',
  updateAvailable: Boolean(channel && channel !== current),
})
