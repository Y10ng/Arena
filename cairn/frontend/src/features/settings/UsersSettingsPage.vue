<template>
  <article class="console-page users-settings-page">
    <Alert v-if="loadError && users.length > 0" tone="warning" title="刷新失败，保留上次用户列表">
      {{ errorText(loadError) }}
    </Alert>

    <header class="resource-page-heading users-page-heading">
      <h1 class="sr-only">用户管理</h1>
      <p class="text-muted">已加载 {{ users.length }} 个账户</p>
      <div class="list-actions">
        <IconButton label="刷新用户列表" :loading="loading" @click="reload"
          ><RefreshCw :size="16"
        /></IconButton>
        <Button @click="openCreate">新增用户</Button>
      </div>
    </header>
    <section class="users-list-card" aria-label="用户列表">
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
      <EmptyState v-else-if="users.length === 0" title="暂无用户" />
      <DataTable
        v-else
        :rows="users"
        :columns="userColumns"
        :row-key="(user) => user.id"
        :row-attrs="userRowAttrs"
        caption="管理员用户列表"
        density="default"
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
          <DropdownMenu :items="userActions(user)" @select="handleUserAction($event, user)">
            <template #trigger>
              <IconButton
                label="用户操作"
                :loading="busyUserId === user.id"
                @click="rememberUserTrigger"
                @keydown="rememberUserTrigger"
                ><MoreHorizontal :size="16"
              /></IconButton>
            </template>
          </DropdownMenu>
        </template>
      </DataTable>

      <footer v-if="users.length > 0 && hasMore" class="load-more-row">
        <Button variant="secondary" :disabled="loadingMore" @click="loadMore">
          {{ loadingMore ? '加载中...' : '加载更多' }}
        </Button>
      </footer>
    </section>

    <Dialog
      :open="createOpen"
      title="新增用户"
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
import { computed, nextTick, ref, watch, type HTMLAttributes } from 'vue'
import { useRouter } from 'vue-router'
import { MoreHorizontal, RefreshCw } from '@lucide/vue'
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
  Dialog,
  DropdownMenu,
  IconButton,
  EmptyState,
  FormField,
  Input,
  Skeleton,
} from '@/components/ui'
import type { DropdownMenuOption } from '@/components/ui/DropdownMenu.vue'
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
let userActionTrigger: HTMLElement | null = null
function rememberUserTrigger(event: Event) {
  userActionTrigger = event.currentTarget as HTMLElement
}
function userActions(user: AdminUser): DropdownMenuOption[] {
  const busy = busyUserId.value === user.id
  const self = isCurrentUser(user)
  return [
    { value: 'password', label: '重置密码', disabled: busy },
    {
      value: 'status',
      label: user.disabled ? '启用账户' : '禁用账户',
      disabled: busy || self,
      description: self ? '不能禁用当前账户' : undefined,
      separatorBefore: true,
      tone: user.disabled ? 'default' : 'danger',
    },
    {
      value: 'revoke',
      label: '撤销会话',
      tone: 'danger',
      disabled: busy || self || user.active_session_count === 0,
      description: self
        ? '不能撤销当前账户会话'
        : user.active_session_count === 0
          ? '没有活跃会话'
          : undefined,
    },
  ]
}
watch(pendingAction, (action, previous) => {
  if (!action && previous)
    void nextTick(() => userActionTrigger?.isConnected && userActionTrigger.focus())
})
function handleUserAction(action: string, user: AdminUser) {
  if (action === 'password') openPasswordReset(user, userActionTrigger)
  else if (action === 'status') requestStatusToggle(user)
  else if (action === 'revoke') requestSessionRevoke(user)
}

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

function openPasswordReset(user: AdminUser, trigger: HTMLElement | null) {
  if (busyUserId.value === user.id) return
  passwordResetTrigger.value = trigger
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
  gap: var(--space-3);
  padding-inline: var(--layout-content-padding);
}
.users-page-heading {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 24px;
}
.users-page-heading p {
  margin: 0;
  font-size: var(--text-sm);
}
.list-actions,
.status-stack {
  display: flex;
  align-items: center;
  gap: 8px;
}
.status-stack {
  flex-wrap: wrap;
}
.users-table-skeleton {
  display: grid;
  gap: 8px;
}
.user-identity {
  display: flex;
  align-items: baseline;
  gap: var(--space-2);
  min-width: 190px;
  white-space: nowrap;
}
.user-identity strong {
  font-weight: 500;
  font-size: var(--text-md);
}
.user-identity small {
  font-size: var(--text-sm);
}
.current-user-label {
  margin-left: 8px;
  color: var(--text-muted);
  font-size: var(--text-sm);
  font-weight: 400;
}
.tenant-cell {
  display: block;
  max-width: 220px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.load-more-row {
  display: flex;
  justify-content: center;
  padding-top: 20px;
}
.dialog-note {
  margin: 0 0 20px;
  color: var(--text-muted);
  font-size: var(--text-md);
  line-height: 1.7;
}
@media (max-width: 640px) {
  .users-settings-page {
    padding-inline: 12px;
  }
  .users-page-heading {
    align-items: flex-start;
    flex-direction: column;
  }
}
</style>
