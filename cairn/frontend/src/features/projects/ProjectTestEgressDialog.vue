<template>
  <Dialog
    :open="true"
    title="代理设置"
    description="仅影响后续新执行；已经运行的 scope 保持原出口，不会暂停或恢复项目。"
    :dismissible="!saving && !discardOpen"
    :close-on-outside="false"
    @update:open="!$event && close()"
  >
    <TestEgressFields
      v-model="draft"
      :auth-configured="current?.auth_configured"
      :disabled="saving || discardOpen"
      :error="validationError"
      @update:model-value="markDraftChanged"
    />
    <Alert v-if="error" tone="danger" title="代理设置保存未确认">{{ error }}</Alert>
    <Alert v-if="saved" tone="success">代理设置已保存；尚未验证代理连接。</Alert>
    <template #footer>
      <Button variant="secondary" :disabled="saving" @click="close">关闭</Button>
      <Button :loading="saving" :disabled="discardOpen" @click="save">保存代理设置</Button>
    </template>
    <ConfirmAction
      :open="discardOpen"
      title="放弃未保存的代理设置？"
      description="未保存的地址与认证输入将被丢弃，已保存的代理设置不会改变。"
      confirm-label="放弃修改"
      cancel-label="继续编辑"
      @confirm="resolveDiscard(true)"
      @update:open="!$event && resolveDiscard(false)"
    />
  </Dialog>
</template>

<script setup lang="ts">
import { onBeforeUnmount, ref } from 'vue'
import { onBeforeRouteLeave, onBeforeRouteUpdate } from 'vue-router'
import { Alert, Button, Dialog } from '@/components/ui'
import { ConfirmAction } from '@/components/patterns'
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
const discardOpen = ref(false)
let discardResolver: ((allowed: boolean) => void) | null = null
let discardReturnFocus: HTMLElement | null = null

function markDraftChanged() {
  dirty.value = true
  saved.value = false
}
function canLeave(): boolean | Promise<boolean> {
  if (saving.value || discardOpen.value) return false
  if (!dirty.value) return true
  discardReturnFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null
  discardOpen.value = true
  return new Promise<boolean>((resolve) => {
    discardResolver = resolve
  })
}
function resolveDiscard(allowed: boolean) {
  const resolve = discardResolver
  discardResolver = null
  discardOpen.value = false
  const returnFocus = discardReturnFocus
  discardReturnFocus = null
  // 等待确认层卸载，取消后继续在原代理字段编辑，不能把焦点落到页面背景。
  if (!allowed && returnFocus)
    requestAnimationFrame(() => {
      if (returnFocus.isConnected) returnFocus.focus()
    })
  resolve?.(allowed)
}
async function close() {
  if (await canLeave()) emit('close')
}
async function leaveRoute() {
  const allowed = await canLeave()
  if (allowed) emit('close')
  return allowed
}
onBeforeRouteLeave(leaveRoute)
onBeforeRouteUpdate(leaveRoute)
function beforeUnload(event: BeforeUnloadEvent) {
  if (dirty.value || saving.value) {
    event.preventDefault()
    event.returnValue = ''
  }
}
window.addEventListener('beforeunload', beforeUnload)
onBeforeUnmount(() => {
  window.removeEventListener('beforeunload', beforeUnload)
  // 父页被移除时结束未决确认，不能让路由误把未确认的 Promise 当作允许离开。
  resolveDiscard(false)
})
async function save() {
  if (saving.value || discardOpen.value) return
  validationError.value = testEgressError(draft.value)
  if (validationError.value) return
  saving.value = true
  error.value = ''
  saved.value = false
  try {
    const value = await updateProjectTestEgress(props.projectId, testEgressInput(draft.value))
    current.value = value
    draft.value = testEgressDraft(value)
    dirty.value = false
    saved.value = true
    emit('saved', value)
  } catch {
    // 不把可能含请求输入的错误原文带到提示中，失败保留内存输入供核对。
    error.value = '未确认保存成功，请重新读取项目核对，或重试保存；当前输入已保留。'
  } finally {
    saving.value = false
  }
}
</script>
