<template>
  <section class="project-agent workbench-surface" :class="{ 'has-deliverables': showWork && hasDeliverables }" aria-label="项目 Agent">
    <aside class="agent-sidebar" aria-label="会话导航">
      <div class="agent-sidebar-heading"><Bot :size="23" :stroke-width="1.6" aria-hidden="true" /><h1>项目 Agent</h1></div>
      <Button class="agent-new-conversation" :loading="creating" :disabled="loading || creating" @click="create">
        <template #icon><Plus :size="18" /></template>新建会话
      </Button>
      <Alert v-if="createError" tone="danger" title="新建会话失败">{{ createError }}<Button v-if="ownProjectTenant" variant="ghost" size="sm" @click="openConfiguration">配置默认模型</Button><p v-else>请项目所属租户检查其 Agent 默认模型。</p></Alert>
      <div class="agent-history-heading"><History :size="15" aria-hidden="true" /><h2>会话历史</h2></div>
      <nav class="agent-history" aria-label="会话历史">
        <p v-if="loading && !conversations.length" role="status">正在读取会话…</p>
        <p v-else-if="!conversations.length">还没有会话</p>
        <div v-for="conversation in orderedConversations" :key="conversation.id" class="agent-history-row">
          <button class="agent-history-select" type="button" :disabled="Boolean(deleting)"
            :aria-current="conversation.id === conversationId ? 'true' : undefined" @click="selectConversation(conversation.id)">
            <span class="agent-history-title"><MessageSquare :size="16" aria-hidden="true" /><strong>{{ conversationLabel(conversation.id) }}</strong></span>
            <time :datetime="conversation.created_at" :title="dateTime(conversation.created_at)">{{ formatRelativeTime(conversation.created_at) }}</time>
          </button>
          <DropdownMenu :items="[{ value: 'delete', label: '删除会话', tone: 'danger' }]" @select="requestDelete(conversation.id)">
            <template #trigger><Button variant="ghost" class="agent-history-menu" :aria-label="`${conversationLabel(conversation.id)}菜单`" :disabled="Boolean(deleting)"><Ellipsis :size="17" /></Button></template>
          </DropdownMenu>
        </div>
      </nav>
      <div class="agent-model-setting">
        <span>{{ ownProjectTenant ? '默认模型' : '我的默认模型' }}</span>
        <p v-if="!ownProjectTenant">当前项目使用所属租户的默认模型。</p>
        <Button variant="ghost" class="agent-model-trigger" aria-label="配置项目 Agent 默认模型" @click="openConfiguration">
          <span>{{ configuration?.model_display_name || configuration?.model_name || '尚未配置' }}</span><Settings2 :size="16" aria-hidden="true" />
        </Button>
        <p v-if="configurationError" role="alert">{{ configurationError }}</p>
        <p v-else-if="configuration && !configuration.available" role="status">{{ modelUnavailable(configuration.unavailable_reason) }}</p>
      </div>
    </aside>

    <div class="agent-conversation">
      <header class="agent-toolbar">
        <div class="agent-conversation-heading">
          <h2>{{ currentConversationLabel }}</h2>
          <span v-if="currentConversation" class="agent-current-model">{{ currentConversation.model_display_name || currentConversation.model_name || '模型未配置' }}</span>
        </div>
        <Button v-if="hasDeliverables" variant="ghost" :aria-expanded="showWork" @click="showWork = !showWork">
          <template #icon><PanelRight :size="18" /></template>工作与成果
        </Button>
      </header>
      <div class="agent-notices" aria-live="polite">
        <Alert v-if="listError" tone="danger" title="会话加载失败">{{ listError }} <Button variant="ghost" size="sm" @click="loadConversations">重新加载</Button></Alert>
        <Alert v-if="syncError" tone="warning">{{ syncError }} <Button variant="ghost" size="sm" :disabled="loadingMessages" @click="reconnect">重新连接</Button></Alert>
        <Alert v-if="workError" tone="warning">{{ workError }} <Button variant="ghost" size="sm" :disabled="loadingWork" @click="reloadWork">重新读取</Button></Alert>
        <Alert v-if="currentConversation?.available === false" tone="warning" title="此会话的模型不可用">
          {{ currentConversation.model_id ? modelUnavailable(currentConversation.unavailable_reason) : '此历史会话未绑定模型，修改默认模型不影响历史会话；可以新建会话继续。' }} 历史消息仍可阅读。
          <router-link v-if="ownProjectTenant" to="/console/workers#models">检查模型与凭据</router-link>
          <span v-else>请项目所属租户检查模型与凭据。</span>
        </Alert>
      </div>

      <div class="agent-reading-area">
        <div ref="messageList" class="agent-messages" aria-label="会话消息" :aria-busy="loadingMessages" tabindex="0" @scroll="trackScroll">
          <p v-if="loadingMessages && !messages.length" class="agent-loading" role="status">正在读取历史消息…</p>
          <div v-else-if="!messages.length && !loading && !syncError" class="agent-empty">
            <div class="agent-empty-mark"><Bot :size="32" :stroke-width="1.4" aria-hidden="true" /></div>
            <h2>{{ conversationId ? '一起看看这个项目' : '开始项目对话' }}</h2>
            <p>{{ conversationId ? '读取研究记录，梳理已有发现，继续你的工作。' : '使用独立模型会话，不需要选择研究 Worker。' }}</p>
            <div v-if="conversationId" class="agent-suggestions">
              <button type="button" @click="fillSuggestion('请查看当前项目的研究执行与会话索引，说明来源、状态、更新时间和不可读范围。只读取，不修改研究状态。')"><BookOpen :size="19" /><span>梳理研究进展</span><ArrowUpRight :size="16" /></button>
              <button type="button" @click="fillSuggestion('请列出当前项目已有的漏洞，区分已确认发现和未决线索，不启动复测或新任务。')"><ScanLine :size="19" /><span>查看已有发现</span><ArrowUpRight :size="16" /></button>
            </div>
            <Button v-else variant="secondary" :loading="creating" @click="create">新建会话</Button>
          </div>
          <div class="agent-reading-column">
            <article v-for="message in messages" :id="`agent-turn-${message.id}`" :key="message.id" class="agent-turn" :aria-label="`第 ${message.id} 轮对话`">
              <div class="agent-user-text" aria-label="你的消息">{{ message.text }}</div>
              <ul v-if="message.attachments.length" class="agent-message-attachments" aria-label="消息附件">
                <li v-for="attachment in message.attachments" :key="attachment.id"><Paperclip :size="15" aria-hidden="true" /><a v-if="agentDownloadUrl(attachment.download_url)" :href="agentDownloadUrl(attachment.download_url)" download>{{ attachment.filename }}</a><span v-else>{{ attachment.filename }}（下载不可用）</span><span>{{ attachmentSize(attachment.size) }}</span></li>
              </ul>
              <div class="agent-answer-region" aria-label="Agent 回复">
                <div class="agent-author"><Bot :size="18" aria-hidden="true" /><span>项目 Agent</span></div>
                <div v-if="message.status !== 'succeeded' || message.error_code === 'execution_unconfirmed'" class="agent-answer-heading" role="status">
                  <Badge v-if="message.error_code === 'execution_unconfirmed'" tone="warning" size="sm">执行状态待确认</Badge>
                  <Badge v-else :tone="statusMeta[message.status].tone" size="sm">{{ statusMeta[message.status].label }}</Badge>
                </div>
                <Alert v-if="message.error_code === 'execution_unconfirmed'" tone="warning">暂时无法确认执行是否停止。本轮仍按运行中处理，后续消息可能继续等待；状态将在此处更新。</Alert>
                <Alert v-if="message.error_code && modelConfigurationErrors.has(message.error_code)" tone="warning" :title="message.status === 'queued' ? '等待模型配置修复' : '模型不可用'">
                  {{ message.error_code === 'model_unconfigured' ? '本会话未绑定模型。修改默认模型仅影响新会话，历史记录保持不变。' : modelUnavailable(message.error_code) }} 本轮尚未得到有效回复。
                </Alert>
                <AgentMarkdown v-if="message.answer !== null" class="agent-answer" :content="message.answer" />
                <p v-else-if="message.status === 'succeeded'" class="agent-message-note">本轮未返回公开回复。</p>
                <p v-else-if="message.status === 'failed'" class="agent-message-note">本轮未完成，请展开失败原因查看诊断。</p>
                <Button v-if="message.actions.length || message.artifacts.length" class="agent-turn-deliverables" variant="secondary" size="sm" @click="revealWork(message.id)">
                  <template #icon><PanelRight :size="16" /></template>查看本轮工作与成果<ArrowUpRight :size="14" />
                </Button>
                <footer class="agent-turn-footer">
                  <Button v-if="message.answer !== null" variant="ghost" size="sm" :aria-label="`复制第 ${message.id} 轮回复`" title="复制回复" @click="copyAnswer(message)">
                    <template #icon><Check v-if="copyFeedback?.id === message.id && copyFeedback.success" :size="15" /><Copy v-else :size="15" /></template>
                  </Button>
                  <time :datetime="message.created_at" :title="dateTime(message.created_at)">{{ formatRelativeTime(message.created_at) }}</time>
                  <details v-if="message.error_code || message.task_run_id" class="agent-error-detail">
                    <summary>{{ message.error_code && message.error_code !== 'execution_unconfirmed' ? '失败原因' : '执行诊断' }}</summary>
                    <dl>
                      <template v-if="message.error_code"><dt>错误码</dt><dd><code>{{ message.error_code }}</code></dd></template>
                      <template v-if="message.task_run_id"><dt>TaskRun</dt><dd><code>{{ message.task_run_id }}</code></dd></template>
                    </dl>
                  </details>
                  <span v-if="copyFeedback?.id === message.id" role="status">{{ copyFeedback.text }}</span>
                  <Button v-if="copyFeedback?.id === message.id && !copyFeedback.success" variant="ghost" size="sm" @click="selectAnswerSource(message)">选择回复原文</Button>
                </footer>
              </div>
            </article>
          </div>
        </div>
        <Button v-if="!nearBottom" class="agent-latest" variant="secondary" size="sm" @click="scrollToLatest"><template #icon><ArrowDown :size="15" /></template>回到最新</Button>
      </div>

      <form v-if="conversationId" class="agent-composer" @submit.prevent="send">
        <Alert v-if="draft.error" tone="warning" :title="draft.rejected ? '消息未受理' : '消息受理结果未确认'">
          {{ draft.error }}
          <p v-if="!draft.rejected">重试会保留同一输入标识，不会另建相同轮次。请勿重新输入相同消息。</p>
          <Button v-else variant="ghost" size="sm" @click="editRejectedInput">修改后再发送</Button>
        </Alert>
        <Alert v-if="attachmentError" tone="warning">{{ attachmentError }} <Button variant="ghost" size="sm" :disabled="loadingAttachments" @click="loadAttachments">重新读取附件</Button></Alert>
        <div class="agent-input-surface">
          <ul v-if="draft.attachments.length" class="agent-draft-attachments" aria-label="待发送附件">
            <li v-for="item in draft.attachments" :key="item.key">
              <Paperclip :size="16" aria-hidden="true" /><span class="agent-attachment-name">{{ item.attachment?.filename || item.file?.name }}</span>
              <span role="status">{{ item.status === 'uploading' ? '上传中…' : item.status === 'removing' ? '移除中…' : item.status === 'failed' ? '上传失败' : '已上传' }}</span>
              <Button v-if="item.status === 'failed'" variant="ghost" size="sm" :disabled="attachmentsLocked" @click="uploadAttachment(item)">重试上传</Button>
              <Button variant="ghost" size="sm" :aria-label="`移除附件 ${item.attachment?.filename || item.file?.name}`" :disabled="attachmentsLocked || ['uploading', 'removing'].includes(item.status)" @click="removeAttachment(item)"><X :size="16" /></Button>
              <p v-if="item.error" role="alert">{{ item.error }}</p>
            </li>
          </ul>
          <textarea ref="composerInput" v-model="draft.text" aria-label="消息" rows="2" :readonly="Boolean(draft.pending)"
            placeholder="询问项目进展，或说明你想完成的工作…" @keydown="handleComposerKeydown" />
          <div class="agent-composer-actions">
            <input ref="attachmentInput" class="agent-file-input" type="file" multiple aria-label="选择消息附件" :disabled="attachmentsLocked || !attachmentLimits" @change="chooseAttachments" />
            <Button variant="ghost" :disabled="attachmentsLocked || !attachmentLimits" @click="attachmentInput?.click()"><template #icon><Paperclip :size="18" /></template>附件</Button>
            <span v-if="loadingAttachments" role="status">正在读取附件…</span>
            <span v-else class="agent-shortcut">Ctrl / ⌘ + Enter 发送</span>
            <Button type="submit" :loading="draft.sending" :disabled="!canSend"><template #icon><ArrowUp :size="18" /></template>{{ draft.pending && !draft.sending ? '重试同一消息' : hasActiveMessages ? '发送并排队' : '发送' }}</Button>
          </div>
        </div>
        <details v-if="attachmentLimits" class="agent-attachment-limits"><summary>附件限制</summary><p>单文件最多 {{ attachmentSize(attachmentLimits.max_file_bytes) }}，会话最多 {{ attachmentLimits.max_count }} 个文件、合计 {{ attachmentSize(attachmentLimits.max_total_bytes) }}。UTF-8 文本可读取；其他文件交由工具处理，上传成功不等于已解析。</p></details>
        <p class="agent-cost-note">发送使用模型；复测或报告可能产生额外费用。</p>
      </form>
    </div>

    <aside v-if="hasDeliverables && showWork" class="agent-deliverables" aria-label="工作与成果">
      <header><h2>工作与成果</h2><Button variant="ghost" :loading="loadingWork" aria-label="刷新工作" title="刷新工作" @click="reloadWork"><template #icon><RefreshCw :size="16" /></template></Button></header>
      <div class="agent-deliverables-scroll">
        <section v-for="message in messagesWithDeliverables" :id="`agent-work-${message.id}`" :key="message.id" class="agent-deliverable-group">
          <button class="agent-work-source" type="button" @click="revealTurn(message.id)"><MessageSquare :size="14" /><span>{{ message.text }}</span><ArrowUpRight :size="14" /></button>
          <ul v-if="message.actions.length" class="agent-work" aria-label="本轮发起的工作">
            <li v-for="action in message.actions" :key="action.id" class="agent-work-item">
              <div class="agent-work-heading"><component :is="action.kind === 'verification' ? ScanLine : FileText" :size="19" aria-hidden="true" /><strong>{{ action.title }}</strong></div>
              <Badge :tone="workStatus(workById.get(action.id)).tone" size="sm">{{ workStatus(workById.get(action.id)).label }}</Badge>
              <template v-if="workById.has(action.id)">
                <p v-if="workSummary(workById.get(action.id)!)" class="agent-work-result">{{ workSummary(workById.get(action.id)!) }}</p>
                <p v-if="workById.get(action.id)!.next_attempt_at" class="agent-message-note">下次尝试时间：<time :datetime="workById.get(action.id)!.next_attempt_at!">{{ dateTime(workById.get(action.id)!.next_attempt_at!) }}</time></p>
                <p v-if="workById.get(action.id)!.error" class="agent-work-error" role="status">{{ workById.get(action.id)!.error }}</p>
                <p v-if="hasPreviousReport(workById.get(action.id)!)" class="agent-message-note">上次报告不是本轮成果。</p>
                <div class="agent-work-actions">
                  <router-link v-if="workRoute(workById.get(action.id)!.url)" :to="workRoute(workById.get(action.id)!.url)!">{{ actionLinks[action.kind] }}<ArrowUpRight :size="14" aria-hidden="true" /></router-link>
                  <a v-if="workDownload(workById.get(action.id)!)" :href="workDownload(workById.get(action.id)!)" download><Download :size="15" aria-hidden="true" />下载成果</a>
                </div>
              </template>
            </li>
          </ul>
          <ul v-if="message.artifacts.length" class="agent-artifacts" aria-label="可下载成果">
            <li v-for="artifact in message.artifacts" :key="artifact.id">
              <a v-if="agentDownloadUrl(artifact.download_url)" :href="agentDownloadUrl(artifact.download_url)" download><Download :size="17" aria-hidden="true" />{{ artifact.title }}</a>
              <span v-else>{{ artifact.title }}（下载地址不可用）</span>
            </li>
          </ul>
        </section>
      </div>
    </aside>

    <Dialog :open="Boolean(deleteTarget)" title="删除会话" description="从聊天历史移除此会话，不能再继续发送。这不是安全擦除；不删除已生成报告／复测等业务成果，也不取消已发起的业务工作。" size="sm" content-class="workbench-surface" :dismissible="!deleting" @update:open="value => { if (!value && !deleting) deleteTarget = '' }">
      <p>确认删除{{ deleteTarget ? conversationLabel(deleteTarget) : '会话' }}？</p>
      <p v-if="deleteTarget && conversationHasDraft(deleteTarget)">此会话有未发送的输入或附件，删除后将清除本页草稿。</p>
      <Alert v-if="deleteError" tone="danger" title="会话未删除">{{ deleteError }}</Alert>
      <template #footer><Button variant="secondary" :disabled="Boolean(deleting)" @click="deleteTarget = ''">取消</Button><Button variant="danger" :loading="Boolean(deleting)" :disabled="Boolean(deleting)" @click="confirmDelete">确认删除</Button></template>
    </Dialog>
    <Dialog v-model:open="answerSourceOpen" title="回复原文" description="选择原文后使用浏览器复制操作。" size="lg" content-class="workbench-surface">
      <textarea ref="answerSourceInput" class="agent-answer-source" :value="answerSource" readonly aria-label="回复原文" @focus="answerSourceInput?.select()" />
    </Dialog>
    <Dialog v-model:open="configurationOpen" title="项目 Agent 默认模型" description="仅影响本人租户的新会话。已有会话继续使用原模型，不改变正在执行的轮次。" size="md" content-class="workbench-surface" :dismissible="!savingConfiguration">
      <form id="agent-model-form" class="agent-configuration-form" @submit.prevent="saveConfiguration">
        <FormField label="租户模型"><template #default="{ controlAttrs }"><Select v-bind="controlAttrs" v-model="modelId" :options="modelOptions" :disabled="loadingConfiguration || savingConfiguration" placeholder="选择已配置的模型" /></template></FormField>
        <p v-if="loadingConfiguration" role="status">正在读取模型配置…</p>
        <p v-else-if="!modelOptions.length">尚无模型。请先在模型管理中配置模型与凭据。</p>
        <Alert v-if="configurationError" tone="danger" title="模型配置未更新">{{ configurationError }}</Alert>
        <p v-if="configuration?.unavailable_reason" class="agent-message-note">{{ modelUnavailable(configuration.unavailable_reason) }}</p>
        <router-link to="/console/workers#models" @click="configurationOpen = false">管理模型与凭据<ArrowUpRight :size="14" /></router-link>
      </form>
      <template #footer><Button variant="secondary" :disabled="savingConfiguration" @click="configurationOpen = false">取消</Button><Button type="submit" form="agent-model-form" :loading="savingConfiguration" :disabled="!modelId || loadingConfiguration || savingConfiguration">保存默认模型</Button></template>
    </Dialog>
  </section>
</template>

<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { onBeforeRouteLeave, onBeforeRouteUpdate, useRoute, useRouter } from 'vue-router'
import { ArrowDown, ArrowUp, ArrowUpRight, BookOpen, Bot, Check, Copy, Download, Ellipsis, FileText, History, MessageSquare, PanelRight, Paperclip, Plus, RefreshCw, ScanLine, Settings2, X } from '@lucide/vue'
import { Alert, Badge, Button, Dialog, DropdownMenu, FormField, Select } from '@/components/ui'
import AgentMarkdown from './AgentMarkdown.vue'
import type { BadgeTone } from '@/components/ui/Badge.vue'
import { agentDownloadUrl, getAgentConfiguration, saveAgentConfiguration, type AgentConfiguration, type AgentMessage, type AgentWork } from '@/api/projectAgent'
import { listTenantModels } from '@/api/client'
import { useAuthStore } from '@/stores/auth'
import { useWorkspaceStore } from '@/stores/workspace'
import type { TenantModel } from '@/api/types'
import { dateTime, formatRelativeTime } from '@/lib/format'
import { copyText } from '@/lib/copyText'
import { useProjectAgent } from './useProjectAgent'

const route = useRoute()
const router = useRouter()
const auth = useAuthStore()
const workspace = useWorkspaceStore()
const ownProjectTenant = computed(() => workspace.projectDetail?.project.tenant_id === auth.session?.tenant_id)
const projectId = computed(() => String(route.params.id || ''))
const conversationId = computed(() => typeof route.query.conversation === 'string' ? route.query.conversation : '')
const {
  conversations, messages, work, workError, loadingWork, reloadWork,
  loading, loadingMessages, creating, listError, createError, syncError,
  draft, hasUnsavedInput, loadConversations, createConversation, reconnect, send, editRejectedInput,
  deleting, deleteError, removeConversation, conversationHasDraft,
  canSend, attachmentsLocked, attachmentLimits, attachmentError, loadingAttachments,
  loadAttachments, addAttachments, uploadAttachment, removeAttachment,
} = useProjectAgent(projectId, conversationId)
const messageList = ref<HTMLElement>()
const composerInput = ref<HTMLTextAreaElement>()
const attachmentInput = ref<HTMLInputElement>()
const deleteTarget = ref('')
const answerSourceOpen = ref(false)
const answerSource = ref('')
const answerSourceInput = ref<HTMLTextAreaElement>()
let copyTimer: ReturnType<typeof setTimeout> | undefined
let copyOperation = 0
function attachmentSize(size: number) {
  return size >= 1024 * 1024 ? `${Number((size / 1024 / 1024).toFixed(1))} MiB` : size >= 1024 ? `${Number((size / 1024).toFixed(1))} KiB` : `${size} B`
}
function chooseAttachments(event: Event) {
  const input = event.target as HTMLInputElement
  const files = Array.from(input.files || [])
  input.value = ''
  void addAttachments(files)
}
function requestDelete(id: string) {
  deleteError.value = ''
  deleteTarget.value = id
}
async function confirmDelete() {
  const id = deleteTarget.value
  if (!id || !await removeConversation(id)) return
  deleteTarget.value = ''
  if (conversationId.value === id) {
    const latest = [...conversations.value].sort((a, b) => b.created_at.localeCompare(a.created_at))[0]
    await router.replace({ query: { ...route.query, conversation: latest?.id } })
  }
  await nextTick()
  const next = document.querySelector<HTMLElement>('.agent-history-select[aria-current]')
    || document.querySelector<HTMLElement>('.agent-new-conversation')
  next?.focus()
}
async function selectAnswerSource(message: AgentMessage) {
  answerSource.value = message.answer || ''
  answerSourceOpen.value = true
  await nextTick()
  answerSourceInput.value?.focus()
  answerSourceInput.value?.select()
}
const nearBottom = ref(true)
const showWork = ref(true)
const copyFeedback = ref<{ id: number; text: string; success: boolean }>()
const orderedConversations = computed(() => [...conversations.value].reverse())
const currentConversation = computed(() => conversations.value.find(row => row.id === conversationId.value))
const currentConversationLabel = computed(() => conversationId.value ? conversationLabel(conversationId.value) : '新对话')
function conversationLabel(id: string) {
  const index = conversations.value.findIndex(conversation => conversation.id === id)
  return index >= 0 ? `会话 ${index + 1}` : '会话'
}
const hasActiveMessages = computed(() => messages.value.some(message => ['queued', 'running'].includes(message.status)))
const statusMeta: Record<AgentMessage['status'], { label: string; tone: BadgeTone }> = {
  queued: { label: '排队中', tone: 'neutral' }, running: { label: '处理中', tone: 'info' },
  succeeded: { label: '已完成', tone: 'success' }, failed: { label: '失败', tone: 'danger' },
  cancelled: { label: '已取消', tone: 'warning' },
}
const configuration = ref<AgentConfiguration>()
const configurationOpen = ref(false)
const configurationError = ref('')
const loadingConfiguration = ref(false)
const savingConfiguration = ref(false)
const models = ref<TenantModel[]>([])
const modelId = ref('')
const modelOptions = computed(() => models.value.map(model => ({
  value: model.id,
  label: `${model.display_name} · ${model.model_name}${!model.enabled ? ' · 已停用' : !model.credential_configured ? ' · 缺少凭据' : ''}`,
  disabled: !model.enabled || !model.credential_configured,
})))
const modelConfigurationErrors = new Set(['model_unconfigured', 'model_not_found', 'model_disabled', 'model_incomplete', 'model_protocol_unsupported', 'credential_missing', 'model_endpoint_invalid'])
function modelUnavailable(reason?: string | null) {
  const labels: Record<string, string> = {
    model_unconfigured: '尚未配置项目 Agent 默认模型。', model_not_found: '所引用的模型已不存在。',
    model_disabled: '模型已停用，请在模型管理中启用。', model_incomplete: '模型配置不完整，请检查模型名称与连接设置。',
    model_protocol_unsupported: '当前模型协议不受支持。', credential_missing: '模型缺少可用凭据，请在模型管理中配置。',
    model_endpoint_invalid: '模型地址无效，请检查连接设置。',
  }
  return reason ? labels[reason] || `模型当前不可用：${reason}` : '模型当前不可用，请检查配置。'
}
let configurationVisit = 0
async function loadConfiguration(includeModels = false) {
  const visit = ++configurationVisit
  loadingConfiguration.value = true
  configurationError.value = ''
  try {
    const [result, availableModels] = await Promise.all([getAgentConfiguration(), includeModels ? listTenantModels() : Promise.resolve(undefined)])
    if (visit !== configurationVisit) return
    configuration.value = result
    modelId.value = result.model_id || ''
    if (availableModels) models.value = availableModels
  } catch (error) {
    if (visit === configurationVisit) configurationError.value = error instanceof Error ? error.message : '无法读取模型配置。'
  } finally {
    if (visit === configurationVisit) loadingConfiguration.value = false
  }
}
function openConfiguration() {
  configurationOpen.value = true
  void loadConfiguration(true)
}
async function saveConfiguration() {
  if (!modelId.value || savingConfiguration.value) return
  savingConfiguration.value = true
  configurationError.value = ''
  try {
    configuration.value = await saveAgentConfiguration(modelId.value)
    configurationOpen.value = false
    createError.value = ''
  } catch (error) {
    configurationError.value = error instanceof Error ? error.message : '无法保存默认模型。'
  } finally {
    savingConfiguration.value = false
  }
}
onMounted(() => void loadConfiguration())
onBeforeUnmount(() => { configurationVisit += 1 })

const workById = computed(() => new Map(work.value.map(item => [item.id, item])))
const messagesWithDeliverables = computed(() => messages.value.filter(message => message.actions.length || message.artifacts.length))
const hasDeliverables = computed(() => messagesWithDeliverables.value.length > 0)
const actionLinks: Record<AgentWork['kind'], string> = { verification: '查看复测', report: '查看报告', project: '查看项目', intent: '查看研究任务' }
const workStatuses: Record<string, { label: string; tone: BadgeTone }> = {
  queued: { label: '排队中', tone: 'neutral' }, running: { label: '执行中', tone: 'info' },
  cancelling: { label: '正在取消', tone: 'warning' }, completed: { label: '已完成', tone: 'success' },
  succeeded: { label: '已完成', tone: 'success' }, failed: { label: '失败', tone: 'danger' },
  cancelled: { label: '已取消', tone: 'warning' }, superseded: { label: '已有更新一轮报告', tone: 'neutral' },
  unavailable: { label: '不可读取', tone: 'warning' },
}
function workSummary(item: AgentWork) {
  return item.result && 'summary' in item.result ? item.result.summary : ''
}
function workStatus(item?: AgentWork): { label: string; tone: BadgeTone } {
  if (item?.status === 'succeeded' && ['project', 'intent'].includes(item.kind)) {
    return { label: item.kind === 'project' ? '已创建' : '已追加', tone: 'success' }
  }
  if (!item) return { label: loadingWork.value ? '读取中' : '状态未读取', tone: 'neutral' }
  if (item.status === 'queued' && item.next_attempt_at) return { label: '等待重试', tone: 'warning' }
  return workStatuses[item.status] || { label: '状态待确认', tone: 'warning' }
}
function hasPreviousReport(item: AgentWork) {
  return item.kind === 'report' && Boolean(item.accepted_generation_token)
    && item.accepted_generation_token !== item.generation_token
}
function workDownload(item: AgentWork) {
  return !hasPreviousReport(item) && item.download_url ? agentDownloadUrl(item.download_url) : undefined
}
function workRoute(value: string) {
  const sameOrigin = agentDownloadUrl(value)
  if (!value || !sameOrigin) return undefined
  const url = new URL(sameOrigin)
  return url.pathname + url.search + url.hash
}
async function revealWork(messageId: number) {
  showWork.value = true
  await nextTick()
  document.getElementById(`agent-work-${messageId}`)?.scrollIntoView({ block: 'nearest' })
}
function revealTurn(messageId: number) {
  nearBottom.value = false
  document.getElementById(`agent-turn-${messageId}`)?.scrollIntoView({ block: 'start' })
}
function selectConversation(id: string) {
  return router.replace({ query: { ...route.query, conversation: id } })
}
function fillSuggestion(text: string) {
  if (draft.value.pending || draft.value.text) return
  draft.value.text = text
  composerInput.value?.focus()
}
function handleComposerKeydown(event: KeyboardEvent) {
  if (event.key !== 'Enter' || !(event.ctrlKey || event.metaKey) || event.isComposing || event.keyCode === 229) return
  event.preventDefault()
  void send()
}
watch(() => draft.value.text, async () => {
  await nextTick()
  const input = composerInput.value
  if (!input) return
  input.style.height = 'auto'
  input.style.height = `${input.scrollHeight}px`
}, { flush: 'post' })
async function copyAnswer(message: AgentMessage) {
  if (message.answer === null) return
  const operation = ++copyOperation
  clearTimeout(copyTimer)
  try {
    await copyText(message.answer)
    if (operation !== copyOperation) return
    copyFeedback.value = { id: message.id, text: '已复制原文', success: true }
    copyTimer = setTimeout(() => { copyFeedback.value = undefined }, 2500)
  } catch {
    if (operation === copyOperation) copyFeedback.value = { id: message.id, text: '浏览器未完成复制。请检查剪贴板权限，或选择回复原文手动复制。', success: false }
  }
}
watch(conversations, rows => {
  if (!conversationId.value && rows[0]) void selectConversation(rows.at(-1)!.id)
})
async function create() {
  const conversation = await createConversation()
  if (!conversation) return
  await selectConversation(conversation.id)
  await nextTick()
  composerInput.value?.focus()
}
function trackScroll() {
  const list = messageList.value
  if (list) nearBottom.value = list.scrollHeight - list.scrollTop - list.clientHeight < 64
}
function scrollToLatest() {
  const list = messageList.value
  if (list) list.scrollTop = list.scrollHeight
  nearBottom.value = true
}
watch(conversationId, () => {
  nearBottom.value = true
  copyOperation += 1
  clearTimeout(copyTimer)
  copyFeedback.value = undefined
  answerSourceOpen.value = false
})
onBeforeUnmount(() => { copyOperation += 1; clearTimeout(copyTimer) })
watch([messages, work], async () => {
  await nextTick()
  if (nearBottom.value) scrollToLatest()
})
let messageResizeObserver: ResizeObserver | undefined
onMounted(() => {
  messageResizeObserver = new ResizeObserver(() => {
    if (nearBottom.value) scrollToLatest()
    else trackScroll()
  })
  if (messageList.value) messageResizeObserver.observe(messageList.value)
})
onBeforeUnmount(() => messageResizeObserver?.disconnect())
function allowLeave() {
  return !creating.value && !savingConfiguration.value && !deleting.value && (!hasUnsavedInput.value || window.confirm('还有未发送或受理未确认的消息，离开将丢失本页输入。已受理消息仍会保留；请勿重复发送未确认的输入。确认离开？'))
}
onBeforeRouteLeave(allowLeave)
onBeforeRouteUpdate(to => to.params.id === route.params.id || allowLeave())
function protectReload(event: BeforeUnloadEvent) {
  if (!hasUnsavedInput.value && !creating.value && !savingConfiguration.value && !deleting.value) return
  event.preventDefault()
  event.returnValue = ''
}
onMounted(() => window.addEventListener('beforeunload', protectReload))
onBeforeUnmount(() => window.removeEventListener('beforeunload', protectReload))
</script>

<style scoped>
.project-agent.project-agent { display: grid; grid-template-columns: var(--workbench-history-width) minmax(0, 1fr); flex: 1 1 0; min-width: 0; min-height: 0; color: var(--color-text); background: var(--color-canvas); font-size: var(--workbench-body-size); }
.project-agent.has-deliverables { grid-template-columns: var(--workbench-history-width) minmax(0, 1fr) var(--workbench-results-width); }
.agent-sidebar { display: flex; flex-direction: column; gap: var(--space-5); min-width: 0; min-height: 0; padding: var(--space-6) var(--space-5) var(--space-5); background: var(--color-sidebar); border-right: 1px solid var(--color-border-subtle); }
.agent-sidebar-heading { display: flex; align-items: center; gap: var(--space-3); }
.agent-sidebar-heading h1 { margin: 0; font-size: var(--text-2xl); font-weight: var(--font-semibold); letter-spacing: var(--tracking-tight); }
.agent-new-conversation { width: 100%; min-height: var(--control-height-xl); justify-content: flex-start; font-size: var(--text-md); }
.agent-history-heading { display: flex; align-items: center; gap: var(--space-2); color: var(--color-text-secondary); margin-top: var(--space-3); }
.agent-history-heading h2 { font-size: var(--text-sm); font-weight: var(--font-medium); margin: 0; }
.agent-history { flex: 1; min-height: 0; overflow-y: auto; margin: calc(-1 * var(--space-3)) calc(-1 * var(--space-2)); padding: 0 var(--space-2); }
.agent-history p { font-size: var(--text-md); color: var(--color-text-secondary); }
.agent-history-select { display: grid; width: 100%; gap: var(--space-2); padding: var(--space-3); margin-bottom: var(--space-1); border: 1px solid transparent; border-radius: var(--radius-lg); background: transparent; text-align: left; color: var(--color-text); cursor: pointer; }
.agent-history-select:hover { background: var(--color-surface-hover); }
.agent-history-select[aria-current] { background: var(--color-surface); border-color: var(--color-border); box-shadow: var(--shadow-xs); }
.agent-history-row { display: flex; align-items: center; gap: var(--space-1); }
.agent-history-select { flex: 1; min-width: 0; }
.agent-history-menu { flex: none; }
.agent-history-title { display: flex; align-items: center; gap: var(--space-2); }
.agent-history strong { font-weight: var(--font-medium); font-size: var(--text-md); }
.agent-history time { padding-left: var(--space-5); color: var(--color-text-secondary); font-size: var(--text-xs); }
.agent-model-setting { border-top: 1px solid var(--color-border); padding-top: var(--space-4); font-size: var(--text-sm); color: var(--color-text-secondary); }
.agent-model-trigger { width: 100%; justify-content: space-between; margin-top: var(--space-1); padding-inline: 0; text-align: left; }
.agent-model-trigger > span { overflow: hidden; text-overflow: ellipsis; }
.agent-model-setting p { margin: var(--space-2) 0 0; line-height: var(--leading-relaxed); color: var(--color-warning); }
.agent-conversation { display: flex; flex-direction: column; min-width: 0; min-height: 0; }
.agent-toolbar { display: flex; align-items: center; justify-content: space-between; gap: var(--space-4); min-height: 80px; padding: var(--space-4) var(--space-7); }
.agent-conversation-heading { display: flex; align-items: baseline; flex-wrap: wrap; gap: var(--space-3); }
.agent-conversation-heading h2 { margin: 0; font-weight: var(--font-medium); font-size: var(--text-xl); }
.agent-current-model { font-size: var(--text-sm); color: var(--color-text-secondary); }
.agent-notices { display: grid; gap: var(--space-2); padding-inline: var(--space-7); }
.agent-notices:empty { display: none; }
.agent-notices a { display: inline-block; margin-left: var(--space-2); }
.agent-reading-area { position: relative; display: flex; flex: 1 1 0; min-height: 0; }
.agent-messages { flex: 1; min-width: 0; overflow-y: auto; overscroll-behavior: contain; scrollbar-gutter: stable both-edges; padding: var(--space-5) var(--space-7) var(--space-8); }
.agent-reading-column, .agent-composer { width: min(100%, var(--workbench-reading-width)); margin-inline: auto; }
.agent-turn + .agent-turn { margin-top: var(--space-8); padding-top: var(--space-5); }
.agent-user-text { width: fit-content; max-width: 86%; margin-left: auto; padding: var(--space-4) var(--space-5); border-radius: var(--workbench-bubble-radius); background: var(--color-surface-inset); white-space: pre-wrap; overflow-wrap: anywhere; line-height: var(--leading-relaxed); }
.agent-answer-region { margin-top: var(--space-6); }
.agent-author { display: flex; align-items: center; gap: var(--space-2); margin-bottom: var(--space-4); font-size: var(--text-sm); color: var(--color-text-secondary); font-weight: var(--font-medium); }
.agent-answer-heading { margin-bottom: var(--space-3); }
.agent-turn-footer { display: flex; align-items: center; flex-wrap: wrap; gap: var(--space-2) var(--space-3); margin-top: var(--space-4); color: var(--color-text-secondary); font-size: var(--text-xs); }
.agent-error-detail { overflow-wrap: anywhere; }
.agent-error-detail summary { cursor: pointer; padding-block: var(--space-2); }
.agent-error-detail[open] { flex-basis: 100%; }
.agent-error-detail dl { display: grid; grid-template-columns: auto 1fr; gap: var(--space-2) var(--space-3); margin: var(--space-2) 0; }
.agent-error-detail dd { margin: 0; }
.agent-latest { position: absolute; bottom: var(--space-3); left: 50%; transform: translateX(-50%); box-shadow: var(--shadow-sm); }
.agent-empty { min-height: 100%; display: flex; flex-direction: column; align-items: center; justify-content: center; gap: var(--space-5); text-align: center; padding-bottom: var(--space-8); }
.agent-empty-mark { display: grid; place-items: center; width: var(--space-10); height: var(--space-10); border-radius: var(--workbench-bubble-radius); background: var(--color-surface-inset); color: var(--color-text-secondary); }
.agent-empty h2 { font-size: var(--workbench-welcome-size); line-height: var(--leading-tight); font-weight: var(--font-medium); letter-spacing: var(--tracking-tight); margin: 0; }
.agent-empty p { margin: 0; color: var(--color-text-secondary); }
.agent-suggestions { display: flex; flex-wrap: wrap; justify-content: center; gap: var(--space-4); margin-top: var(--space-3); }
.agent-suggestions button { display: flex; align-items: center; gap: var(--space-3); min-width: 220px; padding: var(--space-4) var(--space-5); border: 1px solid var(--color-border); border-radius: var(--radius-xl); color: var(--color-text); background: var(--color-surface); cursor: pointer; font: inherit; font-size: var(--text-lg); text-align: left; }
.agent-suggestions button:hover { background: var(--color-surface-hover); border-color: var(--color-border-strong); }
.agent-suggestions button span { flex: 1; }
.agent-loading { text-align: center; color: var(--color-text-secondary); }
.agent-composer { width: min(calc(100% - var(--space-7) * 2), var(--workbench-reading-width)); flex-shrink: 0; padding-block: var(--space-2) var(--space-5); box-sizing: border-box; }
.agent-composer > .ui-alert { margin-bottom: var(--space-3); }
.agent-input-surface { border: 1px solid var(--color-border-strong); border-radius: var(--workbench-input-radius); background: var(--color-surface); box-shadow: var(--shadow-sm); }
.agent-input-surface:focus-within { border-color: var(--color-accent); box-shadow: var(--ring); }
.agent-input-surface textarea { display: block; box-sizing: border-box; width: 100%; min-height: 94px; max-height: min(240px, 25dvh); padding: var(--space-5) var(--space-5) var(--space-2); resize: none; border: none; border-radius: var(--workbench-input-radius); outline: none; background: transparent; color: var(--color-text); font: inherit; line-height: var(--leading-relaxed); }
.agent-input-surface textarea::placeholder { color: var(--color-text-tertiary); }
.agent-composer-actions { display: flex; align-items: center; gap: var(--space-3); justify-content: space-between; padding: var(--space-2) var(--space-4) var(--space-4); }
.agent-composer-actions > .ui-button { min-height: var(--control-height-lg); padding-inline: var(--space-5); }
.agent-shortcut, .agent-cost-note { color: var(--color-text-secondary); font-size: var(--text-xs); }
.agent-file-input { display: none; }
.agent-draft-attachments, .agent-message-attachments { display: grid; gap: var(--space-2); list-style: none; margin: var(--space-3) var(--space-4); padding: 0; font-size: var(--text-sm); }
.agent-draft-attachments { max-height: 180px; overflow-y: auto; }
.agent-draft-attachments li, .agent-message-attachments li { display: flex; align-items: center; flex-wrap: wrap; gap: var(--space-2); }
.agent-draft-attachments li p { flex-basis: 100%; color: var(--color-danger); margin: 0; overflow-wrap: anywhere; }
.agent-attachment-name { min-width: 0; overflow-wrap: anywhere; flex: 1; }
.agent-message-attachments { justify-items: end; }
.agent-message-attachments a { overflow-wrap: anywhere; }
.agent-attachment-limits { margin-top: var(--space-2); font-size: var(--text-sm); color: var(--color-text-secondary); }
.agent-attachment-limits summary { cursor: pointer; width: fit-content; }
.agent-attachment-limits p { margin: var(--space-2) 0; line-height: var(--leading-relaxed); }
.agent-answer-source { width: 100%; min-height: 260px; resize: vertical; font: inherit; color: var(--color-text); background: var(--color-surface); }
.agent-cost-note { margin: var(--space-3) 0 0; text-align: center; line-height: var(--leading-normal); }
.agent-message-note, .agent-configuration-form p { margin: 0; color: var(--color-text-secondary); line-height: var(--leading-relaxed); }
.agent-configuration-form { display: grid; gap: var(--space-5); }
.agent-configuration-form a { display: inline-flex; align-items: center; gap: var(--space-1); }
.agent-turn-deliverables { margin-top: var(--space-4); }
.agent-deliverables { display: flex; flex-direction: column; min-height: 0; min-width: 0; border-left: 1px solid var(--color-border-subtle); background: var(--color-sidebar); font-size: var(--text-md); }
.agent-deliverables > header { display: flex; align-items: center; justify-content: space-between; min-height: 80px; padding: var(--space-4) var(--space-5); }
.agent-deliverables h2 { font-size: var(--text-lg); font-weight: var(--font-medium); margin: 0; }
.agent-deliverables-scroll { overflow-y: auto; min-height: 0; padding: 0 var(--space-5) var(--space-5); }
.agent-deliverable-group + .agent-deliverable-group { border-top: 1px solid var(--color-border); margin-top: var(--space-5); padding-top: var(--space-5); }
.agent-work-source { display: flex; align-items: center; width: 100%; gap: var(--space-2); padding: var(--space-2) 0 var(--space-3); border: 0; color: var(--color-text-secondary); background: transparent; text-align: left; font: inherit; font-size: var(--text-xs); cursor: pointer; }
.agent-work-source span { flex: 1; min-width: 0; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.agent-work-source svg { flex-shrink: 0; }
.agent-work { list-style: none; padding: 0; margin: 0; }
.agent-work-item { display: grid; justify-items: start; gap: var(--space-3); padding: var(--space-4); border: 1px solid var(--color-border); border-radius: var(--radius-xl); background: var(--color-surface); }
.agent-work-item + .agent-work-item { margin-top: var(--space-3); }
.agent-work-heading { display: flex; align-items: center; gap: var(--space-2); }
.agent-work-heading strong { overflow-wrap: anywhere; font-weight: var(--font-medium); }
.agent-work-heading > svg { flex-shrink: 0; color: var(--color-text-secondary); }
.agent-work-result, .agent-work-error { margin: 0; white-space: pre-wrap; overflow-wrap: anywhere; line-height: var(--leading-relaxed); }
.agent-work-error { color: var(--color-danger); }
.agent-work-actions { display: flex; flex-wrap: wrap; gap: var(--space-4); }
.agent-work-actions a { display: inline-flex; align-items: center; gap: var(--space-1); min-height: var(--control-height); }
.agent-work-actions:empty { display: none; }
.agent-work-actions a, .agent-artifacts a { color: var(--color-link); text-underline-offset: 3px; }
.agent-work-actions a:hover, .agent-artifacts a:hover { color: var(--color-link-hover); }
.agent-artifacts { display: grid; gap: var(--space-3); list-style: none; margin: var(--space-3) 0 0; padding: 0; }
.agent-artifacts li { padding: var(--space-4); border: 1px solid var(--color-border); border-radius: var(--radius-xl); background: var(--color-surface); }
.agent-artifacts a { display: inline-flex; align-items: center; gap: var(--space-2); min-height: var(--control-height); overflow-wrap: anywhere; }
.agent-artifacts svg { flex-shrink: 0; }
@container workspace (max-width: 1100px) {
  .project-agent.has-deliverables { grid-template-columns: var(--workbench-history-width) minmax(0, 1fr); }
  .agent-deliverables { grid-column: 2; max-height: 40dvh; border-top: 1px solid var(--color-border); }
  .agent-sidebar { grid-row: 1 / 3; }
}
@container workspace (max-width: 760px) {
  .project-agent.project-agent { grid-template-columns: minmax(0, 1fr); }
  .agent-sidebar { grid-row: auto; flex-direction: row; flex-wrap: wrap; align-items: center; gap: var(--space-3); padding: var(--space-3); border-right: 0; border-bottom: 1px solid var(--color-border); }
  .agent-sidebar-heading h1 { font-size: var(--text-lg); }
  .agent-new-conversation { width: auto; margin-left: auto; }
  .agent-history-heading { display: none; }
  .agent-history { display: flex; flex: 1 1 100%; overflow-x: auto; max-height: 90px; margin: 0; padding: 0; }
  .agent-history-select { min-width: 150px; }
  .agent-model-setting { width: 100%; padding-top: var(--space-2); }
  .agent-model-setting > span { display: none; }
  .agent-toolbar { min-height: 64px; padding: var(--space-3); }
  .agent-notices, .agent-messages { padding-inline: var(--space-3); }
  .agent-composer { width: calc(100% - var(--space-3) * 2); }
  .agent-deliverables { grid-column: 1; }
  .agent-user-text { max-width: 94%; }
  .agent-shortcut { display: none; }
  .agent-composer-actions { justify-content: flex-end; }
  .agent-empty h2 { font-size: var(--text-3xl); }
}
</style>
