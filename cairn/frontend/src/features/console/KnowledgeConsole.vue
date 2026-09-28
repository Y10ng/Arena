<template>
  <article class="console-page console-editor-page">
    <div class="section-header section-header--actions">
      <div class="header-actions">
        <IconButton
          variant="secondary"
          size="sm"
          label="新建文件"
          title="新建文件"
          :disabled="!selectedKnowledgeKey"
          @click="createNewKnowledgeFile"
        >
          <FilePlus :size="14" />
        </IconButton>
        <IconButton
          variant="secondary"
          size="sm"
          label="上传文件"
          title="上传文件"
          :loading="isUploadingKnowledgeFile"
          :disabled="!selectedKnowledgeKey"
          @click="triggerKnowledgeUpload"
        >
          <Upload :size="14" />
        </IconButton>
        <input
          ref="knowledgeUploadInput"
          class="hidden-file-input"
          type="file"
          @change="handleKnowledgeUpload"
        />
      </div>
    </div>

    <section class="console-card form-partition">
      <Alert v-if="isLoadingKnowledgeSets" class="state-banner" tone="info">
        <Spinner size="sm" decorative />
        正在加载资料库...
      </Alert>
      <Alert v-else-if="isKnowledgeSetsError" class="state-banner" tone="danger">
        加载资料库失败：{{ formatError(knowledgeSetsError) }}
      </Alert>
      <MasterDetail v-else>
        <template #master>
          <div class="inline-add-row">
            <Input
              v-model="newKnowledgeSetKey"
              class="mono"
              placeholder="new_knowledge_set"
              aria-label="新资料库键值"
              @keyup.enter="handleCreateKnowledgeSet"
            />
            <IconButton
              label="创建资料库"
              title="创建资料库"
              :loading="isCreatingKnowledgeSet"
              :disabled="!newKnowledgeSetKey.trim()"
              @click="handleCreateKnowledgeSet"
            >
              <Plus :size="14" />
            </IconButton>
          </div>

          <div class="knowledge-set-list master-list">
            <button
              v-for="set in knowledgeSets"
              :key="set.key"
              type="button"
              class="knowledge-set-item"
              :class="{ active: selectedKnowledgeKey === set.key }"
              @click="handleKnowledgeSetSelection(set.key)"
            >
              <span class="knowledge-set-main">
                <span class="sc-title mono">{{ set.key }}</span>
                <span class="text-faint">{{ set.file_count }} 文件</span>
              </span>
              <span class="item-badges">
                <span class="default-badge" v-if="set.builtin">内置</span>
                <span class="usage-badge" v-if="set.referenced_by.length"
                  >{{ set.referenced_by.length }} 引用</span
                >
              </span>
            </button>
          </div>

          <div class="master-subhead">目录</div>
          <Alert v-if="isLoadingKnowledgeFiles" class="state-banner" tone="info">
            <Spinner size="sm" decorative />
            正在加载文件...
          </Alert>
          <Alert v-else-if="isKnowledgeFilesError" class="state-banner" tone="danger">
            加载文件失败：{{ formatError(knowledgeFilesError) }}
          </Alert>
          <template v-else>
            <div class="knowledge-directory-list master-list">
              <button
                v-for="group in knowledgeDirectoryGroups"
                :key="group.directory || '__root__'"
                type="button"
                class="knowledge-directory-item"
                :class="{ active: selectedKnowledgeDirectory === group.directory }"
                @click="selectKnowledgeDirectory(group.directory)"
              >
                <span
                  class="knowledge-directory-main"
                  :style="{ paddingLeft: `${group.depth * 12}px` }"
                >
                  <FolderOpen v-if="selectedKnowledgeDirectory === group.directory" :size="13" />
                  <Folder v-else :size="13" />
                  <span class="mono">{{ group.label }}</span>
                </span>
                <span class="text-faint">{{ group.files.length }} 文件</span>
              </button>
            </div>

            <div class="master-subhead">文件</div>
            <div class="knowledge-file-list master-list">
              <button
                v-for="file in knowledgeFilesInSelectedDirectory"
                :key="file.path"
                type="button"
                class="knowledge-file-item"
                :class="{ active: selectedKnowledgeFilePath === file.path }"
                :title="file.path"
                @click="selectKnowledgeFile(file.path)"
              >
                <span class="knowledge-file-main">
                  <FileText :size="13" />
                  <span class="mono">{{ knowledgeFileName(file.path) }}</span>
                </span>
                <span class="text-faint">{{ file.size }} B</span>
              </button>
              <div
                v-if="knowledgeFilesInSelectedDirectory.length === 0"
                class="empty-note text-faint"
              >
                当前目录暂无文件
              </div>
            </div>
          </template>

          <Button
            variant="danger"
            block
            data-console-action="delete-knowledge-set"
            :loading="isDeletingKnowledgeSet"
            :disabled="!canDeleteSelectedKnowledgeSet"
            :title="
              selectedKnowledgeSet?.builtin
                ? '内置资料库不可删除'
                : selectedKnowledgeReferencedBy.length
                  ? '被项目版本引用的资料库不可删除'
                  : '删除当前资料库'
            "
            @click="handleOpenDeleteKnowledgeSet"
          >
            删除资料库
          </Button>
        </template>

        <template #detail>
          <div class="knowledge-files-pane" v-if="selectedKnowledgeSet">
            <div class="knowledge-meta-strip">
              <span class="mono">{{ selectedKnowledgeSet.key }}</span>
              <span>目录：{{ selectedKnowledgeDirectoryLabel }}</span>
              <span v-if="currentKnowledgeRevision"
                >当前版本：v{{ currentKnowledgeRevision.version }}</span
              >
              <span v-if="currentKnowledgeRevision" class="mono"
                >{{ currentKnowledgeRevision.digest_sha256.slice(0, 12) }}…</span
              >
              <span v-if="selectedKnowledgeReferencedBy.length"
                >引用：{{ selectedKnowledgeReferencedBy.join(', ') }}</span
              >
              <span v-else class="text-faint">未被冻结项目引用</span>
            </div>

            <details class="knowledge-revision-panel">
              <summary>版本历史（{{ knowledgeRevisions.length }}）</summary>
              <Alert v-if="isLoadingKnowledgeRevisions" class="state-banner" tone="info">
                <Spinner size="sm" decorative />
                正在加载版本历史...
              </Alert>
              <div v-else class="knowledge-revision-list">
                <div
                  v-for="revision in knowledgeRevisions"
                  :key="revision.version"
                  class="knowledge-revision-item"
                >
                  <strong>v{{ revision.version }}</strong>
                  <span>{{ revision.file_count }} 文件</span>
                  <span class="mono">{{ revision.digest_sha256.slice(0, 12) }}…</span>
                  <span class="text-faint">{{
                    revision.message ||
                    (revision.source === 'legacy_filesystem' ? '旧资料导入' : '资料更新')
                  }}</span>
                </div>
                <span v-if="knowledgeRevisions.length === 0" class="text-faint">尚未发布版本</span>
              </div>
            </details>

            <div class="knowledge-editor-pane">
              <FormField
                v-slot="{ controlAttrs }"
                class="form-group"
                label="文件路径"
                hint="支持任意安全相对路径和文本扩展名，可使用嵌套目录。"
                required
              >
                <Input
                  v-model="knowledgeFileDraftPath"
                  v-bind="controlAttrs"
                  class="mono"
                  placeholder="README.md"
                  :disabled="isLoadingKnowledgeFile"
                />
              </FormField>

              <TextEditor
                v-model="knowledgeFileContent"
                label="内容"
                placeholder="选择或新建文件后编辑内容"
                :file-name="knowledgeFileDraftPath"
                :min-height="520"
                :disabled="isLoadingKnowledgeFile"
              />
              <span
                class="field-error"
                v-for="warning in selectedKnowledgeFileSummary?.warnings || []"
                :key="warning"
                >{{ warning }}</span
              >

              <footer class="editor-footer">
                <Button
                  variant="danger"
                  data-console-action="delete-knowledge-file"
                  :loading="isDeletingKnowledgeFile"
                  :disabled="!selectedKnowledgeFilePath"
                  @click="handleOpenDeleteKnowledgeFile"
                >
                  <template #icon><Trash2 :size="14" /></template>
                  删除文件
                </Button>
                <div class="footer-actions">
                  <Button
                    variant="secondary"
                    :disabled="!isKnowledgeFileDirty"
                    @click="resetKnowledgeFileDraft"
                  >
                    重置
                  </Button>
                  <Button
                    variant="success"
                    :loading="isSavingKnowledgeFile"
                    :disabled="!knowledgeFileDraftPath.trim()"
                    @click="saveKnowledgeFile"
                  >
                    <template #icon><Save :size="14" /></template>
                    {{ isSavingKnowledgeFile ? '保存中...' : '保存文件' }}
                  </Button>
                </div>
              </footer>
            </div>
          </div>

          <EmptyState
            v-else
            class="editor-placeholder"
            title="请选择资料库"
            description="请先创建或选择一个资料库。"
          />
        </template>
      </MasterDetail>
    </section>

    <ConfirmAction
      :open="deleteKnowledgeSetTarget !== null"
      title="删除资料库"
      :description="`确定删除资料库「${deleteKnowledgeSetTarget?.key || ''}」及其中所有文件吗？`"
      confirm-label="确认删除"
      tone="danger"
      :loading="isDeletingKnowledgeSet"
      @update:open="handleDeleteKnowledgeSetOpenChange"
      @confirm="handleDeleteKnowledgeSet"
    />

    <ConfirmAction
      :open="deleteKnowledgeFileTarget !== null"
      title="删除资料库文件"
      :description="`确定删除文件「${deleteKnowledgeFileTarget?.path || ''}」吗？`"
      confirm-label="确认删除"
      tone="danger"
      :loading="isDeletingKnowledgeFile"
      @update:open="handleDeleteKnowledgeFileOpenChange"
      @confirm="handleDeleteKnowledgeFile"
    />
  </article>
</template>

<script setup lang="ts">
import { watch } from 'vue'
import { onBeforeRouteLeave, useRoute, useRouter } from 'vue-router'
import { FilePlus, FileText, Folder, FolderOpen, Plus, Save, Trash2, Upload } from '@lucide/vue'
import { ConfirmAction, MasterDetail, TextEditor } from '@/components/patterns'
import { Alert, Button, EmptyState, FormField, IconButton, Input, Spinner } from '@/components/ui'
import { useKnowledgeConsole } from './useKnowledgeConsole'
import './console.css'

const route = useRoute()
const router = useRouter()
const {
  canDeleteSelectedKnowledgeSet,
  currentKnowledgeRevision,
  confirmDiscardKnowledgeChanges,
  createNewKnowledgeFile,
  deleteKnowledgeFileTarget,
  deleteKnowledgeSetTarget,
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
  isKnowledgeFileDirty,
  isKnowledgeFilesError,
  isKnowledgeSetsError,
  isLoadingKnowledgeFile,
  isLoadingKnowledgeFiles,
  isLoadingKnowledgeRevisions,
  isLoadingKnowledgeSets,
  isSavingKnowledgeFile,
  isUploadingKnowledgeFile,
  knowledgeFileContent,
  knowledgeDirectoryGroups,
  knowledgeFileDraftPath,
  knowledgeFileName,
  knowledgeFilesInSelectedDirectory,
  knowledgeFilesError,
  knowledgeRevisions,
  knowledgeSets,
  knowledgeSetsError,
  knowledgeUploadInput,
  newKnowledgeSetKey,
  openDeleteKnowledgeFile,
  openDeleteKnowledgeSet,
  resetKnowledgeFileDraft,
  saveKnowledgeFile,
  selectKnowledgeDirectory,
  selectKnowledgeFile,
  selectKnowledgeSet,
  selectedKnowledgeDirectory,
  selectedKnowledgeDirectoryLabel,
  selectedKnowledgeFilePath,
  selectedKnowledgeFileSummary,
  selectedKnowledgeKey,
  selectedKnowledgeReferencedBy,
  selectedKnowledgeSet,
  triggerKnowledgeUpload,
} = useKnowledgeConsole()

const actionTrigger = (event: MouseEvent) => event.currentTarget as HTMLElement
const handleOpenDeleteKnowledgeSet = (event: MouseEvent) =>
  openDeleteKnowledgeSet(actionTrigger(event))
const handleOpenDeleteKnowledgeFile = (event: MouseEvent) =>
  openDeleteKnowledgeFile(actionTrigger(event))

void knowledgeUploadInput

const replaceKnowledgeFocus = (key: string | null) => {
  const query = { ...route.query }
  if (key) query.focus = key
  else delete query.focus
  void router.replace({ query })
}

const handleKnowledgeSetSelection = (key: string) => {
  if (selectKnowledgeSet(key)) replaceKnowledgeFocus(key)
}

watch(
  [knowledgeSets, () => route.query.focus],
  ([sets, focus]) => {
    const key = typeof focus === 'string' ? focus : ''
    if (!key || selectedKnowledgeKey.value === key || !sets.some((set) => set.key === key)) return
    if (!selectKnowledgeSet(key)) replaceKnowledgeFocus(selectedKnowledgeKey.value)
  },
  { immediate: true },
)

onBeforeRouteLeave(() => confirmDiscardKnowledgeChanges())
</script>
