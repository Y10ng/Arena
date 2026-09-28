<template>
  <article class="console-page configuration-page tenant-workers-page">
    <div class="section-header section-header--actions">
      <Button variant="secondary" size="sm" :disabled="saving" @click="startCreate">
        新增 Worker
      </Button>
    </div>

    <ErrorState
      v-if="workersLoadError"
      title="Worker 加载失败"
      :description="errorText(workersLoadError)"
      @retry="refetchWorkers"
    />
    <ErrorState
      v-if="modelsLoadError"
      title="Model 加载失败"
      :description="errorText(modelsLoadError)"
      @retry="modelsQuery.refetch()"
    />
    <Alert v-else-if="!modelsQuery.isPending.value && !models.length" tone="warning">
      请先在 <router-link to="/console/models">Models</router-link> 页面新增 Model、选择模型名并配置
      API Key。pi 支持 Anthropic Messages、OpenAI Chat Completions 和 OpenAI Responses。
    </Alert>
    <Alert v-if="revisionConflict" tone="warning">
      Worker 已被其他请求更新，已加载最新 revision；当前草稿已保留，请确认后重试。
    </Alert>

    <MasterDetail configuration>
      <template #master>
        <Card class="console-card configuration-panel configuration-list worker-master">
          <div class="list-header">
            <strong>Tenant Workers</strong><span class="text-faint">{{ workers.length }} 个</span>
          </div>
          <template v-if="workersQuery.isPending.value">
            <Skeleton
              v-for="index in 3"
              :key="index"
              :decorative="index !== 1"
              label="正在加载 Worker"
            />
          </template>
          <button
            v-for="worker in workers"
            :key="worker.id"
            class="worker-card"
            :class="{ active: selectedId === worker.id && !creating }"
            type="button"
            :disabled="saving"
            @click="selectWorker(worker.id)"
          >
            <strong class="worker-name" :title="worker.display_name">{{
              worker.display_name
            }}</strong>
            <span
              class="worker-model text-faint"
              :title="worker.model_display_name || worker.model_id"
              >{{ worker.model_display_name || worker.model_id }}</span
            >
            <span class="worker-meta">
              <Badge size="sm">{{ worker.engine }}</Badge>
              <StatusBadge
                :status="worker.available && worker.executor_available ? 'healthy' : 'unhealthy'"
                size="sm"
              >
                {{ tenantWorkerStatusLabel(worker) }}
              </StatusBadge>
            </span>
          </button>
          <EmptyState
            v-if="!workersQuery.isPending.value && !workers.length"
            title="暂无 Worker"
            description="请新增当前 Tenant 的 pi Worker。"
          />
        </Card>
      </template>

      <template #detail>
        <Card
          v-if="creating || currentWorker"
          class="console-card configuration-panel worker-detail"
        >
          <div class="editor-title-row">
            <div>
              <h2>{{ creating ? '新增 Worker' : 'Worker 详情' }}</h2>
              <p class="mono text-faint">{{ currentWorker?.id || '保存后生成 ID' }}</p>
            </div>
            <Button
              v-if="currentWorker && !creating"
              variant="danger"
              size="sm"
              :disabled="saving"
              @click="requestWorkerRemoval"
            >
              删除
            </Button>
          </div>
          <Alert v-if="legacyWorker" tone="warning">
            历史引擎 {{ currentWorker?.engine }} 不受支持，不能执行或转换；请新建 pi Worker。
          </Alert>
          <form class="managed-form" @submit.prevent="saveWorker">
            <FormField v-slot="{ controlAttrs }" class="form-group" label="名称" required>
              <Input
                v-bind="controlAttrs"
                :model-value="draft.display_name"
                @update:model-value="updateDisplayName"
              />
            </FormField>
            <label class="form-group">
              <span class="form-label">Model</span>
              <Select
                v-model="draft.model_id"
                class="mono"
                :options="modelOptions"
                placeholder="请选择 Model"
                aria-label="Worker Model"
                required
              />
            </label>
            <div class="form-grid">
              <label class="form-group">
                <span class="form-label">执行引擎</span>
                <Input :model-value="creating ? 'pi' : currentWorker?.engine || ''" disabled />
              </label>
              <label class="form-group toggle-field">
                <span class="form-label">状态</span>
                <Checkbox v-model="draft.enabled" label="启用" />
              </label>
              <label class="form-group">
                <span class="form-label">最大并发</span>
                <Input
                  :model-value="draft.max_running"
                  type="number"
                  :min="TENANT_WORKER_MAX_RUNNING.min"
                  :max="TENANT_WORKER_MAX_RUNNING.max"
                  step="1"
                  required
                  @update:model-value="updateMaxRunning"
                />
              </label>
              <label class="form-group">
                <span class="form-label">优先级</span>
                <Input
                  :model-value="draft.priority"
                  type="number"
                  :min="TENANT_WORKER_PRIORITY.min"
                  :max="TENANT_WORKER_PRIORITY.max"
                  step="1"
                  required
                  @update:model-value="updatePriority"
                />
              </label>
            </div>
            <fieldset class="form-group">
              <legend class="form-label">任务类型</legend>
              <Checkbox
                v-for="task in taskTypes"
                :key="task"
                class="task-check"
                :model-value="draft.task_types.includes(task)"
                :label="task"
                @update:model-value="(checked) => updateTaskType(task, checked)"
              />
            </fieldset>
            <footer class="editor-footer">
              <Button variant="secondary" @click="resetDraft">取消修改</Button>
              <Button type="submit" :loading="saving" :disabled="!canSave">
                {{ saving ? '保存中...' : '保存 Worker' }}
              </Button>
            </footer>
          </form>
        </Card>
        <EmptyState
          v-else
          class="console-card empty-detail"
          title="选择或新增 Worker"
          description="Worker 配置按当前登录用户的个人 Tenant 隔离。"
        />
      </template>
    </MasterDetail>

    <ConfirmAction
      :open="removeOpen"
      title="删除 Worker"
      :description="removeDescription"
      confirm-label="确认删除"
      tone="danger"
      :loading="saving"
      @update:open="handleRemoveOpenChange"
      @confirm="removeWorker"
    />
  </article>
</template>

<script setup lang="ts">
import { computed, reactive, ref, watch } from 'vue'
import { useQuery, useQueryClient } from '@tanstack/vue-query'
import {
  createTenantWorker,
  deleteTenantWorker,
  isRevisionConflict,
  listTenantModels,
  listTenantWorkers,
  updateTenantWorker,
} from '@/api/client'
import type { TenantWorkerTaskType } from '@/api/types'
import { ConfirmAction, ErrorState, MasterDetail } from '@/components/patterns'
import {
  Alert,
  Badge,
  Button,
  Card,
  Checkbox,
  EmptyState,
  FormField,
  Input,
  Select,
  Skeleton,
  StatusBadge,
} from '@/components/ui'
import { useUIStore } from '@/stores/ui'
import {
  defaultTenantWorkerDraft,
  selectAfterWorkerListChange,
  TENANT_WORKER_MAX_RUNNING,
  TENANT_WORKER_PRIORITY,
  tenantWorkerDraft,
  tenantWorkerDraftIsValid,
  tenantWorkerStatusLabel,
} from './tenantWorkersModel'
import '@/design-system/configuration-workspace.css'
import './tenantWorkersPage.css'

const uiStore = useUIStore()
const queryClient = useQueryClient()
const taskTypes: TenantWorkerTaskType[] = ['reason', 'bootstrap', 'explore']
const selectedId = ref('')
const creating = ref(false)
const saving = ref(false)
const removeOpen = ref(false)
const revisionConflict = ref(false)
const draftRevision = ref<number | null>(null)
const draft = reactive(defaultTenantWorkerDraft())
const workersQuery = useQuery({ queryKey: ['tenant-workers'], queryFn: listTenantWorkers })
const modelsQuery = useQuery({ queryKey: ['tenant-models'], queryFn: listTenantModels })
const workers = computed(() => workersQuery.data.value || [])
const workersLoadError = computed(() => workersQuery.error.value)
const modelsLoadError = computed(() => modelsQuery.error.value)
const models = computed(() => modelsQuery.data.value || [])
const modelOptions = computed(() =>
  models.value.map((model) => ({
    value: model.id,
    label: `${model.display_name} · ${model.model_name || '未选择 Provider Model ID'} · ${model.base_url}`,
  })),
)
const currentWorker = computed(
  () => workers.value.find((worker) => worker.id === selectedId.value) || null,
)
const removeDescription = computed(() =>
  currentWorker.value
    ? `删除 Worker「${currentWorker.value.display_name}」？该操作无法撤销。`
    : '删除当前 Worker？该操作无法撤销。',
)
const legacyWorker = computed(() => !creating.value && currentWorker.value?.engine !== 'pi')
const canSave = computed(() => !legacyWorker.value && tenantWorkerDraftIsValid(draft))

watch(
  workers,
  (value) => {
    if (creating.value || revisionConflict.value) return
    const nextId = selectAfterWorkerListChange(value, selectedId.value)
    if (nextId !== selectedId.value) {
      selectedId.value = nextId
      loadDraft()
    }
  },
  { immediate: true },
)

function loadDraft() {
  if (!currentWorker.value) return
  Object.assign(draft, tenantWorkerDraft(currentWorker.value))
  draftRevision.value = currentWorker.value.revision
}

function resetDraft() {
  revisionConflict.value = false
  if (creating.value) Object.assign(draft, defaultTenantWorkerDraft(models.value[0]?.id || ''))
  else loadDraft()
}

function updateDisplayName(value: string | number | null) {
  draft.display_name = String(value ?? '').trim()
}

function updateMaxRunning(value: string | number | null) {
  if (typeof value === 'number') draft.max_running = value
}

function updatePriority(value: string | number | null) {
  if (typeof value === 'number') draft.priority = value
}

function updateTaskType(task: TenantWorkerTaskType, checked: boolean) {
  if (checked) {
    if (!draft.task_types.includes(task)) draft.task_types.push(task)
    return
  }
  draft.task_types = draft.task_types.filter((value) => value !== task)
}

function selectWorker(id: string) {
  if (saving.value) return
  selectedId.value = id
  creating.value = false
  revisionConflict.value = false
  loadDraft()
}

function startCreate() {
  if (saving.value) return
  creating.value = true
  selectedId.value = ''
  draftRevision.value = null
  revisionConflict.value = false
  Object.assign(draft, defaultTenantWorkerDraft(models.value[0]?.id || ''))
}

function errorText(error: unknown) {
  return error instanceof Error ? error.message : String(error)
}

async function refetchWorkers() {
  const result = await workersQuery.refetch()
  if (result.error) throw result.error
}

async function refreshDependentWorkerQueries() {
  await Promise.all([
    queryClient.invalidateQueries({ queryKey: ['tenant-worker-options'] }),
    queryClient.invalidateQueries({ queryKey: ['worker-templates'] }),
    queryClient.invalidateQueries({ queryKey: ['projects'] }),
  ])
}

async function handleMutationError(error: unknown) {
  if (isRevisionConflict(error)) {
    revisionConflict.value = true
    // 仅刷新服务端 revision，不覆盖用户尚未保存的草稿。
    try {
      await refetchWorkers()
    } catch {
      /* 保留原始冲突提示。 */
    }
  }
  uiStore.showToast(errorText(error), 'danger')
}

async function saveWorker() {
  if (!canSave.value) return
  saving.value = true
  revisionConflict.value = false
  try {
    const body = {
      display_name: draft.display_name,
      model_id: draft.model_id,
      task_types: [...draft.task_types],
      max_running: draft.max_running,
      priority: draft.priority,
      enabled: draft.enabled,
    }
    const saved = creating.value
      ? await createTenantWorker(body)
      : await updateTenantWorker(currentWorker.value!.id, {
          ...body,
          expected_revision: draftRevision.value!,
        })
    creating.value = false
    selectedId.value = saved.id
    Object.assign(draft, tenantWorkerDraft(saved))
    draftRevision.value = saved.revision
    await refetchWorkers()
    await refreshDependentWorkerQueries()
    uiStore.showToast('Worker 已保存', 'success')
  } catch (error) {
    await handleMutationError(error)
  } finally {
    saving.value = false
  }
}

function requestWorkerRemoval() {
  if (!currentWorker.value || saving.value) return
  removeOpen.value = true
}

function handleRemoveOpenChange(open: boolean) {
  if (!open && saving.value) return
  removeOpen.value = open
}

async function removeWorker() {
  if (!currentWorker.value || saving.value) return
  saving.value = true
  revisionConflict.value = false
  try {
    await deleteTenantWorker(
      currentWorker.value.id,
      draftRevision.value || currentWorker.value.revision,
    )
    await refetchWorkers()
    await refreshDependentWorkerQueries()
    selectedId.value = selectAfterWorkerListChange(workers.value, '')
    if (selectedId.value) loadDraft()
    uiStore.showToast('Worker 已删除', 'info')
  } catch (error) {
    await handleMutationError(error)
  } finally {
    saving.value = false
    removeOpen.value = false
  }
}
</script>
