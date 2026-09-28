<template>
  <main class="login-page">
    <Card as="section" padding="none" class="login-card" aria-labelledby="login-title">
      <div class="login-brand">
        <span class="login-logo" aria-hidden="true">A</span>
        <div>
          <p class="login-kicker">Arena</p>
          <h1 id="login-title">登录</h1>
        </div>
      </div>

      <p class="login-copy">使用管理员创建的账户访问你的个人 Tenant 与授权资源。</p>

      <form class="login-form" @submit.prevent="submit">
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
    </Card>
  </main>
</template>

<script setup lang="ts">
import { ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { Alert, Button, Card, FormField, Input } from '@/components/ui'
import { useAuthStore } from '@/stores/auth'
import { safeRedirect } from './passwordValidation'

const authStore = useAuthStore()
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
  min-height: 100vh;
  display: grid;
  place-items: center;
  padding: var(--space-6);
  background:
    radial-gradient(circle at 20% 15%, var(--accent-dim), transparent 34%), var(--bg-base);
}

.login-card {
  width: min(420px, 100%);
  gap: 0;
  padding: 34px;
  box-shadow: var(--shadow-pop);
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
  font-size: 24px;
  letter-spacing: -0.02em;
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

.login-error {
  font-size: 12px;
}

@media (max-width: 520px) {
  .login-page {
    align-items: start;
    padding: var(--space-4);
  }

  .login-card {
    margin-top: 10vh;
    padding: var(--space-5);
  }
}
</style>
