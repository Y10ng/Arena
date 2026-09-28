<template>
  <div class="new-project-container">
    <main class="new-project-workflow">
      <Card id="np-section-basic" class="form-section" padding="lg">
        <div class="section-title-row">
          <div>
            <h2>项目配置</h2>
            <p class="section-desc">选择场景，自由描述已知信息与目标，并确认执行 Worker。</p>
          </div>
        </div>

        <FormField label="项目名称" for="np-title" required :error="validationErrors.title">
          <Input
            id="np-title"
            ref="titleInput"
            v-model="form.title"
            placeholder="例如: arena-web-recon"
          />
        </FormField>

        <FormField
          id="np-group"
          class="validation-anchor"
          label="所属项目组"
          for="np-group-select"
          required
          :error="validationErrors.group"
          hint="新项目必须加入当前 Tenant 的项目组；可在项目列表页管理项目组。"
        >
          <div v-if="groupsLoading" aria-label="正在加载项目组">
            <Skeleton class="select-skeleton" />
          </div>
          <ErrorState
            v-else-if="groupsError"
            title="项目组加载失败"
            description="无法确认当前 Tenant 的项目组，暂时无法创建项目。"
            @retry="refetchGroups()"
          />
          <EmptyState v-else-if="groups.length === 0" title="暂无可用项目组">
            <template #description>
              请先前往<router-link to="/projects">项目列表</router-link>创建项目组。
            </template>
          </EmptyState>
          <Select
            v-else
            id="np-group-select"
            v-model="form.group_id"
            :options="groupOptions"
            placeholder="请选择项目组"
            required
          />
        </FormField>

        <FormField label="探索场景 (Scenario)">
          <div v-if="scenariosLoading" class="skeleton-scenarios" aria-label="正在加载场景">
            <Skeleton v-for="i in 2" :key="i" class="skeleton-card" />
          </div>
          <ErrorState
            v-else-if="scenariosError"
            title="场景目录加载失败"
            description="场景目录暂不可用，无法创建项目。"
            @retry="refetchScenarios()"
          />
          <EmptyState
            v-else-if="scenarios.length === 0"
            title="暂无可用场景"
            description="场景目录为空，暂时无法创建项目。"
          />
          <div v-else class="scenarios-grid">
            <button
              v-for="sc in scenarios"
              :key="sc.key"
              type="button"
              class="scenario-card"
              :class="{ selected: form.scenario === sc.key }"
              :aria-pressed="form.scenario === sc.key"
              @click="selectScenario(sc)"
            >
              <span class="scenario-header">
                <span class="scenario-title">{{ sc.label }}</span>
                <Badge v-if="sc.is_default" tone="info" size="sm">默认项</Badge>
              </span>
              <span class="scenario-desc">应用对应的执行画像、Prompt 与资源策略。</span>
            </button>
          </div>
        </FormField>

        <FormField
          id="np-origin"
          class="validation-anchor"
          label="Origin（已知信息）"
          for="np-origin-input"
          required
          :error="validationErrors.origin"
          hint="自由描述已有信息、业务背景、观察、线索或资料情况，无需包含网址。密码、Token、Cookie 等凭据请勿填写在这里。"
        >
          <Textarea
            id="np-origin-input"
            v-model="form.origin"
            placeholder="例如：已有接口说明和两个测试角色，重点关注订单审批相关功能。"
            :rows="7"
            @input="scenarioTemplateMeta.origin.userTouched = true"
          />
        </FormField>

        <FormField
          id="np-goal"
          class="validation-anchor"
          label="Goal（目标）"
          for="np-goal-input"
          required
          :error="validationErrors.goal"
          hint="自由描述希望达成的目标、要回答的问题、预期结果与明确排除项。"
        >
          <Textarea
            id="np-goal-input"
            v-model="form.goal"
            placeholder="例如：重点检查普通用户之间的数据隔离及审批流程。"
            :rows="7"
            @input="scenarioTemplateMeta.goal.userTouched = true"
          />
        </FormField>

        <FormField
          id="np-workers"
          class="api-config-group validation-anchor"
          label="Workers"
          required
          hint="项目只会使用这里选择的 Tenant Worker，不会回退到 Dispatcher 全局配置。"
        >
          <div v-if="workersLoading" class="worker-loading" aria-label="正在加载 Worker">
            <Skeleton v-for="i in 2" :key="i" class="worker-skeleton" />
          </div>
          <ErrorState
            v-else-if="workersError"
            title="Worker 加载失败"
            description="无法确认当前 Tenant 的可用 Worker。"
            @retry="refetchWorkers()"
          />
          <EmptyState v-else-if="workers.length === 0" title="暂无可用 Worker">
            <template #description>
              <template v-if="allWorkers.length === 0">
                当前 Tenant 尚未创建 Worker。请前往
                <router-link to="/console/workers">Workers</router-link> 新增配置。
              </template>
              <template v-else>
                现有 Worker 配置尚未就绪。请前往
                <router-link to="/console/workers">Workers</router-link> 查看 Model、Key
                或启用状态。
              </template>
            </template>
          </EmptyState>
          <template v-else>
            <FormField label="Worker 模板" for="np-worker-template">
              <Skeleton v-if="workerTemplatesLoading" class="select-skeleton" />
              <ErrorState
                v-else-if="workerTemplatesError"
                title="Worker 模板加载失败"
                description="仍可手动选择 Worker。"
                @retry="refetchWorkerTemplates()"
              />
              <Select
                v-else
                id="np-worker-template"
                v-model="selectedTemplateId"
                :options="workerTemplateOptions"
                @update:model-value="applyWorkerTemplate"
              />
            </FormField>
            <div class="worker-options">
              <div
                v-for="worker in workers"
                :key="worker.id"
                class="worker-option"
                :class="{
                  selected: form.tenant_worker_ids.includes(worker.id),
                  disabled: isSubmitting,
                }"
              >
                <Checkbox
                  :id="`np-worker-${worker.id}`"
                  :model-value="form.tenant_worker_ids.includes(worker.id)"
                  :disabled="isSubmitting"
                  :aria-label="`选择 Worker ${worker.display_name}`"
                  @update:model-value="setWorkerSelection(worker.id, $event)"
                />
                <label class="worker-option-content" :for="`np-worker-${worker.id}`">
                  <span class="worker-option-name">{{ worker.display_name }}</span>
                  <span class="worker-option-meta"
                    >{{ worker.engine }} · {{ workerLabel(worker) }}</span
                  >
                  <span class="worker-option-tasks">{{ worker.task_types.join('/') }}</span>
                </label>
              </div>
            </div>
          </template>
        </FormField>

        <details class="advanced-settings" open>
          <summary>
            <span>高级设置</span>
            <span class="summary-hint">预算、时限与辅助资料</span>
          </summary>

          <div class="advanced-settings-content">
            <div class="advanced-subsection">
              <div class="section-title-row compact-title-row">
                <div>
                  <h3>执行边界</h3>
                  <p class="section-desc">逻辑预算、执行截止与真实授权期限相互独立。</p>
                </div>
              </div>
              <div class="execution-settings-grid">
                <FormField
                  label="Explore 逻辑预算"
                  for="np-explore-budget"
                  required
                  :error="validationErrors.exploreBudget"
                  hint="按逻辑 Explore Attempt 计费；同一 Attempt 的物理重试不会重复扣减。"
                >
                  <Input
                    id="np-explore-budget"
                    v-model="form.explore_budget"
                    type="number"
                    min="1"
                    max="10000"
                    step="1"
                  />
                </FormField>
                <FormField
                  label="执行截止时间"
                  for="np-explore-deadline"
                  :error="validationErrors.exploreDeadline"
                  hint="可选。到时停止新增 Explore，不代表真实授权期限。"
                >
                  <Input
                    id="np-explore-deadline"
                    v-model="form.explore_deadline"
                    type="datetime-local"
                  />
                </FormField>
                <FormField
                  label="真实授权期限"
                  for="np-authorization-expires-at"
                  required
                  :error="validationErrors.authorizationExpiresAt"
                  hint="必须明确填写未来的绝对时间，或确认真实授权无限期；不会从执行截止时间推导。"
                >
                  <Input
                    id="np-authorization-expires-at"
                    v-model="form.authorization_expires_at"
                    type="datetime-local"
                    :disabled="authorizationUnlimited"
                  />
                  <label class="authorization-choice">
                    <Checkbox
                      v-model="authorizationUnlimited"
                      :disabled="isSubmitting"
                      aria-label="授权无限期有效"
                    />
                    <span>明确确认真实授权无限期有效</span>
                  </label>
                </FormField>
              </div>
            </div>

            <div class="advanced-subsection">
              <div class="section-title-row compact-title-row">
                <div>
                  <h3>可复用资料库 <Badge tone="info" size="sm">可选</Badge></h3>
                  <p class="section-desc">创建时冻结当前版本，后续全局更新不会改变本项目。</p>
                </div>
              </div>
              <div v-if="libraryCatalogLoading" class="library-loading" aria-label="正在加载资料库">
                <Skeleton v-for="i in 2" :key="i" class="library-skeleton" />
              </div>
              <ErrorState
                v-else-if="libraryCatalogError"
                title="资料库目录加载失败"
                description="可重试；资料库为可选项。"
                @retry="refetchLibraryCatalog()"
              />
              <div v-else-if="libraryCatalog.length" class="library-options">
                <button
                  v-for="library in libraryCatalog"
                  :key="library.key"
                  type="button"
                  class="library-option"
                  :class="{ selected: form.library_keys.includes(library.key) }"
                  :aria-pressed="form.library_keys.includes(library.key)"
                  @click="toggleLibrary(library.key)"
                >
                  <span class="library-option-title">{{ library.name }}</span>
                  <span class="mono"
                    >{{ library.key }} · v{{ library.latest_version }} ·
                    {{ library.file_count }} 文件</span
                  >
                </button>
              </div>
              <EmptyState
                v-else
                title="暂无可选资料库"
                description="无资料库不会影响评估完整性。"
              />
            </div>

            <div class="advanced-subsection">
              <div class="resource-title-row">
                <div>
                  <h3>项目本地辅助资料 <Badge tone="info" size="sm">可选</Badge></h3>
                  <p class="section-desc">任意文件原样上传，可包含授权范围内的凭据。</p>
                </div>
              </div>
              <ProjectResourceBatchUploader
                ref="resourceUploader"
                v-model="resourceBatch"
                :disabled="isSubmitting"
                @busy="resourceBusy = $event"
              />
            </div>
          </div>
        </details>
      </Card>

      <Card class="form-bottom-bar" padding="md">
        <Button
          variant="secondary"
          :disabled="isSubmitting || resourceBusy"
          @click="cancelNewProject"
          >取消</Button
        >
        <Button
          variant="success"
          :loading="isSubmitting"
          :disabled="!canSubmitProject"
          @click="submitProject"
        >
          确认创建项目
        </Button>
      </Card>
    </main>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import { useQuery } from '@tanstack/vue-query'
import { useRouter } from 'vue-router'
import {
  api,
  createProject,
  listLibraryCatalog,
  listProjectGroups,
  listTenantWorkerOptions,
  listWorkerTemplates,
} from '@/api/client'
import type {
  AdvisoryLibrary,
  CreateProjectRequest,
  ProjectGroup,
  ProjectResourceBatch,
  ScenarioCatalogEntry,
  TenantWorker,
  WorkerTemplate,
} from '@/api/types'
import { useUIStore } from '@/stores/ui'
import { ErrorState } from '@/components/patterns'
import {
  Badge,
  Button,
  Card,
  Checkbox,
  EmptyState,
  FormField,
  Input,
  Select,
  Skeleton,
  Textarea,
  type SelectOption,
} from '@/components/ui'
import ProjectResourceBatchUploader from './ProjectResourceBatchUploader.vue'
import { applyScenarioTemplates, type ScenarioTemplateState } from './newProjectScenarioTemplates'

const router = useRouter()
const uiStore = useUIStore()

const isSubmitting = ref(false)
const titleInput = ref<{ focus: (options?: FocusOptions) => void } | null>(null)
const resourceUploader = ref<{ cancel: () => Promise<void> } | null>(null)
const resourceBatch = ref<ProjectResourceBatch | null>(null)
const resourceBusy = ref(false)

const form = reactive({
  title: '',
  origin: '',
  goal: '',
  scenario: '',
  tenant_worker_ids: [] as string[],
  group_id: '',
  library_keys: [] as string[],
  explore_budget: 40 as number | null,
  explore_deadline: '',
  authorization_expires_at: '',
})
const authorizationUnlimited = ref(false)
const workerSelectionTouched = ref(false)

const scenarioTemplateMeta = reactive({
  selectedKey: null as string | null,
  origin: { userTouched: false, lastAutoApplied: null as string | null },
  goal: { userTouched: false, lastAutoApplied: null as string | null },
})

const validationErrors = reactive({
  title: '',
  group: '',
  origin: '',
  goal: '',
  exploreBudget: '',
  exploreDeadline: '',
  authorizationExpiresAt: '',
})

const {
  data: scenariosData,
  isLoading: scenariosLoading,
  isError: scenariosError,
  refetch: refetchScenarios,
} = useQuery<ScenarioCatalogEntry[]>({
  queryKey: ['scenario-catalog'],
  queryFn: () => api<ScenarioCatalogEntry[]>('GET', '/scenario-catalog'),
})
const scenarios = computed(() => scenariosData.value || [])
const selectedScenarioExists = computed(() =>
  scenarios.value.some((scenario) => scenario.key === form.scenario),
)

const {
  data: workersData,
  isLoading: workersLoading,
  isError: workersError,
  refetch: refetchWorkers,
} = useQuery<TenantWorker[]>({
  queryKey: ['tenant-worker-options'],
  queryFn: listTenantWorkerOptions,
})
const allWorkers = computed(() => workersData.value || [])
const workers = computed(() => allWorkers.value.filter((worker) => worker.available))
const availableWorkerIds = computed(() => new Set(workers.value.map((worker) => worker.id)))

const {
  data: groupsData,
  isLoading: groupsLoading,
  isError: groupsError,
  refetch: refetchGroups,
} = useQuery<ProjectGroup[]>({
  queryKey: ['project-groups'],
  queryFn: listProjectGroups,
})
const groups = computed(() => groupsData.value || [])
const groupOptions = computed<SelectOption[]>(() =>
  groups.value.map((group) => ({ value: group.id, label: group.name })),
)
const selectedGroupExists = computed(() => groups.value.some((group) => group.id === form.group_id))

const {
  data: libraryCatalogData,
  isLoading: libraryCatalogLoading,
  isError: libraryCatalogError,
  refetch: refetchLibraryCatalog,
} = useQuery<AdvisoryLibrary[]>({
  queryKey: ['library-catalog'],
  queryFn: listLibraryCatalog,
})
const libraryCatalog = computed(() => libraryCatalogData.value || [])

const {
  data: workerTemplatesData,
  isLoading: workerTemplatesLoading,
  isError: workerTemplatesError,
  refetch: refetchWorkerTemplates,
} = useQuery<WorkerTemplate[]>({
  queryKey: ['worker-templates'],
  queryFn: listWorkerTemplates,
})
const workerTemplates = computed(() => workerTemplatesData.value || [])
const workerTemplateOptions = computed<SelectOption[]>(() => [
  { value: '__none__', label: '不使用模板' },
  ...workerTemplates.value.map((template) => ({
    value: template.id,
    label: `${template.name} (${template.tenant_worker_ids.length})`,
  })),
])
const selectedTemplateId = ref('__none__')

const canSubmitProject = computed(
  () =>
    !isSubmitting.value &&
    !resourceBusy.value &&
    !scenariosLoading.value &&
    !scenariosError.value &&
    !workersLoading.value &&
    !workersError.value &&
    !groupsLoading.value &&
    !groupsError.value &&
    selectedGroupExists.value &&
    selectedScenarioExists.value &&
    form.tenant_worker_ids.length > 0,
)

const selectScenario = (scenario: ScenarioCatalogEntry) => {
  const currentState: ScenarioTemplateState = {
    selectedKey: scenarioTemplateMeta.selectedKey,
    origin: {
      value: form.origin,
      userTouched: scenarioTemplateMeta.origin.userTouched,
      lastAutoApplied: scenarioTemplateMeta.origin.lastAutoApplied,
    },
    goal: {
      value: form.goal,
      userTouched: scenarioTemplateMeta.goal.userTouched,
      lastAutoApplied: scenarioTemplateMeta.goal.lastAutoApplied,
    },
  }
  const nextState = applyScenarioTemplates(currentState, scenario)

  form.scenario = scenario.key
  form.origin = nextState.origin.value
  form.goal = nextState.goal.value
  scenarioTemplateMeta.selectedKey = nextState.selectedKey
  scenarioTemplateMeta.origin.userTouched = nextState.origin.userTouched
  scenarioTemplateMeta.origin.lastAutoApplied = nextState.origin.lastAutoApplied
  scenarioTemplateMeta.goal.userTouched = nextState.goal.userTouched
  scenarioTemplateMeta.goal.lastAutoApplied = nextState.goal.lastAutoApplied
}

watch(
  scenarios,
  (list) => {
    if (list.length === 0) return
    const selected = list.find((scenario) => scenario.key === form.scenario)
    if (selected) {
      selectScenario(selected)
      return
    }
    selectScenario(list.find((scenario) => scenario.is_default) || list[0])
  },
  { immediate: true },
)

watch(
  workers,
  (list) => {
    // 刷新时只清理失效绑定；用户操作过选择后，不再擅自补回默认 Worker。
    form.tenant_worker_ids = form.tenant_worker_ids.filter((workerId) =>
      availableWorkerIds.value.has(workerId),
    )
    if (!workerSelectionTouched.value && form.tenant_worker_ids.length === 0 && list[0]) {
      form.tenant_worker_ids = [list[0].id]
    }
  },
  { immediate: true },
)

watch(
  groups,
  () => {
    // 项目组被删除或 Tenant 上下文切换后，不保留已经失效的创建选择。
    if (form.group_id && !selectedGroupExists.value) form.group_id = ''
    if (selectedGroupExists.value) validationErrors.group = ''
  },
  { immediate: true },
)

watch(
  () => form.group_id,
  () => {
    if (selectedGroupExists.value) validationErrors.group = ''
  },
)

const setWorkerSelection = (workerId: string, selected: boolean) => {
  workerSelectionTouched.value = true
  const index = form.tenant_worker_ids.indexOf(workerId)
  if (selected && index < 0) form.tenant_worker_ids.push(workerId)
  if (!selected && index >= 0) form.tenant_worker_ids.splice(index, 1)
}

const workerLabel = (worker: TenantWorker) => worker.model_display_name || worker.model_id

const applyWorkerTemplate = () => {
  workerSelectionTouched.value = true
  if (selectedTemplateId.value === '__none__') return
  const template = workerTemplates.value.find((item) => item.id === selectedTemplateId.value)
  if (!template) return

  form.tenant_worker_ids = template.tenant_worker_ids.filter((workerId) =>
    availableWorkerIds.value.has(workerId),
  )
  if (form.tenant_worker_ids.length < template.tenant_worker_ids.length) {
    uiStore.showToast('模板中部分 Worker 当前不可用，已自动忽略', 'info')
  }
}

const toggleLibrary = (key: string) => {
  const index = form.library_keys.indexOf(key)
  if (index >= 0) form.library_keys.splice(index, 1)
  else form.library_keys.push(key)
}

const optionalDateTimeToIso = (value: string) =>
  value ? new Date(value).toISOString() : undefined

const validateForm = (): { valid: boolean; firstErrorAnchor: string | null } => {
  let valid = true
  let firstErrorAnchor: string | null = null

  if (!form.title.trim()) {
    validationErrors.title = '项目标题不能为空'
    valid = false
    firstErrorAnchor = 'np-section-basic'
  } else {
    validationErrors.title = ''
  }

  if (!form.origin.trim()) {
    validationErrors.origin = '已知信息不能为空'
    valid = false
    firstErrorAnchor = firstErrorAnchor || 'np-origin'
  } else {
    validationErrors.origin = ''
  }

  if (!form.goal.trim()) {
    validationErrors.goal = '目标不能为空'
    valid = false
    firstErrorAnchor = firstErrorAnchor || 'np-goal'
  } else {
    validationErrors.goal = ''
  }

  if (
    form.explore_budget === null ||
    !Number.isInteger(form.explore_budget) ||
    form.explore_budget < 1 ||
    form.explore_budget > 10000
  ) {
    validationErrors.exploreBudget = 'Explore 逻辑预算必须是 1 到 10000 之间的整数'
    valid = false
    firstErrorAnchor = firstErrorAnchor || 'np-section-basic'
  } else {
    validationErrors.exploreBudget = ''
  }

  const validateFutureDate = (value: string, label: string) => {
    if (!value) return ''
    const timestamp = new Date(value).getTime()
    if (!Number.isFinite(timestamp)) return `${label}格式无效`
    if (timestamp <= Date.now()) return `${label}必须晚于当前时间`
    return ''
  }
  validationErrors.exploreDeadline = validateFutureDate(form.explore_deadline, '执行截止时间')
  validationErrors.authorizationExpiresAt = authorizationUnlimited.value
    ? ''
    : form.authorization_expires_at
      ? validateFutureDate(form.authorization_expires_at, '授权过期时间')
      : '请选择授权过期时间或明确确认无限期授权'
  if (validationErrors.exploreDeadline || validationErrors.authorizationExpiresAt) {
    valid = false
    firstErrorAnchor = firstErrorAnchor || 'np-section-basic'
  }

  return { valid, firstErrorAnchor }
}

const submitProject = async () => {
  if (isSubmitting.value) return
  if (!selectedScenarioExists.value) {
    uiStore.showToast(
      scenariosLoading.value ? '场景目录仍在加载，请稍后重试' : '请选择有效的探索场景',
      'danger',
    )
    return
  }
  if (workersLoading.value || workersError.value) {
    uiStore.showToast(
      workersLoading.value ? 'Worker 列表仍在加载，请稍后重试' : '无法确认可用 Worker，请重试加载',
      'danger',
    )
    return
  }
  if (form.tenant_worker_ids.length === 0) {
    uiStore.showToast('请至少选择一个可用 Worker', 'danger')
    document.getElementById('np-workers')?.scrollIntoView({ behavior: 'smooth', block: 'center' })
    return
  }
  if (groupsLoading.value || groupsError.value) {
    uiStore.showToast(
      groupsLoading.value ? '项目组列表仍在加载，请稍后重试' : '无法确认可用项目组，请重试加载',
      'danger',
    )
    return
  }
  if (!selectedGroupExists.value) {
    validationErrors.group = '请选择有效的项目组'
    uiStore.showToast('请选择有效的项目组', 'danger')
    document.getElementById('np-group')?.scrollIntoView({ behavior: 'smooth', block: 'center' })
    return
  }

  if (resourceBusy.value) {
    uiStore.showToast('辅助资料仍在上传，请稍后重试', 'danger')
    return
  }

  const { valid, firstErrorAnchor } = validateForm()
  if (!valid) {
    uiStore.showToast('请检查表单必填项', 'danger')
    if (firstErrorAnchor) {
      document
        .getElementById(firstErrorAnchor)
        ?.scrollIntoView({ behavior: 'smooth', block: 'start' })
    }
    return
  }

  const payload: CreateProjectRequest = {
    title: form.title.trim(),
    origin: form.origin.trim(),
    goal: form.goal.trim(),
    scenario: form.scenario,
    tenant_worker_ids: [...form.tenant_worker_ids],
    group_id: form.group_id,
    explore_budget: form.explore_budget as number,
    ...(form.explore_deadline
      ? { explore_deadline: optionalDateTimeToIso(form.explore_deadline) }
      : {}),
    authorization_expires_at: authorizationUnlimited.value
      ? null
      : new Date(form.authorization_expires_at).toISOString(),
    ...(resourceBatch.value ? { resource_batch_id: resourceBatch.value.id } : {}),
    library_keys: [...form.library_keys],
  }

  try {
    isSubmitting.value = true
    const response = await createProject(payload)
    resourceBatch.value = null
    uiStore.showToast('项目已创建，正在等待 Dispatcher 调度。', 'success')
    await router.push(`/projects/${response.project.id}`)
  } catch (err: any) {
    uiStore.showToast(err.message || '项目创建失败，请重试。', 'danger')
  } finally {
    isSubmitting.value = false
  }
}

const cancelNewProject = async () => {
  if (resourceBusy.value || isSubmitting.value) return
  try {
    await resourceUploader.value?.cancel()
  } catch (err: any) {
    uiStore.showToast(err.message || '取消临时上传批次失败', 'danger')
  }
  await router.push('/projects')
}

const handleKeydown = (event: KeyboardEvent) => {
  if ((event.metaKey || event.ctrlKey) && event.key === 'Enter') {
    void submitProject()
  }
}

onMounted(() => {
  titleInput.value?.focus()
  window.addEventListener('keydown', handleKeydown)
})

onUnmounted(() => {
  window.removeEventListener('keydown', handleKeydown)
})
</script>

<style src="./NewProject.css" scoped></style>

<style scoped>
.new-project-header {
  max-width: 860px;
  margin-right: auto;
  margin-left: auto;
}

.new-project-workflow {
  width: min(100%, 860px);
  margin: 0 auto;
}

.form-section {
  padding: var(--space-5);
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  background-color: var(--bg-surface);
  box-shadow: var(--shadow-sm);
}

.section-title-row {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: var(--space-3);
  margin-bottom: var(--space-4);
}

.section-title-row h2,
.section-title-row h3,
.resource-title-row h3 {
  margin: 0;
  color: var(--text);
  font-size: 15px;
}

.section-desc {
  margin: 4px 0 0;
  color: var(--text-muted);
  font-size: 12px;
}

.validation-anchor {
  scroll-margin-top: 16px;
}

.field-hint {
  display: block;
  margin-top: var(--space-1);
  color: var(--text-faint);
  font-size: 11px;
}

.required-mark {
  color: var(--danger);
}

.api-config-group {
  margin-top: var(--space-5);
}

.worker-options {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-top: 12px;
}

.worker-option {
  position: relative;
  display: flex;
  align-items: flex-start;
  gap: var(--space-2);
  padding: 8px 12px;
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  background: var(--bg-surface);
  color: var(--text);
  cursor: pointer;
  transition:
    border-color var(--dur-fast),
    background var(--dur-fast);
}

.worker-option:hover {
  border-color: var(--border-strong);
}

.worker-option.selected {
  border-color: var(--accent);
  background: var(--accent-dim);
}

.worker-option :deep(.ui-checkbox) {
  position: relative;
  z-index: 1;
}

.worker-option.disabled,
.worker-option.disabled .worker-option-content {
  cursor: not-allowed;
}

.worker-option.disabled {
  opacity: 0.7;
}

.worker-option-content {
  display: flex;
  flex: 1;
  flex-direction: column;
  gap: 2px;
  cursor: pointer;
}

.worker-option-content::before {
  position: absolute;
  inset: 0;
  content: '';
}

.worker-option-name {
  font-size: 12px;
  font-weight: 600;
}

.worker-option-meta {
  color: var(--text-muted);
  font-size: 11px;
}

.worker-option-tasks {
  color: var(--text-faint);
  font-size: 10px;
}

.worker-loading,
.library-loading {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: var(--space-2);
}

.worker-skeleton,
.library-skeleton,
.select-skeleton {
  min-height: var(--control-height-lg);
}

.template-row {
  display: flex;
  align-items: center;
  gap: 6px;
  margin: 12px 0;
}

.template-label {
  color: var(--text-muted);
  font-size: 12px;
}

.template-select {
  padding: 4px 8px;
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  background: var(--bg-surface);
  color: var(--text);
  font-size: 12px;
}

.advanced-settings {
  margin-top: var(--space-5);
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  background: var(--bg-inset);
}

.advanced-settings summary {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-3);
  padding: var(--space-3);
  color: var(--text);
  font-size: 12px;
  font-weight: 700;
  cursor: pointer;
}

.summary-hint {
  color: var(--text-faint);
  font-size: 11px;
  font-weight: 400;
}

.advanced-settings-content {
  display: flex;
  flex-direction: column;
  gap: var(--space-5);
  padding: 0 var(--space-3) var(--space-3);
}

.advanced-subsection {
  padding-top: var(--space-4);
  border-top: 1px solid var(--border);
}

.compact-title-row {
  margin-bottom: var(--space-3);
}

.execution-settings-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: var(--space-3);
}

.authorization-choice {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  margin-top: var(--space-2);
  color: var(--text-muted);
  font-size: 12px;
  cursor: pointer;
}

.optional-badge {
  margin-left: 6px;
  padding: 1px 6px;
  border: 1px solid var(--accent);
  border-radius: var(--radius-sm);
  background: var(--accent-dim);
  color: var(--accent);
  font-size: 10px;
  font-weight: 600;
  vertical-align: middle;
}

.library-options {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: var(--space-2);
}

.library-option {
  min-width: 0;
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 3px;
  padding: var(--space-3);
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  background: var(--bg-surface);
  color: var(--text-muted);
  cursor: pointer;
  text-align: left;
}

.library-option:hover {
  border-color: var(--border-strong);
}

.library-option.selected {
  border-color: var(--accent);
  background: var(--accent-dim);
}

.library-option-title {
  color: var(--text);
  font-weight: 700;
}

.resource-title-row {
  display: flex;
  align-items: start;
  justify-content: space-between;
  gap: var(--space-3);
}

.resource-title-row > div:first-child {
  flex: 1;
}

.resource-file-input {
  display: none;
}

.resource-guidance {
  margin-bottom: var(--space-3);
}

.resource-editor-list {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}

.resource-editor-row {
  display: grid;
  grid-template-columns: 90px minmax(0, 1fr) 72px 28px;
  align-items: center;
  gap: var(--space-2);
}

.resource-size {
  color: var(--text-faint);
  font-size: 11px;
  text-align: right;
}

.form-bottom-bar {
  display: flex;
  flex-direction: row;
  justify-content: flex-end;
  gap: var(--space-3);
  margin-top: var(--space-4);
  padding: var(--space-4);
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  background-color: var(--bg-surface);
}

.btn-link-action {
  border: none;
  background: transparent;
  color: var(--accent);
  cursor: pointer;
  font-size: 12px;
}

@media (max-width: 700px) {
  .form-section {
    padding: var(--space-4);
  }

  .execution-settings-grid,
  .library-options {
    grid-template-columns: 1fr;
  }

  .resource-editor-row {
    grid-template-columns: 80px minmax(0, 1fr) 28px;
  }

  .resource-size {
    display: none;
  }

  .summary-hint {
    display: none;
  }
}
</style>
