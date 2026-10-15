import type { ProjectSchedule, ScheduleRules } from '@/api/projectSchedules'

export interface ScheduleDraft {
  mode: 'immediate' | ScheduleRules['mode']
  timezone: string
  startLocal: string
  startAt: string
  stopMode: 'unlimited' | 'specified'
  stopLocal: string
  stopAt: string
  localTime: string
  weekdays: number[]
  endDate: string
  maxRuns: string
}
export function scheduleModeOptions(mode: ScheduleDraft['mode'], locked = false) {
  const recurring = [
    { value: 'daily', label: '每日重复' },
    { value: 'weekly', label: '每周重复' },
  ]
  const once = { value: 'once', label: '单次定时' }
  if (locked) return mode === 'once' ? [once] : recurring
  return [{ value: 'immediate', label: '立即启动' }, once, ...recurring]
}

export const newScheduleDraft = (): ScheduleDraft => ({
  mode: 'immediate',
  timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
  startLocal: '',
  startAt: '',
  stopMode: 'unlimited',
  stopLocal: '',
  stopAt: '',
  localTime: '09:00',
  weekdays: [0],
  endDate: '',
  maxRuns: '',
})
export function localDateTime(at: string, timezone: string): string {
  const parts = new Intl.DateTimeFormat('sv-SE', {
    timeZone: timezone,
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    hourCycle: 'h23',
  }).formatToParts(new Date(at))
  const get = (name: string) => parts.find((part) => part.type === name)?.value
  return `${get('year')}-${get('month')}-${get('day')}T${get('hour')}:${get('minute')}`
}
export function draftFromSchedule(schedule: ProjectSchedule): ScheduleDraft {
  const deadline = schedule.mode === 'once' ? schedule.explore_deadline : null
  return {
    mode: schedule.mode,
    timezone: schedule.timezone,
    startLocal: localDateTime(schedule.start_at, schedule.timezone),
    startAt: schedule.start_at,
    stopMode: deadline ? 'specified' : 'unlimited',
    stopLocal: deadline ? localDateTime(deadline, schedule.timezone) : '',
    stopAt: deadline || '',
    localTime: schedule.local_time || '09:00',
    weekdays: [...schedule.weekdays],
    endDate: schedule.end_date || '',
    maxRuns: schedule.max_runs?.toString() || '',
  }
}

// 这里只将用户选定的本地边界转换为绝对时刻；未来触发及 DST 策略由 Server 预览负责。
export function localTimeCandidates(
  local: string,
  timezone: string,
): Array<{ value: string; label: string }> {
  if (!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$/.test(local)) return []
  const wall = Date.parse(`${local}:00Z`)
  if (!Number.isFinite(wall) || new Date(wall).toISOString().slice(0, 16) !== local) return []
  const offsets = new Set<number>()
  for (let hours = -48; hours <= 48; hours += 6) {
    const timestamp = wall + hours * 3600000
    const displayed = localDateTime(new Date(timestamp).toISOString(), timezone)
    offsets.add(Date.parse(`${displayed}:00Z`) - timestamp)
  }
  return [...offsets]
    .sort((a, b) => b - a)
    .flatMap((offset) => {
      const timestamp = wall - offset
      if (localDateTime(new Date(timestamp).toISOString(), timezone) !== local) return []
      const minutes = offset / 60000
      const sign = minutes < 0 ? '-' : '+'
      const absolute = Math.abs(minutes)
      const suffix = `${sign}${Math.floor(absolute / 60)
        .toString()
        .padStart(2, '0')}:${(absolute % 60).toString().padStart(2, '0')}`
      return [
        {
          value: `${local}:00${suffix}`,
          label: `UTC${suffix}${offsets.size > 1 ? '（请选择明确偏移）' : ''}`,
        },
      ]
    })
}
export class ScheduleTimeError extends Error {
  readonly field: 'start' | 'stop' | 'rules'
  constructor(message: string, field: 'start' | 'stop' | 'rules') {
    super(message)
    this.field = field
  }
}
export function selectedTimeCandidate(
  candidates: ReturnType<typeof localTimeCandidates>,
  at: string,
) {
  // 分钟控件未编辑时保留服务端原时刻的秒、精度和偏移，折叠时仍须明确选择。
  return candidates.find((candidate) => {
    const delta = Date.parse(at) - Date.parse(candidate.value)
    return delta >= 0 && delta < 60000
  }) || (candidates.length === 1 ? candidates[0] : undefined)
}
function absoluteTime(local: string, at: string, timezone: string, field: 'start' | 'stop') {
  const label = field === 'start' ? '启动时间' : '停止时间'
  let candidates: ReturnType<typeof localTimeCandidates>
  try {
    candidates = localTimeCandidates(local, timezone)
  } catch (error) {
    if (error instanceof RangeError)
      throw new ScheduleTimeError('时区无效，请填写 IANA 时区，例如 Asia/Shanghai。', field)
    throw error
  }
  if (!candidates.length)
    throw new ScheduleTimeError(`${label}无效，或该本地时刻因夏令时不存在，请重新选择。`, field)
  const selected = selectedTimeCandidate(candidates, at)
  if (!selected)
    throw new ScheduleTimeError(`${label}出现两次，请选择明确的 UTC 偏移。`, field)
  const delta = Date.parse(at) - Date.parse(selected.value)
  return delta >= 0 && delta < 60000 ? at : selected.value
}
export function scheduleRulesFromDraft(
  draft: ScheduleDraft,
  allowPastStart = false,
): ScheduleRules {
  if (draft.mode === 'immediate') throw new Error('立即启动无需计划时间。')
  if (!draft.timezone) throw new ScheduleTimeError('请填写 IANA 时区。', 'start')
  const startAt = absoluteTime(draft.startLocal, draft.startAt, draft.timezone, 'start')
  if (!allowPastStart && Date.parse(startAt) <= Date.now())
    throw new ScheduleTimeError('起始边界必须晚于当前时间。', 'start')
  if (draft.mode !== 'once' && !/^([01]\d|2[0-3]):[0-5]\d$/.test(draft.localTime))
    throw new ScheduleTimeError('请选择有效的每日执行时刻。', 'rules')
  if (draft.mode === 'weekly' && !draft.weekdays.length)
    throw new ScheduleTimeError('每周运行至少选择一天。', 'rules')
  const maxRuns = draft.mode !== 'once' && draft.maxRuns ? Number(draft.maxRuns) : null
  if (maxRuns !== null && (!Number.isInteger(maxRuns) || maxRuns < 1))
    throw new ScheduleTimeError('成功生成次数必须是大于零的整数。', 'rules')
  if (draft.mode !== 'once' && draft.endDate && draft.endDate < draft.startLocal.slice(0, 10))
    throw new ScheduleTimeError('结束日期不能早于起始日期。', 'rules')
  return {
    mode: draft.mode,
    timezone: draft.timezone,
    start_at: startAt,
    local_time: draft.mode === 'once' ? null : draft.localTime,
    weekdays: draft.mode === 'weekly' ? [...draft.weekdays].sort((a, b) => a - b) : [],
    end_date: draft.mode === 'once' ? null : draft.endDate || null,
    max_runs: maxRuns,
  }
}
export function scheduleDeadlineFromDraft(draft: ScheduleDraft, startAt: string): string | null {
  if (draft.mode !== 'once' || draft.stopMode === 'unlimited') return null
  if (!draft.stopLocal) throw new ScheduleTimeError('请填写指定停止时间。', 'stop')
  const deadline = absoluteTime(draft.stopLocal, draft.stopAt, draft.timezone, 'stop')
  if (Date.parse(deadline) <= Date.parse(startAt))
    throw new ScheduleTimeError('停止时间必须严格晚于启动时间。', 'stop')
  return deadline
}
export function scheduleTimesFromDraft(draft: ScheduleDraft, allowPastStart = false) {
  const rules = scheduleRulesFromDraft(draft, allowPastStart)
  return { rules, deadline: scheduleDeadlineFromDraft(draft, rules.start_at) }
}
