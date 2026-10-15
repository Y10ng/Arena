<template>
  <section class="verification-theme verification-page" aria-label="复测详情">
    <header class="verification-heading">
      <Button variant="secondary" @click="router.push({ name: 'Verifications', query: listQuery })">返回列表</Button>
      <h1>{{ detail?.title || '复测详情' }}</h1>
      <Button variant="secondary" :loading="isFetching" @click="refresh">刷新</Button>
    </header>
    <Skeleton v-if="isLoading" aria-label="正在加载复测详情" />
    <ErrorState v-else-if="isError && !detail" title="复测详情加载失败" :description="errorMessage(error)" @retry="refresh" />
    <template v-else-if="detail">
      <Alert v-if="isError" tone="warning" title="刷新失败">保留上次取得的信息，最新执行状态尚未确认。{{ errorMessage(error) }}</Alert>
      <div class="verification-detail-toolbar">
        <div class="verification-actions"><Badge :tone="statusTone(detail.execution_status)">{{ statusLabels[detail.execution_status] }}</Badge><Badge v-if="detail.result" :tone="outcomeTone(detail.result.outcome)">{{ outcomeLabels[detail.result.outcome] }}</Badge><span class="verification-muted">{{ modeLabels[detail.mode] }} · {{ detail.external_id || '无外部编号' }}</span></div>
        <div class="verification-actions"><Button :disabled="cancelling" @click="router.push({ name: 'NewVerification', query: { ...listQuery, source_request_id: detail.id } })">再次复测</Button><a v-if="detail.result" :href="verificationReportUrl(detail.id)" download target="_blank" rel="noopener">下载报告</a><Button v-if="isActiveVerification(detail.execution_status)" variant="danger" :disabled="cancelling || detail.execution_status === 'cancelling'" @click="confirmStop = true">停止本次执行</Button></div>
      </div>
      <p v-if="detail.execution_binding" class="verification-muted">本轮实际执行：{{ detail.execution_binding.display_name }} · {{ detail.execution_binding.model_name }} · Worker 版本 {{ detail.execution_binding.worker_revision }} / 模型版本 {{ detail.execution_binding.model_revision }}</p>
      <div v-if="detail.input_source" class="verification-source-link"><span class="verification-muted">漏洞来源 · {{ detail.input_source.input_mode === 'copied_snapshot' ? '复制上一轮输入快照' : '受理时读取的平台漏洞原文' }}</span><RouterLink :to="{ name: 'Findings', query: { vulnerabilityProjectId: detail.input_source.project_id, vulnerabilityId: detail.input_source.vulnerability_id } }">返回来源漏洞 · {{ detail.input_source.vulnerability_id }}</RouterLink><span class="verification-muted">来源后续修改或删除不影响本次已保存输入。</span></div>
      <div v-if="isActiveVerification(detail.execution_status)" class="verification-activity" role="status"><LoaderCircle :size="16" /><div><strong>{{ activityTitle }}</strong><p v-if="detail.execution_message" class="verification-text">{{ detail.execution_message }}</p><p class="verification-muted">{{ activityDescription }}{{ autoPaused ? ' 自动更新已暂停，可手动刷新继续。' : ' 每 5 秒更新，最长 10 分钟。' }}</p></div></div>
      <Alert v-if="cancelError" tone="warning" title="停止请求结果未确认">{{ cancelError }}<p>不能据此认为执行已停止。请刷新核对，必要时可明确再次请求停止。</p></Alert>
      <Alert v-if="detail.error" tone="danger" title="本次执行错误，未形成业务结论"><details><summary>展开错误与诊断</summary><p class="verification-text">{{ detail.error.message }}</p><p class="verification-muted">{{ detail.error.code }}</p></details></Alert>
      <div class="verification-cleanup"><span class="verification-muted">运行环境 · {{ cleanupLabels[detail.cleanup.status] || '回收状态待解释' }}</span><details v-if="detail.cleanup.error"><summary>回收异常，展开诊断</summary><p class="verification-text">{{ detail.cleanup.error.message }}</p><p class="verification-muted">{{ detail.cleanup.error.code }}</p></details><span v-else class="verification-muted">原件与证据独立保留</span></div>
      <div class="verification-tabs" role="tablist" aria-label="复测内容"><button v-for="tab in tabs" :id="`verification-tab-${tab.id}`" :key="tab.id" type="button" role="tab" :aria-selected="activeTab === tab.id" :aria-controls="`verification-panel-${tab.id}`" @click="activeTab = tab.id">{{ tab.label }}</button></div>
      <section :id="`verification-panel-${activeTab}`" class="verification-reading" role="tabpanel" :aria-labelledby="`verification-tab-${activeTab}`">
        <template v-if="activeTab === 'result'">
          <template v-if="detail.result">
            <div class="verification-result-title"><Badge :tone="outcomeTone(detail.result.outcome)">{{ outcomeLabels[detail.result.outcome] }}</Badge><h2>本轮结论</h2></div>
            <p class="verification-text verification-result-summary">{{ detail.result.summary }}</p>
            <h3>实际测试范围</h3><p class="verification-text">{{ detail.result.tested_scope }}</p>
            <h3>前提、操作与观察</h3><SafeReportMarkdown :content="detail.result.notes" />
            <h3>限制与未做事项</h3><ul v-if="detail.result.limitations.length"><li v-for="(limitation, index) in detail.result.limitations" :key="index" class="verification-text">{{ limitation }}</li></ul><p v-else class="verification-muted">本轮结果未列出额外限制；不意味着所有相关路径都已测试。</p>
            <p class="verification-muted">未复现只限本次实际测试条件，不等同于漏洞不存在、安全证明或修复通过。</p>
            <h3>结论引用的证据</h3><ul v-if="detail.result.evidence_refs.length"><li v-for="(evidence, index) in detail.result.evidence_refs" :key="index"><a :href="verificationArtifactUrl(detail.id, evidence.artifact_id)" download target="_blank" rel="noopener">{{ artifactName(evidence.artifact_id) }}</a><span v-if="evidence.pointer" class="verification-muted"> · {{ evidence.pointer }}</span></li></ul><p v-else class="verification-muted">本轮无引用证据，具体缺口以上述结论与限制为准。</p>
            <details v-if="detail.result.reason_code"><summary>结论原因码</summary>{{ detail.result.reason_code }}</details>
          </template>
          <EmptyState v-else :title="isActiveVerification(detail.execution_status) ? activityTitle : '尚无已接受的业务结论'" :description="isActiveVerification(detail.execution_status) ? '以当前真实执行阶段为准，结果接受后将在此展示摘要、范围、限制与证据。' : '执行错误或停止不会被显示为未复现，更不会显示为修复通过。可查看执行记录和输入资料。'" />
        </template>
        <template v-else-if="activeTab === 'materials'">
          <h2>本轮任务与实际授权范围</h2><p class="verification-text">{{ detail.origin }}</p>
          <p class="verification-muted">原件保留原始字节；派生材料帮助消费，不替代原件，也不是本轮验证证据。</p>
          <div v-if="detail.materials.length" class="verification-material-reader">
            <div class="verification-material-list" aria-label="输入原件与派生材料"><button v-for="material in detail.materials" :key="material.id" type="button" :class="{ 'is-selected': preview?.id === material.id }" @click="previewMaterial(material)"><span>{{ material.path }}</span><span class="verification-muted">{{ material.id === detail.report.resource_id ? (detail.input_source ? '主资料原文快照 · ' : '主资料 · ') : material.id === detail.report.manifest_resource_id ? '来源清单 · ' : '' }}{{ material.derived_from_resource_id ? '派生材料' : '留档输入' }} · {{ formatProjectResourceSize(material.size_bytes) }}</span></button></div>
            <article class="verification-material-content">
              <template v-if="preview"><header class="verification-heading"><h3>{{ preview.path }}</h3><a :href="verificationMaterialUrl(detail.id, preview.id)" download target="_blank" rel="noopener">下载{{ preview.derived_from_resource_id ? '派生材料' : '原件' }}</a></header><Skeleton v-if="previewLoading" /><Alert v-else-if="previewError" tone="danger" title="材料读取失败">{{ previewError }}<template #actions><Button size="sm" variant="secondary" @click="previewMaterial(preview!)">重新读取</Button></template></Alert><img v-else-if="isImage(preview.path)" class="verification-image" :src="verificationMaterialUrl(requestId, preview.id)" :alt="preview.path" @error="previewError = '图像读取失败，请下载原件核对。'" /><SafeReportMarkdown v-else-if="/\.md$/i.test(preview.path)" :content="previewText" /><p v-else-if="isPreviewable(preview.path)" class="verification-text">{{ previewText }}</p><p v-else class="verification-muted">此文件请下载原件查看，不生成伪预览。</p><details class="verification-section"><summary>完整性与来源</summary><p class="verification-text">SHA256 {{ preview.sha256 }}</p><p v-if="preview.derived_from_resource_id" class="verification-muted">来源原件：{{ preview.derived_from_resource_id }}</p></details></template>
              <p v-else class="verification-muted">选择左侧资料阅读。</p>
            </article>
          </div><EmptyState v-else title="当前无可读取材料" description="以服务端实际保存记录为准。" />
        </template>
        <template v-else-if="activeTab === 'runs'">
          <h2>本轮执行记录</h2><div class="verification-scroll"><table v-if="detail.runs.length" class="verification-table"><thead><tr><th>执行者</th><th>运行状态</th><th>开始 / 结束时间</th><th>诊断</th></tr></thead><tbody><tr v-for="run in detail.runs" :key="run.id"><td>{{ run.worker_name }}</td><td>{{ runStatusLabels[run.status] || '运行状态待解释' }}</td><td>{{ dateTime(run.started_at) || '未开始' }}<br />{{ dateTime(run.ended_at) || '未确认结束' }}</td><td><details><summary>运行详情</summary><dl class="verification-metadata"><dt>运行 ID</dt><dd>{{ run.id }}</dd><dt>原始状态</dt><dd>{{ run.status }}</dd><dt>错误类型</dt><dd>{{ run.error_type || '无' }}</dd></dl><p v-if="run.error_message" class="verification-text">{{ run.error_message }}</p></details></td></tr></tbody></table><p v-else class="verification-muted">尚无本轮运行记录。</p></div>
          <h3>本轮证据文件</h3><div v-for="artifact in detail.artifacts" :key="artifact.id" class="verification-file-row"><span>{{ artifact.filename }}<span class="verification-muted"> · {{ artifact.kind }} · {{ formatProjectResourceSize(artifact.size) }}</span></span><a :href="verificationArtifactUrl(detail.id, artifact.id)" download target="_blank" rel="noopener">下载证据</a><details><summary>证据详情</summary><dl class="verification-metadata"><dt>证据 ID</dt><dd>{{ artifact.id }}</dd><dt>来源运行</dt><dd>{{ artifact.task_run_id || '未标注' }}</dd><dt>SHA256</dt><dd>{{ artifact.sha256 }}</dd></dl></details></div><p v-if="!detail.artifacts.length" class="verification-muted">尚无允许交付的本轮证据文件。</p>
        </template>
        <template v-else>
          <h2>历史关联</h2><RouterLink v-if="detail.previous_request_id" :to="{ name: 'VerificationDetail', params: { id: detail.previous_request_id }, query: listQuery }">查看上一轮复测</RouterLink><ul v-if="detail.history.length" class="verification-history"><li v-for="item in detail.history" :key="item.id"><RouterLink :to="{ name: 'VerificationDetail', params: { id: item.id }, query: listQuery }">{{ item.title }}</RouterLink><span>{{ statusLabels[item.execution_status] }} · {{ item.result ? outcomeLabels[item.result.outcome] : '无业务结论' }}</span><span class="verification-muted">{{ dateTime(item.created_at) }}</span></li></ul><p v-else class="verification-muted">当前未返回其他关联复测记录。</p>
        </template>
      </section>
      <details class="verification-technical"><summary>请求与实际执行配置</summary><dl class="verification-metadata"><dt>请求 ID</dt><dd>{{ detail.request_id }}</dd><dt>提交者</dt><dd>{{ detail.source.type === 'integration' ? '外部调用身份' : '平台用户' }} · {{ detail.source.name }}</dd><dt>受理 / 终结时间</dt><dd>{{ dateTime(detail.accepted_at) }} / {{ dateTime(detail.terminal_at) || '未终结' }}</dd><template v-if="detail.group_id"><dt>历史项目组</dt><dd>{{ detail.group_id }}</dd></template><dt>本轮实际执行配置</dt><dd v-if="detail.execution_binding">{{ detail.execution_binding.display_name }} · Worker 版本 {{ detail.execution_binding.worker_revision }}<br />{{ detail.execution_binding.model_name }} · 模型版本 {{ detail.execution_binding.model_revision }}</dd><dd v-else>尚无首次认领的实际配置，开始时确定；不以当前配置替代历史。</dd><dt>关联 Worker</dt><dd><template v-if="detail.tenant_workers.length"><div v-for="worker in detail.tenant_workers" :key="worker.id">{{ worker.display_name }} · {{ worker.revision === null ? '版本在开始时确定' : `当次版本 ${worker.revision}` }}<span class="verification-muted"> · {{ worker.id }}</span></div></template><span v-else>无 Worker 关联记录</span></dd><template v-if="detail.worker_template_id"><dt>历史 Worker 模板</dt><dd>{{ detail.worker_template_id }}（仅历史记录，不用于新提交）</dd></template><dt>授权到期时间</dt><dd>{{ dateTime(detail.authorization_expires_at) || '未设置' }}</dd><dt>执行停止时间</dt><dd>{{ dateTime(detail.execution_deadline) || '未设置' }}</dd><dt>执行上下文 ID</dt><dd>{{ detail.project_id }}</dd></dl></details>
    </template>
    <ConfirmAction :open="confirmStop" title="停止本次复测" description="请求接受后仍需确认执行结束；已保存材料与历史结论不会删除。再次复测须建立新请求。" confirm-label="请求停止" :loading="cancelling" content-class="verification-theme verification-confirm" overlay-class="verification-confirm-overlay" @update:open="confirmStop = $event" @confirm="stop" />
  </section>
</template>
<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useQuery, useQueryClient } from '@tanstack/vue-query'
import { LoaderCircle } from '@lucide/vue'
import { fetchText } from '@/api/client'
import { cancelVerificationRequest, getVerificationRequest, verificationArtifactUrl, verificationMaterialUrl, verificationReportUrl, type VerificationMaterial } from '@/api/verificationRequests'
import { Alert, Badge, Button, EmptyState, Skeleton } from '@/components/ui'
import { ConfirmAction, ErrorState } from '@/components/patterns'
import SafeReportMarkdown from '@/features/reports/SafeReportMarkdown.vue'
import { dateTime } from '@/lib/format'
import { formatProjectResourceSize } from '@/features/projects/projectResources'
import { cleanupLabels, errorMessage, isActiveVerification, modeLabels, outcomeLabels, outcomeTone, runStatusLabels, statusLabels, statusTone, verificationListQuery } from './verificationPresentation'
import './verifications.css'
const route = useRoute()
const router = useRouter()
const queryClient = useQueryClient()
const requestId = computed(() => String(route.params.id))
const listQuery = computed(() => verificationListQuery(route.query))
const tabs = [{ id: 'result', label: '结论与证据' }, { id: 'materials', label: '输入资料' }, { id: 'runs', label: '执行记录' }, { id: 'history', label: '历史' }]
const activeTab = ref('result')
let refreshUntil = Date.now() + 10 * 60_000
let refreshFailures = 0
const autoPaused = ref(false)
const { data: detail, isLoading, isFetching, isError, error, refetch } = useQuery({
  queryKey: ['verification-request', requestId],
  queryFn: async ({ signal }) => {
    try { const value = await getVerificationRequest(requestId.value, signal); refreshFailures = 0; return value }
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
function refresh() { refreshUntil = Date.now() + 10 * 60_000; refreshFailures = 0; autoPaused.value = false; void refetch() }
const activeRun = computed(() => detail.value?.runs.find((run) => run.status === 'running') || detail.value?.runs.find((run) => run.status === 'queued'))
const activityTitle = computed(() => detail.value?.execution_status === 'cancelling' ? '正在确认停止与执行结束' : activeRun.value ? `${activeRun.value.worker_name} · ${runStatusLabels[activeRun.value.status] || activeRun.value.status}` : detail.value?.execution_status === 'queued' ? '等待调度执行' : '执行中，等待已接受结果')
const activityDescription = computed(() => detail.value?.execution_status === 'cancelling' ? '停止请求已接受，尚未确认停止，不能视为已结束。' : activeRun.value?.started_at ? `本轮运行开始于 ${dateTime(activeRun.value.started_at)}。` : detail.value?.execution_status === 'queued' ? '暂未开始本轮运行；配置有效不代表当前立即有执行容量。' : '以服务端返回的真实运行状态为准，不推算完成百分比。')
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
function artifactName(id: string) { return detail.value?.artifacts.find((item) => item.id === id)?.filename || id }
const isImage = (path: string) => /\.(png|jpe?g)$/i.test(path)
const isPreviewable = (path: string) => isImage(path) || /\.(md|txt|text|json)$/i.test(path)
const preview = ref<VerificationMaterial | null>(null)
const previewLoading = ref(false)
const previewText = ref('')
const previewError = ref('')
let previewGeneration = 0
function closePreview() { previewGeneration++; preview.value = null; previewText.value = ''; previewError.value = ''; previewLoading.value = false }
async function previewMaterial(material: VerificationMaterial) {
  closePreview(); preview.value = material
  if (isImage(material.path) || !isPreviewable(material.path)) return
  const generation = previewGeneration
  previewLoading.value = true
  try { const text = await fetchText(verificationMaterialUrl(requestId.value, material.id)); if (generation === previewGeneration) previewText.value = text }
  catch (error) { if (generation === previewGeneration) previewError.value = errorMessage(error) }
  finally { if (generation === previewGeneration) previewLoading.value = false }
}
watch([activeTab, () => detail.value?.materials], () => { if (activeTab.value === 'materials' && !preview.value && detail.value?.materials[0]) void previewMaterial(detail.value.materials[0]) })
watch(requestId, () => { closePreview(); activeTab.value = 'result'; cancelError.value = ''; confirmStop.value = false; refreshUntil = Date.now() + 10 * 60_000; refreshFailures = 0; autoPaused.value = false })
onBeforeUnmount(closePreview)
</script>
