<template>
  <article class="console-page console-editor-page">
    <h1 class="sr-only">Prompt 模板</h1>
    <div v-if="isLoadingPromptGroups || isPromptGroupsError" class="prompt-group-actions">
      <Button
        variant="secondary"
        data-console-action="clone-prompt-group"
        :disabled="!selectedPromptGroup || isCreatingPromptGroup"
        @click="handleOpenClonePromptGroup"
      >
        <template #icon><Copy :size="16" /></template>克隆模板组
      </Button>
    </div>

    <section class="prompt-resource-workspace">
      <Alert v-if="isLoadingPromptGroups" class="state-banner" tone="info">
        <Spinner size="sm" decorative />
        正在加载模板组...
      </Alert>
      <Alert v-else-if="isPromptGroupsError" class="state-banner" tone="danger">
        加载模板组失败：{{ formatError(promptGroupsError) }}
      </Alert>
      <div v-else class="prompt-group-workspace">
        <div class="prompt-group-toolbar">
          <Popover v-model:open="groupPickerOpen" align="start" size="lg">
            <template #trigger
              ><Button variant="secondary" class="prompt-group-trigger"
                ><template #icon><FolderOpen :size="16" /></template
                >{{ selectedPromptGroupKey || '选择模板组' }}<ChevronDown :size="16" /></Button
            ></template>
            <div class="prompt-group-picker">
              <h2>模板组目录</h2>
              <div class="prompt-create-row">
                <Input
                  v-model="newPromptGroupKey"
                  class="mono"
                  placeholder="new_prompt_group"
                  aria-label="新模板组键值"
                  @keyup.enter="
                    () => {
                      void handleCreatePromptGroup()
                    }
                  "
                />
                <Select
                  v-model="newPromptCloneFrom"
                  class="mono"
                  title="克隆来源"
                  aria-label="克隆来源"
                  :options="promptGroups.map((group) => ({ value: group.key, label: group.key }))"
                />
                <IconButton
                  label="创建模板组"
                  title="创建模板组"
                  :loading="isCreatingPromptGroup"
                  :disabled="!newPromptGroupKey.trim()"
                  @click="
                    () => {
                      void handleCreatePromptGroup()
                    }
                  "
                >
                  <Plus :size="14" />
                </IconButton>
              </div>

              <div class="prompt-group-list">
                <button
                  v-for="group in promptGroups"
                  :key="group.key"
                  type="button"
                  class="prompt-group-option"
                  :class="{ active: selectedPromptGroupKey === group.key, invalid: !group.valid }"
                  @click="choosePromptGroup(group.key)"
                  :aria-pressed="selectedPromptGroupKey === group.key"
                  :disabled="isPromptDestructivePending"
                >
                  <span class="knowledge-set-main">
                    <span class="sc-title mono">{{ group.key }}</span>
                    <span class="text-faint">{{ group.file_count }} 文件</span>
                  </span>
                  <span class="item-badges">
                    <span class="default-badge">{{ promptGroupKindLabel(group) }}</span>
                    <span class="usage-badge">{{ promptGroupRoleLabel(group) }}</span>
                    <span class="usage-badge" v-if="group.referenced_by.length"
                      >{{ group.referenced_by.length }} 引用</span
                    >
                    <span class="usage-badge danger" v-if="!group.valid">异常</span>
                  </span>
                </button>
              </div>
            </div>
          </Popover>
          <div class="prompt-group-actions">
            <Button
              variant="secondary"
              data-console-action="clone-prompt-group"
              :disabled="!selectedPromptGroup || isCreatingPromptGroup"
              @click="handleOpenClonePromptGroup"
            >
              <template #icon><Copy :size="16" /></template>克隆模板组
            </Button>
            <Button
              variant="secondary"
              :loading="isValidatingPromptGroup"
              :disabled="!selectedPromptGroup"
              @click="validateSelectedPromptGroup"
              ><template #icon><CheckCircle :size="14" /></template>校验模板组</Button
            >
            <Button
              v-if="canRepairSelectedPromptGroup"
              variant="secondary"
              data-console-action="repair-prompt-group"
              :loading="isRepairingPromptGroup"
              @click="handleOpenRepairPromptGroup"
              >修复模板组</Button
            >
            <Button
              v-if="canRestoreSelectedPromptGroup"
              variant="secondary"
              data-console-action="restore-prompt-group"
              :loading="isRestoringPromptGroup"
              @click="handleOpenRestorePromptGroup"
              >恢复内置</Button
            >
            <Button
              variant="danger"
              data-console-action="delete-prompt-group"
              :loading="isDeletingPromptGroup"
              :disabled="!canDeleteSelectedPromptGroup"
              :title="selectedPromptGroupDeleteTitle"
              @click="handleOpenDeletePromptGroup"
            >
              删除模板组
            </Button>
          </div>
        </div>
        <div class="prompt-files-pane" v-if="selectedPromptGroup">
          <div class="prompt-group-meta">
            <span>{{ promptGroupKindLabel(selectedPromptGroup) }}</span>
            <span>{{ promptGroupRoleLabel(selectedPromptGroup) }}</span>
            <span v-if="selectedPromptReferencedBy.length"
              >引用：{{ selectedPromptReferencedBy.join(', ') }}</span
            >
            <span v-else class="text-faint">未被场景引用</span>
            <span v-if="selectedPromptGroup.builtin && !selectedPromptGroup.managed"
              >只读，请先克隆后编辑</span
            >
            <span v-if="!canDeleteSelectedPromptGroup">{{ selectedPromptGroupDeleteTitle }}</span>
          </div>

          <Alert v-if="isLoadingPromptFiles" class="state-banner" tone="info">
            <Spinner size="sm" decorative />
            正在加载模板文件...
          </Alert>
          <Alert v-else-if="isPromptFilesError" class="state-banner" tone="danger">
            加载模板文件失败：{{ formatError(promptFilesError) }}
          </Alert>
          <div v-else class="prompt-workbench">
            <Tabs
              v-if="promptFileTabOptions.length > 0"
              class="prompt-file-tabs"
              :model-value="selectedPromptFileName || ''"
              :options="promptFileTabOptions"
              activation-mode="manual"
              variant="line"
              @update:model-value="handlePromptFileTabChange"
            >
              <template #label="{ option }">
                <span class="prompt-file-tab-label">
                  <span class="prompt-file-category-label">
                    {{ promptFileCategoryLabel(option.value) }}
                  </span>
                  <span class="mono">{{ option.label }}</span>
                  <span
                    class="token-count"
                    :class="{ 'token-count--invalid': promptFileHasMissingTokens(option.value) }"
                  >
                    {{ promptFileTokenLabel(option.value) }}
                  </span>
                </span>
              </template>

              <template #panel>
                <div class="prompt-file-panel">
                  <Alert
                    v-if="selectedPromptValidation"
                    class="state-banner"
                    :tone="selectedPromptValidation.valid ? 'success' : 'danger'"
                    :title="selectedPromptValidation.valid ? '模板组校验通过' : '模板组校验未通过'"
                  >
                    <span v-if="promptValidationStale">（当前草稿已变更，结果待重新校验）</span>
                    <div v-if="selectedPromptValidation.missing_files.length">
                      缺少文件：{{ selectedPromptValidation.missing_files.join(', ') }}
                    </div>
                    <div
                      v-for="(tokens, file) in selectedPromptValidation.missing_tokens"
                      :key="file"
                    >
                      {{ file }} 缺少：{{ tokens.join(', ') }}
                    </div>
                    <div v-for="warning in selectedPromptValidation.warnings" :key="warning">
                      {{ warning }}
                    </div>
                  </Alert>

                  <TextEditor
                    v-model="promptFileContent"
                    label="内容"
                    :file-name="selectedPromptFileName || ''"
                    :min-height="520"
                    :disabled="
                      !selectedPromptCanEdit || isLoadingPromptFile || isPromptDestructivePending
                    "
                  >
                    <template #heading>
                      <div class="prompt-validation-strip">
                        <span class="mono">{{ selectedPromptFileName || '未选择文件' }}</span>
                        <span v-if="isPromptFileDirty" class="validation-bad">未保存</span>
                        <span v-else-if="selectedPromptMissingTokens.length === 0" class="validation-ok">占位符齐全</span>
                        <span v-else class="validation-bad">缺失：{{ selectedPromptMissingTokens.join(', ') }}</span>
                      </div>
                    </template>
                  </TextEditor>

                  <span
                    class="field-error"
                    v-for="token in selectedPromptMissingTokens"
                    :key="token"
                    >{{ selectedPromptFileName }} 缺少 {{ token }}</span
                  >

                  <footer class="editor-footer">
                    <span class="text-muted">当前文件的修改仅在保存后生效</span>
                    <div class="footer-actions">
                      <Button
                        variant="secondary"
                        :disabled="!isPromptFileDirty"
                        @click="resetPromptFileDraft"
                      >
                        恢复已保存内容
                      </Button>
                      <Button
                        variant="success"
                        :loading="isSavingPromptFile"
                        :disabled="
                          !isPromptFileDirty ||
                          !selectedPromptCanEdit ||
                          !selectedPromptFileName ||
                          selectedPromptMissingTokens.length > 0
                        "
                        :title="
                          selectedPromptMissingTokens.length
                            ? '占位符缺失，无法保存'
                            : '保存模板文件'
                        "
                        @click="savePromptFile"
                      >
                        <template #icon><Save :size="14" /></template>
                        {{ isSavingPromptFile ? '保存中...' : '保存文件' }}
                      </Button>
                    </div>
                  </footer>
                </div>
              </template>
            </Tabs>

            <EmptyState v-else title="暂无文件" />
          </div>
        </div>

        <EmptyState
          v-else
          class="editor-placeholder"
          title="请选择模板组"
        />
      </div>
    </section>

    <ConfirmAction
      :open="discardConfirmationOpen"
      title="放弃未保存的 Prompt 修改？"
      description="取消可继续编辑当前文件；已保存的模板内容不受影响。"
      confirm-label="放弃修改"
      cancel-label="继续编辑"
      tone="warning"
      @confirm="resolveDiscard(true)"
      @update:open="!$event && resolveDiscard(false)"
    />
    <ConfirmAction
      :open="deletePromptGroupTarget !== null"
      title="删除模板组"
      :description="`确认删除模板组「${deletePromptGroupTarget?.key || ''}」及其中全部文件吗？`"
      confirm-label="确认删除"
      tone="danger"
      :loading="isDeletingPromptGroup"
      @update:open="handleDeletePromptGroupOpenChange"
      @confirm="handleDeletePromptGroup"
    />

    <ConfirmAction
      :open="repairPromptGroupTarget !== null"
      title="修复模板组"
      :description="`修复将重建模板组「${repairPromptGroupTarget?.key || ''}」的受管文件，确认继续吗？`"
      confirm-label="确认修复"
      tone="warning"
      :loading="isRepairingPromptGroup"
      @update:open="handleRepairPromptGroupOpenChange"
      @confirm="handleRepairPromptGroup"
    />

    <ConfirmAction
      :open="restorePromptGroupTarget !== null"
      title="恢复内置模板组"
      :description="`确认移除「${restorePromptGroupTarget?.key || ''}」的受管覆盖并恢复内置版本吗？`"
      confirm-label="确认恢复"
      tone="warning"
      :loading="isRestoringPromptGroup"
      @update:open="handleRestorePromptGroupOpenChange"
      @confirm="handleRestorePromptGroup"
    />

    <Dialog
      :open="clonePromptGroupSource !== null"
      title="克隆模板组"
      :description="`从「${clonePromptGroupSource?.key || ''}」创建独立副本。`"
      size="sm"
      :dismissible="!isCreatingPromptGroup"
      :close-on-outside="!isCreatingPromptGroup"
      @update:open="handleClonePromptGroupOpenChange"
    >
      <form id="clone-prompt-group-form" @submit.prevent="submitClonePromptGroup">
        <FormField
          v-slot="{ controlAttrs, invalid }"
          label="新模板组键值"
          required
          :error="clonePromptGroupError"
        >
          <Input
            v-bind="controlAttrs"
            v-model="clonePromptGroupKey"
            class="mono"
            autocomplete="off"
            autofocus
            :invalid="invalid"
            :disabled="isCreatingPromptGroup"
          />
        </FormField>
      </form>
      <template #footer>
        <Button
          variant="secondary"
          :disabled="isCreatingPromptGroup"
          @click="handleClonePromptGroupOpenChange(false)"
        >
          取消
        </Button>
        <Button
          type="submit"
          form="clone-prompt-group-form"
          :loading="isCreatingPromptGroup"
          :disabled="!clonePromptGroupKey.trim()"
        >
          创建副本
        </Button>
      </template>
    </Dialog>
  </article>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { onBeforeRouteLeave, useRoute } from 'vue-router'
import { CheckCircle, ChevronDown, Copy, FolderOpen, Plus, Save } from '@lucide/vue'
import { ConfirmAction, TextEditor } from '@/components/patterns'
import {
  Alert,
  Button,
  Dialog,
  EmptyState,
  FormField,
  IconButton,
  Input,
  Select,
  Popover,
  Spinner,
  Tabs,
} from '@/components/ui'
import type { TabsOption } from '@/components/ui'
import { usePromptGroupsConsole } from './usePromptGroupsConsole'
import './console.css'

const route = useRoute()
const {
  discardConfirmationOpen,
  resolveDiscard,
  canDeleteSelectedPromptGroup,
  canRepairSelectedPromptGroup,
  canRestoreSelectedPromptGroup,
  clonePromptGroupError,
  clonePromptGroupKey,
  clonePromptGroupSource,
  confirmDiscardPromptChanges,
  deletePromptGroupTarget,
  formatError,
  handleClonePromptGroupOpenChange,
  handleCreatePromptGroup,
  handleDeletePromptGroup,
  handleDeletePromptGroupOpenChange,
  handleRepairPromptGroup,
  handleRepairPromptGroupOpenChange,
  handleRestorePromptGroup,
  handleRestorePromptGroupOpenChange,
  isCreatingPromptGroup,
  isDeletingPromptGroup,
  isLoadingPromptFile,
  isLoadingPromptFiles,
  isLoadingPromptGroups,
  isPromptDestructivePending,
  isPromptFileDirty,
  isPromptFilesError,
  isPromptGroupsError,
  isRepairingPromptGroup,
  isRestoringPromptGroup,
  isSavingPromptFile,
  isValidatingPromptGroup,
  newPromptCloneFrom,
  newPromptGroupKey,
  openClonePromptGroup,
  openDeletePromptGroup,
  openRepairPromptGroup,
  openRestorePromptGroup,
  promptFileCategories,
  promptFileContent,
  promptFiles,
  promptFilesError,
  promptGroupKindLabel,
  promptGroupRoleLabel,
  promptGroups,
  promptGroupsError,
  promptValidationStale,
  repairPromptGroupTarget,
  resetPromptFileDraft,
  restorePromptGroupTarget,
  savePromptFile,
  selectPromptFile,
  selectPromptGroup,
  selectedPromptCanEdit,
  selectedPromptFileName,
  selectedPromptGroup,
  selectedPromptGroupDeleteTitle,
  selectedPromptGroupKey,
  selectedPromptMissingTokens,
  selectedPromptReferencedBy,
  selectedPromptValidation,
  submitClonePromptGroup,
  syncPromptRoute,
  validateSelectedPromptGroup,
} = usePromptGroupsConsole()

const groupPickerOpen = ref(false)
const choosePromptGroup = async (key: string) => {
  if (await selectPromptGroup(key)) groupPickerOpen.value = false
}
const protectBrowserLeave = (event: BeforeUnloadEvent) => {
  if (!isPromptFileDirty.value && !isSavingPromptFile.value) return
  event.preventDefault()
  event.returnValue = ''
}
onMounted(() => window.addEventListener('beforeunload', protectBrowserLeave))
onBeforeUnmount(() => window.removeEventListener('beforeunload', protectBrowserLeave))
const actionTrigger = (event: MouseEvent) => event.currentTarget as HTMLElement
const handleOpenClonePromptGroup = (event: MouseEvent) => openClonePromptGroup(actionTrigger(event))
const handleOpenDeletePromptGroup = (event: MouseEvent) =>
  openDeletePromptGroup(actionTrigger(event))
const handleOpenRepairPromptGroup = (event: MouseEvent) =>
  openRepairPromptGroup(actionTrigger(event))
const handleOpenRestorePromptGroup = (event: MouseEvent) =>
  openRestorePromptGroup(actionTrigger(event))

const promptFileTabOptions = computed<TabsOption[]>(() =>
  promptFileCategories.value.flatMap((category) =>
    category.files.map((file) => ({
      value: file.path,
      label: file.path,
    })),
  ),
)

const promptFileCategoryByPath = computed(
  () =>
    new Map(
      promptFileCategories.value.flatMap((category) =>
        category.files.map((file) => [file.path, category.label] as const),
      ),
    ),
)

const promptFileByPath = computed(
  () => new Map(promptFiles.value.map((file) => [file.path, file] as const)),
)

const promptFileCategoryLabel = (path: string) => promptFileCategoryByPath.value.get(path) || '文件'

const promptFileHasMissingTokens = (path: string) =>
  (promptFileByPath.value.get(path)?.missing_tokens.length || 0) > 0

const promptFileTokenLabel = (path: string) => {
  const file = promptFileByPath.value.get(path)
  if (!file) return '0 token'
  return file.missing_tokens.length
    ? `${file.missing_tokens.length} 缺失`
    : `${file.required_tokens.length} token`
}

const handlePromptFileTabChange = (path: string) => {
  void selectPromptFile(path)
}

let promptRouteSelectionGeneration = 0

watch(
  [promptGroups, promptFiles, () => route.query.focus, () => route.query.file],
  async ([groups, files, focus, file]) => {
    const generation = ++promptRouteSelectionGeneration
    const targetGroup = typeof focus === 'string' ? focus : ''
    const targetFile = typeof file === 'string' ? file : ''
    if (!targetGroup || !groups.some((group) => group.key === targetGroup)) return

    if (selectedPromptGroupKey.value !== targetGroup) {
      if (!(await selectPromptGroup(targetGroup, { syncRoute: false }))) {
        syncPromptRoute(selectedPromptGroupKey.value, selectedPromptFileName.value)
        return
      }
    }

    if (!targetFile || !files.some((item) => item.path === targetFile)) return
    const selected = await selectPromptFile(targetFile, {
      force: selectedPromptGroupKey.value === targetGroup && !isPromptFileDirty.value,
      syncRoute: false,
    })
    if (generation === promptRouteSelectionGeneration && !selected) {
      syncPromptRoute(selectedPromptGroupKey.value, selectedPromptFileName.value)
    }
  },
  { immediate: true },
)

onBeforeRouteLeave(() => confirmDiscardPromptChanges())
</script>
