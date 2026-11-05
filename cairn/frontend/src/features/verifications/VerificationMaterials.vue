<template>
  <section class="verification-materials verification-stack" aria-label="复测资料上传">
    <header class="verification-materials-toolbar">
      <h3>添加本轮文件</h3>
      <div class="verification-actions">
        <label class="verification-file-picker" :class="{ 'is-disabled': disabled || busy }">选择文件<input type="file" multiple aria-label="选择文件" :disabled="disabled || busy" @change="selectFiles" /></label>
        <label class="verification-file-picker" :class="{ 'is-disabled': disabled || busy }">选择目录<input type="file" multiple webkitdirectory aria-label="选择目录" :disabled="disabled || busy" @change="selectFiles" /></label>
      </div>
    </header>
    <Alert v-if="message" tone="warning" title="资料操作提示">{{ message }}</Alert>
    <section v-if="queue.length" class="verification-material-group" aria-label="待上传文件">
      <h3>待上传 · {{ queue.length }}</h3>
      <ul class="verification-file-list">
        <li v-for="entry in queue" :key="entry.id" class="verification-file-row">
          <div class="verification-file-content">
            <label :for="`verification-file-path-${entry.id}`">{{ entry.file.name }}<span class="verification-muted">{{ formatProjectResourceSize(entry.file.size) }}</span></label>
            <Input :id="`verification-file-path-${entry.id}`" v-model="entry.path" :aria-label="`${entry.file.name} 的相对路径`" :disabled="disabled || busy || entry.attempted" />
            <p :class="entry.error ? 'verification-file-error' : 'verification-muted'" :role="entry.error ? 'alert' : undefined">{{ entry.error || '尚未上传' }}</p>
          </div>
          <div class="verification-file-actions">
            <Button size="sm" variant="secondary" :disabled="disabled || busy" @click="upload(entry)">{{ entry.attempted ? '按原路径重试' : '上传' }}</Button>
            <Button size="sm" variant="ghost" :disabled="disabled || busy" @click="removePending(entry.id)">移出待上传</Button>
          </div>
        </li>
      </ul>
    </section>
    <section v-if="batch" class="verification-material-group" aria-label="已准备文件">
      <header class="verification-materials-toolbar"><h3>已准备 · {{ batch.members.length }}</h3><Button size="sm" variant="ghost" :disabled="disabled || busy" @click="refresh">核对服务端批次</Button></header>
      <ul v-if="batch.members.length" class="verification-file-list">
        <li v-for="member in batch.members" :key="member.id" class="verification-file-row">
          <div class="verification-file-content">
            <span class="verification-file-name">{{ member.path }}</span>
            <p class="verification-muted">已落盘 · {{ formatProjectResourceSize(member.size_bytes) }}</p>
            <details><summary>文件摘要</summary><span class="verification-muted">SHA256 {{ member.sha256 }}</span></details>
          </div>
          <Button size="sm" variant="ghost" :disabled="disabled || busy || batch.state !== 'open'" @click="toDelete = member.id">移除</Button>
        </li>
      </ul>
      <details class="verification-batch-details">
        <summary>批次与有效期</summary>
        <p class="verification-muted">{{ batch.state === 'open' ? '待提交批次' : '已消费批次' }} · 到期 {{ dateTime(batch.expires_at) }}。请求受理后原件不随批次到期删除。</p>
        <Button size="sm" variant="secondary" :disabled="disabled || busy" @click="confirmAbandon = true">重新准备资料</Button>
      </details>
    </section>
    <p v-if="queue.length || batch?.members.length" class="verification-muted">目录保留相对路径。上传成功不代表已理解资料，DOCX 与截图以实际执行结果为准。</p>
    <EmptyState v-if="!queue.length && !batch?.members.length" title="暂无文件资料" description="内联 Markdown／文字不需要上传；文件主资料或附件可在此添加。" />
    <ConfirmAction content-class="retest-material-confirm" overlay-class="retest-material-confirm-overlay" :open="Boolean(toDelete)" title="移除待提交资料" description="只从本轮尚未消费的批次移除，不改变历史复测原件。" confirm-label="移除" :loading="busy" @update:open="!$event && (toDelete = '')" @confirm="deleteMember" />
    <ConfirmAction content-class="retest-material-confirm" overlay-class="retest-material-confirm-overlay" :open="confirmAbandon" title="重新准备本轮资料" description="请求取消未消费的批次并放弃当前引用，不删除历史原件。已上传文件须重新选择，或再次明确复制上一轮原件。尚未上传的文件保留；取消失败会显示原因，未消费的临时资料按有效期处理。" confirm-label="取消批次，重新准备" tone="warning" :loading="busy" @update:open="confirmAbandon = $event" @confirm="abandonBatch" />
  </section>
</template>
<script lang="ts">
export interface PendingVerificationFile { id: number; file: File; path: string; attempted: boolean; error: string }
</script>
<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { Alert, Button, EmptyState, Input } from '@/components/ui'
import { ConfirmAction } from '@/components/patterns'
import { cancelVerificationBatch, createVerificationBatch, deleteVerificationBatchMember, getVerificationBatch, uploadVerificationFile, type VerificationBatch } from '@/api/verificationRequests'
import { formatProjectResourceSize, inferProjectResourceKind, projectResourceFilePath, projectResourcePathError } from '@/features/projects/projectResources'
import { dateTime } from '@/lib/format'
import { errorMessage } from './verificationPresentation'
const props = defineProps<{ modelValue: VerificationBatch | null; files: PendingVerificationFile[]; disabled: boolean }>()
const emit = defineEmits<{
  (event: 'update:modelValue', value: VerificationBatch | null): void
  (event: 'update:files', value: PendingVerificationFile[]): void
  (event: 'busy', value: boolean): void
  (event: 'pending', value: boolean): void
  (event: 'confirming', value: boolean): void
}>()
const batch = computed(() => props.modelValue)
// File 与逐项失败信息由表单持有，资料浮层收起后仍可按原路径重试。
const queue = computed({ get: () => props.files, set: (value) => emit('update:files', value) })
const busy = ref(false)
const message = ref('')
const toDelete = ref('')
const confirmAbandon = ref(false)
let nextId = Math.max(0, ...props.files.map((file) => file.id))
watch(busy, (value) => emit('busy', value), { flush: 'sync' })
watch(() => Boolean(toDelete.value || confirmAbandon.value), (value) => emit('confirming', value), { flush: 'sync' })
watch(() => queue.value.length, (value) => emit('pending', value > 0), { flush: 'sync' })
function selectFiles(event: Event) {
  const input = event.target as HTMLInputElement
  for (const file of Array.from(input.files || [])) {
    const path = projectResourceFilePath(file)
    queue.value.push({ id: ++nextId, file, path, attempted: false, error: '' })
  }
  input.value = ''
}
function removePending(id: number) {
  queue.value = queue.value.filter((entry) => entry.id !== id)
}
async function abandonBatch() {
  if (props.disabled || busy.value || !batch.value) return
  busy.value = true
  try {
    await cancelVerificationBatch(batch.value.id)
    message.value = '待提交批次已取消。请重新上传或复制本轮需要的资料。'
  } catch (error) {
    message.value = `取消批次未确认，仅放弃本页引用；服务端若仍保留临时资料，将按有效期处理。${errorMessage(error)}`
  } finally {
    emit('update:modelValue', null)
    confirmAbandon.value = false
    busy.value = false
  }
}
async function upload(entry: PendingVerificationFile) {
  if (props.disabled || busy.value) return
  const pathError = projectResourcePathError(entry.path)
  if (pathError) { entry.error = pathError; return }
  if (batch.value?.members.some((member) => member.path === entry.path)) {
    entry.error = '服务端已有同路径成员，请核对后移出待上传项；不会覆盖原文件。'
    return
  }
  busy.value = true
  message.value = ''
  try {
    const current = batch.value || await createVerificationBatch()
    emit('update:modelValue', current)
    entry.attempted = true
    const member = await uploadVerificationFile(current.id, entry.file, entry.path, inferProjectResourceKind(entry.path))
    emit('update:modelValue', {
      ...current, members: [...current.members, member],
      resource_count: current.resource_count + 1, total_bytes: current.total_bytes + member.size_bytes,
    })
    removePending(entry.id)
  } catch (error) {
    entry.error = errorMessage(error)
    message.value = '上传结果可能未确认。请核对服务端批次；如已有该文件，可核对路径和摘要后移出待上传项。不会自动重复上传。'
  } finally { busy.value = false }
}
async function refresh() {
  if (!batch.value || props.disabled || busy.value) return
  busy.value = true
  try { emit('update:modelValue', await getVerificationBatch(batch.value.id)); message.value = '' }
  catch (error) { message.value = errorMessage(error) }
  finally { busy.value = false }
}
async function deleteMember() {
  if (!batch.value || !toDelete.value || props.disabled || busy.value) return
  busy.value = true
  try {
    await deleteVerificationBatchMember(batch.value.id, toDelete.value)
    toDelete.value = ''
    emit('update:modelValue', await getVerificationBatch(batch.value.id))
    message.value = ''
  } catch (error) { message.value = `移除结果未确认，请核对服务端批次。${errorMessage(error)}` }
  finally { busy.value = false }
}
</script>

<style scoped>
.verification-materials { display: flex; flex-direction: column; min-width: 0; gap: var(--space-4); }
.verification-materials h3 { margin: 0; font-size: var(--text-section); font-weight: var(--font-semibold); }
.verification-muted { margin: 0; color: var(--text-muted); font-size: var(--text-sm); overflow-wrap: anywhere; }
.verification-materials-toolbar, .verification-actions { display: flex; flex-wrap: wrap; align-items: center; gap: var(--space-2); }
.verification-materials-toolbar { justify-content: space-between; gap: var(--space-3); }
.verification-file-picker { position: relative; display: inline-flex; align-items: center; min-height: var(--control-height-sm); padding: var(--space-1) var(--space-3); border: 1px solid var(--border-strong); border-radius: var(--radius-md); color: var(--text); font-size: var(--text-control); overflow: hidden; }
.verification-file-picker:hover { background: var(--surface-hover); }
.verification-file-picker input { position: absolute; inset: 0; width: 100%; opacity: 0; cursor: pointer; }
.verification-file-picker.is-disabled { opacity: 0.5; }
.verification-file-picker input:disabled { cursor: not-allowed; }
.verification-file-picker:focus-within { outline: 2px solid var(--accent); outline-offset: 2px; }
.verification-material-group { display: grid; gap: var(--space-3); min-width: 0; }
.verification-file-list { margin: 0; padding: 0; list-style: none; border-top: 1px solid var(--border-subtle); }
.verification-file-row { display: flex; align-items: flex-start; gap: var(--space-4); min-width: 0; padding-block: var(--space-4); border-bottom: 1px solid var(--border-subtle); }
.verification-file-content { display: grid; flex: 1; gap: var(--space-2); min-width: 0; overflow-wrap: anywhere; }
.verification-file-content > label { display: flex; flex-wrap: wrap; justify-content: space-between; gap: var(--space-2); }
.verification-file-name { font-weight: var(--font-medium); }
.verification-file-actions { display: grid; justify-items: end; gap: var(--space-2); }
.verification-file-error { margin: 0; color: var(--danger); font-size: var(--text-sm); white-space: pre-wrap; }
.verification-materials summary { cursor: pointer; color: var(--text-muted); font-size: var(--text-sm); padding-block: var(--space-2); }
.verification-materials summary:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
.verification-batch-details > p { margin-bottom: var(--space-3); }
@media (max-width: 640px) { .verification-file-row { flex-wrap: wrap; } .verification-file-content { flex-basis: 100%; } .verification-file-actions { display: flex; flex-wrap: wrap; } }
</style>
<style>
.retest-material-confirm.ui-alert-dialog__content, .retest-material-confirm-overlay.ui-alert-dialog__overlay { z-index: var(--z-overlay-child); }
</style>
