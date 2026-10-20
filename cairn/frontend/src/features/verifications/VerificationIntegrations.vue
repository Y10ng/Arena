<template>
  <section class="verification-page verification-config-page" aria-label="漏洞复测配置">
    <header class="page-header config-heading">
      <h1>漏洞复测配置</h1>
      <Button variant="secondary" @click="router.push({ name: 'Verifications' })">复测记录</Button>
    </header>
    <FormField v-if="auth.isSuperAdmin && tenants.length > 1" class="config-tenant" label="管理租户" for="verification-tenant" required>
      <Select id="verification-tenant" :model-value="tenantId" :options="tenantOptions" :disabled="busy || configSaving || configLoading || loading" placeholder="选择管理范围" @update:model-value="selectTenant" />
    </FormField>
    <Alert v-if="auth.isSuperAdmin && tenantError" tone="danger" title="租户列表读取失败">
      {{ tenantError }}
      <template #actions><Button variant="secondary" :disabled="tenantsLoading" @click="loadTenants">重新加载</Button></template>
    </Alert>
    <Alert v-if="scopeError" tone="danger" title="无法访问此配置">
      {{ scopeError }}
      <template v-if="!auth.isSuperAdmin && auth.session" #actions><Button variant="secondary" @click="router.push({ name: 'VerificationConfiguration' })">返回本人配置</Button></template>
    </Alert>
    <Skeleton v-else-if="tenantsLoading && !tenantId" aria-label="正在读取管理范围" />
    <EmptyState v-else-if="!tenantId" title="请选择目标租户" />
    <Tabs v-else v-model="activeTab" class="config-workspace" :options="configTabs">
      <template #panel="{ option }">
        <section v-if="option.value === 'execution'" class="config-panel" aria-label="执行配置">
          <Skeleton v-if="configLoading && !configuration" aria-label="正在加载执行配置" />
          <Alert v-if="configError" tone="danger" title="执行配置未加载">
            {{ configError }}
            <template #actions><Button variant="secondary" :disabled="configLoading" @click="reloadConfig">重新读取</Button></template>
          </Alert>
          <form v-if="configuration && options" class="config-form" @submit.prevent="saveConfig">
            <div class="config-main-fields">
              <div class="config-model">
                <div class="config-model-heading">
                  <label for="verification-config-model">模型 <span class="config-required" aria-hidden="true">*</span></label>
                  <RouterLink v-if="managingOwnModels" :to="{ name: 'ConsoleModels' }" class="config-model-link">个人模型管理</RouterLink>
                </div>
                <FormField for="verification-config-model" required>
                  <Select id="verification-config-model" v-model="configForm.model_id" :options="modelOptions" :disabled="configLoading || configSaving" placeholder="选择模型" required />
                </FormField>
              </div>
              <div class="config-secondary-fields">
                <FormField label="名称（可选）" for="verification-config-name">
                  <Input id="verification-config-name" v-model="configForm.display_name" :placeholder="options.defaults.display_name" :disabled="configLoading || configSaving" />
                </FormField>
                <Checkbox v-model="configForm.enabled" class="config-enabled" label="启用复测" :disabled="configLoading || configSaving" />
              </div>
            </div>
            <p v-if="!managingOwnModels" class="verification-muted">当前为代管范围；模型由该租户用户在个人模型管理中配置。</p>
            <p v-if="!options.models.length" class="config-problem">{{ managingOwnModels ? '尚无模型，请先在个人模型管理中配置。' : '该租户尚无模型，请由该租户用户先配置个人模型。' }}</p>
            <p v-else-if="modelProblem" class="config-problem" role="status">{{ modelProblem }}</p>
            <details class="config-advanced">
              <summary>高级设置</summary>
              <div class="config-advanced-fields">
                <FormField label="执行器" for="verification-config-engine">
                  <Select id="verification-config-engine" v-model="configForm.engine" :options="engineOptions" :disabled="configLoading || configSaving" />
                </FormField>
                <div class="config-parameter-grid">
                  <FormField label="最大并发" for="verification-config-running">
                    <Input id="verification-config-running" v-model="configForm.max_running" type="number" min="1" step="1" required :disabled="configLoading || configSaving" />
                  </FormField>
                  <FormField label="调度优先级" for="verification-config-priority">
                    <Input id="verification-config-priority" v-model="configForm.priority" type="number" step="1" required :disabled="configLoading || configSaving" />
                  </FormField>
                </div>
              </div>
            </details>
            <footer class="config-save">
              <div v-if="!saveError" class="config-state" role="status" aria-live="polite">
                <Badge :tone="configDirty ? 'neutral' : configuration.ready ? 'info' : 'warning'">{{ configStatus }}</Badge>
                <span v-if="!configuration.ready && !modelProblem">{{ configuration.message }}</span>
              </div>
              <Alert v-if="saveError" tone="danger" title="保存结果未确认">
                {{ saveError }}
                <p>编辑值已保留；请核对已保存配置后再重试。</p>
              </Alert>
              <div class="config-actions">
                <Button type="submit" :loading="configSaving" :disabled="configLoading || Boolean(configError) || !configForm.model_id">保存执行配置</Button>
                <Button variant="secondary" :disabled="configSaving || configLoading" @click="reloadConfig">重新读取已保存配置</Button>
              </div>
              <p>用于尚未开始的复测</p>
            </footer>
          </form>
        </section>
        <section v-else-if="canManageIntegrations" class="config-panel config-integrations" aria-label="外部调用">
          <header class="config-section-heading">
            <h2>调用令牌</h2>
            <div class="config-actions">
              <Button :disabled="Boolean(secret) || busy || Boolean(unconfirmedCreate)" @click="openCreate">创建调用令牌</Button>
              <Button variant="secondary" :loading="loading" :disabled="busy" @click="refresh">刷新</Button>
            </div>
          </header>
          <Alert v-if="configuration && !configuration.ready" tone="warning" title="执行配置尚不可用">{{ configuration.message }}</Alert>
          <Alert v-if="secret" tone="warning" title="令牌仅本次显示，请安全保存">
            离开或关闭后无法再次查看。不要放入 URL、日志、报告或源码。
            <p>{{ secret.name }}</p>
            <pre class="config-token">{{ secret.token }}</pre>
            <template #actions>
              <Button variant="secondary" @click="copyToken">复制令牌</Button>
              <Button variant="secondary" @click="confirmHideSecret = true">已保存，关闭显示</Button>
            </template>
            <p v-if="copyMessage">{{ copyMessage }}</p>
          </Alert>
          <Alert v-if="operationError" tone="warning" title="管理操作提示">{{ operationError }}</Alert>
          <Alert v-if="unconfirmedCreate" tone="warning" title="创建结果未确认">
            请求可能已受理，不会自动重复创建。请刷新核对；成功但未收到令牌时，可对该集成明确轮换。
            <template #actions>
              <Button variant="secondary" :loading="loading" @click="refresh">核对列表</Button>
              <Button variant="secondary" @click="confirmAbandon = true">放弃未确认表单</Button>
            </template>
          </Alert>
          <Skeleton v-if="loading && !loaded" aria-label="正在加载外部调用令牌" />
          <ErrorState v-else-if="loadError && !loaded" title="外部调用令牌加载失败" :description="loadError" @retry="refresh" />
          <Alert v-else-if="loadError" tone="warning" title="刷新失败">保留上次取得的列表。{{ loadError }}</Alert>
          <EmptyState v-if="loaded && !integrations.length" title="暂无调用令牌" />
          <div v-if="integrations.length" class="config-table-scroll" role="region" aria-label="外部调用令牌列表" tabindex="0">
            <table class="config-table">
              <thead><tr><th scope="col">集成</th><th scope="col">状态</th><th scope="col">创建时间</th><th scope="col">操作</th></tr></thead>
              <tbody>
                <tr v-for="item in integrations" :key="item.id">
                  <td>{{ item.name }}</td>
                  <td><Badge :tone="item.enabled ? 'info' : 'neutral'">{{ item.enabled ? '已启用' : '已停用' }}</Badge></td>
                  <td>{{ dateTime(item.created_at) }}</td>
                  <td>
                    <div class="config-actions">
                      <Button variant="secondary" size="sm" :disabled="busy || Boolean(secret)" @click="openEdit(item)">编辑</Button>
                      <Button variant="secondary" size="sm" :disabled="busy || Boolean(secret)" @click="action = { type: 'rotate', integration: item }">轮换令牌</Button>
                      <Button v-if="item.enabled" variant="danger" size="sm" :disabled="busy" @click="action = { type: 'disable', integration: item }">停用</Button>
                    </div>
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
          <details class="config-api">
            <summary>接口接入示例</summary>
            <p>同一次提交重试须保留 request_id 和完整原输入；令牌应保存在安全的凭据存储中。</p>
            <pre class="config-code">POST /api/v1/verification-requests
Authorization: Bearer &lt;集成令牌&gt;
Content-Type: application/json

{{ apiExample }}</pre>
          </details>
        </section>
      </template>
    </Tabs>
    <template v-if="canManageIntegrations">
    <Dialog :open="editorOpen" :title="editingId ? '编辑外部调用身份' : '创建调用令牌'" description="" :dismissible="!busy" :close-on-outside="false" @update:open="editorOpen = $event">
      <form id="verification-integration-form" class="config-integration-form" @submit.prevent="save">
        <Alert v-if="editorError" tone="danger" title="集成未保存">{{ editorError }}</Alert>
        <FormField label="集成名称" for="integration-name" required><Input id="integration-name" v-model="form.name" required :disabled="busy" /></FormField>
        <Checkbox v-if="editingId" v-model="form.enabled" label="启用该集成凭据" :disabled="busy" />
      </form>
      <template #footer>
        <Button variant="secondary" :disabled="busy" @click="editorOpen = false">取消</Button>
        <Button type="submit" form="verification-integration-form" :loading="busy" :disabled="Boolean(unconfirmedCreate)">{{ editingId ? '保存身份与启停' : '创建并显示一次性令牌' }}</Button>
      </template>
    </Dialog>
    <ConfirmAction :open="Boolean(action)" :title="action?.type === 'disable' ? '停用集成凭据' : '轮换集成令牌'" :description="action?.type === 'disable' ? '阻止新的接口调用，不自动停止已受理任务；需要停止执行请到复测详情操作。' : '旧令牌将立即失效，新令牌仅显示一次；集成身份和历史保持不变。'" :confirm-label="action?.type === 'disable' ? '确认停用' : '轮换并显示新令牌'" :loading="busy" @update:open="!$event && (action = null)" @confirm="performAction" />
    <ConfirmAction :open="confirmHideSecret" title="关闭一次性令牌显示" description="请确认已安全保存；关闭后无法重新查看，只能明确发起轮换。" confirm-label="已保存，关闭显示" tone="warning" @update:open="confirmHideSecret = $event" @confirm="hideSecret" />
    <ConfirmAction :open="confirmAbandon" title="放弃未确认创建表单" description="这不撤销可能已受理的集成，也不代表创建失败。请先核对列表；已有集成可通过明确轮换取得新令牌。" confirm-label="放弃表单，不撤销集成" tone="warning" @update:open="confirmAbandon = $event" @confirm="unconfirmedCreate = null; confirmAbandon = false" />
    </template>
  </section>
</template>
<script setup lang="ts">
import { computed, onBeforeUnmount, reactive, ref, watch } from 'vue'
import { onBeforeRouteLeave, onBeforeRouteUpdate, useRoute, useRouter } from 'vue-router'
import { ApiError } from '@/api/client'
import { createVerificationIntegration, disableVerificationIntegration, getVerificationConfiguration, getVerificationConfigurationOptions, listVerificationIntegrations, listVerificationTenants, rotateVerificationIntegrationToken, saveVerificationConfiguration, updateVerificationIntegration, type VerificationConfiguration, type VerificationConfigurationOptions, type VerificationIntegration, type VerificationIntegrationInput, type VerificationTenant } from '@/api/verificationRequests'
import { Alert, Badge, Button, Checkbox, Dialog, EmptyState, FormField, Input, Select, Skeleton, Tabs } from '@/components/ui'
import { ConfirmAction, ErrorState } from '@/components/patterns'
import { dateTime } from '@/lib/format'
import { useAuthStore } from '@/stores/auth'
import { errorMessage } from './verificationPresentation'
import './verifications.css'
const router = useRouter()
const route = useRoute()
const auth = useAuthStore()
const authIdentity = computed(() => JSON.stringify([auth.authGeneration, auth.session?.user_id, auth.session?.tenant_id, auth.session?.role, auth.isSuperAdmin]))
const scopeKey = computed(() => JSON.stringify([authIdentity.value, route.query.tenant_id]))
const activeTab = ref('execution')
const configTabs = computed(() => [
  { value: 'execution', label: '执行配置' },
  ...(auth.isSuperAdmin ? [{ value: 'integrations', label: '外部调用' }] : []),
])
const tenants = ref<VerificationTenant[]>([])
const tenantsLoaded = ref(false)
const tenantsLoading = ref(false)
const tenantError = ref('')
const scopeError = computed(() => {
  if (!auth.session?.tenant_id) return '请先登录后管理复测执行配置。'
  const requested = route.query.tenant_id
  if (requested !== undefined && (typeof requested !== 'string' || !requested)) return '租户范围参数无效，请重新选择配置入口。'
  if (!auth.isSuperAdmin && requested !== undefined && requested !== auth.session.tenant_id) return '无权访问其他用户的复测执行配置。可明确返回本人配置。'
  if (auth.isSuperAdmin && tenantsLoaded.value && !tenants.value.some((tenant) => tenant.id === (requested ?? auth.session?.tenant_id))) return '所选租户不存在或不可管理，请重新选择管理范围。'
  return ''
})
const tenantId = computed(() => {
  if (scopeError.value) return ''
  const id = typeof route.query.tenant_id === 'string' ? route.query.tenant_id : auth.session?.tenant_id
  return !auth.isSuperAdmin || tenants.value.some((tenant) => tenant.id === id) ? id || '' : ''
})
const managingOwnModels = computed(() => tenantId.value === auth.session?.tenant_id)
const canManageIntegrations = computed(() => auth.isSuperAdmin && Boolean(tenantId.value))
const tenantOptions = computed(() => tenants.value.map((tenant) => ({ value: tenant.id, label: tenant.name })))
const configuration = ref<VerificationConfiguration | null>(null)
const options = ref<VerificationConfigurationOptions | null>(null)
const configForm = reactive({ model_id: '', display_name: '', engine: 'pi', max_running: 1 as number | null, priority: 100 as number | null, enabled: true })
const savedConfigForm = ref('')
const configLoading = ref(false)
const configSaving = ref(false)
const configError = ref('')
const saveError = ref('')
const configDirty = computed(() => Boolean(savedConfigForm.value) && JSON.stringify(configForm) !== savedConfigForm.value)
const modelOptions = computed(() => options.value?.models.map((model) => ({ value: model.id, label: `${model.display_name} · ${model.model_name}${!model.enabled ? ' · 已停用' : !model.credential_configured ? ' · 未配置凭据' : ''}` })) || [])
const engineOptions = computed(() => options.value?.engines.map((engine) => ({ value: engine.id, label: engine.name })) || [])
const selectedModel = computed(() => options.value?.models.find((model) => model.id === configForm.model_id))
const modelProblem = computed(() => {
  if (!selectedModel.value) return ''
  if (!selectedModel.value.enabled) return '所选模型已停用，请启用或更换模型。'
  return selectedModel.value.credential_configured ? '' : '所选模型尚未配置凭据。'
})
const configStatus = computed(() => {
  if (configSaving.value) return '正在保存'
  if (configDirty.value) return '有未保存修改'
  if (!configuration.value?.tenant_worker_id) return '尚未保存'
  if (!configuration.value.enabled) return '已停用'
  return configuration.value.ready ? '已保存' : '配置不可用'
})
function applySavedConfig(saved: VerificationConfiguration, choices: VerificationConfigurationOptions) {
  Object.assign(configForm, {
    model_id: saved.model_id || '', display_name: saved.display_name ?? choices.defaults.display_name,
    engine: saved.engine ?? choices.defaults.engine, max_running: saved.max_running ?? choices.defaults.max_running,
    priority: saved.priority ?? choices.defaults.priority, enabled: saved.enabled ?? choices.defaults.enabled,
  })
  savedConfigForm.value = JSON.stringify(configForm)
}
let scopeGeneration = 0
let tenantGeneration = 0
let configGeneration = 0
async function loadTenants() {
  if (!auth.isSuperAdmin || !auth.session || tenantsLoading.value) return
  const generation = ++tenantGeneration
  tenantsLoading.value = true
  tenantError.value = ''
  try {
    const items = await listVerificationTenants()
    if (generation !== tenantGeneration) return
    tenants.value = items; tenantsLoaded.value = true
  } catch (error) { if (generation === tenantGeneration) tenantError.value = errorMessage(error) }
  finally { if (generation === tenantGeneration) tenantsLoading.value = false }
}
function selectTenant(value: string) {
  if (auth.isSuperAdmin && tenants.value.some((tenant) => tenant.id === value)) void router.push({ name: 'VerificationConfiguration', query: { tenant_id: value } })
}
async function loadConfig() {
  if (!tenantId.value || configLoading.value || configSaving.value) return
  const scope = scopeGeneration
  const generation = ++configGeneration
  const target = tenantId.value
  const requestTenant = auth.isSuperAdmin ? target : undefined
  configLoading.value = true
  configError.value = ''
  try {
    const [saved, choices] = await Promise.all([getVerificationConfiguration(requestTenant), getVerificationConfigurationOptions(requestTenant)])
    if (scope !== scopeGeneration || generation !== configGeneration) return
    if (saved.tenant_id !== target) throw new Error('执行配置响应与当前范围不一致，请重新读取。')
    configuration.value = saved; options.value = choices; saveError.value = ''
    applySavedConfig(saved, choices)
  } catch (error) { if (scope === scopeGeneration && generation === configGeneration) configError.value = errorMessage(error) }
  finally { if (scope === scopeGeneration && generation === configGeneration) configLoading.value = false }
}
function reloadConfig() { if (!configDirty.value || window.confirm('重新读取将放弃未保存的执行设置，继续吗？')) void loadConfig() }
async function saveConfig() {
  if (!tenantId.value || !options.value || configSaving.value || configLoading.value || configError.value) return
  if (!configForm.model_id) { saveError.value = '请选择模型。'; return }
  if (!Number.isInteger(configForm.max_running) || Number(configForm.max_running) < 1 || !Number.isInteger(configForm.priority)) { saveError.value = '最大并发须为正整数，优先级须为整数。'; return }
  const scope = scopeGeneration
  const generation = ++configGeneration
  const target = tenantId.value
  const choices = options.value
  configSaving.value = true
  saveError.value = ''
  try {
    const saved = await saveVerificationConfiguration(auth.isSuperAdmin ? target : undefined, { ...configForm, display_name: configForm.display_name.trim() || choices.defaults.display_name, engine: 'pi', max_running: Number(configForm.max_running), priority: Number(configForm.priority) })
    if (scope !== scopeGeneration || generation !== configGeneration) return
    if (saved.tenant_id !== target) throw new Error('保存响应与当前范围不一致，请重新核对已保存配置。')
    configuration.value = saved
    applySavedConfig(saved, choices)
  } catch (error) { if (scope === scopeGeneration && generation === configGeneration) saveError.value = errorMessage(error) }
  finally { if (scope === scopeGeneration && generation === configGeneration) configSaving.value = false }
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
  if (!canManageIntegrations.value || loading.value || busy.value) return
  const generation = ++integrationGeneration
  loading.value = true
  try { const items = await listVerificationIntegrations(tenantId.value); if (generation === integrationGeneration) { integrations.value = items; loaded.value = true; loadError.value = '' } }
  catch (error) { if (generation === integrationGeneration) loadError.value = errorMessage(error) }
  finally { if (generation === integrationGeneration) loading.value = false }
}
function openCreate() {
  if (!canManageIntegrations.value || busy.value || secret.value || unconfirmedCreate.value) return
  editingId.value = ''; form.name = ''; form.enabled = true; editorError.value = ''; editorOpen.value = true
}
function openEdit(item: VerificationIntegration) {
  if (!canManageIntegrations.value || busy.value || secret.value) return
  editingId.value = item.id; form.name = item.name; form.enabled = item.enabled; editorError.value = ''; editorOpen.value = true
}
function replaceIntegration(item: VerificationIntegration) { integrations.value = [...integrations.value.filter((entry) => entry.id !== item.id), item]; loaded.value = true }
async function save() {
  if (!canManageIntegrations.value || busy.value || unconfirmedCreate.value) return
  if (!form.name.trim()) { editorError.value = '请填写集成名称。'; return }
  const scope = scopeGeneration
  const target = tenantId.value
  const editing = editingId.value
  const payload: VerificationIntegrationInput = { name: form.name }
  busy.value = true; editorError.value = ''; operationError.value = ''
  try {
    if (editing) {
      const item = await updateVerificationIntegration(target, editing, { ...payload, enabled: form.enabled })
      if (scope !== scopeGeneration) return
      replaceIntegration(item)
    } else {
      const response = await createVerificationIntegration(target, payload)
      if (scope !== scopeGeneration) return
      replaceIntegration(response.integration); secret.value = { name: response.integration.name, token: response.token }; copyMessage.value = ''
    }
    editorOpen.value = false
  } catch (error) {
    if (scope !== scopeGeneration) return
    editorError.value = errorMessage(error)
    const rejected = error instanceof ApiError && [400, 401, 403, 404, 409, 422].includes(error.status)
    if (!editing && !rejected) { unconfirmedCreate.value = payload; editorOpen.value = false; operationError.value = '创建结果未确认，不会自动重复创建，请核对列表。' }
  } finally { if (scope === scopeGeneration) busy.value = false }
}
async function performAction() {
  if (!canManageIntegrations.value || !action.value || busy.value || (action.value.type === 'rotate' && secret.value)) return
  const scope = scopeGeneration
  const target = tenantId.value
  const current = action.value
  busy.value = true; operationError.value = ''
  try {
    if (current.type === 'disable') {
      const item = await disableVerificationIntegration(target, current.integration.id)
      if (scope !== scopeGeneration) return
      replaceIntegration(item)
    } else {
      const response = await rotateVerificationIntegrationToken(target, current.integration.id)
      if (scope !== scopeGeneration) return
      replaceIntegration(response.integration); secret.value = { name: response.integration.name, token: response.token }; copyMessage.value = ''
    }
  } catch (error) { if (scope === scopeGeneration) operationError.value = current.type === 'rotate' ? `轮换结果未确认，旧令牌可能已失效，新令牌尚未取得。不会自动轮换；核对后可明确再次轮换。${errorMessage(error)}` : `停用结果未确认，请刷新核对。${errorMessage(error)}` }
  finally { if (scope === scopeGeneration) { busy.value = false; action.value = null } }
}
async function copyToken() {
  if (!canManageIntegrations.value || !secret.value) return
  const current = secret.value
  const scope = scopeGeneration
  try { await navigator.clipboard.writeText(current.token); if (scope === scopeGeneration && secret.value === current) copyMessage.value = '已复制，请粘贴到安全的凭据存储。' }
  catch { if (scope === scopeGeneration && secret.value === current) copyMessage.value = '浏览器无法自动复制，请手工选中并安全保存上方令牌。' }
}
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
onBeforeUnmount(() => { scopeGeneration++; tenantGeneration++; configGeneration++; integrationGeneration++; hideSecret(); window.removeEventListener('beforeunload', beforeUnload) })
watch(authIdentity, () => {
  tenantGeneration++; tenants.value = []; tenantsLoaded.value = false; tenantsLoading.value = false; tenantError.value = ''
  activeTab.value = 'execution'
}, { immediate: true, flush: 'sync' })
// 身份或实际管理范围一变就清空内存；加载另行批处理，避免旧请求回写新身份。
watch([scopeKey, tenantId], () => {
  scopeGeneration++; configGeneration++; integrationGeneration++
  configLoading.value = false; configSaving.value = false; loading.value = false; busy.value = false
  configuration.value = null; options.value = null; savedConfigForm.value = ''
  Object.assign(configForm, { model_id: '', display_name: '', engine: 'pi', max_running: 1, priority: 100, enabled: true })
  integrations.value = []; loaded.value = false; editingId.value = ''; form.name = ''; form.enabled = true
  configError.value = ''; saveError.value = ''; loadError.value = ''; operationError.value = ''; editorError.value = ''
  unconfirmedCreate.value = null; editorOpen.value = false; action.value = null; confirmAbandon.value = false; hideSecret()
}, { immediate: true, flush: 'sync' })
watch(authIdentity, () => { if (auth.isSuperAdmin && auth.session) void loadTenants() }, { immediate: true })
watch([scopeKey, tenantId], () => {
  if (tenantId.value) { void loadConfig(); if (activeTab.value === 'integrations') void refresh() }
}, { immediate: true })
watch(activeTab, (tab) => { if (tab === 'integrations' && canManageIntegrations.value && !loaded.value) void refresh() })
const apiExample = JSON.stringify({ schema_version: 1, request_id: '<调用方生成并保留的唯一 ID>', external_id: 'ISSUE-123', mode: 'retest', title: '授权环境修复后复测', origin: '<当前目标、身份、授权范围与排除项>', report: { type: 'markdown', content: '<原始漏洞资料>' }, authorization_expires_at: null }, null, 2)
</script>

<style scoped>
/* 仅此配置页预留滚动条空间，避免页签切换或展开高级项时跳宽。 */
:global(.content-viewport:has(.verification-config-page)) {
  scrollbar-gutter: stable;
}

.verification-config-page {
  max-width: none;
  margin-inline: 0;
  container: verification-config / inline-size;
  color: var(--text);
  font-size: var(--text-md);
  line-height: var(--leading-normal);
}

.config-heading {
  align-items: center;
  flex-direction: row;
  flex-wrap: wrap;
}

.config-heading > h1 {
  min-width: 0;
  overflow-wrap: anywhere;
}

.config-tenant,
.config-workspace {
  width: 100%;
  min-width: 0;
}

.config-tenant :deep(.ui-select__trigger > span:first-child),
.config-model :deep(.ui-select__trigger > span:first-child) {
  white-space: normal;
  overflow-wrap: anywhere;
}

.config-workspace :deep(.ui-tabs__trigger[data-state='active']::after) {
  bottom: 0;
}

.config-panel {
  min-width: 0;
  overflow-wrap: anywhere;
  padding: var(--space-6);
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-lg);
  background: var(--bg-surface);
}

.config-form,
.config-integrations,
.config-integration-form {
  display: flex;
  flex-direction: column;
  min-width: 0;
  gap: var(--space-5);
}

.config-main-fields,
.config-advanced-fields {
  display: grid;
  grid-template-columns: minmax(0, 3fr) minmax(0, 2fr);
  align-items: end;
  min-width: 0;
  gap: var(--space-5);
}

.config-model {
  display: flex;
  min-width: 0;
  flex-direction: column;
  gap: var(--space-2);
}

.config-model-heading {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-2);
}

.config-model-heading > label {
  font-size: var(--text-sm);
  font-weight: var(--font-medium);
}

.config-required {
  color: var(--danger);
}

.config-model-link {
  min-height: var(--control-height-sm);
  display: inline-flex;
  align-items: center;
  color: var(--link);
  text-underline-offset: 3px;
}

.config-secondary-fields {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  align-items: end;
  gap: var(--space-4);
}

.config-enabled {
  min-height: var(--control-height);
}

.config-model-link:hover {
  color: var(--link-hover);
}

.config-problem {
  margin: 0;
  color: var(--danger);
  overflow-wrap: anywhere;
}

.config-advanced {
  border-top: 1px solid var(--border-subtle);
}

.config-advanced > summary,
.config-api > summary {
  min-height: var(--control-height);
  padding-block: var(--space-2);
  color: var(--text-muted);
  cursor: pointer;
}

.config-advanced > summary:focus-visible,
.config-api > summary:focus-visible,
.config-table-scroll:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 2px;
}

.config-advanced-fields {
  padding-top: var(--space-3);
}

.config-parameter-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  width: 100%;
  max-width: 28rem;
  gap: var(--space-4);
}

.config-state,
.config-actions,
.config-section-heading {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--space-3);
}

.config-state {
  gap: var(--space-2);
  color: var(--text-muted);
  overflow-wrap: anywhere;
}

.config-save {
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
  padding-top: var(--space-5);
  border-top: 1px solid var(--border-subtle);
}

.config-save > p {
  margin: 0;
  color: var(--text-muted);
  font-size: var(--text-sm);
}

.config-section-heading {
  justify-content: space-between;
}

.config-section-heading > h2 {
  margin: 0;
  font-size: var(--text-lg);
  font-weight: var(--font-semibold);
}

.config-table-scroll {
  min-width: 0;
  overflow-x: auto;
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-md);
}

.config-table {
  width: 100%;
  min-width: 720px;
  border-collapse: collapse;
  text-align: left;
}

.config-table th,
.config-table td {
  padding: var(--space-3) var(--space-4);
  border-bottom: 1px solid var(--border-subtle);
  vertical-align: middle;
}

.config-table th {
  background: var(--bg-inset);
  color: var(--text-muted);
  font-size: var(--text-sm);
  font-weight: var(--font-medium);
  white-space: nowrap;
}

.config-table td:first-child {
  max-width: 280px;
  overflow-wrap: anywhere;
}

.config-table tbody tr:last-child td {
  border-bottom: 0;
}

.config-table tbody tr:hover {
  background: var(--surface-hover);
}

.config-api {
  border-top: 1px solid var(--border-subtle);
}

.config-api > p {
  margin: var(--space-3) 0;
  color: var(--text-muted);
}

.config-token,
.config-code {
  max-width: 100%;
  padding: var(--space-4);
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  background: var(--bg-inset);
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  font: var(--text-md) / var(--leading-relaxed) var(--font-mono);
}

.config-token {
  user-select: all;
}

@container verification-config (max-width: 900px) {
  .config-main-fields,
  .config-advanced-fields {
    grid-template-columns: minmax(0, 1fr);
  }
}

@container verification-config (max-width: 640px) {
  .config-panel {
    padding: var(--space-4);
  }

  .config-secondary-fields,
  .config-parameter-grid {
    grid-template-columns: minmax(0, 1fr);
  }

  .config-save > .config-actions {
    align-items: stretch;
  }

  .config-save > .config-actions > .ui-button {
    flex: 1 1 auto;
    white-space: normal;
  }
}
</style>
