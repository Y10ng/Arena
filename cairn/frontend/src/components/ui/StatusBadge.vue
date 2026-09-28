<template>
  <Badge :tone="tone" :size="size" :dot="dot">
    <slot :meta="meta">{{ meta.text }}</slot>
  </Badge>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { statusMeta } from '@/lib/format'
import Badge from './Badge.vue'
import type { BadgeSize, BadgeTone } from './Badge.vue'

const props = withDefaults(
  defineProps<{
    status: string | number | null
    size?: BadgeSize
    dot?: boolean
  }>(),
  {
    size: 'md',
    dot: false,
  },
)

const meta = computed(() => statusMeta(props.status))
const tone = computed<BadgeTone>(() => {
  const tones: Record<string, BadgeTone> = {
    good: 'success',
    bad: 'danger',
    warn: 'warning',
    info: 'info',
    neutral: 'neutral',
  }
  return tones[meta.value.tone] ?? 'neutral'
})
</script>
