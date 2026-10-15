<template>
  <AlertDialog
    :open="open"
    :title="title"
    :description="description"
    :tone="tone"
    :dismissible="!loading"
    :content-class="contentClass"
    :overlay-class="overlayClass"
    @update:open="emit('update:open', $event)"
  >
    <template v-if="$slots.trigger" #trigger>
      <slot name="trigger" />
    </template>

    <slot />

    <template #cancel>
      <Button variant="secondary" :disabled="loading">
        {{ cancelLabel }}
      </Button>
    </template>

    <template #action>
      <Button
        :variant="resolvedConfirmVariant"
        :loading="loading"
        :disabled="confirmDisabled"
        @click="emit('confirm')"
      >
        {{ confirmLabel }}
      </Button>
    </template>
  </AlertDialog>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import AlertDialog from '../ui/AlertDialog.vue'
import type { AlertDialogTone } from '../ui/AlertDialog.vue'
import Button from '../ui/Button.vue'
import type { ButtonVariant } from '../ui/Button.vue'

const props = withDefaults(
  defineProps<{
    open: boolean
    title: string
    description: string
    confirmLabel?: string
    cancelLabel?: string
    tone?: AlertDialogTone
    confirmVariant?: ButtonVariant
    loading?: boolean
    confirmDisabled?: boolean
    contentClass?: string
    overlayClass?: string
  }>(),
  {
    confirmLabel: '确认',
    cancelLabel: '取消',
    tone: 'danger',
    confirmVariant: undefined,
    loading: false,
    confirmDisabled: false,
  },
)

const emit = defineEmits<{
  (event: 'update:open', value: boolean): void
  (event: 'confirm'): void
}>()

const resolvedConfirmVariant = computed<ButtonVariant>(() => {
  if (props.confirmVariant) return props.confirmVariant
  if (props.tone === 'danger') return 'danger'
  if (props.tone === 'warning') return 'warning'
  return 'primary'
})

defineOptions({ name: 'ConfirmAction' })
</script>
