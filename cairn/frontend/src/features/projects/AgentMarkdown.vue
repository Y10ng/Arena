<template>
  <div class="agent-markdown" v-html="html" />
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { renderAgentMarkdown } from './agentMarkdown'

const props = defineProps<{ content: string }>()
const html = computed(() => renderAgentMarkdown(props.content))
</script>

<style scoped>
.agent-markdown { min-width: 0; color: var(--text); font-size: var(--text-reading); overflow-wrap: anywhere; line-height: var(--leading-reading); }
.agent-markdown :deep(> :first-child) { margin-top: 0; }
.agent-markdown :deep(> :last-child) { margin-bottom: 0; }
.agent-markdown :deep(p), .agent-markdown :deep(ul), .agent-markdown :deep(ol) { margin-block: var(--space-3); }
.agent-markdown :deep(ul), .agent-markdown :deep(ol) { padding-left: var(--space-5); }
.agent-markdown :deep(li + li) { margin-top: var(--space-1); }
.agent-markdown :deep(h1), .agent-markdown :deep(h2), .agent-markdown :deep(h3),
.agent-markdown :deep(h4), .agent-markdown :deep(h5), .agent-markdown :deep(h6) {
  margin: var(--space-5) 0 var(--space-2);
  font-size: var(--text-section);
  font-weight: var(--font-semibold);
  line-height: var(--leading-normal);
}
.agent-markdown :deep(h1) { font-size: var(--text-object); }
.agent-markdown :deep(h2) { font-size: var(--text-section); }
.agent-markdown :deep(code) { font-family: var(--font-mono); font-size: var(--text-body); }
.agent-markdown :deep(:not(pre) > code) { padding: 2px var(--space-1); border-radius: var(--radius-sm); background: var(--color-surface-inset); }
.agent-markdown :deep(pre) { overflow-x: auto; padding: var(--space-4); border: 1px solid var(--color-border-subtle); border-radius: var(--radius-md); background: var(--color-surface-inset); }
.agent-markdown :deep(pre code) { white-space: pre; overflow-wrap: normal; }
.agent-markdown :deep(blockquote) { margin-inline: 0; padding-left: var(--space-4); border-left: 2px solid var(--color-border); color: var(--color-text-secondary); }
.agent-markdown :deep(.agent-markdown-table) { overflow-x: auto; margin-block: var(--space-4); }
.agent-markdown :deep(table) { border-collapse: collapse; width: 100%; font-size: 0.94em; }
.agent-markdown :deep(th), .agent-markdown :deep(td) { border-bottom: 1px solid var(--color-border); padding: var(--space-3) var(--space-4); text-align: left; min-width: 6rem; }
.agent-markdown :deep(th) { background: var(--color-surface-inset); font-weight: var(--font-medium); }
.agent-markdown :deep(hr) { border: 0; border-top: 1px solid var(--color-border-subtle); margin-block: var(--space-5); }
</style>
