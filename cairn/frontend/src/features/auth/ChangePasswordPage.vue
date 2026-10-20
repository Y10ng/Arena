<template>
  <main class="login-page">
    <Card as="section" padding="none" class="login-card" aria-labelledby="change-password-title">
      <div class="login-brand">
        <span class="login-logo" aria-hidden="true">A</span>
        <div>
          <p class="login-kicker">Arena</p>
          <h1 id="change-password-title">修改密码</h1>
        </div>
      </div>

      <p class="login-copy">
        {{
          authStore.mustChangePassword
            ? '当前账户正在使用默认密码。首次登录必须修改密码后才能继续。'
            : '验证当前密码后设置新密码。修改成功后，其他登录会话将失效。'
        }}
      </p>

      <form class="login-form" @submit.prevent="submit">
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
        <FormField v-slot="{ controlAttrs, invalid }" label="新密码" required>
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
        <p class="password-hint">新密码不能使用默认密码 Arena@2026，也不能与当前密码相同。</p>
        <Alert v-if="errorMessage" class="login-error" tone="danger">{{ errorMessage }}</Alert>
        <Button type="submit" block :loading="submitting" :disabled="submitting || !canSubmit">
          {{ submitting ? '正在修改…' : '确认修改' }}
        </Button>
        <Button
          type="button"
          variant="secondary"
          block
          :loading="loggingOut"
          :disabled="submitting || loggingOut"
          @click="logout"
        >
          {{ loggingOut ? '正在退出…' : '退出登录' }}
        </Button>
      </form>
    </Card>
  </main>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { Alert, Button, Card, FormField, Input } from '@/components/ui'
import { useAuthStore } from '@/stores/auth'
import { safeRedirect, validatePasswordChange } from './passwordValidation'

const authStore = useAuthStore()
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
  if (submitting.value) return
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
  if (loggingOut.value) return
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
  min-height: 100vh;
  display: grid;
  place-items: center;
  padding: var(--space-6);
  background-color: var(--bg-base);
}

.login-card {
  width: min(440px, 100%);
  gap: 0;
  padding: var(--space-6);
  box-shadow: var(--shadow-xs);
}

.login-brand {
  display: flex;
  align-items: center;
  gap: var(--space-4);
}
.login-logo {
  display: grid;
  place-items: center;
  width: 64px;
  height: 32px;
  color: var(--text);
  font-size: 28px;
  font-weight: var(--font-semibold);
  line-height: 1;
}
.login-kicker {
  color: var(--accent);
  font-size: var(--text-sm);
  font-weight: var(--font-semibold);
  letter-spacing: 0.01em;
}
h1 {
  margin-top: 2px;
  font-size: var(--text-2xl);
  font-weight: var(--font-semibold);
  line-height: var(--leading-snug);
  letter-spacing: var(--tracking-tight);
}
.login-copy {
  margin: var(--space-5) 0;
  color: var(--text-muted);
}
.login-form {
  display: flex;
  flex-direction: column;
  gap: var(--space-4);
}
.password-hint {
  margin: calc(-1 * var(--space-2)) 0 0;
  color: var(--text-faint);
  font-size: var(--text-sm);
}
.login-error {
  font-size: var(--text-md);
}

@media (max-width: 520px) {
  .login-page {
    align-items: start;
    padding: var(--space-4);
  }
  .login-card {
    margin-top: 6vh;
    padding: var(--space-5);
  }
}
</style>
