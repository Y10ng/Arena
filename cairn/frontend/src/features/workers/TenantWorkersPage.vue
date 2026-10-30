<template>
  <article class="console-page configuration-page tenant-workers-page">
    <div class="section-header">
      <h2>Worker 配置</h2>
      <Button v-if="activeTab === 'workers'" @click="modelsPanel?.startCreate()">新增模型</Button>
    </div>
    <component
      :is="authStore.isSuperAdmin ? Tabs : 'div'"
      v-bind="
        authStore.isSuperAdmin
          ? { modelValue: activeTab, options: tabs, activationMode: 'manual', unmountOnHide: false }
          : {}
      "
      @update:model-value="changeTab"
    >
      <template #[panelSlot]="panel">
        <section
          v-if="!authStore.isSuperAdmin || panel.option.value === 'workers'"
          class="worker-model-content"
        >
          <h3>普通 Worker</h3>
          <ErrorState
            v-if="workersLoadError"
            title="Worker 加载失败"
            :description="errorText(workersLoadError)"
            @retry="workersQuery.refetch()"
          />
          <ErrorState
            v-if="modelsLoadError"
            title="模型加载失败"
            :description="errorText(modelsLoadError)"
            @retry="modelsQuery.refetch()"
          />
          <Alert v-else-if="!modelsQuery.isPending.value && !models.length" tone="warning">
            尚未配置模型。<Button variant="ghost" size="sm" @click="modelsPanel?.startCreate()"
              >新增模型</Button
            >
          </Alert>
          <Skeleton v-if="workersQuery.isPending.value" label="正在加载 Worker" />
          <div v-else-if="workers.length" class="worker-table-container">
            <table class="worker-table">
              <thead>
                <tr>
                  <th scope="col">类型</th>
                  <th scope="col">模型</th>
                  <th scope="col">并发</th>
                  <th scope="col">状态</th>
                  <th scope="col">操作</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="worker in workers" :key="worker.id">
                  <th scope="row">
                    {{ roleLabels[worker.task_types[0]!]
                    }}<span class="worker-role-id">{{ worker.task_types[0] }}</span>
                  </th>
                  <td>
                    <Select
                      :model-value="drafts[worker.id]!.model_id ?? '__unconfigured__'"
                      :options="modelOptions"
                      :aria-label="`${worker.task_types[0]} 模型`"
                      :disabled="
                        savingId === worker.id ||
                        modelsQuery.isPending.value ||
                        Boolean(modelsLoadError)
                      "
                      @update:model-value="
                        drafts[worker.id]!.model_id =
                          $event === '__unconfigured__' ? null : String($event)
                      "
                    />
                  </td>
                  <td>
                    <Input
                      :model-value="drafts[worker.id]!.max_running"
                      type="number"
                      :min="TENANT_WORKER_MAX_RUNNING.min"
                      :max="TENANT_WORKER_MAX_RUNNING.max"
                      step="1"
                      :aria-label="`${worker.task_types[0]} 并发`"
                      :disabled="savingId === worker.id"
                      @update:model-value="
                        drafts[worker.id]!.max_running =
                          $event === null ? Number.NaN : Number($event)
                      "
                    />
                  </td>
                  <td>
                    <StatusBadge
                      :status="
                        worker.available && worker.executor_available ? 'healthy' : 'unhealthy'
                      "
                      size="sm"
                      >{{ tenantWorkerStatusLabel(worker) }}</StatusBadge
                    >
                  </td>
                  <td>
                    <div class="worker-actions">
                      <Button
                        size="sm"
                        :aria-label="`保存 ${worker.task_types[0]}`"
                        :loading="savingId === worker.id"
                        :disabled="
                          Boolean(savingId) || !tenantWorkerDraftIsValid(drafts[worker.id]!)
                        "
                        @click="saveWorker(worker)"
                        >保存</Button
                      >
                      <details class="worker-advanced">
                        <summary>高级设置</summary>
                        <Checkbox
                          v-model="drafts[worker.id]!.enabled"
                          label="启用"
                          :aria-label="`${worker.task_types[0]} 启用`"
                          :disabled="savingId === worker.id"
                        />
                        <label class="form-group"
                          ><span class="form-label">优先级</span
                          ><Input
                            :model-value="drafts[worker.id]!.priority"
                            type="number"
                            :min="TENANT_WORKER_PRIORITY.min"
                            :max="TENANT_WORKER_PRIORITY.max"
                            step="1"
                            :aria-label="`${worker.task_types[0]} 优先级`"
                            :disabled="savingId === worker.id"
                            @update:model-value="
                              drafts[worker.id]!.priority =
                                $event === null ? Number.NaN : Number($event)
                            "
                        /></label>
                      </details>
                    </div>
                    <p v-if="conflicts[worker.id]" class="worker-feedback" role="alert">
                      配置已被更新，草稿已保留。请核对后再次保存；刷新失败时不能重试覆盖。
                    </p>
                    <p v-if="errors[worker.id]" class="worker-feedback" role="alert">
                      {{ errors[worker.id] }}
                    </p>
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
          <Alert v-else-if="!workersLoadError" tone="warning"
            >未读取到固定 Worker 配置，请刷新或联系管理员检查初始化。</Alert
          >
          <p class="section-desc">
            并发为该 Worker
            跨项目的上限。保存影响后续新执行，降低并发不会停止在途执行；保存不代表模型连接已验证。
          </p>
          <div ref="modelsSection" id="worker-models"><TenantModelsPanel ref="modelsPanel" /></div>
        </section>
        <RuntimeSettingsPanel
          v-if="authStore.isSuperAdmin && panel.option.value === 'runtime' && runtimeVisited"
          ref="runtimePanel"
        />
      </template>
    </component>
    <ConfirmAction
      :open="leaveConfirmation"
      title="放弃未保存的配置？"
      description="离开后将丢弃尚未保存的 Worker 或运行设置，已保存的配置不受影响。"
      confirm-label="放弃修改"
      @confirm="resolveLeave(true)"
      @update:open="!$event && resolveLeave(false)"
    />
  </article>
</template>

<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { onBeforeRouteLeave, useRoute } from 'vue-router'
import { useQuery, useQueryClient } from '@tanstack/vue-query'
import {
  isRevisionConflict,
  listTenantModels,
  listTenantWorkers,
  updateTenantWorker,
} from '@/api/client'
import type { TenantWorker, UpdateTenantWorkerRequest } from '@/api/types'
import { ConfirmAction, ErrorState } from '@/components/patterns'
import {
  Alert,
  Button,
  Checkbox,
  Input,
  Select,
  Skeleton,
  StatusBadge,
  Tabs,
} from '@/components/ui'
import { useAuthStore } from '@/stores/auth'
import { useUIStore } from '@/stores/ui'
import TenantModelsPanel from '@/features/models/TenantModelsPanel.vue'
import RuntimeSettingsPanel from './RuntimeSettingsPanel.vue'
import {
  TENANT_WORKER_MAX_RUNNING,
  TENANT_WORKER_PRIORITY,
  tenantWorkerDraft,
  tenantWorkerDraftIsValid,
  tenantWorkerStatusLabel,
} from './tenantWorkersModel'
import '@/design-system/configuration-workspace.css'
import './tenantWorkersPage.css'

const authStore = useAuthStore()
const uiStore = useUIStore()
const route = useRoute()
const queryClient = useQueryClient()
const activeTab = ref('workers')
const runtimeVisited = ref(false)
// 普通用户直接展示同一份内容，不创建管理员页签或挂载运行设置。
const panelSlot = computed(() => (authStore.isSuperAdmin ? 'panel' : 'default'))
const tabs = [
  { value: 'workers', label: 'Worker 与模型' },
  { value: 'runtime', label: '运行设置' },
]
const modelsPanel = ref<InstanceType<typeof TenantModelsPanel> | null>(null)
const runtimePanel = ref<InstanceType<typeof RuntimeSettingsPanel> | null>(null)
const modelsSection = ref<HTMLElement | null>(null)
const savingId = ref('')
const drafts = reactive<Record<string, UpdateTenantWorkerRequest>>({})
const baselines = reactive<Record<string, string>>({})
const errors = reactive<Record<string, string>>({})
const conflicts = reactive<Record<string, boolean>>({})
const workersQuery = useQuery({ queryKey: ['tenant-workers'], queryFn: listTenantWorkers })
const modelsQuery = useQuery({ queryKey: ['tenant-models'], queryFn: listTenantModels })
const roles = ['bootstrap', 'reason', 'explore']
const roleLabels: Record<string, string> = { bootstrap: '初始化', reason: '决策', explore: '探索' }
const workers = computed(() =>
  [...(workersQuery.data.value || [])].sort(
    (a, b) => roles.indexOf(a.task_types[0]!) - roles.indexOf(b.task_types[0]!),
  ),
)
const workersLoadError = computed(() => workersQuery.error.value)
const modelsLoadError = computed(() => modelsQuery.error.value)
const models = computed(() => modelsQuery.data.value || [])
const modelOptions = computed(() => [
  { value: '__unconfigured__', label: '未配置模型' },
  ...models.value.map((model) => ({
    value: model.id,
    label: `${model.display_name} · ${model.model_name || '未选择模型'}`,
  })),
])
const workerDirty = computed(() =>
  Object.entries(drafts).some(([id, draft]) => JSON.stringify(draft) !== baselines[id]),
)
watch(
  workers,
  (items) => {
    // 后台刷新不覆盖其他行尚未保存的输入或编辑版本。
    for (const worker of items)
      if (!drafts[worker.id]) {
        drafts[worker.id] = tenantWorkerDraft(worker)
        baselines[worker.id] = JSON.stringify(drafts[worker.id])
      }
  },
  { immediate: true },
)
function errorText(error: unknown) {
  return error instanceof Error ? error.message : String(error)
}
async function saveWorker(worker: TenantWorker) {
  const draft = drafts[worker.id]!
  if (savingId.value || !tenantWorkerDraftIsValid(draft)) return
  savingId.value = worker.id
  errors[worker.id] = ''
  try {
    // 冲突后重新读取权威版本，保留输入；用户再次保存才确认覆盖。
    if (conflicts[worker.id]) {
      const result = await workersQuery.refetch()
      if (result.error) throw result.error
      const latest = result.data?.find((item) => item.id === worker.id)
      if (!latest) throw new Error('未读取到当前 Worker，请刷新后重试')
      draft.expected_revision = latest.revision
    }
    const saved = await updateTenantWorker(worker.id, { ...draft })
    drafts[worker.id] = tenantWorkerDraft(saved)
    baselines[worker.id] = JSON.stringify(drafts[worker.id])
    conflicts[worker.id] = false
    queryClient.setQueryData<TenantWorker[]>(['tenant-workers'], (current = []) =>
      current.map((item) => (item.id === saved.id ? saved : item)),
    )
    uiStore.showToast('Worker 配置已保存', 'success')
  } catch (error) {
    conflicts[worker.id] = conflicts[worker.id] || isRevisionConflict(error)
    errors[worker.id] = errorText(error)
  } finally {
    savingId.value = ''
  }
}
async function changeTab(tab: string) {
  if (tab === activeTab.value || (tab === 'runtime' && !authStore.isSuperAdmin)) return
  if (modelsPanel.value && !(await modelsPanel.value.requestLeave())) return
  if (tab === 'runtime') runtimeVisited.value = true
  activeTab.value = tab
}
watch(
  () => route.query.section,
  async (section) => {
    if (section !== 'models') return
    await changeTab('workers')
    await nextTick()
    modelsSection.value?.scrollIntoView({ block: 'start' })
  },
  { immediate: true },
)
const leaveConfirmation = ref(false)
let leaveResolver: ((leave: boolean) => void) | null = null
function resolveLeave(leave: boolean) {
  leaveConfirmation.value = false
  leaveResolver?.(leave)
  leaveResolver = null
}
onBeforeRouteLeave(async () => {
  if (savingId.value || runtimePanel.value?.saving) return false
  if (modelsPanel.value && !(await modelsPanel.value.requestLeave())) return false
  if (!workerDirty.value && !runtimePanel.value?.dirty) return true
  if (leaveResolver) return false
  leaveConfirmation.value = true
  return new Promise<boolean>((resolve) => {
    leaveResolver = resolve
  })
})
function beforeUnload(event: BeforeUnloadEvent) {
  if (
    workerDirty.value ||
    savingId.value ||
    modelsPanel.value?.dirty ||
    modelsPanel.value?.busy ||
    runtimePanel.value?.dirty ||
    runtimePanel.value?.saving
  )
    event.preventDefault()
}
onMounted(() => window.addEventListener('beforeunload', beforeUnload))
onBeforeUnmount(() => {
  window.removeEventListener('beforeunload', beforeUnload)
  resolveLeave(false)
})
</script>
