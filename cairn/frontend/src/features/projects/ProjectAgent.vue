<template>
  <section class="project-agent workbench-surface" :class="{ 'has-deliverables': showWork && hasDeliverables }" aria-label="ChatBot">
    <nav id="agent-history-panel" class="agent-history" aria-label="会话历史">
      <header class="agent-history-heading"><h2>会话历史</h2></header>
      <Button class="agent-new-conversation" variant="secondary" :loading="creating" :disabled="loading || creating" @click="create"><template #icon><Plus :size="18" /></template>新建会话</Button>
      <div class="agent-history-list">
        <p v-if="loading && !conversations.length" role="status">正在读取会话…</p>
        <p v-else-if="!conversations.length">还没有会话</p>
        <div v-for="conversation in orderedConversations" :key="conversation.id" class="agent-history-row" :class="{ 'is-current': conversation.id === conversationId }">
          <button class="agent-history-select" type="button" :title="`${conversationLabel(conversation.id)} · ${dateTime(conversation.created_at)}`" :disabled="Boolean(deleting)" :aria-current="conversation.id === conversationId ? 'true' : undefined" @click="selectConversation(conversation.id)">
            <MessageSquare :size="17" aria-hidden="true" /><span>{{ conversationLabel(conversation.id) }}</span>
          </button>
          <DropdownMenu :items="[{ value: 'delete', label: '删除会话', tone: 'danger' }]" @select="requestDelete(conversation.id)">
            <template #trigger><Button variant="ghost" class="agent-history-menu" :aria-label="`${conversationLabel(conversation.id)}菜单`" :disabled="Boolean(deleting)"><Ellipsis :size="18" /></Button></template>
          </DropdownMenu>
        </div>
      </div>
      <Button v-if="pendingCleanupResults.length" class="agent-cleanup-entry" variant="ghost" size="sm" aria-haspopup="dialog" @click="openCleanupDetails">
        <span v-if="cleanupPendingCount">回收中 {{ cleanupPendingCount }}</span>
        <Badge v-if="cleanupFailedCount" tone="warning" size="sm">回收异常 {{ cleanupFailedCount }}</Badge>
        <span v-if="cleanupWithoutRequestCount">无回收请求 {{ cleanupWithoutRequestCount }}</span>
      </Button>
    </nav>
    <div class="agent-conversation">
      <header class="agent-toolbar">
        <div class="agent-conversation-heading">
          <h2>{{ currentConversationLabel }}</h2>
          <span v-if="currentConversation" class="agent-current-model">{{ currentConversation.model_display_name || currentConversation.model_name || '模型未配置' }}</span>
        </div>
        <div class="agent-toolbar-actions">
          <Button variant="ghost" aria-label="配置 ChatBot 默认模型" title="默认模型（仅影响新会话）" @click="openConfiguration"><Settings2 :size="18" /></Button>
          <Button v-if="hasDeliverables" variant="ghost" :aria-expanded="showWork" aria-controls="agent-work-panel" @click="toggleWork"><template #icon><PanelRight :size="18" /></template>工作与成果</Button>
        </div>
      </header>
      <div class="agent-notices" aria-live="polite">
        <Alert v-if="projectsError" tone="warning">项目列表未更新：{{ projectsError }} <Button variant="ghost" size="sm" @click="loadProjects">重新读取</Button></Alert>
        <Alert v-else-if="projectId && !projects.some(project => project.id === projectId) && !loadingProjects" tone="warning">所选项目已不可读取。历史消息不受影响；请选择其他项目或清空上下文后发送。</Alert>
        <Alert v-if="createError" tone="danger" title="新建会话失败">{{ createError }}<Button variant="ghost" size="sm" @click="openConfiguration">配置默认模型</Button></Alert>
        <Alert v-if="configurationError && !configurationOpen" tone="warning">{{ configurationError }}</Alert>
        <Alert v-else-if="configuration && !configuration.available" tone="warning">{{ modelUnavailable(configuration.unavailable_reason) }}<Button variant="ghost" size="sm" @click="openConfiguration">配置默认模型</Button></Alert>
        <Alert v-if="listError" tone="danger" title="会话加载失败">{{ listError }} <Button variant="ghost" size="sm" @click="loadConversations">重新加载</Button></Alert>
        <Alert v-if="syncError" tone="warning">{{ syncError }} <Button variant="ghost" size="sm" :disabled="loadingMessages" @click="reconnect">重新连接</Button></Alert>
        <Alert v-if="workError" tone="warning">{{ workError }} <Button variant="ghost" size="sm" :disabled="loadingWork" @click="reloadWork">重新读取</Button></Alert>
        <Alert v-if="stopError" tone="warning">{{ stopError.text }}</Alert>
        <Alert v-if="cleanupListError" tone="warning">{{ cleanupListError }} <Button variant="ghost" size="sm" @click="loadCleanupRequests">重新读取回收请求</Button></Alert>
        <Alert v-for="(error, id) in unconfirmedDeletes" :key="id" tone="warning" title="删除结果未确认">
          <p>会话 {{ id }}：{{ error }}</p>
          <p>尚未确认是否已受理删除，身份和本页草稿暂时保留；核实前暂停发送。</p>
          <Button variant="ghost" size="sm" :loading="loadingCleanup === id" :disabled="Boolean(loadingCleanup)" @click="verifyDeletion(id)">核实删除结果</Button>
          <p v-if="cleanupErrors[id]">{{ cleanupErrors[id] }}</p>
        </Alert>
        <Alert v-if="currentConversation?.available === false" tone="warning" title="此会话暂不可续聊">
          {{ conversationUnavailable(currentConversation.unavailable_reason) }} 历史消息与已有成品仍可读取。
          <router-link v-if="modelConfigurationErrors.has(currentConversation.unavailable_reason || '')" to="/console/workers#models">检查模型与凭据</router-link>
        </Alert>
      </div>

      <div class="agent-reading-area">
        <div ref="messageList" class="agent-messages" aria-label="会话消息" :aria-busy="loadingMessages" tabindex="0" @scroll="trackScroll">
          <p v-if="loadingMessages && !messages.length" class="agent-loading" role="status">正在读取历史消息…</p>
          <div v-else-if="!messages.length && !loading && !syncError" class="agent-empty">
            <p>{{ conversationId ? '暂无消息' : '暂无会话' }}</p>
            <div v-if="conversationId && projectId" class="agent-suggestions">
              <button type="button" @click="fillSuggestion('请查看当前项目的研究执行与会话索引，说明来源、状态、更新时间和不可读范围。只读取，不修改研究状态。')"><BookOpen :size="19" /><span>梳理研究进展</span><ArrowUpRight :size="16" /></button>
              <button type="button" @click="fillSuggestion('请列出当前项目已有的漏洞，区分已确认发现和未决线索，不启动复测或新任务。')"><ScanLine :size="19" /><span>查看已有发现</span><ArrowUpRight :size="16" /></button>
            </div>
          </div>
          <div class="agent-reading-column">
            <article v-for="message in messages" :id="`agent-turn-${message.id}`" :key="message.id" class="agent-turn" :aria-label="`第 ${message.id} 轮对话`">
              <header class="agent-turn-identity"><span>你</span><span class="agent-message-context">{{ message.project_id ? `项目：${message.project_id}` : '无项目上下文' }}</span><time :datetime="message.created_at" :title="dateTime(message.created_at)">{{ formatRelativeTime(message.created_at) }}</time></header>
              <div class="agent-user-text" aria-label="你的消息">{{ message.text }}</div>
              <ul v-if="message.attachments.length" class="agent-message-attachments" aria-label="消息附件">
                <li v-for="attachment in message.attachments" :key="attachment.id"><Paperclip :size="15" aria-hidden="true" /><a v-if="agentDownloadUrl(attachment.download_url)" :href="agentDownloadUrl(attachment.download_url)" download>{{ attachment.filename }}</a><span v-else>{{ attachment.filename }}（下载不可用）</span><span>{{ attachmentSize(attachment.size) }}</span></li>
              </ul>
              <div class="agent-answer-region" aria-label="ChatBot 回复">
                <div class="agent-author"><Bot :size="18" aria-hidden="true" /><span>ChatBot</span></div>
                <div v-if="message.status !== 'succeeded' || message.error_code === 'execution_unconfirmed'" class="agent-answer-heading" role="status">
                  <Badge v-if="message.error_code === 'execution_unconfirmed'" tone="warning" size="sm">执行状态待确认</Badge>
                  <Badge v-else-if="message.status === 'running' && message.cancel_requested_at" tone="warning" size="sm">停止待确认</Badge>
                  <Badge v-else :tone="statusMeta[message.status].tone" size="sm">{{ statusMeta[message.status].label }}</Badge>
                  <Button v-if="message.status === 'running'" variant="secondary" size="sm" :loading="stopping === message.id" :disabled="stopping !== null || Boolean(message.cancel_requested_at)" @click="stopTurn(message)">停止本轮</Button>
                </div>
                <p v-if="message.status === 'running' && message.cancel_requested_at" class="agent-message-note">已请求停止模型及本轮测试进程，尚未确认退出；已保存材料和独立复测／报告不受影响。</p>
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
                <Alert v-if="message.consumption_error" class="agent-consumption-error" tone="warning">本轮计量读取失败，实际用量与费用未知。重新进入会话可再次读取。</Alert>
                <footer class="agent-turn-footer">
                  <Button v-if="message.answer !== null" variant="ghost" size="sm" :aria-label="`复制第 ${message.id} 轮回复`" title="复制回复" @click="copyAnswer(message)">
                    <template #icon><Check v-if="copyFeedback?.id === message.id && copyFeedback.success" :size="15" /><Copy v-else :size="15" /></template>
                  </Button>
                  <Button v-if="message.answer !== null" variant="ghost" size="sm" @click="selectAnswerSource(message)">回复原文</Button>
                  <details v-if="message.error_code || message.task_run_id" class="agent-error-detail">
                    <summary>{{ message.error_code && message.error_code !== 'execution_unconfirmed' ? '失败原因' : '执行诊断' }}</summary>
                    <dl>
                      <template v-if="message.error_code"><dt>错误码</dt><dd><code>{{ message.error_code }}</code></dd></template>
                      <template v-if="message.task_run_id"><dt>TaskRun</dt><dd><code>{{ message.task_run_id }}</code></dd></template>
                    </dl>
                  </details>
                  <details class="agent-error-detail agent-usage-detail">
                    <summary>{{ message.consumption_error ? '本轮用量（读取失败）' : message.unobserved_consumption_bindings ? '本轮用量（计量回执未齐）' : message.consumption?.partial_responses ? '本轮用量（已记录小计）' : '本轮用量' }}</summary>
                    <p v-if="message.consumption_error" class="agent-message-note">计量读取失败，实际用量与费用未知。</p>
                    <template v-else>
                      <dl>
                        <dt>输入 tokens</dt><dd>{{ message.usage?.input_tokens ?? '未记录' }}</dd>
                        <dt>输出 tokens</dt><dd>{{ message.usage?.output_tokens ?? '未记录' }}</dd>
                        <dt>缓存读取 tokens</dt><dd>{{ message.usage?.cache_read_tokens ?? '未记录' }}</dd>
                        <dt>缓存写入 tokens</dt><dd>{{ message.usage?.cache_write_tokens ?? '未记录' }}</dd>
                      </dl>
                      <p class="agent-message-note">{{ estimatedAmounts(message.consumption) }}</p>
                      <p v-if="!message.consumption" class="agent-message-note">当前未提供消费回执，无法判断完整用量。</p>
                      <p v-else-if="message.consumption.partial_responses" class="agent-message-note">{{ message.consumption.partial_responses }} 次响应为部分计量；以上仅为已记录小计。</p>
                      <p v-if="message.unobserved_consumption_bindings" class="agent-message-note">有 {{ message.unobserved_consumption_bindings }} 个执行绑定尚无计量回执，已记录用量可能不完整。重新进入会话可读取最新计量。</p>
                    </template>
                    <p class="agent-message-note">仅本轮对话，不含关联项目／复测／报告任务；非完整账单。</p>
                    <p v-if="message.status === 'queued' || message.status === 'running'" class="agent-message-note">本轮尚未结束，当前记录不代表完整用量。</p>
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

      <div v-if="conversationId" class="agent-composer-region">
      <form class="agent-composer" @submit.prevent="send">
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
            placeholder="输入问题，或说明需要读取、整理的材料…" @keydown="handleComposerKeydown" />
          <div class="agent-composer-actions">
            <span v-if="loadingProjects" class="agent-project-context" role="status">正在读取项目…</span>
            <Select v-else class="agent-project-context" aria-label="本条消息的项目上下文" :model-value="projectId || '__none__'" :options="projectOptions" :disabled="Boolean(draft.pending || draft.sending)" @update:model-value="selectProject" />
            <input ref="attachmentInput" class="agent-file-input" type="file" multiple aria-label="选择消息附件" :disabled="attachmentsLocked || !attachmentLimits" @change="chooseAttachments" />
            <Button variant="ghost" :disabled="attachmentsLocked || !attachmentLimits" @click="attachmentInput?.click()"><template #icon><Paperclip :size="18" /></template>附件</Button>
            <span v-if="loadingAttachments" role="status">正在读取附件…</span>
            <Button type="submit" :loading="draft.sending" :disabled="!canSend"><template #icon><ArrowUp :size="18" /></template>{{ draft.pending && !draft.sending ? '重试同一消息' : hasActiveMessages ? '发送并排队' : '发送' }}</Button>
          </div>
        </div>
        <div class="agent-composer-meta">
          <details v-if="attachmentLimits" class="agent-attachment-limits"><summary>附件限制</summary><p>单文件最多 {{ attachmentSize(attachmentLimits.max_file_bytes) }}，会话最多 {{ attachmentLimits.max_count }} 个文件、合计 {{ attachmentSize(attachmentLimits.max_total_bytes) }}。UTF-8 文本可读取；其他文件交由工具处理，上传成功不等于已解析。</p></details>
          <p class="agent-cost-note">发送消息会使用模型。</p>
        </div>
      </form>
      </div>
    </div>

    <aside v-if="hasDeliverables && showWork" id="agent-work-panel" class="agent-deliverables" aria-label="工作与成果">
      <header><h2>工作与成果</h2><div class="agent-toolbar-actions"><Button variant="ghost" :loading="loadingWork" aria-label="刷新工作" title="刷新工作" @click="reloadWork"><template #icon><RefreshCw :size="16" /></template></Button><Button variant="ghost" aria-label="收起工作与成果" @click="toggleWork"><X :size="16" /></Button></div></header>
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
              <details class="agent-artifact-sources">
                <summary>{{ artifact.sources?.verified.length ? `来源 · 已核验 ${artifact.sources.verified.length} 项` : '来源 · 未核验' }}</summary>
                <p v-if="!artifact.sources?.verified.length" class="agent-message-note">没有服务端已核验来源；历史声明未补造核验。</p>
                <template v-else>
                  <p class="agent-message-note">仅证明交付时来源可读且版本匹配；核验时间不是首次读取时间，不证明模型使用了材料或结论正确。</p>
                  <div v-for="(source, index) in artifact.sources.verified" :key="index" class="agent-artifact-source">
                    <strong>{{ source.label }}</strong>
                    <p>{{ source.scope }}</p>
                    <p v-if="source.limitations">来源证据限制：{{ source.limitations }}</p>
                    <p>发布核验：<time :datetime="source.verified_at">{{ dateTime(source.verified_at) }}</time></p>
                    <details><summary>对象与版本</summary><p>SHA-256：{{ source.version }}</p><pre>{{ JSON.stringify(source.reference, null, 2) }}</pre><pre v-if="source.material_version && Object.keys(source.material_version).length">{{ JSON.stringify(source.material_version, null, 2) }}</pre></details>
                  </div>
                </template>
                <template v-if="artifact.sources?.declared.length || artifact.sources?.declared_read_at">
                  <h3>来源声明（未核验）</h3>
                  <p v-for="(note, index) in artifact.sources.declared" :key="index">{{ note }}</p>
                  <p v-if="artifact.sources.declared_read_at">声明读取时间：{{ artifact.sources.declared_read_at }}（未经服务端核验）</p>
                </template>
                <template v-if="artifact.sources?.limitations.length"><h3>证据限制（交付声明）</h3><p v-for="(limit, index) in artifact.sources.limitations" :key="index">{{ limit }}</p></template>
                <p class="agent-message-note">会话成品不等于正式评估接受或漏洞确认。</p>
              </details>
            </li>
          </ul>
        </section>
      </div>
    </aside>

    <Dialog :open="Boolean(deleteTarget)" title="删除会话" description="移除聊天历史并请求回收此会话的运行容器，不能再继续发送。已保存的工作区、session、证据及成果保留，不取消独立复测／报告。这不是安全擦除；回收结果将单独显示。" size="sm" content-class="workbench-surface" :dismissible="!deleting" @update:open="value => { if (!value && !deleting) deleteTarget = '' }">
      <p>确认删除{{ deleteTarget ? conversationLabel(deleteTarget) : '会话' }}？</p>
      <p v-if="deleteTarget && conversationHasDraft(deleteTarget)">此会话有未发送的输入或附件，删除后将清除本页草稿。</p>
      <Alert v-if="deleteError" tone="danger" title="删除请求未受理">{{ deleteError }}</Alert>
      <Alert v-if="unconfirmedDeletes[deleteTarget]" tone="warning" title="删除结果未确认">
        {{ unconfirmedDeletes[deleteTarget] }}。身份和本页草稿暂时保留；请核实已有回收请求。
        <p v-if="cleanupErrors[deleteTarget]">{{ cleanupErrors[deleteTarget] }}</p>
      </Alert>
      <template #footer><Button variant="secondary" :disabled="Boolean(deleting)" @click="deleteTarget = ''">{{ unconfirmedDeletes[deleteTarget] ? '关闭' : '取消' }}</Button><Button v-if="unconfirmedDeletes[deleteTarget]" variant="secondary" :loading="loadingCleanup === deleteTarget" :disabled="Boolean(loadingCleanup)" @click="verifyDeletion(deleteTarget)">核实删除结果</Button><Button v-else variant="danger" :loading="Boolean(deleting)" :disabled="Boolean(deleting)" @click="confirmDelete">确认删除</Button></template>
    </Dialog>
    <Dialog v-model:open="cleanupDetailsOpen" title="运行资源回收" size="md" content-class="workbench-surface">
      <div class="agent-cleanup-details">
        <Alert v-if="cleanupListError" tone="warning">{{ cleanupListError }}</Alert>
        <p v-if="!pendingCleanupResults.length">暂无待处理回收请求</p>
        <Alert v-for="result in pendingCleanupResults" :key="result.conversation_id" :tone="result.cleanup?.state === 'requested' ? 'info' : 'warning'" :title="`会话 ${result.conversation_id} 已从历史移除`">
          <p>{{ cleanupText(result.cleanup) }}</p>
          <details v-if="result.cleanup" class="agent-error-detail">
            <summary>请求详情</summary>
            <dl>
              <dt>请求标识</dt><dd><code>{{ result.cleanup.request_id }}</code></dd>
              <dt>已尝试次数</dt><dd>{{ result.cleanup.attempts }}</dd>
              <template v-if="result.cleanup.error_code"><dt>错误码</dt><dd><code>{{ result.cleanup.error_code }}</code></dd></template>
              <template v-if="result.cleanup.retry_at"><dt>重试时间</dt><dd>{{ dateTime(new Date(result.cleanup.retry_at * 1000).toISOString()) }}</dd></template>
            </dl>
          </details>
          <Button variant="ghost" size="sm" :loading="loadingCleanup === result.conversation_id" :disabled="Boolean(loadingCleanup)" @click="refreshCleanup(result.conversation_id)">刷新回收状态</Button>
          <p v-if="cleanupErrors[result.conversation_id]">{{ cleanupErrors[result.conversation_id] }}</p>
        </Alert>
      </div>
      <template #footer><Button variant="secondary" @click="loadCleanupRequests">刷新状态</Button></template>
    </Dialog>
    <Dialog v-model:open="answerSourceOpen" title="回复原文" size="lg" content-class="workbench-surface">
      <textarea ref="answerSourceInput" class="agent-answer-source" :value="answerSource" readonly aria-label="回复原文" @focus="answerSourceInput?.select()" />
    </Dialog>
    <Dialog v-model:open="configurationOpen" title="ChatBot 默认模型" description="仅影响本人租户的新会话。已有会话继续使用原模型，不改变正在执行的轮次。" size="md" content-class="workbench-surface" :dismissible="!savingConfiguration">
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
    <AlertDialog :open="leaveOpen" title="离开当前输入？" description="还有未发送或受理未确认的消息，离开将丢失本页输入。已受理消息仍会保留；请勿重复发送未确认的输入。" @update:open="value => { if (!value) resolveLeave(false) }">
      <template #cancel><Button variant="secondary">继续编辑</Button></template>
      <template #action><Button @click="resolveLeave(true)">确认离开</Button></template>
    </AlertDialog>
  </section>
</template>

<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { onBeforeRouteLeave, useRoute, useRouter } from 'vue-router'
import { ArrowDown, ArrowUp, ArrowUpRight, BookOpen, Bot, Check, Copy, Download, Ellipsis, FileText, MessageSquare, PanelRight, Paperclip, Plus, RefreshCw, ScanLine, Settings2, X } from '@lucide/vue'
import { Alert, AlertDialog, Badge, Button, Dialog, DropdownMenu, FormField, Select } from '@/components/ui'
import AgentMarkdown from './AgentMarkdown.vue'
import type { BadgeTone } from '@/components/ui/Badge.vue'
import { agentDownloadUrl, getAgentConfiguration, saveAgentConfiguration, type AgentConfiguration, type AgentCleanup, type AgentMessage, type AgentWork } from '@/api/projectAgent'
import { api, listTenantModels } from '@/api/client'
import type { TenantModel } from '@/api/types'
import { dateTime, formatRelativeTime } from '@/lib/format'
import { copyText } from '@/lib/copyText'
import { estimatedAmounts } from '@/lib/consumption'
import { toLogicalPixels } from '@/lib/uiScale'
import { useProjectAgent } from './useProjectAgent'
import { useUIStore } from '@/stores/ui'

const route = useRoute()
const router = useRouter()
const projectId = computed(() => typeof route.query.project === 'string' ? route.query.project : '')
const projects = ref<{ id: string; title: string }[]>([])
const projectsError = ref('')
const loadingProjects = ref(false)
const projectOptions = computed(() => [
  { value: '__none__', label: '无项目上下文' },
  ...(projectId.value && !projects.value.some(project => project.id === projectId.value)
    ? [{ value: projectId.value, label: `${projectId.value}（不可读取）` }] : []),
  ...projects.value.map(project => ({ value: project.id, label: `${project.title} · ${project.id}` })),
])
let projectController: AbortController | undefined
async function loadProjects() {
  projectController?.abort()
  const controller = new AbortController()
  projectController = controller
  loadingProjects.value = true
  projectsError.value = ''
  try {
    const rows = await api<{ id: string; title: string }[]>('GET', '/projects', undefined, controller.signal)
    if (!controller.signal.aborted) projects.value = rows
  } catch (error) {
    if (!controller.signal.aborted) projectsError.value = error instanceof Error ? error.message : '请求失败'
  } finally {
    if (!controller.signal.aborted) loadingProjects.value = false
  }
}
function selectProject(value: string | number) {
  if (draft.value.pending || draft.value.sending) return
  void router.replace({ query: { ...route.query, project: value === '__none__' ? undefined : String(value) } })
}
onMounted(() => void loadProjects())
onBeforeUnmount(() => projectController?.abort())
const conversationId = computed(() => typeof route.query.conversation === 'string' ? route.query.conversation : '')
const {
  conversations, messages, work, workError, loadingWork, reloadWork,
  loading, loadingMessages, creating, listError, createError, syncError,
  draft, hasUnsavedInput, loadConversations, createConversation, reconnect, send, editRejectedInput,
  deleting, deleteError, unconfirmedDeletes, removeConversation, conversationHasDraft,
  cleanupResults, cleanupListError, loadCleanupRequests, loadingCleanup, cleanupErrors, refreshCleanup, stopping, stopError, stopTurn,
  canSend, attachmentsLocked, attachmentLimits, attachmentError, loadingAttachments,
  loadAttachments, addAttachments, uploadAttachment, removeAttachment,
} = useProjectAgent(projectId, conversationId)
const messageList = ref<HTMLElement>()
const composerInput = ref<HTMLTextAreaElement>()
const attachmentInput = ref<HTMLInputElement>()
const deleteTarget = ref('')
const uiStore = useUIStore()
const cleanupDetailsOpen = ref(false)
const pendingCleanupResults = computed(() => cleanupResults.value.filter(result => result.cleanup?.state !== 'completed'))
const cleanupPendingCount = computed(() => pendingCleanupResults.value.filter(result => result.cleanup?.state === 'requested').length)
const cleanupFailedCount = computed(() => pendingCleanupResults.value.filter(result => result.cleanup?.state === 'failed').length)
const cleanupWithoutRequestCount = computed(() => pendingCleanupResults.value.filter(result => !result.cleanup).length)
const activeDeletions = new Set<string>()
const notifiedCleanupRequests = new Set<string>()
const notifiedRemovals = new Set<string>()
function openCleanupDetails() {
  cleanupDetailsOpen.value = true
  void loadCleanupRequests()
}
watch(cleanupResults, (results, previous) => {
  for (const result of results) {
    const cleanup = result.cleanup
    if (!cleanup || cleanup.state !== 'completed' || notifiedCleanupRequests.has(cleanup.request_id)) continue
    // 历史完成只登记，不重放；本页操作或已观察到的未完成转移才生成短反馈。
    notifiedCleanupRequests.add(cleanup.request_id)
    const prior = previous.find(row => row.cleanup?.request_id === cleanup.request_id)?.cleanup
    if (activeDeletions.has(result.conversation_id) || (prior && prior.state !== 'completed')) {
      uiStore.showToast('会话已移除，运行资源已回收', 'success')
    }
  }
}, { flush: 'sync' })
function notifyRemoval(id: string) {
  const result = cleanupResults.value.find(row => row.conversation_id === id)
  if (!result || result.cleanup?.state === 'completed') return
  const key = result.cleanup?.request_id || id
  if (notifiedRemovals.has(key)) return
  notifiedRemovals.add(key)
  uiStore.showToast('已从历史移除', 'success')
}
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
  if (!id) return
  activeDeletions.add(id)
  try {
    if (await removeConversation(id)) {
      notifyRemoval(id)
      await finishDelete(id)
    }
  } finally {
    activeDeletions.delete(id)
  }
}
async function verifyDeletion(id: string) {
  activeDeletions.add(id)
  try {
    if (await refreshCleanup(id)) {
      notifyRemoval(id)
      await finishDelete(id)
    }
  } finally {
    activeDeletions.delete(id)
  }
}
async function finishDelete(id: string) {
  if (deleteTarget.value === id) deleteTarget.value = ''
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
const showWork = ref(false)
async function changePanels(change: () => void) {
  const list = messageList.value
  const following = nearBottom.value
  const anchor = list && [...list.querySelectorAll<HTMLElement>('.agent-turn')]
    .find(turn => turn.getBoundingClientRect().bottom > list.getBoundingClientRect().top)
  const top = anchor?.getBoundingClientRect().top
  change()
  await nextTick()
  // 辅栏改变可读宽度；阅读旧轮次时保留锚点，不把用户带回底部。
  if (following) scrollToLatest()
  else if (list && anchor && top !== undefined) list.scrollTop += toLogicalPixels(anchor.getBoundingClientRect().top - top)
  nearBottom.value = following
}
async function toggleWork() {
  await changePanels(() => {
    showWork.value = !showWork.value
  })
  document.querySelector<HTMLButtonElement>('[aria-controls="agent-work-panel"]')?.focus()
}
const copyFeedback = ref<{ id: number; text: string; success: boolean }>()
const orderedConversations = computed(() => [...conversations.value].reverse())
const currentConversation = computed(() => conversations.value.find(row => row.id === conversationId.value))
const currentConversationLabel = computed(() => conversationId.value ? conversationLabel(conversationId.value) : 'ChatBot')
function conversationLabel(id: string) {
  const index = conversations.value.findIndex(conversation => conversation.id === id)
  return index >= 0 ? `会话 ${index + 1}` : '会话'
}
function cleanupText(cleanup: AgentCleanup | null) {
  if (!cleanup) return '此历史删除没有回收请求；本次未自动清理历史资源。'
  if (cleanup.state === 'completed') return '运行资源已确认回收；宿主持久材料仍保留。'
  if (cleanup.state === 'failed') return cleanup.attempts >= 6
    ? '运行资源回收未确认，自动重试已用尽；需检查执行机器。已保存材料仍保留。'
    : '运行资源回收暂未确认，系统将按原请求重试；已保存材料仍保留。'
  return '运行资源回收已请求，尚未确认完成。可刷新回收状态；已保存材料仍保留。'
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
    model_unconfigured: '尚未配置 ChatBot 默认模型。', model_not_found: '所引用的模型已不存在。',
    model_disabled: '模型已停用，请在模型管理中启用。', model_incomplete: '模型配置不完整，请检查模型名称与连接设置。',
    model_protocol_unsupported: '当前模型协议不受支持。', credential_missing: '模型缺少可用凭据，请在模型管理中配置。',
    model_endpoint_invalid: '模型地址无效，请检查连接设置。',
  }
  return reason ? labels[reason] || `模型当前不可用：${reason}` : '模型当前不可用，请检查配置。'
}
function conversationUnavailable(reason?: string | null) {
  if (reason === 'session_verification_required') return '原 session 来源尚未验证，不能继续此会话。可以手动新建独立会话，原历史不会被替换。'
  return modelUnavailable(reason)
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
  await changePanels(() => { showWork.value = true })
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
const leaveOpen = ref(false)
let leaveResolver: ((value: boolean) => void) | undefined
function resolveLeave(value: boolean) {
  leaveOpen.value = false
  const resolve = leaveResolver
  leaveResolver = undefined
  resolve?.(value)
}
function allowLeave(): boolean | Promise<boolean> {
  if (creating.value || savingConfiguration.value || deleting.value) return false
  if (!hasUnsavedInput.value) return true
  if (leaveResolver) return false
  leaveOpen.value = true
  return new Promise(resolve => { leaveResolver = resolve })
}
onBeforeUnmount(() => resolveLeave(false))
onBeforeRouteLeave(allowLeave)
function protectReload(event: BeforeUnloadEvent) {
  if (!hasUnsavedInput.value && !creating.value && !savingConfiguration.value && !deleting.value) return
  event.preventDefault()
  event.returnValue = ''
}
onMounted(() => window.addEventListener('beforeunload', protectReload))
onBeforeUnmount(() => window.removeEventListener('beforeunload', protectReload))
</script>

<style scoped>
.project-agent.project-agent { display: grid; grid-template-columns: var(--workbench-history-width) minmax(0, 1fr); flex: 1 1 0; min-width: 0; min-height: 0; margin: var(--layout-content-padding); border: 1px solid var(--color-border-subtle); border-radius: var(--radius-lg); box-shadow: var(--shadow-sm); color: var(--color-text); background: var(--color-surface); font-size: var(--text-body); }
.project-agent.has-deliverables { grid-template-columns: var(--workbench-history-width) minmax(0, 1fr) var(--workbench-results-width); }
.agent-history { display: flex; flex-direction: column; gap: var(--space-2); min-width: 0; min-height: 0; padding: 0 var(--space-3) var(--space-3); border-right: 1px solid var(--color-border-subtle); border-radius: var(--radius-lg) 0 0 var(--radius-lg); background: var(--color-surface); }
.agent-history-heading { display: flex; align-items: center; min-height: var(--layout-topbar-height); }
.agent-history h2 { margin: 0; font-size: var(--text-section); font-weight: var(--font-semibold); }
.agent-history p { font-size: var(--text-sm); color: var(--color-text-secondary); }
.agent-history-list { flex: 1; min-height: 0; overflow-y: auto; overscroll-behavior: contain; }
.agent-cleanup-entry { flex: none; flex-wrap: wrap; justify-content: flex-start; }
.agent-cleanup-details { display: grid; gap: var(--space-3); }
.agent-history-row { display: flex; align-items: center; min-height: var(--control-height); border-radius: var(--radius-md); transition: background-color var(--dur-fast) var(--ease), color var(--dur-fast) var(--ease); }
.agent-history-row:hover { background: var(--color-surface-hover); }
.agent-history-row.is-current { background: var(--color-surface-hover); color: var(--color-text); box-shadow: inset 2px 0 var(--color-accent); }
.agent-history-select { display: flex; align-items: center; gap: var(--space-2); flex: 1; min-width: 0; min-height: var(--control-height); padding: 0 var(--space-2); border: 0; border-radius: var(--radius-md); background: transparent; text-align: left; color: inherit; font: inherit; cursor: pointer; }
.agent-history-select span { overflow: hidden; white-space: nowrap; text-overflow: ellipsis; }
.agent-history-select svg { flex: none; }
.agent-history-select:focus-visible { outline: 2px solid var(--color-accent); outline-offset: -2px; }
.agent-history-select:disabled { cursor: not-allowed; opacity: .5; }
.agent-history-menu.ui-button { flex: none; width: var(--control-height-sm); min-height: var(--control-height-sm); padding: 0; }
.agent-conversation { display: flex; flex-direction: column; min-width: 0; min-height: 0; background: var(--color-surface); }
.agent-toolbar { display: flex; align-items: center; flex-wrap: wrap; justify-content: space-between; gap: var(--space-2) var(--space-4); min-height: var(--layout-topbar-height); padding: var(--space-2) var(--layout-content-padding); border-bottom: 1px solid var(--color-border); }
.agent-toolbar-actions { display: flex; align-items: center; flex-wrap: wrap; gap: var(--space-2); }
.agent-composer-actions :deep(.agent-project-context) { width: min(20rem, 45%); margin-right: auto; }
.agent-conversation-heading { display: flex; align-items: center; flex-wrap: wrap; gap: var(--space-3); min-width: 0; }
.agent-conversation-heading h2 { margin: 0; font-size: var(--text-section); font-weight: var(--font-semibold); }
.agent-current-model { font-size: var(--text-sm); color: var(--color-text-secondary); }
.agent-notices { display: grid; gap: var(--space-2); padding-inline: var(--layout-content-padding); }
.agent-notices:empty { display: none; }
.agent-notices a { display: inline-block; margin-left: var(--space-2); }
.agent-reading-area { position: relative; display: flex; flex: 1 1 0; min-height: 0; }
/* 阅读区和输入区保留相同的双侧滚动槽，滚动条出现时也共轴。 */
.agent-messages { flex: 1; min-width: 0; overflow-y: auto; scrollbar-gutter: stable both-edges; overscroll-behavior: contain; padding: var(--layout-content-padding) var(--layout-content-padding) var(--space-6); }
.agent-reading-column, .agent-composer { width: min(100%, var(--workbench-reading-width)); margin-inline: auto; box-sizing: border-box; }
.agent-turn + .agent-turn { margin-top: var(--space-7); padding-top: var(--space-7); border-top: 1px solid var(--color-border); }
.agent-turn-identity { display: flex; justify-content: flex-end; flex-wrap: wrap; align-items: center; gap: var(--space-3); margin-bottom: var(--space-3); font-size: var(--text-sm); }
.agent-turn-identity time { color: var(--color-text-secondary); }
.agent-user-text { width: fit-content; max-width: 80%; margin-left: auto; padding: var(--space-3) var(--space-5); border-radius: var(--workbench-bubble-radius); background: var(--color-surface-selected); white-space: pre-wrap; overflow-wrap: anywhere; line-height: var(--leading-reading); }
.agent-answer { font-size: var(--workbench-body-size); }
.agent-answer-region { margin-top: var(--space-6); }
.agent-author { display: flex; align-items: center; gap: var(--space-2); margin-bottom: var(--space-4); font-size: var(--text-sm); color: var(--color-text-secondary); font-weight: var(--font-medium); }
.agent-answer-heading { margin-bottom: var(--space-3); }
.agent-turn-footer { display: flex; align-items: center; flex-wrap: wrap; gap: var(--space-2) var(--space-3); margin-top: var(--space-4); color: var(--color-text-secondary); font-size: var(--text-xs); }
.agent-error-detail { overflow-wrap: anywhere; }
.agent-error-detail summary { cursor: pointer; padding-block: var(--space-2); }
.agent-error-detail[open] { flex-basis: 100%; }
.agent-error-detail dl { display: grid; grid-template-columns: auto 1fr; gap: var(--space-2) var(--space-3); margin: var(--space-2) 0; }
.agent-error-detail dd { margin: 0; }
.agent-usage-detail dd { font-variant-numeric: tabular-nums; }
.agent-latest { position: absolute; bottom: var(--space-3); left: 50%; transform: translateX(-50%); box-shadow: var(--shadow-sm); }
.agent-empty { width: min(100%, var(--workbench-reading-width)); margin: var(--space-8) auto 0; display: flex; flex-direction: column; align-items: flex-start; gap: var(--space-4); box-sizing: border-box; }
.agent-empty p { margin: 0; color: var(--color-text-secondary); }
.agent-suggestions { display: flex; flex-wrap: wrap; justify-content: center; gap: var(--space-4); margin-top: var(--space-3); }
.agent-suggestions button { display: flex; align-items: center; gap: var(--space-3); min-height: var(--control-height); padding: var(--space-2) var(--space-3); border: 1px solid var(--color-border-strong); border-radius: var(--radius-md); color: var(--color-text); background: var(--color-surface); cursor: pointer; font: inherit; font-size: var(--text-md); text-align: left; }
.agent-suggestions button:hover { background: var(--color-surface-hover); border-color: var(--color-border-strong); }
.agent-suggestions button span { flex: 1; }
.agent-loading { text-align: center; color: var(--color-text-secondary); }
.agent-composer-region { flex: 0 1 auto; min-height: 0; max-height: 50%; overflow-y: auto; scrollbar-gutter: stable both-edges; overscroll-behavior: contain; }
.agent-composer { width: min(calc(100% - var(--layout-content-padding) * 2), var(--workbench-reading-width)); padding-block: var(--space-3); border-top: 1px solid var(--color-border-subtle); }
.agent-composer > .ui-alert { margin-bottom: var(--space-3); }
.agent-input-surface { border: 1px solid var(--color-control-border); border-radius: var(--radius-lg); background: var(--color-surface); box-shadow: var(--shadow-sm); }
.agent-input-surface:focus-within { border-color: var(--color-accent); box-shadow: var(--ring); }
.agent-input-surface textarea { display: block; box-sizing: border-box; width: 100%; min-height: 64px; max-height: 240px; padding: var(--space-3) var(--space-5) var(--space-2); resize: none; overflow-y: auto; border: none; border-radius: var(--radius-md); outline: none; background: transparent; color: var(--color-text); font: inherit; font-size: var(--text-body); line-height: 1.6; }
.agent-input-surface textarea::placeholder { color: var(--color-text-tertiary); }
.agent-composer-actions { display: flex; align-items: center; gap: var(--space-3); justify-content: space-between; padding: var(--space-1) var(--space-3) var(--space-3); }
.agent-composer-actions > .ui-button { min-height: var(--control-height); padding-inline: var(--space-4); }
.agent-composer-meta { display: flex; align-items: baseline; flex-wrap: wrap; justify-content: space-between; gap: var(--space-1) var(--space-3); margin-top: var(--space-2); color: var(--color-text-secondary); font-size: var(--text-xs); }
.agent-cost-note { margin: 0 0 0 auto; line-height: var(--leading-normal); }
.agent-file-input { display: none; }
.agent-draft-attachments, .agent-message-attachments { display: grid; gap: var(--space-2); list-style: none; margin: var(--space-3) var(--space-4); padding: 0; font-size: var(--text-sm); }
.agent-draft-attachments { max-height: 180px; overflow-y: auto; }
.agent-draft-attachments li, .agent-message-attachments li { display: flex; align-items: center; flex-wrap: wrap; gap: var(--space-2); }
.agent-draft-attachments li p { flex-basis: 100%; color: var(--color-danger); margin: 0; overflow-wrap: anywhere; }
.agent-attachment-name { min-width: 0; overflow-wrap: anywhere; flex: 1; }
.agent-message-attachments { justify-items: end; }
.agent-message-attachments a { overflow-wrap: anywhere; }
.agent-attachment-limits { font-size: var(--text-sm); color: var(--color-text-secondary); }
.agent-attachment-limits summary { cursor: pointer; width: fit-content; }
.agent-attachment-limits p { margin: var(--space-2) 0; line-height: var(--leading-relaxed); }
.agent-answer-source { box-sizing: border-box; width: 100%; min-height: 320px; max-height: calc(65 * var(--ui-dvh)); padding: var(--space-4); border: 1px solid var(--color-border-strong); border-radius: var(--radius-md); resize: vertical; font: var(--text-body)/var(--leading-reading) var(--font-sans); color: var(--color-text); background: var(--color-surface); }
.agent-message-note, .agent-configuration-form p { margin: 0; color: var(--color-text-secondary); line-height: var(--leading-relaxed); }
.agent-configuration-form { display: grid; gap: var(--space-5); }
.agent-configuration-form a { display: inline-flex; align-items: center; gap: var(--space-1); }
.agent-turn-deliverables, .agent-consumption-error { margin-top: var(--space-4); }
.agent-deliverables { display: flex; flex-direction: column; min-height: 0; min-width: 0; border-left: 1px solid var(--color-border-subtle); background: var(--color-sidebar); font-size: var(--text-md); }
.agent-deliverables > header { display: flex; align-items: center; flex-wrap: wrap; justify-content: space-between; min-height: var(--layout-topbar-height); padding: var(--space-2) var(--space-5); }
.agent-deliverables h2 { font-size: var(--text-section); font-weight: var(--font-semibold); margin: 0; }
.agent-deliverables-scroll { overflow-y: auto; min-height: 0; padding: 0 var(--space-5) var(--space-5); }
.agent-deliverable-group + .agent-deliverable-group { border-top: 1px solid var(--color-border); margin-top: var(--space-5); padding-top: var(--space-5); }
.agent-work-source { display: flex; align-items: center; width: 100%; gap: var(--space-2); padding: var(--space-2) 0 var(--space-3); border: 0; color: var(--color-text-secondary); background: transparent; text-align: left; font: inherit; font-size: var(--text-xs); cursor: pointer; }
.agent-work-source span { flex: 1; min-width: 0; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.agent-work-source svg { flex-shrink: 0; }
.agent-work { list-style: none; padding: 0; margin: 0; }
.agent-work-item { display: grid; justify-items: start; gap: var(--space-3); padding-block: var(--space-4); border-top: 1px solid var(--color-border); }
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
.agent-artifacts li { padding-block: var(--space-3); border-top: 1px solid var(--color-border); }
.agent-artifacts a { display: inline-flex; align-items: center; gap: var(--space-2); min-height: var(--control-height); overflow-wrap: anywhere; }
.agent-artifacts svg { flex-shrink: 0; }
.agent-artifact-sources { margin-top: var(--space-2); overflow-wrap: anywhere; }
.agent-artifact-sources summary { cursor: pointer; color: var(--color-text-secondary); }
.agent-artifact-sources p { margin-block: var(--space-2); white-space: pre-wrap; }
.agent-artifact-sources h3 { font-size: inherit; margin-block: var(--space-3) var(--space-2); }
.agent-artifact-source { margin-block: var(--space-3); }
.agent-artifact-sources pre { white-space: pre-wrap; font-size: var(--text-xs); }
@container workspace (max-width: 1100px) {
  .project-agent.has-deliverables { grid-template-columns: var(--workbench-history-width) minmax(0, 1fr); grid-template-rows: minmax(0, 1fr) auto; }
  .project-agent.has-deliverables .agent-history { grid-row: 1 / 3; }
  .agent-deliverables { grid-column: 2; max-height: calc(40 * var(--ui-dvh)); border-top: 1px solid var(--color-border); }
}
@container workspace (max-width: 760px) {
  .project-agent.project-agent { grid-template-columns: minmax(140px, 28%) minmax(0, 1fr); }
  .agent-toolbar { padding: var(--space-3); }
  .agent-notices, .agent-messages { padding-inline: var(--space-3); }
  .agent-composer { width: calc(100% - var(--space-3) * 2); }
  .agent-composer-actions { flex-wrap: wrap; justify-content: flex-end; }
}
@media (prefers-reduced-motion: reduce) {
  .agent-history-row { transition: none; }
}
</style>
