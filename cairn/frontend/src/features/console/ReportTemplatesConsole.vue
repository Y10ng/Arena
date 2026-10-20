<template>
  <article class="console-page report-templates-console">
    <Alert
      v-if="templatesQuery.isError.value && templates.length"
      tone="danger"
      title="模板目录同步失败"
    >
      当前为上次读取内容，已暂停保存。{{ errorMessage(templatesQuery.error.value) }}
      <Button variant="secondary" size="sm" :disabled="isBusy" @click="templatesQuery.refetch()"
        >重试</Button
      >
    </Alert>
    <Alert v-if="templatesQuery.isPending.value" tone="info">
      <Spinner size="sm" decorative />正在加载报告模板…
    </Alert>
    <Alert
      v-else-if="templatesQuery.isError.value && !templates.length"
      tone="danger"
      title="报告模板加载失败"
    >
      {{ errorMessage(templatesQuery.error.value) }}
      <Button variant="secondary" size="sm" @click="templatesQuery.refetch()">重试</Button>
    </Alert>
    <div v-else class="report-template-workspace" :class="{ 'is-reading': showEditor }">
      <aside class="report-template-directory" aria-label="模板目录">
        <header class="report-template-directory-heading">
          <h1>报告模板</h1>
          <Button size="sm" :disabled="isBusy" @click="openCreateTemplate"
            ><Plus :size="16" aria-hidden="true" />新建</Button
          >
        </header>
        <div class="report-template-list">
          <button
            v-for="template in templates"
            :key="template.key"
            type="button"
            class="report-template-item"
            :class="{ active: !creating && selectedTemplateKey === template.key }"
            :aria-current="!creating && selectedTemplateKey === template.key ? 'true' : undefined"
            :disabled="isBusy"
            @click="selectTemplate(template.key)"
          >
            <strong>{{ template.name }}</strong>
            <span v-if="!template.markdown_content" class="report-template-item-note"
              >待填写正文</span
            >
          </button>
          <EmptyState
            v-if="!templates.length"
            title="暂无报告模板"
            description="新建一个模板开始编辑。"
          />
        </div>
      </aside>
      <section class="report-template-detail" aria-label="模板工作区">
        <EmptyState
          v-if="!creating && !selectedTemplateKey"
          title="选择报告模板"
          description="选择模板或新建一项。"
        />
        <template v-else>
          <header class="report-template-heading">
            <Button
              class="report-template-back"
              variant="ghost"
              size="sm"
              :disabled="isBusy"
              @click="backToDirectory"
            >
              <ArrowLeft :size="16" aria-hidden="true" />返回模板列表
            </Button>
            <FormField
              v-slot="{ controlAttrs }"
              label="模板名称"
              required
              class="report-template-name"
            >
              <Input
                v-model="draft.name"
                v-bind="controlAttrs"
                placeholder="安全评估报告"
                :maxlength="256"
                :disabled="editorLocked"
              />
            </FormField>
            <details class="report-template-description">
              <summary>说明{{ draft.description ? '（已填写）' : '' }}</summary>
              <FormField v-slot="{ controlAttrs }" label="模板说明">
                <Textarea
                  v-model="draft.description"
                  v-bind="controlAttrs"
                  :rows="2"
                  :maxlength="2000"
                  :disabled="editorLocked"
                />
              </FormField>
            </details>
          </header>
          <div v-if="isLoadingTemplate" class="report-template-loading" role="status">
            <Spinner /><span>正在读取模板…</span>
          </div>
          <Alert
            v-else-if="readError"
            class="report-template-notice"
            tone="danger"
            title="模板读取失败"
          >
            {{ readError }}
            <Button variant="secondary" size="sm" :disabled="isBusy" @click="reloadTemplate"
              >重试读取</Button
            >
          </Alert>
          <Tabs
            v-else
            v-model="editorMode"
            class="report-template-editor"
            :options="editorOptions"
            :unmount-on-hide="false"
            variant="line"
          >
            <template #panel="{ option }">
              <MonacoEditor
                v-if="option.value === 'edit'"
                v-model="draft.markdown_content"
                language="markdown"
                aria-label="Markdown 模板"
                height="100%"
                :min-height="0"
                :disabled="editorLocked"
              />
              <div v-else class="report-template-preview">
                <SafeReportMarkdown :content="draft.markdown_content" />
              </div>
            </template>
          </Tabs>
          <footer class="report-template-save">
            <Alert v-if="writeError" tone="danger">
              {{ writeError }}
              <Button
                v-if="!creating"
                variant="secondary"
                size="sm"
                :disabled="isBusy"
                @click="reloadTemplate"
                >重新读取当前模板</Button
              >
            </Alert>
            <Alert v-if="draftLimitError" tone="danger">{{ draftLimitError }}</Alert>
            <div class="report-template-save-actions">
              <Button
                v-if="!creating"
                variant="danger"
                :disabled="isBusy || isLoadingTemplate || Boolean(readError)"
                @click="openDeleteTemplate"
                >删除模板</Button
              >
              <span class="report-template-dirty" role="status">{{
                isDirty ? '未保存更改' : ''
              }}</span>
              <Button variant="secondary" :disabled="!isDirty || editorLocked" @click="resetDraft"
                >重置</Button
              >
              <Button :loading="isSaving" :disabled="!canSave" @click="saveTemplate">{{
                creating ? '创建模板' : '保存'
              }}</Button>
            </div>
          </footer>
        </template>
      </section>
    </div>
    <ConfirmAction
      :open="Boolean(templateToDelete)"
      title="删除报告模板"
      :description="deleteDescription"
      confirm-label="确认删除"
      :loading="isDeleting"
      @update:open="handleDeleteOpenChange"
      @confirm="handleDeleteTemplate"
    />
  </article>
</template>

<script setup lang="ts">
import { computed, defineAsyncComponent, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import { onBeforeRouteLeave } from 'vue-router'
import { useQuery, useQueryClient } from '@tanstack/vue-query'
import { ArrowLeft, Plus } from '@lucide/vue'
import {
  ApiError,
  createReportTemplate,
  deleteReportTemplate,
  getReportTemplate,
  listReportTemplates,
  updateReportTemplate,
} from '@/api/client'
import type { ReportTemplate } from '@/api/types'
import { ConfirmAction } from '@/components/patterns'
import {
  Alert,
  Button,
  EmptyState,
  FormField,
  Input,
  Spinner,
  Tabs,
  Textarea,
} from '@/components/ui'
import SafeReportMarkdown from '@/features/reports/SafeReportMarkdown.vue'
import { useUIStore } from '@/stores/ui'
import './console.css'
import './report-template-console.css'

const MonacoEditor = defineAsyncComponent(() => import('@/components/MonacoEditor.vue'))
const uiStore = useUIStore()
const queryClient = useQueryClient()
const selectedTemplateKey = ref('')
const currentTemplate = ref<ReportTemplate | null>(null)
const creating = ref(false)
const draft = reactive({ name: '', description: '', markdown_content: '' })
const acceptedDraft = ref(JSON.stringify(draft))
const isLoadingTemplate = ref(false)
const readError = ref('')
const writeError = ref('')
const busyOperation = ref<'save' | 'delete' | null>(null)
const isBusy = computed(() => busyOperation.value !== null)
const isSaving = computed(() => busyOperation.value === 'save')
const isDeleting = computed(() => busyOperation.value === 'delete')
const templateToDelete = ref<ReportTemplate | null>(null)
const showEditor = ref(false)
const editorMode = ref('edit')
const editorOptions = [
  { value: 'edit', label: '正文编辑' },
  { value: 'preview', label: '模板文字预览' },
]
let active = true
let loadGeneration = 0
let operationGeneration = 0
let readAbort: AbortController | null = null
const errorMessage = (error: unknown) =>
  error instanceof Error && error.message ? error.message : '未知错误'
const mutationError = (action: string, error: unknown) =>
  error instanceof ApiError && error.status >= 400 && error.status < 500
    ? `${action}失败：${errorMessage(error)}。输入已保留。`
    : `${action}结果未确认：${errorMessage(error)}。输入已保留，请先核对当前模板与目录，再决定是否重新提交。`
const templatesQuery = useQuery<ReportTemplate[]>({
  queryKey: ['reportTemplates'],
  queryFn: ({ signal }) => listReportTemplates(signal),
})
const templates = computed(() => templatesQuery.data.value || [])
const isDirty = computed(() => JSON.stringify(draft) !== acceptedDraft.value)
const editorLocked = computed(
  () =>
    isBusy.value ||
    isLoadingTemplate.value ||
    Boolean(readError.value) ||
    templatesQuery.isError.value,
)
const draftLimitError = computed(() => {
  if (Array.from(draft.name.trim()).length > 256) return '名称不能超过 256 个字符。'
  if (Array.from(draft.description).length > 2000) return '说明不能超过 2000 个字符。'
  if (new TextEncoder().encode(draft.markdown_content).length > 500_000)
    return '模板正文不能超过 500000 个 UTF-8 字节。'
  return ''
})
const canSave = computed(
  () =>
    !editorLocked.value &&
    !draftLimitError.value &&
    Boolean(draft.name.trim() && draft.markdown_content.trim()) &&
    (creating.value || isDirty.value),
)
const deleteDescription = computed(
  () => `删除「${templateToDelete.value?.name || ''}」？已生成的报告和已受理任务不受影响。`,
)
const acceptDraft = (template?: ReportTemplate) => {
  Object.assign(draft, {
    name: template?.name || '',
    description: template?.description || '',
    markdown_content: template?.markdown_content || '',
  })
  acceptedDraft.value = JSON.stringify(draft)
}
const confirmDiscard = () =>
  !isBusy.value && (!isDirty.value || window.confirm('当前模板有未保存更改，是否放弃？'))
const cancelRead = () => {
  readAbort?.abort()
  loadGeneration += 1
  isLoadingTemplate.value = false
  readError.value = ''
}
const activateTemplate = async (key: string, reveal = true) => {
  cancelRead()
  creating.value = false
  selectedTemplateKey.value = key
  currentTemplate.value = null
  acceptDraft()
  writeError.value = ''
  editorMode.value = 'edit'
  showEditor.value = reveal
  if (!key) return
  const controller = new AbortController()
  readAbort = controller
  const generation = loadGeneration
  const isCurrent = () =>
    active && generation === loadGeneration && selectedTemplateKey.value === key && !creating.value
  isLoadingTemplate.value = true
  try {
    const detail = await getReportTemplate(key, controller.signal)
    if (!isCurrent()) return
    if (detail.key !== key) throw new Error('模板身份不匹配，已阻止载入。')
    currentTemplate.value = detail
    acceptDraft(detail)
  } catch (error) {
    if (isCurrent()) readError.value = errorMessage(error)
  } finally {
    if (isCurrent()) isLoadingTemplate.value = false
  }
}
const selectTemplate = (key: string) => {
  if (isBusy.value) return
  if (!creating.value && key === selectedTemplateKey.value) {
    showEditor.value = true
    return
  }
  if (confirmDiscard()) void activateTemplate(key)
}
const openCreateTemplate = () => {
  if (!confirmDiscard()) return
  cancelRead()
  creating.value = true
  selectedTemplateKey.value = ''
  currentTemplate.value = null
  writeError.value = ''
  editorMode.value = 'edit'
  showEditor.value = true
  acceptDraft()
}
const resetDraft = () => {
  if (editorLocked.value || !confirmDiscard()) return
  Object.assign(draft, JSON.parse(acceptedDraft.value))
  writeError.value = ''
}
const backToDirectory = () => {
  if (!confirmDiscard()) return
  Object.assign(draft, JSON.parse(acceptedDraft.value))
  showEditor.value = false
}
const reloadTemplate = () => {
  if (!creating.value && confirmDiscard()) void activateTemplate(selectedTemplateKey.value)
}
const syncDirectory = async () => {
  void queryClient.invalidateQueries({ queryKey: ['reportTemplateCatalog'] })
  await templatesQuery.refetch()
}
const saveTemplate = async () => {
  if (!canSave.value) return
  const key = selectedTemplateKey.value
  const wasCreating = creating.value
  const generation = ++operationGeneration
  const isCurrent = () =>
    active &&
    generation === operationGeneration &&
    selectedTemplateKey.value === key &&
    creating.value === wasCreating
  const body = {
    name: draft.name.trim(),
    description: draft.description.trim(),
    markdown_content: draft.markdown_content,
    ...(currentTemplate.value?.compatibility_profiles.length
      ? { compatibility_profiles: currentTemplate.value.compatibility_profiles }
      : {}),
  }
  busyOperation.value = 'save'
  writeError.value = ''
  try {
    const saved = wasCreating
      ? await createReportTemplate(body)
      : await updateReportTemplate(key, body)
    if (!isCurrent()) return
    if (!saved.key || (!wasCreating && saved.key !== key))
      throw new Error('保存响应的模板身份不匹配，请核对模板目录。')
    queryClient.setQueryData<ReportTemplate[]>(['reportTemplates'], (items = []) => [
      ...items.filter((item) => item.key !== saved.key),
      saved,
    ])
    await syncDirectory()
    if (!isCurrent()) return
    creating.value = false
    selectedTemplateKey.value = saved.key
    currentTemplate.value = saved
    acceptDraft(saved)
    uiStore.showToast(wasCreating ? '报告模板已创建' : '报告模板已保存', 'success')
    if (templatesQuery.isError.value)
      writeError.value = '保存已完成，但目录同步失败。请重试读取目录，勿重复提交。'
  } catch (error) {
    if (isCurrent()) writeError.value = mutationError(wasCreating ? '创建模板' : '保存模板', error)
  } finally {
    if (active && generation === operationGeneration) busyOperation.value = null
  }
}
const openDeleteTemplate = () => {
  if (currentTemplate.value && confirmDiscard()) templateToDelete.value = currentTemplate.value
}
const handleDeleteOpenChange = (open: boolean) => {
  if (!open && !isBusy.value) templateToDelete.value = null
}
const handleDeleteTemplate = async () => {
  const target = templateToDelete.value
  if (!target || isBusy.value || target.key !== selectedTemplateKey.value) return
  const generation = ++operationGeneration
  const isCurrent = () =>
    active && generation === operationGeneration && target.key === selectedTemplateKey.value
  busyOperation.value = 'delete'
  writeError.value = ''
  try {
    await deleteReportTemplate(target.key)
    if (!isCurrent()) return
    queryClient.setQueryData<ReportTemplate[]>(['reportTemplates'], (items = []) =>
      items.filter((item) => item.key !== target.key),
    )
    await syncDirectory()
    if (!isCurrent()) return
    templateToDelete.value = null
    await activateTemplate(templates.value[0]?.key || '', false)
    uiStore.showToast(`报告模板「${target.name}」已删除`, 'success')
    if (templatesQuery.isError.value)
      writeError.value = '删除已完成，但目录同步失败。请重试读取目录。'
  } catch (error) {
    if (isCurrent()) {
      templateToDelete.value = null
      writeError.value = mutationError('删除模板', error)
    }
  } finally {
    if (active && generation === operationGeneration) busyOperation.value = null
  }
}
watch(
  templates,
  (items) => {
    if (isBusy.value || isDirty.value || creating.value) return
    if (!items.some((item) => item.key === selectedTemplateKey.value))
      void activateTemplate(items[0]?.key || '', false)
  },
  { immediate: true },
)
const handleBeforeUnload = (event: BeforeUnloadEvent) => {
  if (!isBusy.value && !isDirty.value) return
  event.preventDefault()
  event.returnValue = ''
}
onBeforeRouteLeave(() => {
  if (isBusy.value) {
    uiStore.showToast('模板请求仍在处理中，请等待完成后再离开。', 'info')
    return false
  }
  return confirmDiscard()
})
onMounted(() => window.addEventListener('beforeunload', handleBeforeUnload))
onUnmounted(() => {
  active = false
  operationGeneration += 1
  cancelRead()
  window.removeEventListener('beforeunload', handleBeforeUnload)
})
</script>
