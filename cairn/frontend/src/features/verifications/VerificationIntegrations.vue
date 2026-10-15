<template>
  <section class="verification-theme verification-page" aria-label="漏洞复测配置">
    <header class="verification-heading"><h1>漏洞复测配置</h1><Button variant="secondary" @click="router.push({ name: 'Verifications' })">复测记录</Button></header>
    <div class="verification-config-target"><FormField v-if="tenants.length > 1" label="管理租户" for="verification-tenant" required><Select content-class="verification-theme verification-select" id="verification-tenant" :model-value="tenantId" :options="tenantOptions" :disabled="busy || configSaving || configLoading || loading" placeholder="选择管理范围" @update:model-value="selectTenant" /></FormField><p class="verification-muted">{{ tenantName ? `当前租户：${tenantName}。` : '' }}专用 Worker 不参与普通任务；内部复测不需要调用令牌。</p></div>
    <Alert v-if="tenantError" tone="danger" title="租户列表读取失败">{{ tenantError }}<template #actions><Button variant="secondary" @click="loadTenants">重新加载</Button></template></Alert>
    <div class="verification-tabs" role="tablist" aria-label="复测配置分区"><button id="verification-settings-tab" type="button" role="tab" :aria-selected="activeTab === 'execution'" aria-controls="verification-settings-panel" @click="activeTab = 'execution'">执行设置</button><button id="verification-integrations-tab" type="button" role="tab" :aria-selected="activeTab === 'integrations'" aria-controls="verification-integrations-panel" @click="activeTab = 'integrations'">外部调用令牌</button></div>
    <EmptyState v-if="!tenantId" title="请选择目标租户" description="每个租户只有一份复测执行配置，管理请求必须显式指定目标租户。" />
    <section v-else-if="activeTab === 'execution'" id="verification-settings-panel" role="tabpanel" aria-labelledby="verification-settings-tab" class="verification-reading verification-config-panel">
      <h2>复测专用 Worker</h2><p class="verification-muted">在此直接配置专用执行者与模型，无须项目组、普通 Worker 或模板。保存影响尚未开始的复测；已开始的执行不会热切换。</p><p class="verification-muted">普通任务原槽位不变。复测仅使用独立额外容量，每个 Dispatcher 最多同时执行 1 条；宿主机、网络与模型限额仍可能共享。</p>
      <Skeleton v-if="configLoading" />
      <Alert v-if="configError" tone="danger" title="执行配置未加载">{{ configError }}<template #actions><Button variant="secondary" :disabled="configLoading" @click="reloadConfig">重新读取</Button></template></Alert>
      <form v-if="configuration && options" class="verification-form" @submit.prevent="saveConfig">
        <FormField label="模型" for="verification-config-model" required><Select content-class="verification-theme verification-select" id="verification-config-model" v-model="configForm.model_id" :options="modelOptions" :disabled="configLoading || configSaving" placeholder="选择本租户已有模型" /><p v-if="!options.models.length" class="verification-muted">尚无模型，请先到现有模型管理中配置。</p><RouterLink :to="{ name: 'ConsoleModels' }">模型管理</RouterLink></FormField>
        <p v-if="selectedModel" class="verification-muted">{{ selectedModel.model_name }} · {{ selectedModel.protocol }} · {{ selectedModel.enabled ? '模型已启用' : '模型已停用' }} · {{ selectedModel.credential_configured ? '已配置凭据' : '尚未配置凭据' }}</p>
        <label class="verification-actions"><input v-model="configForm.enabled" type="checkbox" :disabled="configLoading || configSaving" />启用复测专用 Worker</label>
        <details class="verification-extra-settings"><summary>名称与执行参数</summary><div class="verification-stack">
          <FormField label="专用 Worker 名称" for="verification-config-name" required><Input id="verification-config-name" v-model="configForm.display_name" required :disabled="configLoading || configSaving" /></FormField>
          <FormField label="执行器" for="verification-config-engine"><Select content-class="verification-theme verification-select" id="verification-config-engine" v-model="configForm.engine" :options="engineOptions" :disabled="configLoading || configSaving" /></FormField>
          <div class="verification-grid"><FormField label="Worker 最大并发" for="verification-config-running"><Input id="verification-config-running" v-model="configForm.max_running" type="number" min="1" step="1" required :disabled="configLoading || configSaving" /></FormField><FormField label="调度优先级" for="verification-config-priority"><Input id="verification-config-priority" v-model="configForm.priority" type="number" step="1" required :disabled="configLoading || configSaving" /></FormField></div>
          <p class="verification-muted">最大并发仍受复测独立槽位约束，不占用或缩减普通任务配额。</p>
        </div></details>
        <div v-if="configuration.tenant_worker_id" class="verification-worker-summary"><strong>{{ configuration.display_name }}</strong><p class="verification-muted">{{ configuration.engine }} · {{ configuration.model_display_name }} · {{ configuration.model_name }} · 版本 {{ configuration.revision }} · {{ configuration.enabled ? '已启用' : '已停用' }}</p></div>
        <div class="verification-readiness"><Badge :tone="configuration.ready ? 'info' : 'warning'" size="sm">{{ configuration.ready ? '已保存配置就绪' : '已保存配置尚未就绪' }}</Badge><span>{{ configuration.message }}</span></div>
        <Alert v-if="saveError" tone="danger" title="保存结果未确认">{{ saveError }}<p>当前编辑值已保留；请重新核对已保存配置，不能视为保存成功。</p></Alert>
        <p v-if="saveMessage" role="status" class="verification-muted">{{ saveMessage }}</p>
        <div class="verification-actions"><Button type="submit" :loading="configSaving" :disabled="configLoading || Boolean(configError) || !configForm.model_id">保存执行设置</Button><Button variant="secondary" :disabled="configSaving || configLoading" @click="reloadConfig">重新读取已保存配置</Button><Button v-if="configuration.ready && tenantId === auth.session?.tenant_id" variant="secondary" @click="router.push({ name: 'NewVerification', query: { input: 'platform' } })">选择已有漏洞复测</Button><span class="verification-muted">{{ configuration.updated_at ? `最后保存 ${dateTime(configuration.updated_at)}` : '尚未保存执行配置' }}</span></div>
      </form>
    </section>
    <section v-else id="verification-integrations-panel" role="tabpanel" aria-labelledby="verification-integrations-tab" class="verification-reading">
      <header class="verification-heading"><h2>外部调用令牌</h2><Button :disabled="Boolean(secret) || busy || Boolean(unconfirmedCreate)" @click="openCreate">创建调用令牌</Button><Button variant="secondary" :loading="loading" @click="refresh">刷新</Button></header>
      <p class="verification-muted">仅外部系统调用需要令牌，内部选择漏洞复测不需要。令牌只管理调用身份与启停，使用统一专用 Worker；不同调用身份仍各自隔离资料与历史。</p>
      <Alert v-if="configuration && !configuration.ready" tone="warning" title="执行配置尚未就绪">{{ configuration.message }}。可以管理调用令牌，但暂不能提交执行。</Alert>
      <Alert v-if="secret" tone="warning" title="令牌仅本次显示，请立即安全保存">离开、刷新或关闭后无法再次取得；轮换会使旧令牌失效。不要放入 URL、日志、报告或源码。<p>{{ secret.name }}</p><pre class="verification-token">{{ secret.token }}</pre><template #actions><Button variant="secondary" @click="copyToken">复制令牌</Button><Button variant="secondary" @click="confirmHideSecret = true">已保存，关闭显示</Button></template><p v-if="copyMessage">{{ copyMessage }}</p></Alert>
      <Alert v-if="operationError" tone="warning" title="管理操作提示">{{ operationError }}</Alert>
      <Alert v-if="unconfirmedCreate" tone="warning" title="创建结果未确认">请求可能已受理，不会自动重复创建。请刷新核对；成功但未收到令牌时，可对该集成明确轮换。<template #actions><Button variant="secondary" :loading="loading" @click="refresh">核对列表</Button><Button variant="secondary" @click="confirmAbandon = true">放弃未确认表单</Button></template></Alert>
      <Skeleton v-if="loading && !loaded" aria-label="正在加载外部调用令牌" /><ErrorState v-else-if="loadError && !loaded" title="外部调用令牌加载失败" :description="loadError" @retry="refresh" /><Alert v-else-if="loadError" tone="warning" title="刷新失败">保留上次取得的列表。{{ loadError }}</Alert>
      <EmptyState v-if="loaded && !integrations.length" title="暂无外部调用令牌" description="内部复测不需要令牌；需要外部接入时填写调用名称，并安全保存一次性令牌。" />
      <div v-if="integrations.length" class="verification-scroll"><table class="verification-table"><thead><tr><th>集成</th><th>凭据状态</th><th>创建时间</th><th>操作</th></tr></thead><tbody><tr v-for="item in integrations" :key="item.id"><td>{{ item.name }}</td><td><Badge :tone="item.enabled ? 'info' : 'neutral'">{{ item.enabled ? '已启用' : '已停用' }}</Badge></td><td>{{ dateTime(item.created_at) }}</td><td><div class="verification-actions"><Button variant="secondary" size="sm" :disabled="busy || Boolean(secret)" @click="openEdit(item)">编辑</Button><Button variant="secondary" size="sm" :disabled="busy || Boolean(secret)" @click="action = { type: 'rotate', integration: item }">轮换令牌</Button><Button v-if="item.enabled" variant="danger" size="sm" :disabled="busy" @click="action = { type: 'disable', integration: item }">停用</Button></div></td></tr></tbody></table></div>
      <details class="verification-section"><summary>接口接入示例</summary><p class="verification-muted">使用独立安全存储的令牌；同一次提交重试必须保留 request_id 和完整原输入，不传租户、Worker、模板、项目组、模型或 Prompt 覆盖。</p><pre class="verification-code">POST /api/v1/verification-requests
Authorization: Bearer &lt;集成令牌&gt;
Content-Type: application/json

{{ apiExample }}</pre></details>
    </section>
    <Dialog :open="editorOpen" :title="editingId ? '编辑外部调用身份' : '创建调用令牌'" description="只管理身份信息；执行者由所选租户的执行设置统一决定。" :dismissible="!busy" :close-on-outside="false" content-class="verification-theme verification-config-dialog" overlay-class="verification-create-overlay" @update:open="editorOpen = $event">
      <Alert v-if="editorError" tone="danger" title="集成未保存">{{ editorError }}</Alert><form id="verification-integration-form" class="verification-form" @submit.prevent="save"><FormField label="集成名称" for="integration-name" required><Input id="integration-name" v-model="form.name" required :disabled="busy" /></FormField><label v-if="editingId" class="verification-actions"><input v-model="form.enabled" type="checkbox" :disabled="busy" />启用该集成凭据</label></form>
      <template #footer><Button variant="secondary" :disabled="busy" @click="editorOpen = false">取消</Button><Button type="submit" form="verification-integration-form" :loading="busy" :disabled="Boolean(unconfirmedCreate)">{{ editingId ? '保存身份与启停' : '创建并显示一次性令牌' }}</Button></template>
    </Dialog>
    <ConfirmAction :open="Boolean(action)" :title="action?.type === 'disable' ? '停用集成凭据' : '轮换集成令牌'" :description="action?.type === 'disable' ? '阻止新的接口调用，不自动停止已受理任务；需要停止执行请到复测详情操作。' : '旧令牌将立即失效，新令牌仅显示一次；集成身份和历史保持不变。'" :confirm-label="action?.type === 'disable' ? '确认停用' : '轮换并显示新令牌'" :loading="busy" content-class="verification-theme verification-confirm" overlay-class="verification-confirm-overlay" @update:open="!$event && (action = null)" @confirm="performAction" />
    <ConfirmAction :open="confirmHideSecret" title="关闭一次性令牌显示" description="请确认已安全保存；关闭后无法重新查看，只能明确发起轮换。" confirm-label="已保存，关闭显示" tone="warning" content-class="verification-theme verification-confirm" overlay-class="verification-confirm-overlay" @update:open="confirmHideSecret = $event" @confirm="hideSecret" />
    <ConfirmAction :open="confirmAbandon" title="放弃未确认创建表单" description="这不撤销可能已受理的集成，也不代表创建失败。请先核对列表；已有集成可通过明确轮换取得新令牌。" confirm-label="放弃表单，不撤销集成" tone="warning" content-class="verification-theme verification-confirm" overlay-class="verification-confirm-overlay" @update:open="confirmAbandon = $event" @confirm="unconfirmedCreate = null; confirmAbandon = false" />
  </section>
</template>
<script setup lang="ts">
import { computed, onBeforeUnmount, reactive, ref, watch } from 'vue'
import { onBeforeRouteLeave, onBeforeRouteUpdate, useRoute, useRouter } from 'vue-router'
import { ApiError } from '@/api/client'
import { createVerificationIntegration, disableVerificationIntegration, getVerificationConfiguration, getVerificationConfigurationOptions, listVerificationIntegrations, listVerificationTenants, rotateVerificationIntegrationToken, saveVerificationConfiguration, updateVerificationIntegration, type VerificationConfiguration, type VerificationConfigurationOptions, type VerificationIntegration, type VerificationIntegrationInput, type VerificationTenant } from '@/api/verificationRequests'
import { Alert, Badge, Button, Dialog, EmptyState, FormField, Input, Select, Skeleton } from '@/components/ui'
import { ConfirmAction, ErrorState } from '@/components/patterns'
import { dateTime } from '@/lib/format'
import { useAuthStore } from '@/stores/auth'
import { errorMessage } from './verificationPresentation'
import './verifications.css'
const router = useRouter()
const route = useRoute()
const auth = useAuthStore()
const activeTab = ref('execution')
const tenants = ref<VerificationTenant[]>([])
const tenantError = ref('')
const tenantId = computed(() => {
  const id = typeof route.query.tenant_id === 'string' ? route.query.tenant_id : auth.session?.tenant_id
  return tenants.value.some((tenant) => tenant.id === id) ? id || '' : ''
})
const tenantName = computed(() => tenants.value.find((tenant) => tenant.id === tenantId.value)?.name)
const tenantOptions = computed(() => tenants.value.map((tenant) => ({ value: tenant.id, label: tenant.name })))
const configuration = ref<VerificationConfiguration | null>(null)
const options = ref<VerificationConfigurationOptions | null>(null)
const configForm = reactive({ model_id: '', display_name: '', engine: 'pi', max_running: 1 as number | null, priority: 100 as number | null, enabled: true })
const savedConfigForm = ref('')
const configLoading = ref(false)
const configSaving = ref(false)
const configError = ref('')
const saveError = ref('')
const saveMessage = ref('')
const configDirty = computed(() => Boolean(savedConfigForm.value) && JSON.stringify(configForm) !== savedConfigForm.value)
const modelOptions = computed(() => options.value?.models.map((model) => ({ value: model.id, label: `${model.display_name} · ${model.model_name}${!model.enabled ? ' · 已停用' : !model.credential_configured ? ' · 未配置凭据' : ''}` })) || [])
const engineOptions = computed(() => options.value?.engines.map((engine) => ({ value: engine.id, label: engine.name })) || [])
const selectedModel = computed(() => options.value?.models.find((model) => model.id === configForm.model_id))
function applySavedConfig(saved: VerificationConfiguration, choices: VerificationConfigurationOptions) {
  Object.assign(configForm, {
    model_id: saved.model_id || '', display_name: saved.display_name ?? choices.defaults.display_name,
    engine: saved.engine ?? choices.defaults.engine, max_running: saved.max_running ?? choices.defaults.max_running,
    priority: saved.priority ?? choices.defaults.priority, enabled: saved.enabled ?? choices.defaults.enabled,
  })
  savedConfigForm.value = JSON.stringify(configForm)
}
async function loadTenants() { try { tenants.value = await listVerificationTenants(); tenantError.value = '' } catch (error) { tenantError.value = errorMessage(error) } }
function selectTenant(value: string) { void router.push({ name: 'VerificationConfiguration', query: { tenant_id: value } }) }
let configGeneration = 0
async function loadConfig() {
  if (!tenantId.value || configLoading.value) return
  const generation = ++configGeneration
  configLoading.value = true; configError.value = ''; saveMessage.value = ''
  try {
    const [saved, choices] = await Promise.all([getVerificationConfiguration(tenantId.value), getVerificationConfigurationOptions(tenantId.value)])
    if (generation !== configGeneration) return
    configuration.value = saved; options.value = choices; saveError.value = ''
    applySavedConfig(saved, choices)
  } catch (error) { if (generation === configGeneration) configError.value = errorMessage(error) }
  finally { if (generation === configGeneration) configLoading.value = false }
}
function reloadConfig() { if (!configDirty.value || window.confirm('重新读取将放弃未保存的执行设置，继续吗？')) void loadConfig() }
async function saveConfig() {
  if (!tenantId.value || configSaving.value || configLoading.value) return
  if (!configForm.model_id || !configForm.display_name.trim()) { saveError.value = '请选择模型并填写专用 Worker 名称。'; return }
  if (!Number.isInteger(configForm.max_running) || Number(configForm.max_running) < 1 || !Number.isInteger(configForm.priority)) { saveError.value = '最大并发须为正整数，优先级须为整数。'; return }
  configSaving.value = true; saveError.value = ''; saveMessage.value = ''
  try {
    configuration.value = await saveVerificationConfiguration(tenantId.value, { ...configForm, engine: 'pi', max_running: Number(configForm.max_running), priority: Number(configForm.priority) })
    applySavedConfig(configuration.value, options.value!)
    saveMessage.value = '专用执行设置已保存，尚未开始的复测将在开始时使用该配置；已开始的执行保持不变。'
  } catch (error) { saveError.value = errorMessage(error) }
  finally { configSaving.value = false }
}
const integrations = ref<VerificationIntegration[]>([])
const loaded = ref(false)
const loading = ref(false)
const loadError = ref('')
const operationError = ref('')
const editorOpen = ref(false)
const editingId = ref('')
const form = reactive({ name: '', enabled: true })
const editorError = ref('')
const busy = ref(false)
const unconfirmedCreate = ref<VerificationIntegrationInput | null>(null)
const action = ref<{ type: 'rotate' | 'disable'; integration: VerificationIntegration } | null>(null)
// 明文令牌只在当前组件内存中显示，不进入缓存、日志或浏览器存储。
const secret = ref<{ name: string; token: string } | null>(null)
const copyMessage = ref('')
const confirmHideSecret = ref(false)
const confirmAbandon = ref(false)
let integrationGeneration = 0
async function refresh() {
  if (!tenantId.value || loading.value) return
  const generation = ++integrationGeneration
  loading.value = true
  try { const items = await listVerificationIntegrations(tenantId.value); if (generation === integrationGeneration) { integrations.value = items; loaded.value = true; loadError.value = '' } }
  catch (error) { if (generation === integrationGeneration) loadError.value = errorMessage(error) }
  finally { if (generation === integrationGeneration) loading.value = false }
}
function openCreate() { editingId.value = ''; form.name = ''; form.enabled = true; editorError.value = ''; editorOpen.value = true }
function openEdit(item: VerificationIntegration) { editingId.value = item.id; form.name = item.name; form.enabled = item.enabled; editorError.value = ''; editorOpen.value = true }
function replaceIntegration(item: VerificationIntegration) { integrations.value = [...integrations.value.filter((entry) => entry.id !== item.id), item]; loaded.value = true }
async function save() {
  if (!tenantId.value || busy.value || unconfirmedCreate.value) return
  if (!form.name.trim()) { editorError.value = '请填写集成名称。'; return }
  const payload: VerificationIntegrationInput = { name: form.name }
  busy.value = true; editorError.value = ''; operationError.value = ''
  try {
    if (editingId.value) replaceIntegration(await updateVerificationIntegration(tenantId.value, editingId.value, { ...payload, enabled: form.enabled }))
    else { const response = await createVerificationIntegration(tenantId.value, payload); replaceIntegration(response.integration); secret.value = { name: response.integration.name, token: response.token }; copyMessage.value = '' }
    editorOpen.value = false
  } catch (error) {
    editorError.value = errorMessage(error)
    const rejected = error instanceof ApiError && [400, 401, 403, 404, 409, 422].includes(error.status)
    if (!editingId.value && !rejected) { unconfirmedCreate.value = payload; editorOpen.value = false; operationError.value = '创建结果未确认，不会自动重复创建，请核对列表。' }
  } finally { busy.value = false }
}
async function performAction() {
  if (!tenantId.value || !action.value || busy.value || (action.value.type === 'rotate' && secret.value)) return
  const current = action.value
  busy.value = true; operationError.value = ''
  try {
    if (current.type === 'disable') replaceIntegration(await disableVerificationIntegration(tenantId.value, current.integration.id))
    else { const response = await rotateVerificationIntegrationToken(tenantId.value, current.integration.id); replaceIntegration(response.integration); secret.value = { name: response.integration.name, token: response.token }; copyMessage.value = '' }
  } catch (error) { operationError.value = current.type === 'rotate' ? `轮换结果未确认，旧令牌可能已失效，新令牌尚未取得。不会自动轮换；核对后可明确再次轮换。${errorMessage(error)}` : `停用结果未确认，请刷新核对。${errorMessage(error)}` }
  finally { busy.value = false; action.value = null }
}
async function copyToken() { if (!secret.value) return; try { await navigator.clipboard.writeText(secret.value.token); copyMessage.value = '已复制，请粘贴到安全的凭据存储。' } catch { copyMessage.value = '浏览器无法自动复制，请手工选中并安全保存上方令牌。' } }
function hideSecret() { secret.value = null; copyMessage.value = ''; confirmHideSecret.value = false }
function canLeave() {
  if (busy.value || configSaving.value) return false
  if (secret.value && !window.confirm('令牌仅显示一次。请确认已安全保存；离开后无法重新查看，继续吗？')) return false
  if (unconfirmedCreate.value && !window.confirm('集成创建结果未确认，离开不撤销可能已创建的身份。请先核对列表，仍要离开吗？')) return false
  return !configDirty.value || window.confirm('执行设置尚未保存，离开将丢失编辑值。继续吗？')
}
onBeforeRouteLeave(canLeave)
onBeforeRouteUpdate((to) => to.query.tenant_id === route.query.tenant_id || canLeave())
function beforeUnload(event: BeforeUnloadEvent) { if (!secret.value && !unconfirmedCreate.value && !configDirty.value && !busy.value && !configSaving.value) return; event.preventDefault(); event.returnValue = '' }
window.addEventListener('beforeunload', beforeUnload)
onBeforeUnmount(() => { configGeneration++; integrationGeneration++; hideSecret(); window.removeEventListener('beforeunload', beforeUnload) })
watch(tenantId, () => {
  configGeneration++; integrationGeneration++; configLoading.value = false; loading.value = false
  configuration.value = null; options.value = null; savedConfigForm.value = ''; integrations.value = []; loaded.value = false
  configError.value = ''; saveError.value = ''; saveMessage.value = ''; loadError.value = ''; operationError.value = ''; unconfirmedCreate.value = null; editorOpen.value = false; action.value = null; hideSecret()
  if (tenantId.value) { void loadConfig(); if (activeTab.value === 'integrations') void refresh() }
}, { immediate: true })
watch(activeTab, (tab) => { if (tab === 'integrations' && tenantId.value && !loaded.value) void refresh() })
const apiExample = JSON.stringify({ schema_version: 1, request_id: '<调用方生成并保留的唯一 ID>', external_id: 'ISSUE-123', mode: 'retest', title: '授权环境修复后复测', origin: '<当前目标、身份、授权范围与排除项>', report: { type: 'markdown', content: '<原始漏洞资料>' }, authorization_expires_at: null }, null, 2)
void loadTenants()
</script>
