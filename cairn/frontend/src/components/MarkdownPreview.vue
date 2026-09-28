<template>
  <article class="markdown-preview">
    <pre class="markdown-preview__content">{{ content }}</pre>
  </article>
</template>

<script setup lang="ts">
defineProps<{
  content: string
}>()
</script>

<style scoped>
.markdown-preview {
  height: 100%;
  min-width: 0;
  overflow: auto;
  padding: var(--space-4);
  color: var(--text);
  background-color: var(--bg-inset);
  font-size: 13px;
  line-height: 1.7;
}

.markdown-preview__content {
  min-width: 0;
  margin: 0;
  overflow-wrap: anywhere;
  color: inherit;
  font: inherit;
  white-space: pre-wrap;
  user-select: text;
}
</style>
