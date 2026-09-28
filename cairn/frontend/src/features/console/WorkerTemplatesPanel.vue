<template>
  <section class="console-card form-partition tpl-list-panel">
    <div class="partition-header">
      <h2>Worker 模板</h2>
      <Button variant="secondary" size="sm" @click="emit('create')">
        <template #icon><Plus :size="14" /></template>
        新建模板
      </Button>
    </div>
    <p class="subtitle text-faint">
      保存一组 Tenant Worker ID，建项目时一键套用。模板与 Worker 均按当前登录 Tenant 隔离。
    </p>

    <Alert v-if="isLoadingTemplates" tone="info">
      <Spinner size="sm" decorative />
      正在加载 worker 模板...
    </Alert>
    <Alert v-else-if="isTemplatesError" tone="danger">
      加载 worker 模板失败：{{ errorMessage(templatesError) }}
    </Alert>
    <EmptyState v-else-if="templates.length === 0" title="暂无 Worker 模板">
      <template #actions>
        <Button size="sm" @click="emit('create')">新建模板</Button>
      </template>
    </EmptyState>

    <div v-else class="tpl-list">
      <div
        v-for="template in templates"
        :key="template.id"
        class="tpl-row"
        :class="{ active: selectedId === template.id }"
      >
        <button
          type="button"
          class="tpl-row-select"
          :aria-pressed="selectedId === template.id"
          @click="emit('select', template)"
        >
          <span class="tpl-row-head">
            <strong class="tpl-name">{{ template.name }}</strong>
            <span class="tpl-count text-faint">
              {{ template.tenant_worker_ids.length }} 个 worker
            </span>
          </span>
          <span v-if="template.tenant_worker_ids.length > 0" class="tpl-chips">
            <span
              v-for="workerId in template.tenant_worker_ids"
              :key="workerId"
              class="tpl-chip mono"
            >
              {{ workerLabel(workerId) }}
            </span>
          </span>
          <span v-else-if="template.worker_names.length" class="text-faint tpl-empty">
            {{ template.worker_names.join(' · ') }}（历史模板不可执行，请重新绑定 pi Worker）
          </span>
          <span v-else class="text-faint tpl-empty">（未绑定 worker）</span>
        </button>

        <div class="tpl-row-actions">
          <ConfirmAction
            :open="deleteTarget?.id === template.id"
            title="删除 Worker 模板"
            :description="`确认删除模板「${deleteTarget?.name || ''}」吗？`"
            confirm-label="确认删除"
            tone="danger"
            :loading="isDeleting"
            @update:open="handleDeleteOpenChange($event, template)"
            @confirm="deleteSelectedTemplate"
          >
            <template #trigger>
              <IconButton
                variant="danger"
                size="sm"
                label="删除模板"
                title="删除模板"
                :disabled="isDeleting"
                @click="deleteTarget = template"
              >
                <Trash2 :size="14" />
              </IconButton>
            </template>
          </ConfirmAction>
        </div>
      </div>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'
import { useQuery, useQueryClient } from '@tanstack/vue-query'
import { Plus, Trash2 } from '@lucide/vue'
import { deleteWorkerTemplate, listTenantWorkers, listWorkerTemplates } from '@/api/client'
import type { TenantWorker, WorkerTemplate } from '@/api/types'
import { ConfirmAction } from '@/components/patterns'
import { Alert, Button, EmptyState, IconButton, Spinner } from '@/components/ui'
import { useUIStore } from '@/stores/ui'
import { tenantWorkerUnavailableLabel } from '@/features/workers/tenantWorkersModel'
import './console.css'

const uiStore = useUIStore()
const queryClient = useQueryClient()

defineProps<{
  selectedId?: string | null
}>()

const emit = defineEmits<{
  (event: 'select', template: WorkerTemplate): void
  (event: 'create'): void
}>()

const {
  data: templatesData,
  isLoading: isLoadingTemplates,
  isError: isTemplatesError,
  error: templatesError,
} = useQuery<WorkerTemplate[]>({
  queryKey: ['worker-templates'],
  queryFn: listWorkerTemplates,
})
const { data: workersData } = useQuery<TenantWorker[]>({
  queryKey: ['tenant-workers'],
  queryFn: listTenantWorkers,
})

const templates = computed(() => templatesData.value || [])
const workersById = computed(
  () => new Map((workersData.value || []).map((worker) => [worker.id, worker])),
)
const workerLabel = (workerId: string) => {
  const worker = workersById.value.get(workerId)
  if (!worker) return workerId
  const label = `${worker.display_name} · ${worker.engine}`
  return worker.available
    ? label
    : `${label} · ${tenantWorkerUnavailableLabel(worker.unavailable_reason)}`
}
const deleteTarget = ref<WorkerTemplate | null>(null)
const isDeleting = ref(false)

const errorMessage = (error: unknown) => (error instanceof Error ? error.message : String(error))

function handleDeleteOpenChange(open: boolean, template: WorkerTemplate) {
  if (isDeleting.value) return
  deleteTarget.value = open ? template : null
}

async function deleteSelectedTemplate() {
  const target = deleteTarget.value
  if (!target || isDeleting.value) return

  isDeleting.value = true
  try {
    await deleteWorkerTemplate(target.id)
    uiStore.showToast('模板已删除', 'info')
    deleteTarget.value = null
    await queryClient.invalidateQueries({ queryKey: ['worker-templates'] })
  } catch (error: unknown) {
    uiStore.showToast(errorMessage(error) || '删除模板失败', 'danger')
  } finally {
    isDeleting.value = false
  }
}
</script>

<style scoped>
.tpl-list-panel {
  flex: 1 1 0;
  min-height: 0;
  padding: var(--space-3);
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.tpl-list {
  min-height: 0;
  margin-top: var(--space-3);
  padding-right: var(--space-1);
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  overflow-y: auto;
}

.tpl-row {
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  position: relative;
  background: var(--bg-base);
  transition:
    border-color var(--dur-fast),
    background var(--dur-fast);
}

.tpl-row:hover {
  border-color: var(--border-strong);
  background: var(--bg-elevated);
}

.tpl-row.active {
  border-color: var(--accent);
  background: var(--accent-dim);
}

.tpl-row-select {
  width: 100%;
  padding: var(--space-3) calc(var(--control-height-sm) + var(--space-4)) var(--space-3)
    var(--space-3);
  display: flex;
  flex-direction: column;
  align-items: stretch;
  color: inherit;
  text-align: left;
  cursor: pointer;
}

.tpl-row-select:focus-visible {
  border-radius: inherit;
  box-shadow: var(--ring);
}

.tpl-row-head {
  display: flex;
  align-items: center;
  gap: var(--space-2);
}

.tpl-name {
  font-size: var(--text-md);
}

.tpl-count,
.tpl-empty {
  font-size: var(--text-sm);
}

.tpl-row-actions {
  position: absolute;
  top: var(--space-2);
  right: var(--space-2);
}

.tpl-chips {
  margin-top: var(--space-2);
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-1);
}

.tpl-chip {
  border-radius: var(--radius-sm);
  padding: calc(var(--space-1) / 2) var(--space-2);
  background: var(--bg-inset);
  color: var(--text-muted);
  font-size: var(--text-sm);
}

.tpl-row.active .tpl-chip {
  background: var(--bg-elevated);
  color: var(--accent);
}

.tpl-empty {
  margin-top: var(--space-2);
}
</style>
