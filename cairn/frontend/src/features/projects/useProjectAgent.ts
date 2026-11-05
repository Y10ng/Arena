import { computed, onBeforeUnmount, reactive, ref, watch, type Ref } from 'vue'
import {
  agentEventsUrl, createAgentConversation, deleteAgentConversation, listAgentConversations, listAgentMessages, listAgentWork,
  deleteAgentAttachment, getAgentAttachmentLimits, listAgentAttachments, uploadAgentAttachment,
  getAgentCleanup, listAgentCleanupRequests, stopAgentTurn, type AgentCleanupResult,
  sendAgentMessage, type AgentMessageInput, type AgentAttachment, type AgentAttachmentLimits, type AgentConversation, type AgentMessage, type AgentWork,
} from '@/api/projectAgent'
import { ApiError } from '@/api/client'

interface DraftAttachment {
  key: string
  file?: File
  attachment?: AgentAttachment
  status: 'uploading' | 'ready' | 'failed' | 'removing'
  error: string
}
interface Draft {
  text: string
  attachments: DraftAttachment[]
  pending: AgentMessageInput | null
  sending: boolean
  error: string
  rejected: boolean
}
const errorText = (error: unknown) => error instanceof Error ? error.message : '请求失败，请重试。'
const terminal = (message: AgentMessage) => ['succeeded', 'failed', 'cancelled'].includes(message.status)
function checkedMessages(rows: unknown): AgentMessage[] {
  if (!Array.isArray(rows) || rows.some(row => !row || !Number.isInteger(row.id)
    || typeof row.text !== 'string' || typeof row.client_message_id !== 'string'
    || !(row.answer === null || typeof row.answer === 'string')
    || !['queued', 'running', 'succeeded', 'failed', 'cancelled'].includes(row.status)
    || !Array.isArray(row.actions) || !Array.isArray(row.attachments) || !Array.isArray(row.artifacts))) {
    throw new SyntaxError('消息数据格式无法解析。')
  }
  return rows as AgentMessage[]
}

function messageId() {
  if (typeof globalThis.crypto?.getRandomValues !== 'function') {
    throw new Error('当前浏览器无法生成安全消息标识，消息尚未发送。请检查浏览器是否支持 Web Crypto。')
  }
  // getRandomValues 在局域网 HTTP 可用；保留 UUID v4 合同及同一输入的重试身份。
  const bytes = crypto.getRandomValues(new Uint8Array(16))
  bytes[6] = (bytes[6]! & 0x0f) | 0x40
  bytes[8] = (bytes[8]! & 0x3f) | 0x80
  const hex = Array.from(bytes, byte => byte.toString(16).padStart(2, '0')).join('')
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`
}

export function useProjectAgent(projectId: Ref<string>, conversationId: Ref<string>) {
  const conversations = ref<AgentConversation[]>([])
  const messages = ref<AgentMessage[]>([])
  const work = ref<AgentWork[]>([])
  const workError = ref('')
  const loadingWork = ref(false)
  const loading = ref(false)
  const loadingMessages = ref(false)
  const creating = ref(false)
  const listError = ref('')
  const createError = ref('')
  const syncError = ref('')
  const connected = ref(false)
  const deleting = ref('')
  const deleteError = ref('')
  const unconfirmedDeletes = ref<Record<string, string>>({})
  const cleanupResults = ref<AgentCleanupResult[]>([])
  const cleanupListError = ref('')
  const loadingCleanup = ref('')
  const cleanupErrors = ref<Record<string, string>>({})
  const stopping = ref<number | null>(null)
  const stopError = ref<{ messageId: number; text: string } | null>(null)
  const attachmentLimits = ref<AgentAttachmentLimits>()
  const attachmentError = ref('')
  const loadingAttachments = ref(false)
  const drafts = reactive(new Map<string, Draft>())
  const draftKey = () => conversationId.value
  const draft = computed(() => {
    const key = draftKey()
    if (!drafts.has(key)) drafts.set(key, { text: '', attachments: [], pending: null, sending: false, error: '', rejected: false })
    return drafts.get(key)!
  })
  const hasUnsavedInput = computed(() => [...drafts.values()].some(item => item.text || item.pending || item.sending || item.attachments.length))
  const canSend = computed(() => Boolean(conversationId.value && draft.value.text.trim()) && !draft.value.sending
    && conversations.value.some(row => row.id === conversationId.value && row.available !== false)
    && !deleting.value && !unconfirmedDeletes.value[conversationId.value] && !loadingAttachments.value && !attachmentError.value
    && draft.value.attachments.every(item => item.status === 'ready'))
  const attachmentsLocked = computed(() => Boolean(draft.value.pending || draft.value.sending || deleting.value
    || unconfirmedDeletes.value[conversationId.value] || loadingAttachments.value || attachmentError.value))
  let attachmentController: AbortController | undefined
  let attachmentKey = 0
  let listController: AbortController | undefined
  let cleanupListController: AbortController | undefined
  let syncController: AbortController | undefined
  let stream: EventSource | undefined
  let reconnectTimer: ReturnType<typeof setTimeout> | undefined
  let generation = 0
  let eventCursor = '0'
  let reconnectAttempts = 0
  let disposed = false
  let workController: AbortController | undefined
  let workTimer: ReturnType<typeof setTimeout> | undefined
  let workFailures = 0

  async function refreshWork() {
    clearTimeout(workTimer)
    workController?.abort()
    const conversation = conversationId.value
    if (!conversation || disposed) return
    const visit = generation
    const controller = new AbortController()
    workController = controller
    loadingWork.value = true
    try {
      const rows = await listAgentWork(conversation, controller.signal)
      if (controller.signal.aborted || visit !== generation) return
      work.value = rows
      workError.value = ''
      workFailures = 0
      // 业务完成不改变聊天消息 revision；queued 也包含原报告服务安排的退避，不在页面重算重试条件。
      if (rows.some(row => ['queued', 'running', 'cancelling'].includes(row.status))) {
        workTimer = setTimeout(() => void refreshWork(), 5000)
      }
    } catch (error) {
      if (controller.signal.aborted || visit !== generation) return
      workError.value = `工作状态未更新：${errorText(error)}`
      workFailures += 1
      if (workFailures < 3 && !(error instanceof ApiError && [401, 403, 404].includes(error.status))) {
        workTimer = setTimeout(() => void refreshWork(), 5000)
      }
    } finally {
      if (!controller.signal.aborted && visit === generation) loadingWork.value = false
    }
  }

  function reloadWork() {
    workFailures = 0
    void refreshWork()
  }

  async function loadConversations() {
    listController?.abort()
    const controller = new AbortController()
    listController = controller
    loading.value = true
    listError.value = ''
    try {
      const rows = await listAgentConversations(controller.signal)
      if (!controller.signal.aborted) conversations.value = rows
    } catch (error) {
      if (!controller.signal.aborted) listError.value = errorText(error)
    } finally {
      if (!controller.signal.aborted) loading.value = false
    }
  }

  async function createConversation() {
    if (creating.value) return
    creating.value = true
    createError.value = ''
    try {
      const conversation = await createAgentConversation(projectId.value || null)
      if (disposed) return
      conversations.value = [...conversations.value, conversation]
      return conversation
    } catch (error) {
      if (!disposed) createError.value = errorText(error)
    } finally {
      creating.value = false
    }
  }

  function conversationHasDraft(id: string) {
    const item = drafts.get(id)
    return Boolean(item && (item.text || item.pending || item.attachments.length))
  }

  async function loadCleanupRequests() {
    cleanupListController?.abort()
    const controller = new AbortController()
    cleanupListController = controller
    const snapshot = new Map(cleanupResults.value.map(row => [row.conversation_id, row]))
    cleanupListError.value = ''
    try {
      const rows = await listAgentCleanupRequests(controller.signal)
      if (controller.signal.aborted) return
      const results = new Map(cleanupResults.value.map(row => [row.conversation_id, row]))
      for (const row of rows) {
        // 列表读取期间若已有 DELETE 或单项刷新回执，不让迟到快照覆盖它。
        if (results.get(row.conversation_id) === snapshot.get(row.conversation_id)) results.set(row.conversation_id, row)
      }
      cleanupResults.value = [...results.values()]
    } catch (error) {
      if (!controller.signal.aborted) cleanupListError.value = `回收请求未读取：${errorText(error)}`
    }
  }

  function acceptDeletion(result: AgentCleanupResult) {
    const id = result.conversation_id
    cleanupResults.value = [...cleanupResults.value.filter(row => row.conversation_id !== id), result]
    delete unconfirmedDeletes.value[id]
    if (conversationId.value === id) {
      generation += 1
      closeConnection()
      workController?.abort()
      attachmentController?.abort()
      clearTimeout(workTimer)
      messages.value = []
      work.value = []
    }
    listController?.abort()
    loading.value = false
    drafts.delete(id)
    conversations.value = conversations.value.filter(row => row.id !== id)
  }

  async function refreshCleanup(id: string) {
    if (loadingCleanup.value || disposed) return false
    loadingCleanup.value = id
    cleanupErrors.value[id] = ''
    try {
      const result = await getAgentCleanup(id)
      if (disposed) return false
      if (unconfirmedDeletes.value[id]) {
        // 没有请求可能是尚未受理，也可能是历史 tombstone，不能据此推断删除结果。
        if (!result.cleanup) {
          cleanupErrors.value[id] = '尚未查到已受理的回收请求，删除结果仍未确认。'
          return false
        }
        acceptDeletion(result)
      } else {
        cleanupResults.value = cleanupResults.value.map(row => row.conversation_id === id ? result : row)
      }
      return true
    } catch (error) {
      if (!disposed) cleanupErrors.value[id] = `回收状态未更新：${errorText(error)}`
      return false
    } finally {
      loadingCleanup.value = ''
    }
  }

  async function stopTurn(message: AgentMessage) {
    if (stopping.value !== null || message.status !== 'running' || message.cancel_requested_at) return
    const conversation = conversationId.value
    const visit = generation
    stopping.value = message.id
    stopError.value = null
    try {
      const result = await stopAgentTurn(conversation, message.id)
      if (!disposed && visit === generation && conversation === conversationId.value) mergeMessages(checkedMessages([result]))
    } catch (error) {
      const latest = messages.value.find(row => row.id === message.id)
      if (!disposed && visit === generation && latest && !latest.cancel_requested_at && !terminal(latest)) {
        stopError.value = { messageId: message.id, text: `第 ${message.id} 轮停止请求未确认：${errorText(error)}` }
      }
    } finally {
      if (visit === generation) stopping.value = null
    }
  }

  async function removeConversation(id: string) {
    if (deleting.value || unconfirmedDeletes.value[id]) return false
    const current = drafts.get(id)
    deleteError.value = ''
    if (current?.sending || current?.pending || current?.attachments.some(item => ['uploading', 'removing'].includes(item.status))) {
      deleteError.value = '消息提交或附件操作尚未确认，请先完成或重试原操作，再删除会话。'
      return false
    }
    deleting.value = id
    try {
      const result = await deleteAgentConversation(id)
      if (disposed) return false
      acceptDeletion(result)
      return true
    } catch (error) {
      if (disposed) return false
      if (error instanceof ApiError && [400, 401, 403, 409, 422].includes(error.status)) {
        deleteError.value = errorText(error)
        return false
      }
      // 响应丢失与普通 404 都不证明未删除；只凭本人已有的权威请求确认受理。
      unconfirmedDeletes.value[id] = errorText(error)
      return await refreshCleanup(id)
    } finally {
      deleting.value = ''
    }
  }

  async function loadAttachments() {
    attachmentController?.abort()
    const conversation = conversationId.value
    if (!conversation) return
    const current = draft.value
    const controller = new AbortController()
    attachmentController = controller
    loadingAttachments.value = true
    attachmentError.value = ''
    try {
      const [rows, limits] = await Promise.all([
        listAgentAttachments(conversation, controller.signal),
        getAgentAttachmentLimits(conversation, controller.signal),
      ])
      if (controller.signal.aborted) return
      attachmentLimits.value = limits
      const known = new Set(current.attachments.map(item => item.attachment?.id))
      for (const attachment of rows) {
        if (attachment.message_id === null && !known.has(attachment.id)
          && !messages.value.some(message => message.attachments.some(item => item.id === attachment.id))) {
          current.attachments.push({ key: attachment.id, attachment, status: 'ready', error: '' })
        }
      }
      const sent = new Set(rows.filter(row => row.message_id !== null).map(row => row.id))
      current.attachments = current.attachments.filter(item => !item.attachment || !sent.has(item.attachment.id))
    } catch (error) {
      if (!controller.signal.aborted) attachmentError.value = `附件未读取：${errorText(error)}`
    } finally {
      if (!controller.signal.aborted) loadingAttachments.value = false
    }
  }

  async function uploadAttachment(item: DraftAttachment, current = draft.value, conversation = conversationId.value) {
    if (!item.file || current.pending || current.sending) return
    item.status = 'uploading'
    item.error = ''
    try {
      item.attachment = await uploadAgentAttachment(conversation, item.file)
      current.attachments = current.attachments.filter(row => row.key === item.key || row.attachment?.id !== item.attachment!.id)
      item.status = 'ready'
      item.file = undefined
    } catch (error) {
      item.status = 'failed'
      item.error = errorText(error)
    }
  }

  async function addAttachments(files: File[]) {
    if (attachmentsLocked.value || !attachmentLimits.value) return
    const current = draft.value
    const conversation = conversationId.value
    // 顺序上传减少突发请求；切换会话后仍只更新原会话草稿。
    const items = files.map(file => ({ key: `upload-${++attachmentKey}`, file, status: 'uploading' as const, error: '' }))
    current.attachments.push(...items)
    for (const item of current.attachments.filter(row => items.some(added => added.key === row.key))) {
      await uploadAttachment(item, current, conversation)
    }
  }

  async function removeAttachment(item: DraftAttachment) {
    if (attachmentsLocked.value || ['uploading', 'removing'].includes(item.status)) return
    const current = draft.value
    item.error = ''
    if (item.attachment) {
      item.status = 'removing'
      try {
        await deleteAgentAttachment(conversationId.value, item.attachment.id)
      } catch (error) {
        item.status = 'ready'
        item.error = errorText(error)
        return
      }
    }
    current.attachments = current.attachments.filter(row => row.key !== item.key)
  }

  function mergeMessages(rows: AgentMessage[]) {
    const merged = new Map(messages.value.map(message => [message.id, message]))
    for (const message of rows) {
      const previous = merged.get(message.id)
      // GET／POST 可能比 SSE 晚返回；不能把已完成的轮次退回排队中。
      if (!previous || (!terminal(previous) && !(previous.status === 'running' && message.status === 'queued'))) {
        merged.set(message.id, previous?.cancel_requested_at && !message.cancel_requested_at
          ? { ...message, cancel_requested_at: previous.cancel_requested_at } : message)
      }
      const currentDraft = drafts.get(draftKey())
      if (currentDraft?.pending?.client_message_id === message.client_message_id && currentDraft.pending.text === message.text
        && currentDraft.pending.project_id === message.project_id
        && [...currentDraft.pending.attachment_ids].sort().join(',') === message.attachments.map(item => item.id).sort().join(',')) {
        currentDraft.pending = null
        currentDraft.attachments = []
        currentDraft.text = ''
        currentDraft.error = ''
        currentDraft.rejected = false
      }
    }
    messages.value = [...merged.values()].sort((left, right) => left.id - right.id)
    const stopped = stopError.value && merged.get(stopError.value.messageId)
    if (stopped && (stopped.cancel_requested_at || terminal(stopped))) stopError.value = null
  }

  function closeConnection() {
    syncController?.abort()
    stream?.close()
    stream = undefined
    clearTimeout(reconnectTimer)
    connected.value = false
  }

  function scheduleReconnect(visit: number, reason = '') {
    if (visit !== generation || disposed) return
    if (reconnectAttempts >= 3) {
      syncError.value = `消息更新暂不可用，已显示状态可能滞后。请重新连接。${reason}`
      return
    }
    const delay = 1000 * 2 ** reconnectAttempts++
    reconnectTimer = setTimeout(() => void synchronize(visit), delay)
  }

  async function synchronize(visit = generation) {
    closeConnection()
    const conversation = conversationId.value
    if (!conversation) return
    const controller = new AbortController()
    syncController = controller
    loadingMessages.value = true
    try {
      // 消息 id 不是状态版本；重连必须补读旧轮次，事件游标单独保存。
      const rows = await listAgentMessages(conversation, controller.signal)
      if (controller.signal.aborted || visit !== generation) return
      mergeMessages(checkedMessages(rows))
      const source = new EventSource(agentEventsUrl(conversation, eventCursor))
      let openedAt: number | undefined
      stream = source
      source.onopen = () => {
        if (visit !== generation || source !== stream) return
        openedAt = performance.now()
        connected.value = true
        syncError.value = ''
      }
      source.addEventListener('messages', (event) => {
        if (visit !== generation || source !== stream) return
        try {
          const messageEvent = event as MessageEvent<string>
          mergeMessages(checkedMessages(JSON.parse(messageEvent.data)))
          if (messageEvent.lastEventId) eventCursor = messageEvent.lastEventId
        } catch {
          source.close()
          stream = undefined
          connected.value = false
          syncError.value = '消息更新无法解析，已显示状态可能滞后。请重新连接。'
        }
      })
      source.addEventListener('close', event => {
        if (visit !== generation || source !== stream) return
        source.close()
        stream = undefined
        connected.value = false
        try {
          const data = JSON.parse((event as MessageEvent<string>).data)
          if (data.reason !== 'rotate') throw new Error('invalid_close')
          // 只有真实稳定连接才恢复失败预算，不能用快速轮换掩盖持续故障。
          if (openedAt !== undefined && performance.now() - openedAt >= 30_000) {
            reconnectAttempts = 0
            reconnectTimer = setTimeout(() => void synchronize(visit), 0)
          } else scheduleReconnect(visit)
        } catch {
          syncError.value = '连接关闭原因无法解析，请重新连接。'
        }
      })
      source.onerror = event => {
        if (visit !== generation || source !== stream) return
        source.close()
        stream = undefined
        connected.value = false
        if (event instanceof MessageEvent) {
          try {
            const data = JSON.parse(event.data)
            if (typeof data.detail !== 'string' || !data.detail) throw new Error('invalid_error')
            syncError.value = `消息更新已停止：${data.detail}`
          } catch {
            syncError.value = '消息更新错误无法解析，请重新连接。'
          }
          return
        }
        if (openedAt !== undefined && performance.now() - openedAt >= 30_000) reconnectAttempts = 0
        scheduleReconnect(visit)
      }
    } catch (error) {
      if (controller.signal.aborted || visit !== generation) return
      if (error instanceof SyntaxError || (error instanceof ApiError && error.status < 500)) {
        syncError.value = `消息补读失败：${errorText(error)}`
      } else scheduleReconnect(visit, errorText(error))
    } finally {
      if (!controller.signal.aborted && visit === generation) loadingMessages.value = false
    }
  }

  function reconnect() {
    reconnectAttempts = 0
    void synchronize()
    reloadWork()
  }

  async function send() {
    const conversation = conversationId.value
    const current = draft.value
    if (!canSend.value) return
    let pending = current.pending
    current.sending = true
    current.error = ''
    current.rejected = false
    try {
      current.pending ??= { client_message_id: messageId(), text: current.text, project_id: projectId.value || null,
        attachment_ids: current.attachments.map(item => item.attachment!.id).sort() }
      pending = current.pending
      const message = await sendAgentMessage(conversation, pending)
      if (conversation === conversationId.value && !disposed) mergeMessages([message])
      if (current.pending === pending) {
        current.text = ''
        current.attachments = []
        current.pending = null
      }
    } catch (error) {
      // SSE 或补读已确认同一输入时，迟到的 HTTP 失败不再误报受理未知。
      if (current.pending === pending) {
        current.error = errorText(error)
        current.rejected = !pending || (error instanceof ApiError && [400, 401, 403, 404, 422].includes(error.status))
      }
    } finally {
      current.sending = false
    }
  }

  function editRejectedInput() {
    if (!draft.value.rejected || draft.value.sending) return
    draft.value.pending = null
    draft.value.error = ''
    draft.value.rejected = false
  }

  watch(() => messages.value.flatMap(message => message.actions.map(action => action.id)).join(','), ids => {
    if (ids) reloadWork()
  })

  void loadConversations()
  void loadCleanupRequests()

  watch(conversationId, () => {
    generation += 1
    closeConnection()
    messages.value = []
    stopping.value = null
    stopError.value = null
    attachmentController?.abort()
    attachmentError.value = ''
    attachmentLimits.value = undefined
    loadingAttachments.value = false
    void loadAttachments()
    workController?.abort()
    clearTimeout(workTimer)
    work.value = []
    workError.value = ''
    workFailures = 0
    loadingWork.value = false
    void refreshWork()
    syncError.value = ''
    loadingMessages.value = false
    eventCursor = '0'
    reconnectAttempts = 0
    void synchronize()
  }, { immediate: true })

  onBeforeUnmount(() => {
    disposed = true
    generation += 1
    listController?.abort()
    cleanupListController?.abort()
    attachmentController?.abort()
    workController?.abort()
    clearTimeout(workTimer)
    closeConnection()
  })

  return {
    conversations, messages, work, workError, loadingWork, reloadWork,
    loading, loadingMessages, creating, listError, createError, syncError,
    connected, draft, hasUnsavedInput, loadConversations, createConversation, reconnect, send, editRejectedInput,
    deleting, deleteError, unconfirmedDeletes, removeConversation, conversationHasDraft,
    cleanupResults, cleanupListError, loadCleanupRequests, loadingCleanup, cleanupErrors, refreshCleanup, stopping, stopError, stopTurn,
    canSend, attachmentsLocked, attachmentLimits, attachmentError, loadingAttachments,
    loadAttachments, addAttachments, uploadAttachment, removeAttachment,
  }
}
