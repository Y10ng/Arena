<template>
  <article class="console-page">
    <Card padding="none" class="console-card form-partition timeout-card">
      <Alert v-if="isLoadingTimeouts" class="timeout-state" tone="neutral">
        <Spinner size="sm" decorative />
        正在加载生命周期超时配置...
      </Alert>
      <Alert v-else-if="isTimeoutsError" class="timeout-state" tone="danger">
        加载超时配置失败：{{ formatError(timeoutsError) }}
      </Alert>
      <div v-else-if="timeoutForm" class="pane-form">
        <FormField
          v-slot="{ controlAttrs, invalid }"
          label="决策意图判定超时阈值 (Intent Timeout, 秒)"
          hint="自治意图无心跳或无法提供工具产物证据链条的最大搁置倒计时 (≥5秒)"
          :error="timeoutErrors.intent"
          required
        >
          <Input
            v-model="timeoutForm.intent_timeout"
            v-bind="controlAttrs"
            :invalid="invalid"
            type="number"
            class="mono"
            min="5"
            step="1"
          />
        </FormField>

        <FormField
          v-slot="{ controlAttrs, invalid }"
          label="自治决策推理超时阈值 (Reason Timeout, 秒)"
          hint="自治循环 reason 阶段评估的最长运行时间预算 (≥5秒)"
          :error="timeoutErrors.reason"
          required
        >
          <Input
            v-model="timeoutForm.reason_timeout"
            v-bind="controlAttrs"
            :invalid="invalid"
            type="number"
            class="mono"
            min="5"
            step="1"
          />
        </FormField>

        <footer class="editor-footer justify-end">
          <Button
            variant="success"
            :loading="isSavingTimeouts"
            :disabled="!isTimeoutsValid || isSavingTimeouts"
            :aria-disabled="!isTimeoutsValid || isSavingTimeouts"
            :title="timeoutErrors.intent || timeoutErrors.reason || '保存超时策略'"
            @click="saveTimeouts"
          >
            {{ isSavingTimeouts ? '保存中...' : '保存超时策略' }}
          </Button>
        </footer>
      </div>
    </Card>
  </article>
</template>

<script setup lang="ts">
import { Alert, Button, Card, FormField, Input, Spinner } from '@/components/ui'
import { useTimeoutConsole } from './useTimeoutConsole'
import './console.css'

const {
  formatError,
  isLoadingTimeouts,
  isSavingTimeouts,
  isTimeoutsError,
  isTimeoutsValid,
  saveTimeouts,
  timeoutErrors,
  timeoutForm,
  timeoutsError,
} = useTimeoutConsole()
</script>

<style scoped>
.timeout-card {
  gap: 0;
}

.timeout-state :deep(.ui-alert__content) {
  display: flex;
  align-items: center;
  gap: var(--space-2);
}
</style>
