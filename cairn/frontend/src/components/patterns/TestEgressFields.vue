<template>
  <fieldset class="test-egress-fields" :disabled="disabled" aria-label="代理设置">
    <FormField label="连接方式" :for="`${id}-mode`">
      <Select :id="`${id}-mode`" :model-value="modelValue.mode" :options="modeOptions" :disabled="disabled" @update:model-value="setMode" />
    </FormField>
    <template v-if="modelValue.mode !== 'direct'">
      <div class="test-egress-grid">
        <FormField label="代理地址" :for="`${id}-host`" required><Input :id="`${id}-host`" :model-value="modelValue.host" autocomplete="off" placeholder="proxy.example.test" @update:model-value="update({ host: String($event ?? '') })" /></FormField>
        <FormField label="代理端口" :for="`${id}-port`" required><Input :id="`${id}-port`" :model-value="modelValue.port" type="number" :min="1" :max="65535" :step="1" @update:model-value="update({ port: $event === null ? null : Number($event) })" /></FormField>
      </div>
      <p class="test-egress-note">{{ authConfigured ? '认证已配置' : '未配置认证' }}</p>
      <FormField label="代理认证" :for="`${id}-auth`"><Select :id="`${id}-auth`" :model-value="modelValue.auth_action" :options="authOptions" :disabled="disabled" @update:model-value="setAuth" /></FormField>
      <div v-if="modelValue.auth_action === 'replace'" class="test-egress-grid">
        <FormField label="代理用户名" :for="`${id}-username`" required><Input :id="`${id}-username`" :model-value="modelValue.username" autocomplete="off" @update:model-value="update({ username: String($event ?? '') })" /></FormField>
        <FormField label="代理密码" :for="`${id}-password`" required><Input :id="`${id}-password`" :model-value="modelValue.password" type="password" autocomplete="new-password" @update:model-value="update({ password: String($event ?? '') })" /></FormField>
      </div>
      <p class="test-egress-note">TCP 经所选代理；代理不可用时不回退直连。UDP／QUIC 与原生 IPv6 连接暂拒绝。</p>
    </template>
    <p v-if="error" class="test-egress-error" role="alert">{{ error }}</p>
  </fieldset>
</template>
<script setup lang="ts">
import { computed, useId } from 'vue'
import { FormField, Input, Select } from '@/components/ui'
import type { TestEgressDraft } from '@/features/projects/testEgressForm'
const props = defineProps<{ modelValue: TestEgressDraft; authConfigured?: boolean; disabled?: boolean; error?: string }>()
const emit = defineEmits<{ 'update:modelValue': [value: TestEgressDraft] }>()
const id = useId()
const modeOptions = [{ value: 'direct', label: '直接连接' }, { value: 'http', label: 'HTTP 代理' }, { value: 'socks5', label: 'SOCKS5 代理' }]
const authOptions = computed(() => [
  { value: 'keep', label: props.authConfigured ? '保留现有认证' : '不设置认证' },
  { value: 'replace', label: props.authConfigured ? '替换认证' : '设置认证' },
  { value: 'clear', label: '清除认证' },
])
function update(patch: Partial<TestEgressDraft>) { emit('update:modelValue', { ...props.modelValue, ...patch }) }
function setMode(value: string | number) {
  update({ mode: value as TestEgressDraft['mode'], ...(value === 'direct' ? { username: '', password: '', auth_action: 'clear' as const } : {}) })
}
function setAuth(value: string | number) { update({ auth_action: value as TestEgressDraft['auth_action'], username: '', password: '' }) }
</script>
<style scoped>
.test-egress-fields { min-width: 0; display: grid; gap: var(--space-3); padding: 0; margin: 0; border: 0; }
.test-egress-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: var(--space-3); }
.test-egress-note, .test-egress-error { margin: 0; font-size: var(--text-sm); line-height: var(--leading-normal); overflow-wrap: anywhere; }
.test-egress-note { color: var(--text-muted); }
.test-egress-error { color: var(--danger); }
@media (max-width: 600px) { .test-egress-grid { grid-template-columns: 1fr; } }
</style>
