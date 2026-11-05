<template>
  <details ref="disclosure" class="evidence-item-disclosure" :open="open" @toggle.self="open = ($event.target as HTMLDetailsElement).open">
    <summary ref="summary">{{ item.title || (item.kind === 'http' ? 'HTTP 交互' : item.kind === 'text' ? '文本材料' : '文件材料') }}</summary>
    <div v-if="open" class="evidence-item-disclosure__body">
      <slot />
      <EvidenceReader
        :entity-key="entityKey"
        :project-id="projectId"
        :source="source"
        :items="[item]"
        :selected-item-id="item.id"
        :load-preview="loadPreview"
        :original-url="originalUrl"
        :image-url="imageUrl"
        :artifact-size="artifactSize"
        :reading-states="readingStates"
        presentation="step"
      />
    </div>
  </details>
</template>

<script setup lang="ts">
import { nextTick, ref, watch } from 'vue'
import type { ArtifactPreview } from '@/api/client'
import type { EvidenceItem, EvidenceRef, EvidenceSource } from '@/api/types'
import EvidenceReader, { type EvidenceReadingStates } from './EvidenceReader.vue'

const props = defineProps<{
  item: EvidenceItem
  entityKey: string
  projectId: string
  source?: EvidenceSource
  loadPreview: (reference: EvidenceRef, signal: AbortSignal) => Promise<ArtifactPreview>
  originalUrl: (reference: EvidenceRef) => string | null
  imageUrl?: (reference: EvidenceRef) => string | null
  artifactSize?: (reference: EvidenceRef) => number | null | undefined
  initialOpen?: boolean
}>()
const open = ref(Boolean(props.initialOpen))
const disclosure = ref<HTMLDetailsElement>()
const summary = ref<HTMLElement>()
// 收起卸载读取器，仅为同一来源条目保留轻量阅读位置，不保留大正文读取许可。
const readingStates: EvidenceReadingStates = new Map()
watch(() => [props.entityKey, props.item.id], () => { open.value = false; readingStates.clear() }, { flush: 'sync' })
async function reveal() {
  open.value = true
  await nextTick()
  disclosure.value?.scrollIntoView({ block: 'nearest', behavior: 'auto' })
  summary.value?.focus({ preventScroll: true })
}
defineExpose({ reveal })
</script>

<style scoped>
.evidence-item-disclosure { min-width: 0; border-top: 1px solid var(--border); }
.evidence-item-disclosure > summary { padding: var(--space-4) 0; cursor: pointer; overflow-wrap: anywhere; font-size: var(--text-md); font-weight: var(--font-medium); }
.evidence-item-disclosure > summary:hover { color: var(--accent); }
.evidence-item-disclosure > summary:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
.evidence-item-disclosure__body { min-width: 0; }
</style>
