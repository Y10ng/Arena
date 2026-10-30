<template>
  <section id="models" class="tenant-models-panel" aria-labelledby="models-heading">
    <div class="section-header">
      <h3 id="models-heading">模型</h3>
      <IconButton label="刷新模型" :loading="refreshing" @click="refreshModels"
        ><RefreshCw :size="16"
      /></IconButton>
    </div>
    <ErrorState
      v-if="modelsQuery.error.value"
      title="模型加载失败"
      :description="errorText(modelsQuery.error.value)"
      @retry="refreshModels"
    />
    <Skeleton v-if="modelsQuery.isPending.value" label="正在加载模型" />
    <div v-else-if="models.length" class="model-table-container">
      <table class="model-table">
        <thead>
          <tr>
            <th scope="col">名称</th>
            <th scope="col">协议 / 模型 ID</th>
            <th scope="col">凭据</th>
            <th scope="col">状态</th>
            <th scope="col">操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="model in models" :key="model.id">
            <th scope="row">{{ model.display_name }}</th>
            <td>
              <span>{{ protocolLabel(model.protocol) }}</span>
              <div class="mono model-id">{{ model.model_name }}</div>
            </td>
            <td>{{ model.credential_configured ? '已配置' : '未配置' }}</td>
            <td>
              <StatusBadge :status="model.enabled ? 'healthy' : 'stopped'" size="sm">{{
                model.enabled ? '已启用' : '已停用'
              }}</StatusBadge>
            </td>
            <td>
              <div class="inline-actions">
                <Button variant="secondary" size="sm" :disabled="busy" @click="editModel(model)"
                  >编辑</Button
                ><Button
                  variant="ghost"
                  size="sm"
                  :disabled="busy"
                  @click="requestRemoval(model, 'delete-model')"
                  >删除</Button
                >
              </div>
            </td>
          </tr>
        </tbody>
      </table>
    </div>
    <EmptyState v-else-if="!modelsQuery.error.value" title="尚未配置模型"
      ><Button variant="secondary" @click="startCreate">新增模型</Button></EmptyState
    >
    <Alert v-if="listError" tone="danger">{{ listError }}</Alert>

    <Dialog
      :open="open"
      :title="editing ? '编辑模型' : '新增模型'"
      description=""
      size="lg"
      content-class="tenant-model-dialog"
      :dismissible="!busy"
      @update:open="handleOpenChange"
      @close-auto-focus="restoreFocus"
    >
      <form id="tenant-model-form" class="model-form" @submit.prevent="saveModel">
        <fieldset :disabled="busy || mutationUnknown" class="model-fields">
          <div class="model-form-grid">
            <FormField v-slot="{ controlAttrs }" label="名称" required
              ><Input v-bind="controlAttrs" v-model="draft.display_name" maxlength="128"
            /></FormField>
            <FormField v-slot="{ controlAttrs }" label="协议格式"
              ><Select
                v-bind="controlAttrs"
                :model-value="draft.protocol"
                :options="protocolOptions"
                aria-label="模型协议格式"
                @update:model-value="updateProtocol"
            /></FormField>
          </div>
          <FormField v-slot="{ controlAttrs }" label="Base URL" required
            ><Input
              v-bind="controlAttrs"
              v-model="draft.base_url"
              type="url"
              class="mono"
              placeholder="https://api.example.com"
          /></FormField>
          <div class="model-discovery-row">
            <FormField v-slot="{ controlAttrs }" label="模型 ID" required
              ><Input v-bind="controlAttrs" v-model="draft.model_name" class="mono"
            /></FormField>
            <Button
              variant="secondary"
              :loading="discovering"
              :disabled="!canRequest || discovering"
              @click="discoverModels"
              ><template #icon><Search :size="14" /></template>获取模型列表</Button
            >
          </div>
          <FormField
            v-if="discoveryResult?.model_names.length"
            v-slot="{ controlAttrs }"
            label="获取结果"
            ><Select
              v-bind="controlAttrs"
              v-model="draft.model_name"
              :options="discoveryOptions"
              aria-label="模型 ID 获取结果"
          /></FormField>
          <p v-if="discoveryError" class="field-error" role="alert">
            获取失败：{{ discoveryError }}
          </p>
          <div class="model-form-grid">
            <FormField v-slot="{ controlAttrs }" label="API Key"
              ><Input
                v-bind="controlAttrs"
                v-model="credentialInput"
                type="password"
                autocomplete="new-password"
                :placeholder="
                  editing?.credential_configured ? '留空保留已保存 Key' : '可留空保存配置'
                "
            /></FormField>
            <div class="model-enabled"><Checkbox v-model="draft.enabled" label="启用模型" /></div>
          </div>
          <details class="model-advanced" :open="Boolean(headerError)">
            <summary>高级设置</summary>
            <div class="model-advanced-body">
              <FormField v-slot="{ controlAttrs }" label="认证字段"
                ><Select
                  v-bind="controlAttrs"
                  :model-value="draft.auth_header"
                  :options="authHeaderOptions"
                  aria-label="模型认证字段"
                  @update:model-value="updateAuthHeader"
              /></FormField>
              <Button
                v-if="editing?.credential_configured"
                variant="danger"
                size="sm"
                @click="requestRemoval(editing, 'delete-credential')"
                >删除已保存 Key</Button
              >
              <div class="section-header">
                <h4>自定义 Headers</h4>
                <Button
                  variant="secondary"
                  size="sm"
                  :disabled="draft.custom_headers.length >= 32"
                  @click="draft.custom_headers.push({ name: '', value: '' })"
                  >新增 Header</Button
                >
              </div>
              <p class="section-desc">名称和值将明文保存并完整回显，仅在受控内部环境中使用。</p>
              <div
                v-for="(header, index) in draft.custom_headers"
                :key="index"
                class="model-header-row"
              >
                <Input
                  v-model="header.name"
                  :aria-label="`Header ${index + 1} 名称`"
                  placeholder="Header 名称"
                  class="mono"
                />
                <Input
                  v-model="header.value"
                  :aria-label="`Header ${index + 1} 值`"
                  placeholder="Header 值"
                  class="mono"
                />
                <Button variant="ghost" size="sm" @click="draft.custom_headers.splice(index, 1)"
                  >删除</Button
                >
              </div>
              <p v-if="headerError" class="field-error" role="alert">{{ headerError }}</p>
              <div class="model-form-grid">
                <FormField v-slot="{ controlAttrs }" label="Models endpoint"
                  ><Input
                    v-bind="controlAttrs"
                    v-model="draft.models_endpoint"
                    :placeholder="defaults.models"
                    class="mono"
                /></FormField>
                <FormField v-slot="{ controlAttrs }" label="调用 endpoint"
                  ><Input
                    v-bind="controlAttrs"
                    v-model="draft.inference_endpoint"
                    :placeholder="defaults.inference"
                    class="mono"
                /></FormField>
              </div>
            </div>
          </details>
          <details class="model-advanced">
            <summary>连接测试（可选）</summary>
            <div class="model-advanced-body">
              <p class="section-desc">发送真实模型请求，可能产生费用。保存配置不会自动测试。</p>
              <FormField v-slot="{ controlAttrs }" label="测试内容"
                ><Textarea v-bind="controlAttrs" v-model="promptInput" :rows="3" maxlength="16384"
              /></FormField>
              <Button
                variant="secondary"
                :loading="testing"
                :disabled="!canTest || testing"
                @click="testModel"
                ><template #icon><Activity :size="14" /></template>发送测试</Button
              >
              <p v-if="requestDisabledReason" class="section-desc">{{ requestDisabledReason }}</p>
              <div v-if="testResult" class="model-test-result">
                <p>
                  {{ testResult.success ? '请求成功' : '上游返回失败状态' }} · HTTP
                  {{ testResult.http_status }} · {{ testResult.latency_ms }} ms
                </p>
                <pre>{{ testResult.response_body }}</pre>
              </div>
              <p v-if="testError" class="field-error" role="alert">测试失败：{{ testError }}</p>
            </div>
          </details>
        </fieldset>
        <Alert v-if="revisionConflict" tone="warning"
          >模型已被其他会话修改，草稿已保留。先读取最新版本并核对，再决定是否覆盖。<Button
            variant="secondary"
            size="sm"
            :loading="refreshing"
            @click="reviewRevision"
            >读取最新版本</Button
          ><template v-if="reviewedModel"
            ><p>
              服务端当前：{{ reviewedModel.display_name }} · {{ reviewedModel.model_name }} · 修订
              {{ reviewedModel.revision }} · {{ reviewedModel.enabled ? '已启用' : '已停用' }}
            </p>
            <Button variant="secondary" size="sm" @click="acceptRevision"
              >已核对，保留草稿继续编辑</Button
            ></template
          ></Alert
        >
        <Alert v-if="mutationUnknown" tone="warning"
          ><p>操作结果未确认，不会自动重试。请重新读取并核对模型清单，避免重复新增或覆盖。</p>
          <Button variant="secondary" size="sm" :loading="refreshing" @click="reviewUnknown"
            >重新读取模型清单</Button
          ><template v-if="unknownReviewed"
            ><ul>
              <li v-for="model in models" :key="model.id">
                {{ model.display_name }} · {{ model.model_name }} · {{ model.id }} · 修订
                {{ model.revision }}
                <Button variant="secondary" size="sm" @click="useReviewedModel(model)"
                  >编辑此模型</Button
                >
              </li>
            </ul>
            <Button variant="secondary" size="sm" @click="acknowledgeUnknown"
              >已核对，保留草稿继续编辑</Button
            ></template
          ></Alert
        >
        <p v-if="saveError" class="field-error" role="alert">{{ saveError }}</p>
      </form>
      <template #footer
        ><Button variant="secondary" :disabled="busy" @click="requestLeave">取消</Button
        ><Button type="submit" form="tenant-model-form" :loading="saving" :disabled="!canSave"
          >保存模型</Button
        ></template
      >
    </Dialog>
    <ConfirmAction
      :open="discardOpen"
      title="放弃模型修改？"
      description="未保存的输入将被清除。"
      confirm-label="放弃修改"
      cancel-label="继续编辑"
      tone="warning"
      @update:open="handleDiscardOpen"
      @confirm="finishDiscard(true)"
    />
    <ConfirmAction
      :open="Boolean(pendingAction)"
      :title="pendingAction?.kind === 'delete-credential' ? '删除 API Key' : '删除模型'"
      :description="pendingDescription"
      :confirm-label="pendingAction?.kind === 'delete-credential' ? '确认删除 Key' : '确认删除模型'"
      :loading="busy"
      @update:open="handlePendingOpen"
      @confirm="confirmRemoval"
    />
  </section>
</template>

<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, reactive, ref, watch } from 'vue'
import { useQuery, useQueryClient } from '@tanstack/vue-query'
import { Activity, RefreshCw, Search } from '@lucide/vue'
import {
  ApiError,
  createTenantModel,
  deleteTenantModel,
  deleteTenantModelCredential,
  discoverTenantModelDraft,
  isRevisionConflict,
  listTenantModels,
  testTenantModelDraft,
  updateTenantModel,
} from '@/api/client'
import type { ModelProtocol, TenantModel, TenantModelConfig } from '@/api/types'
import { ConfirmAction, ErrorState } from '@/components/patterns'
import {
  Alert,
  Button,
  Checkbox,
  Dialog,
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
  tenantModelDraft,
  tenantModelDraftIsValid,
} from './tenantModelsModel'
import './tenantModelsPanel.css'

const emit = defineEmits<{ saved: [model: TenantModel] }>()
const protocolOptions = [
  { value: 'anthropic-messages', label: 'Anthropic Messages' },
  { value: 'openai-chat-completions', label: 'OpenAI Chat Completions' },
  { value: 'openai-responses', label: 'OpenAI Responses API' },
]
const authHeaderOptions = [
  { value: 'authorization-bearer', label: 'Authorization: Bearer key' },
  { value: 'x-api-key', label: 'x-api-key: key' },
]
type Removal = { model: TenantModel; kind: 'delete-model' | 'delete-credential' }
const uiStore = useUIStore()
const queryClient = useQueryClient()
const modelsQuery = useQuery({ queryKey: ['tenant-models'], queryFn: listTenantModels })
const models = computed(() => modelsQuery.data.value || [])
const open = ref(false)
const editing = ref<TenantModel | null>(null)
const draft = reactive(emptyTenantModelDraft())
const baseline = ref('')
const credentialInput = ref('')
const promptInput = ref('hello')
const saving = ref(false)
const removing = ref(false)
const refreshing = ref(false)
const busy = computed(() => saving.value || removing.value)
const discovering = ref(false)
const testing = ref(false)
const discoveryResult = ref<Awaited<ReturnType<typeof discoverTenantModelDraft>> | null>(null)
const testResult = ref<Awaited<ReturnType<typeof testTenantModelDraft>> | null>(null)
const discoveryError = ref('')
const testError = ref('')
const saveError = ref('')
const listError = ref('')
const revisionConflict = ref(false)
const reviewedModel = ref<TenantModel | null>(null)
const mutationUnknown = ref(false)
const unknownReviewed = ref(false)
const pendingAction = ref<Removal | null>(null)
const discardOpen = ref(false)
let resolveDiscard: ((value: boolean) => void) | null = null
let returnFocus: HTMLElement | null = null
let requestGeneration = 0
let disposed = false
const dirty = computed(
  () =>
    open.value &&
    (mutationUnknown.value ||
      JSON.stringify(draft) !== baseline.value ||
      Boolean(credentialInput.value)),
)
const headerError = computed(() => customHeaderError(draft))
const defaults = computed(() => protocolDefaultEndpoints(draft.protocol))
const canRequest = computed(
  () =>
    !busy.value &&
    !mutationUnknown.value &&
    Boolean(
      draft.base_url.trim() &&
      (credentialInput.value || editing.value?.credential_configured) &&
      !headerError.value,
    ),
)
const canTest = computed(
  () => canRequest.value && Boolean(draft.model_name.trim() && promptInput.value),
)
const canSave = computed(
  () =>
    open.value &&
    !busy.value &&
    !revisionConflict.value &&
    !mutationUnknown.value &&
    tenantModelDraftIsValid(draft),
)
const discoveryOptions = computed(() =>
  (discoveryResult.value?.model_names || []).map((name) => ({ value: name, label: name })),
)
const requestDisabledReason = computed(() =>
  !draft.base_url.trim()
    ? '填写 Base URL 后可发送请求。'
    : !(credentialInput.value || editing.value?.credential_configured)
      ? '配置 API Key 后可发送请求。'
      : headerError.value,
)
const pendingDescription = computed(() =>
  pendingAction.value?.kind === 'delete-credential'
    ? `删除模型「${pendingAction.value.model.display_name}」已保存的 API Key？该操作无法撤销。`
    : `删除模型「${pendingAction.value?.model.display_name || ''}」？该操作无法撤销，仍被引用的模型不能删除。`,
)

watch(
  () => [
    draft.protocol,
    draft.base_url,
    draft.auth_header,
    draft.models_endpoint,
    draft.inference_endpoint,
    draft.model_name,
    draft.custom_headers,
    credentialInput.value,
    promptInput.value,
  ],
  clearRequestResults,
  { deep: true, flush: 'sync' },
)
function errorText(error: unknown) {
  return error instanceof Error ? error.message : '请求失败，请重试。'
}
function protocolLabel(protocol: ModelProtocol) {
  return protocolOptions.find((option) => option.value === protocol)?.label
}
function updateProtocol(value: string) {
  if (
    value === 'anthropic-messages' ||
    value === 'openai-chat-completions' ||
    value === 'openai-responses'
  )
    draft.protocol = value
}
function updateAuthHeader(value: string) {
  if (value === 'authorization-bearer' || value === 'x-api-key') draft.auth_header = value
}
function clearRequestResults() {
  requestGeneration += 1
  discovering.value = false
  testing.value = false
  discoveryResult.value = null
  testResult.value = null
  discoveryError.value = ''
  testError.value = ''
}
let editGeneration = 0
function setDraft(model: TenantModel | null) {
  editGeneration += 1
  editing.value = model
    ? { ...model, custom_headers: model.custom_headers.map((header) => ({ ...header })) }
    : null
  Object.assign(draft, model ? tenantModelDraft(model) : emptyTenantModelDraft())
  baseline.value = JSON.stringify(draft)
  credentialInput.value = ''
  promptInput.value = 'hello'
  revisionConflict.value = false
  reviewedModel.value = null
  saveError.value = ''
  mutationUnknown.value = false
  unknownReviewed.value = false
  clearRequestResults()
}
async function startCreate() {
  const trigger = document.activeElement instanceof HTMLElement ? document.activeElement : null
  if (!(await requestLeave())) return
  returnFocus = trigger
  setDraft(null)
  open.value = true
}
async function editModel(model: TenantModel) {
  const trigger = document.activeElement instanceof HTMLElement ? document.activeElement : null
  if (!(await requestLeave())) return
  returnFocus = trigger
  setDraft(model)
  open.value = true
}
function closeEditor() {
  open.value = false
  setDraft(null)
}
function requestLeave(): Promise<boolean> {
  if (busy.value) return Promise.resolve(false)
  if (mutationUnknown.value) {
    saveError.value = '请先读取并核对未确认操作的结果。'
    return Promise.resolve(false)
  }
  if (!dirty.value) {
    closeEditor()
    return Promise.resolve(true)
  }
  if (resolveDiscard) return Promise.resolve(false)
  discardOpen.value = true
  return new Promise((resolve) => {
    resolveDiscard = resolve
  })
}
function finishDiscard(confirmed: boolean) {
  const resolve = resolveDiscard
  resolveDiscard = null
  discardOpen.value = false
  if (confirmed) closeEditor()
  resolve?.(confirmed)
}
function handleDiscardOpen(value: boolean) {
  if (!value) finishDiscard(false)
}
function handleOpenChange(value: boolean) {
  if (!value) void requestLeave()
}
function restoreFocus(event: Event) {
  if (returnFocus?.isConnected) {
    event.preventDefault()
    void nextTick(() => returnFocus?.focus())
  }
}
function requestConfig(): TenantModelConfig {
  return {
    ...draft,
    display_name: draft.display_name.trim(),
    base_url: draft.base_url.trim(),
    model_name: draft.model_name.trim(),
    custom_headers: draft.custom_headers.map((header) => ({ ...header })),
    models_endpoint: draft.models_endpoint || null,
    inference_endpoint: draft.inference_endpoint || null,
  }
}
function draftRequest() {
  return {
    ...requestConfig(),
    model_id: editing.value?.id,
    api_key: credentialInput.value || undefined,
    timeout_seconds: 15,
  }
}
async function discoverModels() {
  if (!canRequest.value || discovering.value) return
  const generation = requestGeneration
  discovering.value = true
  discoveryError.value = ''
  try {
    const result = await discoverTenantModelDraft(draftRequest())
    if (!disposed && generation === requestGeneration) discoveryResult.value = result
  } catch (error) {
    if (!disposed && generation === requestGeneration) discoveryError.value = errorText(error)
  } finally {
    if (generation === requestGeneration) discovering.value = false
  }
}
async function testModel() {
  if (!canTest.value || testing.value) return
  const generation = requestGeneration
  testing.value = true
  testError.value = ''
  try {
    const result = await testTenantModelDraft({ ...draftRequest(), prompt: promptInput.value })
    if (!disposed && generation === requestGeneration) testResult.value = result
  } catch (error) {
    if (!disposed && generation === requestGeneration) testError.value = errorText(error)
  } finally {
    if (generation === requestGeneration) testing.value = false
  }
}
async function refreshModels() {
  refreshing.value = true
  listError.value = ''
  try {
    const result = await modelsQuery.refetch()
    if (result.error) throw result.error
    return result.data || []
  } catch (error) {
    listError.value = errorText(error)
    return null
  } finally {
    refreshing.value = false
  }
}
function updateCache(saved: TenantModel) {
  queryClient.setQueryData<TenantModel[]>(['tenant-models'], (items = []) =>
    items.some((item) => item.id === saved.id)
      ? items.map((item) => (item.id === saved.id ? saved : item))
      : [...items, saved],
  )
  void queryClient.invalidateQueries({ queryKey: ['tenant-workers'] })
  void queryClient.invalidateQueries({ queryKey: ['tenant-worker-options'] })
}
function mutationFailed(error: unknown) {
  if (isRevisionConflict(error)) {
    revisionConflict.value = true
    reviewedModel.value = null
    saveError.value = '保存冲突，尚未覆盖服务端配置。'
  } else if (!(error instanceof ApiError) || error.status >= 500) {
    mutationUnknown.value = true
    unknownReviewed.value = false
    saveError.value = ''
  } else saveError.value = errorText(error)
}
async function saveModel() {
  if (!canSave.value) return
  saving.value = true
  saveError.value = ''
  try {
    const config = requestConfig()
    const saved = editing.value
      ? await updateTenantModel(editing.value.id, {
          ...config,
          expected_revision: editing.value.revision,
          api_key: credentialInput.value || undefined,
        })
      : await createTenantModel({ ...config, api_key: credentialInput.value || undefined })
    if (disposed) return
    updateCache(saved)
    emit('saved', saved)
    closeEditor()
    uiStore.showToast('模型已保存', 'success')
  } catch (error) {
    if (!disposed) mutationFailed(error)
  } finally {
    saving.value = false
  }
}
async function reviewRevision() {
  const generation = editGeneration
  const items = await refreshModels()
  if (disposed || generation !== editGeneration) return
  if (!items) {
    saveError.value = '读取最新版本失败，草稿已保留。'
    return
  }
  reviewedModel.value = items.find((item) => item.id === editing.value?.id) || null
  if (!reviewedModel.value) saveError.value = '该模型已不存在，请关闭后核对模型清单。'
}
function acceptRevision() {
  if (!reviewedModel.value || !editing.value) return
  editing.value = reviewedModel.value
  reviewedModel.value = null
  revisionConflict.value = false
  saveError.value = ''
}
async function reviewUnknown() {
  const generation = editGeneration
  const items = await refreshModels()
  if (!disposed && generation === editGeneration) {
    unknownReviewed.value = Boolean(items)
    if (!items) saveError.value = '读取失败，操作结果仍未确认。'
  }
}
function acknowledgeUnknown() {
  if (!unknownReviewed.value) return
  if (editing.value) {
    const current = models.value.find((item) => item.id === editing.value?.id)
    if (!current) {
      closeEditor()
      return
    }
    editing.value = current
  }
  mutationUnknown.value = false
  unknownReviewed.value = false
  saveError.value = ''
}
function useReviewedModel(model: TenantModel) {
  if (unknownReviewed.value) setDraft(model)
}
async function requestRemoval(model: TenantModel, kind: Removal['kind']) {
  if (busy.value || mutationUnknown.value) return
  if (kind === 'delete-model' && !(await requestLeave())) return
  pendingAction.value = { kind, model: { ...model } }
}
function handlePendingOpen(value: boolean) {
  if (!value && !busy.value) pendingAction.value = null
}
async function confirmRemoval() {
  const action = pendingAction.value
  if (!action || busy.value) return
  removing.value = true
  try {
    if (action.kind === 'delete-model') {
      await deleteTenantModel(action.model.id, action.model.revision)
      if (disposed) return
      queryClient.setQueryData<TenantModel[]>(['tenant-models'], (items = []) =>
        items.filter((item) => item.id !== action.model.id),
      )
      uiStore.showToast('模型已删除', 'success')
    } else {
      const saved = await deleteTenantModelCredential(action.model.id, action.model.revision)
      if (disposed) return
      updateCache(saved)
      // 删除 Key 只更新凭据版本，不清掉同一弹窗中其他未保存字段。
      editing.value = saved
      credentialInput.value = ''
      uiStore.showToast('API Key 已删除', 'success')
    }
  } catch (error) {
    if (disposed) return
    if (open.value) mutationFailed(error)
    else
      listError.value = isRevisionConflict(error)
        ? '模型已被修改，请刷新清单后重试删除。'
        : error instanceof ApiError && error.status < 500
          ? errorText(error)
          : '未确认删除成功，请刷新模型清单核对。'
  } finally {
    removing.value = false
    pendingAction.value = null
  }
}
onBeforeUnmount(() => {
  disposed = true
  clearRequestResults()
  credentialInput.value = ''
  pendingAction.value = null
  resolveDiscard?.(false)
})
defineExpose({ startCreate, requestLeave, dirty, busy })
</script>
