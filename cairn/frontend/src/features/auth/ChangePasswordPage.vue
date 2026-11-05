<template>
  <main class="login-page">
    <header class="auth-header">
      <span class="auth-brand"><span class="auth-mark" aria-hidden="true">A</span>Arena</span>
      <IconButton
        :label="uiStore.theme === 'dark' ? '切换到浅色主题' : '切换到深色主题'"
        @click="uiStore.toggleTheme"
      >
        <Sun v-if="uiStore.theme === 'dark'" :size="18" /><Moon v-else :size="18" />
      </IconButton>
    </header>
    <section class="login-panel" aria-labelledby="change-password-title">
      <header class="login-heading">
        <h1 id="change-password-title">修改密码</h1>
        <p class="login-copy">
          {{
            authStore.mustChangePassword
              ? '当前账户正在使用默认密码。首次登录必须修改密码后才能继续。'
              : '验证当前密码后设置新密码。修改成功后，其他登录会话将失效。'
          }}
        </p>
      </header>

      <form class="login-form" :aria-busy="submitting || loggingOut" @submit.prevent="submit">
        <FormField v-slot="{ controlAttrs, invalid }" label="当前密码" required>
          <Input
            v-model="currentPassword"
            v-bind="controlAttrs"
            :invalid="invalid"
            type="password"
            autocomplete="current-password"
            autofocus
          />
        </FormField>
        <FormField
          v-slot="{ controlAttrs, invalid }"
          label="新密码"
          required
          hint="新密码不能使用默认密码 Arena@2026，也不能与当前密码相同。"
        >
          <Input
            v-model="newPassword"
            v-bind="controlAttrs"
            :invalid="invalid"
            type="password"
            autocomplete="new-password"
          />
        </FormField>
        <FormField v-slot="{ controlAttrs, invalid }" label="确认新密码" required>
          <Input
            v-model="confirmPassword"
            v-bind="controlAttrs"
            :invalid="invalid"
            type="password"
            autocomplete="new-password"
          />
        </FormField>
        <Alert v-if="errorMessage" class="login-error" tone="danger">{{ errorMessage }}</Alert>
        <Button
          type="submit"
          block
          :loading="submitting"
          :disabled="submitting || loggingOut || !canSubmit"
        >
          {{ submitting ? '正在修改…' : '确认修改' }}
        </Button>
        <Button
          type="button"
          variant="ghost"
          block
          :loading="loggingOut"
          :disabled="submitting || loggingOut"
          @click="logout"
        >
          {{ loggingOut ? '正在退出…' : '退出登录' }}
        </Button>
      </form>
    </section>
  </main>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { Moon, Sun } from '@lucide/vue'
import { Alert, Button, FormField, IconButton, Input } from '@/components/ui'
import { useUIStore } from '@/stores/ui'
import { useAuthStore } from '@/stores/auth'
import { safeRedirect, validatePasswordChange } from './passwordValidation'

const authStore = useAuthStore()
const uiStore = useUIStore()
const route = useRoute()
const router = useRouter()
const currentPassword = ref('')
const newPassword = ref('')
const confirmPassword = ref('')
const submitting = ref(false)
const loggingOut = ref(false)
const errorMessage = ref('')

const canSubmit = computed(() =>
  Boolean(currentPassword.value && newPassword.value && confirmPassword.value),
)

const clearPasswords = () => {
  currentPassword.value = ''
  newPassword.value = ''
  confirmPassword.value = ''
}

const submit = async () => {
  if (submitting.value || loggingOut.value) return
  errorMessage.value = validatePasswordChange({
    currentPassword: currentPassword.value,
    newPassword: newPassword.value,
    confirmPassword: confirmPassword.value,
  })
  if (errorMessage.value) return

  submitting.value = true
  try {
    await authStore.updatePassword(currentPassword.value, newPassword.value)
    clearPasswords()
    await router.replace(safeRedirect(route.query.redirect))
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '密码修改失败，请重试'
  } finally {
    submitting.value = false
  }
}

const logout = async () => {
  if (loggingOut.value || submitting.value) return
  loggingOut.value = true
  clearPasswords()
  try {
    await authStore.logout()
  } finally {
    await router.replace({ name: 'Login' })
    loggingOut.value = false
  }
}
</script>

<style scoped>
.login-page {
  min-height: calc(100 * var(--ui-dvh));
  display: grid;
  grid-template-rows: auto 1fr;
  background: var(--color-canvas);
  color: var(--text);
}
.auth-header {
  min-height: var(--layout-topbar-height);
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: var(--space-3) var(--space-7);
  border-bottom: 1px solid var(--border);
}
.auth-brand {
  display: flex;
  align-items: center;
  gap: var(--space-3);
  font-size: var(--text-object);
  font-weight: var(--font-semibold);
}
.auth-mark {
  display: grid;
  place-items: center;
  width: 31px;
  height: 31px;
  border-radius: var(--radius-md);
  background: var(--color-primary);
  color: var(--color-primary-contrast);
  font-size: var(--text-lg);
}
.login-panel {
  align-self: center;
  width: min(480px, calc(100% - 48px));
  margin: var(--space-8) auto;
}
.login-heading {
  margin-bottom: var(--space-6);
}
h1 {
  margin: 0;
  font-size: var(--text-object);
  font-weight: var(--font-semibold);
  line-height: 1.4;
  letter-spacing: var(--tracking-tight);
}
.login-copy {
  margin: var(--space-3) 0 0;
  color: var(--text-muted);
  line-height: 1.8;
  font-size: var(--text-md);
}
.login-form {
  display: flex;
  flex-direction: column;
  gap: var(--space-5);
}
.login-error {
  font-size: var(--text-md);
  overflow-wrap: anywhere;
}
@media (max-width: 520px) {
  .auth-header {
    padding-inline: var(--space-5);
  }
  .login-panel {
    align-self: start;
    margin-top: calc(6 * var(--ui-vh));
  }
}
</style>
