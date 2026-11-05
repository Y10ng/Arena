<template>
  <Tooltip :content="description">
    <span class="project-consumption" tabindex="0" :aria-label="`累计 Token：${description}`">
      {{ value }}<span v-if="partial" class="project-consumption-partial"> · 部分</span>
    </span>
  </Tooltip>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { Tooltip } from '@/components/ui'
import type { ConsumptionSummary } from '@/api/types/consumption'
const props = defineProps<{ consumption?: ConsumptionSummary | null }>()
const known = computed(() => props.consumption && Object.values(props.consumption.usage).some((value) => value !== null))
const partial = computed(() => known.value && Boolean(props.consumption?.partial_responses || props.consumption?.unobserved_bindings))
const value = computed(() => !props.consumption ? '未加载' : !known.value ? '未记录' : new Intl.NumberFormat('zh-CN', { notation: 'compact', maximumFractionDigits: 1 }).format(props.consumption.tokens))
const description = computed(() => {
  const item = props.consumption
  if (!item) return '未加载'
  const amount = known.value ? `${item.tokens.toLocaleString('zh-CN')} Token` : item.responses ? '未记录（响应计量全未知）' : '未记录（无响应记录）'
  return [amount, item.partial_responses ? `${item.partial_responses} 条响应部分计量` : '', item.unobserved_bindings ? `${item.unobserved_bindings} 个绑定尚无回执` : ''].filter(Boolean).join('；')
})
</script>

<style scoped>
.project-consumption { white-space: nowrap; font-variant-numeric: tabular-nums; }
.project-consumption:focus-visible { outline: 2px solid var(--accent); outline-offset: 3px; }
.project-consumption-partial { color: var(--text-muted); font-size: var(--text-xs); }
</style>
