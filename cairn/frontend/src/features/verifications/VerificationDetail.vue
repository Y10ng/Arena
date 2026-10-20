<template>
  <section class="verification-theme verification-page verification-detail-page" aria-label="复测详情">
    <header class="verification-detail-heading">
      <Button variant="ghost" @click="backToList"><template #icon><ArrowLeft :size="16" /></template>复测列表</Button>
      <div class="verification-detail-title"><h1>{{ detail?.title || '复测详情' }}</h1><Badge v-if="detail" :tone="statusTone(detail.execution_status)">{{ statusLabels[detail.execution_status] }}</Badge></div>
      <div class="verification-actions">
        <Button variant="ghost" :loading="isFetching" aria-label="刷新复测详情" @click="refresh"><template #icon><RefreshCw :size="16" /></template></Button>
        <Button v-if="detail && detail.purge_state !== 'pending'" @click="openAgain">再次复测</Button>
        <VerificationActions v-if="detail" :key="detail.id" ref="requestActions" context="detail" :request="detail" @deleted="backToList" @error="actionError = $event" />
      </div>
    </header>
    <Skeleton v-if="isLoading" aria-label="正在加载复测详情" />
    <ErrorState v-else-if="isError && !detail" title="复测详情加载失败" :description="errorMessage(error)" @retry="refresh" />
    <template v-else-if="detail">
      <Alert v-if="isError" tone="warning" title="刷新失败">保留上次取得的信息，最新执行状态尚未确认。{{ errorMessage(error) }}</Alert>
      <Alert v-if="actionError" tone="danger" title="操作未完成">{{ actionError }}<template #actions><Button variant="ghost" @click="actionError = ''">关闭提示</Button></template></Alert>
      <Alert v-if="detail.purge_state === 'pending'" tone="warning" title="清理未完成，可重试"><p v-if="detail.purge_error" class="verification-text">{{ detail.purge_error }}</p><template #actions><Button variant="secondary" @click="requestActions?.retryCleanup()">重试清理</Button></template></Alert>
      <template v-else>
      <RouterLink v-if="detail.input_source" class="verification-source-link" :to="{ name: 'Findings', query: { vulnerabilityProjectId: detail.input_source.project_id, vulnerabilityId: detail.input_source.vulnerability_id } }">来源漏洞</RouterLink>
      <div v-if="isActiveVerification(detail.execution_status)" class="verification-activity" role="status">
        <LoaderCircle :size="18" /><div><p v-if="detail.execution_message" class="verification-text">{{ detail.execution_message }}</p><p v-if="detail.execution_status === 'cancelling'" class="verification-text">停止请求已接受，尚未确认执行结束。</p><p v-if="autoPaused" class="verification-text">自动更新已暂停，请手动刷新核对。</p></div>
        <Button variant="secondary" :disabled="cancelling || detail.execution_status === 'cancelling'" @click="confirmStop = true">停止本次执行</Button>
      </div>
      <Alert v-if="cancelError" tone="warning" title="停止请求结果未确认">{{ cancelError }}<p>不能据此认为执行已停止，请刷新核对。</p></Alert>
      <Alert v-if="detail.error" tone="danger" :title="detail.result ? '执行出现异常' : '本次执行错误，未形成业务结论'"><p class="verification-text">{{ detail.error.message }}</p><details><summary>错误详情</summary>{{ detail.error.code }}</details></Alert>
      <Alert v-if="detail.cleanup.error" tone="warning" title="运行环境回收异常"><details><summary>查看回收错误</summary><p class="verification-text">{{ detail.cleanup.error.message }}</p></details></Alert>
      <article v-if="detail.result" class="verification-reading" aria-label="复测结果">
        <section class="verification-report-section verification-conclusion">
          <h2>一、结论</h2>
          <div class="verification-section-body">
            <p class="verification-result-outcome">{{ outcomeLabels[detail.result.outcome] }}</p>
            <p class="verification-text verification-result-summary">{{ detail.result.summary }}</p>
            <p v-if="detail.result.tested_scope" class="verification-text">本次条件：{{ detail.result.tested_scope }}</p>
            <p v-for="(limitation, index) in detail.result.limitations" :key="index" class="verification-text">{{ limitation }}</p>
          </div>
        </section>
        <section class="verification-report-section verification-key-evidence">
          <h2>二、关键证据</h2>
          <div class="verification-section-body">
            <template v-if="detail.result.schema_version === 3">
              <ul v-if="reasons.length" class="verification-reasons"><li v-for="(evidence, index) in reasons" :key="index"><p class="verification-text">{{ evidence.reason }}</p><div v-if="evidence.targets.length" class="verification-evidence-links"><Button v-for="target in evidence.targets" :key="target.itemId" size="sm" variant="link" @click="goToEvidence(target.groupKey, target.itemId)">{{ target.title }} · {{ target.stepIndex === null ? '未关联步骤' : `步骤 ${target.stepIndex + 1}` }}</Button></div></li></ul>
              <p v-else class="verification-text">本次未记录可用于判断的关键证据。</p>
            </template>
            <p v-else class="verification-text">历史记录未单独记录判断依据。</p>
          </div>
        </section>
        <section class="verification-report-section verification-process">
          <h2>三、测试过程</h2>
          <div class="verification-section-body verification-section-body--process">
          <template v-if="detail.result.schema_version === 3">
            <p v-if="!steps.length" class="verification-text verification-process-empty">本次未记录已执行的测试步骤。</p>
            <div v-if="currentGroup" class="verification-process-layout" :class="{ 'verification-process-layout--single': readingGroups.length === 1 }">
              <template v-if="readingGroups.length > 1">
                <nav class="verification-step-navigation" :aria-label="unlinkedEvidence.length ? '测试步骤与证据' : '测试步骤'"><ol><li v-for="group in readingGroups" :key="group.key"><button type="button" :aria-current="selectedGroupKey === group.key ? group.stepIndex === null ? 'true' : 'step' : undefined" @click="selectGroup(group.key)"><span v-if="group.stepIndex !== null" class="verification-step-number">{{ group.stepIndex + 1 }}</span><span>{{ group.label }}</span></button></li></ol></nav>
                <div class="verification-step-select"><Select :model-value="selectedGroupKey" :options="groupOptions" aria-label="选择测试步骤或证据" @update:model-value="selectGroup($event)" /></div>
              </template>
              <div ref="currentStepPanel" class="verification-current-step" tabindex="-1" :aria-label="currentGroup.stepIndex === null ? '未关联步骤的证据' : '当前测试步骤'">
                <h3>{{ currentGroup.stepIndex === null ? currentGroup.label : `${currentGroup.stepIndex + 1}. ${currentGroup.label}` }}</h3>
                <p class="verification-text verification-step-observation">{{ currentGroup.observation }}</p>
                <EvidenceReader v-if="currentGroup.items.length" :key="`${detail.id}:${currentGroup.key}`" v-model:selected-item-id="selectedEvidenceId" presentation="step" :entity-key="`${detail.id}:${currentGroup.key}`" :project-id="detail.project_id" :items="currentGroup.items" :load-preview="loadResultEvidence" :original-url="resultOriginalUrl" :image-url="resultImageUrl" />
              </div>
            </div>
          </template>
          <div v-else class="verification-history-process">
            <SafeReportMarkdown v-if="detail.result.notes" :content="detail.result.notes" />
            <p v-else class="verification-text">历史记录未单独记录测试过程。</p>
            <template v-if="detail.result.schema_version === 2 && detail.result.evidence_items.length"><p class="verification-text verification-history-notice">以下为历史交互，未记录与步骤的对应关系。</p><EvidenceReader presentation="step" :entity-key="`${detail.id}:history`" :project-id="detail.project_id" :items="detail.result.evidence_items" :load-preview="loadResultEvidence" :original-url="resultOriginalUrl" :image-url="resultImageUrl" /></template>
            <template v-else-if="detail.result.schema_version !== 2"><p class="verification-text verification-history-notice">历史结果未记录交互配对。</p><div class="verification-actions"><a v-for="(reference, index) in detail.result.evidence_refs" :key="index" :href="resultOriginalUrl(reference)" target="_blank" rel="noopener" download>查看历史证据 {{ index + 1 }}</a></div></template>
          </div>
          </div>
        </section>
      </article>
      <EmptyState v-else-if="!isActiveVerification(detail.execution_status) && !detail.error" :title="detail.execution_status === 'cancelled' ? '本次执行已停止，尚无结果' : '尚无已接受的结果'" />
      </template>
    </template>
    <ConfirmAction :open="confirmStop" title="停止本次复测" description="停止请求接受后仍需等待执行结束，已保存材料不会删除。" confirm-label="请求停止" :loading="cancelling" @update:open="confirmStop = $event" @confirm="stop" />
  </section>
</template>
<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useQuery, useQueryClient } from '@tanstack/vue-query'
import { ArrowLeft, LoaderCircle, RefreshCw } from '@lucide/vue'
import { cancelVerificationRequest, getVerificationRequest, getVerificationArtifactPreview, verificationArtifactUrl } from '@/api/verificationRequests'
import type { EvidenceRef } from '@/api/types'
import EvidenceReader from '@/components/patterns/EvidenceReader.vue'
import { Alert, Badge, Button, EmptyState, Select, Skeleton } from '@/components/ui'
import { ConfirmAction, ErrorState } from '@/components/patterns'
import SafeReportMarkdown from '@/features/reports/SafeReportMarkdown.vue'
import VerificationActions from './VerificationActions.vue'
import { errorMessage, isActiveVerification, outcomeLabels, statusLabels, statusTone, verificationListQuery } from './verificationPresentation'
import './verifications.css'
const route = useRoute()
const router = useRouter()
const queryClient = useQueryClient()
const currentStepPanel = ref<HTMLElement>()
const requestActions = ref<InstanceType<typeof VerificationActions>>()
const requestId = computed(() => String(route.params.id))
const listQuery = computed(() => verificationListQuery(route.query))
const actionError = ref('')
function backToList() { void router.push({ name: 'Verifications', query: listQuery.value }) }
function openAgain() { if (detail.value) void router.push({ name: 'NewVerification', query: { ...listQuery.value, source_request_id: detail.value.id, return_to: 'detail' } }) }
let refreshUntil = Date.now() + 10 * 60_000
let refreshFailures = 0
const autoPaused = ref(false)
const { data: queryDetail, isLoading, isFetching, isError, error, refetch } = useQuery({
  queryKey: ['verification-request', requestId],
  queryFn: async ({ signal }) => {
    try { const id = requestId.value; const value = await getVerificationRequest(id, signal); if (value.id !== id) throw new Error('复测响应身份不匹配。'); refreshFailures = 0; return value }
    catch (error) { if (!signal.aborted) refreshFailures++; throw error }
  },
  retry: false,
  refetchOnWindowFocus: false,
  refetchInterval: (query) => {
    if (!query.state.data || !isActiveVerification(query.state.data.execution_status)) return false
    if (Date.now() >= refreshUntil || refreshFailures >= 3) { autoPaused.value = true; return false }
    return 5000
  },
})
const detail = computed(() => queryDetail.value?.id === requestId.value ? queryDetail.value : undefined)
function refresh() { refreshUntil = Date.now() + 10 * 60_000; refreshFailures = 0; autoPaused.value = false; void refetch() }
const cancelling = ref(false)
const confirmStop = ref(false)
const cancelError = ref('')
async function stop() {
  if (!detail.value || cancelling.value) return
  const id = requestId.value
  cancelling.value = true; cancelError.value = ''
  try { const receipt = await cancelVerificationRequest(id); queryClient.setQueryData(['verification-request', id], receipt); if (requestId.value === id) confirmStop.value = false }
  catch (error) { if (requestId.value === id) { cancelError.value = errorMessage(error); confirmStop.value = false } }
  finally { cancelling.value = false }
}
const steps = computed(() => {
  const result = detail.value?.result
  return result?.schema_version === 3 ? result.test_steps.map((step) => ({ ...step, items: step.evidence_item_ids.flatMap((id) => result.evidence_items.filter((item) => item.id === id)) })) : []
})
const unlinkedEvidence = computed(() => {
  const result = detail.value?.result
  if (result?.schema_version !== 3) return []
  const linkedIds = new Set(result.test_steps.flatMap((step) => step.evidence_item_ids))
  return result.evidence_items.filter((item) => !linkedIds.has(item.id))
})
const readingGroups = computed(() => {
  const groups = steps.value.map((step, index) => ({ key: `step:${index}`, stepIndex: index as number | null, label: step.action, observation: step.observation, items: step.items }))
  if (unlinkedEvidence.value.length) groups.push({ key: 'unlinked', stepIndex: null, label: '未关联步骤的证据', observation: '本次未记录这些证据与测试步骤的对应关系。', items: unlinkedEvidence.value })
  return groups
})
const selectedGroupKey = ref('')
const selectedEvidenceId = ref('')
const currentGroup = computed(() => readingGroups.value.find((group) => group.key === selectedGroupKey.value))
const groupOptions = computed(() => readingGroups.value.map((group) => ({ value: group.key, label: group.stepIndex === null ? group.label : `${group.stepIndex + 1}. ${group.label}` })))
const reasons = computed(() => {
  const result = detail.value?.result
  if (result?.schema_version !== 3) return []
  return result.key_evidence.map((reason) => ({ ...reason, targets: reason.evidence_item_ids.flatMap((itemId) => {
    const group = readingGroups.value.find((group) => group.items.some((item) => item.id === itemId))
    const item = result.evidence_items.find((evidence) => evidence.id === itemId)
    return group && item ? [{ groupKey: group.key, stepIndex: group.stepIndex, itemId, title: item.title || `交互 ${result.evidence_items.indexOf(item) + 1}` }] : []
  }) }))
})
function selectGroup(key: string) {
  const group = readingGroups.value.find((group) => group.key === key)
  if (!group || key === selectedGroupKey.value) return
  selectedGroupKey.value = key
  selectedEvidenceId.value = group.items.find((item) => item.role === 'primary')?.id || group.items[0]?.id || ''
}
let selectionEntity = ''
watch([requestId, readingGroups], ([id]) => {
  if (selectionEntity !== id || !currentGroup.value) {
    selectionEntity = id
    selectedGroupKey.value = ''
    const firstPrimary = readingGroups.value.find((group) => group.items.some((item) => item.role === 'primary'))
    selectGroup(firstPrimary?.key || readingGroups.value[0]?.key || '')
  } else if (!currentGroup.value.items.some((item) => item.id === selectedEvidenceId.value)) {
    selectedEvidenceId.value = currentGroup.value.items.find((item) => item.role === 'primary')?.id || currentGroup.value.items[0]?.id || ''
  }
}, { immediate: true })
async function goToEvidence(key: string, itemId: string) {
  selectGroup(key)
  selectedEvidenceId.value = itemId
  await nextTick()
  currentStepPanel.value?.scrollIntoView({ block: 'nearest', behavior: 'auto' })
  currentStepPanel.value?.focus({ preventScroll: true })
}
function loadResultEvidence(reference: EvidenceRef, signal: AbortSignal) { return getVerificationArtifactPreview(requestId.value, reference.artifact_id, signal) }
function resultOriginalUrl(reference: EvidenceRef) { return verificationArtifactUrl(requestId.value, reference.artifact_id) }
function resultImageUrl(reference: EvidenceRef) {
  const artifact = detail.value?.artifacts.find((item) => item.id === reference.artifact_id)
  return artifact?.kind === 'screenshot' && /\.(png|jpe?g|webp|gif)$/i.test(artifact.filename) ? resultOriginalUrl(reference) : null
}
watch(requestId, () => { actionError.value = ''; cancelError.value = ''; confirmStop.value = false; refreshUntil = Date.now() + 10 * 60_000; refreshFailures = 0; autoPaused.value = false })
</script>
