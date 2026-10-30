<template>
  <section class="verification-theme verification-page verification-detail-page" aria-label="复测详情">
    <header class="verification-detail-heading">
      <Button variant="ghost" @click="backToList"><template #icon><ArrowLeft :size="16" /></template>复测列表</Button>
      <div class="verification-detail-title"><h1>{{ detail?.title || '复测详情' }}</h1><Badge v-if="detail" :tone="statusTone(detail.execution_status)">{{ statusLabels[detail.execution_status] }}</Badge></div>
      <div class="verification-actions">
        <Button variant="ghost" :loading="isFetching" aria-label="刷新复测详情" @click="refresh"><template #icon><RefreshCw :size="16" /></template></Button>
        <Button v-if="detail && detail.purge_state !== 'pending'" @click="openAgain">再次复测</Button>
        <VerificationActions v-if="detail" :key="detail.id" context="detail" :request="detail" @deleted="backToList" @error="actionError = $event" />
      </div>
    </header>
    <Skeleton v-if="isLoading" aria-label="正在加载复测详情" />
    <ErrorState v-else-if="isError && !detail" title="复测详情加载失败" :description="errorMessage(error)" @retry="refresh" />
    <template v-else-if="detail">
      <Alert v-if="isError" tone="warning" title="刷新失败">保留上次取得的信息，最新执行状态尚未确认。{{ errorMessage(error) }}</Alert>
      <Alert v-if="actionError" tone="danger" title="操作未完成">{{ actionError }}<template #actions><Button variant="ghost" @click="actionError = ''">关闭提示</Button></template></Alert>
      <Alert v-if="detail.purge_state === 'pending'" tone="warning" title="清理未完成，可重试"><p v-if="detail.purge_error" class="verification-text">{{ detail.purge_error }}</p></Alert>
      <template v-else>
      <template v-if="detail.input_source">
        <RouterLink v-if="detail.input_source.availability === 'available'" class="verification-source-link" :to="{ name: 'Findings', query: { vulnerabilityProjectId: detail.input_source.project_id, vulnerabilityId: detail.input_source.vulnerability_id } }">来源漏洞</RouterLink>
        <p v-else class="verification-muted">{{ detail.input_source.availability === 'deleted' ? '来源漏洞已删除' : '来源不可访问' }}<span v-if="detail.input_source.availability_reason"> · {{ detail.input_source.availability_reason }}</span></p>
      </template>
      <div v-if="detail.target_url || detail.source_target" class="verification-stack">
        <p v-if="detail.source_target" class="verification-text">历史来源：{{ detail.source_target }}</p>
        <p class="verification-text">本次目标：{{ detail.target_url ?? detail.source_target }}</p>
      </div>
      <div v-if="isActiveVerification(detail.execution_status)" class="verification-activity" role="status">
        <LoaderCircle :size="18" /><div><p v-if="detail.execution_message" class="verification-text">{{ detail.execution_message }}</p><p v-if="detail.execution_status === 'cancelling'" class="verification-text">停止请求已接受，尚未确认执行结束。</p><p v-if="autoPaused" class="verification-text">自动更新已暂停，请手动刷新核对。</p></div>
        <Button variant="secondary" :disabled="cancelling || detail.execution_status === 'cancelling'" @click="confirmStop = true">停止本次执行</Button>
      </div>
      <Alert v-if="cancelError" tone="warning" title="停止请求结果未确认">{{ cancelError }}<p>不能据此认为执行已停止，请刷新核对。</p></Alert>
      <Alert v-if="detail.error" tone="danger" :title="detail.result ? '执行出现异常' : '本次执行错误，未形成业务结论'"><p class="verification-text">{{ detail.error.message }}</p><details><summary>错误详情</summary>{{ detail.error.code }}</details></Alert>
      <Alert v-if="detail.cleanup.error" tone="warning" title="运行环境回收异常"><details><summary>查看回收错误</summary><p class="verification-text">{{ detail.cleanup.error.message }}</p></details></Alert>
      <article v-if="detail.result" class="verification-reading" aria-label="复测结果">
        <section class="verification-report-section verification-conclusion">
          <h2>一、漏洞概述</h2>
          <div class="verification-section-body">
            <p class="verification-result-outcome">{{ outcomeLabels[detail.result.outcome] }}</p>
            <SafeReportMarkdown class="verification-result-summary" :content="detail.result.summary" />
            <template v-if="detail.result.tested_scope"><h3>本次条件</h3><SafeReportMarkdown :content="detail.result.tested_scope" /></template>
            <template v-if="detail.result.limitations.length"><h3>限制</h3><SafeReportMarkdown v-for="(limitation, index) in detail.result.limitations" :key="index" :content="limitation" /></template>
            <h3>判断依据</h3>
            <template v-if="detail.result.schema_version === 3 || detail.result.schema_version === 4">
              <ul v-if="reasons.length" class="verification-reasons"><li v-for="(evidence, index) in reasons" :key="index"><SafeReportMarkdown :content="evidence.reason" /><div v-if="evidence.targets.length" class="verification-evidence-links"><Button v-for="target in evidence.targets" :key="target.itemId" size="sm" variant="link" @click="goToEvidence(target.groupKey, target.itemId)">{{ target.title }} · {{ target.stepIndex === null ? '未关联步骤' : `步骤 ${target.stepIndex + 1}` }}</Button></div><p v-for="id in evidence.missingIds" :key="id" class="verification-muted">{{ id }}（未找到引用）</p></li></ul>
              <p v-else class="verification-text">本次未记录可用于判断的关键证据。</p>
            </template>
            <p v-else class="verification-text">历史记录未单独记录判断依据。</p>
          </div>
        </section>
        <section class="verification-report-section verification-process">
          <h2>二、复现过程与数据包</h2>
          <div class="verification-section-body verification-section-body--process">
          <SafeReportMarkdown v-if="detail.display_images?.length" class="verification-display-images" content="" :identity="detail.id" :images="detail.display_images" :load-image="loadDisplayImage" />
          <template v-if="detail.result.schema_version === 3 || detail.result.schema_version === 4">
            <p v-if="!steps.length" class="verification-text verification-process-empty">本次未记录已执行的测试步骤。</p>
            <div :key="detail.id" class="verification-test-steps">
              <section v-for="group in readingGroups" :key="group.key" class="verification-test-step" :aria-label="group.stepIndex === null ? '其他材料' : `步骤 ${group.stepIndex + 1}`">
                <h3>{{ group.stepIndex === null ? '其他材料' : `步骤 ${group.stepIndex + 1}` }}</h3>
                <SafeReportMarkdown :content="group.label" />
                <SafeReportMarkdown v-if="group.observation" class="verification-step-observation" :content="group.observation" />
                <div class="verification-step-evidence">
                  <EvidenceItemDisclosure v-for="item in group.items" :key="item.id" :ref="(instance) => setEvidenceDisclosure(group.key, item.id, instance)" :item="item" :entity-key="`${detail.id}:${group.key}`" :project-id="detail.project_id" :load-preview="loadResultEvidence" :original-url="resultOriginalUrl" :image-url="resultImageUrl" :artifact-size="resultArtifactSize" />
                  <p v-for="id in group.missingIds" :key="id" class="verification-muted">{{ id }}（未找到引用）</p>
                </div>
              </section>
            </div>
          </template>
          <div v-else class="verification-history-process">
            <SafeReportMarkdown v-if="detail.result.notes" :content="detail.result.notes" />
            <p v-else class="verification-text">历史记录未单独记录测试过程。</p>
            <template v-if="detail.result.schema_version === 2 && detail.result.evidence_items.length"><p class="verification-text verification-history-notice">以下为历史交互，未记录与步骤的对应关系。</p><div :key="detail.id" class="verification-step-evidence"><EvidenceItemDisclosure v-for="item in detail.result.evidence_items" :key="item.id" :item="item" :entity-key="`${detail.id}:history`" :project-id="detail.project_id" :load-preview="loadResultEvidence" :original-url="resultOriginalUrl" :image-url="resultImageUrl" :artifact-size="resultArtifactSize" /></div></template>
            <template v-else-if="detail.result.schema_version !== 2"><p class="verification-text verification-history-notice">历史结果未记录交互配对。</p><div class="verification-actions"><a v-for="(reference, index) in detail.result.evidence_refs" :key="index" :href="resultOriginalUrl(reference)" target="_blank" rel="noopener" download>查看历史证据 {{ index + 1 }}</a></div></template>
          </div>
          </div>
        </section>
        <section class="verification-report-section verification-remediation">
          <h2>三、修复建议</h2>
          <div class="verification-section-body">
            <SafeReportMarkdown v-if="detail.result.schema_version === 4" :content="detail.result.remediation" />
            <p v-else class="verification-text">历史结果未记录本轮修复建议。</p>
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
import { computed, ref, watch, type ComponentPublicInstance } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useQuery, useQueryClient } from '@tanstack/vue-query'
import { ArrowLeft, LoaderCircle, RefreshCw } from '@lucide/vue'
import { cancelVerificationRequest, getVerificationRequest, getVerificationArtifactPreview, verificationArtifactUrl } from '@/api/verificationRequests'
import type { EvidenceRef } from '@/api/types'
import { fetchBlob } from '@/api/client'
import type { ReportDisplayImage } from '@/features/reports/reportImages'
import EvidenceItemDisclosure from '@/components/patterns/EvidenceItemDisclosure.vue'
import { Alert, Badge, Button, EmptyState, Skeleton } from '@/components/ui'
import { ConfirmAction, ErrorState } from '@/components/patterns'
import SafeReportMarkdown from '@/features/reports/SafeReportMarkdown.vue'
import VerificationActions from './VerificationActions.vue'
import { errorMessage, isActiveVerification, outcomeLabels, statusLabels, statusTone, verificationListQuery } from './verificationPresentation'
import './verifications.css'
const route = useRoute()
const router = useRouter()
const queryClient = useQueryClient()
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
  return result && (result.schema_version === 3 || result.schema_version === 4) ? result.test_steps.map((step) => ({
    ...step,
    items: step.evidence_item_ids.flatMap((id) => result.evidence_items.filter((item) => item.id === id)),
    missingIds: step.evidence_item_ids.filter((id) => !result.evidence_items.some((item) => item.id === id)),
  })) : []
})
const unlinkedEvidence = computed(() => {
  const result = detail.value?.result
  if (!result || (result.schema_version !== 3 && result.schema_version !== 4)) return []
  const linkedIds = new Set(result.test_steps.flatMap((step) => step.evidence_item_ids))
  return result.evidence_items.filter((item) => !linkedIds.has(item.id))
})
const readingGroups = computed(() => {
  const groups = steps.value.map((step, index) => ({ key: `step:${index}`, stepIndex: index as number | null, label: step.action, observation: step.observation, items: step.items, missingIds: step.missingIds }))
  if (unlinkedEvidence.value.length) groups.push({ key: 'unlinked', stepIndex: null, label: '其他材料', observation: '本次未记录这些证据与测试步骤的对应关系。', items: unlinkedEvidence.value, missingIds: [] })
  return groups
})
const reasons = computed(() => {
  const result = detail.value?.result
  if (!result || (result.schema_version !== 3 && result.schema_version !== 4)) return []
  return result.key_evidence.map((reason) => ({ ...reason, missingIds: reason.evidence_item_ids.filter((id) => !result.evidence_items.some((item) => item.id === id)), targets: reason.evidence_item_ids.flatMap((itemId) => {
    const group = readingGroups.value.find((group) => group.items.some((item) => item.id === itemId))
    const item = result.evidence_items.find((evidence) => evidence.id === itemId)
    return group && item ? [{ groupKey: group.key, stepIndex: group.stepIndex, itemId, title: item.title || `交互 ${result.evidence_items.indexOf(item) + 1}` }] : []
  }) }))
})
const evidenceDisclosures = new Map<string, InstanceType<typeof EvidenceItemDisclosure>>()
function setEvidenceDisclosure(groupKey: string, itemId: string, instance: Element | ComponentPublicInstance | null) {
  const key = JSON.stringify([groupKey, itemId])
  if (instance) evidenceDisclosures.set(key, instance as InstanceType<typeof EvidenceItemDisclosure>)
  else evidenceDisclosures.delete(key)
}
function goToEvidence(groupKey: string, itemId: string) {
  // 同一项被多步引用时，判断依据定位首个真实引用，不虚构唯一归属。
  void evidenceDisclosures.get(JSON.stringify([groupKey, itemId]))?.reveal()
}
function loadDisplayImage(image: ReportDisplayImage, signal: AbortSignal) { return fetchBlob(verificationArtifactUrl(requestId.value, image.artifact_id), signal) }
function resultArtifactSize(reference: EvidenceRef) { return detail.value?.artifacts.find((item) => item.id === reference.artifact_id)?.size }
function loadResultEvidence(reference: EvidenceRef, signal: AbortSignal) { return getVerificationArtifactPreview(requestId.value, reference.artifact_id, signal) }
function resultOriginalUrl(reference: EvidenceRef) { return verificationArtifactUrl(requestId.value, reference.artifact_id) }
function resultImageUrl(reference: EvidenceRef) {
  const artifact = detail.value?.artifacts.find((item) => item.id === reference.artifact_id)
  return artifact?.kind === 'screenshot' && /\.(png|jpe?g|webp|gif)$/i.test(artifact.filename) ? resultOriginalUrl(reference) : null
}
watch(requestId, () => { actionError.value = ''; cancelError.value = ''; confirmStop.value = false; refreshUntil = Date.now() + 10 * 60_000; refreshFailures = 0; autoPaused.value = false })
</script>
