<template>
  <article class="console-page knowledge-page">
    <h1 class="sr-only">资料库</h1>
    <FilterBar label="资料库与文件搜索" class="knowledge-toolbar">
      <Input
        v-model="fileSearch"
        class="knowledge-path-search"
        placeholder="搜索资料库、文件名或路径"
        aria-label="搜索资料库、文件名或路径"
      />
      <template #actions>
        <Button variant="secondary" :loading="isRefreshingKnowledge" @click="refreshAllLibraries">
          <template #icon><RefreshCw :size="16" /></template>刷新
        </Button>
      </template>
    </FilterBar>
    <input
      ref="knowledgeUploadInput"
      class="hidden-file-input"
      type="file"
      @change="handleKnowledgeUpload"
    />

    <Alert v-if="isLoadingKnowledgeSets" tone="info"
      ><Spinner size="sm" decorative />正在加载资料库...</Alert
    >
    <Alert v-else-if="isKnowledgeSetsError" tone="danger"
      >加载资料库失败：{{ formatError(knowledgeSetsError) }}</Alert
    >
    <Alert v-if="knowledgeSelectionMessage" tone="warning">{{ knowledgeSelectionMessage }}</Alert>
    <Alert v-if="targetActionError" tone="warning">{{ targetActionError }}</Alert>
    <Alert v-if="requestedFocusMissing" tone="warning">定位的资料库不可用，请重新选择。</Alert>
    <Alert v-if="isLibraryCatalogError" tone="warning"
      >资料库名称读取失败：{{ formatError(libraryCatalogError) }}</Alert
    >

    <MasterDetail
      v-if="!isLoadingKnowledgeSets && !isKnowledgeSetsError"
      class="knowledge-workspace"
      fill-height
    >
      <template #master>
        <KnowledgeTreeMenu class="knowledge-navigation" role="navigation" aria-label="资料库文件树" hide-trigger
          :target="{ key: rootLibrary?.key || '', kind: 'library', path: '' }" label="根目录"
          :items="nodeActions(rootLibrary?.key || '', 'library', '')" @select="handleNodeAction">
          <template v-for="library in treeLibraries" :key="library.set.key">
            <KnowledgeTreeMenu class="knowledge-navigation-row knowledge-tree-entry" :data-library-key="library.set.key"
              :class="{ active: isActive(library.set.key, 'library', '') }"
              :style="{ '--knowledge-depth': library.set.builtin ? 0 : 1 }"
              :target="{ key: library.set.key, kind: 'library', path: '' }"
              :label="knowledgeTargetName(library.set.key)" :items="nodeActions(library.set.key, 'library', '')"
              @select="handleNodeAction" @click="activateContainer(library.set.key, 'library', '')">
              <button type="button" class="knowledge-tree-toggle"
                :aria-label="`${library.expanded ? '收起' : '展开'}资料库 ${knowledgeTargetName(library.set.key)}`"
                :aria-expanded="library.expanded" @click.stop="toggleLibrary(library.set.key)">
                <ChevronRight :size="14" :class="{ expanded: library.expanded }" />
              </button>
              <button type="button" class="knowledge-navigation-item knowledge-library-root"
                :title="library.set.key">
                <FolderOpen v-if="library.expanded" :size="16" /><Folder v-else :size="16" />
                <span>{{ knowledgeTargetName(library.set.key) }}</span>
              </button>
            </KnowledgeTreeMenu>
            <template v-if="library.expanded">
              <p v-if="library.loading" class="knowledge-empty-note" role="status">正在加载文件…</p>
              <p v-else-if="library.error" class="knowledge-empty-note" role="alert">加载文件失败：{{ formatError(library.error) }}</p>
              <template v-else>
                <KnowledgeTreeMenu v-for="entry in library.entries" :key="`${library.set.key}:${entry.kind}:${entry.path}`"
                  class="knowledge-navigation-row knowledge-tree-entry" :data-library-key="library.set.key"
                  :class="{ active: isActive(library.set.key, entry.kind, entry.path) }"
                  :style="{ '--knowledge-depth': entry.depth + (library.set.builtin ? 1 : 2) }"
                  :target="{ key: library.set.key, kind: entry.kind, path: entry.path }"
                  :label="entry.path" :items="nodeActions(library.set.key, entry.kind, entry.path)" @select="handleNodeAction"
                  @click="entry.kind === 'directory' ? activateContainer(library.set.key, 'directory', entry.path) : openKnowledgeFile(library.set.key, entry.path)">
                  <button v-if="entry.kind === 'directory'" type="button" class="knowledge-tree-toggle"
                    :aria-label="`${entry.expanded ? '收起' : '展开'}目录 ${entry.path}`"
                    :aria-expanded="entry.expanded" @click.stop="toggleKnowledgeDirectory(library.set.key, entry.path)">
                    <ChevronRight :size="14" :class="{ expanded: entry.expanded }" />
                  </button>
                  <span v-else class="knowledge-tree-spacer" />
                  <button type="button" class="knowledge-navigation-item"
                    :class="{ 'knowledge-navigation-item--directory': entry.kind === 'directory' }"
                    :aria-current="isActive(library.set.key, entry.kind, entry.path) ? 'true' : undefined"
                    :title="entry.path" :data-knowledge-key="library.set.key" :data-knowledge-path="entry.path">
                    <FolderOpen v-if="entry.kind === 'directory' && entry.expanded" :size="16" />
                    <Folder v-else-if="entry.kind === 'directory'" :size="16" />
                    <FileText v-else :size="16" /><span>{{ entry.name }}</span>
                  </button>
                </KnowledgeTreeMenu>
                <p v-if="!library.entries.length && !fileSearch" class="knowledge-empty-note">资料库中还没有文件</p>
              </template>
            </template>
          </template>
          <p v-if="!treeLibraries.length" class="knowledge-empty-note">{{ fileSearch ? '没有匹配的资料' : '还没有资料库' }}</p>
        </KnowledgeTreeMenu>
      </template>

      <template #detail>
        <section class="knowledge-reader" aria-label="资料阅读">
          <header class="knowledge-reader-header">
            <div class="knowledge-reader-identity">
              <h2>
                {{ selectedKnowledgeFilePath ? knowledgeFileName(selectedKnowledgeFilePath) : '选择文件' }}
              </h2>
              <p v-if="selectedKnowledgeKey && selectedKnowledgeFilePath" class="knowledge-secondary">
                {{ knowledgeTargetName(selectedKnowledgeKey) }} / {{ selectedKnowledgeFilePath }}
              </p>
            </div>
            <div class="knowledge-reader-actions">
              <Button
                v-if="selectedKnowledgeFilePath && isMarkdown"
                variant="secondary"
                size="sm"
                :aria-pressed="showSource"
                @click="showSource = !showSource"
              >
                {{ showSource ? '排版阅读' : '查看原文' }}
              </Button>
              <Button
                v-if="selectedKnowledgeFilePath"
                variant="secondary"
                size="sm"
                :disabled="isLoadingKnowledgeFile"
                @click="rememberKnowledgeEditorTrigger($event); editKnowledgeFile()"
              >
                <template #icon><Pencil :size="14" /></template>
                编辑
              </Button>
            </div>
          </header>
          <div class="knowledge-reader-scroll">
            <Alert v-if="isLoadingKnowledgeFile" tone="info"
              ><Spinner size="sm" decorative />正在读取 {{ loadingKnowledgeFilePath }}...</Alert
            >
            <Alert v-if="knowledgeFileReadError" tone="danger">{{ knowledgeFileReadError }}</Alert>
            <p v-if="selectedFileOutsideSearch" class="knowledge-search-context">
              当前文件不在搜索结果中
            </p>
            <template v-if="selectedKnowledgeFilePath">
              <SafeReportMarkdown
                v-if="isMarkdown && !showSource"
                class="knowledge-reading-content"
                :content="savedKnowledgeContent"
              />
              <pre v-else class="knowledge-source">{{ savedKnowledgeContent }}</pre>
              <Alert
                v-for="warning in selectedKnowledgeFileSummary?.warnings || []"
                :key="warning"
                tone="warning"
                >{{ warning }}</Alert
              >
            </template>
            <EmptyState
              v-else-if="!isLoadingKnowledgeFile"
              title="选择文件阅读"
              :description="
                knowledgeFiles.length ? undefined : '仅支持 UTF-8 文本。'
              "
            />
          </div>
        </section>
      </template>
    </MasterDetail>

    <Dialog
      :open="isEditingKnowledgeFile"
      :title="selectedKnowledgeFilePath ? '编辑文件' : '新建文件'"
      description=""
      size="editor"
      :dismissible="!isSavingKnowledgeFile"
      @update:open="handleEditorOpenChange"
      @close-auto-focus="restoreKnowledgeEditorFocus"
    >
      <div class="knowledge-edit-form">
        <FormField v-slot="{ controlAttrs }" label="文件路径" required>
          <Input
            v-model="knowledgeFileDraftPath"
            v-bind="controlAttrs"
            class="mono"
            placeholder="README.md"
            :readonly="Boolean(selectedKnowledgeFilePath && selectedKnowledgeFilePath === selectedKnowledgeSet?.safety_path)"
            :disabled="isLoadingKnowledgeFile"
          />
        </FormField>
        <p
          v-if="
            selectedKnowledgeFilePath && knowledgeFileDraftPath.trim() !== selectedKnowledgeFilePath
          "
          class="knowledge-search-context"
        >
          将写入新路径，原文件保留。
        </p>
        <TextEditor
          v-model="knowledgeFileContent"
          label="内容"
          placeholder="输入文件内容"
          :file-name="knowledgeFileDraftPath"
          :min-height="360"
          :disabled="isLoadingKnowledgeFile"
        />
      </div>
      <template #footer>
        <Button
          variant="secondary"
          :disabled="isSavingKnowledgeFile"
          @click="closeKnowledgeFileEditor"
          >取消</Button
        >
        <Button
          variant="secondary"
          :disabled="!isKnowledgeFileDirty || isSavingKnowledgeFile"
          @click="resetKnowledgeFileDraft"
          >重置</Button
        >
        <Button
          :loading="isSavingKnowledgeFile"
          :disabled="!knowledgeFileDraftPath.trim() || !selectedKnowledgeSet"
          @click="saveKnowledgeFile"
        >
          <template #icon><Save :size="16" /></template>
          保存文件
        </Button>
      </template>
    </Dialog>

    <Dialog
      v-model:open="isCreatingLibraryOpen"
      title="新建资料库"
      description=""
      size="sm"
      :dismissible="!isCreatingKnowledgeSet"
      @close-auto-focus="restoreLibraryActionFocus"
    >
      <FormField v-slot="{ controlAttrs }" label="资料库键值" required>
        <Input
          v-model="newKnowledgeSetKey"
          v-bind="controlAttrs"
          class="mono"
          placeholder="new_knowledge_set"
          @keyup.enter="createLibrary"
        />
      </FormField>
      <template #footer>
        <Button
          variant="secondary"
          :disabled="isCreatingKnowledgeSet"
          @click="isCreatingLibraryOpen = false"
          >取消</Button
        >
        <Button
          :loading="isCreatingKnowledgeSet"
          :disabled="!newKnowledgeSetKey.trim()"
          @click="createLibrary"
          >创建资料库</Button
        >
      </template>
    </Dialog>

    <ConfirmAction
      :open="discardConfirmationOpen"
      title="放弃未保存的资料修改？"
      description="取消可继续编辑当前文件；已保存的正文不受影响。"
      confirm-label="放弃修改"
      cancel-label="继续编辑"
      tone="warning"
      @confirm="resolveDiscard(true)"
      @update:open="!$event && resolveDiscard(false)"
    />
    <ConfirmAction
      :open="deleteKnowledgeSetTarget !== null"
      title="归档资料库"
      :description="`确定归档资料库「${deleteKnowledgeSetTarget?.key || ''}」吗？归档不删除已保存资料。`"
      confirm-label="确认归档"
      tone="danger"
      :loading="isDeletingKnowledgeSet"
      @update:open="handleDeleteKnowledgeSetOpenChange"
      @confirm="handleDeleteKnowledgeSet"
    />
    <ConfirmAction
      :open="deleteKnowledgeFileTarget !== null"
      title="删除资料库文件"
      :description="`确定删除资料库「${deleteKnowledgeFileTarget?.key || ''}」中的文件「${deleteKnowledgeFileTarget?.path || ''}」吗？`"
      confirm-label="确认删除"
      tone="danger"
      :loading="isDeletingKnowledgeFile"
      @update:open="handleDeleteKnowledgeFileOpenChange"
      @confirm="handleDeleteKnowledgeFile"
    />
  </article>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useQueries, useQueryClient } from '@tanstack/vue-query'
import { onBeforeRouteLeave, onBeforeRouteUpdate, useRoute } from 'vue-router'
import { ChevronRight, FileText, Folder, FolderOpen, Pencil, RefreshCw, Save } from '@lucide/vue'
import KnowledgeTreeMenu, { type KnowledgeMenuTarget } from './KnowledgeTreeMenu.vue'
import type { DropdownMenuOption } from '@/components/ui/DropdownMenu.vue'
import { listKnowledgeFiles } from '@/api/client'
import { ConfirmAction, FilterBar, MasterDetail, TextEditor } from '@/components/patterns'
import {
  Alert,
  Button,
  Dialog,
  EmptyState,
  FormField,
  Input,
  Spinner,
} from '@/components/ui'
import { isMarkdownFile } from '@/lib/fileLanguage'
import SafeReportMarkdown from '@/features/reports/SafeReportMarkdown.vue'
import { buildKnowledgeNavigation, knowledgeFileDirectory } from './knowledgeConsoleModel'
import { useKnowledgeConsole } from './useKnowledgeConsole'
import './console.css'

const route = useRoute()
const queryClient = useQueryClient()
const {
  discardConfirmationOpen,
  resolveDiscard,
  closeKnowledgeFileEditor,
  confirmDiscardKnowledgeChanges,
  createNewKnowledgeFile,
  deleteKnowledgeFileTarget,
  deleteKnowledgeSetTarget,
  editKnowledgeFile,
  formatError,
  handleCreateKnowledgeSet,
  handleDeleteKnowledgeFile,
  handleDeleteKnowledgeFileOpenChange,
  handleDeleteKnowledgeSet,
  handleDeleteKnowledgeSetOpenChange,
  handleKnowledgeUpload,
  isCreatingKnowledgeSet,
  isDeletingKnowledgeFile,
  isDeletingKnowledgeSet,
  isEditingKnowledgeFile,
  isKnowledgeFileDirty,
  isKnowledgeSetsError,
  isLibraryCatalogError,
  isLoadingKnowledgeFile,
  isLoadingKnowledgeSets,
  isRefreshingKnowledge,
  isSavingKnowledgeFile,
  isUploadingKnowledgeFile,
  knowledgeFileContent,
  knowledgeFileDraftPath,
  knowledgeFileName,
  knowledgeFileReadError,
  knowledgeFileSavedSnapshot,
  knowledgeFiles,
  knowledgeSelectionMessage,
  knowledgeSetName,
  knowledgeSets,
  knowledgeSetsError,
  knowledgeUploadInput,
  libraryCatalogError,
  loadingKnowledgeFilePath,
  newKnowledgeSetKey,
  openDeleteKnowledgeFile,
  openDeleteKnowledgeSet,
  refreshKnowledgeLibrary,
  resetKnowledgeFileDraft,
  saveKnowledgeFile,
  selectKnowledgeFile,
  selectKnowledgeSet,
  selectedKnowledgeDirectory,
  selectedKnowledgeFilePath,
  selectedKnowledgeFileSummary,
  selectedKnowledgeKey,
  selectedKnowledgeSet,
  triggerKnowledgeUpload,
} = useKnowledgeConsole()

const fileSearch = ref('')
const isCreatingLibraryOpen = ref(false)
const target = ref<{ key: string; directory: string } | null>(null)
const targetActionError = ref('')
let fileSelectionGeneration = 0
let targetSelectionGeneration = 0
const duplicateKnowledgeNames = computed(() => {
  const seen = new Set<string>()
  const duplicates = new Set<string>()
  for (const set of knowledgeSets.value) {
    const name = knowledgeSetName(set.key)
    if (seen.has(name)) duplicates.add(name)
    else seen.add(name)
  }
  return duplicates
})
const knowledgeTargetLabelsCollide = computed(() => {
  const labels = knowledgeSets.value.map((set) => {
    const name = knowledgeSetName(set.key)
    return duplicateKnowledgeNames.value.has(name) ? `${name} (${set.key})` : name
  })
  return new Set(labels).size !== labels.length
})
const knowledgeTargetName = (key: string) => {
  if (knowledgeSets.value.some((set) => set.key === key && set.builtin)) return '根目录'
  const name = knowledgeSetName(key)
  // 唯一库名也可能恰好等于另一库生成的“名称 (key)”标签。
  return knowledgeTargetLabelsCollide.value || duplicateKnowledgeNames.value.has(name) ? `${name} (${key})` : name
}
const expandedLibraries = ref(new Set<string>())
const expandedDirectories = ref<Record<string, Set<string>>>({})
// 搜索自动展开命中祖先，但允许用户临时收起；退出搜索后恢复原展开状态。
const collapsedSearchLibraries = ref(new Set<string>())
const collapsedSearchDirectories = ref<Record<string, Set<string>>>({})
watch(fileSearch, () => {
  collapsedSearchLibraries.value = new Set()
  collapsedSearchDirectories.value = {}
})
const activeNode = ref<KnowledgeMenuTarget | null>(null)
const rootLibrary = computed(() => knowledgeSets.value.find((set) => set.builtin))
const treeQueries = useQueries({
  queries: computed(() => knowledgeSets.value.map((set) => ({
    queryKey: ['knowledgeFiles', set.key],
    queryFn: ({ signal }: { signal: AbortSignal }) => listKnowledgeFiles(set.key, signal),
  }))),
})
const treeLibraries = computed(() => {
  const query = fileSearch.value.trim().toLocaleLowerCase()
  return knowledgeSets.value.map((set, index) => {
    const result = treeQueries.value[index]
    const files = result?.data || []
    const matchesLibrary = `${knowledgeSetName(set.key)} ${set.key}`.toLocaleLowerCase().includes(query)
    const matchesFile = files.some((file) => file.path.toLocaleLowerCase().includes(query))
    return {
      set,
      loading: !result || result.isPending,
      error: result?.isError ? result.error : null,
      expanded: query ? !collapsedSearchLibraries.value.has(set.key) : expandedLibraries.value.has(set.key),
      entries: buildKnowledgeNavigation(
        files,
        expandedDirectories.value[set.key] || new Set(),
        matchesLibrary ? '' : query,
        collapsedSearchDirectories.value[set.key] || new Set(),
      ).sort((a, b) => Number(b.path === set.safety_path) - Number(a.path === set.safety_path)),
      visible: set.builtin || !query || matchesLibrary || matchesFile || !result || result.isPending || result.isError,
    }
  }).filter((library) => library.visible && (library.set.builtin || (query
    ? !collapsedSearchLibraries.value.has(rootLibrary.value?.key || '')
    : expandedLibraries.value.has(rootLibrary.value?.key || ''))))
})
let knowledgeEditorTrigger: HTMLElement | null = null
const rememberKnowledgeEditorTrigger = (event: MouseEvent) => {
  knowledgeEditorTrigger = event.currentTarget as HTMLElement
}
const restoreKnowledgeEditorFocus = (event: Event) => {
  event.preventDefault()
  if (knowledgeEditorTrigger?.isConnected) knowledgeEditorTrigger.focus()
}
const restoreLibraryActionFocus = (event: Event) => {
  event.preventDefault()
  libraryActionTrigger?.focus()
}
let libraryActionTrigger: HTMLElement | null = null
const isActive = (key: string, kind: KnowledgeMenuTarget['kind'], path: string) =>
  activeNode.value?.key === key && activeNode.value.kind === kind && activeNode.value.path === path
const activateContainer = (key: string, kind: 'library' | 'directory', path: string) => {
  activeNode.value = { key, kind, path }
  setTarget(key, path)
  if (kind === 'library') toggleLibrary(key)
  else toggleKnowledgeDirectory(key, path)
}
function nodeActions(key: string, kind: KnowledgeMenuTarget['kind'], path: string): DropdownMenuOption[] {
  const set = knowledgeSets.value.find((item) => item.key === key)
  if (!set) return []
  if (kind === 'file') return [
    { value: 'edit', label: '编辑正文', disabled: isSavingKnowledgeFile.value },
    { value: 'delete', label: '删除文件', tone: 'danger',
      disabled: isDeletingKnowledgeFile.value || path === set.safety_path,
      description: path === set.safety_path ? '系统文件不可删除，路径不可更改' : undefined },
  ]
  return [
    { value: 'create-file', label: '新建文件', disabled: isSavingKnowledgeFile.value },
    { value: 'upload', label: '上传文本', disabled: isUploadingKnowledgeFile.value },
    ...(set.builtin && kind === 'library' ? [{ value: 'create-library', label: '新建资料库' }] : []),
    ...(kind === 'library' ? [{ value: 'archive', label: '归档资料库', tone: 'danger' as const,
      separatorBefore: true, disabled: set.builtin || Boolean(set.referenced_by.length) || isDeletingKnowledgeSet.value,
      description: set.builtin ? '根目录不可归档或删除' : set.referenced_by.length ? '资料库仍被引用' : undefined }] : []),
  ]
}
async function handleNodeAction(action: string, node: KnowledgeMenuTarget, trigger: HTMLElement | null) {
  libraryActionTrigger = trigger
  knowledgeEditorTrigger = trigger
  if (action === 'delete') { openDeleteKnowledgeFile({ key: node.key, path: node.path }, trigger); return }
  if (action === 'archive') {
    const set = knowledgeSets.value.find((item) => item.key === node.key)
    if (set) openDeleteKnowledgeSet(trigger, set)
    return
  }
  if (action === 'create-library') { isCreatingLibraryOpen.value = true; return }
  if (action === 'edit') {
    if (await openKnowledgeFile(node.key, node.path)) editKnowledgeFile()
    return
  }
  setTarget(node.key, node.kind === 'file' ? knowledgeFileDirectory(node.path) : node.path)
  await requestTarget(action === 'upload' ? 'upload' : 'file')
}
const setTarget = (key: string, directory: string) => {
  targetSelectionGeneration++
  target.value = { key, directory }
}
let rootInitialized = false
watch(rootLibrary, (root) => {
  if (!root || rootInitialized) return
  rootInitialized = true
  expandedLibraries.value = new Set([...expandedLibraries.value, root.key])
  activeNode.value = { key: root.key, kind: 'library', path: '' }
  setTarget(root.key, '')
}, { immediate: true })
const createLibrary = async () => {
  const targetGeneration = targetSelectionGeneration
  const key = await handleCreateKnowledgeSet()
  if (!key) return
  if (targetGeneration === targetSelectionGeneration) {
    setTarget(key, '')
    activeNode.value = { key, kind: 'library', path: '' }
  }
  expandedLibraries.value = new Set([...expandedLibraries.value, key])
}
const requestTarget = async (action: 'file' | 'upload') => {
  const destination = target.value
  targetActionError.value = ''
  if (!destination || !knowledgeSets.value.some((set) => set.key === destination.key)) {
    targetActionError.value = '目标资料库已不可用，请重新选择。'
    return
  }
  if (action === 'upload') {
    triggerKnowledgeUpload(destination)
    return
  }
  const generation = ++fileSelectionGeneration
  const targetGeneration = targetSelectionGeneration
  // 等待文件清单时若用户另选写入位置，不再打开绑定旧目标的编辑器。
  const isCurrent = () => generation === fileSelectionGeneration && targetGeneration === targetSelectionGeneration
  try {
    const files = await queryClient.fetchQuery({
      queryKey: ['knowledgeFiles', destination.key],
      queryFn: ({ signal }) => listKnowledgeFiles(destination.key, signal),
    })
    if (!isCurrent() || !(await selectKnowledgeSet(destination.key, { isCurrent }))) return
    selectedKnowledgeDirectory.value = destination.directory
    await createNewKnowledgeFile(files, isCurrent)
  } catch (error) {
    if (isCurrent()) targetActionError.value = `无法读取目标资料库的文件：${formatError(error)}`
  }
}
const refreshAllLibraries = async () => {
  await Promise.all([
    refreshKnowledgeLibrary(),
    queryClient.invalidateQueries({ queryKey: ['knowledgeFiles'] }),
  ])
}
const showSource = ref(false)
const isMarkdown = computed(() => isMarkdownFile(selectedKnowledgeFilePath.value || ''))
const savedKnowledgeContent = computed(() =>
  knowledgeFileSavedSnapshot.value
    ? (JSON.parse(knowledgeFileSavedSnapshot.value) as { content: string }).content
    : '',
)
const selectedFileOutsideSearch = computed(() =>
  Boolean(
    selectedKnowledgeFilePath.value &&
    fileSearch.value.trim() &&
    !`${selectedKnowledgeKey.value || ''} ${knowledgeSetName(selectedKnowledgeKey.value || '')} ${selectedKnowledgeFilePath.value}`
      .toLocaleLowerCase().includes(fileSearch.value.trim().toLocaleLowerCase()),
  ),
)
const requestedFocusMissing = computed(
  () =>
    typeof route.query.focus === 'string' &&
    !isLoadingKnowledgeSets.value &&
    !isKnowledgeSetsError.value &&
    !knowledgeSets.value.some((set) => set.key === route.query.focus),
)

const toggleLibrary = (key: string) => {
  if (fileSearch.value.trim()) {
    const collapsed = new Set(collapsedSearchLibraries.value)
    if (collapsed.has(key)) collapsed.delete(key)
    else collapsed.add(key)
    collapsedSearchLibraries.value = collapsed
    return
  }
  const expanded = new Set(expandedLibraries.value)
  if (expanded.has(key)) expanded.delete(key)
  else expanded.add(key)
  expandedLibraries.value = expanded
}
const toggleKnowledgeDirectory = (key: string, directory: string) => {
  const query = fileSearch.value.trim().toLocaleLowerCase()
  const matchesLibrary = `${knowledgeSetName(key)} ${key}`.toLocaleLowerCase().includes(query)
  if (query && !matchesLibrary) {
    const collapsed = new Set(collapsedSearchDirectories.value[key] || [])
    if (collapsed.has(directory)) collapsed.delete(directory)
    else collapsed.add(directory)
    collapsedSearchDirectories.value = { ...collapsedSearchDirectories.value, [key]: collapsed }
    return
  }
  const expanded = new Set(expandedDirectories.value[key] || [])
  if (expanded.has(directory)) expanded.delete(directory)
  else expanded.add(directory)
  expandedDirectories.value = { ...expandedDirectories.value, [key]: expanded }
}
const openKnowledgeFile = async (key: string, path: string) => {
  const generation = ++fileSelectionGeneration
  activeNode.value = { key, kind: 'file', path }
  setTarget(key, knowledgeFileDirectory(path))
  const isCurrent = () => generation === fileSelectionGeneration
  if (!(await selectKnowledgeSet(key, { isCurrent })) || !isCurrent()) return false
  if (!(await selectKnowledgeFile(path, { isCurrent })) || !isCurrent()) return false
  // 读取只更新阅读对象，不抢走树焦点，也不覆盖后来选择的活动节点或菜单目标。
  return true
}
const handleEditorOpenChange = (open: boolean) => {
  if (!open) closeKnowledgeFileEditor()
}
void knowledgeUploadInput

watch([selectedKnowledgeKey, selectedKnowledgeFilePath], () => {
  // 迟到的读取不能重新展开用户随后收起的节点。
  showSource.value = false
})
watch(isCreatingKnowledgeSet, (creating, wasCreating) => {
  if (wasCreating && !creating && !newKnowledgeSetKey.value) isCreatingLibraryOpen.value = false
})
let pendingKnowledgeFocus: string | null = null
watch(
  [knowledgeSets, () => route.query.focus],
  ([sets, focus], previous) => {
    const focusChanged = focus !== previous[1]
    if (focusChanged) pendingKnowledgeFocus = typeof focus === 'string' ? focus : null
    const key = pendingKnowledgeFocus
    if (!key || !sets.some((set) => set.key === key)) return
    // 库数据刷新不代表导航意图；仅消费一次定位，延迟出现的库仍须保护草稿。
    pendingKnowledgeFocus = null
    const generation = ++fileSelectionGeneration
    const targetGeneration = targetSelectionGeneration
    const isCurrent = () => generation === fileSelectionGeneration
    void selectKnowledgeSet(key, { force: focusChanged && previous.length > 0, isCurrent }).then((selected) => {
      if (!selected || !isCurrent()) return
      if (targetGeneration !== targetSelectionGeneration) return
      setTarget(key, '')
      activeNode.value = { key, kind: 'library', path: '' }
      expandedLibraries.value = new Set([...expandedLibraries.value, key])
    })
  },
  { immediate: true },
)

onBeforeRouteUpdate((to) => {
  const key = typeof to.query.focus === 'string' ? to.query.focus : ''
  if (key !== selectedKnowledgeKey.value && key !== route.query.focus)
    return confirmDiscardKnowledgeChanges()
})
onBeforeRouteLeave(() => confirmDiscardKnowledgeChanges())
const protectBrowserLeave = (event: BeforeUnloadEvent) => {
  if (!isKnowledgeFileDirty.value && !isSavingKnowledgeFile.value) return
  event.preventDefault()
  event.returnValue = ''
}
onMounted(() => window.addEventListener('beforeunload', protectBrowserLeave))
onBeforeUnmount(() => window.removeEventListener('beforeunload', protectBrowserLeave))
</script>
