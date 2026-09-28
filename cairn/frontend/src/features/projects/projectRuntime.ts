import { computed, onScopeDispose, ref, toValue, type MaybeRefOrGetter } from 'vue'
import { useQuery, useQueryClient } from '@tanstack/vue-query'
import { api } from '@/api/client'
import type {
  ProjectDetail,
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

export const runtimeSnapshotExpired = (runtime: ProjectRuntime, now = Date.now()) => {
  const observed = Date.parse(runtime.observed_at)
  // 只容忍少量时钟偏差，明显来自未来的观测不能作为当前状态显示。
  return !Number.isFinite(observed) || now - observed > 60_000 || observed - now > 5000
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
        runtimeSnapshotExpired(query.data.value, Math.max(now.value, Date.now())),
      ),
  )
  return { ...query, unavailable }
}

const actionReasons: Record<string, string> = {
  project_pause_draining: '旧执行仍在取消或清理中。请等待停止确认后再恢复，不要重复启动。',
  project_pause_unconfirmed:
    '尚未取得旧执行清理完成的可信证明，暂不能修改资料或生成报告。请联系管理员核查。',
  execution_lifecycle_machine_required:
    '此操作需要由执行生命周期服务处理，请刷新状态或联系管理员。',
  project_pause_unsupported: '当前 Dispatcher 不支持安全暂停。请联系管理员升级后重试。',
  project_dispatcher_unavailable: '无法确认 Dispatcher 状态。请联系管理员恢复连接后重试。',
  assessment_new_project_required: '此评估已经结束，不能恢复。请新建项目继续评估。',
  project_not_paused: '项目并未处于可恢复的暂停状态。请刷新项目后重试。',
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
  explore_deadline_exceeded: '授权时间窗已到期，请先在本项目重新确认授权。',
  assessment_report_finalization_in_progress: '报告正在生成或冻结，不能恢复评估。请等待报告完成。',
  assessment_report_project_paused: '项目仅暂停，尚未结束。请先结束评估，再生成部分报告。',
  project_deadline_exceeded: '授权时间窗已到期，请先在本项目重新确认授权。',
  assessment_deadline_exceeded: '授权时间窗已到期，请先在本项目重新确认授权。',
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
    /^(project|assessment|authorization|explore|dispatcher|worker|runtime|model|binding|engine|execution|completion)_[a-z0-9_]{1,64}$/.test(
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

export function projectActionAllowed(
  runtime: ProjectRuntime | undefined | null,
  action: ProjectLifecycleAction,
  unavailable = false,
  project?: Pick<ProjectDetail['project'], 'id' | 'status'> | null,
): boolean {
  // 普通项目动作只按项目元数据提供入口，最终资格由 Server 在项目锁内判定。
  if (action === 'pause') return project?.status === 'active'
  if (action === 'resume') return project?.status === 'paused'
  if (action === 'finish') return project?.status === 'active' || project?.status === 'paused'
  // 重新确认授权与重开仍沿用各自的运行条件，不属于普通暂停／继续／结束。
  if (
    !isSupportedProjectRuntime(runtime) ||
    unavailable ||
    runtimeSnapshotExpired(runtime) ||
    !runtime.actions?.[action]?.allowed ||
    !Array.isArray(runtime.active_runs) ||
    runtime.active_runs.length > 0
  )
    return false
  if (action === 'reauthorize') return project?.status === 'paused' && runtime.state === 'paused'
  return project?.status === 'completed' && runtime.state === 'completed'
}

export function useProjectLifecycle(
  runtimeFor: (id: string) => ProjectRuntime | undefined,
  unavailable: () => boolean,
  onSuccess: (id: string) => Promise<unknown> | unknown,
  projectFor: (id: string) => Pick<ProjectDetail['project'], 'id' | 'status'> | undefined,
) {
  const queryClient = useQueryClient()
  const pendingProjectId = ref<string | null>(null)
  const actionError = ref('')
  let generation = 0
  const clear = () => {
    generation++
    actionError.value = ''
    pendingProjectId.value = null
  }
  onScopeDispose(clear)
  async function perform(
    id: string,
    action: 'reauthorize',
    body: ReauthorizeProjectRequest,
  ): Promise<boolean>
  async function perform(id: string, action: BodylessProjectLifecycleAction): Promise<boolean>
  async function perform(
    id: string,
    action: ProjectLifecycleAction,
    body?: ReauthorizeProjectRequest,
  ): Promise<boolean> {
    if (
      pendingProjectId.value ||
      !projectActionAllowed(runtimeFor(id), action, unavailable(), projectFor(id))
    )
      return false
    const requestGeneration = ++generation
    pendingProjectId.value = id
    actionError.value = ''
    let succeeded = false
    try {
      if (action === 'reauthorize') {
        if (!body) return false
        await performProjectAction(id, action, body)
      } else {
        await performProjectAction(id, action)
      }
      if (requestGeneration !== generation) return false
      succeeded = true
    } catch (error) {
      if (requestGeneration === generation) actionError.value = runtimeRequestError(error)
    } finally {
      // 成功与冲突都重新读取项目元数据和权威运行快照，不自动重试新授权。
      if (requestGeneration === generation) {
        try {
          await onSuccess(id)
        } catch {
          // 保留原始动作结果；轮询仍会继续刷新元数据。
        }
      }
      void queryClient.invalidateQueries({ queryKey: ['projects'] })
      void queryClient.invalidateQueries({ queryKey: projectRuntimeKey(id) })
      void queryClient.invalidateQueries({ queryKey: projectRuntimeBatchKey })
      if (requestGeneration === generation) pendingProjectId.value = null
    }
    return succeeded && requestGeneration === generation
  }
  return { pendingProjectId, actionError, perform, clear }
}
