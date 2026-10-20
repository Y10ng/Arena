<template>
  <div
    class="artifacts-page"
    :class="{
      'artifacts-page--report': selectedArtifactIsReportMarkdown,
      'artifacts-page--previewing': Boolean(selectedArtifact) && !directoryVisible,
    }"
  >
    <Card v-show="!isReportReading" ref="fileList" class="artifacts-list-card" padding="md">
      <FilterBar label="产物筛选" class="artifacts-filter-bar">
        <Tabs v-model="viewMode" :options="viewOptions" variant="pill" />
        <FormField label="搜索文件名" class="artifacts-search-field">
          <template #default="{ controlAttrs }">
            <Input
              v-model="filenameSearch"
              v-bind="controlAttrs"
              placeholder="按文件名筛选…"
              :disabled="isLoading && !hasQueryData"
              @keydown.esc="filenameSearch = ''"
            />
          </template>
        </FormField>
        <FormField label="文件类别" class="artifacts-kind-field">
          <template #default="{ controlAttrs }">
            <Select
              v-model="kindFilter"
              v-bind="controlAttrs"
              :options="kindOptions"
              :disabled="isLoading && !hasQueryData"
            />
          </template>
        </FormField>
        <template v-if="isFetching && hasQueryData" #status>同步中…</template>
        <template #actions>
          <Button
            variant="secondary"
            size="sm"
            :loading="isFetching"
            :disabled="!currentProjectId"
            @click="refreshArtifacts"
          >
            刷新
          </Button>
        </template>
      </FilterBar>

      <div
        v-if="isLoading && !hasQueryData"
        class="artifacts-list-skeleton"
        role="status"
        aria-label="正在加载项目产物"
      >
        <Skeleton v-for="index in 4" :key="index" shape="text" />
      </div>

      <ErrorState
        v-else-if="isError && !hasQueryData"
        title="产物加载失败"
        :description="listErrorMessage"
        @retry="refreshArtifacts"
      />

      <EmptyState
        v-else-if="hasQueryData && !artifacts.length"
        title="暂无项目产物"
        description="Worker 产生报告、截图、日志或数据文件后会显示在这里。"
      />

      <template v-else-if="artifacts.length">
        <ErrorState
          v-if="isError"
          class="artifacts-background-error"
          title="产物同步失败"
          :description="listErrorMessage"
          @retry="refreshArtifacts"
        />

        <EmptyState
          v-if="!filteredArtifacts.length"
          title="没有符合当前筛选条件的产物"
          description="请调整产物视图、文件类别或文件名搜索条件。"
        />

        <DataTable
          v-else
          :rows="filteredArtifacts"
          :columns="artifactColumns"
          :row-key="artifactRowKey"
          :is-row-active="isArtifactSelected"
          active-row-aria="current"
          caption="项目产物列表"
          density="compact"
        >
          <template #cell-file="{ row }">
            <span class="artifact-file-name" :title="row.uri">{{ artifactFilename(row) }}</span>
            <span class="artifact-file-secondary">
              {{ artifactKindLabel(row.kind) }} · {{ bytes(row.size) }}
              <span v-if="row.kind_deprecated"> · 历史类别</span>
            </span>
          </template>
          <template #cell-actions="{ row }">
            <Button
              variant="link"
              size="sm"
              :aria-label="`预览产物 ${artifactFilename(row)}`"
              @click="selectArtifact(row)"
            >
              {{ selectedArtifactId === row.id ? '已打开' : '预览' }}
            </Button>
          </template>
        </DataTable>
      </template>
    </Card>

    <ReportReader
      v-if="selectedArtifact && selectedArtifactIsReportMarkdown"
      v-show="isReportReading"
      v-model:mode="reportMode"
      class="artifact-report-reader"
      :identity="artifactRowKey(selectedArtifact)"
      :content="textPreviewContent"
      :ready="hasTextPreview && !isPreviewLoading"
    >
      <template #title>
        <div class="artifact-report-title">
          <Button variant="ghost" size="sm" @click="returnToFiles"
            ><ArrowLeft :size="16" aria-hidden="true" />返回文件</Button
          >
          <h2 :title="selectedArtifact.uri">{{ artifactFilename(selectedArtifact) }}</h2>
        </div>
      </template>
      <template #actions>
        <a class="artifact-download-link" :href="artifactContentUrl(selectedArtifact)" download
          >下载完整报告</a
        >
        <Popover align="end" size="md">
          <template #trigger><Button variant="ghost" size="sm">文件信息</Button></template>
          <dl class="artifact-file-info">
            <dt>大小</dt>
            <dd>{{ bytes(selectedArtifact.size) }}</dd>
            <dt>生成于</dt>
            <dd>{{ formatRelativeTime(selectedArtifact.created_at) }}</dd>
            <dt>SHA256</dt>
            <dd>
              {{ selectedArtifact.sha256
              }}<Button variant="link" size="sm" @click="copySha">复制 HASH</Button>
            </dd>
            <template v-if="selectedArtifact.task_run_id"
              ><dt>运行</dt>
              <dd>
                <Button variant="link" size="sm" @click="goToRun(selectedArtifact.task_run_id)"
                  >查看运行</Button
                >
              </dd></template
            >
          </dl>
        </Popover>
      </template>
      <template #notice>
        <Alert
          v-if="previewData?.truncated"
          class="artifact-preview-alert"
          tone="info"
          :title="previewWindow === 'head' ? '当前仅显示报告开头' : '当前仅显示报告尾部'"
        >
          已读取 {{ bytes(previewData.bytes_read) }}，原文件共
          {{ bytes(previewData.size) }}。目录仅覆盖已载入部分，下载可获取完整报告。
          <template #actions
            ><Button variant="secondary" size="sm" @click="togglePreviewWindow">{{
              previewWindow === 'head' ? '查看文件尾部' : '查看文件开头'
            }}</Button></template
          >
        </Alert>
        <Alert
          v-if="selectedArtifact.kind_deprecated"
          class="artifact-preview-alert"
          tone="warning"
          title="历史兼容类别"
          >{{ artifactDeprecationMessage(selectedArtifact) }}</Alert
        >
      </template>
      <template #state>
        <div v-if="isPreviewLoading" class="artifact-preview-loading" role="status">
          <Spinner /><span>正在载入报告…</span>
        </div>
        <ErrorState
          v-else
          title="报告预览载入失败"
          :description="previewError || '预览尚未载入，请重试或下载原文件。'"
          @retry="retryPreview"
        />
      </template>
    </ReportReader>

    <Card v-else class="artifact-preview-card" padding="none">
      <EmptyState
        v-if="!selectedArtifact"
        class="artifact-preview-empty"
        :title="invalidArtifactNotice ? '指定产物不存在或已删除' : '选择一个文件开始审阅'"
        :description="
          invalidArtifactNotice
            ? '当前项目中找不到深链指定的产物，已移除无效选择。'
            : '支持预览标准输出、报告、补丁、HAR 和截图；二进制产物可下载后分析。'
        "
      />

      <template v-else>
        <header class="artifact-preview-header">
          <Button class="artifact-files-back" variant="ghost" size="sm" @click="returnToFiles"
            ><ArrowLeft :size="16" aria-hidden="true" />返回文件</Button
          >
          <div class="artifact-preview-title">
            <h2 :title="selectedArtifact.uri">{{ artifactFilename(selectedArtifact) }}</h2>
          </div>
          <Popover align="end" size="md">
            <template #trigger><Button variant="ghost" size="sm">文件信息</Button></template>
            <dl class="artifact-file-info">
              <dt>大小</dt>
              <dd>{{ bytes(selectedArtifact.size) }}</dd>
              <dt>生成于</dt>
              <dd>{{ selectedArtifact.created_at }}</dd>
              <dt>SHA256</dt>
              <dd>
                {{ selectedArtifact.sha256
                }}<Button variant="link" size="sm" @click="copySha">复制 HASH</Button>
              </dd>
              <template v-if="selectedArtifact.task_run_id"
                ><dt>运行</dt>
                <dd>
                  <Button variant="link" size="sm" @click="goToRun(selectedArtifact.task_run_id)"
                    >查看运行</Button
                  >
                </dd></template
              >
            </dl>
          </Popover>
          <a
            class="artifact-download-link"
            :href="artifactContentUrl(selectedArtifact)"
            download
            title="下载完整原文件"
          >
            下载原文件
          </a>
        </header>

        <Alert
          v-if="selectedArtifact.kind_deprecated"
          class="artifact-preview-alert"
          tone="warning"
          title="历史兼容类别"
        >
          {{ artifactDeprecationMessage(selectedArtifact) }}
        </Alert>

        <Alert
          v-if="previewData?.truncated"
          class="artifact-preview-alert artifact-window-alert"
          tone="info"
          :title="previewWindow === 'tail' ? '当前显示文件尾部窗口' : '当前显示文件开头窗口'"
        >
          已读取 {{ bytes(previewData.bytes_read) }}，原文件共 {{ bytes(previewData.size) }}。
          {{ previewWindow === 'head' ? '这不是完整文件。' : '下载原文件可获取完整服务器产物。' }}
          <template #actions>
            <Button variant="secondary" size="sm" @click="togglePreviewWindow">
              {{ previewWindow === 'tail' ? '查看文件开头' : '查看文件尾部' }}
            </Button>
          </template>
        </Alert>

        <div class="artifact-preview-body">
          <div v-if="isPreviewLoading" class="artifact-preview-loading" role="status">
            <Spinner />
            <span>正在载入预览内容…</span>
          </div>

          <template v-else-if="artifactIsScreenshot(selectedArtifact)">
            <div v-if="!screenshotFailed" class="artifact-image-preview">
              <img
                :key="screenshotIdentity"
                :src="artifactContentUrl(selectedArtifact)"
                :alt="`产物截图：${artifactFilename(selectedArtifact)}`"
                class="artifact-screenshot"
                @error="handleScreenshotError"
              />
            </div>
            <EmptyState
              v-else
              title="截图载入失败"
              description="可以下载原文件后使用本地图片查看器审阅。"
            >
              <template #actions>
                <a
                  class="artifact-download-link"
                  :href="artifactContentUrl(selectedArtifact)"
                  download
                >
                  下载截图原文件
                </a>
              </template>
            </EmptyState>
          </template>

          <div v-else-if="hasTextPreview" class="artifact-text-preview">
            <MarkdownPreview v-if="selectedArtifactIsMarkdown" :content="textPreviewContent" />
            <MonacoEditor
              v-else
              :model-value="textPreviewContent"
              :language="selectedArtifactLanguage"
              read-only
              height="100%"
              :min-height="0"
              :word-wrap="artifactWordWrap"
              :aria-label="`产物 ${artifactFilename(selectedArtifact)} 的文本预览`"
            />
          </div>

          <EmptyState
            v-else-if="artifactIsTextPreviewable(selectedArtifact)"
            title="无法载入文本预览"
            :description="previewError || '该文件支持文本预览，但内容暂时没有载入成功。'"
          >
            <template #actions>
              <Button size="sm" @click="retryPreview">重试预览</Button>
              <a
                class="artifact-download-link"
                :href="artifactContentUrl(selectedArtifact)"
                download
              >
                下载原文件
              </a>
            </template>
          </EmptyState>

          <EmptyState
            v-else
            title="该类型不支持在线文本预览"
            :description="`文件大小 ${bytes(selectedArtifact.size)}。请下载后使用对应的本地工具分析。`"
          >
            <template #actions>
              <a
                class="artifact-download-link"
                :href="artifactContentUrl(selectedArtifact)"
                download
              >
                下载原文件
              </a>
            </template>
          </EmptyState>
        </div>
      </template>
    </Card>
  </div>
</template>

<script setup lang="ts">
import { computed, defineAsyncComponent, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useQuery } from '@tanstack/vue-query'
import {
  fetchArtifactPreview,
  fetchArtifactTailPreview,
  listProjectArtifacts,
  type ArtifactPreview,
  type ProjectArtifact,
} from '@/api/client'
import MarkdownPreview from '@/components/MarkdownPreview.vue'
import { DataTable, ErrorState, FilterBar, type DataTableColumn } from '@/components/patterns'
import { ArrowLeft } from '@lucide/vue'
import ReportReader from '@/features/reports/ReportReader.vue'
import {
  Alert,
  Button,
  Card,
  Popover,
  EmptyState,
  FormField,
  Input,
  Select,
  Skeleton,
  Spinner,
  Tabs,
  type TabsOption,
} from '@/components/ui'
import { fileExtension, isMarkdownFile, resolveFileLanguage } from '@/lib/fileLanguage'
import { bytes, formatRelativeTime } from '@/lib/format'
import { useUIStore } from '@/stores/ui'
import { useWorkspaceStore } from '@/stores/workspace'
import {
  artifactFilename,
  artifactIsScreenshot,
  artifactIsTextPreviewable,
  artifactKindLabel,
  filterAndSortArtifacts,
  kindOptionsForArtifacts,
  viewModeForArtifact,
  type ArtifactKindFilter,
  type ArtifactPreviewWindow,
  type ArtifactViewMode,
} from './artifactsModel'

const MonacoEditor = defineAsyncComponent(() => import('@/components/MonacoEditor.vue'))

const route = useRoute()
const router = useRouter()
const workspaceStore = useWorkspaceStore()
const uiStore = useUIStore()
const currentProjectId = computed(() => workspaceStore.currentProjectId)

const viewOptions = [
  { value: 'deliverable', label: '交付产物' },
  { value: 'diagnostic', label: '诊断日志' },
] satisfies readonly TabsOption[]
const viewMode = ref<ArtifactViewMode>('deliverable')
const kindFilter = ref<ArtifactKindFilter>('all')
const filenameSearch = ref('')
const selectedArtifactId = ref<string | null>(null)
const invalidArtifactNotice = ref(false)
const directoryVisible = ref(false)
const fileList = ref<InstanceType<typeof Card> | null>(null)
const reportMode = ref<'rendered' | 'raw'>('rendered')
let previewAbort: AbortController | null = null
const previewWindow = ref<ArtifactPreviewWindow>('tail')
const previewData = ref<ArtifactPreview | null>(null)
const previewError = ref('')
const isPreviewLoading = ref(false)
const screenshotFailedIdentity = ref<string | null>(null)
let previewRequestGeneration = 0
let projectVisitGeneration = 0
let mounted = true
let preserveInvalidNoticeForNextQueryClear = false

const {
  data: artifactsData,
  error,
  isError,
  isFetching,
  isLoading,
  refetch,
} = useQuery<ProjectArtifact[]>({
  queryKey: ['workspaceArtifacts', currentProjectId],
  queryFn: () => {
    const projectId = currentProjectId.value
    return projectId ? listProjectArtifacts(projectId) : Promise.resolve([])
  },
  enabled: computed(() => Boolean(currentProjectId.value)),
})

const hasQueryData = computed(() => artifactsData.value !== undefined)
const artifacts = computed(() => artifactsData.value ?? [])
const kindOptions = computed(() => kindOptionsForArtifacts(artifacts.value, viewMode.value))
const filteredArtifacts = computed(() =>
  filterAndSortArtifacts(artifacts.value, viewMode.value, kindFilter.value, filenameSearch.value),
)
const selectedArtifact = computed(() => {
  const projectId = currentProjectId.value
  if (!projectId || !selectedArtifactId.value) return null
  return (
    artifacts.value.find(
      (artifact) => artifact.id === selectedArtifactId.value && artifact.project_id === projectId,
    ) ?? null
  )
})
const listErrorMessage = computed(() =>
  error.value instanceof Error && error.value.message ? error.value.message : '请稍后重试。',
)
const selectedArtifactLanguage = computed(() =>
  resolveFileLanguage(selectedArtifact.value?.uri ?? '', selectedArtifact.value?.kind ?? ''),
)
const selectedArtifactIsMarkdown = computed(() =>
  isMarkdownFile(selectedArtifact.value?.uri ?? '', selectedArtifact.value?.kind ?? ''),
)
const selectedArtifactIsReportMarkdown = computed(() => {
  const artifact = selectedArtifact.value
  if (artifact?.kind !== 'report') return false
  return ['md', 'markdown'].includes(fileExtension(artifact.uri))
})
const isReportReading = computed(
  () => selectedArtifactIsReportMarkdown.value && !directoryVisible.value,
)
const artifactWordWrap = computed<'on' | 'off'>(() =>
  selectedArtifact.value?.kind === 'raw_output' ? 'off' : 'on',
)
const hasTextPreview = computed(() => {
  const artifact = selectedArtifact.value
  const preview = previewData.value
  return Boolean(
    artifact &&
    preview &&
    preview.artifact_id === artifact.id &&
    preview.project_id === currentProjectId.value &&
    typeof preview.content === 'string',
  )
})
const textPreviewContent = computed(() =>
  hasTextPreview.value ? (previewData.value?.content ?? '') : '',
)
const screenshotIdentity = computed(() => {
  const artifact = selectedArtifact.value
  return artifact ? `${projectVisitGeneration}:${artifact.project_id}:${artifact.id}` : ''
})
const screenshotFailed = computed(
  () =>
    Boolean(screenshotIdentity.value) &&
    screenshotFailedIdentity.value === screenshotIdentity.value,
)

const artifactColumns = [
  { key: 'file', label: '文件' },
  { key: 'actions', label: '操作', align: 'end' },
] satisfies readonly DataTableColumn<ProjectArtifact, 'file' | 'actions'>[]

function artifactRowKey(artifact: ProjectArtifact): string {
  return `${artifact.project_id}:${artifact.id}`
}

function isArtifactSelected(artifact: ProjectArtifact): boolean {
  return selectedArtifactId.value === artifact.id && currentProjectId.value === artifact.project_id
}

function artifactContentUrl(artifact: ProjectArtifact): string {
  return `/projects/${encodeURIComponent(artifact.project_id)}/artifacts/${encodeURIComponent(artifact.id)}/content`
}

function artifactDeprecationMessage(artifact: ProjectArtifact): string {
  return (
    artifact.deprecation_message || '冻结兼容：当前阶段不再新建或采集此类产物，仅保留历史读取。'
  )
}

function queryArtifactId(): string | null {
  const value = route.query.artifactId
  return typeof value === 'string' && value.trim() ? value : null
}

function invalidatePreview() {
  previewRequestGeneration += 1
  previewAbort?.abort()
  previewAbort = null
  previewData.value = null
  previewError.value = ''
  isPreviewLoading.value = false
  screenshotFailedIdentity.value = null
}

function clearSelection(resetNotice = true) {
  selectedArtifactId.value = null
  previewWindow.value = 'tail'
  invalidatePreview()
  if (resetNotice) invalidArtifactNotice.value = false
}

function isCurrentPreviewSource(
  requestGeneration: number,
  projectId: string,
  projectVisit: number,
  artifactId: string,
  window: ArtifactPreviewWindow,
): boolean {
  return (
    mounted &&
    previewRequestGeneration === requestGeneration &&
    currentProjectId.value === projectId &&
    projectVisitGeneration === projectVisit &&
    selectedArtifactId.value === artifactId &&
    previewWindow.value === window
  )
}

async function loadPreview(artifact: ProjectArtifact, window = previewWindow.value) {
  if (!artifactIsTextPreviewable(artifact)) {
    invalidatePreview()
    return
  }

  const projectId = artifact.project_id
  const projectVisit = projectVisitGeneration
  const requestGeneration = ++previewRequestGeneration
  const artifactId = artifact.id
  previewAbort?.abort()
  const controller = new AbortController()
  previewAbort = controller
  previewData.value = null
  previewError.value = ''
  isPreviewLoading.value = true

  try {
    const data =
      window === 'tail'
        ? await fetchArtifactTailPreview(projectId, artifactId, controller.signal)
        : await fetchArtifactPreview(projectId, artifactId, controller.signal)
    if (isCurrentPreviewSource(requestGeneration, projectId, projectVisit, artifactId, window)) {
      if (data.project_id !== projectId || data.artifact_id !== artifactId) {
        previewError.value = '服务端返回的项目或 Artifact 身份与所选文件不一致，已阻止渲染。'
      } else {
        previewData.value = data
      }
    }
  } catch (caught) {
    if (isCurrentPreviewSource(requestGeneration, projectId, projectVisit, artifactId, window)) {
      previewError.value =
        caught instanceof Error && caught.message ? caught.message : '载入预览失败'
      uiStore.showToast('载入预览失败', 'danger')
    }
  } finally {
    if (isCurrentPreviewSource(requestGeneration, projectId, projectVisit, artifactId, window)) {
      isPreviewLoading.value = false
    }
  }
}

function applySelection(artifact: ProjectArtifact, updateRoute: boolean) {
  invalidArtifactNotice.value = false
  if (viewModeForArtifact(artifact) !== viewMode.value) {
    viewMode.value = viewModeForArtifact(artifact)
    kindFilter.value = 'all'
  }
  selectedArtifactId.value = artifact.id
  directoryVisible.value = false
  reportMode.value = 'rendered'
  previewWindow.value =
    artifact.kind === 'report' && isMarkdownFile(artifact.uri, artifact.kind) ? 'head' : 'tail'
  invalidatePreview()
  if (updateRoute) {
    void router.replace({
      name: 'WorkspaceArtifacts',
      params: { id: artifact.project_id },
      query: { ...route.query, artifactId: artifact.id },
    })
  }
}

function selectArtifact(artifact: ProjectArtifact) {
  directoryVisible.value = false
  if (isArtifactSelected(artifact)) return
  applySelection(artifact, true)
}

async function returnToFiles() {
  directoryVisible.value = true
  await nextTick()
  const root = fileList.value?.$el as HTMLElement | undefined
  root?.querySelector<HTMLElement>('[aria-current="true"] button')?.focus({ preventScroll: true })
}

function reconcileRouteSelection() {
  const projectId = currentProjectId.value
  const artifactId = queryArtifactId()
  if (!projectId || !artifactId || !hasQueryData.value) return

  const artifact = artifacts.value.find(
    (candidate) => candidate.id === artifactId && candidate.project_id === projectId,
  )
  if (artifact) {
    if (selectedArtifactId.value !== artifact.id) applySelection(artifact, false)
    return
  }
  if (isFetching.value) return

  clearSelection(false)
  invalidArtifactNotice.value = true
  preserveInvalidNoticeForNextQueryClear = true
  const nextQuery = { ...route.query }
  delete nextQuery.artifactId
  void router.replace({ name: 'WorkspaceArtifacts', params: { id: projectId }, query: nextQuery })
}

function refreshArtifacts() {
  if (currentProjectId.value) void refetch()
}

function togglePreviewWindow() {
  previewWindow.value = previewWindow.value === 'tail' ? 'head' : 'tail'
}

function retryPreview() {
  if (selectedArtifact.value) void loadPreview(selectedArtifact.value)
}

function goToRun(runId: string) {
  const projectId = currentProjectId.value
  if (!projectId) return
  void router.push({ name: 'WorkspaceRuns', params: { id: projectId }, query: { runId } })
}

async function copySha() {
  const sha = selectedArtifact.value?.sha256
  if (!sha) return
  try {
    if (!navigator.clipboard?.writeText) throw new Error('clipboard unavailable')
    await navigator.clipboard.writeText(sha)
    uiStore.showToast('SHA256 HASH 值已复制到剪切板', 'success')
  } catch {
    uiStore.showToast('复制 SHA256 HASH 失败', 'danger')
  }
}

function handleScreenshotError() {
  if (screenshotIdentity.value) screenshotFailedIdentity.value = screenshotIdentity.value
}

watch(viewMode, () => {
  kindFilter.value = 'all'
})

watch(currentProjectId, () => {
  projectVisitGeneration += 1
  viewMode.value = 'deliverable'
  kindFilter.value = 'all'
  filenameSearch.value = ''
  clearSelection()
})

watch(
  () => route.query.artifactId,
  () => {
    const artifactId = queryArtifactId()
    if (!artifactId) {
      if (preserveInvalidNoticeForNextQueryClear) {
        preserveInvalidNoticeForNextQueryClear = false
        return
      }
      clearSelection()
      return
    }
    reconcileRouteSelection()
  },
  { immediate: true },
)

watch(
  [artifactsData, isFetching],
  ([data]) => {
    if (data === undefined) return
    reconcileRouteSelection()
  },
  { immediate: true },
)

watch(
  [selectedArtifact, previewWindow],
  ([artifact]) => {
    if (artifact) void loadPreview(artifact)
    else invalidatePreview()
  },
  { immediate: true },
)

watch(
  () => uiStore.globalRefreshKey,
  () => refreshArtifacts(),
)

onBeforeUnmount(() => {
  mounted = false
  previewRequestGeneration += 1
  previewAbort?.abort()
})
</script>

<style src="./ArtifactsPage.css" scoped></style>
