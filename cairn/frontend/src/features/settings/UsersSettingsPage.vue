<template>
  <article class="console-page users-settings-page">
    <Alert v-if="loadError && users.length > 0" tone="warning" title="刷新失败，保留上次用户列表">
      {{ errorText(loadError) }}
    </Alert>

    <Card class="users-list-card" padding="lg">
      <div class="list-header">
        <div>
          <strong>用户列表</strong>
          <p class="text-faint">已加载 {{ users.length }} 个账户</p>
        </div>
        <div class="list-actions">
          <Button variant="success" size="sm" @click="openCreate">新增用户</Button>
          <Button variant="secondary" size="sm" :disabled="loading" @click="reload">刷新</Button>
        </div>
      </div>

      <ErrorState
        v-if="loadError && users.length === 0"
        title="用户列表加载失败"
        :description="errorText(loadError)"
        @retry="reload"
      />
      <div v-else-if="loading && users.length === 0" class="users-table-skeleton" role="status">
        <Skeleton v-for="index in 5" :key="index" height="3.5rem" />
        <span class="sr-only">正在加载用户列表</span>
      </div>
      <EmptyState v-else-if="users.length === 0" title="暂无用户" description="暂无用户。" />
      <DataTable
        v-else
        :rows="users"
        :columns="userColumns"
        :row-key="(user) => user.id"
        :row-attrs="userRowAttrs"
        caption="管理员用户列表"
        density="compact"
      >
        <template #cell-user="{ row: user }">
          <div class="user-identity">
            <strong>
              {{ user.login }}
              <span v-if="isCurrentUser(user)" class="current-user-label">当前账户</span>
            </strong>
            <small class="mono text-faint">{{ user.id }}</small>
          </div>
        </template>
        <template #cell-role="{ row: user }">
          <Badge size="sm">{{ user.role === 'super_admin' ? '超级管理员' : '用户' }}</Badge>
        </template>
        <template #cell-status="{ row: user }">
          <div class="status-stack">
            <Badge :tone="user.disabled ? 'neutral' : 'success'" size="sm">
              {{ user.disabled ? '已禁用' : '启用' }}
            </Badge>
            <Badge v-if="user.must_change_password" tone="warning" size="sm">需首次改密</Badge>
          </div>
        </template>
        <template #cell-tenant="{ row: user }"
          ><span class="mono tenant-cell">{{ user.tenant_id || '—' }}</span></template
        >
        <template #cell-sessions="{ row: user }">{{ user.active_session_count }}</template>
        <template #cell-created="{ row: user }">{{ formatDate(user.created_at) }}</template>
        <template #cell-actions="{ row: user }">
          <div class="user-actions">
            <Button
              variant="secondary"
              size="sm"
              :disabled="busyUserId === user.id || isCurrentUser(user)"
              @click="requestStatusToggle(user)"
            >
              {{ user.disabled ? '启用' : '禁用账户' }}
            </Button>
            <Button
              variant="secondary"
              size="sm"
              :disabled="busyUserId === user.id"
              @click="openPasswordReset(user, $event)"
              >重置密码</Button
            >
            <Button
              variant="danger"
              size="sm"
              :disabled="
                busyUserId === user.id || isCurrentUser(user) || user.active_session_count === 0
              "
              @click="requestSessionRevoke(user)"
              >撤销会话</Button
            >
          </div>
        </template>
      </DataTable>

      <footer v-if="users.length > 0 && hasMore" class="load-more-row">
        <Button variant="secondary" :disabled="loadingMore" @click="loadMore">
          {{ loadingMore ? '加载中...' : '加载更多' }}
        </Button>
      </footer>
    </Card>

    <Dialog
      :open="createOpen"
      title="新增用户"
      description="新用户自动获得独立个人 Tenant。"
      size="sm"
      close-label="关闭新增用户"
      :dismissible="!creating"
      @update:open="handleCreateOpenChange"
      @open-auto-focus="focusCreateLogin"
      @close-auto-focus="restoreCreateTriggerFocus"
    >
      <p class="dialog-note">
        初始密码固定为 <strong class="mono">{{ DEFAULT_PROVISIONED_PASSWORD }}</strong
        >，用户首次登录后必须立即修改。
      </p>
      <form id="create-user-form" @submit.prevent="submitCreate">
        <FormField label="登录名" required>
          <template #default="{ controlAttrs }">
            <Input
              ref="createLoginInput"
              v-model="createLogin"
              v-bind="controlAttrs"
              autocomplete="username"
            />
          </template>
        </FormField>
      </form>
      <template #footer>
        <Button variant="secondary" :disabled="creating" @click="closeCreate">取消</Button>
        <Button
          form="create-user-form"
          type="submit"
          variant="success"
          :loading="creating"
          :disabled="!createLogin.trim()"
        >
          确认新增
        </Button>
      </template>
    </Dialog>

    <Dialog
      :open="Boolean(passwordTarget)"
      title="重置密码"
      :description="passwordTarget ? `用户：${passwordTarget.login}` : '为用户设置新密码。'"
      size="sm"
      close-label="关闭重置密码"
      :dismissible="!resettingPassword"
      @update:open="handlePasswordResetOpenChange"
      @open-auto-focus="focusResetPassword"
      @close-auto-focus="restorePasswordResetTriggerFocus"
    >
      <Alert v-if="passwordTarget && isCurrentUser(passwordTarget)" tone="warning">
        你正在重置当前账户密码。成功后当前会话会立即失效，并返回登录页。
      </Alert>
      <p class="dialog-note">重置成功后，该账户的全部现有会话都会失效。</p>
      <form id="reset-password-form" @submit.prevent="submitPasswordReset">
        <FormField label="新密码" required>
          <template #default="{ controlAttrs }">
            <Input
              ref="resetPasswordInputRef"
              v-model="resetPasswordInput"
              v-bind="controlAttrs"
              type="password"
              autocomplete="new-password"
            />
          </template>
        </FormField>
      </form>
      <template #footer>
        <Button variant="secondary" :disabled="resettingPassword" @click="closePasswordReset">
          取消
        </Button>
        <Button
          form="reset-password-form"
          type="submit"
          variant="success"
          :loading="resettingPassword"
          :disabled="!resetPasswordInput"
        >
          确认重置
        </Button>
      </template>
    </Dialog>

    <ConfirmAction
      :open="Boolean(pendingAction)"
      :title="pendingConfirmation.title"
      :description="pendingConfirmation.description"
      :confirm-label="pendingConfirmation.confirmLabel"
      :tone="pendingConfirmation.tone"
      :loading="confirmingAction"
      @update:open="handlePendingActionOpenChange"
      @confirm="confirmPendingAction"
    />
  </article>
</template>

<script setup lang="ts">
import { computed, nextTick, ref, type HTMLAttributes } from 'vue'
import { useRouter } from 'vue-router'
import {
  createAdminUser,
  listAdminUsers,
  resetAdminUserPassword,
  revokeAdminUserSessions,
  updateAdminUserStatus,
} from '@/api/client'
import type { AdminUser } from '@/api/types'
import { ConfirmAction, DataTable, ErrorState, type DataTableColumn } from '@/components/patterns'
import {
  Alert,
  Badge,
  Button,
  Card,
  Dialog,
  EmptyState,
  FormField,
  Input,
  Skeleton,
} from '@/components/ui'
import { DEFAULT_PROVISIONED_PASSWORD } from '@/features/auth/passwordValidation'
import { useAuthStore } from '@/stores/auth'
import { useUIStore } from '@/stores/ui'
import '@/features/console/console.css'

type PendingAction = { kind: 'status'; user: AdminUser } | { kind: 'revoke'; user: AdminUser }

type ConfirmationTone = 'neutral' | 'warning' | 'danger'
type UserColumnKey = 'user' | 'role' | 'status' | 'tenant' | 'sessions' | 'created' | 'actions'

const userColumns = [
  { key: 'user', label: '用户' },
  { key: 'role', label: '角色' },
  { key: 'status', label: '状态' },
  { key: 'tenant', label: '个人 Tenant' },
  { key: 'sessions', label: '活跃会话' },
  { key: 'created', label: '创建时间' },
  { key: 'actions', label: '操作', align: 'end' },
] satisfies readonly DataTableColumn<AdminUser, UserColumnKey>[]

const userRowAttrs = (user: AdminUser): HTMLAttributes & Record<string, string> => ({
  'data-testid': `user-row-${user.id}`,
  'data-user-id': user.id,
})

const PAGE_SIZE = 50
const authStore = useAuthStore()
const uiStore = useUIStore()
const router = useRouter()
const users = ref<AdminUser[]>([])
const loading = ref(false)
const loadingMore = ref(false)
const creating = ref(false)
const loadError = ref<unknown>(null)
const hasMore = ref(true)
const busyUserId = ref('')
const createOpen = ref(false)
const createLogin = ref('')
const createLoginInput = ref<InstanceType<typeof Input> | null>(null)
const createTrigger = ref<HTMLElement | null>(null)
const passwordTarget = ref<AdminUser | null>(null)
const resetPasswordInput = ref('')
const resetPasswordInputRef = ref<InstanceType<typeof Input> | null>(null)
const passwordResetTrigger = ref<HTMLElement | null>(null)
const pendingAction = ref<PendingAction | null>(null)

const resettingPassword = computed(() =>
  Boolean(passwordTarget.value && busyUserId.value === passwordTarget.value.id),
)
const confirmingAction = computed(() =>
  Boolean(pendingAction.value && busyUserId.value === pendingAction.value.user.id),
)
const pendingConfirmation = computed<{
  title: string
  description: string
  confirmLabel: string
  tone: ConfirmationTone
}>(() => {
  const action = pendingAction.value
  if (!action) {
    return {
      title: '确认操作',
      description: '请确认是否继续。',
      confirmLabel: '确认',
      tone: 'neutral',
    }
  }
  if (action.kind === 'revoke') {
    return {
      title: '撤销用户会话',
      description: `撤销用户「${action.user.login}」的全部会话？`,
      confirmLabel: '确认撤销',
      tone: 'danger',
    }
  }
  if (action.user.disabled) {
    return {
      title: '启用用户',
      description: `重新启用用户「${action.user.login}」？`,
      confirmLabel: '确认启用',
      tone: 'neutral',
    }
  }
  return {
    title: '禁用用户',
    description: `禁用账户「${action.user.login}」？其 Tenant 与业务数据会保留，但全部登录会话将立即失效。`,
    confirmLabel: '确认禁用',
    tone: 'warning',
  }
})

function errorText(error: unknown) {
  return error instanceof Error ? error.message : String(error)
}

function formatDate(value: string) {
  return new Date(value).toLocaleString('zh-CN')
}

function isCurrentUser(user: AdminUser) {
  return user.id === authStore.session?.user_id
}

async function fetchPage(offset: number) {
  const page = await listAdminUsers(PAGE_SIZE, offset)
  hasMore.value = page.length === PAGE_SIZE
  return page
}

async function reload() {
  loading.value = true
  loadError.value = null
  try {
    users.value = await fetchPage(0)
  } catch (error) {
    loadError.value = error
  } finally {
    loading.value = false
  }
}

async function loadMore() {
  loadingMore.value = true
  try {
    users.value.push(...(await fetchPage(users.value.length)))
  } catch (error) {
    uiStore.showToast(errorText(error), 'danger')
  } finally {
    loadingMore.value = false
  }
}

function openCreate(event: MouseEvent) {
  createTrigger.value = event.currentTarget instanceof HTMLElement ? event.currentTarget : null
  createLogin.value = ''
  createOpen.value = true
}

function focusCreateLogin(event: Event) {
  event.preventDefault()
  void nextTick(() => createLoginInput.value?.focus())
}

function restoreCreateTriggerFocus(event: Event) {
  event.preventDefault()
  createTrigger.value?.focus()
}

function handleCreateOpenChange(open: boolean) {
  if (!open) closeCreate()
}

function closeCreate() {
  if (creating.value) return
  createLogin.value = ''
  createOpen.value = false
}

async function submitCreate() {
  const login = createLogin.value.trim()
  if (!login) return
  creating.value = true
  try {
    await createAdminUser({ login })
    createOpen.value = false
    createLogin.value = ''
    await reload()
    uiStore.showToast(`用户已创建，初始密码为 ${DEFAULT_PROVISIONED_PASSWORD}`, 'success')
  } catch (error) {
    uiStore.showToast(errorText(error), 'danger')
  } finally {
    creating.value = false
  }
}

function requestStatusToggle(user: AdminUser) {
  if (isCurrentUser(user) || busyUserId.value === user.id) return
  pendingAction.value = { kind: 'status', user }
}

function openPasswordReset(user: AdminUser, event: MouseEvent) {
  if (busyUserId.value === user.id) return
  passwordResetTrigger.value =
    event.currentTarget instanceof HTMLElement ? event.currentTarget : null
  passwordTarget.value = user
  resetPasswordInput.value = ''
}

function focusResetPassword(event: Event) {
  event.preventDefault()
  void nextTick(() => resetPasswordInputRef.value?.focus())
}

function restorePasswordResetTriggerFocus(event: Event) {
  event.preventDefault()
  passwordResetTrigger.value?.focus()
}

function handlePasswordResetOpenChange(open: boolean) {
  if (!open) closePasswordReset()
}

function closePasswordReset() {
  if (resettingPassword.value) return
  resetPasswordInput.value = ''
  passwordTarget.value = null
}

async function submitPasswordReset() {
  if (!passwordTarget.value || !resetPasswordInput.value) return
  const target = passwordTarget.value
  const resettingSelf = isCurrentUser(target)
  busyUserId.value = target.id
  try {
    await resetAdminUserPassword(target.id, {
      password: resetPasswordInput.value,
      expected_updated_at: target.updated_at,
    })
    resetPasswordInput.value = ''
    passwordTarget.value = null
    if (resettingSelf) {
      authStore.clearSession()
      await router.replace({ name: 'Login' })
      return
    }
    await reload()
    uiStore.showToast('密码已重置', 'success')
  } catch (error) {
    uiStore.showToast(errorText(error), 'danger')
  } finally {
    // 失败时也清空密码；不会把明文放入 Query cache、列表模型或持久化存储。
    resetPasswordInput.value = ''
    busyUserId.value = ''
  }
}

function requestSessionRevoke(user: AdminUser) {
  if (isCurrentUser(user) || user.active_session_count === 0 || busyUserId.value === user.id) {
    return
  }
  pendingAction.value = { kind: 'revoke', user }
}

function handlePendingActionOpenChange(open: boolean) {
  if (!open && !confirmingAction.value) pendingAction.value = null
}

async function confirmPendingAction() {
  if (!pendingAction.value || confirmingAction.value) return
  const action = pendingAction.value
  const target = action.user
  if (isCurrentUser(target)) {
    pendingAction.value = null
    return
  }

  busyUserId.value = target.id
  try {
    if (action.kind === 'status') {
      await updateAdminUserStatus(target.id, {
        disabled: !target.disabled,
        expected_updated_at: target.updated_at,
      })
      await reload()
      uiStore.showToast(target.disabled ? '用户已启用' : '用户已禁用', 'success')
    } else {
      await revokeAdminUserSessions(target.id)
      await reload()
      uiStore.showToast('用户会话已撤销', 'success')
    }
  } catch (error) {
    uiStore.showToast(errorText(error), 'danger')
  } finally {
    busyUserId.value = ''
    pendingAction.value = null
  }
}

void reload()
</script>

<style scoped>
.users-settings-page {
  display: grid;
  gap: var(--space-4);
}

.list-header,
.list-actions,
.status-stack {
  display: flex;
  align-items: center;
}

.list-header {
  justify-content: space-between;
  gap: var(--space-4);
}

.list-actions,
.status-stack {
  gap: var(--space-2);
}

.status-stack {
  flex-wrap: wrap;
}

.users-table-skeleton {
  display: grid;
  gap: var(--space-2);
}

.user-identity {
  display: grid;
  gap: 3px;
  min-width: 190px;
}

.users-table-wrap {
  overflow-x: auto;
}

.users-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 12px;
}

.users-table th,
.users-table td {
  padding: 12px 10px;
  border-bottom: 1px solid var(--border-faint);
  text-align: left;
  vertical-align: middle;
}

.users-table th {
  color: var(--text-muted);
  font-weight: 600;
}

.users-table td:first-child {
  display: grid;
  gap: 3px;
  min-width: 190px;
}

.current-user-label {
  margin-left: var(--space-1);
  color: var(--text-faint);
  font-size: var(--text-xs);
  font-weight: var(--font-medium);
}

.tenant-cell {
  max-width: 220px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.user-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  min-width: 280px;
}

.load-more-row {
  display: flex;
  justify-content: center;
  padding-top: var(--space-4);
}

.dialog-note {
  margin: 0 0 var(--space-4);
  color: var(--text-muted);
  font-size: var(--text-md);
  line-height: var(--leading-normal);
}

@media (max-width: 640px) {
  .list-header {
    align-items: flex-start;
    flex-direction: column;
  }
}
</style>
