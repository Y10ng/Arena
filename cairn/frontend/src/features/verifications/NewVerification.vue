<template>
  <VerificationForm ref="form" :source-request-id="queryValue('source_request_id')" :source-project-id="queryValue('source_project_id')" :source-vulnerability-id="queryValue('source_vulnerability_id')" :source-observation-id="queryValue('source_observation_id')" :initial-input="queryValue('input') || 'platform'" @close="emit('close')" @accepted="enterCreated" />
</template>
<script setup lang="ts">
import { ref } from 'vue'
import { onBeforeRouteLeave, onBeforeRouteUpdate, useRoute, useRouter } from 'vue-router'
import type { VerificationDetail } from '@/api/verificationRequests'
import { useUIStore } from '@/stores/ui'
import VerificationForm from './VerificationForm.vue'
import { verificationListQuery } from './verificationPresentation'
const emit = defineEmits<{ (event: 'close'): void }>()
const route = useRoute()
const router = useRouter()
const ui = useUIStore()
const form = ref<InstanceType<typeof VerificationForm> | null>(null)
const queryValue = (key: string) => typeof route.query[key] === 'string' ? route.query[key] as string : ''
const canLeave = () => Boolean(form.value?.canLeave())
onBeforeRouteLeave(canLeave)
onBeforeRouteUpdate((to) => {
  const stillOpen = to.name === 'NewVerification' || to.query.create === '1'
  return (stillOpen && ['source_request_id', 'source_project_id', 'source_vulnerability_id', 'source_observation_id', 'input'].every((key) => to.query[key] === route.query[key])) || canLeave()
})
async function enterCreated(request: VerificationDetail) {
  try { await router.push({ name: 'VerificationDetail', params: { id: request.id }, query: verificationListQuery(route.query) }) }
  catch { ui.showToast('请求已受理，但跳转失败；请进入已受理记录，不要重复提交。', 'warn') }
}
</script>
