<template>
  <article class="console-page configuration-page tenant-models-page">
    <ErrorState
      v-if="loadError"
      title="Model 加载失败"
      :description="errorText(loadError)"
      @retry="refreshModels"
    />
    <Alert v-if="revisionConflict" tone="danger">
      Model 已被其他会话更新。服务端数据已刷新，请核对后重试。
    </Alert>

    <MasterDetail configuration>
      <template #master>
        <Card class="console-card configuration-panel configuration-list model-master">
          <div class="list-header">
            <div>
              <strong>模型</strong>
              <span class="text-faint"> · {{ models.length }} 个配置</span>
            </div>
            <div class="inline-actions">
              <IconButton label="刷新模型" :loading="refreshing" @click="refreshModels"
                ><RefreshCw :size="16"
              /></IconButton>
              <Button size="sm" @click="startCreate">新增</Button>
            </div>
          </div>
          <template v-if="modelsPending">
            <Skeleton
              v-for="index in 3"
              :key="index"
              :decorative="index !== 1"
              label="正在加载 Model"
            />
          </template>
          <button
            v-for="model in models"
            :key="model.id"
            type="button"
            class="model-card"
            :class="{ active: selectedId === model.id && !creating }"
            @click="selectModel(model.id)"
          >
            <span class="model-title">{{ model.display_name }}</span>
            <span class="text-faint mono model-url"
              >{{ model.model_name || '未填写 Provider Model ID' }} · {{ model.base_url }}</span
            >
            <span class="model-meta">
              <Badge size="sm">{{ protocolLabel(model.protocol) }}</Badge>
              <StatusBadge :status="model.enabled ? 'healthy' : 'stopped'" size="sm">
                {{ model.enabled ? '已启用' : '已停用' }}
              </StatusBadge>
              <StatusBadge :status="model.credential_configured ? 'healthy' : 'missing'" size="sm">
                Key {{ model.credential_configured ? '已配置' : '未配置' }}
              </StatusBadge>
            </span>
          </button>
          <EmptyState
            v-if="!modelsPending && !models.length"
            title="暂无 Model"
            description="请先新增当前 Tenant 的 Model 配置。"
          />
        </Card>
      </template>

      <template #detail>
        <Card v-if="creating || currentModel" class="console-card configuration-panel model-detail">
          <div class="editor-title-row">
            <div>
              <h2>{{ creating ? '新增 Model' : '编辑 Model' }}</h2>
            </div>
            <Button
              v-if="currentModel && !creating"
              variant="danger"
              size="sm"
              :disabled="saving || credentialBusy"
              @click="requestModelRemoval"
            >
              <template #icon><Trash2 :size="14" /></template>
              删除
            </Button>
          </div>

          <form class="managed-form model-form" @submit.prevent="saveModel">
            <section class="model-section">
              <div class="model-section-content">
                <FormField
                  v-slot="{ controlAttrs }"
                  class="form-group"
                  label="自定义 Model 名称"
                  required
                >
                  <Input
                    v-bind="controlAttrs"
                    :model-value="draft.display_name"
                    maxlength="128"
                    placeholder="例如：DeepSeek 官方"
                    @update:model-value="updateDisplayName"
                  />
                </FormField>
                <div class="form-grid">
                  <label class="form-group">
                    <span class="form-label">协议格式</span>
                    <Select
                      :model-value="draft.protocol"
                      :options="protocolOptions"
                      aria-label="Model 协议格式"
                      @update:model-value="updateProtocol"
                    />
                  </label>
                  <label class="form-group toggle-field">
                    <span class="form-label">状态</span>
                    <Checkbox v-model="draft.enabled" label="启用该 Model" />
                  </label>
                </div>
                <label class="form-group">
                  <span class="form-label">Base URL</span>
                  <Input
                    :model-value="draft.base_url"
                    class="mono"
                    type="url"
                    placeholder="https://api.example.com/custom/prefix"
                    required
                    @update:model-value="updateBaseUrl"
                  />
                </label>
              </div>
            </section>

            <section class="model-section">
              <div class="model-section-content">
                <div class="form-grid">
                  <label class="form-group">
                    <span class="form-label">认证字段</span>
                    <Select
                      :model-value="draft.auth_header"
                      class="mono"
                      :options="authHeaderOptions"
                      aria-label="Model 认证字段"
                      @update:model-value="updateAuthHeader"
                    />
                  </label>
                  <FormField v-slot="{ controlAttrs }" class="form-group" label="API Key">
                    <Input
                      v-bind="controlAttrs"
                      v-model="credentialInput"
                      class="mono"
                      type="password"
                      autocomplete="new-password"
                      :placeholder="
                        currentModel?.credential_configured
                          ? '留空则保留已保存 Key'
                          : '可留空保存配置'
                      "
                    />
                  </FormField>
                </div>
                <div class="credential-status-row">
                  <span class="text-faint">已保存的 Key 不回填；留空保留原值。</span>
                  <Button
                    v-if="currentModel && !creating"
                    variant="danger"
                    size="sm"
                    :disabled="saving || credentialBusy || !currentModel.credential_configured"
                    @click="requestCredentialRemoval"
                  >
                    删除已保存 Key
                  </Button>
                </div>
              </div>
            </section>

            <details class="advanced-config" :open="Boolean(headerError)">
              <summary>
                自定义 Headers{{
                  draft.custom_headers.length ? `（${draft.custom_headers.length}）` : ''
                }}
              </summary>
              <div class="model-section-content advanced-grid">
                <div class="section-title-row">
                  <div>
                    <p class="text-faint">
                      名称和值将明文保存并完整回显，请仅在受控内部环境中使用。
                    </p>
                  </div>
                  <Button
                    variant="secondary"
                    size="sm"
                    :disabled="draft.custom_headers.length >= 32"
                    @click="addHeader"
                  >
                    新增 Header
                  </Button>
                </div>
                <div
                  v-for="(header, index) in draft.custom_headers"
                  :key="index"
                  class="header-row"
                >
                  <Input v-model="header.name" class="mono" placeholder="Header 名称" />
                  <Input v-model="header.value" class="mono" placeholder="Header 值" />
                  <Button variant="danger" size="sm" @click="removeHeader(index)">删除</Button>
                </div>
                <p v-if="headerError" class="field-error">{{ headerError }}</p>
              </div>
            </details>

            <details class="advanced-config">
              <summary>高级 endpoint 配置</summary>
              <div class="form-grid advanced-grid">
                <label class="form-group">
                  <span class="form-label">Models endpoint</span>
                  <Input
                    v-model="draft.models_endpoint"
                    class="mono"
                    :placeholder="defaults.models"
                  />
                  <span class="text-faint"
                    >留空使用 {{ defaults.models }}；可填写相对路径或绝对 HTTP(S) URL。</span
                  >
                </label>
                <label class="form-group">
                  <span class="form-label">调用 endpoint</span>
                  <Input
                    v-model="draft.inference_endpoint"
                    class="mono"
                    :placeholder="defaults.inference"
                  />
                  <span class="text-faint"
                    >留空使用 {{ defaults.inference }}；可填写相对路径或绝对 HTTP(S) URL。</span
                  >
                </label>
              </div>
            </details>

            <section class="model-section">
              <div class="model-section-content">
                <div class="section-title-row">
                  <label class="form-group grow-field">
                    <span class="form-label">Provider Model ID</span>
                    <Input
                      :model-value="draft.model_name"
                      class="mono"
                      placeholder="例如：deepseek-v4-flash"
                      required
                      @update:model-value="updateModelName"
                    />
                  </label>
                  <Button
                    class="discovery-button"
                    variant="secondary"
                    :loading="discovering"
                    :disabled="!canRequest"
                    @click="discoverModels"
                  >
                    <template #icon><Search :size="14" /></template>
                    {{ discovering ? '获取中...' : '获取模型列表' }}
                  </Button>
                </div>
                <label v-if="discoveryResult?.model_names.length" class="form-group">
                  <span class="form-label">获取结果</span>
                  <Select
                    v-model="draft.model_name"
                    class="mono"
                    :options="discoveryOptions"
                    placeholder="请选择模型"
                    aria-label="Provider Model ID 获取结果"
                  />
                </label>
                <p v-if="discoveryResult" class="request-meta mono">
                  HTTP {{ discoveryResult.http_status }} · {{ discoveryResult.latency_ms }} ms ·
                  {{ discoveryResult.endpoint }}
                </p>
                <p v-if="discoveryError" class="field-error">获取失败：{{ discoveryError }}</p>
                <p v-if="requestDisabledReason" class="text-faint">{{ requestDisabledReason }}</p>
              </div>
            </section>

            <details class="advanced-config">
              <summary>连接测试（可选）</summary>
              <div class="model-section-content advanced-grid">
                <label class="form-group">
                  <span class="form-label">真实请求内容</span>
                  <Textarea
                    v-model="promptInput"
                    class="prompt-input"
                    maxlength="16384"
                    :rows="3"
                  />
                </label>
                <div class="inline-actions">
                  <Button
                    variant="secondary"
                    :loading="testing"
                    :disabled="!canTest"
                    @click="testModel"
                  >
                    <template #icon><Activity :size="14" /></template>
                    {{ testing ? '发送中...' : '发送测试' }}
                  </Button>
                </div>
                <Card
                  v-if="testResult"
                  padding="none"
                  :class="['test-result', testResult.success ? 'success' : 'error']"
                >
                  <div class="request-meta mono">
                    <StatusBadge :status="testResult.success ? 'success' : 'failed'" size="sm">
                      {{ testResult.success ? '请求成功' : '上游返回失败状态' }}
                    </StatusBadge>
                    <span>HTTP {{ testResult.http_status }}</span>
                    <span>{{ testResult.latency_ms }} ms</span>
                    <span>{{ testResult.endpoint }}</span>
                  </div>
                  <pre>{{ testResult.response_body }}</pre>
                </Card>
                <p v-if="testError" class="field-error">测试失败：{{ testError }}</p>
                <p v-if="!canTest" class="text-faint">
                  {{
                    requestDisabledReason ||
                    (!draft.model_name.trim()
                      ? '填写 Provider Model ID 后可测试。'
                      : '填写测试请求内容后可测试。')
                  }}
                </p>
              </div>
            </details>

            <footer class="editor-footer model-save-actions">
              <Button variant="secondary" :disabled="saving" @click="resetDraft"> 取消修改 </Button>
              <Button type="submit" :loading="saving" :disabled="!canSave">
                {{ saving ? '保存中...' : '保存 Model' }}
              </Button>
            </footer>
          </form>
        </Card>

        <EmptyState
          v-else
          class="console-card empty-detail"
          title="选择或新增 Model"
          description="Model 与 API Key 按当前登录用户的个人 Tenant 隔离。"
        />
      </template>
    </MasterDetail>

    <ConfirmAction
      :open="Boolean(pendingAction)"
      :title="pendingActionTitle"
      :description="pendingActionDescription"
      :confirm-label="pendingActionConfirmLabel"
      tone="danger"
      :loading="pendingActionBusy"
      @update:open="handlePendingActionOpenChange"
      @confirm="confirmPendingAction"
    />
  </article>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, reactive, ref, watch } from 'vue'
import { useQuery, useQueryClient } from '@tanstack/vue-query'
import { Activity, RefreshCw, Search, Trash2 } from '@lucide/vue'
import {
  createTenantModel,
  deleteTenantModel,
  deleteTenantModelCredential,
  discoverTenantModelDraft,
  isRevisionConflict,
  listTenantModels,
  testTenantModelDraft,
  updateTenantModel,
} from '@/api/client'
import type { ModelProtocol, TenantModelConfig } from '@/api/types'
import { ConfirmAction, ErrorState, MasterDetail } from '@/components/patterns'
import {
  Alert,
  Badge,
  Button,
  Card,
  Checkbox,
  EmptyState,
  FormField,
  IconButton,
  Input,
  Select,
  Skeleton,
  StatusBadge,
  Textarea,
} from '@/components/ui'
import { useUIStore } from '@/stores/ui'
import {
  customHeaderError,
  emptyTenantModelDraft,
  protocolDefaultEndpoints,
  selectAfterModelListChange,
  tenantModelDraft,
  tenantModelDraftIsValid,
} from './tenantModelsModel'
import '@/design-system/configuration-workspace.css'
import './tenantModelsPage.css'

type PendingModelAction = {
  kind: 'delete-model' | 'delete-credential'
  modelId: string
  modelName: string
  revision: number
}

const protocolOptions = [
  { value: 'anthropic-messages', label: 'Anthropic Messages' },
  { value: 'openai-chat-completions', label: 'OpenAI Chat Completions' },
  { value: 'openai-responses', label: 'OpenAI Responses API' },
] as const
const authHeaderOptions = [
  { value: 'authorization-bearer', label: 'Authorization: Bearer key' },
  { value: 'x-api-key', label: 'x-api-key: key' },
] as const

const uiStore = useUIStore()
const queryClient = useQueryClient()
const selectedId = ref('')
const creating = ref(false)
const saving = ref(false)
const discovering = ref(false)
const testing = ref(false)
const credentialBusy = ref(false)
const refreshing = ref(false)
const revisionConflict = ref(false)
const pendingAction = ref<PendingModelAction | null>(null)
const credentialInput = ref('')
const promptInput = ref('hello')
const discoveryResult = ref<Awaited<ReturnType<typeof discoverTenantModelDraft>> | null>(null)
const testResult = ref<Awaited<ReturnType<typeof testTenantModelDraft>> | null>(null)
const discoveryError = ref('')
const testError = ref('')
const draft = reactive(emptyTenantModelDraft())

const modelsQuery = useQuery({ queryKey: ['tenant-models'], queryFn: listTenantModels })
const models = computed(() => modelsQuery.data.value || [])
const modelsPending = computed(() => modelsQuery.isPending.value)
const loadError = computed(() => modelsQuery.error.value)
const currentModel = computed(
  () => models.value.find((model) => model.id === selectedId.value) || null,
)
const defaults = computed(() => protocolDefaultEndpoints(draft.protocol))
const headerError = computed(() => customHeaderError(draft))
const discoveryOptions = computed(() =>
  (discoveryResult.value?.model_names || []).map((name) => ({ value: name, label: name })),
)
const pendingActionBusy = computed(() => saving.value || credentialBusy.value)
const pendingActionTitle = computed(() =>
  pendingAction.value?.kind === 'delete-credential' ? '删除 API Key' : '删除 Model',
)
const pendingActionDescription = computed(() => {
  const action = pendingAction.value
  if (!action) return '确认执行该操作？'
  return action.kind === 'delete-credential'
    ? `删除 Model「${action.modelName}」已保存的 API Key？该操作无法撤销。`
    : `删除 Model「${action.modelName}」？该操作无法撤销，引用该 Model 的 Worker 可能无法继续运行。`
})
const pendingActionConfirmLabel = computed(() =>
  pendingAction.value?.kind === 'delete-credential' ? '确认删除 Key' : '确认删除 Model',
)
const hasCredential = computed(() =>
  Boolean(credentialInput.value || currentModel.value?.credential_configured),
)
const canRequest = computed(() =>
  Boolean(draft.base_url.trim() && hasCredential.value && !headerError.value),
)
const requestDisabledReason = computed(() =>
  !draft.base_url.trim()
    ? '填写 Base URL 后可获取模型和测试。'
    : !hasCredential.value
      ? '配置 API Key 后可获取模型和测试。'
      : headerError.value,
)
const canTest = computed(() =>
  Boolean(canRequest.value && draft.model_name.trim() && promptInput.value),
)
const canSave = computed(() => tenantModelDraftIsValid(draft))

watch(
  models,
  (value) => {
    if (creating.value) return
    const nextId = selectAfterModelListChange(value, selectedId.value)
    if (nextId !== selectedId.value) {
      selectedId.value = nextId
      loadDraft()
    }
  },
  { immediate: true },
)

watch(
  () => [
    draft.protocol,
    draft.base_url,
    draft.auth_header,
    draft.models_endpoint,
    draft.inference_endpoint,
    draft.model_name,
    draft.custom_headers.map((header) => [header.name, header.value]),
    credentialInput.value,
    promptInput.value,
  ],
  clearRequestResults,
  { deep: true },
)

function errorText(error: unknown) {
  return error instanceof Error ? error.message : String(error)
}

function trimmedInput(value: string | number | null) {
  return String(value ?? '').trim()
}

function updateDisplayName(value: string | number | null) {
  draft.display_name = trimmedInput(value)
}

function updateBaseUrl(value: string | number | null) {
  draft.base_url = trimmedInput(value)
}

function updateModelName(value: string | number | null) {
  draft.model_name = trimmedInput(value)
}

function protocolLabel(protocol: ModelProtocol) {
  if (protocol === 'anthropic-messages') return 'Anthropic Messages'
  if (protocol === 'openai-chat-completions') return 'OpenAI Chat Completions'
  return 'OpenAI Responses API'
}

function updateProtocol(value: string) {
  if (
    value === 'anthropic-messages' ||
    value === 'openai-chat-completions' ||
    value === 'openai-responses'
  ) {
    draft.protocol = value
  }
}

function updateAuthHeader(value: string) {
  if (value === 'authorization-bearer' || value === 'x-api-key') {
    draft.auth_header = value
  }
}

function clearRequestResults() {
  discoveryResult.value = null
  testResult.value = null
  discoveryError.value = ''
  testError.value = ''
}

function loadDraft() {
  if (!currentModel.value) return
  Object.assign(draft, tenantModelDraft(currentModel.value))
  credentialInput.value = ''
  promptInput.value = 'hello'
  clearRequestResults()
}

function selectModel(id: string) {
  selectedId.value = id
  creating.value = false
  revisionConflict.value = false
  loadDraft()
}

function startCreate() {
  selectedId.value = ''
  creating.value = true
  revisionConflict.value = false
  credentialInput.value = ''
  promptInput.value = 'hello'
  Object.assign(draft, emptyTenantModelDraft())
  clearRequestResults()
}

function resetDraft() {
  credentialInput.value = ''
  if (creating.value) Object.assign(draft, emptyTenantModelDraft())
  else loadDraft()
  clearRequestResults()
}

function addHeader() {
  draft.custom_headers.push({ name: '', value: '' })
}

function removeHeader(index: number) {
  draft.custom_headers.splice(index, 1)
}

function requestConfig(): TenantModelConfig {
  return {
    ...draft,
    custom_headers: draft.custom_headers.map((header) => ({ ...header })),
    models_endpoint: draft.models_endpoint || null,
    inference_endpoint: draft.inference_endpoint || null,
  }
}

function draftRequest() {
  return {
    ...requestConfig(),
    model_id: creating.value ? undefined : currentModel.value?.id,
    api_key: credentialInput.value || undefined,
    timeout_seconds: 15,
  }
}

async function refetchModels() {
  await queryClient.invalidateQueries({ queryKey: ['tenant-models'] })
  const result = await modelsQuery.refetch()
  if (result.error) throw result.error
}

async function refreshModelDependents() {
  await Promise.all([
    queryClient.invalidateQueries({ queryKey: ['tenant-models'] }),
    queryClient.invalidateQueries({ queryKey: ['tenant-workers'] }),
    queryClient.invalidateQueries({ queryKey: ['tenant-worker-options'] }),
  ])
  const result = await modelsQuery.refetch()
  if (result.error) throw result.error
}

async function refreshModels() {
  refreshing.value = true
  try {
    await refetchModels()
    if (!creating.value) loadDraft()
  } catch (error) {
    uiStore.showToast(errorText(error), 'danger')
  } finally {
    refreshing.value = false
  }
}

async function handleMutationError(error: unknown) {
  if (isRevisionConflict(error)) {
    revisionConflict.value = true
    try {
      await refetchModels()
    } catch {
      /* 保留原始冲突提示。 */
    }
  }
  uiStore.showToast(errorText(error), 'danger')
}

async function discoverModels() {
  discovering.value = true
  discoveryError.value = ''
  discoveryResult.value = null
  try {
    discoveryResult.value = await discoverTenantModelDraft(draftRequest())
  } catch (error) {
    discoveryError.value = errorText(error)
  } finally {
    discovering.value = false
  }
}

async function testModel() {
  testing.value = true
  testError.value = ''
  testResult.value = null
  try {
    testResult.value = await testTenantModelDraft({ ...draftRequest(), prompt: promptInput.value })
  } catch (error) {
    testError.value = errorText(error)
  } finally {
    testing.value = false
  }
}

async function saveModel() {
  saving.value = true
  revisionConflict.value = false
  try {
    const config = requestConfig()
    const saved = creating.value
      ? await createTenantModel({ ...config, api_key: credentialInput.value || undefined })
      : await updateTenantModel(currentModel.value!.id, {
          ...config,
          expected_revision: currentModel.value!.revision,
          api_key: credentialInput.value || undefined,
        })
    credentialInput.value = ''
    await refreshModelDependents()
    selectedId.value = saved.id
    creating.value = false
    loadDraft()
    uiStore.showToast('Model 已保存', 'success')
  } catch (error) {
    await handleMutationError(error)
  } finally {
    credentialInput.value = ''
    saving.value = false
  }
}

function requestModelRemoval() {
  if (!currentModel.value || saving.value || credentialBusy.value) return
  pendingAction.value = {
    kind: 'delete-model',
    modelId: currentModel.value.id,
    modelName: currentModel.value.display_name || currentModel.value.id,
    revision: currentModel.value.revision,
  }
}

function requestCredentialRemoval() {
  if (
    !currentModel.value ||
    saving.value ||
    credentialBusy.value ||
    !currentModel.value.credential_configured
  )
    return
  pendingAction.value = {
    kind: 'delete-credential',
    modelId: currentModel.value.id,
    modelName: currentModel.value.display_name || currentModel.value.id,
    revision: currentModel.value.revision,
  }
}

function handlePendingActionOpenChange(open: boolean) {
  if (!open && pendingActionBusy.value) return
  if (!open) pendingAction.value = null
}

async function confirmPendingAction() {
  const action = pendingAction.value
  if (!action || pendingActionBusy.value) return
  if (action.kind === 'delete-credential') await removeCredential(action)
  else await removeModel(action)
}

async function removeModel(action: PendingModelAction) {
  saving.value = true
  revisionConflict.value = false
  try {
    await deleteTenantModel(action.modelId, action.revision)
    await refreshModelDependents()
    selectedId.value = selectAfterModelListChange(models.value, '')
    if (selectedId.value) loadDraft()
    uiStore.showToast('Model 已删除', 'success')
  } catch (error) {
    await handleMutationError(error)
  } finally {
    credentialInput.value = ''
    saving.value = false
    pendingAction.value = null
  }
}

async function removeCredential(action: PendingModelAction) {
  credentialBusy.value = true
  revisionConflict.value = false
  try {
    await deleteTenantModelCredential(action.modelId, action.revision)
    await refreshModelDependents()
    if (selectedId.value === action.modelId) loadDraft()
    uiStore.showToast('API Key 已删除', 'success')
  } catch (error) {
    await handleMutationError(error)
  } finally {
    credentialInput.value = ''
    credentialBusy.value = false
    pendingAction.value = null
  }
}

onBeforeUnmount(() => {
  credentialInput.value = ''
  pendingAction.value = null
})
</script>
