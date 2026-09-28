import { computed, ref, watch } from 'vue'
import { useQuery, useQueryClient } from '@tanstack/vue-query'
import {
  api,
  isRevisionConflict,
  readDispatcherConfig,
  readDispatcherYaml,
  updateDispatcherConfig,
  updateDispatcherYaml,
} from '@/api/client'
import type { DispatcherConfigDocument, DispatcherYamlDocument, MonitoringSummary } from '@/api/types'
import { useAuthStore } from '@/stores/auth'
import { useUIStore } from '@/stores/ui'

// Dispatcher 运行配置（dispatch.yaml）的编辑逻辑。
// 当前仅由 Dispatcher 运行态页面使用；Tenant Worker 配置通过独立 API 管理，
// 不再与全局 dispatch.yaml Worker 清单形成并列的用户入口。

type ConfigRecord = Record<string, any>

const runtimeNumberFields = [
  'interval',
  'max_workers',
  'max_running_projects',
  'max_project_workers',
  'healthcheck_timeout',
]
const taskNames = ['bootstrap', 'reason', 'explore']

const emptyDispatcher: MonitoringSummary['dispatcher'] = {
  project_count: 0,
  active_project_count: 0,
  running_project_count: 0,
  running_task_count: 0,
  queued_task_count: 0,
  heartbeat_stale: false,
  worker_cooldowns: [],
  worker_rejections: [],
  worker_pool: [],
  container_images: [],
  cap_baseline: [],
}

// 新草稿固定为 pi；读取历史配置时不改写旧引擎。
export interface WorkerDraft {
  name: string
  type: string
  task_types: string[]
  max_running: number
  priority: number
  enabled: boolean
  env: Record<string, unknown>
}

export function emptyWorkerDraft(): WorkerDraft {
  return {
    name: '',
    type: 'pi',
    task_types: ['reason'],
    max_running: 1,
    priority: 10,
    enabled: true,
    env: {},
  }
}

export function useDispatcherFormConfig() {
  const authStore = useAuthStore()
  const uiStore = useUIStore()
  const queryClient = useQueryClient()

  const yamlContent = ref('')
  const yamlOriginal = ref('')
  const formConfig = ref<ConfigRecord | null>(null)
  const formOriginal = ref('')
  const isSavingYaml = ref(false)
  const isSavingForm = ref(false)
  const isRefreshing = ref(false)

  const {
    data: summaryData,
    isLoading: isLoadingSummary,
    isError: isSummaryError,
    error: summaryError,
    refetch: refetchSummary,
  } = useQuery<MonitoringSummary>({
    queryKey: ['consoleDispatcherSummary'],
    queryFn: () => api<MonitoringSummary>('GET', '/admin/monitoring?profile=dispatcher'),
    enabled: computed(() => authStore.isSuperAdmin),
    refetchInterval: 5000,
  })

  const dispatcher = computed<MonitoringSummary['dispatcher']>(() => summaryData.value?.dispatcher || emptyDispatcher)
  const dispatcherYamlPath = computed(() => dispatcher.value.server_reachable_path || dispatcher.value.config_path || '')
  const yamlWritable = computed(() => Boolean(dispatcherYamlPath.value))

  const {
    data: yamlData,
    isError: isYamlError,
    error: yamlError,
    refetch: refetchYaml,
  } = useQuery<DispatcherYamlDocument>({
    queryKey: ['dispatcherYaml'],
    queryFn: readDispatcherYaml,
    enabled: yamlWritable,
  })

  const {
    data: configData,
    isLoading: isLoadingConfig,
    isError: isConfigError,
    error: configError,
    refetch: refetchConfig,
  } = useQuery<DispatcherConfigDocument>({
    queryKey: ['dispatcherConfig'],
    queryFn: readDispatcherConfig,
    enabled: yamlWritable,
  })

  const snapshotConfig = computed(() => configFromSnapshot(dispatcher.value))
  const snapshotYaml = computed(() => dumpYaml(snapshotConfig.value))
  const yamlReadOnly = computed(() => !yamlWritable.value || isYamlError.value)
  const formReadOnly = computed(() => !yamlWritable.value || isConfigError.value)
  const yamlDirty = computed(() => !yamlReadOnly.value && yamlContent.value !== yamlOriginal.value)
  const formDirty = computed(() => Boolean(formConfig.value && formSnapshot(formConfig.value) !== formOriginal.value))
  // 已保存配置里的 worker 名，传给模板面板：保存后立即可选，不等 dispatcher 心跳上报。
  const configWorkerNames = computed(() =>
    (formConfig.value?.workers || []).map((w: { name?: string }) => w.name).filter(Boolean),
  )

  // 原始 YAML 编辑器内容：可写时绑定本地草稿，只读时展示快照。
  const editorYamlContent = computed({
    get: () => (yamlReadOnly.value ? snapshotYaml.value : yamlContent.value),
    set: value => {
      if (!yamlReadOnly.value) yamlContent.value = value
    },
  })

  const refreshAll = async () => {
    isRefreshing.value = true
    try {
      await refetchSummary()
      if (yamlWritable.value) {
        await Promise.all([refetchYaml(), refetchConfig()])
      }
      uiStore.showToast('Dispatcher 数据已刷新', 'success')
    } catch (err) {
      uiStore.showToast(`刷新失败: ${formatError(err)}`, 'danger')
    } finally {
      isRefreshing.value = false
    }
  }

  const saveYaml = async () => {
    if (!yamlDirty.value || yamlReadOnly.value) return
    try {
      isSavingYaml.value = true
      const saved = await updateDispatcherYaml(yamlContent.value, yamlData.value?.revision)
      yamlContent.value = saved.content
      yamlOriginal.value = saved.content
      await Promise.all([refetchSummary(), refetchConfig(), refetchYaml()])
      queryClient.invalidateQueries({ queryKey: ['infra-workers'] })
      uiStore.showToast('dispatch.yaml 已保存，dispatcher 将在下一轮热加载', 'success')
    } catch (err) {
      if (isRevisionConflict(err)) {
        await Promise.all([refetchYaml(), refetchConfig()])
        uiStore.showToast('配置已被其他会话更新，已重新加载，请确认后再保存', 'danger')
      } else {
        uiStore.showToast(`保存 YAML 失败: ${formatError(err)}`, 'danger')
      }
    } finally {
      isSavingYaml.value = false
    }
  }

  const saveForm = async () => {
    if (!formConfig.value || !formDirty.value || formReadOnly.value) return
    try {
      isSavingForm.value = true
      const saved = await updateDispatcherConfig(formConfig.value, configData.value?.revision)
      setFormConfig(saved.config)
      yamlContent.value = saved.content
      yamlOriginal.value = saved.content
      await Promise.all([refetchSummary(), refetchConfig(), refetchYaml()])
      // 刷新 worker 清单查询，让模板面板能立即看到新增/改动的 worker。
      queryClient.invalidateQueries({ queryKey: ['infra-workers'] })
      uiStore.showToast('dispatch.yaml 已保存，dispatcher 将在下一轮热加载', 'success')
    } catch (err) {
      if (isRevisionConflict(err)) {
        await Promise.all([refetchConfig(), refetchYaml()])
        uiStore.showToast('配置已被其他会话更新，已重新加载，请确认后再保存', 'danger')
      } else {
        uiStore.showToast(`保存表单失败: ${formatError(err)}`, 'danger')
      }
    } finally {
      isSavingForm.value = false
    }
  }

  const resetYaml = () => {
    yamlContent.value = yamlOriginal.value
  }

  const resetForm = () => {
    if (!formOriginal.value) return
    formConfig.value = normalizeConfig(JSON.parse(formOriginal.value))
  }

  const setFormConfig = (config: ConfigRecord) => {
    const normalized = normalizeConfig(config)
    formConfig.value = normalized
    formOriginal.value = formSnapshot(normalized)
  }

  // 通用：从数组里删除一项（images / profiles / workers 共用）。
  const removeItem = (items: unknown[], index: number) => {
    items.splice(index, 1)
  }

  // 三路数据源就绪后同步到本地可编辑状态：dispatcher 上报 YAML 路径时用接口返回的
  // 结构化配置 / 原文 YAML；未上报时退化为心跳快照拼出的只读 config 与 YAML。
  // 放在所有 const 定义之后，避免 immediate watcher 命中 setFormConfig 的 TDZ。
  watch(yamlData, value => {
    if (!value) return
    yamlContent.value = value.content
    yamlOriginal.value = value.content
  }, { immediate: true })

  watch(configData, value => {
    if (!value) return
    setFormConfig(value.config)
  }, { immediate: true })

  watch(snapshotConfig, value => {
    if (yamlWritable.value || configData.value) return
    setFormConfig(value)
    yamlContent.value = snapshotYaml.value
    yamlOriginal.value = snapshotYaml.value
  }, { immediate: true })

  return {
    // 查询与状态
    dispatcher,
    dispatcherYamlPath,
    yamlWritable,
    isLoadingSummary,
    isSummaryError,
    summaryError,
    isLoadingConfig,
    isConfigError,
    configError,
    isYamlError,
    yamlError,
    isRefreshing,
    isSavingYaml,
    isSavingForm,
    // 表单配置
    formConfig,
    formDirty,
    formReadOnly,
    configWorkerNames,
    saveForm,
    resetForm,
    // YAML 配置
    yamlContent,
    yamlOriginal,
    yamlDirty,
    yamlReadOnly,
    editorYamlContent,
    saveYaml,
    resetYaml,
    // 通用操作
    refreshAll,
    removeItem,
  }
}

// ============ 纯函数辅助（模块级，供两个页面与 composable 共用） ============

export const joinList = (value: unknown) => stringList(value).join(', ')

export const splitList = (value: string) => value.split(',').map(item => item.trim()).filter(Boolean)

export function stringList(value: unknown) {
  if (!Array.isArray(value)) return []
  const seen = new Set<string>()
  return value
    .map(item => String(item).trim())
    .filter(item => {
      if (!item || seen.has(item)) return false
      seen.add(item)
      return true
    })
}

export const formatJson = (value: unknown) => JSON.stringify(isRecord(value) ? value : {}, null, 2)

export function parseJsonObject(value: string, fallback: unknown) {
  try {
    const parsed = JSON.parse(value || '{}')
    if (isRecord(parsed)) return parsed
  } catch {
    // 调用方负责 toast；这里只返回 fallback 保持与原实现一致的签名。
  }
  return fallback
}

export function normalizeConfig(raw: ConfigRecord) {
  const config = clonePlain(raw || {})
  config.runtime = {
    interval: 3,
    max_workers: 1,
    max_running_projects: 1,
    max_project_workers: 1,
    healthcheck_timeout: 20,
    prompt_group: 'goal-driven-v1',
    ...(isRecord(config.runtime) ? config.runtime : {}),
  }
  config.tasks = isRecord(config.tasks) ? config.tasks : {}
  for (const task of taskNames) {
    const defaults = task === 'explore'
      ? { timeout: 900, conclude_timeout: 300 }
      : {
          timeout: 300,
          ...(task === 'reason' ? { max_intents: 2 } : { conclude_timeout: 90 }),
        }
    config.tasks[task] = {
      ...defaults,
      ...(isRecord(config.tasks[task]) ? config.tasks[task] : {}),
    }
  }
  config.container = {
    image: '',
    allowed_images: [],
    network_mode: 'none',
    completed_action: 'stop',
    execution_profile: 'shared_local',
    cap_add: [],
    ...(isRecord(config.container) ? config.container : {}),
  }
  config.container.allowed_images = stringList(config.container.allowed_images)
  config.container.cap_add = stringList(config.container.cap_add)
  config.images = Array.isArray(config.images) ? config.images.map(normalizeImage) : []
  config.execution_profiles = Array.isArray(config.execution_profiles) ? config.execution_profiles.map(normalizeProfile) : []
  config.workers = Array.isArray(config.workers) ? config.workers.map(normalizeWorker) : []
  if (!isRecord(config.common_env)) config.common_env = {}
  if (!Array.isArray(config.providers)) config.providers = []
  if (!Array.isArray(config.models)) config.models = []
  return config
}

export function normalizeWorker(value: unknown): WorkerDraft & Record<string, unknown> {
  const item = isRecord(value) ? value : {}
  return {
    ...item,
    name: String(item.name || item.id || ''),
    type: String(item.type || item.engine || ''),
    task_types: stringList(item.task_types),
    max_running: Number(item.max_running || item.concurrency || 1),
    priority: Number(item.priority || 0),
    enabled: item.enabled !== false,
    env: isRecord(item.env) ? item.env : {},
  }
}

function normalizeImage(value: unknown) {
  return {
    id: isRecord(value) ? String(value.id || '') : '',
    image: isRecord(value) ? String(value.image || '') : String(value || ''),
  }
}

function normalizeProfile(value: unknown) {
  const item = isRecord(value) ? value : {}
  return {
    id: String(item.id || ''),
    image_ref: String(item.image_ref || 'default'),
    network_policy: String(item.network_policy || 'none'),
    cap_add: stringList(item.cap_add),
    isolation: String(item.isolation || 'shared_container'),
    completed_action: String(item.completed_action || 'stop'),
  }
}

// dispatcher 未上报可写 YAML 路径时，用心跳快照拼出只读 config 供展示。
function configFromSnapshot(value: MonitoringSummary['dispatcher']) {
  return {
    server: '',
    dispatcher_id: value.last_dispatcher_id || '',
    runtime: value.runtime || {},
    tasks: (value.runtime || {}).tasks || {},
    container: {
      ...(value.container || {}),
      allowed_images: value.container_images || [],
      cap_add: value.cap_baseline || [],
    },
    images: (value.container_images || []).map((image, index) => ({ id: index === 0 ? 'default' : slugId(image), image })),
    execution_profiles: inventoryList(value.declared_inventory?.execution_profiles),
    workers: (value.worker_pool || []).map(worker => ({
      name: worker.worker_name,
      type: worker.worker_type,
      task_types: worker.task_types || [],
      max_running: worker.max_running,
      priority: worker.priority,
      enabled: (worker as unknown as Record<string, unknown>).enabled !== false,
      env: {},
    })),
  }
}

function inventoryList(value: unknown) {
  return Array.isArray(value) ? clonePlain(value) : []
}

export function dumpYaml(value: unknown, indent = 0): string {
  if (Array.isArray(value)) {
    if (value.length === 0) return `${spaces(indent)}[]\n`
    return value.map(item => {
      if (isScalar(item)) return `${spaces(indent)}- ${formatYamlScalar(item)}\n`
      const nested = dumpYaml(item, indent + 2)
      return `${spaces(indent)}-\n${nested}`
    }).join('')
  }
  if (isRecord(value)) {
    return Object.entries(value).map(([key, item]) => {
      if (Array.isArray(item) && item.length === 0) return `${spaces(indent)}${key}: []\n`
      if (isRecord(item) && Object.keys(item).length === 0) return `${spaces(indent)}${key}: {}\n`
      if (isScalar(item)) return `${spaces(indent)}${key}: ${formatYamlScalar(item)}\n`
      return `${spaces(indent)}${key}:\n${dumpYaml(item, indent + 2)}`
    }).join('')
  }
  return `${spaces(indent)}${formatYamlScalar(value)}\n`
}

function formatYamlScalar(value: unknown) {
  if (value === null || value === undefined) return 'null'
  if (typeof value === 'number' || typeof value === 'boolean') return String(value)
  const text = String(value)
  if (!text) return '""'
  if (/^[A-Za-z0-9_.:/@+-]+$/.test(text)) return text
  return JSON.stringify(text)
}

function spaces(count: number) {
  return ' '.repeat(count)
}

function isScalar(value: unknown) {
  return value === null || value === undefined || ['string', 'number', 'boolean'].includes(typeof value)
}

export function isRecord(value: unknown): value is ConfigRecord {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
}

function clonePlain<T>(value: T): T {
  return JSON.parse(JSON.stringify(value))
}

function formSnapshot(value: ConfigRecord) {
  return JSON.stringify(value)
}

export const inputValue = (event: Event) => (event.target as HTMLInputElement | HTMLTextAreaElement).value

export const formatError = (err: unknown) => err instanceof Error ? err.message : String(err || '未知错误')

export function slugId(value: string) {
  return value.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '') || 'item'
}

export const taskFields = (task: string) => {
  if (task === 'reason') return ['timeout', 'max_intents']
  return ['timeout', 'conclude_timeout']
}

export { runtimeNumberFields, taskNames }
