<template>
  <MasterDetail class="artifacts-page" fill-height>
    <template #master>
      <header class="workspace-tree-header">
        <form class="workspace-searchbox" role="search" @submit.prevent="submitSearch">
          <Button type="submit" variant="ghost" size="sm" aria-label="搜索文件" :disabled="!workspaceId || searchLoading">
            <Search :size="16" aria-hidden="true" />
          </Button>
          <Input
            ref="searchInput"
            :model-value="searchDraft"
            aria-label="搜索文件名或路径"
            placeholder="搜索文件名或路径…"
            :maxlength="512"
            @update:model-value="updateSearchDraft(String($event ?? ''))"
            @compositionstart="composing = true"
            @compositionend="composing = false"
            @keydown.enter="guardSearchEnter"
            @keydown.esc.stop.prevent="clearSearch"
          />
          <Button v-if="searchDraft" variant="ghost" size="sm" aria-label="清除搜索" @click="clearSearch">
            <X :size="14" aria-hidden="true" />
          </Button>
        </form>
        <Button
          variant="secondary"
          :loading="refreshing"
          :disabled="!projectId || searchLoading"
          @click="refreshFiles"
        >
          <template #icon><RefreshCw :size="16" /></template>
          刷新
        </Button>
      </header>
      <div v-if="searchQuery" ref="searchPanel" class="workspace-tree-scroll" aria-label="文件搜索结果" @keydown.esc.stop.prevent="clearSearch">
        <p v-if="searchLoading" role="status">{{ searchResults.length ? '搜索更新中…' : '正在查找…' }}</p>
        <div v-if="searchError" class="workspace-tree-error" role="alert">
          <p>{{ searchError }}</p>
          <Button size="sm" variant="secondary" @click="runSearch()">重新查找</Button>
        </div>
        <p v-else-if="searchComplete && !searchResults.length && !searchLoading" class="workspace-tree-empty" role="status">未找到匹配文件</p>
        <ul class="workspace-search-results">
          <li v-for="entry in searchResults" :key="entry.path">
            <button
              class="workspace-search-result"
              :data-path="entry.path"
              :aria-pressed="selection?.kind === 'live' && selection.entry.path === entry.path"
              @click="selectFile(entry)"
            >
              <File :size="16" aria-hidden="true" />
              <span><strong>{{ entry.name }}</strong><small>{{ entry.path }}</small></span>
            </button>
          </li>
        </ul>
        <Button v-if="searchCursor && !searchError" size="sm" variant="secondary" :loading="searchLoading" @click="runSearch(true)">继续查找</Button>
      </div>
      <div v-show="!searchQuery" ref="treePanel" class="workspace-tree-scroll" aria-label="研究目录">
        <p v-if="root.loading && !root.loaded" role="status">正在读取目录…</p>
        <div v-if="root.error" class="workspace-tree-error" role="alert">
          <p>{{ root.error }}</p>
          <Button size="sm" variant="secondary" @click="refreshFiles">重试目录</Button>
        </div>
        <p
          v-if="root.loaded && !root.entries.length && !root.loading && !root.error"
          class="workspace-tree-empty"
        >
          目录为空
        </p>
        <TreeRoot
          v-if="treeItems.length"
          v-slot="{ flattenItems }"
          :items="treeItems"
          :model-value="selectedNode"
          :expanded="expanded"
          :get-key="nodeKey"
          :get-children="nodeChildren"
          selection-behavior="replace"
          aria-label="研究文件树"
          class="workspace-tree"
          @update:expanded="updateExpanded"
        >
          <TreeItem
            v-for="item in flattenItems"
            :key="item._id"
            v-slot="{ isExpanded }"
            v-bind="item.bind"
            :disabled="item.value.kind === 'status'"
            class="workspace-tree-row"
            :data-path="item.value.path"
            :data-kind="item.value.kind"
            :style="{ paddingInlineStart: `${8 + (item.level - 1) * 18}px` }"
            :title="
              item.value.kind === 'blocked'
                ? `${item.value.path}：链接或特殊文件不允许读取`
                : item.value.name
            "
            @select="selectNode($event, item.value)"
          >
            <ChevronRight
              v-if="item.value.kind === 'directory'"
              :size="14"
              class="workspace-tree-chevron"
              :class="{ 'is-open': isExpanded }"
              aria-hidden="true"
            />
            <span v-else class="workspace-tree-chevron" />
            <component
              :is="
                item.value.kind === 'directory'
                  ? isExpanded
                    ? FolderOpen
                    : Folder
                  : item.value.kind === 'blocked'
                    ? LockKeyhole
                    : item.value.kind === 'file'
                      ? File
                      : Ellipsis
              "
              :size="16"
              class="workspace-tree-icon"
              aria-hidden="true"
            />
            <span class="workspace-tree-label">{{ item.value.name }}</span>
          </TreeItem>
        </TreeRoot>
        <Button
          v-if="root.nextCursor"
          size="sm"
          variant="ghost"
          :loading="root.loading"
          @click="loadDirectory('', true)"
          >继续加载目录</Button
        >
      </div>
    </template>
    <template #detail>
      <section class="artifact-preview-card" aria-label="文件预览">
        <template v-if="selection || savedId">
          <header class="artifact-preview-header">
            <div class="artifact-preview-title">
              <Badge v-if="selection?.kind === 'saved'" size="sm" tone="neutral">已保存原件</Badge>
              <h2 :title="displayName">{{ displayName }}</h2>
              <p v-if="selection?.kind === 'live'" class="artifact-relative-path">
                {{ selection.entry.path }}
              </p>
            </div>
            <Popover v-if="selection" align="end" size="md">
              <template #trigger><Button size="sm" variant="ghost">文件信息</Button></template>
              <dl class="artifact-file-info">
                <template v-if="selection.kind === 'live'">
                  <dt>工作目录</dt>
                  <dd>/home/kali/workspace</dd>
                  <dt>相对路径</dt>
                  <dd>{{ selection.entry.path }}</dd>
                  <dt>修改时间</dt>
                  <dd>{{ livePreview?.modified_at || selection.entry.modified_at }}</dd>
                </template>
                <template v-else>
                  <dt>Artifact ID</dt>
                  <dd>{{ selection.artifact.id }}</dd>
                  <dt>保存时间</dt>
                  <dd>{{ selection.artifact.created_at }}</dd>
                  <dt>SHA256</dt>
                  <dd>
                    {{ selection.artifact.sha256
                    }}<Button size="sm" variant="link" @click="copySha">复制 HASH</Button>
                  </dd>
                  <template v-if="selection.artifact.task_run_id"
                    ><dt>来源运行</dt>
                    <dd>
                      <Button
                        size="sm"
                        variant="link"
                        @click="goToRun(selection.artifact.task_run_id)"
                        >查看运行</Button
                      >
                    </dd></template
                  >
                </template>
                <dt>大小</dt>
                <dd>{{ fileSize == null ? '未知' : bytes(fileSize) }}</dd>
              </dl>
            </Popover>
            <a v-if="downloadUrl" class="artifact-download-link" :href="downloadUrl" download
              >下载完整文件</a
            >
          </header>
          <div v-if="previewKind === 'text' && (preview?.truncated || isMarkdown)" class="artifact-preview-tools">
            <span v-if="preview?.truncated"
              >{{ previewWindow === 'head' ? '文件开头' : '文件尾部' }} · 已读取
              {{ bytes(preview?.bytes_read ?? 0) }} / {{ bytes(preview?.size ?? 0)
              }}<template v-if="preview?.truncated"> · 内容已截断</template></span
            >
            <Button v-if="preview?.truncated" size="sm" variant="ghost" @click="toggleWindow">{{
              previewWindow === 'head' ? '查看文件尾部' : '查看文件开头'
            }}</Button>
            <Button
              v-if="isMarkdown"
              size="sm"
              variant="ghost"
              @click="rawMarkdown = !rawMarkdown"
              >{{ rawMarkdown ? '阅读文本' : '查看源码' }}</Button
            >
          </div>
          <Alert v-if="livePreview?.encoding_lossy" tone="info" class="artifact-preview-alert"
            >当前 UTF-8 窗口含替代字符；下载完整文件可保留原始字节。</Alert
          >
          <p v-if="previewLoading && preview" class="artifact-refresh-note" role="status">
            预览更新中，暂显示上次读取内容…
          </p>
          <div ref="previewBody" class="artifact-preview-body">
            <div v-if="previewLoading && !preview" class="artifact-preview-loading" role="status">
              <Spinner />正在载入预览…
            </div>
            <ErrorState
              v-else-if="previewError"
              title="文件读取失败"
              :description="previewError"
              @retry="retryPreview"
            />
            <template v-else-if="previewKind === 'text'">
              <MarkdownPreview
                v-if="isMarkdown && !rawMarkdown"
                :content="preview?.content || ''"
              />
              <MonacoEditor
                v-else
                :model-value="preview?.content || ''"
                :reading-state="readingState"
                :reading-view="readingView"
                :language="language"
                read-only
                height="100%"
                :min-height="0"
                word-wrap="on"
                :aria-label="`${displayName} 的文本预览`"
              />
            </template>
            <div v-else-if="previewKind === 'image' && !imageFailed" class="artifact-image-preview">
              <img :key="imageUrl" :src="imageUrl" :alt="displayName" @error="handleImageError" />
            </div>
            <EmptyState
              v-else
              :title="imageFailed ? '图片载入失败' : '该文件不支持在线预览'"
            />
          </div>
        </template>
        <EmptyState
          v-else
          title="选择文件以预览"
        />
      </section>
    </template>
  </MasterDetail>
</template>

<script setup lang="ts">
import {
  computed,
  defineAsyncComponent,
  nextTick,
  onBeforeUnmount,
  reactive,
  ref,
  watch,
} from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { TreeRoot, TreeItem } from 'reka-ui'
import { ChevronRight, Ellipsis, File, Folder, FolderOpen, LockKeyhole, RefreshCw, Search, X } from '@lucide/vue'
import {
  fetchArtifactPreview,
  fetchArtifactTailPreview,
  listProjectArtifacts,
  type ArtifactPreview,
  type ProjectArtifact,
} from '@/api/client'
import {
  fetchWorkspacePreview,
  listWorkspaceEntries,
  searchWorkspaceFiles,
  workspaceContentUrl,
  type WorkspaceEntry,
  type WorkspacePreview,
  type WorkspaceWindow,
} from '@/api/workspace'
import MarkdownPreview from '@/components/MarkdownPreview.vue'
import type { MonacoReadingState } from '@/components/MonacoEditor.vue'
import { MasterDetail, ErrorState } from '@/components/patterns'
import { Alert, Badge, Button, EmptyState, Input, Popover, Spinner } from '@/components/ui'
import { isMarkdownFile, resolveFileLanguage } from '@/lib/fileLanguage'
import { bytes } from '@/lib/format'
import { useUIStore } from '@/stores/ui'
import { useWorkspaceStore } from '@/stores/workspace'
import {
  artifactFilename,
  artifactIsTextPreviewable,
  workspaceErrorCode,
  workspaceErrorMessage,
  workspaceNodeKey,
} from './artifactsModel'

const MonacoEditor = defineAsyncComponent(() => import('@/components/MonacoEditor.vue'))
const route = useRoute()
const router = useRouter()
const uiStore = useUIStore()
const store = useWorkspaceStore()
const projectId = computed(() => store.currentProjectId || '')
const workspaceId = ref('')
type Directory = {
  entries: WorkspaceEntry[]
  nextCursor: string | null
  loaded: boolean
  loading: boolean
  error: string
  pages: number
}
type Node = {
  key: string
  name: string
  path: string
  kind: WorkspaceEntry['kind'] | 'more' | 'retry' | 'status'
  children?: Node[]
  entry?: WorkspaceEntry
}
type Selection =
  | { kind: 'live'; entry: WorkspaceEntry; workspaceId: string }
  | { kind: 'saved'; artifact: ProjectArtifact }
const emptyDirectory = (): Directory => ({
  entries: [],
  nextCursor: null,
  loaded: false,
  loading: false,
  error: '',
  pages: 0,
})
// 用 Map 保留任意真实文件名，避免 __proto__ 等键干扰对象或响应式访问。
const directories = reactive(new Map<string, Directory>([['', emptyDirectory()]]))
const root = computed(() => directories.get('')!)
const expanded = ref<string[]>([])
const selection = ref<Selection | null>(null)
const savedId = ref('')
const previewWindow = ref<WorkspaceWindow>('head')
const livePreview = ref<WorkspacePreview | null>(null)
const savedPreview = ref<ArtifactPreview | null>(null)
const previewLoading = ref(false)
const previewError = ref('')
const imageFailed = ref(false)
const rawMarkdown = ref(false)
const refreshing = ref(false)
const searchDraft = ref('')
const searchQuery = ref('')
const searchResults = ref<WorkspaceEntry[]>([])
const searchCursor = ref<string | null>(null)
const searchComplete = ref(false)
const searchLoading = ref(false)
const searchError = ref('')
const composing = ref(false)
const searchInput = ref<InstanceType<typeof Input>>()
const searchPanel = ref<HTMLElement>()
let searchAbort: AbortController | undefined
const treePanel = ref<HTMLElement>()
const previewBody = ref<HTMLElement>()
const readingState = reactive<MonacoReadingState>({
  views: {},
  searchOpen: false,
  query: '',
  matchIndex: 0,
})
const readingView = computed(() =>
  JSON.stringify([
    projectId.value,
    selection.value?.kind,
    selection.value?.kind === 'live' ? selection.value.workspaceId : '',
    selection.value?.kind === 'live' ? selection.value.entry.path : selection.value?.artifact.id,
    previewWindow.value,
  ]),
)
let visit = 0
let previewGeneration = 0
let savedGeneration = 0
let previewAbort: AbortController | undefined
const directoryRequests = new Map<string, AbortController>()
let mounted = true
const key = (kind: string, path: string) =>
  workspaceNodeKey(projectId.value, workspaceId.value, kind, path)
const nodeKey = (node: Node) => node?.key || ''
const nodeChildren = (node: Node) => node.children
function nodes(path: string): Node[] {
  const dir = directories.get(path)
  if (!dir) return []
  const result: Node[] = dir.entries.map((entry) => ({
    key: key(entry.kind, entry.path),
    name: entry.name,
    path: entry.path,
    kind: entry.kind,
    entry,
    ...(entry.kind === 'directory' ? { children: nodes(entry.path) } : {}),
  }))
  if (path) {
    if (dir.loading)
      result.push({ key: key('status', path), name: '正在读取目录…', path, kind: 'status' })
    else if (dir.error)
      result.push({ key: key('retry', path), name: `${dir.error} 重试目录`, path, kind: 'retry' })
    else if (dir.loaded && !result.length)
      result.push({ key: key('status', path), name: '目录为空', path, kind: 'status' })
    if (dir.nextCursor && !dir.loading)
      result.push({ key: key('more', path), name: '继续加载目录', path, kind: 'more' })
  }
  return result
}
const treeItems = computed(() => nodes(''))
const selectedNode = computed<Node | undefined>(() =>
  selection.value?.kind === 'live'
    ? { key: key('file', selection.value.entry.path), ...selection.value.entry }
    : undefined,
)
const preview = computed(() => livePreview.value || savedPreview.value)
const savedImage = computed(
  () =>
    selection.value?.kind === 'saved' &&
    selection.value.artifact.kind === 'screenshot' &&
    selection.value.artifact.size <= 16 * 1024 * 1024 &&
    /\.(png|jpe?g|gif|webp)$/i.test(artifactFilename(selection.value.artifact)),
)
const previewKind = computed(
  () =>
    livePreview.value?.preview_kind ||
    (savedPreview.value ? 'text' : savedImage.value ? 'image' : 'binary'),
)
const displayName = computed(() =>
  selection.value?.kind === 'live'
    ? selection.value.entry.name
    : selection.value?.kind === 'saved'
      ? artifactFilename(selection.value.artifact)
      : savedId.value,
)
const sourceName = computed(() =>
  selection.value?.kind === 'live'
    ? selection.value.entry.path
    : selection.value?.artifact.uri || '',
)
const isMarkdown = computed(() => isMarkdownFile(sourceName.value))
const language = computed(() => resolveFileLanguage(sourceName.value))
const fileSize = computed(
  () =>
    preview.value?.size ??
    (selection.value?.kind === 'live'
      ? selection.value.entry.size
      : selection.value?.artifact.size),
)
const downloadUrl = computed(() => {
  const value = selection.value
  if (!value) return ''
  return value.kind === 'live'
    ? workspaceContentUrl(projectId.value, value.workspaceId, value.entry.path)
    : `/projects/${encodeURIComponent(value.artifact.project_id)}/artifacts/${encodeURIComponent(value.artifact.id)}/content`
})
const imageUrl = computed(() =>
  selection.value?.kind === 'live' && livePreview.value?.preview_kind === 'image'
    ? workspaceContentUrl(
        projectId.value,
        selection.value.workspaceId,
        selection.value.entry.path,
        true,
      )
    : savedImage.value
      ? downloadUrl.value
      : '',
)
function clearPreview() {
  previewGeneration++
  previewAbort?.abort()
  livePreview.value = null
  savedPreview.value = null
  previewLoading.value = false
  previewError.value = ''
  imageFailed.value = false
}
function resetSearch() {
  searchAbort?.abort()
  searchAbort = undefined
  searchQuery.value = ''
  searchResults.value = []
  searchCursor.value = null
  searchComplete.value = false
  searchLoading.value = false
  searchError.value = ''
}
function clearSearch() {
  searchDraft.value = ''
  resetSearch()
  searchInput.value?.focus()
}
function updateSearchDraft(value: string) {
  searchDraft.value = value
  // 修改后先退出旧查询；Enter 才启动新搜索，旧回包不能冒充当前输入的结果。
  if (value.trim() !== searchQuery.value) resetSearch()
}
function guardSearchEnter(event: KeyboardEvent) {
  if (event.isComposing || composing.value || event.keyCode === 229) event.preventDefault()
}
function submitSearch() {
  if (composing.value || searchLoading.value || !workspaceId.value) return
  const query = searchDraft.value.trim()
  if (!query) return clearSearch()
  resetSearch()
  searchQuery.value = query
  void runSearch()
}
async function runSearch(append = false, preserve = false): Promise<boolean> {
  if (!searchQuery.value || !workspaceId.value || searchLoading.value) return false
  searchAbort?.abort()
  const controller = new AbortController()
  searchAbort = controller
  const project = projectId.value
  const identity = workspaceId.value
  const query = searchQuery.value
  const epoch = visit
  const current = () => mounted && searchAbort === controller && project === projectId.value &&
    identity === workspaceId.value && query === searchQuery.value && epoch === visit
  searchLoading.value = true
  searchError.value = ''
  searchComplete.value = false
  if (!append && !preserve) searchResults.value = []
  try {
    const response = await searchWorkspaceFiles(project, identity, query, append ? searchCursor.value || undefined : undefined, controller.signal)
    if (!current()) return false
    if (response.project_id !== project || response.workspace_id !== identity || response.query !== query)
      throw new Error('identity mismatch')
    searchResults.value = [...new Map([
      ...(append ? searchResults.value : []), ...response.entries,
    ].map((entry) => [entry.path, entry])).values()]
    searchCursor.value = response.next_cursor
    searchComplete.value = !response.has_more
    return true
  } catch (error) {
    if (current()) {
      if (workspaceErrorCode(error) === 'workspace_identity_changed')
        resetWorkspace(workspaceErrorMessage(error))
      else searchError.value = workspaceErrorMessage(error)
    }
    return false
  } finally {
    if (current()) searchLoading.value = false
  }
}
function resetWorkspace(message = '') {
  visit++
  resetSearch()
  searchDraft.value = ''
  directoryRequests.forEach((controller) => controller.abort())
  directoryRequests.clear()
  directories.clear()
  directories.set('', emptyDirectory())
  root.value.error = message
  workspaceId.value = ''
  expanded.value = []
  if (selection.value?.kind === 'live') {
    selection.value = null
    clearPreview()
  }
}
async function loadDirectory(path: string, append = false, refresh = false): Promise<boolean> {
  const project = projectId.value
  if (!project || (path && !workspaceId.value)) return false
  if (!directories.has(path)) directories.set(path, emptyDirectory())
  const dir = directories.get(path)!
  if (dir.loading && !refresh) return false
  directoryRequests.get(path)?.abort()
  const controller = new AbortController()
  directoryRequests.set(path, controller)
  const epoch = visit
  const expected = workspaceId.value
  const current = () =>
    mounted &&
    visit === epoch &&
    projectId.value === project &&
    directoryRequests.get(path) === controller
  dir.loading = true
  dir.error = ''
  let cursor = append ? dir.nextCursor || undefined : undefined
  const pages = refresh ? Math.max(1, dir.pages) : 1
  let result: WorkspaceEntry[] = append ? [...dir.entries] : []
  try {
    for (let page = 0; page < pages; page++) {
      const response = await listWorkspaceEntries(
        project,
        path,
        expected || undefined,
        cursor,
        controller.signal,
      )
      if (!current()) return false
      if (
        response.project_id !== project ||
        response.path !== path ||
        !response.workspace_id ||
        (expected && response.workspace_id !== expected)
      )
        throw new Error('identity mismatch')
      if (!expected) workspaceId.value = response.workspace_id
      result = [
        ...new Map([...result, ...response.entries].map((entry) => [entry.path, entry])).values(),
      ]
      cursor = response.next_cursor || undefined
      if (!cursor) break
    }
    dir.entries = result
    dir.nextCursor = cursor || null
    dir.pages = append ? dir.pages + 1 : pages
    dir.loaded = true
    return true
  } catch (error) {
    if (current()) {
      if (workspaceErrorCode(error) === 'workspace_identity_changed')
        resetWorkspace(workspaceErrorMessage(error))
      else dir.error = workspaceErrorMessage(error)
    }
    return false
  } finally {
    if (current()) {
      dir.loading = false
      directoryRequests.delete(path)
    }
  }
}
function updateExpanded(keys: string[]) {
  const previous = expanded.value
  expanded.value = keys
  for (const value of keys.filter((value) => !previous.includes(value))) {
    const path = JSON.parse(value)[3] as string
    if (!directories.get(path)?.loaded) void loadDirectory(path)
  }
}
function selectNode(event: CustomEvent, node: Node) {
  event.preventDefault()
  if (node.kind === 'directory') {
    // Reka 点击会自行 toggle；Enter/Space 只有 select，需要补齐键盘展开。
    if (event.detail.originalEvent instanceof KeyboardEvent)
      updateExpanded(
        expanded.value.includes(node.key)
          ? expanded.value.filter((value) => value !== node.key)
          : [...expanded.value, node.key],
      )
    return
  }
  if (node.kind === 'more' || node.kind === 'retry') {
    void loadDirectory(node.path, node.kind === 'more')
    return
  }
  if (node.kind !== 'file' || !node.entry) return
  selectFile(node.entry)
}
function selectFile(entry: WorkspaceEntry) {
  savedGeneration++
  savedId.value = ''
  selection.value = { kind: 'live', entry, workspaceId: workspaceId.value }
  previewWindow.value = 'head'
  rawMarkdown.value = false
  const query = { ...route.query }
  delete query.artifactId
  if (route.query.artifactId)
    void router.replace({ name: 'WorkspaceArtifacts', params: { id: projectId.value }, query })
  void loadPreview()
}
async function loadPreview(preserve = false) {
  if (preserve) {
    previewGeneration++
    previewAbort?.abort()
    previewError.value = ''
    imageFailed.value = false
  } else clearPreview()
  const source = selection.value
  if (!source) return
  if (source.kind === 'saved' && !artifactIsTextPreviewable(source.artifact)) return
  const generation = previewGeneration
  const epoch = visit
  const project = projectId.value
  const window = previewWindow.value
  const controller = new AbortController()
  previewAbort = controller
  const current = () =>
    mounted &&
    generation === previewGeneration &&
    (source.kind === 'saved' || epoch === visit) &&
    project === projectId.value &&
    window === previewWindow.value
  previewLoading.value = true
  try {
    if (source.kind === 'live') {
      const response = await fetchWorkspacePreview(
        project,
        source.workspaceId,
        source.entry.path,
        window,
        controller.signal,
      )
      if (!current()) return
      if (
        response.project_id !== project ||
        response.workspace_id !== source.workspaceId ||
        response.path !== source.entry.path ||
        response.window !== window
      )
        throw new Error('identity mismatch')
      livePreview.value = response
    } else {
      const response = await (window === 'head' ? fetchArtifactPreview : fetchArtifactTailPreview)(
        project,
        source.artifact.id,
        controller.signal,
      )
      if (!current()) return
      if (response.project_id !== project || response.artifact_id !== source.artifact.id)
        throw new Error('identity mismatch')
      savedPreview.value = response
    }
  } catch (error) {
    if (current()) {
      if (workspaceErrorCode(error) === 'workspace_identity_changed')
        resetWorkspace(workspaceErrorMessage(error))
      else previewError.value = workspaceErrorMessage(error)
    }
  } finally {
    if (current()) previewLoading.value = false
  }
}
async function loadSaved(id: string) {
  savedId.value = id
  selection.value = null
  clearPreview()
  const generation = ++savedGeneration
  const project = projectId.value
  previewLoading.value = true
  const current = () =>
    mounted && generation === savedGeneration && project === projectId.value && savedId.value === id
  try {
    const artifacts = await listProjectArtifacts(project)
    if (!current()) return
    const artifact = artifacts.find((item) => item.id === id && item.project_id === project)
    if (!artifact) {
      previewError.value = '指定的已保存原件不存在、已删除或不属于当前项目。'
      previewLoading.value = false
      return
    }
    selection.value = { kind: 'saved', artifact }
    previewWindow.value = 'head'
    rawMarkdown.value = false
    await loadPreview()
  } catch {
    if (current()) {
      previewError.value = '已保存原件读取失败，请重试。'
      previewLoading.value = false
    }
  }
}
function toggleWindow() {
  previewWindow.value = previewWindow.value === 'head' ? 'tail' : 'head'
  void loadPreview()
}
async function retryPreview(preserve = false) {
  if (savedId.value && !selection.value) await loadSaved(savedId.value)
  else await loadPreview(preserve)
}
async function refreshFiles() {
  if (refreshing.value || searchLoading.value) return
  refreshing.value = true
  const project = projectId.value
  const epoch = visit
  const view = readingView.value
  const panel = searchQuery.value ? searchPanel.value : treePanel.value
  const focused = panel?.contains(document.activeElement)
    ? (document.activeElement as HTMLElement).dataset.path
    : undefined
  const treeScroll = panel?.scrollTop || 0
  const scrolls = [
    previewBody.value,
    ...Array.from(
      previewBody.value?.querySelectorAll<HTMLElement>(
        '.markdown-preview, .artifact-image-preview',
      ) || [],
    ),
  ]
    .filter((element): element is HTMLElement => Boolean(element))
    .map((element) => ({ element, top: element.scrollTop, left: element.scrollLeft }))
  const paths = expanded.value
    .map((value) => JSON.parse(value)[3] as string)
    .sort((a, b) => a.split('/').length - b.split('/').length)
  try {
    if (searchQuery.value) {
      await runSearch(false, true)
      if (project === projectId.value && epoch === visit && view === readingView.value)
        await retryPreview(true)
    } else if (await loadDirectory('', false, true)) {
      for (const path of paths) {
        if (project !== projectId.value || epoch !== visit) return
        await loadDirectory(path, false, true)
      }
      if (project === projectId.value && epoch === visit && view === readingView.value)
        await retryPreview(true)
    }
  } finally {
    refreshing.value = false
    await nextTick()
    if (project === projectId.value && epoch === visit) {
      if (panel?.isConnected) panel.scrollTop = treeScroll
      if (view === readingView.value)
        scrolls.forEach(({ element, top, left }) => {
          element.scrollTop = top
          element.scrollLeft = left
        })
      if (focused !== undefined)
        Array.from(panel?.querySelectorAll<HTMLElement>('[data-path]') || [])
          .find((item) => item.dataset.path === focused)
          ?.focus({ preventScroll: true })
    }
  }
}
function handleImageError(event: Event) {
  if ((event.currentTarget as HTMLImageElement).getAttribute('src') === imageUrl.value)
    imageFailed.value = true
}
function goToRun(runId: string) {
  void router.push({ name: 'WorkspaceRuns', params: { id: projectId.value }, query: { runId } })
}
async function copySha() {
  if (selection.value?.kind !== 'saved') return
  try {
    await navigator.clipboard.writeText(selection.value.artifact.sha256)
    uiStore.showToast('SHA256 已复制', 'success')
  } catch {
    uiStore.showToast('复制 SHA256 失败', 'danger')
  }
}
watch(
  projectId,
  () => {
    savedGeneration++
    selection.value = null
    savedId.value = ''
    clearPreview()
    resetWorkspace()
    if (projectId.value) void loadDirectory('')
  },
  { immediate: true },
)
watch(
  [projectId, () => route.query.artifactId],
  ([project, value]) => {
    if (project && typeof value === 'string' && value.trim()) void loadSaved(value)
    else if (savedId.value) {
      savedGeneration++
      savedId.value = ''
      selection.value = null
      clearPreview()
    }
  },
  { immediate: true },
)
watch(
  () => uiStore.globalRefreshKey,
  () => {
    void refreshFiles()
  },
)
onBeforeUnmount(() => {
  mounted = false
  resetSearch()
  savedGeneration++
  clearPreview()
  directoryRequests.forEach((controller) => controller.abort())
})
</script>

<style src="./ArtifactsPage.css" scoped></style>
