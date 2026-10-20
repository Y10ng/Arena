<template>
  <article class="console-page knowledge-page">
    <FilterBar label="资料库与文件搜索" class="knowledge-toolbar">
      <Popover v-model:open="isLibraryPickerOpen" align="start" size="md">
        <template #trigger>
          <Button class="knowledge-library-trigger" variant="secondary" aria-label="选择资料库">
            <template #icon><Library :size="16" /></template>
            <span>{{
              selectedKnowledgeKey ? knowledgeSetName(selectedKnowledgeKey) : '选择资料库'
            }}</span>
            <ChevronDown :size="16" />
          </Button>
        </template>
        <div class="knowledge-library-picker">
          <Input v-model="librarySearch" placeholder="搜索库名称或键值" aria-label="搜索资料库" />
          <div class="knowledge-library-options" aria-label="资料库列表">
            <button
              v-for="set in filteredKnowledgeSets"
              :key="set.key"
              type="button"
              class="knowledge-library-option"
              :class="{ active: selectedKnowledgeKey === set.key }"
              :aria-pressed="selectedKnowledgeKey === set.key"
              @click="handleKnowledgeSetSelection(set.key)"
            >
              <span class="knowledge-set-main">
                <span>{{ knowledgeSetName(set.key) }}</span>
                <span
                  v-if="knowledgeSetName(set.key) !== set.key"
                  class="knowledge-secondary mono"
                  >{{ set.key }}</span
                >
              </span>
              <span class="knowledge-secondary">{{ set.file_count }} 文件</span>
            </button>
            <p v-if="!filteredKnowledgeSets.length" class="knowledge-empty-note">
              {{ librarySearch ? '没有匹配的资料库' : '还没有资料库' }}
            </p>
          </div>
        </div>
      </Popover>
      <Input
        v-model="fileSearch"
        class="knowledge-path-search"
        placeholder="搜索当前库的文件名或路径"
        aria-label="搜索当前库的文件名或路径"
        :disabled="!selectedKnowledgeSet"
      />
      <template #actions>
        <IconButton
          variant="secondary"
          label="刷新资料库"
          :loading="isRefreshingKnowledge"
          @click="refreshKnowledgeLibrary"
          ><RefreshCw :size="16"
        /></IconButton>
        <DropdownMenu
          :items="[
            {
              value: 'upload',
              label: isUploadingKnowledgeFile ? '正在上传…' : '上传文本',
              disabled: !selectedKnowledgeSet || isUploadingKnowledgeFile,
            },
            { value: 'create-library', label: '新建资料库' },
            {
              value: 'archive-library',
              label: isDeletingKnowledgeSet ? '正在归档…' : '归档资料库',
              separatorBefore: true,
              tone: 'danger',
              disabled: !canDeleteSelectedKnowledgeSet || isDeletingKnowledgeSet,
              description: selectedKnowledgeSet?.builtin
                ? '内置资料库不可归档'
                : selectedKnowledgeReferencedBy.length
                  ? '资料库已被项目引用，不能归档'
                  : undefined,
            },
          ]"
          @select="handleLibraryAction"
        >
          <template #trigger
            ><Button
              variant="secondary"
              aria-label="资料库更多操作"
              data-console-action="knowledge-library-menu"
              @click="rememberLibraryActionTrigger"
              @keydown="rememberLibraryActionTrigger"
              ><MoreHorizontal :size="16" />更多</Button
            ></template
          >
        </DropdownMenu>
        <Button
          data-console-action="create-knowledge-file"
          :disabled="!selectedKnowledgeSet"
          @click="createNewKnowledgeFile"
        >
          <template #icon><FilePlus :size="16" /></template>新建文件
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
    <Alert v-if="requestedFocusMissing" tone="warning">定位的资料库不可用，请重新选择。</Alert>
    <Alert v-if="isLibraryCatalogError" tone="warning"
      >资料库名称读取失败：{{ formatError(libraryCatalogError) }}</Alert
    >

    <MasterDetail
      v-if="!isLoadingKnowledgeSets && !isKnowledgeSetsError && selectedKnowledgeSet"
      class="knowledge-workspace"
      :class="{ 'knowledge-workspace--reading': isReading }"
      fill-height
    >
      <template #master>
        <header class="knowledge-navigation-heading">
          <span>目录与文件</span>
          <span class="knowledge-secondary">{{ knowledgeFiles.length }} 文件</span>
        </header>
        <nav class="knowledge-navigation" aria-label="库内目录与文件">
          <Alert v-if="isLoadingKnowledgeFiles" tone="info"
            ><Spinner size="sm" decorative />正在加载文件...</Alert
          >
          <Alert v-else-if="isKnowledgeFilesError" tone="danger"
            >加载文件失败：{{ formatError(knowledgeFilesError) }}</Alert
          >
          <template v-else>
            <div
              v-for="entry in knowledgeNavigation"
              :key="`${selectedKnowledgeSet.key}:${entry.kind}:${entry.path}`"
              class="knowledge-navigation-row"
              :class="{ active: entry.kind === 'file' && selectedKnowledgeFilePath === entry.path }"
              :style="{ '--knowledge-depth': entry.depth }"
            >
              <button
                type="button"
                class="knowledge-navigation-item"
                :class="{ 'knowledge-navigation-item--directory': entry.kind === 'directory' }"
                :aria-expanded="entry.kind === 'directory' ? entry.expanded : undefined"
                :aria-current="
                  entry.kind === 'file' && selectedKnowledgeFilePath === entry.path
                    ? 'page'
                    : undefined
                "
                :title="entry.path"
                @click="
                  entry.kind === 'directory'
                    ? toggleKnowledgeDirectory(entry.path)
                    : openKnowledgeFile(entry.path)
                "
              >
                <ChevronRight
                  v-if="entry.kind === 'directory'"
                  :size="14"
                  class="knowledge-directory-chevron"
                  :class="{ expanded: entry.expanded }"
                />
                <FolderOpen v-if="entry.kind === 'directory' && entry.expanded" :size="16" />
                <Folder v-else-if="entry.kind === 'directory'" :size="16" />
                <FileText v-else :size="16" />
                <span>{{ entry.name }}</span>
              </button>
              <DropdownMenu
                v-if="entry.kind === 'file'"
                :items="[
                  {
                    value: 'delete',
                    label: '删除文件',
                    tone: 'danger',
                    disabled: isDeletingKnowledgeFile,
                  },
                ]"
                @select="handleKnowledgeFileAction($event, selectedKnowledgeSet.key, entry.path)"
              >
                <template #trigger>
                  <IconButton
                    class="knowledge-file-menu-trigger"
                    :label="`文件更多操作：${entry.path}`"
                    :data-console-action="`knowledge-file-menu:${selectedKnowledgeSet.key}:${entry.path}`"
                    @click="
                      rememberKnowledgeFileActionTarget($event, selectedKnowledgeSet.key, entry.path)
                    "
                    @keydown="
                      rememberKnowledgeFileActionTarget($event, selectedKnowledgeSet.key, entry.path)
                    "
                  ><MoreHorizontal :size="16" /></IconButton>
                </template>
              </DropdownMenu>
            </div>
            <p v-if="!knowledgeNavigation.length" class="knowledge-empty-note">
              {{ fileSearch ? '没有匹配的文件' : '资料库中还没有文件' }}
            </p>
          </template>
        </nav>
      </template>

      <template #detail>
        <section class="knowledge-reader" aria-label="资料阅读">
          <header class="knowledge-reader-header">
            <Button class="knowledge-back" variant="ghost" size="sm" @click="returnToKnowledgeList">
              <template #icon><ArrowLeft :size="16" /></template>
              返回文件列表
            </Button>
            <h2>
              {{
                selectedKnowledgeFilePath
                  ? knowledgeFileName(selectedKnowledgeFilePath)
                  : '选择文件'
              }}
            </h2>
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
                @click="editKnowledgeFile"
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
                knowledgeFiles.length ? '从左侧目录选择一个文件。' : '可新建文件或上传 UTF-8 文本。'
              "
            />
            <details class="knowledge-information">
              <summary>文件与版本信息</summary>
              <div class="knowledge-information-content">
                <dl class="knowledge-file-meta">
                  <div>
                    <dt>资料库</dt>
                    <dd>
                      {{ knowledgeSetName(selectedKnowledgeSet.key) }}
                      <span class="mono">{{ selectedKnowledgeSet.key }}</span>
                    </dd>
                  </div>
                  <div v-if="selectedKnowledgeFilePath">
                    <dt>文件路径</dt>
                    <dd class="mono">{{ selectedKnowledgeFilePath }}</dd>
                  </div>
                  <div v-if="currentKnowledgeRevision">
                    <dt>当前版本</dt>
                    <dd>
                      v{{ currentKnowledgeRevision.version }}
                      <span class="mono">{{ currentKnowledgeRevision.digest_sha256 }}</span>
                    </dd>
                  </div>
                  <div v-if="selectedKnowledgeReferencedBy.length">
                    <dt>项目引用</dt>
                    <dd>{{ selectedKnowledgeReferencedBy.join('、') }}</dd>
                  </div>
                </dl>
                <details class="knowledge-revisions">
                  <summary>版本历史（{{ knowledgeRevisions.length }}）</summary>
                  <Alert v-if="isLoadingKnowledgeRevisions" tone="info"
                    ><Spinner size="sm" decorative />正在加载版本历史...</Alert
                  >
                  <Alert v-else-if="isKnowledgeRevisionsError" tone="danger"
                    >加载版本失败：{{ formatError(knowledgeRevisionsError) }}</Alert
                  >
                  <ol v-else class="knowledge-revision-list">
                    <li
                      v-for="revision in knowledgeRevisions"
                      :key="revision.version"
                      class="knowledge-revision-item"
                    >
                      <strong>v{{ revision.version }}</strong>
                      <span>{{ revision.file_count }} 文件</span>
                      <span class="mono">{{ revision.digest_sha256 }}</span>
                      <span v-if="revision.message">{{ revision.message }}</span>
                    </li>
                    <li v-if="!knowledgeRevisions.length">尚未发布版本</li>
                  </ol>
                </details>
              </div>
            </details>
          </div>
        </section>
      </template>
    </MasterDetail>
    <EmptyState
      v-else-if="!isLoadingKnowledgeSets && !isKnowledgeSetsError"
      title="请选择资料库"
      description="选择资料库后浏览文件，或新建一个资料库。"
    />

    <Dialog
      :open="isEditingKnowledgeFile"
      :title="selectedKnowledgeFilePath ? '编辑文件' : '新建文件'"
      description=""
      size="lg"
      :dismissible="!isSavingKnowledgeFile"
      @update:open="handleEditorOpenChange"
    >
      <div class="knowledge-edit-form">
        <FormField v-slot="{ controlAttrs }" label="文件路径" required>
          <Input
            v-model="knowledgeFileDraftPath"
            v-bind="controlAttrs"
            class="mono"
            placeholder="README.md"
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
    >
      <FormField v-slot="{ controlAttrs }" label="资料库键值" required>
        <Input
          v-model="newKnowledgeSetKey"
          v-bind="controlAttrs"
          class="mono"
          placeholder="new_knowledge_set"
          @keyup.enter="handleCreateKnowledgeSet"
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
          @click="handleCreateKnowledgeSet"
          >创建资料库</Button
        >
      </template>
    </Dialog>

    <ConfirmAction
      :open="deleteKnowledgeSetTarget !== null"
      title="归档资料库"
      :description="`确定归档资料库「${deleteKnowledgeSetTarget?.key || ''}」吗？归档不物理删除历史版本。`"
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
import { onBeforeRouteLeave, onBeforeRouteUpdate, useRoute, useRouter } from 'vue-router'
import {
  ArrowLeft,
  ChevronDown,
  ChevronRight,
  FilePlus,
  FileText,
  Folder,
  FolderOpen,
  Library,
  MoreHorizontal,
  Pencil,
  RefreshCw,
  Save,
} from '@lucide/vue'
import { ConfirmAction, FilterBar, MasterDetail, TextEditor } from '@/components/patterns'
import {
  Alert,
  Button,
  Dialog,
  DropdownMenu,
  EmptyState,
  FormField,
  IconButton,
  Input,
  Popover,
  Spinner,
} from '@/components/ui'
import { isMarkdownFile } from '@/lib/fileLanguage'
import SafeReportMarkdown from '@/features/reports/SafeReportMarkdown.vue'
import { buildKnowledgeNavigation, knowledgeFileDirectory } from './knowledgeConsoleModel'
import { useKnowledgeConsole } from './useKnowledgeConsole'
import './console.css'

const route = useRoute()
const router = useRouter()
const {
  canDeleteSelectedKnowledgeSet,
  closeKnowledgeFileEditor,
  confirmDiscardKnowledgeChanges,
  createNewKnowledgeFile,
  currentKnowledgeRevision,
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
  isKnowledgeFilesError,
  isKnowledgeRevisionsError,
  isKnowledgeSetsError,
  isLibraryCatalogError,
  isLoadingKnowledgeFile,
  isLoadingKnowledgeFiles,
  isLoadingKnowledgeRevisions,
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
  knowledgeFilesError,
  knowledgeRevisions,
  knowledgeRevisionsError,
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
  selectedKnowledgeReferencedBy,
  selectedKnowledgeSet,
  triggerKnowledgeUpload,
} = useKnowledgeConsole()

const librarySearch = ref('')
const fileSearch = ref('')
const isLibraryPickerOpen = ref(false)
const isCreatingLibraryOpen = ref(false)
let libraryActionTrigger: HTMLElement | null = null
const rememberLibraryActionTrigger = (event: Event) => {
  libraryActionTrigger = event.currentTarget as HTMLElement
}
const handleLibraryAction = (action: string) => {
  if (action === 'create-library') isCreatingLibraryOpen.value = true
  else if (action === 'upload' && selectedKnowledgeSet.value && !isUploadingKnowledgeFile.value)
    triggerKnowledgeUpload()
  else if (action === 'archive-library') openDeleteKnowledgeSet(libraryActionTrigger)
}
let knowledgeFileActionTarget: { key: string; path: string; trigger: HTMLElement } | null = null
const rememberKnowledgeFileActionTarget = (event: Event, key: string, path: string) => {
  knowledgeFileActionTarget = { key, path, trigger: event.currentTarget as HTMLElement }
}
const handleKnowledgeFileAction = (action: string, key: string, path: string) => {
  const target = knowledgeFileActionTarget
  if (action === 'delete' && target?.key === key && target.path === path)
    openDeleteKnowledgeFile({ key, path }, target.trigger)
}
const isReading = ref(false)
const showSource = ref(false)
const expandedDirectories = ref(new Set<string>())
const filteredKnowledgeSets = computed(() => {
  const search = librarySearch.value.trim().toLocaleLowerCase()
  return knowledgeSets.value.filter((set) =>
    `${knowledgeSetName(set.key)} ${set.key}`.toLocaleLowerCase().includes(search),
  )
})
const knowledgeNavigation = computed(() =>
  buildKnowledgeNavigation(knowledgeFiles.value, expandedDirectories.value, fileSearch.value),
)
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
    !selectedKnowledgeFilePath.value
      .toLocaleLowerCase()
      .includes(fileSearch.value.trim().toLocaleLowerCase()),
  ),
)
const requestedFocusMissing = computed(
  () =>
    typeof route.query.focus === 'string' &&
    !isLoadingKnowledgeSets.value &&
    !isKnowledgeSetsError.value &&
    !knowledgeSets.value.some((set) => set.key === route.query.focus),
)

const toggleKnowledgeDirectory = (directory: string) => {
  selectedKnowledgeDirectory.value = directory
  const expanded = new Set(expandedDirectories.value)
  if (expanded.has(directory)) expanded.delete(directory)
  else expanded.add(directory)
  expandedDirectories.value = expanded
}
const openKnowledgeFile = async (path: string) => {
  const selection = selectKnowledgeFile(path)
  if (isLoadingKnowledgeFile.value) isReading.value = true
  if (await selection) isReading.value = true
}
const returnToKnowledgeList = () => {
  if (confirmDiscardKnowledgeChanges()) isReading.value = false
}
const handleEditorOpenChange = (open: boolean) => {
  if (!open) closeKnowledgeFileEditor()
}
void knowledgeUploadInput

const replaceKnowledgeFocus = (key: string | null) => {
  const query = { ...route.query }
  if (key) query.focus = key
  else delete query.focus
  void router.replace({ query })
}
const handleKnowledgeSetSelection = (key: string) => {
  if (!selectKnowledgeSet(key)) return
  isLibraryPickerOpen.value = false
  replaceKnowledgeFocus(key)
}
watch(selectedKnowledgeKey, () => {
  expandedDirectories.value = new Set()
  fileSearch.value = ''
  isReading.value = false
  showSource.value = false
})
watch(selectedKnowledgeFilePath, (path) => {
  showSource.value = false
  if (!path) {
    isReading.value = isLoadingKnowledgeFile.value
    return
  }
  isReading.value = true
  const expanded = new Set(expandedDirectories.value)
  let directory = knowledgeFileDirectory(path)
  while (directory) {
    expanded.add(directory)
    directory = knowledgeFileDirectory(directory)
  }
  expandedDirectories.value = expanded
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
    selectKnowledgeSet(key, { force: focusChanged && previous.length > 0 })
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
