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
    <section class="login-panel" aria-labelledby="login-title">
      <header class="login-heading">
        <h1 id="login-title">登录 Arena</h1>
      </header>
      <form class="login-form" :aria-busy="submitting" @submit.prevent="submit">
        <FormField v-slot="{ controlAttrs, invalid }" label="登录名" required>
          <Input
            v-model="loginName"
            v-bind="controlAttrs"
            :invalid="invalid"
            autocomplete="username"
            autofocus
          />
        </FormField>
        <FormField v-slot="{ controlAttrs, invalid }" label="密码" required>
          <Input
            v-model="password"
            v-bind="controlAttrs"
            :invalid="invalid"
            type="password"
            autocomplete="current-password"
          />
        </FormField>
        <Alert v-if="errorMessage" class="login-error" tone="danger">{{ errorMessage }}</Alert>
        <Button
          type="submit"
          block
          :loading="submitting"
          :disabled="submitting || !loginName.trim() || !password"
        >
          {{ submitting ? '正在验证…' : '登录' }}
        </Button>
      </form>
    </section>
  </main>
</template>

<script setup lang="ts">
import { ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { Moon, Sun } from '@lucide/vue'
import { Alert, Button, FormField, IconButton, Input } from '@/components/ui'
import { useUIStore } from '@/stores/ui'
import { useAuthStore } from '@/stores/auth'
import { safeRedirect } from './passwordValidation'

const authStore = useAuthStore()
const uiStore = useUIStore()
const route = useRoute()
const router = useRouter()
const loginName = ref('')
const password = ref('')
const submitting = ref(false)
const errorMessage = ref('')

const submit = async () => {
  if (submitting.value) return
  submitting.value = true
  errorMessage.value = ''
  try {
    await authStore.login(loginName.value.trim(), password.value)
    const redirect = safeRedirect(route.query.redirect)
    if (authStore.mustChangePassword) {
      await router.replace({ name: 'ChangePassword', query: { redirect } })
    } else {
      await router.replace(redirect)
    }
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '登录失败，请重试'
  } finally {
    submitting.value = false
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
  width: min(440px, calc(100% - 48px));
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
    margin-top: calc(10 * var(--ui-vh));
  }
}
</style>
