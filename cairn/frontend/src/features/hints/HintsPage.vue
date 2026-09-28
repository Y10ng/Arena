<template>
  <main class="hints-page">
    <Card class="hints-create-card" padding="md">
      <form class="hints-create-form" @submit.prevent="submitHint">
        <div class="hints-create-heading">
          <div>
            <h3>添加新提示</h3>
            <p>可绑定 Intent、Fact，或作为项目级提示加入后续工作空间上下文。</p>
          </div>
        </div>

        <div class="hints-anchor-fields">
          <FormField label="目标线索 Intent（可选，优先）">
            <template #default="{ controlAttrs }">
              <Select
                v-bind="controlAttrs"
                v-model="newHintIntentId"
                :options="intentOptions"
                :disabled="mutationPending"
                placeholder="不绑定具体线索"
              />
            </template>
          </FormField>

          <FormField v-if="!newHintIntentId" label="目标 Fact 节点（可选）">
            <template #default="{ controlAttrs }">
              <Select
                v-bind="controlAttrs"
                v-model="newHintFactId"
                :options="factOptions"
                :disabled="mutationPending"
                placeholder="项目级提示（不绑定节点）"
              />
            </template>
          </FormField>
        </div>

        <FormField
          label="提示内容"
          required
          hint="支持多行输入；按 Ctrl+Enter 或 Command+Enter 快速提交。"
        >
          <template #default="{ controlAttrs, invalid }">
            <Textarea
              v-bind="controlAttrs"
              v-model="newHintContent"
              :invalid="invalid"
              :disabled="mutationPending"
              :rows="4"
              placeholder="请输入指引提示内容..."
              @keydown.meta.enter.prevent="submitHint"
              @keydown.ctrl.enter.prevent="submitHint"
            />
          </template>
        </FormField>

        <div class="hints-create-actions">
          <Button
            type="submit"
            variant="primary"
            :loading="mutationPending"
            :disabled="!newHintContent.trim()"
          >
            提交提示
          </Button>
        </div>
      </form>
    </Card>

    <FilterBar label="提示列表状态" class="hints-status-bar">
      <strong>提示流 · 共 {{ sortedHints.length }} 条</strong>
      <template #status>随工作空间每 10 秒同步</template>
    </FilterBar>

    <EmptyState
      v-if="sortedHints.length === 0"
      title="暂无人工提示"
      description="添加提示后，Agent 可在后续工作空间上下文中参考这些信息。"
    />

    <DataTable
      v-else
      :rows="sortedHints"
      :columns="hintColumns"
      :row-key="hintRowKey"
      caption="项目人工提示列表"
      density="compact"
    >
      <template #cell-creator="{ row }">
        <div class="hint-meta-cell">
          <Badge :tone="hintCreatorMeta(row.creator).tone" size="sm">
            {{ hintCreatorMeta(row.creator).label }}
          </Badge>
          <code>#{{ row.id }}</code>
        </div>
      </template>
      <template #cell-anchor="{ row }">
        <Badge :tone="hintAnchorMeta(row).tone" size="sm">
          {{ hintAnchorMeta(row).label }}
        </Badge>
      </template>
      <template #cell-content="{ row }">
        <div class="hint-content">{{ row.content }}</div>
      </template>
      <template #cell-created="{ row }">
        <span class="hint-time" :title="row.created_at">
          {{ formatRelativeTime(row.created_at) }}
        </span>
      </template>
    </DataTable>
  </main>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { createProjectHint } from '@/api/client'
import type { Hint } from '@/api/types'
import { DataTable, FilterBar, type DataTableColumn } from '@/components/patterns'
import {
  Badge,
  Button,
  Card,
  EmptyState,
  FormField,
  Select,
  Textarea,
  type SelectOption,
} from '@/components/ui'
import { formatRelativeTime } from '@/lib/format'
import { useUIStore } from '@/stores/ui'
import { useWorkspaceStore } from '@/stores/workspace'
import {
  createHintPayload,
  hintAnchorMeta,
  hintCreatorMeta,
  mergeCreatedHint,
  middleHintFacts,
  sortHintsNewestFirst,
} from './hintsModel'
import './HintsPage.css'

const workspaceStore = useWorkspaceStore()
const uiStore = useUIStore()

const newHintContent = ref('')
const newHintFactId = ref('')
const newHintIntentId = ref('')
const pendingToken = ref<symbol | null>(null)
const mutationPending = computed(() => pendingToken.value !== null)
let projectVisitGeneration = 0

const currentProjectId = computed(() => workspaceStore.currentProjectId)
const hints = computed(() => workspaceStore.projectDetail?.hints ?? [])
const facts = computed(() => workspaceStore.projectDetail?.facts ?? [])
const intents = computed(() => workspaceStore.projectDetail?.intents ?? [])
const sortedHints = computed(() => sortHintsNewestFirst(hints.value))
const factOptions = computed<SelectOption[]>(() =>
  middleHintFacts(facts.value).map((fact) => ({
    value: fact.id,
    label: `${fact.id} · ${fact.description}`,
  })),
)
const intentOptions = computed<SelectOption[]>(() =>
  intents.value.map((intent) => ({
    value: intent.id,
    label: `${intent.id} · ${intent.description}`,
  })),
)

const hintColumns = [
  { key: 'creator', label: '创建者' },
  { key: 'anchor', label: '锚点' },
  { key: 'content', label: '提示内容' },
  { key: 'created', label: '创建时间' },
] satisfies readonly DataTableColumn<Hint, 'creator' | 'anchor' | 'content' | 'created'>[]

function hintRowKey(hint: Hint): string {
  return hint.id
}

function beginMutation(): symbol | null {
  if (mutationPending.value) return null
  const token = Symbol('hint-mutation')
  pendingToken.value = token
  return token
}

function finishMutation(token: symbol) {
  if (pendingToken.value === token) pendingToken.value = null
}

function isCurrentProjectVisit(projectId: string, generation: number): boolean {
  return currentProjectId.value === projectId && projectVisitGeneration === generation
}

function resetDraft() {
  newHintContent.value = ''
  newHintFactId.value = ''
  newHintIntentId.value = ''
}

async function submitHint() {
  const projectId = currentProjectId.value
  if (!projectId || !newHintContent.value.trim()) return
  const token = beginMutation()
  if (!token) return
  const visitGeneration = projectVisitGeneration
  const body = createHintPayload(newHintContent.value, newHintIntentId.value, newHintFactId.value)

  try {
    const created = await createProjectHint(projectId, body)
    if (!isCurrentProjectVisit(projectId, visitGeneration)) return

    workspaceStore.replaceProjectHints(projectId, mergeCreatedHint(hints.value, created))
    resetDraft()
    uiStore.showToast('指引提示已成功添加', 'success')
    void workspaceStore.loadProject(projectId)
  } catch (caught) {
    if (!isCurrentProjectVisit(projectId, visitGeneration)) return
    uiStore.showToast(
      caught instanceof Error && caught.message ? caught.message : '添加提示失败',
      'danger',
    )
  } finally {
    finishMutation(token)
  }
}

watch(currentProjectId, () => {
  projectVisitGeneration += 1
  resetDraft()
  pendingToken.value = null
})
</script>
