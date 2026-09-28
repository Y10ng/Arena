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
      <div>
        <strong>待导入资料</strong>
        <p>可上传 PDF、图片、Office、脚本及其他任意文件；普通 ZIP 也可作为单个文件保存。</p>
      </div>
      <div class="resource-batch-buttons">
        <Button
          size="sm"
          variant="secondary"
          :disabled="controlsDisabled"
          @click="fileInput?.click()"
        >
          添加文件
        </Button>
        <Button
          size="sm"
          variant="secondary"
          :disabled="controlsDisabled"
          @click="zipInput?.click()"
        >
          导入 ZIP
        </Button>
      </div>
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

    <div v-if="batch?.members.length" class="resource-batch-members">
      <div v-for="member in batch.members" :key="member.id" class="resource-batch-member">
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
        <span class="resource-member-meta">
          {{ projectResourceKindLabel(member.kind) }} ·
          {{ formatProjectResourceSize(member.size_bytes) }} ·
          {{ member.source === 'zip' ? 'ZIP 成员' : '文件' }}
        </span>
        <IconButton
          label="移除待导入资料"
          size="sm"
          variant="ghost"
          :disabled="controlsDisabled"
          @click="removeMember(member)"
          >×</IconButton
        >
      </div>
    </div>
    <EmptyState
      v-else
      title="未添加辅助资料"
      description="文件会先进入临时批次，提交项目时再原子加入。"
    />

    <div v-if="config" class="resource-batch-usage mono">
      <span>{{ usedCount }} / {{ config.limits.max_active_count }} 个文件</span>
      <span>
        {{ formatProjectResourceSize(usedBytes) }} /
        {{ formatProjectResourceSize(config.limits.max_active_total_bytes) }}
      </span>
      <span>单文件上限 {{ formatProjectResourceSize(config.limits.max_file_bytes) }}</span>
    </div>
    <p class="resource-batch-guidance">
      “导入 ZIP”会在服务器安全展开并保留相对目录；ZIP
      内嵌归档保持为普通文件，脚本和二进制文件不会自动执行。
    </p>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from 'vue'
import type {
  ProjectResourceBatch,
  ProjectResourceBatchMember,
  ProjectResourceKind,
} from '@/api/types'
import { Button, EmptyState, IconButton, Input, Select } from '@/components/ui'
import { useUIStore } from '@/stores/ui'
import {
  formatProjectResourceSize,
  projectResourceKindLabel,
  projectResourceKindOptions,
} from './projectResources'
import { useProjectResourceBatch } from './useProjectResourceBatch'

const props = withDefaults(
  defineProps<{
    modelValue: ProjectResourceBatch | null
    disabled?: boolean
    existingCount?: number
    existingBytes?: number
    existingPaths?: string[]
  }>(),
  {
    disabled: false,
    existingCount: 0,
    existingBytes: 0,
    existingPaths: () => [],
  },
)

const emit = defineEmits<{
  'update:modelValue': [batch: ProjectResourceBatch | null]
  busy: [busy: boolean]
}>()

const uiStore = useUIStore()
const fileInput = ref<HTMLInputElement | null>(null)
const zipInput = ref<HTMLInputElement | null>(null)
const memberPaths = reactive<Record<string, string>>({})
const {
  config,
  batch,
  operation,
  busy,
  loadConfig,
  uploadFiles,
  importZip,
  updateMember,
  deleteMember,
  cancel,
} = useProjectResourceBatch(props.modelValue, (nextBatch) => emit('update:modelValue', nextBatch))

const controlsDisabled = computed(() => props.disabled || busy.value || !config.value)
const kindOptions = computed(() => projectResourceKindOptions(config.value))
const usedCount = computed(() => props.existingCount + (batch.value?.resource_count || 0))
const usedBytes = computed(() => props.existingBytes + (batch.value?.total_bytes || 0))

watch(busy, (value) => emit('busy', value), { immediate: true })
watch(
  () => batch.value?.members,
  (members) => {
    for (const member of members || []) memberPaths[member.id] = member.path
  },
  { immediate: true },
)

const showError = (error: unknown, fallback: string) => {
  uiStore.showToast(error instanceof Error && error.message ? error.message : fallback, 'danger')
}

const handleFiles = async (event: Event) => {
  const input = event.target as HTMLInputElement
  const files = Array.from(input.files || [])
  input.value = ''
  if (!files.length) return
  try {
    await uploadFiles(files, props.existingCount, props.existingBytes, props.existingPaths)
  } catch (error) {
    showError(error, '上传辅助资料失败')
  }
}

const handleZip = async (event: Event) => {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  input.value = ''
  if (!file) return
  try {
    await importZip(file)
  } catch (error) {
    showError(error, 'ZIP 导入失败')
  }
}

const changeKind = async (member: ProjectResourceBatchMember, value: string | number) => {
  try {
    await updateMember(member, { kind: String(value) as ProjectResourceKind })
  } catch (error) {
    showError(error, '更新资源类型失败')
  }
}

const savePath = async (member: ProjectResourceBatchMember) => {
  const path = memberPaths[member.id]?.trim() || ''
  if (path === member.path) return
  try {
    await updateMember(member, { path })
  } catch (error) {
    memberPaths[member.id] = member.path
    showError(error, '更新资源路径失败')
  }
}

const removeMember = async (member: ProjectResourceBatchMember) => {
  try {
    await deleteMember(member)
  } catch (error) {
    showError(error, '移除辅助资料失败')
  }
}

onMounted(async () => {
  try {
    await loadConfig()
  } catch (error) {
    showError(error, '加载资源配置失败')
  }
})

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

.resource-batch-actions,
.resource-batch-usage,
.resource-batch-member {
  display: flex;
  align-items: center;
  gap: var(--space-3);
}

.resource-batch-actions {
  justify-content: space-between;
}

.resource-batch-actions p,
.resource-batch-guidance {
  margin: var(--space-1) 0 0;
  color: var(--color-text-secondary);
  font-size: var(--font-size-sm);
}

.resource-batch-buttons {
  display: flex;
  flex: 0 0 auto;
  gap: var(--space-2);
}

.resource-batch-status {
  margin: 0;
  color: var(--color-text-secondary);
}

.resource-batch-members {
  display: grid;
  gap: var(--space-2);
}

.resource-batch-member {
  display: grid;
  grid-template-columns: 9rem minmax(12rem, 1fr) auto auto;
  padding: var(--space-2);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
}

.resource-member-meta,
.resource-batch-usage {
  color: var(--color-text-secondary);
  font-size: var(--font-size-xs);
}

.resource-batch-usage {
  flex-wrap: wrap;
  justify-content: space-between;
}

@media (max-width: 760px) {
  .resource-batch-actions {
    align-items: flex-start;
    flex-direction: column;
  }

  .resource-batch-member {
    grid-template-columns: 1fr auto;
  }

  .resource-batch-member > :nth-child(2),
  .resource-member-meta {
    grid-column: 1 / -1;
  }
}
</style>
