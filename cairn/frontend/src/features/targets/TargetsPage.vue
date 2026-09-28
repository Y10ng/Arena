<template>
  <div class="targets-page">
    <div class="targets-header targets-header--actions">
      <Button
        variant="primary"
        size="sm"
        :aria-expanded="isAddingInline"
        aria-controls="target-create-card"
        :disabled="mutationPending"
        @click="isAddingInline = !isAddingInline"
      >
        手动添加目标
      </Button>
    </div>

    <Card v-if="isAddingInline" id="target-create-card" class="target-create-card" padding="md">
      <form class="target-create-form" @submit.prevent="saveNewTarget">
        <h3>手动定义授权探索目标</h3>
        <div class="target-create-fields">
          <FormField label="目标类别" required>
            <template #default="{ controlAttrs, invalid }">
              <Select
                v-model="newTarget.type"
                v-bind="controlAttrs"
                :invalid="invalid"
                :options="TARGET_TYPE_OPTIONS"
                :disabled="mutationPending"
              />
            </template>
          </FormField>
          <FormField label="目标值 (Value)" required class="target-value-field">
            <template #default="{ controlAttrs, invalid }">
              <Input
                v-model="newTarget.value"
                v-bind="controlAttrs"
                class="target-value-input"
                :invalid="invalid"
                :disabled="mutationPending"
                placeholder="例如: 192.168.1.100 或 intranet.internal"
              />
            </template>
          </FormField>
        </div>
        <div class="target-create-actions">
          <Button variant="secondary" size="sm" :disabled="mutationPending" @click="cancelCreate">
            取消
          </Button>
          <Button
            type="submit"
            variant="success"
            size="sm"
            :loading="mutationPending"
            :disabled="!newTarget.value.trim()"
          >
            添加并批准
          </Button>
        </div>
      </form>
    </Card>

    <Card
      v-if="pendingDiscoveredTargets.length"
      class="targets-review-card"
      padding="sm"
      role="status"
    >
      <div class="targets-review-copy">
        <strong>发现 {{ pendingDiscoveredTargets.length }} 个新资产目标等待授权</strong>
        <span>批准后 Agent 将自动把它们纳入自治分析范围。</span>
      </div>
      <div class="targets-review-actions">
        <Button
          variant="success"
          size="sm"
          :disabled="mutationPending"
          @click="batchUpdateDiscovered('approved')"
        >
          全部批准
        </Button>
        <Button
          variant="warning"
          size="sm"
          :disabled="mutationPending"
          @click="batchUpdateDiscovered('rejected')"
        >
          全部搁置
        </Button>
      </div>
    </Card>

    <FilterBar label="目标筛选" class="targets-filter-bar">
      <FormField label="状态">
        <template #default="{ controlAttrs }">
          <Select
            v-model="activeStatus"
            v-bind="controlAttrs"
            :options="statusFilterOptions"
            :disabled="mutationPending"
          />
        </template>
      </FormField>
      <FormField label="搜索目标值" class="targets-search-field">
        <template #default="{ controlAttrs }">
          <Input v-model="searchQuery" v-bind="controlAttrs" placeholder="过滤目标值..." />
        </template>
      </FormField>

      <template v-if="isFetching && hasQueryData" #status>同步中…</template>
      <template #actions>
        <Button
          variant="secondary"
          size="sm"
          :loading="isFetching"
          :disabled="!currentProjectId"
          @click="refetch()"
        >
          刷新
        </Button>
        <Button
          variant="secondary"
          size="sm"
          :disabled="!filteredTargets.length || mutationPending"
          :aria-pressed="allFilteredSelected"
          @click="toggleSelectFiltered"
        >
          {{ allFilteredSelected ? '取消选择当前筛选结果' : '选择当前筛选结果' }}
        </Button>
      </template>
    </FilterBar>

    <div class="targets-counts" aria-label="目标状态统计">
      <span v-for="option in statusFilterOptions" :key="option.value">
        {{ option.label }}
        <strong>{{ statusCount(option.value) }}</strong>
      </span>
    </div>

    <Card
      v-if="selectedIds.size"
      class="targets-bulk-bar"
      padding="sm"
      aria-label="已选择目标的批量操作"
    >
      <strong>已选择 {{ selectedIds.size }} 项</strong>
      <div class="targets-bulk-actions">
        <Button
          variant="success"
          size="sm"
          :disabled="mutationPending"
          @click="bulkActionUpdate('approved')"
        >
          批量批准
        </Button>
        <Button
          variant="danger"
          size="sm"
          :disabled="mutationPending"
          @click="bulkActionUpdate('rejected')"
        >
          批量拒绝
        </Button>
        <Button variant="secondary" size="sm" :disabled="mutationPending" @click="clearSelection">
          取消选择
        </Button>
      </div>
    </Card>

    <div
      v-if="isLoading && !hasQueryData"
      class="targets-skeleton"
      role="status"
      aria-label="正在加载目标"
    >
      <Card v-for="index in 4" :key="index" padding="sm">
        <Skeleton shape="text" />
      </Card>
    </div>

    <ErrorState
      v-else-if="isError && !hasQueryData"
      title="目标加载失败"
      :description="errorMessage"
      @retry="refetch()"
    />

    <EmptyState
      v-else-if="hasQueryData && !targets.length"
      title="暂无目标"
      description="手动添加目标，或等待 Worker 在探索过程中发现新资产。"
    />

    <template v-else-if="targets.length">
      <ErrorState
        v-if="isError"
        class="targets-background-error"
        title="目标同步失败"
        :description="errorMessage"
        @retry="refetch()"
      />

      <EmptyState
        v-if="!filteredTargets.length"
        title="没有符合筛选条件的目标"
        description="请调整状态或目标值搜索条件。"
      />

      <DataTable
        v-else
        :rows="filteredTargets"
        :columns="targetColumns"
        :row-key="targetRowKey"
        caption="项目目标范围列表"
        density="compact"
        :is-row-active="isTargetSelected"
        active-row-aria="selected"
      >
        <template #cell-selection="{ row }">
          <Checkbox
            class="target-checkbox-label"
            :model-value="selectedIds.has(row.id)"
            :disabled="mutationPending"
            :aria-label="`选择目标 ${row.value}`"
            @update:modelValue="toggleSelectRow(row.id)"
          />
        </template>
        <template #cell-value="{ row }">
          <span class="target-value" :title="row.value">{{ row.value }}</span>
        </template>
        <template #cell-type="{ row }">
          <Badge :tone="targetTypeMeta(row.type).tone" size="sm">
            {{ targetTypeMeta(row.type).label }}
          </Badge>
        </template>
        <template #cell-status="{ row }">
          <Badge :tone="targetStatusMeta(row.status).tone" size="sm">
            {{ targetStatusMeta(row.status).label }}
          </Badge>
        </template>
        <template #cell-fact="{ row }">
          <Button
            v-if="row.source_fact_id"
            variant="link"
            size="sm"
            class="target-context-link"
            @click="goToFact(row.source_fact_id)"
          >
            #{{ row.source_fact_id }}
          </Button>
          <span v-else class="target-muted">手动/Seed</span>
        </template>
        <template #cell-run="{ row }">
          <Button
            v-if="row.discovered_by_run_id"
            variant="link"
            size="sm"
            class="target-context-link"
            @click="goToRun(row.discovered_by_run_id)"
          >
            run_{{ row.discovered_by_run_id.slice(0, 8) }}
          </Button>
          <span v-else class="target-muted">—</span>
        </template>
        <template #cell-created="{ row }">
          <span class="target-time">{{ formatRelativeTime(row.created_at) }}</span>
        </template>
        <template #cell-actions="{ row }">
          <div class="target-row-actions">
            <Button
              v-if="row.status !== 'approved'"
              variant="link"
              size="sm"
              :disabled="mutationPending"
              :aria-label="`批准目标 ${row.value}`"
              @click="updateStatus(row.id, 'approved')"
            >
              批准
            </Button>
            <Button
              v-if="row.status !== 'rejected'"
              variant="link"
              size="sm"
              :disabled="mutationPending"
              :aria-label="`拒绝目标 ${row.value}`"
              @click="updateStatus(row.id, 'rejected')"
            >
              拒绝
            </Button>
          </div>
        </template>
      </DataTable>
    </template>
  </div>
</template>

<script setup lang="ts">
import { computed, reactive, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { useQuery, useQueryClient } from '@tanstack/vue-query'
import { createProjectTarget, listProjectTargets, updateProjectTarget } from '@/api/client'
import type { Target, TargetStatus, TargetType } from '@/api/types'
import { DataTable, ErrorState, FilterBar, type DataTableColumn } from '@/components/patterns'
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
} from '@/components/ui'
import { formatRelativeTime } from '@/lib/format'
import { useUIStore } from '@/stores/ui'
import { useWorkspaceStore } from '@/stores/workspace'
import {
  TARGET_STATUS_FILTER_OPTIONS,
  TARGET_TYPE_OPTIONS,
  countTargetsByStatus,
  filterAndSortTargets,
  reconcileSelectedTargetIds,
  targetStatusMeta,
  targetTypeMeta,
  type TargetStatusFilter,
} from './targetsModel'

const router = useRouter()
const workspaceStore = useWorkspaceStore()
const uiStore = useUIStore()
const queryClient = useQueryClient()

const currentProjectId = computed(() => workspaceStore.currentProjectId)
const searchQuery = ref('')
const activeStatus = ref<TargetStatusFilter>('all')
const isAddingInline = ref(false)
const selectedIds = ref(new Set<string>())
const pendingToken = ref<symbol | null>(null)
const mutationPending = computed(() => pendingToken.value !== null)
let projectVisitGeneration = 0

const newTarget = reactive<{ value: string; type: TargetType }>({
  value: '',
  type: 'ip',
})

const {
  data: targetsData,
  error,
  isError,
  isFetching,
  isLoading,
  refetch,
} = useQuery<Target[]>({
  queryKey: ['workspaceTargets', currentProjectId],
  queryFn: () => {
    const projectId = currentProjectId.value
    return projectId ? listProjectTargets(projectId) : Promise.resolve([])
  },
  enabled: computed(() => Boolean(currentProjectId.value)),
})

const hasQueryData = computed(() => targetsData.value !== undefined)
const targets = computed(() => targetsData.value || [])
const filteredTargets = computed(() =>
  filterAndSortTargets(targets.value, searchQuery.value, activeStatus.value),
)
const targetCounts = computed(() => countTargetsByStatus(targets.value))
const pendingDiscoveredTargets = computed(() =>
  targets.value.filter((target) => target.status === 'discovered'),
)
const allFilteredSelected = computed(
  () =>
    filteredTargets.value.length > 0 &&
    filteredTargets.value.every((target) => selectedIds.value.has(target.id)),
)
const errorMessage = computed(() =>
  error.value instanceof Error && error.value.message ? error.value.message : '请稍后重试。',
)

const statusFilterOptions = TARGET_STATUS_FILTER_OPTIONS.map((option) => ({
  value: option.value,
  label: option.label,
}))

const targetColumns = [
  { key: 'selection', label: '选择' },
  { key: 'value', label: '资产目标值 (Value)' },
  { key: 'type', label: '类型' },
  { key: 'status', label: '当前状态' },
  { key: 'fact', label: '发现来源 Fact' },
  { key: 'run', label: '触发的任务 Run' },
  { key: 'created', label: '建立时间' },
  { key: 'actions', label: '探索审计操作', align: 'end' },
] satisfies readonly DataTableColumn<
  Target,
  'selection' | 'value' | 'type' | 'status' | 'fact' | 'run' | 'created' | 'actions'
>[]

function statusCount(status: TargetStatusFilter): number {
  return status === 'all' ? targets.value.length : targetCounts.value[status]
}

function targetRowKey(target: Target): string {
  return target.id
}

function isTargetSelected(target: Target): boolean {
  return selectedIds.value.has(target.id)
}

function toggleSelectFiltered() {
  const next = new Set(selectedIds.value)
  for (const target of filteredTargets.value) {
    if (allFilteredSelected.value) next.delete(target.id)
    else next.add(target.id)
  }
  selectedIds.value = next
}

function toggleSelectRow(targetId: string) {
  const next = new Set(selectedIds.value)
  if (next.has(targetId)) next.delete(targetId)
  else next.add(targetId)
  selectedIds.value = next
}

function clearSelection() {
  selectedIds.value = new Set()
}

function cancelCreate() {
  isAddingInline.value = false
}

function beginMutation(): symbol | null {
  if (mutationPending.value) return null
  const token = Symbol('target-mutation')
  pendingToken.value = token
  return token
}

function finishMutation(token: symbol) {
  if (pendingToken.value === token) pendingToken.value = null
}

function isCurrentProjectVisit(projectId: string, generation: number): boolean {
  return currentProjectId.value === projectId && projectVisitGeneration === generation
}

function replaceCachedTargets(projectId: string, nextTargets: Target[]) {
  queryClient.setQueryData<Target[]>(['workspaceTargets', projectId], nextTargets)
  workspaceStore.replaceProjectTargets(projectId, nextTargets)
}

function refreshProjectTargets(projectId: string) {
  void queryClient
    .invalidateQueries({ queryKey: ['workspaceTargets', projectId] })
    .catch(() => undefined)
}

function applyReturnedTargets(
  projectId: string,
  returnedTargets: readonly Target[],
  append = false,
) {
  const cached = queryClient.getQueryData<Target[]>(['workspaceTargets', projectId])
  const base = cached ?? (currentProjectId.value === projectId ? targets.value : [])
  const returnedById = new Map(returnedTargets.map((target) => [target.id, target]))
  const next = base.map((target) => returnedById.get(target.id) ?? target)
  const existingIds = new Set(base.map((target) => target.id))
  const additions = returnedTargets.filter((target) => !existingIds.has(target.id))
  replaceCachedTargets(projectId, append ? [...additions, ...next] : [...next, ...additions])
}

async function saveNewTarget() {
  const projectId = currentProjectId.value
  const value = newTarget.value.trim()
  if (!projectId || !value) return
  const token = beginMutation()
  if (!token) return
  const visitGeneration = projectVisitGeneration

  try {
    const created = await createProjectTarget(projectId, {
      value,
      type: newTarget.type,
      status: 'approved',
    })
    if (isCurrentProjectVisit(projectId, visitGeneration)) {
      applyReturnedTargets(projectId, [created], true)
      newTarget.value = ''
      isAddingInline.value = false
      uiStore.showToast('手动目标范围添加成功！', 'success')
    }
    refreshProjectTargets(projectId)
  } catch (caught) {
    if (isCurrentProjectVisit(projectId, visitGeneration)) {
      uiStore.showToast(
        caught instanceof Error && caught.message ? caught.message : '添加目标失败',
        'danger',
      )
    }
  } finally {
    finishMutation(token)
  }
}

async function updateStatus(
  targetId: string,
  status: Extract<TargetStatus, 'approved' | 'rejected'>,
) {
  const projectId = currentProjectId.value
  if (!projectId) return
  const token = beginMutation()
  if (!token) return
  const visitGeneration = projectVisitGeneration

  try {
    const updated = await updateProjectTarget(projectId, targetId, { status })
    if (isCurrentProjectVisit(projectId, visitGeneration)) {
      applyReturnedTargets(projectId, [updated])
      uiStore.showToast(status === 'approved' ? '目标已批准探索' : '目标已成功拒绝', 'success')
    }
    refreshProjectTargets(projectId)
  } catch (caught) {
    if (isCurrentProjectVisit(projectId, visitGeneration)) {
      uiStore.showToast(
        caught instanceof Error && caught.message ? caught.message : '更新状态失败',
        'danger',
      )
    }
  } finally {
    finishMutation(token)
  }
}

async function updateManyTargets(
  projectId: string,
  targetIds: readonly string[],
  status: Extract<TargetStatus, 'approved' | 'rejected'>,
  source: 'discovered' | 'selected',
) {
  if (!targetIds.length) return
  const token = beginMutation()
  if (!token) return
  const visitGeneration = projectVisitGeneration

  try {
    const settled = await Promise.allSettled(
      targetIds.map((targetId) => updateProjectTarget(projectId, targetId, { status })),
    )
    const succeeded: Target[] = []
    const succeededIds = new Set<string>()
    let failedCount = 0

    settled.forEach((result, index) => {
      if (result.status === 'fulfilled') {
        succeeded.push(result.value)
        succeededIds.add(targetIds[index]!)
      } else {
        failedCount += 1
      }
    })

    if (succeeded.length && isCurrentProjectVisit(projectId, visitGeneration)) {
      applyReturnedTargets(projectId, succeeded)
    }
    refreshProjectTargets(projectId)

    if (isCurrentProjectVisit(projectId, visitGeneration)) {
      if (source === 'selected' && succeededIds.size) {
        selectedIds.value = new Set(
          [...selectedIds.value].filter((targetId) => !succeededIds.has(targetId)),
        )
      }

      if (!failedCount) {
        uiStore.showToast(
          source === 'discovered' ? '全部发现目标处理成功' : '批量更新状态成功',
          'success',
        )
      } else if (!succeeded.length) {
        uiStore.showToast('批量操作全部失败，请重试', 'danger')
      } else {
        uiStore.showToast(
          `批量操作部分成功：成功 ${succeeded.length} 项，失败 ${failedCount} 项`,
          'warn',
        )
      }
    }
  } finally {
    finishMutation(token)
  }
}

function batchUpdateDiscovered(status: Extract<TargetStatus, 'approved' | 'rejected'>) {
  const projectId = currentProjectId.value
  if (!projectId) return
  const targetIds = pendingDiscoveredTargets.value.map((target) => target.id)
  return updateManyTargets(projectId, targetIds, status, 'discovered')
}

function bulkActionUpdate(status: Extract<TargetStatus, 'approved' | 'rejected'>) {
  const projectId = currentProjectId.value
  if (!projectId) return
  const validIds = new Set(targets.value.map((target) => target.id))
  const targetIds = [...selectedIds.value].filter((targetId) => validIds.has(targetId))
  return updateManyTargets(projectId, targetIds, status, 'selected')
}

function goToFact(factId: string) {
  const projectId = currentProjectId.value
  if (!projectId) return
  router.push({ path: `/projects/${projectId}`, query: { nodeId: factId } })
}

function goToRun(runId: string) {
  const projectId = currentProjectId.value
  if (!projectId) return
  router.push({ path: `/projects/${projectId}/runs`, query: { runId } })
}

watch(currentProjectId, () => {
  projectVisitGeneration += 1
  searchQuery.value = ''
  activeStatus.value = 'all'
  selectedIds.value = new Set()
  isAddingInline.value = false
  newTarget.value = ''
  newTarget.type = 'ip'
  pendingToken.value = null
})

watch(
  targetsData,
  (data) => {
    const projectId = currentProjectId.value
    if (!projectId || data === undefined) return
    workspaceStore.replaceProjectTargets(projectId, data)
    selectedIds.value = reconcileSelectedTargetIds(selectedIds.value, data)
  },
  { immediate: true },
)

watch(
  () => uiStore.globalRefreshKey,
  () => {
    if (currentProjectId.value) refetch()
  },
)
</script>

<style src="./TargetsPage.css" scoped></style>
