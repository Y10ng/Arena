<template>
  <main class="hints-page">
    <section class="hints-history" aria-labelledby="hints-history-title">
      <header class="hints-history-header">
        <div class="hints-history-heading">
          <h2 id="hints-history-title">提示记录</h2>
          <span>{{ sortedHints.length }} 条</span>
        </div>
      </header>
      <EmptyState
        v-if="sortedHints.length === 0"
        title="暂无人工提示"
      />
      <ol v-else class="hints-records" aria-label="项目人工提示记录">
        <li v-for="hint in sortedHints" :key="hint.id">
          <article class="hint-record" :aria-label="`提示 ${hint.id}`">
            <header class="hint-record-header">
              <Badge :tone="hintCreatorMeta(hint.creator).tone" size="sm">
                {{ hintCreatorMeta(hint.creator).label }}
              </Badge>
              <time class="hint-time" :datetime="hint.created_at" :title="hint.created_at">
                {{ formatRelativeTime(hint.created_at) }}
              </time>
            </header>
            <div class="hint-content">{{ hint.content }}</div>
            <footer class="hint-record-footer">
              <span class="hint-anchor">{{ hintAnchorMeta(hint).label }}</span>
              <code>#{{ hint.id }}</code>
            </footer>
          </article>
        </li>
      </ol>
    </section>

    <aside class="hints-compose" aria-labelledby="hints-create-title">
      <form class="hints-create-form" @submit.prevent="submitHint">
        <header class="hints-create-heading">
          <h2 id="hints-create-title">添加提示</h2>
        </header>

        <FormField
          label="提示内容"
          required
        >
          <template #default="{ controlAttrs, invalid }">
            <Textarea
              v-bind="controlAttrs"
              v-model="newHintContent"
              :invalid="invalid"
              :disabled="mutationPending"
              :rows="8"
              placeholder="请输入指引提示内容..."
              @keydown.meta.enter.prevent="submitHint"
              @keydown.ctrl.enter.prevent="submitHint"
            />
          </template>
        </FormField>

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
    </aside>
  </main>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { createProjectHint } from '@/api/client'
import {
  Badge,
  Button,
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
