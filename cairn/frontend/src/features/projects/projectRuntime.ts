import { computed, onScopeDispose, ref, shallowRef, toValue, watch, type MaybeRefOrGetter } from 'vue'
import { useQuery, useQueryClient } from '@tanstack/vue-query'
import { api } from '@/api/client'
import type {
  ProjectDetail,
  ProjectExecutionControl,
  ProjectLifecycleAction,
  ProjectRuntime,
  ReauthorizeProjectRequest,
} from '@/api/types'

export const projectRuntimeKey = (id: string) => ['project-runtime', id] as const
export const projectRuntimeBatchKey = ['project-runtime-batch'] as const

export const isSupportedProjectRuntime = (runtime: unknown): runtime is ProjectRuntime => {
  if (!runtime || typeof runtime !== 'object') return false
  const version = (runtime as { schema_version?: unknown }).schema_version
  return version === 1 || version === 2
}

export const getProjectRuntime = (id: string, signal?: AbortSignal) =>
  api<ProjectRuntime>(
    'GET',
    `/projects/${encodeURIComponent(id)}/runtime?schema_version=2`,
    undefined,
    signal,
  )

export const getProjectRuntimes = async (ids: string[], signal?: AbortSignal) => {
  const uniqueIds = [...new Set(ids)]
  const requests: Array<Promise<{ items: ProjectRuntime[] }>> = []
  // 服务端单批最多 100 个项目；不回退到逐卡片请求或截断列表。
  for (let offset = 0; offset < uniqueIds.length; offset += 100) {
    const params = new URLSearchParams({
      project_ids: uniqueIds.slice(offset, offset + 100).join(','),
      schema_version: '2',
    })
    requests.push(
      api<{ items: ProjectRuntime[] }>('GET', `/projects/runtime?${params}`, undefined, signal),
    )
  }
  return { items: (await Promise.all(requests)).flatMap((batch) => batch.items) }
}

export const runtimeSnapshotExpired = (dataUpdatedAt: number, now = Date.now()) => {
  // 只比较同一浏览器的成功读取时间；Server observed_at 保留为事实时间和同源排序依据。
  return !Number.isFinite(dataUpdatedAt) || dataUpdatedAt <= 0 || now - dataUpdatedAt > 60_000
}

export function useRuntimeClock() {
  const now = ref(Date.now())
  const timer = setInterval(() => {
    now.value = Date.now()
  }, 8000)
  onScopeDispose(() => clearInterval(timer))
  return now
}

type BodylessProjectLifecycleAction = Exclude<ProjectLifecycleAction, 'reauthorize'>

export function performProjectAction(
  id: string,
  action: 'reauthorize',
  body: ReauthorizeProjectRequest,
): Promise<ProjectDetail['project']>
export function performProjectAction(
  id: string,
  action: BodylessProjectLifecycleAction,
): Promise<ProjectDetail['project']>
export function performProjectAction(
  id: string,
  action: ProjectLifecycleAction,
  body?: ReauthorizeProjectRequest,
) {
  const path = `/projects/${encodeURIComponent(id)}/${action}`
  return action === 'reauthorize'
    ? api<ProjectDetail['project']>('POST', path, body)
    : api<ProjectDetail['project']>('POST', path)
}

export function useProjectRuntime(projectId: MaybeRefOrGetter<string | null>) {
  const now = useRuntimeClock()
  const query = useQuery({
    queryKey: computed(() => projectRuntimeKey(toValue(projectId) || '')),
    queryFn: ({ queryKey, signal }) => getProjectRuntime(queryKey[1], signal),
    enabled: computed(() => Boolean(toValue(projectId))),
    // 工作空间与运行页共用快照和请求；后挂载的子页不会立即重复请求。
    staleTime: 4000,
    refetchInterval: 8000,
    retry: false,
  })
  const unavailable = computed(
    () =>
      query.isError.value ||
      Boolean(
        query.data.value &&
        runtimeSnapshotExpired(query.dataUpdatedAt.value, Math.max(now.value, Date.now())),
      ),
  )
  return { ...query, unavailable, now }
}

const actionReasons: Record<string, string> = {
  project_pause_draining: '暂停请求已受理，物理冻结尚未确认。请等待原现场暂停回执，不要重复恢复。',
  project_pause_unconfirmed: '尚未取得原现场的物理暂停回执，请刷新状态或联系管理员核查。',
  project_execution_owner_unknown: '执行归属尚未确认，物理状态仍未知。请联系管理员核查。',
  execution_control_failed: '执行控制失败，物理状态尚未确认；原材料保留，请核查后重试。',
  execution_control_unconfirmed: '尚未取得所属执行的物理回执，请刷新状态或联系管理员核查。',
  execution_scene_lost: '未能核验原运行现场，物理状态未确认；系统不会自动重建或重跑。',
  execution_control_pending: '控制请求已受理，请等待原现场的物理回执，不要重复操作。',
  execution_scene_unavailable: '没有原执行的热暂停回执，不能原地恢复；保留材料不等于可恢复的进程现场。',
  execution_scene_generation_mismatch: '原执行归属已失效，不能热恢复；已保存材料仍保留。',
  docker_daemon_unavailable: '无法连接原 Docker daemon，物理状态尚未确认。',
  docker_daemon_changed: 'Docker daemon 身份已变化，无法核验原现场；不会自动创建替代执行。',
  container_identity_mismatch: '原容器身份不一致，不能确认本次执行控制结果。',
  container_ownership_mismatch: '容器归属不匹配，未操作其他执行域。',
  execution_control_invalid: '执行控制请求或回执无效，请刷新状态或联系管理员核查。',
  execution_control_request_stale: '控制请求已被更新，请刷新查看最新请求。',
  execution_lifecycle_machine_required:
    '此操作需要由执行生命周期服务处理，请刷新状态或联系管理员。',
  project_pause_unsupported: '当前 Dispatcher 不支持安全暂停。请联系管理员升级后重试。',
  project_dispatcher_unavailable: '无法确认 Dispatcher 状态。请联系管理员恢复连接后重试。',
  assessment_new_project_required: '此评估已经结束，不能恢复。请新建项目继续评估。',
  project_not_paused: '项目并未处于可恢复的暂停状态。请刷新项目后重试。',
  project_pending_schedule_required: '项目尚未启动，请通过启动计划管理预约，不能直接恢复执行。',
  project_not_active: '项目当前不是活跃状态。请刷新项目后重试。',
  project_completed: '项目已经完成，不能通过恢复重新执行。',
  project_stopped: '项目已经结束，不能恢复。请新建项目继续评估。',
  project_reauthorization_required: '当前项目需要显式确认授权，请暂停后在本项目重新确认。',
  project_authorization_scope_upgrade_required:
    '项目使用旧版授权范围格式，需要在当前范围上显式确认一次授权。请暂停后重新确认，不是等待 Worker。',
  project_authorization_scope_changed: '项目授权范围已变化，请暂停后核对当前范围并重新确认授权。',
  project_authorization_expired: '项目授权已过期，请暂停后在本项目重新确认授权。',
  project_authorization_current: '当前授权仍然有效，无需重复确认。',
  project_authorization_conflict: '授权状态已被其他操作更新，请刷新后重新确认。',
  authorization_epoch_mismatch: '授权版本已变化，请刷新项目后重新确认。',
  authorization_scope_mismatch: '授权范围已变化，请刷新项目并重新核对后确认。',
  assessment_contract_unsupported: '当前项目合同不支持此授权操作。',
  project_status_invalid: '项目当前状态不允许此操作，请刷新项目。',
  completion_intent_sources_missing: '历史完成记录缺少来源，不能安全重新开启项目。',
  explore_deadline_exceeded: '已到执行停止时间，不能继续研究；重新授权不会延长此时间。',
  assessment_report_finalization_in_progress: '报告正在生成或冻结，不能恢复评估。请等待报告完成。',
  assessment_report_project_paused: '项目仅暂停，尚未结束。请先结束评估，再生成部分报告。',
  project_deadline_exceeded: '已到执行停止时间，不能继续研究；重新授权不会延长此时间。',
  assessment_deadline_exceeded: '已到执行停止时间，不能继续研究；重新授权不会延长此时间。',
  assessment_report_frozen: '报告已经冻结，不能恢复执行或修改本次评估。',
  runtime_unavailable: '状态暂未更新。请刷新项目后重试。',
}

export function actionReasonText(reason: string | null | undefined): string {
  if (!reason) return '当前状态不允许此操作，请刷新项目。'
  return actionReasons[reason] || '服务端当前不允许此操作，请刷新项目或联系管理员。'
}

export function runtimeRequestError(error: unknown): string {
  const candidate = error as {
    status?: number
    data?: { detail?: unknown }
    message?: string
  } | null
  const detail = candidate?.data?.detail
  const rawCode =
    typeof detail === 'string'
      ? detail
      : detail && typeof detail === 'object' && 'code' in detail
        ? String(detail.code)
        : candidate?.message || ''
  // 只显示稳定关联码，不将代理返回的 HTML、原始异常或路径直接透传到界面。
  const code =
    /^(project|assessment|authorization|explore|dispatcher|worker|runtime|model|binding|engine|execution|completion|docker|container)_[a-z0-9_]{1,64}$/.test(
      rawCode,
    )
      ? rawCode
      : ''
  if (code) return `${actionReasonText(code)}（关联码：${code}）`
  if (candidate?.status === 401) return '登录已失效，请重新登录后重试。（HTTP 401）'
  if (candidate?.status === 403) return '没有执行此操作的权限，请联系管理员。（HTTP 403）'
  if (candidate?.status === 404)
    return '项目不存在或服务尚未支持此接口，请刷新或联系管理员。（HTTP 404）'
  if (candidate?.status === 409) return '项目状态已变化，请刷新项目后重试。（HTTP 409）'
  return '请求失败，无法确认最新状态。请检查连接并刷新项目后重试。'
}

export function executionControlConfirmed(
  control: ProjectExecutionControl,
  state: 'paused' | 'running' | 'stopped',
): boolean {
  const expected = { pause: 'paused', resume: 'running', finish: 'stopped' }
  const requested = Date.parse(control.requested_at || '')
  const confirmed = Date.parse(control.confirmed_at || '')
  return Boolean(
    control.action &&
    expected[control.action] === state &&
    control.state === state &&
    Number.isFinite(requested) &&
    Number.isFinite(confirmed) &&
    confirmed >= requested,
  )
}

export function isResumeRetry(project: { status: string; execution_control?: ProjectExecutionControl } | null | undefined): boolean {
  return project?.status === 'active' && project.execution_control?.action === 'resume' &&
    ['failed', 'lost'].includes(project.execution_control.state || '')
}

export function projectResumeLabel(project: { execution_control?: ProjectExecutionControl } | null | undefined): string {
  const control = project?.execution_control
  if (control?.action === 'resume') {
    if (control.state === 'requested') return '等待恢复确认'
    if (control.state === 'failed' || control.state === 'lost') return '重试继续'
  }
  return '继续项目'
}

export function projectActionAllowed(
  runtime: ProjectRuntime | undefined | null,
  action: ProjectLifecycleAction,
  unavailable = false,
  project?: Pick<ProjectDetail['project'], 'id' | 'status' | 'execution_control'> | null,
): boolean {
  // 控制受理期间只等待；恢复资格来自 Server，暂停回执不能替代授权等准入检查。
  const control = project?.execution_control
  if (control?.state === 'requested' && action !== 'finish') return false
  if (action === 'pause') return project?.status === 'active'
  if (action === 'resume') {
    if (project?.status !== 'paused' && !isResumeRetry(project)) return false
    return (
      isSupportedProjectRuntime(runtime) &&
      !unavailable &&
      runtime.project_id === project?.id &&
      runtime.project_status === project?.status &&
      (!control?.request_id || runtime.execution_control?.request_id === control.request_id) &&
      runtime.actions?.resume?.allowed === true
    )
  }
  if (action === 'finish') return project?.status === 'active' || project?.status === 'paused'
  // 重新确认授权与重开仍沿用各自的运行条件，不属于普通暂停／继续／结束。
  if (
    !isSupportedProjectRuntime(runtime) ||
    unavailable ||
    !runtime.actions?.[action]?.allowed ||
    !Array.isArray(runtime.active_runs) ||
    runtime.active_runs.length > 0
  )
    return false
  if (action === 'reauthorize')
    return (
      (project?.status === 'paused' && runtime.state === 'paused') ||
      (project?.status === 'pending' && runtime.project_status === 'pending')
    )
  return project?.status === 'completed' && runtime.state === 'completed'
}

type ControlProject = Pick<ProjectDetail['project'], 'id' | 'status' | 'execution_control'>

// 只合并服务端事实；同一请求的旧 requested 不能覆盖已经取得的物理回执。
function latestControlProject<T extends ControlProject>(current: T, incoming?: ControlProject): T {
  const before = current.execution_control
  const after = incoming?.execution_control
  if (!incoming || !after?.request_id) return current
  if (before?.request_id) {
    if (Date.parse(after.requested_at || '') < Date.parse(before.requested_at || '')) return current
    if (before.request_id === after.request_id && before.state !== 'requested' && after.state === 'requested')
      return current
  }
  return {
    ...current,
    status: before?.request_id === after.request_id && current.status === 'completed'
      ? current.status : incoming.status,
    execution_control: after,
  }
}

type RuntimeObservation = { runtime: ProjectRuntime; dataUpdatedAt: number }

export function useProjectLifecycle<T extends ControlProject>(
  rawRuntimeFor: (id: string) => ProjectRuntime | undefined,
  reading: () => { dataUpdatedAt: number; failed: boolean; now: number },
  onRefresh: (id: string) => Promise<unknown> | unknown,
  rawProjects: () => T[],
) {
  const queryClient = useQueryClient()
  const pendingProjectId = ref<string | null>(null)
  const actionError = ref('')
  // 仅保存本页收到的权威回执；不推导物理成功，也不持久化客户端控制状态。
  const receipts = shallowRef<Record<string, ControlProject>>({})
  const observations = shallowRef<Record<string, RuntimeObservation>>({})
  const expired = ref(new Set<string>())
  const superseded = new Map<string, Set<string>>()
  const watches = new Map<string, {
    requestId: string
    controller: AbortController
    next?: ReturnType<typeof setTimeout>
    deadline: ReturnType<typeof setTimeout>
  }>()
  let generation = 0

  const observationFor = (id: string) => {
    const raw = rawRuntimeFor(id)
    const candidates = [observations.value[id], raw && { runtime: raw, dataUpdatedAt: reading().dataUpdatedAt }].filter(
      (item): item is RuntimeObservation => Boolean(item && item.runtime.project_id === id &&
        !superseded.get(id)?.has(item.runtime.execution_control?.request_id || '')),
    )
    return candidates.sort((a, b) => {
      // Server 时间为秒精度；同一请求不能因晚到的 requested 快照丢失确认及动作资格。
      if (a.runtime.execution_control?.request_id === b.runtime.execution_control?.request_id) {
        const aPending = a.runtime.execution_control?.state === 'requested'
        const bPending = b.runtime.execution_control?.state === 'requested'
        if (aPending !== bPending) return aPending ? 1 : -1
      }
      return Date.parse(b.runtime.observed_at) - Date.parse(a.runtime.observed_at) ||
        b.dataUpdatedAt - a.dataUpdatedAt
    })[0]
  }
  const runtimeFor = (id: string) => observationFor(id)?.runtime
  const runtimeUpdatedAtFor = (id: string) => observationFor(id)?.dataUpdatedAt || 0
  const unavailableFor = (id: string) => reading().failed ||
    runtimeSnapshotExpired(runtimeUpdatedAtFor(id), Math.max(reading().now, Date.now()))
  const projectFor = (id: string): T | undefined => {
    const raw = rawProjects().find((project) => project.id === id)
    if (!raw) return undefined
    let project = latestControlProject(raw, receipts.value[id])
    // 原始 GET 可能先于 POST 发出；已被替换的请求不能再参与投影。
    const runtime = runtimeFor(id)
    if (runtime && !superseded.get(id)?.has(runtime.execution_control?.request_id || '')) {
      project = latestControlProject(project, {
        id, status: runtime.project_status, execution_control: runtime.execution_control,
      })
    }
    return project
  }
  const needsObservation = (project: ControlProject) => {
    const control = project.execution_control
    if (!control?.request_id) return false
    const runtimeControl = runtimeFor(project.id)?.execution_control
    // 元数据确认不等于动作资格已更新；同请求的拒绝资格也是完整结果，不等待 allowed=true。
    return control.state === 'requested' || runtimeControl?.request_id !== control.request_id ||
      !runtimeControl.state || runtimeControl.state === 'requested'
  }
  const observationExpired = (id: string) => {
    const project = projectFor(id)
    const requestId = project?.execution_control?.request_id
    return Boolean(project && requestId && expired.value.has(requestId) && needsObservation(project))
  }
  const stopObservation = (id: string) => {
    const entry = watches.get(id)
    if (!entry) return
    clearTimeout(entry.next)
    clearTimeout(entry.deadline)
    entry.controller.abort()
  }
  const clear = () => {
    generation++
    for (const id of watches.keys()) stopObservation(id)
    watches.clear()
    receipts.value = {}
    observations.value = {}
    superseded.clear()
    expired.value.clear()
    actionError.value = ''
    pendingProjectId.value = null
  }
  onScopeDispose(clear)

  watch(() => rawProjects().map((project) => projectFor(project.id)!), (projects) => {
    const ids = new Set(projects.map((project) => project.id))
    for (const id of watches.keys()) {
      if (!ids.has(id)) {
        stopObservation(id)
        watches.delete(id)
      }
    }
    for (const project of projects) {
      const { id, execution_control: control } = project
      if (!control?.request_id || !needsObservation(project)) {
        const observation = observationFor(id)
        const runtime = observation?.runtime
        // 保存快照及其实际读取时间；旧响应、元数据刷新和重新渲染不能给保留的确认续期。
        if (control?.request_id && observation && runtime?.execution_control?.request_id === control.request_id &&
          runtime.execution_control.state !== 'requested' && observation !== observations.value[id])
          observations.value = { ...observations.value, [id]: observation }
        stopObservation(id)
        continue
      }
      // 到期的同一请求不因普通刷新重新开启加速；新 request_id 才有新窗口。
      if (watches.get(id)?.requestId === control.request_id) continue
      stopObservation(id)
      const requestId = control.request_id
      const controller = new AbortController()
      const entry = {
        requestId, controller,
        next: undefined as ReturnType<typeof setTimeout> | undefined,
        deadline: setTimeout(() => {
          expired.value.add(requestId)
          stopObservation(id)
        }, 15_000),
      }
      watches.set(id, entry)
      const observe = async () => {
        try {
          // 与工作区普通 runtime 查询共用 key，在途读取合并，不读取整份项目图。
          const runtime = await queryClient.fetchQuery({
            queryKey: projectRuntimeKey(id),
            queryFn: ({ signal }) => getProjectRuntime(id, AbortSignal.any([signal, controller.signal])),
            staleTime: 0,
            retry: false,
          })
          // 共享 query 先发布结果时，watch 可能已因确认而停止观察；仍保留这份实际回执。
          // 换请求、换项目或离页则必须丢弃，不让旧结果进入新的作用域。
          if (watches.get(id) !== entry) return
          if (runtime.project_id === id && runtime.execution_control?.request_id === requestId) {
            const previous = observations.value[id]?.runtime.execution_control
            if (previous?.request_id === requestId && previous.state !== 'requested' &&
              runtime.execution_control.state === 'requested') return
            const dataUpdatedAt = queryClient.getQueryState(projectRuntimeKey(id))!.dataUpdatedAt
            observations.value = { ...observations.value, [id]: { runtime, dataUpdatedAt } }
          }
        } catch {
          // 读取失败不改写控制结果；窗口内继续只读观察，到期保留尚未确认。
        }
        // 被取消的旧读取不能停止同项目后来建立的新请求观察器。
        if (controller.signal.aborted || watches.get(id) !== entry) return
        const project = projectFor(id)
        if (project && needsObservation(project)) entry.next = setTimeout(observe, 750)
        else stopObservation(id)
      }
      void observe()
    }
  }, { immediate: true })

  async function perform(id: string, action: 'reauthorize', body: ReauthorizeProjectRequest): Promise<boolean>
  async function perform(id: string, action: BodylessProjectLifecycleAction): Promise<boolean>
  async function perform(id: string, action: ProjectLifecycleAction, body?: ReauthorizeProjectRequest): Promise<boolean> {
    if (pendingProjectId.value || !projectActionAllowed(runtimeFor(id), action, unavailableFor(id), projectFor(id)))
      return false
    const requestGeneration = ++generation
    pendingProjectId.value = id
    actionError.value = ''
    let succeeded = false
    try {
      if (action === 'reauthorize' && !body) return false
      const receipt = action === 'reauthorize'
        ? await performProjectAction(id, action, body!)
        : await performProjectAction(id, action)
      if (requestGeneration !== generation) return false
      const requestId = receipt.execution_control?.request_id
      if (requestId) {
        const retired = superseded.get(id) || new Set<string>()
        for (const old of [projectFor(id)?.execution_control, rawRuntimeFor(id)?.execution_control]) {
          if (old?.request_id && old.request_id !== requestId) retired.add(old.request_id)
        }
        superseded.set(id, retired)
        stopObservation(id)
        // POST 之前的 GET 不可成为新请求的首个观测；受理回执先同步呈现。
        void queryClient.cancelQueries({ queryKey: projectRuntimeKey(id) })
        receipts.value = { ...receipts.value, [id]: receipt }
      }
      succeeded = true
    } catch (error) {
      if (requestGeneration === generation) actionError.value = runtimeRequestError(error)
    } finally {
      if (requestGeneration === generation) {
        pendingProjectId.value = null
        // 控制受理不等待全量项目刷新；非控制动作或拒绝仍刷新原业务数据。
        if (!succeeded || action === 'reauthorize') {
          void Promise.resolve(onRefresh(id)).catch(() => {})
          void queryClient.invalidateQueries({ queryKey: projectRuntimeKey(id) })
          void queryClient.invalidateQueries({ queryKey: projectRuntimeBatchKey })
        } else if (projectFor(id)?.execution_control?.state !== 'requested') {
          void queryClient.invalidateQueries({ queryKey: projectRuntimeKey(id) })
          void queryClient.invalidateQueries({ queryKey: projectRuntimeBatchKey })
        }
      }
    }
    return succeeded && requestGeneration === generation
  }
  return { pendingProjectId, actionError, perform, clear, projectFor, runtimeFor, runtimeUpdatedAtFor, unavailableFor, observationExpired }
}
