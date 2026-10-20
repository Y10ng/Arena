<template>
  <Dialog :open="true" :title="sourceId ? '再次复测' : fixedSource ? '复测漏洞' : '新建复测'" description="" size="lg" content-class="verification-theme verification-create-dialog" overlay-class="verification-create-overlay" :dismissible="!submitting && !copying && !resourceBusy && !materialsConfirm" :close-on-outside="false" @update:open="!$event && requestClose()" @close-auto-focus="emit('closeAutoFocus', $event)">
    <Skeleton v-if="sourceLoading" aria-label="正在读取上一轮信息" />
    <ErrorState v-else-if="sourceError" title="上一轮信息读取失败" :description="sourceError" @retry="loadSource" />
    <Alert v-if="created" tone="success" title="请求已受理">当前状态：{{ statusLabels[created.execution_status] }}。<template #actions><Button @click="enterCreated">进入已受理记录</Button></template></Alert>
    <Alert v-if="submissionError" :tone="pending ? 'warning' : 'danger'" :title="acceptance === 'deleted' ? '原请求已删除' : acceptance === 'source_deleted' ? '来源复测已删除' : pending ? '提交未完成确认' : '请检查输入'">
      {{ submissionError }}
      <p v-if="pending">请求 ID：{{ pending.request_id }}。<template v-if="acceptance === 'deleted'">原请求对应的记录已删除，不会重新创建。</template><template v-else-if="acceptance === 'source_deleted'">新一轮未受理，已复制的独立资料批次仍保留。请关闭表单，从原漏洞新建复测。</template><template v-else-if="!definitelyRejected">原请求已锁定，请用原请求重试；受理确认前不能关闭。</template><template v-else>原请求未受理，可以恢复编辑；正文和补充说明会保留。</template></p>
      <template v-if="pending" #actions><Button v-if="acceptance !== 'batch_expired' && acceptance !== 'deleted' && acceptance !== 'source_deleted'" variant="secondary" :loading="submitting" @click="submitPending">用原请求重试</Button><Button v-if="definitelyRejected && acceptance !== 'deleted' && acceptance !== 'source_deleted'" variant="secondary" :disabled="submitting" @click="editRejected">{{ acceptance === 'batch_expired' ? '更换过期资料并继续编辑' : '修改未受理的输入' }}</Button></template>
    </Alert>
    <form id="new-verification-form" class="verification-form" @submit.prevent="submit">
      <fieldset class="verification-stack" :disabled="locked || sourceLoading || Boolean(sourceError)" @input="dirty = true" @change="dirty = true">
        <div class="verification-grid verification-create-basics">
          <FormField label="任务名称" for="new-verification-title" required><Input id="new-verification-title" v-model="form.title" required placeholder="例如：订单访问控制修复后复测" /></FormField>
          <FormField label="模式" for="new-verification-mode" required><Select content-class="verification-theme verification-select" id="new-verification-mode" v-model="form.mode" :options="modeOptions" :disabled="locked" /></FormField>
        </div>
        <div v-if="configLoading || configError || !readiness?.ready" class="verification-readiness" role="status">
          <span v-if="configLoading">正在读取执行配置…</span>
          <span v-else-if="configError">配置读取失败：{{ configError }}</span>
          <span v-else>{{ readiness?.message || '执行配置尚未读取' }}</span>
          <Button v-if="!configLoading" variant="ghost" :disabled="locked" @click="loadConfig">重新核对</Button>
          <RouterLink v-if="!configLoading && !readiness?.ready" :to="{ name: 'VerificationConfiguration' }">去配置复测执行</RouterLink>
        </div>
        <FormField v-if="!fixedSource" label="输入来源" for="new-verification-input-kind" required><Select content-class="verification-theme verification-select" id="new-verification-input-kind" :model-value="inputKind" :options="inputOptions" :disabled="locked || resourceBusy || copying || hasCopiedMaterials" @update:model-value="changeInputKind" /><p v-if="hasCopiedMaterials" class="verification-muted">已复制旧输入。若要改为当前漏洞或手工资料，请先在“资料”中重新准备本轮资料，避免混用两个版本。</p></FormField>
        <div class="verification-editor">
          <template v-if="inputKind === 'platform'">
            <div class="verification-source-picker verification-stack">
              <div v-if="!fixedSource" class="verification-grid"><FormField label="查找漏洞" for="verification-source-search"><Input id="verification-source-search" v-model="sourceSearch" placeholder="筛选漏洞名称、目标或项目" /></FormField><FormField label="严重程度" for="verification-source-severity"><Select id="verification-source-severity" v-model="sourceSeverity" content-class="verification-theme verification-select" :options="severityOptions" :disabled="locked" /></FormField></div>
              <FormField v-if="!fixedSource" label="平台已有漏洞" for="verification-source-vulnerability" required><Select id="verification-source-vulnerability" :model-value="selectedVulnerabilityKey" content-class="verification-theme verification-select" :options="vulnerabilityOptions" :disabled="locked || vulnerabilitiesLoading" placeholder="选择来源项目与漏洞" @update:model-value="selectPlatformVulnerability" /></FormField>
              <p v-if="!fixedSource && vulnerabilitiesLoading" class="verification-muted">正在读取已有漏洞…</p><Alert v-if="!fixedSource && vulnerabilitiesError" tone="warning" title="漏洞列表读取失败">{{ errorMessage(vulnerabilitiesError) }}<template #actions><Button variant="secondary" size="sm" @click="refetchVulnerabilities()">重新读取</Button></template></Alert>
              <p v-if="!fixedSource && !vulnerabilitiesLoading && !vulnerabilitiesError && !vulnerabilityOptions.length" class="verification-muted">当前没有匹配漏洞，可调整筛选或使用手工资料。</p>
              <Skeleton v-if="platformLoading" aria-label="正在读取漏洞原文与历史证据" /><Alert v-else-if="platformError" tone="danger" title="来源资料尚未读取">{{ platformError }}<template #actions><Button variant="secondary" size="sm" @click="loadPlatformPreview">重新读取</Button></template></Alert>
              <VerificationSourcePreview v-else-if="platformPreview" :preview="platformPreview" />
              
            </div>
          </template>
          <div v-else-if="inputKind === 'copied'" class="verification-editor-file">
            <h3>上一轮输入快照</h3>
            <Button variant="secondary" :loading="copying" :disabled="locked || Boolean(batch) || resourceBusy" @click="copyOriginals">{{ hasCopiedMaterials ? '已复制上一轮输入' : batch ? '请先在资料中重新准备，再复制输入' : '复制上一轮输入' }}</Button>
            <p v-if="form.mainMemberId" class="verification-muted">主资料：{{ batch?.members.find((member) => member.id === form.mainMemberId)?.path }}</p>
            <Alert v-if="copyError" tone="warning" title="原件复制未完成">{{ copyError }}</Alert>
          </div>
          <div v-else class="verification-editor-toolbar">
            <div class="verification-segments" aria-label="主资料编辑模式"><Button size="sm" :variant="editorTab === 'edit' ? 'secondary' : 'ghost'" :aria-pressed="editorTab === 'edit'" @click="editorTab = 'edit'">编辑</Button><Button size="sm" :variant="editorTab === 'preview' ? 'secondary' : 'ghost'" :aria-pressed="editorTab === 'preview'" @click="editorTab = 'preview'">预览</Button></div>
            <label class="verification-report-select">主资料<Select content-class="verification-theme verification-select" id="new-verification-report-type" v-model="form.reportType" :options="reportOptions" :disabled="locked" aria-label="主资料来源" /></label>
          </div>
          <template v-if="inputKind === 'manual' && form.reportType !== 'file'">
            <Textarea v-show="editorTab === 'edit'" id="new-verification-content" v-model="form.content" class="verification-editor-input" :rows="9" :required="editorTab === 'edit'" aria-label="主资料正文" placeholder="漏洞描述、复现步骤、请求与响应" />
            <div v-if="editorTab === 'preview'" class="verification-editor-preview"><SafeReportMarkdown v-if="form.reportType === 'markdown' && form.content" :content="form.content" /><p v-else class="verification-text">{{ form.content || '尚无主资料正文。' }}</p></div>
          </template>
          <div v-else-if="inputKind === 'manual'" class="verification-editor-file">
            <FormField label="主资料文件" for="new-verification-main-file" required><Select content-class="verification-theme verification-select" id="new-verification-main-file" v-model="form.mainMemberId" :options="mainFileOptions" :disabled="locked || resourceBusy" placeholder="打开资料，上传或复制后选择主文件" /></FormField>
            <p class="verification-muted">支持 MD、TXT、DOCX、PNG、JPEG 原件。</p>
          </div>
          <div class="verification-editor-materials">
            <Popover v-model:open="materialsOpen" side="top" align="start" size="lg" content-class="verification-theme verification-materials-popover" :dismissible="!resourceBusy && !materialsConfirm">
              <template #trigger><Button size="sm" variant="secondary" :disabled="locked || resourceBusy || materialsConfirm"><template #icon><Paperclip :size="14" /></template>资料{{ batch?.members.length || files.length ? `（${(batch?.members.length || 0) + files.length}）` : '' }}</Button></template>
              <div class="verification-stack">
                <header class="verification-heading"><h2>本轮资料</h2><Button size="sm" variant="ghost" :disabled="resourceBusy || materialsConfirm" @click="materialsOpen = false">收起</Button></header>
                <VerificationMaterials :key="formScope" v-model="batch" v-model:files="files" :disabled="locked || copying" @busy="resourceBusy = $event" @confirming="materialsConfirm = $event" />
              </div>
            </Popover>
            <span v-if="files.length" class="verification-muted">{{ files.length }} 个待上传{{ failedFiles ? `，${failedFiles} 个失败或待核对` : '' }}</span>
            <span v-if="resourceBusy" class="verification-muted">资料操作中，请勿离开</span>
          </div>
        </div>
        <FormField label="环境变化与补充说明（可选）" for="new-verification-origin">
          <Textarea id="new-verification-origin" v-model="form.origin" class="verification-origin" :rows="3" placeholder="修复或部署变化、必要身份与其他补充资料" />
        </FormField>
        <details class="verification-extra-settings"><summary>外部编号与可选期限</summary><div class="verification-stack">
          <FormField label="外部漏洞编号（可选）" for="new-verification-external"><Input id="new-verification-external" v-model="form.external_id" placeholder="例如：ISSUE-123" /></FormField>
          <div class="verification-grid"><FormField label="授权到期时间（本地时间，可选）" for="new-verification-authorization"><Input id="new-verification-authorization" v-model="form.authorization" type="datetime-local" /></FormField><FormField label="执行停止时间（本地时间，可选）" for="new-verification-deadline"><Input id="new-verification-deadline" v-model="form.deadline" type="datetime-local" /></FormField></div>
        </div></details>
      </fieldset>
    </form>
    <template #footer>
      <span class="verification-muted verification-footer-summary">{{ resourcePending ? '请先完成上传或移出待上传项' : '' }}</span>
      <Button variant="secondary" :disabled="submitting || copying || resourceBusy" @click="requestClose">取消</Button>
      <Button type="submit" form="new-verification-form" :loading="submitting" :disabled="locked || copying || resourceBusy || resourcePending || sourceLoading || Boolean(sourceError) || (inputKind === 'platform' && (platformLoading || Boolean(platformError) || !platformPreview)) || configLoading || Boolean(configError) || !readiness?.ready">开始复测</Button>
    </template>
  </Dialog>
</template>
<script setup lang="ts">
import { computed, onBeforeUnmount, reactive, ref, watch } from 'vue'
import { Paperclip } from '@lucide/vue'
import { ApiError, listAllVulnerabilities } from '@/api/client'
import { useQuery } from '@tanstack/vue-query'
import { vulnerabilityKeys } from '@/features/vulnerabilities/vulnerabilityQueries'
import { vulnerabilitySeverityMeta } from '@/features/vulnerabilities/vulnerabilityMetadata'
import { createCreationRequestId } from '@/api/projectSchedules'
import { createVerificationBatch, createVerificationRequest, getVerificationReadiness, getVerificationRequest, getVerificationSourcePreview, type CreateVerificationRequest, type VerificationBatch, type VerificationDetail, type VerificationMode, type VerificationReadiness, type VerificationReportInput, type VerificationSourcePreview as SourcePreview } from '@/api/verificationRequests'
import { Alert, Button, Dialog, FormField, Input, Popover, Select, Skeleton, Textarea } from '@/components/ui'
import { ErrorState } from '@/components/patterns'
import { useAuthStore } from '@/stores/auth'
import SafeReportMarkdown from '@/features/reports/SafeReportMarkdown.vue'
import VerificationMaterials, { type PendingVerificationFile } from './VerificationMaterials.vue'
import VerificationSourcePreview from './VerificationSourcePreview.vue'
import { errorMessage, modeLabels, statusLabels } from './verificationPresentation'
import './verifications.css'
const props = withDefaults(defineProps<{ sourceRequestId?: string; sourceProjectId?: string; sourceVulnerabilityId?: string; sourceObservationId?: string; initialInput?: string; fixedSource?: boolean }>(), { sourceRequestId: '', sourceProjectId: '', sourceVulnerabilityId: '', sourceObservationId: '', initialInput: 'platform', fixedSource: false })
const emit = defineEmits<{ (event: 'close'): void; (event: 'accepted', request: VerificationDetail): void; (event: 'closeAutoFocus', value: Event): void }>()
const auth = useAuthStore()
const authIdentity = computed(() => JSON.stringify([auth.authGeneration, auth.session?.user_id, auth.session?.tenant_id, auth.session?.role, auth.isSuperAdmin]))
const sourceId = computed(() => props.sourceRequestId)
const formScope = computed(() => JSON.stringify([authIdentity.value, sourceId.value, props.sourceProjectId, props.sourceVulnerabilityId, props.sourceObservationId, props.initialInput, props.fixedSource]))
const form = reactive({ title: '', external_id: '', mode: 'retest', origin: '', reportType: 'markdown', content: '', mainMemberId: '', authorization: '', deadline: '', sourceProjectId: '', sourceVulnerabilityId: '', sourceObservationId: '' })
const inputKind = ref(sourceId.value ? 'copied' : props.initialInput === 'manual' ? 'manual' : 'platform')
const inputOptions = computed(() => [
  ...(source.value ? [{ value: 'copied', label: '上一轮输入快照' }] : []),
  { value: 'platform', label: source.value?.input_source ? '当前平台漏洞' : '平台已有漏洞' },
  { value: 'manual', label: '手工资料' },
])
const sourceSearch = ref('')
const sourceSeverity = ref('all')
const severityOptions = [{ value: 'all', label: '全部严重程度' }, ...(['critical', 'high', 'medium', 'low', 'info'] as const).map((value) => ({ value, label: vulnerabilitySeverityMeta(value).label }))]
const { data: vulnerabilities, isLoading: vulnerabilitiesLoading, error: vulnerabilitiesError, refetch: refetchVulnerabilities } = useQuery({
  queryKey: computed(() => [...vulnerabilityKeys.allList(sourceSeverity.value === 'all' ? '' : sourceSeverity.value, ''), 'current-user', authIdentity.value]),
  queryFn: ({ signal }) => listAllVulnerabilities(sourceSeverity.value === 'all' ? undefined : sourceSeverity.value, undefined, signal, true),
  enabled: computed(() => !props.fixedSource && inputKind.value === 'platform' && Boolean(auth.session?.tenant_id)), retry: false, refetchOnWindowFocus: false,
})
const platformPreview = ref<SourcePreview | null>(null)
const platformLoading = ref(false)
const platformError = ref('')
const vulnerabilityKey = (item: { project_id: string; id: string }) => JSON.stringify([item.project_id, item.id])
const selectedVulnerabilityKey = computed(() => form.sourceProjectId && form.sourceVulnerabilityId ? JSON.stringify([form.sourceProjectId, form.sourceVulnerabilityId]) : '')
const vulnerabilityOptions = computed(() => {
  const search = sourceSearch.value.toLocaleLowerCase()
  const items = (vulnerabilities.value || []).filter((item) => `${item.title}\n${item.project_title}\n${item.target}\n${item.id}`.toLocaleLowerCase().includes(search))
  const choices = items.map((item) => ({ value: vulnerabilityKey(item), label: `${item.project_title} · ${item.title}（${item.id}）` }))
  if (platformPreview.value && !choices.some((item) => item.value === selectedVulnerabilityKey.value)) choices.unshift({ value: selectedVulnerabilityKey.value, label: `${platformPreview.value.project_title} · ${platformPreview.value.vulnerability.title}（已选）` })
  return choices
})
function selectPlatformVulnerability(value: string) {
  const item = vulnerabilities.value?.find((candidate) => vulnerabilityKey(candidate) === value)
  if (!item) return
  form.sourceProjectId = item.project_id; form.sourceVulnerabilityId = item.id; form.sourceObservationId = ''
  form.title = item.title; dirty.value = true
}
function changeInputKind(value: string) {
  if (locked.value || hasCopiedMaterials.value || resourceBusy.value || copying.value) return
  inputKind.value = value; dirty.value = true
}
let platformGeneration = 0
let platformController: AbortController | null = null
async function loadPlatformPreview() {
  const generation = ++platformGeneration
  platformController?.abort()
  const controller = new AbortController()
  platformController = controller
  platformPreview.value = null; platformError.value = ''; platformLoading.value = false
  if (!auth.session?.tenant_id || inputKind.value !== 'platform' || !form.sourceProjectId || !form.sourceVulnerabilityId) return
  const projectId = form.sourceProjectId; const vulnerabilityId = form.sourceVulnerabilityId
  platformLoading.value = true
  try {
    const preview = await getVerificationSourcePreview(projectId, vulnerabilityId, controller.signal, form.sourceObservationId || undefined)
    if (generation !== platformGeneration) return
    if (preview.project_id !== projectId || preview.vulnerability_id !== vulnerabilityId || (form.sourceObservationId && preview.evidence.observation_id !== form.sourceObservationId)) throw new Error('来源响应与所选漏洞不一致，请重新读取。')
    platformPreview.value = preview
    if (!form.title) form.title = `复测 · ${preview.vulnerability.title}`
  } catch (error) { if (generation === platformGeneration) platformError.value = errorMessage(error) }
  finally { if (generation === platformGeneration) platformLoading.value = false }
}
const modeOptions = Object.entries(modeLabels).map(([value, label]) => ({ value, label }))
const reportOptions = [{ value: 'markdown', label: 'Markdown 正文' }, { value: 'text', label: '纯文本正文' }, { value: 'file', label: '上传的原件' }]
const editorTab = ref('edit')
const materialsOpen = ref(false)
const materialsConfirm = ref(false)
const files = ref<PendingVerificationFile[]>([])
const failedFiles = computed(() => files.value.filter((file) => file.error).length)
const batch = ref<VerificationBatch | null>(null)
const hasCopiedMaterials = computed(() => Boolean(batch.value?.members.some((member) => member.source_resource_id)))
const source = ref<VerificationDetail | null>(null)
const sourceLoading = ref(false)
const sourceError = ref('')
const copyError = ref('')
const copying = ref(false)
const resourceBusy = ref(false)
const resourcePending = computed(() => files.value.length > 0)
const dirty = ref(false)
const readiness = ref<VerificationReadiness | null>(null)
const configLoading = ref(false)
const configError = ref('')
const mainFileOptions = computed(() => (batch.value?.members || []).filter((member) => /\.(md|txt|docx|png|jpe?g)$/i.test(member.path)).map((member) => ({ value: member.id, label: member.path })))
const pending = ref<CreateVerificationRequest | null>(null)
const created = ref<VerificationDetail | null>(null)
const submitting = ref(false)
const submissionError = ref('')
const acceptance = ref<'unconfirmed' | 'rejected' | 'batch_expired' | 'deleted' | 'source_deleted'>('rejected')
const definitelyRejected = computed(() => acceptance.value !== 'unconfirmed')
const locked = computed(() => !auth.session?.tenant_id || submitting.value || Boolean(pending.value || created.value))
let requestId = createCreationRequestId()
let formGeneration = 0
let configGeneration = 0
async function loadConfig() {
  if (!auth.session?.tenant_id || configLoading.value) return
  const generation = ++configGeneration
  configLoading.value = true
  configError.value = ''
  try { const current = await getVerificationReadiness(); if (generation === configGeneration) readiness.value = current }
  catch (error) { if (generation === configGeneration) configError.value = errorMessage(error) }
  finally { if (generation === configGeneration) configLoading.value = false }
}
let sourceGeneration = 0
let sourceController: AbortController | null = null
async function loadSource() {
  sourceController?.abort()
  if (!auth.session?.tenant_id || !sourceId.value) return
  const controller = new AbortController()
  sourceController = controller
  const generation = ++sourceGeneration
  sourceLoading.value = true
  sourceError.value = ''
  try {
    const detail = await getVerificationRequest(sourceId.value, controller.signal)
    if (generation !== sourceGeneration) return
    if (detail.id !== sourceId.value) throw new Error('上一轮信息身份不匹配。')
    if (detail.purge_state === 'pending' || !detail.report) throw new Error('来源复测已进入永久清理，不能再复制或受理。请从原漏洞新建复测。')
    source.value = detail
    form.title = detail.title
    form.external_id = detail.external_id || ''
    // 新一轮只复用名称与资料入口，环境补充和期限由本轮选填。
    form.origin = ''
    form.mode = 'retest'
    form.reportType = 'file'
    inputKind.value = 'copied'
    if (detail.input_source) { form.sourceProjectId = detail.input_source.project_id; form.sourceVulnerabilityId = detail.input_source.vulnerability_id }
  } catch (error) { if (generation === sourceGeneration) sourceError.value = errorMessage(error) }
  finally { if (generation === sourceGeneration) sourceLoading.value = false }
}
async function copyOriginals() {
  if (!source.value?.report || source.value.purge_state === 'pending' || batch.value || copying.value || locked.value) return
  const generation = formGeneration
  const current = source.value
  copying.value = true
  copyError.value = ''
  try {
    const copied = await createVerificationBatch(current.id)
    if (generation !== formGeneration) return
    batch.value = copied
    const member = copied.members.find((item) => item.source_resource_id === current.report?.resource_id)
    form.mainMemberId = member?.id || ''
    if (!member) copyError.value = '批次已复制，但未定位合法主资料；请从成员中明确选择主文件。'
    dirty.value = true
  } catch (error) { if (generation === formGeneration) copyError.value = `复制结果未确认，尚未绑定新请求。${errorMessage(error)}` }
  finally { if (generation === formGeneration) copying.value = false }
}
function futureTime(value: string, label: string): string | null {
  if (!value) return null
  const date = new Date(value)
  if (!Number.isFinite(date.getTime()) || date.getTime() <= Date.now()) throw new Error(`${label}必须是有效的未来时间。`)
  return date.toISOString()
}
function submit() {
  if (locked.value || copying.value || resourceBusy.value || resourcePending.value) return
  submissionError.value = ''
  try {
    if (!form.title.trim()) throw new Error('请填写任务名称。')
    if (configLoading.value || !readiness.value?.ready || configError.value) throw new Error('复测执行配置尚未就绪，请重新核对或前往漏洞复测配置。')
    if (batch.value && (batch.value.state !== 'open' || Date.parse(batch.value.expires_at) <= Date.now())) throw new Error('资料批次已消费或过期，不能作为新的请求提交。')
    let report: VerificationReportInput
    if (inputKind.value === 'platform') {
      if (platformLoading.value || platformError.value || !platformPreview.value || platformPreview.value.project_id !== form.sourceProjectId || platformPreview.value.vulnerability_id !== form.sourceVulnerabilityId) throw new Error('请先选择并读取本轮来源漏洞及历史证据。')
      report = { type: 'platform_vulnerability', project_id: form.sourceProjectId, vulnerability_id: form.sourceVulnerabilityId, ...(platformPreview.value.evidence.observation_id ? { observation_id: platformPreview.value.evidence.observation_id } : {}) }
    } else if (inputKind.value === 'copied') {
      const member = batch.value?.members.find((item) => item.id === form.mainMemberId && item.source_resource_id === source.value?.report?.resource_id)
      if (!member) throw new Error('请明确复制上一轮输入；必须保留对应的主资料，不能用其他附件代替。')
      report = { type: 'file', batch_member_id: member.id }
    } else if (form.reportType === 'file') {
      if (!mainFileOptions.value.some((item) => item.value === form.mainMemberId)) throw new Error('请上传资料，并选择本轮合法主文件。')
      report = { type: 'file', batch_member_id: form.mainMemberId }
    } else {
      if (!form.content.trim()) throw new Error('主资料正文不能为空。')
      report = { type: form.reportType as 'markdown' | 'text', content: form.content }
    }
    // 一旦发出就锁定原始请求快照，重试不重新读取表单或本次执行配置。
    pending.value = {
      schema_version: 1, request_id: requestId, title: form.title,
      external_id: form.external_id || null, mode: form.mode as VerificationMode,
      origin: form.origin, report,
      authorization_expires_at: futureTime(form.authorization, '授权到期时间'),
      execution_deadline: futureTime(form.deadline, '执行停止时间'),
      ...(batch.value ? { resource_batch_id: batch.value.id } : {}),
      ...(source.value ? { previous_request_id: source.value.id } : {}),
    }
    materialsOpen.value = false
    void submitPending()
  } catch (error) { submissionError.value = errorMessage(error) }
}
async function submitPending() {
  if (!auth.session?.tenant_id || !pending.value || submitting.value || created.value) return
  const generation = formGeneration
  const request = pending.value
  submitting.value = true
  try {
    const accepted = await createVerificationRequest(request)
    if (generation !== formGeneration) return
    created.value = accepted
    pending.value = null
    submissionError.value = ''
  } catch (error) {
    if (generation !== formGeneration) return
    // 同键查找先于批次校验，只有明确的批次过期响应能证明此前未知的请求也未受理。
    const expiredBatch = Boolean(request.resource_batch_id) && error instanceof ApiError && error.status === 410 &&
      (error.data as { detail?: unknown } | null)?.detail === 'Project resource batch expired'
    const deletedRequest = error instanceof ApiError && error.status === 410 &&
      (error.data as { detail?: unknown } | null)?.detail === '此 request_id 对应的复测记录已删除，请使用新的请求 ID'
    const deletedSource = error instanceof ApiError && error.status === 410 &&
      (error.data as { detail?: unknown } | null)?.detail === '来源复测已删除，不能复制或作为上一轮受理，请从原漏洞新建复测'
    if (deletedRequest) acceptance.value = 'deleted'
    else if (deletedSource) acceptance.value = 'source_deleted'
    else if (expiredBatch) acceptance.value = 'batch_expired'
    else if (acceptance.value !== 'unconfirmed' && error instanceof ApiError && [400, 401, 403, 404, 413, 415, 422].includes(error.status)) acceptance.value = 'rejected'
    else acceptance.value = 'unconfirmed'
    submissionError.value = deletedRequest
      ? '原请求对应的复测记录已删除，不会自动重新执行。请关闭此表单。'
      : deletedSource
        ? '服务端确认新一轮未受理：来源复测已删除或进入清理，不能继续引用旧快照。请从原漏洞新建复测。'
        : expiredBatch
          ? '服务端确认原请求未受理，资料批次已过期。请更换过期资料后继续；正文和补充说明会保留，原文件需重新上传或复制。'
          : `${definitelyRejected.value ? '服务端明确拒绝受理。' : '请求可能已受理，请勿另建请求。'}${errorMessage(error)}`
  } finally { if (generation === formGeneration) submitting.value = false }
  if (generation === formGeneration && created.value) await enterCreated()
}
function editRejected() {
  if (!definitelyRejected.value || submitting.value) return
  if (acceptance.value === 'batch_expired') {
    batch.value = null
    form.mainMemberId = ''
    submissionError.value = '已解除过期批次引用，正文和补充说明已保留。请在“资料”中重新上传，或明确复制上一轮输入。'
  } else submissionError.value = ''
  pending.value = null
  acceptance.value = 'rejected'
}
function enterCreated() {
  if (created.value) emit('accepted', created.value)
}
let leaveApproved = false
function canLeave() {
  if (submitting.value || copying.value || resourceBusy.value || sourceLoading.value || materialsConfirm.value) return false
  if (pending.value && !definitelyRejected.value) {
    submissionError.value = '受理结果未知，已保留原请求。请先用原请求重试确认。'
    return false
  }
  if (leaveApproved || created.value || acceptance.value === 'deleted' || acceptance.value === 'source_deleted' || (!dirty.value && !pending.value && !batch.value && !resourcePending.value)) return true
  leaveApproved = window.confirm('离开将丢失未提交的正文、补充说明与待上传文件。仍要离开吗？')
  return leaveApproved
}
function requestClose() { if (canLeave()) emit('close') }
defineExpose({ canLeave })
function beforeUnload(event: BeforeUnloadEvent) {
  if (created.value || acceptance.value === 'deleted' || acceptance.value === 'source_deleted' || (!dirty.value && !pending.value && !batch.value && !resourcePending.value && !resourceBusy.value)) return
  event.preventDefault()
  event.returnValue = ''
}
window.addEventListener('beforeunload', beforeUnload)
onBeforeUnmount(() => { formGeneration++; configGeneration++; sourceGeneration++; sourceController?.abort(); platformGeneration++; platformController?.abort(); window.removeEventListener('beforeunload', beforeUnload) })
watch(batch, (current) => {
  if (form.mainMemberId && !current?.members.some((member) => member.id === form.mainMemberId)) form.mainMemberId = ''
})
watch(formScope, () => {
  formGeneration++; configGeneration++; sourceGeneration++; sourceController?.abort(); platformGeneration++; platformController?.abort(); leaveApproved = false
  source.value = null; batch.value = null; files.value = []; pending.value = null; created.value = null
  sourceLoading.value = false; sourceError.value = ''; submissionError.value = ''; copyError.value = ''; dirty.value = false; acceptance.value = 'rejected'
  platformPreview.value = null; platformError.value = ''; platformLoading.value = false
  readiness.value = null; configLoading.value = false; configError.value = ''
  copying.value = false; submitting.value = false; resourceBusy.value = false; materialsOpen.value = false; materialsConfirm.value = false
  editorTab.value = 'edit'; sourceSearch.value = ''; sourceSeverity.value = 'all'
  requestId = createCreationRequestId()
  inputKind.value = sourceId.value ? 'copied' : props.initialInput === 'manual' ? 'manual' : 'platform'
  Object.assign(form, { title: '', external_id: '', mode: 'retest', origin: '', reportType: 'markdown', content: '', mainMemberId: '', authorization: '', deadline: '', sourceProjectId: props.sourceProjectId, sourceVulnerabilityId: props.sourceVulnerabilityId, sourceObservationId: props.sourceObservationId })
}, { immediate: true, flush: 'sync' })
watch(formScope, () => { void loadSource(); void loadConfig() }, { immediate: true })
watch([formScope, inputKind, () => form.sourceProjectId, () => form.sourceVulnerabilityId, () => form.sourceObservationId], () => { void loadPlatformPreview() }, { immediate: true })
</script>
