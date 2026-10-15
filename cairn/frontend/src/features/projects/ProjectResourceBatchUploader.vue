<template>
  <section class="resource-batch-uploader" aria-label="项目辅助资料上传">
    <input
      ref="fileInput"
      type="file"
      multiple
      class="resource-file-input"
      :disabled="controlsDisabled"
      @change="handleFiles"
    />
    <input
      ref="zipInput"
      type="file"
      accept=".zip,application/zip"
      class="resource-file-input"
      :disabled="controlsDisabled"
      @change="handleZip"
    />

    <div class="resource-batch-actions">
      <div class="resource-batch-buttons">
        <Button
          size="sm"
          variant="secondary"
          :disabled="controlsDisabled"
          @click="fileInput?.click()"
          ><template #icon><FilePlus :size="14" /></template>添加文件</Button
        >
        <Button
          size="sm"
          variant="secondary"
          :disabled="controlsDisabled"
          @click="zipInput?.click()"
          ><template #icon><Archive :size="14" /></template>导入 ZIP</Button
        >
        <slot name="actions" />
      </div>
    </div>

    <div
      class="resource-drop-zone"
      :class="{ 'is-dragging': dragging && !controlsDisabled }"
      @dragover.prevent="dragging = !controlsDisabled"
      @dragleave="dragging = false"
      @drop.prevent="handleDrop"
    >
      <Upload :size="18" aria-hidden="true" />
      <span>将文件拖入此区域，或多选添加文件。ZIP 展开请使用“导入 ZIP”。</span>
    </div>
    <p v-if="operation === 'importing-zip'" class="resource-batch-status" role="status">
      正在由服务器校验并展开 ZIP…
    </p>
    <p v-else-if="operation === 'uploading-files'" class="resource-batch-status" role="status">
      正在上传文件…
    </p>
    <p v-else-if="operation === 'loading-config'" class="resource-batch-status" role="status">
      正在加载服务器资源配置…
    </p>
    <p v-else-if="operation === 'checking-batch'" class="resource-batch-status" role="status">
      正在核对临时批次…
    </p>

    <Alert v-if="errorMessage" tone="danger" title="资料操作未完成">
      {{ errorMessage }}
      <template v-if="!config" #actions
        ><Button
          size="sm"
          variant="secondary"
          :disabled="busy || disabled"
          @click="loadResourceConfig"
          >重试加载配置</Button
        ></template
      >
    </Alert>
    <Alert v-if="hasUnknown" tone="warning" title="资料结果未确认">
      请求可能仍在服务端处理。请先核对批次；核对中没有该成员不能证明上传失败，不会自动重传。 ZIP
      的整体结果不能仅凭成员列表确认。仍未确认时请取消本次临时批次，而不是提交缺资料项目。
      <template #actions
        ><Button
          size="sm"
          variant="secondary"
          :disabled="busy || disabled || !batch"
          @click="checkCurrentBatch"
          >核对批次</Button
        ></template
      >
    </Alert>
    <p v-if="notice" class="resource-batch-status" role="status">{{ notice }}</p>

    <div v-if="uploads.length" class="resource-pending-list" aria-label="尚未加入临时批次的资料">
      <div v-for="item in uploads" :key="item.id" class="resource-pending-item">
        <div class="resource-pending-description">
          <strong>{{ item.file.name }}</strong>
          <span
            >{{ formatProjectResourceSize(item.file.size) }} ·
            {{ item.mode === 'zip' ? 'ZIP 导入' : '本地文件' }} ·
            {{ uploadStateLabel(item.state) }}</span
          >
          <p v-if="item.error" class="resource-pending-error">{{ item.error }}</p>
        </div>
        <div class="resource-pending-actions">
          <Button
            v-if="item.state === 'waiting' || item.state === 'failed'"
            size="sm"
            variant="secondary"
            :disabled="controlsDisabled"
            @click="retryPending(item)"
            >{{ item.state === 'waiting' ? '上传' : '补传' }}</Button
          >
          <Button
            size="sm"
            variant="ghost"
            :disabled="busy || disabled || item.state === 'unknown'"
            @click="skipPending(item)"
            >移除，不参与创建</Button
          >
        </div>
      </div>
    </div>

    <div v-if="batch?.members.length" class="resource-batch-members">
      <div v-for="member in batch.members" :key="member.id" class="resource-batch-member">
        <div class="resource-member-summary">
          <strong class="mono">{{ member.path }}</strong>
          <span class="resource-member-meta"
            >{{ formatProjectResourceSize(member.size_bytes) }} ·
            {{ member.source === 'zip' ? 'ZIP 成员' : '本地文件' }} · 已加入临时批次</span
          >
          <IconButton
            label="移除待导入资料"
            size="sm"
            variant="ghost"
            :disabled="controlsDisabled"
            @click="removeMember(member)"
            ><X :size="14"
          /></IconButton>
        </div>
        <details class="resource-member-details">
          <summary>类型与路径</summary>
          <div class="resource-member-fields">
            <Select
              :model-value="member.kind"
              :options="kindOptions"
              :disabled="controlsDisabled"
              :aria-label="`资源类型 ${member.path}`"
              @update:model-value="changeKind(member, $event)"
            />
            <Input
              v-model="memberPaths[member.id]"
              class="mono"
              :disabled="controlsDisabled"
              aria-label="资源相对路径"
              @blur="savePath(member)"
            />
          </div>
          <span class="resource-member-meta"
            >{{ projectResourceKindLabel(member.kind) }} · 相对路径保留目录结构</span
          >
        </details>
      </div>
    </div>
    <EmptyState
      v-else-if="!uploads.length"
      title="未添加辅助资料"
    />

    <div v-if="config" class="resource-batch-usage mono">
      <span>{{ usedCount }} / {{ config.limits.max_active_count }} 个文件</span>
      <span
        >{{ formatProjectResourceSize(usedBytes) }} /
        {{ formatProjectResourceSize(config.limits.max_active_total_bytes) }}</span
      >
      <span>单文件上限 {{ formatProjectResourceSize(config.limits.max_file_bytes) }}</span>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { Archive, FilePlus, Upload, X } from '@lucide/vue'
import type {
  ProjectResourceBatch,
  ProjectResourceBatchMember,
  ProjectResourceKind,
} from '@/api/types'
import { Alert, Button, EmptyState, IconButton, Input, Select } from '@/components/ui'
import {
  formatProjectResourceSize,
  projectResourceKindLabel,
  projectResourceKindOptions,
} from './projectResources'
import { useProjectResourceBatch, type PendingResourceUpload } from './useProjectResourceBatch'

const props = withDefaults(
  defineProps<{
    modelValue: ProjectResourceBatch | null
    disabled?: boolean
    existingCount?: number
    existingBytes?: number
    existingPaths?: string[]
  }>(),
  { disabled: false, existingCount: 0, existingBytes: 0, existingPaths: () => [] },
)

const emit = defineEmits<{
  'update:modelValue': [batch: ProjectResourceBatch | null]
  busy: [busy: boolean]
  pending: [pending: boolean]
  summary: [summary: string]
}>()

const fileInput = ref<HTMLInputElement | null>(null)
const zipInput = ref<HTMLInputElement | null>(null)
const memberPaths = reactive<Record<string, string>>({})
const dragging = ref(false)
const errorMessage = ref('')
const notice = ref('')
const {
  config,
  batch,
  operation,
  busy,
  uploads,
  pending: pendingOperations,
  hasUnknown,
  syncBatch,
  loadConfig,
  uploadFiles,
  importZip,
  retryUpload,
  removeUpload,
  checkBatch,
  updateMember,
  deleteMember,
  cancel: cancelBatch,
} = useProjectResourceBatch(props.modelValue, (nextBatch) => emit('update:modelValue', nextBatch))

// 路径在 blur 后才保存；未保存的编辑也须阻止创建，避免快捷键提交旧路径。
const pending = computed(
  () =>
    pendingOperations.value ||
    Boolean(
      batch.value?.members.some(
        (member) => memberPaths[member.id] !== undefined && memberPaths[member.id] !== member.path,
      ),
    ),
)
const controlsDisabled = computed(
  () => props.disabled || busy.value || hasUnknown.value || !config.value,
)
const kindOptions = computed(() => projectResourceKindOptions(config.value))
const usedCount = computed(() => props.existingCount + (batch.value?.resource_count || 0))
const usedBytes = computed(() => props.existingBytes + (batch.value?.total_bytes || 0))

// 折叠摘要直接来自上传会话，不把 File、未知请求和路径草稿复制到父组件。
const summary = computed(() => {
  const failed = uploads.value.filter((item) => item.state === 'failed').length
  const waiting = uploads.value.filter((item) => item.state === 'waiting').length
  const unsaved =
    batch.value?.members.filter(
      (member) => memberPaths[member.id] !== undefined && memberPaths[member.id] !== member.path,
    ).length || 0
  return [
    `本地文件 ${(batch.value?.resource_count || 0) + uploads.value.length}`,
    busy.value ? uploadOperationLabel(operation.value) : '',
    failed ? `失败 ${failed} 项` : '',
    waiting ? `待上传 ${waiting} 项` : '',
    hasUnknown.value ? '结果未确认' : '',
    unsaved ? `未保存路径 ${unsaved} 项` : '',
    errorMessage.value && !failed && !hasUnknown.value ? '资料操作未完成' : '',
  ]
    .filter(Boolean)
    .join(' · ')
})
const uploadOperationLabel = (value: typeof operation.value) =>
  ({
    'loading-config': '正在加载配置',
    'uploading-files': '上传中',
    'importing-zip': 'ZIP 导入中',
    'checking-batch': '正在核对批次',
    'updating-member': '正在保存资料修改',
    'deleting-member': '正在移除资料',
    cancelling: '正在取消批次',
    '': '',
  })[value]
watch(summary, (value) => emit('summary', value), { immediate: true })
watch(busy, (value) => emit('busy', value), { immediate: true, flush: 'sync' })
watch(pending, (value) => emit('pending', value), { immediate: true, flush: 'sync' })
watch(() => props.modelValue, syncBatch)
watch(
  () => batch.value?.members,
  (members) => {
    for (const member of members || []) memberPaths[member.id] = member.path
  },
  { immediate: true },
)

const uploadStateLabel = (state: PendingResourceUpload['state']) =>
  ({
    waiting: '待上传',
    uploading: '上传中',
    failed: '上传失败',
    unknown: '结果未确认',
  })[state]

const showError = (error: unknown, fallback: string) => {
  errorMessage.value = error instanceof Error && error.message ? error.message : fallback
}

const checkCurrentBatch = async () => {
  if (busy.value || props.disabled) return
  errorMessage.value = ''
  try {
    await checkBatch()
    notice.value = '已核对临时批次，确认成功的成员已保留。'
  } catch (error) {
    showError(error, '核对临时批次失败')
  }
}

const finishFailedUpload = async (error: unknown, fallback: string) => {
  showError(error, fallback)
  if (hasUnknown.value && batch.value) await checkCurrentBatch()
}

const addFiles = async (files: File[]) => {
  if (controlsDisabled.value || !files.length) return
  errorMessage.value = ''
  notice.value = ''
  try {
    await uploadFiles(files, props.existingCount, props.existingBytes, props.existingPaths)
  } catch (error) {
    await finishFailedUpload(error, '上传辅助资料失败')
  }
}

const handleFiles = async (event: Event) => {
  const input = event.target as HTMLInputElement
  const files = Array.from(input.files || [])
  input.value = ''
  await addFiles(files)
}

const handleDrop = async (event: DragEvent) => {
  dragging.value = false
  await addFiles(Array.from(event.dataTransfer?.files || []))
}

const handleZip = async (event: Event) => {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  input.value = ''
  if (controlsDisabled.value || !file) return
  errorMessage.value = ''
  notice.value = ''
  try {
    await importZip(file)
  } catch (error) {
    await finishFailedUpload(error, 'ZIP 导入失败')
  }
}

const retryPending = async (item: PendingResourceUpload) => {
  if (controlsDisabled.value) return
  errorMessage.value = ''
  notice.value = ''
  try {
    await retryUpload(item, props.existingCount, props.existingBytes, props.existingPaths)
  } catch (error) {
    await finishFailedUpload(error, '补传辅助资料失败')
  }
}

const skipPending = (item: PendingResourceUpload) => {
  if (props.disabled || busy.value || item.state === 'unknown') return
  removeUpload(item)
  notice.value = `已移除 ${item.file.name}，该文件未加入批次，不会参与创建。`
  if (!pending.value) errorMessage.value = ''
}

const changeKind = async (member: ProjectResourceBatchMember, value: string | number) => {
  if (controlsDisabled.value) return
  errorMessage.value = ''
  try {
    await updateMember(member, { kind: String(value) as ProjectResourceKind })
  } catch (error) {
    showError(error, '更新资源类型失败')
  }
}

const savePath = async (member: ProjectResourceBatchMember) => {
  if (controlsDisabled.value) return
  const path = memberPaths[member.id]?.trim() || ''
  if (path === member.path) {
    memberPaths[member.id] = member.path
    return
  }
  errorMessage.value = ''
  try {
    await updateMember(member, { path })
  } catch (error) {
    if (!hasUnknown.value) memberPaths[member.id] = member.path
    showError(error, '更新资源路径失败')
  }
}

const removeMember = async (member: ProjectResourceBatchMember) => {
  if (controlsDisabled.value) return
  errorMessage.value = ''
  try {
    await deleteMember(member)
  } catch (error) {
    showError(error, '移除辅助资料失败')
  }
}

const loadResourceConfig = async () => {
  errorMessage.value = ''
  try {
    await loadConfig()
  } catch (error) {
    showError(error, '加载资源配置失败')
  }
}

const cancel = async () => {
  await cancelBatch()
  errorMessage.value = ''
  notice.value = ''
}

onMounted(loadResourceConfig)
defineExpose({ cancel })
</script>

<style scoped>
.resource-batch-uploader {
  display: grid;
  gap: var(--space-3);
}
.resource-file-input {
  display: none;
}
.resource-batch-buttons,
.resource-pending-actions {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--space-2);
}
.resource-drop-zone {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  padding: var(--space-3);
  border: 1px dashed var(--border);
  border-radius: var(--radius-md);
  color: var(--text-muted);
  font-size: var(--text-sm);
}
.resource-drop-zone.is-dragging {
  border-color: var(--accent);
  background: var(--accent-dim);
}
.resource-drop-zone svg {
  flex-shrink: 0;
}
.resource-batch-status {
  margin: 0;
  color: var(--text-muted);
  font-size: var(--text-sm);
}
.resource-batch-members,
.resource-pending-list {
  display: grid;
  gap: var(--space-2);
}
.resource-batch-member,
.resource-pending-item {
  min-width: 0;
  padding: var(--space-3);
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
}
.resource-member-summary {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  align-items: center;
  gap: var(--space-1) var(--space-2);
}
.resource-member-summary strong {
  overflow-wrap: anywhere;
  font-size: var(--text-sm);
}
.resource-member-summary .resource-member-meta {
  grid-column: 1;
}
.resource-member-summary :deep(.ui-icon-button) {
  grid-column: 2;
  grid-row: 1 / 3;
}
.resource-member-meta,
.resource-batch-usage,
.resource-pending-description span {
  color: var(--text-muted);
  font-size: var(--text-xs);
}
.resource-member-details {
  margin-top: var(--space-2);
}
.resource-member-details summary {
  color: var(--text-muted);
  font-size: var(--text-xs);
  cursor: pointer;
}
.resource-member-fields {
  display: grid;
  grid-template-columns: 8rem minmax(0, 1fr);
  gap: var(--space-2);
  margin: var(--space-2) 0;
}
.resource-batch-usage {
  display: flex;
  flex-wrap: wrap;
  justify-content: space-between;
  gap: var(--space-2);
}
.resource-pending-item {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: var(--space-3);
}
.resource-pending-description {
  min-width: 0;
  display: grid;
  gap: var(--space-1);
  overflow-wrap: anywhere;
}
.resource-pending-description strong {
  font-size: var(--text-sm);
}
.resource-pending-error {
  margin: 0;
  color: var(--danger);
  font-size: var(--text-xs);
}
.resource-pending-actions {
  flex-shrink: 0;
}
@media (max-width: 760px) {
  .resource-member-fields {
    grid-template-columns: 1fr;
  }
  .resource-pending-item {
    flex-direction: column;
  }
}
</style>
