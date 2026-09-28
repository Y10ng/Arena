<template>
  <Alert class="error-state" tone="danger" :title="title">
    <template v-if="$slots.icon" #icon>
      <slot name="icon" />
    </template>
    {{ description }}
    <template #actions>
      <Button variant="secondary" size="sm" @click="emit('retry')">
        {{ retryLabel }}
      </Button>
    </template>
  </Alert>
</template>

<script setup lang="ts">
import Alert from '../ui/Alert.vue'
import Button from '../ui/Button.vue'

withDefaults(
  defineProps<{
    title?: string
    description?: string
    retryLabel?: string
  }>(),
  {
    title: '操作失败',
    description: '',
    retryLabel: '重试',
  },
)

const emit = defineEmits<{
  (event: 'retry'): void
}>()

defineOptions({ name: 'ErrorState' })
</script>

<style scoped>
.error-state {
  width: 100%;
}
</style>
