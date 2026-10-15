<template>
  <Dialog :open="true" :title="sourceId ? '再次复测' : '新建复测'" description="每次复测独立留档，提交后排队执行。" size="lg" content-class="verification-theme verification-create-dialog" overlay-class="verification-create-overlay" :dismissible="!submitting && !copying && !resourceBusy && !materialsConfirm" :close-on-outside="false" @update:open="!$event && requestClose()">
    <Skeleton v-if="sourceLoading" aria-label="正在读取上一轮信息" />
    <ErrorState v-else-if="sourceError" title="上一轮信息读取失败" :description="sourceError" @retry="loadSource" />
    <Alert v-if="created" tone="success" title="请求已受理">当前状态：{{ statusLabels[created.execution_status] }}。<template #actions><Button @click="enterCreated">进入已受理记录</Button></template></Alert>
    <Alert v-if="submissionError" :tone="pending ? 'warning' : 'danger'" :title="pending ? '提交未完成确认' : '请检查输入'">
      {{ submissionError }}
      <p v-if="pending">请求 ID：{{ pending.request_id }}。<template v-if="!definitelyRejected">原请求正文已在本页内存锁定；请用原请求重试确认，不会更换 ID 或重复创建。未知受理结果确认前不能关闭本页。</template><template v-else>原请求未受理，可以恢复编辑；正文和授权会保留。</template></p>
      <template v-if="pending" #actions><Button v-if="acceptance !== 'batch_expired'" variant="secondary" :loading="submitting" @click="submitPending">用原请求重试</Button><Button v-if="definitelyRejected" variant="secondary" :disabled="submitting" @click="editRejected">{{ acceptance === 'batch_expired' ? '更换过期资料并继续编辑' : '修改未受理的输入' }}</Button></template>
    </Alert>
    <form id="new-verification-form" class="verification-form" @submit.prevent="submit">
      <fieldset class="verification-stack" :disabled="locked || sourceLoading || Boolean(sourceError)" @input="dirty = true" @change="dirty = true">
        <div class="verification-grid verification-create-basics">
          <FormField label="任务名称" for="new-verification-title" required><Input id="new-verification-title" v-model="form.title" required placeholder="例如：订单访问控制修复后复测" /></FormField>
          <FormField label="模式" for="new-verification-mode" required><Select content-class="verification-theme verification-select" id="new-verification-mode" v-model="form.mode" :options="modeOptions" :disabled="locked" /></FormField>
        </div>
        <div class="verification-readiness" role="status">
          <span v-if="configLoading">正在核对本租户执行配置…</span>
          <template v-else-if="readiness"><Badge :tone="readiness.ready ? 'info' : 'warning'" size="sm">{{ readiness.ready ? '执行配置就绪' : '暂不可提交' }}</Badge><span>{{ readiness.worker_name || '未配置专用 Worker' }} · {{ readiness.model_display_name || readiness.model_name || '未配置模型' }} · {{ readiness.message }}</span></template>
          <span v-if="configError">配置读取失败：{{ configError }}</span>
          <Button size="sm" variant="ghost" :disabled="locked" @click="loadConfig">重新核对</Button>
          <RouterLink v-if="auth.isSuperAdmin && !readiness?.ready" :to="{ name: 'VerificationConfiguration' }">漏洞复测配置</RouterLink>
          <span v-else-if="!auth.isSuperAdmin && !readiness?.ready">请联系管理员配置。</span>
        </div>
        <p v-if="source" class="verification-muted">已预填任务名称。原授权、期限及执行者不沿用。复制上一轮输入保留当时快照；选择平台已有漏洞则重新读取当前原文，二者不会自动替换。</p>
        <FormField label="本轮输入来源" for="new-verification-input-kind" required><Select content-class="verification-theme verification-select" id="new-verification-input-kind" :model-value="inputKind" :options="inputOptions" :disabled="locked || resourceBusy || copying || hasCopiedMaterials" @update:model-value="changeInputKind" /><p v-if="hasCopiedMaterials" class="verification-muted">已复制旧输入。若要改为当前漏洞或手工资料，请先在“资料”中重新准备本轮资料，避免混用两个版本。</p></FormField>
        <div class="verification-editor">
          <template v-if="inputKind === 'platform'">
            <div class="verification-source-picker verification-stack">
              <div class="verification-grid"><FormField label="查找漏洞" for="verification-source-search"><Input id="verification-source-search" v-model="sourceSearch" placeholder="筛选漏洞名称、目标或项目" /></FormField><FormField label="严重程度" for="verification-source-severity"><Select id="verification-source-severity" v-model="sourceSeverity" content-class="verification-theme verification-select" :options="severityOptions" :disabled="locked" /></FormField></div>
              <FormField label="平台已有漏洞" for="verification-source-vulnerability" required><Select id="verification-source-vulnerability" :model-value="selectedVulnerabilityKey" content-class="verification-theme verification-select" :options="vulnerabilityOptions" :disabled="locked || vulnerabilitiesLoading" placeholder="选择来源项目与漏洞" @update:model-value="selectPlatformVulnerability" /></FormField>
              <p v-if="vulnerabilitiesLoading" class="verification-muted">正在读取已有漏洞…</p><Alert v-if="vulnerabilitiesError" tone="warning" title="漏洞列表读取失败">{{ errorMessage(vulnerabilitiesError) }}<template #actions><Button variant="secondary" size="sm" @click="refetchVulnerabilities()">重新读取</Button></template></Alert>
              <p v-if="!vulnerabilitiesLoading && !vulnerabilitiesError && !vulnerabilityOptions.length" class="verification-muted">当前没有匹配漏洞，可调整筛选或使用手工资料。</p>
              <Skeleton v-if="platformLoading" aria-label="正在读取漏洞原文与历史证据" /><Alert v-else-if="platformError" tone="danger" title="来源资料尚未读取">{{ platformError }}<template #actions><Button variant="secondary" size="sm" @click="loadPlatformPreview">重新读取</Button></template></Alert>
              <VerificationSourcePreview v-else-if="platformPreview" :preview="platformPreview" />
              <p v-else class="verification-muted">选择后展示原文与直接关联的历史证据，不要求复制或改写旧报告。</p>
            </div>
          </template>
          <div v-else-if="inputKind === 'copied'" class="verification-editor-file">
            <h3>复制上一轮输入快照</h3><p class="verification-muted">{{ source?.input_source ? '保留原平台漏洞来源和当次输入，不重新读取当前漏洞。须保留复制的全部原始成员；如需改变旧输入，请改为当前漏洞或手工资料。' : '复制上一轮输入原件，不复制当轮执行结果或新证据。' }}</p>
            <Button variant="secondary" :loading="copying" :disabled="locked || Boolean(batch) || resourceBusy" @click="copyOriginals">{{ hasCopiedMaterials ? '已复制上一轮输入' : batch ? '请先在资料中重新准备，再复制输入' : '复制上一轮输入' }}</Button>
            <p v-if="form.mainMemberId" class="verification-muted">主资料：{{ batch?.members.find((member) => member.id === form.mainMemberId)?.path }}</p>
            <RouterLink v-if="source" :to="{ name: 'VerificationDetail', params: { id: source.id } }" target="_blank">查看上一轮输入与来源</RouterLink>
            <Alert v-if="copyError" tone="warning" title="原件复制未完成">{{ copyError }}</Alert>
          </div>
          <div v-else class="verification-editor-toolbar">
            <div class="verification-segments" aria-label="主资料编辑模式"><Button size="sm" :variant="editorTab === 'edit' ? 'secondary' : 'ghost'" :aria-pressed="editorTab === 'edit'" @click="editorTab = 'edit'">编辑</Button><Button size="sm" :variant="editorTab === 'preview' ? 'secondary' : 'ghost'" :aria-pressed="editorTab === 'preview'" @click="editorTab = 'preview'">预览</Button></div>
            <label class="verification-report-select">主资料<Select content-class="verification-theme verification-select" id="new-verification-report-type" v-model="form.reportType" :options="reportOptions" :disabled="locked" aria-label="主资料来源" /></label>
          </div>
          <template v-if="inputKind === 'manual' && form.reportType !== 'file'">
            <Textarea v-show="editorTab === 'edit'" id="new-verification-content" v-model="form.content" class="verification-editor-input" :rows="9" :required="editorTab === 'edit'" aria-label="主资料正文" placeholder="用 Markdown 保留原始步骤、请求、观察与修复变化；无需固定标题。授权范围请在下方单独填写。" />
            <div v-if="editorTab === 'preview'" class="verification-editor-preview"><SafeReportMarkdown v-if="form.reportType === 'markdown' && form.content" :content="form.content" /><p v-else class="verification-text">{{ form.content || '尚无主资料正文。' }}</p></div>
          </template>
          <div v-else-if="inputKind === 'manual'" class="verification-editor-file">
            <FormField label="主资料文件" for="new-verification-main-file" required><Select content-class="verification-theme verification-select" id="new-verification-main-file" v-model="form.mainMemberId" :options="mainFileOptions" :disabled="locked || resourceBusy" placeholder="打开资料，上传或复制后选择主文件" /></FormField>
            <p class="verification-muted">支持 MD、TXT、DOCX、PNG、JPEG 原件；其他合法资料成员保留为附件。上传不代表已理解，按实际执行结果判断。</p>
          </div>
          <div class="verification-editor-materials">
            <Popover v-model:open="materialsOpen" side="top" align="start" size="lg" content-class="verification-theme verification-materials-popover" :dismissible="!resourceBusy && !materialsConfirm">
              <template #trigger><Button size="sm" variant="secondary" :disabled="locked || resourceBusy || materialsConfirm"><template #icon><Paperclip :size="14" /></template>资料{{ batch?.members.length || files.length ? `（${(batch?.members.length || 0) + files.length}）` : '' }}</Button></template>
              <div class="verification-stack">
                <header class="verification-heading"><h2>本轮资料</h2><Button size="sm" variant="ghost" :disabled="resourceBusy || materialsConfirm" @click="materialsOpen = false">收起</Button></header>
                <p v-if="inputKind === 'copied'" class="verification-muted">旧输入复制在上方明确操作；这里可补充本轮必要资料，不把历史执行证据作为新证据。</p>
                <VerificationMaterials v-model="batch" v-model:files="files" :disabled="locked || copying" @busy="resourceBusy = $event" @confirming="materialsConfirm = $event" />
              </div>
            </Popover>
            <span v-for="member in batch?.members" :key="member.id" class="verification-attachment" :title="member.path">{{ member.id === form.mainMemberId && (inputKind === 'copied' || (inputKind === 'manual' && form.reportType === 'file')) ? '主资料 · ' : '' }}{{ member.path }}</span>
            <span v-if="files.length" class="verification-muted">{{ files.length }} 个待上传{{ failedFiles ? `，${failedFiles} 个失败或待核对` : '' }}</span>
            <span v-if="resourceBusy" class="verification-muted">资料操作中，请勿离开</span>
          </div>
        </div>
        <FormField label="本次任务与实际授权范围、环境及修复变化" for="new-verification-origin" required>
          <Textarea id="new-verification-origin" v-model="form.origin" class="verification-origin" :rows="4" required placeholder="补充本轮目标、当前环境和身份、明确授权范围与排除项，以及本次修复或部署变化（未知请说明）；不必重抄漏洞正文，历史授权和报告内地址不会自动沿用。" />
        </FormField>
        <details class="verification-extra-settings"><summary>外部编号与可选期限</summary><div class="verification-stack">
          <FormField label="外部漏洞编号（可选）" for="new-verification-external"><Input id="new-verification-external" v-model="form.external_id" placeholder="例如：ISSUE-123" /></FormField>
          <div class="verification-grid"><FormField label="授权到期时间（本地时间，可选）" for="new-verification-authorization"><Input id="new-verification-authorization" v-model="form.authorization" type="datetime-local" /></FormField><FormField label="执行停止时间（本地时间，可选）" for="new-verification-deadline"><Input id="new-verification-deadline" v-model="form.deadline" type="datetime-local" /></FormField></div>
          <p class="verification-muted">留空表示不设置期限；再次复测不继承历史期限。使用本轮开始执行时的专用配置，已开始后不热切换。</p>
        </div></details>
      </fieldset>
    </form>
    <template #footer>
      <span class="verification-muted verification-footer-summary">{{ resourcePending ? '请先完成上传或移出待上传项' : '正文和授权仅保留在本页，不写入浏览器存储' }}</span>
      <Button variant="secondary" :disabled="submitting || copying || resourceBusy" @click="requestClose">取消</Button>
      <Button type="submit" form="new-verification-form" :loading="submitting" :disabled="locked || copying || resourceBusy || resourcePending || sourceLoading || Boolean(sourceError) || (inputKind === 'platform' && (platformLoading || Boolean(platformError) || !platformPreview)) || configLoading || Boolean(configError) || !readiness?.ready">开始复测</Button>
    </template>
  </Dialog>
</template>
<script setup lang="ts">
import { computed, onBeforeUnmount, reactive, ref, watch } from 'vue'
import { onBeforeRouteLeave, onBeforeRouteUpdate, useRoute, useRouter } from 'vue-router'
import { Paperclip } from '@lucide/vue'
import { ApiError, listAllVulnerabilities } from '@/api/client'
import { useQuery } from '@tanstack/vue-query'
import { vulnerabilityKeys } from '@/features/vulnerabilities/vulnerabilityQueries'
import { vulnerabilitySeverityMeta } from '@/features/vulnerabilities/vulnerabilityMetadata'
import { createCreationRequestId } from '@/api/projectSchedules'
import { createVerificationBatch, createVerificationRequest, getVerificationReadiness, getVerificationRequest, getVerificationSourcePreview, type CreateVerificationRequest, type VerificationBatch, type VerificationDetail, type VerificationMode, type VerificationReadiness, type VerificationReportInput, type VerificationSourcePreview as SourcePreview } from '@/api/verificationRequests'
import { Alert, Badge, Button, Dialog, FormField, Input, Popover, Select, Skeleton, Textarea } from '@/components/ui'
import { ErrorState } from '@/components/patterns'
import { useAuthStore } from '@/stores/auth'
import SafeReportMarkdown from '@/features/reports/SafeReportMarkdown.vue'
import VerificationMaterials, { type PendingVerificationFile } from './VerificationMaterials.vue'
import VerificationSourcePreview from './VerificationSourcePreview.vue'
import { errorMessage, modeLabels, statusLabels, verificationListQuery } from './verificationPresentation'
import './verifications.css'
const emit = defineEmits<{ (event: 'close'): void }>()
const route = useRoute()
const router = useRouter()
const auth = useAuthStore()
const sourceId = computed(() => typeof route.query.source_request_id === 'string' ? route.query.source_request_id : '')
const form = reactive({ title: '', external_id: '', mode: 'retest', origin: '', reportType: 'markdown', content: '', mainMemberId: '', authorization: '', deadline: '', sourceProjectId: '', sourceVulnerabilityId: '' })
const inputKind = ref(sourceId.value ? 'copied' : route.query.input === 'manual' ? 'manual' : 'platform')
const inputOptions = computed(() => [
  ...(source.value ? [{ value: 'copied', label: '复制上一轮输入（保留快照）' }] : []),
  { value: 'platform', label: source.value?.input_source ? '平台已有漏洞（重新读取当前原文）' : '平台已有漏洞' },
  { value: 'manual', label: '手工资料' },
])
const sourceSearch = ref('')
const sourceSeverity = ref('all')
const severityOptions = [{ value: 'all', label: '全部严重程度' }, ...(['critical', 'high', 'medium', 'low', 'info'] as const).map((value) => ({ value, label: vulnerabilitySeverityMeta(value).label }))]
const { data: vulnerabilities, isLoading: vulnerabilitiesLoading, error: vulnerabilitiesError, refetch: refetchVulnerabilities } = useQuery({
  queryKey: computed(() => [...vulnerabilityKeys.allList(sourceSeverity.value === 'all' ? '' : sourceSeverity.value, ''), 'current-tenant', auth.session?.tenant_id]),
  queryFn: ({ signal }) => listAllVulnerabilities(sourceSeverity.value === 'all' ? undefined : sourceSeverity.value, undefined, signal, true),
  enabled: computed(() => inputKind.value === 'platform' && Boolean(auth.session?.tenant_id)), retry: false, refetchOnWindowFocus: false,
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
  form.sourceProjectId = item.project_id; form.sourceVulnerabilityId = item.id
  form.title = item.title; dirty.value = true
}
function changeInputKind(value: string) {
  if (locked.value || hasCopiedMaterials.value || resourceBusy.value || copying.value) return
  inputKind.value = value; dirty.value = true
}
let platformGeneration = 0
async function loadPlatformPreview() {
  const generation = ++platformGeneration
  platformPreview.value = null; platformError.value = ''; platformLoading.value = false
  if (inputKind.value !== 'platform' || !form.sourceProjectId || !form.sourceVulnerabilityId) return
  const projectId = form.sourceProjectId; const vulnerabilityId = form.sourceVulnerabilityId
  platformLoading.value = true
  try {
    const preview = await getVerificationSourcePreview(projectId, vulnerabilityId)
    if (generation !== platformGeneration) return
    if (preview.project_id !== projectId || preview.vulnerability_id !== vulnerabilityId) throw new Error('来源响应与所选漏洞不一致，请重新读取。')
    platformPreview.value = preview
    if (!form.title) form.title = preview.vulnerability.title
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
const acceptance = ref<'unconfirmed' | 'rejected' | 'batch_expired'>('rejected')
const definitelyRejected = computed(() => acceptance.value !== 'unconfirmed')
const locked = computed(() => submitting.value || Boolean(pending.value || created.value))
let requestId = createCreationRequestId()
async function loadConfig() {
  if (configLoading.value) return
  configLoading.value = true
  configError.value = ''
  try { readiness.value = await getVerificationReadiness() }
  catch (error) { configError.value = errorMessage(error) }
  finally { configLoading.value = false }
}
let sourceGeneration = 0
async function loadSource() {
  if (!sourceId.value) return
  const generation = ++sourceGeneration
  sourceLoading.value = true
  sourceError.value = ''
  try {
    const detail = await getVerificationRequest(sourceId.value)
    if (generation !== sourceGeneration) return
    source.value = detail
    form.title = detail.title
    form.external_id = detail.external_id || ''
    // 历史任务只预填名称和资料入口，本轮授权必须由用户重新表达。
    form.origin = ''
    form.mode = 'retest'
    form.reportType = 'file'
    inputKind.value = 'copied'
    if (detail.input_source) { form.sourceProjectId = detail.input_source.project_id; form.sourceVulnerabilityId = detail.input_source.vulnerability_id }
  } catch (error) { if (generation === sourceGeneration) sourceError.value = errorMessage(error) }
  finally { if (generation === sourceGeneration) sourceLoading.value = false }
}
async function copyOriginals() {
  if (!source.value || batch.value || copying.value || locked.value) return
  copying.value = true
  copyError.value = ''
  try {
    batch.value = await createVerificationBatch(source.value.id)
    const member = batch.value.members.find((item) => item.source_resource_id === source.value!.report.resource_id)
    form.mainMemberId = member?.id || ''
    if (!member) copyError.value = '批次已复制，但未定位合法主资料；请从成员中明确选择主文件。'
    dirty.value = true
  } catch (error) { copyError.value = `复制结果未确认，尚未绑定新请求。${errorMessage(error)}` }
  finally { copying.value = false }
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
    if (!form.title.trim() || !form.origin.trim()) throw new Error('请填写任务名称与当前实际授权范围。')
    if (!readiness.value?.ready || configError.value) throw new Error('本租户执行配置尚未就绪，请重新核对或联系管理员。')
    if (batch.value && (batch.value.state !== 'open' || Date.parse(batch.value.expires_at) <= Date.now())) throw new Error('资料批次已消费或过期，不能作为新的请求提交。')
    let report: VerificationReportInput
    if (inputKind.value === 'platform') {
      if (platformLoading.value || platformError.value || !platformPreview.value || platformPreview.value.project_id !== form.sourceProjectId || platformPreview.value.vulnerability_id !== form.sourceVulnerabilityId) throw new Error('请先选择并读取本轮来源漏洞及历史证据。')
      report = { type: 'platform_vulnerability', project_id: form.sourceProjectId, vulnerability_id: form.sourceVulnerabilityId }
    } else if (inputKind.value === 'copied') {
      const member = batch.value?.members.find((item) => item.id === form.mainMemberId && item.source_resource_id === source.value?.report.resource_id)
      if (!member) throw new Error('请明确复制上一轮输入；必须保留对应的主资料，不能用其他附件代替。')
      report = { type: 'file', batch_member_id: member.id }
    } else if (form.reportType === 'file') {
      if (!mainFileOptions.value.some((item) => item.value === form.mainMemberId)) throw new Error('请上传资料，并选择本轮合法主文件。')
      report = { type: 'file', batch_member_id: form.mainMemberId }
    } else {
      if (!form.content.trim()) throw new Error('主资料正文不能为空。')
      report = { type: form.reportType as 'markdown' | 'text', content: form.content }
    }
    // 一旦发出就锁定原始请求快照，重试不重新读取表单或本次管理员配置。
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
  if (!pending.value || submitting.value || created.value) return
  const request = pending.value
  submitting.value = true
  try {
    created.value = await createVerificationRequest(request)
    pending.value = null
    submissionError.value = ''
  } catch (error) {
    // 同键查找先于批次校验，只有明确的批次过期响应能证明此前未知的请求也未受理。
    const expiredBatch = Boolean(request.resource_batch_id) && error instanceof ApiError && error.status === 410 &&
      (error.data as { detail?: unknown } | null)?.detail === 'Project resource batch expired'
    if (expiredBatch) acceptance.value = 'batch_expired'
    else if (acceptance.value !== 'unconfirmed' && error instanceof ApiError && [400, 401, 403, 404, 413, 415, 422].includes(error.status)) acceptance.value = 'rejected'
    else acceptance.value = 'unconfirmed'
    submissionError.value = acceptance.value === 'batch_expired'
      ? '服务端确认原请求未受理，资料批次已过期。请更换过期资料后继续；正文和授权会保留，原文件需重新上传或复制。'
      : `${definitelyRejected.value ? '服务端明确拒绝受理。' : '请求可能已受理，请勿另建请求。'}${errorMessage(error)}`
  } finally { submitting.value = false }
  if (created.value) await enterCreated()
}
function editRejected() {
  if (!definitelyRejected.value || submitting.value) return
  if (acceptance.value === 'batch_expired') {
    batch.value = null
    form.mainMemberId = ''
    submissionError.value = '已解除过期批次引用，正文和授权已保留。请在“资料”中重新上传，或明确复制上一轮输入。'
  } else submissionError.value = ''
  pending.value = null
  acceptance.value = 'rejected'
}
async function enterCreated() {
  if (!created.value) return
  try { await router.push({ name: 'VerificationDetail', params: { id: created.value.id }, query: verificationListQuery(route.query) }) }
  catch { submissionError.value = '请求已受理，但页面跳转失败；请使用“进入已受理记录”，不要重复提交。' }
}
function canLeave() {
  if (submitting.value || copying.value || resourceBusy.value || sourceLoading.value || materialsConfirm.value) return false
  if (pending.value && !definitelyRejected.value) {
    submissionError.value = '受理结果未知，已保留原请求。请先用原请求重试确认；关闭或另建会失去同 ID 安全重试的依据。'
    return false
  }
  return Boolean(created.value) || (!dirty.value && !pending.value && !batch.value && !resourcePending.value) || window.confirm('离开将丢失未提交的正文、授权与待上传文件；待提交批次到期后回收，不影响历史原件。仍要离开吗？')
}
// 关闭交给路由守卫统一确认，避免关闭按钮与路由跳转重复询问。
function requestClose() { emit('close') }
onBeforeRouteLeave(canLeave)
onBeforeRouteUpdate((to) => {
  const stillOpen = to.name === 'NewVerification' || to.query.create === '1'
  return (stillOpen && ['source_request_id', 'source_project_id', 'source_vulnerability_id', 'input'].every((key) => to.query[key] === route.query[key])) || canLeave()
})
function beforeUnload(event: BeforeUnloadEvent) {
  if (created.value || (!dirty.value && !pending.value && !batch.value && !resourcePending.value && !resourceBusy.value)) return
  event.preventDefault()
  event.returnValue = ''
}
window.addEventListener('beforeunload', beforeUnload)
onBeforeUnmount(() => { sourceGeneration++; platformGeneration++; window.removeEventListener('beforeunload', beforeUnload) })
watch(batch, (current) => {
  if (form.mainMemberId && !current?.members.some((member) => member.id === form.mainMemberId)) form.mainMemberId = ''
})
watch(() => JSON.stringify([sourceId.value, route.query.source_project_id, route.query.source_vulnerability_id, route.query.input]), () => {
  sourceGeneration++; platformGeneration++
  source.value = null; batch.value = null; files.value = []; pending.value = null; created.value = null
  sourceLoading.value = false; sourceError.value = ''; submissionError.value = ''; copyError.value = ''; dirty.value = false; acceptance.value = 'rejected'
  platformPreview.value = null; platformError.value = ''; platformLoading.value = false
  requestId = createCreationRequestId()
  inputKind.value = sourceId.value ? 'copied' : route.query.input === 'manual' ? 'manual' : 'platform'
  Object.assign(form, { title: '', external_id: '', mode: 'retest', origin: '', reportType: 'markdown', content: '', mainMemberId: '', authorization: '', deadline: '', sourceProjectId: typeof route.query.source_project_id === 'string' ? route.query.source_project_id : '', sourceVulnerabilityId: typeof route.query.source_vulnerability_id === 'string' ? route.query.source_vulnerability_id : '' })
  void loadSource()
}, { immediate: true })
watch([inputKind, () => form.sourceProjectId, () => form.sourceVulnerabilityId], () => { void loadPlatformPreview() }, { immediate: true })
void loadConfig()
</script>
