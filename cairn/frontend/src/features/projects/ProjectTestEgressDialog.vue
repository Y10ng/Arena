<template>
  <Dialog :open="true" title="代理设置" description="配置后续新执行使用的出口，不改变已经运行的 scope。" size="sm" :dismissible="!saving" :close-on-outside="false" @update:open="!$event && close()">
    <TestEgressFields v-model="draft" :auth-configured="current?.auth_configured" :disabled="saving" :error="validationError" @update:model-value="dirty = true; saved = false" />
    <Alert v-if="error" tone="danger" title="代理设置保存未确认">{{ error }}</Alert>
    <p v-if="saved" role="status">代理设置已保存；尚未验证代理连接。</p>
    <template #footer>
      <p class="egress-save-boundary">仅影响后续新执行；已经运行的 scope 保持原出口，不会暂停或恢复项目。</p>
      <Button variant="secondary" :disabled="saving" @click="close">关闭</Button>
      <Button :loading="saving" @click="save">保存代理设置</Button>
    </template>
  </Dialog>
</template>
<script setup lang="ts">
import { onBeforeUnmount, ref } from 'vue'
import { onBeforeRouteLeave, onBeforeRouteUpdate } from 'vue-router'
import { Alert, Button, Dialog } from '@/components/ui'
import TestEgressFields from '@/components/patterns/TestEgressFields.vue'
import { updateProjectTestEgress, type TestEgressPublic } from '@/api/testEgress'
import { testEgressDraft, testEgressError, testEgressInput } from './testEgressForm'
const props = defineProps<{ projectId: string; value?: TestEgressPublic }>()
const emit = defineEmits<{ close: []; saved: [value: TestEgressPublic] }>()
const current = ref(props.value)
const draft = ref(testEgressDraft(props.value))
const dirty = ref(false)
const saving = ref(false)
const saved = ref(false)
const error = ref('')
const validationError = ref('')
function canLeave() { return !saving.value && (!dirty.value || window.confirm('放弃未保存的代理设置？')) }
function close() { if (canLeave()) emit('close') }
onBeforeRouteLeave(canLeave)
onBeforeRouteUpdate(canLeave)
function beforeUnload(event: BeforeUnloadEvent) { if (dirty.value || saving.value) { event.preventDefault(); event.returnValue = '' } }
window.addEventListener('beforeunload', beforeUnload)
onBeforeUnmount(() => window.removeEventListener('beforeunload', beforeUnload))
async function save() {
  if (saving.value) return
  validationError.value = testEgressError(draft.value)
  if (validationError.value) return
  saving.value = true; error.value = ''; saved.value = false
  try {
    const value = await updateProjectTestEgress(props.projectId, testEgressInput(draft.value))
    current.value = value; draft.value = testEgressDraft(value); dirty.value = false; saved.value = true
    emit('saved', value)
  } catch {
    // 不把可能含请求输入的错误原文带到提示中，失败保留内存输入供核对。
    error.value = '未确认保存成功，请重新读取项目核对，或重试保存；当前输入已保留。'
  } finally { saving.value = false }
}
</script>
<style scoped>
.egress-save-boundary { flex-basis: 100%; margin: 0; color: var(--text-muted); font-size: var(--text-sm); line-height: var(--leading-normal); }
</style>
