<template>
  <article class="console-page console-editor-page report-templates-console">
    <section class="console-card form-partition">
      <Alert v-if="templatesQuery.isPending.value" class="state-banner" tone="info">
        <Spinner size="sm" decorative /> 正在加载报告模板…
      </Alert>
      <Alert v-else-if="templatesQuery.isError.value" class="state-banner" tone="danger">
        加载报告模板失败：{{ errorMessage(templatesQuery.error.value) }}
      </Alert>
      <MasterDetail v-else>
        <template #master>
          <form class="report-template-create" @submit.prevent="handleCreateTemplate">
            <FormField v-slot="{ controlAttrs }" label="模板键" required>
              <Input
                v-model="createDraft.key"
                v-bind="controlAttrs"
                class="mono"
                placeholder="security-report"
                :disabled="isCreatingTemplate"
              />
            </FormField>
            <FormField v-slot="{ controlAttrs }" label="名称" required>
              <Input
                v-model="createDraft.name"
                v-bind="controlAttrs"
                placeholder="安全评估报告"
                :disabled="isCreatingTemplate"
              />
            </FormField>
            <FormField v-slot="{ controlAttrs }" label="描述">
              <Textarea
                v-model="createDraft.description"
                v-bind="controlAttrs"
                :rows="2"
                :disabled="isCreatingTemplate"
              />
            </FormField>
            <Button
              type="submit"
              block
              :loading="isCreatingTemplate"
              :disabled="!canCreateTemplate"
            >
              创建模板
            </Button>
          </form>

          <div class="master-subhead">模板</div>
          <div class="master-list report-template-list">
            <button
              v-for="template in templates"
              :key="template.key"
              type="button"
              class="knowledge-set-item"
              :class="{ active: selectedTemplateKey === template.key }"
              @click="selectTemplate(template.key)"
            >
              <span class="knowledge-set-main">
                <strong>{{ template.name }}</strong>
                <span class="mono text-faint">{{ template.key }}</span>
              </span>
              <span class="item-badges">
                <Badge v-if="template.archived_at" tone="neutral" size="sm">已归档</Badge>
                <Badge v-else tone="success" size="sm">可用</Badge>
              </span>
            </button>
            <EmptyState
              v-if="templates.length === 0"
              title="暂无报告模板"
              description="创建模板后发布第一个不可变版本。"
            />
          </div>
        </template>

        <template #detail>
          <EmptyState
            v-if="!selectedTemplate"
            title="选择报告模板"
            description="选择左侧模板以查看版本并创建新版本。"
          />
          <div v-else class="report-template-detail">
            <div class="report-template-heading">
              <div>
                <h2>{{ selectedTemplate.name }}</h2>
                <p>{{ selectedTemplate.description || '无描述' }}</p>
              </div>
              <Button
                variant="danger"
                :disabled="Boolean(selectedTemplate.archived_at)"
                @click="openArchiveTemplate"
              >
                归档模板
              </Button>
            </div>

            <Alert v-if="selectedTemplate.archived_at" tone="warning" title="此模板已归档">
              历史版本仍可审阅，但不能再发布新版本，也不会出现在项目模板目录中。
            </Alert>

            <div class="report-template-version-layout">
              <aside class="report-template-versions" aria-label="模板版本">
                <div class="master-subhead">不可变版本</div>
                <Alert v-if="versionsQuery.isPending.value" tone="info">
                  <Spinner size="sm" decorative /> 正在加载版本…
                </Alert>
                <Alert v-else-if="versionsQuery.isError.value" tone="danger">
                  加载版本失败：{{ errorMessage(versionsQuery.error.value) }}
                </Alert>
                <button
                  v-for="version in versions"
                  v-else
                  :key="version.version"
                  type="button"
                  class="report-template-version-item"
                  :class="{ active: selectedVersion === version.version }"
                  @click="selectVersion(version.version)"
                >
                  <strong>v{{ version.version }}</strong>
                  <span class="mono">{{ version.digest_sha256.slice(0, 12) }}…</span>
                  <small>{{ version.message || '无版本说明' }}</small>
                </button>
                <span
                  v-if="!versionsQuery.isPending.value && versions.length === 0"
                  class="text-faint"
                >
                  尚未发布版本
                </span>
              </aside>

              <section class="report-template-editor">
                <div class="report-template-provenance">
                  <span>草稿将发布为 v{{ nextVersion }}</span>
                  <span v-if="selectedVersionDetail">
                    基于 v{{ selectedVersionDetail.version }} ·
                    <code>{{ selectedVersionDetail.digest_sha256 }}</code>
                  </span>
                </div>
                <FormField v-slot="{ controlAttrs }" label="版本说明">
                  <Input
                    v-model="versionMessage"
                    v-bind="controlAttrs"
                    placeholder="说明本次模板调整"
                    :disabled="Boolean(selectedTemplate.archived_at) || isPublishingVersion"
                  />
                </FormField>
                <TextEditor
                  v-model="markdownDraft"
                  label="Markdown 模板"
                  file-name="report-template.md"
                  placeholder="# 最终安全评估报告"
                  :min-height="480"
                  :disabled="Boolean(selectedTemplate.archived_at) || isLoadingVersion"
                />
                <Alert v-if="versionLoadError" tone="danger">{{ versionLoadError }}</Alert>
                <footer class="editor-footer">
                  <Button variant="secondary" :disabled="!isDirty" @click="resetDraft">
                    重置
                  </Button>
                  <Button
                    variant="success"
                    :loading="isPublishingVersion"
                    :disabled="!canPublishVersion"
                    @click="publishVersion"
                  >
                    发布不可变版本 v{{ nextVersion }}
                  </Button>
                </footer>
              </section>
            </div>
          </div>
        </template>
      </MasterDetail>
    </section>

    <ConfirmAction
      :open="Boolean(templateToArchive)"
      title="归档报告模板"
      :description="archiveDescription"
      confirm-label="确认归档"
      :loading="isArchivingTemplate"
      @update:open="handleArchiveOpenChange"
      @confirm="handleArchiveTemplate"
    />
  </article>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import { onBeforeRouteLeave } from 'vue-router'
import { useQuery } from '@tanstack/vue-query'

import {
  archiveReportTemplate,
  createReportTemplate,
  createReportTemplateVersion,
  getReportTemplateVersion,
  listReportTemplates,
  listReportTemplateVersions,
} from '@/api/client'
import type {
  ReportTemplate,
  ReportTemplateVersion,
  ReportTemplateVersionDetail,
} from '@/api/types'
import { ConfirmAction, MasterDetail, TextEditor } from '@/components/patterns'
import {
  Alert,
  Badge,
  Button,
  EmptyState,
  FormField,
  Input,
  Spinner,
  Textarea,
} from '@/components/ui'
import { useUIStore } from '@/stores/ui'
import './console.css'
import './report-template-console.css'

const uiStore = useUIStore()
const selectedTemplateKey = ref('')
const selectedVersion = ref<number | null>(null)
const selectedVersionDetail = ref<ReportTemplateVersionDetail | null>(null)
const markdownDraft = ref('')
const versionMessage = ref('')
const acceptedDraft = ref(JSON.stringify({ markdown: '', message: '' }))
const isLoadingVersion = ref(false)
const versionLoadError = ref('')
const isCreatingTemplate = ref(false)
const isPublishingVersion = ref(false)
const templateToArchive = ref<ReportTemplate | null>(null)
const isArchivingTemplate = ref(false)
let versionLoadGeneration = 0

const createDraft = reactive({ key: '', name: '', description: '' })
const errorMessage = (error: unknown) =>
  error instanceof Error && error.message ? error.message : '未知错误'

const templatesQuery = useQuery<ReportTemplate[]>({
  queryKey: ['reportTemplates'],
  queryFn: ({ signal }) => listReportTemplates(signal),
})
const templates = computed(() => templatesQuery.data.value || [])
const selectedTemplate = computed(
  () => templates.value.find((template) => template.key === selectedTemplateKey.value) || null,
)
const versionsQuery = useQuery<ReportTemplateVersion[]>({
  queryKey: computed(() => ['reportTemplateVersions', selectedTemplateKey.value]),
  queryFn: ({ signal }) => listReportTemplateVersions(selectedTemplateKey.value, signal),
  enabled: computed(() => Boolean(selectedTemplateKey.value)),
})
const versions = computed(() => versionsQuery.data.value || [])
const nextVersion = computed(() => (versions.value[0]?.version || 0) + 1)
const currentDraft = computed(() =>
  JSON.stringify({ markdown: markdownDraft.value, message: versionMessage.value }),
)
const isDirty = computed(() => currentDraft.value !== acceptedDraft.value)
const canCreateTemplate = computed(
  () =>
    !isCreatingTemplate.value &&
    /^[a-z0-9_-]{1,128}$/.test(createDraft.key.trim()) &&
    Boolean(createDraft.name.trim()),
)
const canPublishVersion = computed(
  () =>
    Boolean(selectedTemplate.value) &&
    !selectedTemplate.value?.archived_at &&
    !isPublishingVersion.value &&
    Boolean(markdownDraft.value.trim()),
)
const archiveDescription = computed(() =>
  templateToArchive.value
    ? `归档「${templateToArchive.value.name}」后，项目不能再选择它生成报告；历史版本仍会保留。`
    : '归档后历史版本仍会保留。',
)

const acceptDraft = (markdown: string, message = '') => {
  markdownDraft.value = markdown
  versionMessage.value = message
  acceptedDraft.value = JSON.stringify({ markdown, message })
}
const confirmDiscard = () =>
  !isDirty.value || window.confirm('当前模板草稿尚未发布，是否放弃这些更改？')

const selectTemplate = async (key: string) => {
  if (key === selectedTemplateKey.value || !confirmDiscard()) return
  selectedTemplateKey.value = key
  selectedVersion.value = null
  selectedVersionDetail.value = null
  acceptDraft('')
}

const selectVersion = async (version: number, force = false) => {
  if (!force && version === selectedVersion.value) return
  if (!force && !confirmDiscard()) return
  const key = selectedTemplateKey.value
  const generation = ++versionLoadGeneration
  selectedVersion.value = version
  isLoadingVersion.value = true
  versionLoadError.value = ''
  try {
    const detail = await getReportTemplateVersion(key, version)
    if (generation !== versionLoadGeneration || selectedTemplateKey.value !== key) return
    selectedVersionDetail.value = detail
    acceptDraft(detail.markdown_content)
  } catch (error) {
    if (generation !== versionLoadGeneration || selectedTemplateKey.value !== key) return
    versionLoadError.value = errorMessage(error)
  } finally {
    if (generation === versionLoadGeneration) isLoadingVersion.value = false
  }
}

const resetDraft = () => {
  const accepted = JSON.parse(acceptedDraft.value) as { markdown: string; message: string }
  markdownDraft.value = accepted.markdown
  versionMessage.value = accepted.message
}

const handleCreateTemplate = async () => {
  if (!canCreateTemplate.value || !confirmDiscard()) return
  resetDraft()
  const body = {
    key: createDraft.key.trim(),
    name: createDraft.name.trim(),
    description: createDraft.description.trim(),
  }
  isCreatingTemplate.value = true
  try {
    const created = await createReportTemplate(body)
    createDraft.key = ''
    createDraft.name = ''
    createDraft.description = ''
    await templatesQuery.refetch()
    selectedTemplateKey.value = created.key
    uiStore.showToast(`报告模板「${created.name}」已创建`, 'success')
  } catch (error) {
    uiStore.showToast(`创建报告模板失败：${errorMessage(error)}`, 'danger')
  } finally {
    isCreatingTemplate.value = false
  }
}

const publishVersion = async () => {
  if (!canPublishVersion.value || !selectedTemplate.value) return
  const key = selectedTemplate.value.key
  const payload = {
    markdown_content: markdownDraft.value,
    message: versionMessage.value.trim() || null,
  }
  isPublishingVersion.value = true
  try {
    const created = await createReportTemplateVersion(key, payload)
    if (selectedTemplateKey.value !== key) return
    await Promise.all([versionsQuery.refetch(), templatesQuery.refetch()])
    selectedVersion.value = created.version
    selectedVersionDetail.value = created
    acceptDraft(created.markdown_content)
    uiStore.showToast(`已发布 ${key} v${created.version}`, 'success')
  } catch (error) {
    uiStore.showToast(`发布报告模板版本失败：${errorMessage(error)}`, 'danger')
  } finally {
    isPublishingVersion.value = false
  }
}

const openArchiveTemplate = () => {
  if (!selectedTemplate.value || !confirmDiscard()) return
  resetDraft()
  templateToArchive.value = selectedTemplate.value
}
const handleArchiveOpenChange = (open: boolean) => {
  if (!open && !isArchivingTemplate.value) templateToArchive.value = null
}
const handleArchiveTemplate = async () => {
  const target = templateToArchive.value
  if (!target || isArchivingTemplate.value) return
  isArchivingTemplate.value = true
  try {
    await archiveReportTemplate(target.key)
    templateToArchive.value = null
    await templatesQuery.refetch()
    uiStore.showToast(`报告模板「${target.name}」已归档`, 'success')
  } catch (error) {
    uiStore.showToast(`归档报告模板失败：${errorMessage(error)}`, 'danger')
  } finally {
    isArchivingTemplate.value = false
  }
}

watch(
  templates,
  (items) => {
    if (!items.length) {
      selectedTemplateKey.value = ''
      return
    }
    if (!items.some((item) => item.key === selectedTemplateKey.value)) {
      selectedTemplateKey.value = items[0]?.key || ''
    }
  },
  { immediate: true },
)
watch(versions, (items) => {
  if (!items.length || selectedVersion.value !== null) return
  const latest = items[0]
  if (latest) void selectVersion(latest.version, true)
})
watch(selectedTemplateKey, () => {
  versionLoadGeneration += 1
  selectedVersion.value = null
  selectedVersionDetail.value = null
  acceptDraft('')
})

const handleBeforeUnload = (event: BeforeUnloadEvent) => {
  if (!isDirty.value) return
  event.preventDefault()
  event.returnValue = ''
}
onBeforeRouteLeave(() => confirmDiscard())
onMounted(() => window.addEventListener('beforeunload', handleBeforeUnload))
onUnmounted(() => window.removeEventListener('beforeunload', handleBeforeUnload))
</script>
