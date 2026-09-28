<template>
  <!-- eslint-disable-next-line vue/no-v-html -- 报告内容仅由禁用 HTML 与远程资源的 markdown-it 实例生成。 -->
  <article class="safe-report-markdown" v-html="renderedContent" />
</template>

<script setup lang="ts">
import MarkdownIt from 'markdown-it'
import { computed } from 'vue'

const props = defineProps<{
  content: string
}>()

const safeProtocols = new Set(['http:', 'https:', 'mailto:'])
const markdown = new MarkdownIt({
  breaks: false,
  html: false,
  linkify: false,
  typographer: false,
})
const defaultValidateLink = markdown.validateLink.bind(markdown)
const defaultLinkOpen =
  markdown.renderer.rules.link_open ??
  ((tokens, index, options, _environment, renderer) => renderer.renderToken(tokens, index, options))

markdown.validateLink = (url) => {
  if (!defaultValidateLink(url)) return false

  const protocol = /^[a-z][a-z\d+.-]*:/i.exec(url)?.[0].toLowerCase()
  return !protocol || safeProtocols.has(protocol)
}

markdown.renderer.rules.image = (tokens, index) =>
  markdown.utils.escapeHtml(tokens[index]?.content || '')
markdown.renderer.rules.link_open = (tokens, index, options, environment, renderer) => {
  const href = tokens[index]?.attrGet('href') || ''
  if (/^(?:https?:)?\/\//i.test(href)) {
    tokens[index]?.attrSet('target', '_blank')
    tokens[index]?.attrSet('rel', 'noopener noreferrer')
  }
  return defaultLinkOpen(tokens, index, options, environment, renderer)
}

const renderedContent = computed(() => markdown.render(props.content))
</script>

<style scoped>
.safe-report-markdown {
  min-width: 0;
  max-width: 100%;
  color: var(--color-text);
  font-size: 0.875rem;
  line-height: 1.7;
  overflow-wrap: anywhere;
  user-select: text;
}

.safe-report-markdown :deep(> :first-child) {
  margin-top: 0;
}

.safe-report-markdown :deep(> :last-child) {
  margin-bottom: 0;
}

.safe-report-markdown :deep(h1),
.safe-report-markdown :deep(h2),
.safe-report-markdown :deep(h3),
.safe-report-markdown :deep(h4),
.safe-report-markdown :deep(h5),
.safe-report-markdown :deep(h6) {
  margin: 1.5em 0 0.6em;
  color: var(--color-text);
  line-height: 1.3;
}

.safe-report-markdown :deep(p),
.safe-report-markdown :deep(ul),
.safe-report-markdown :deep(ol),
.safe-report-markdown :deep(blockquote),
.safe-report-markdown :deep(pre),
.safe-report-markdown :deep(table) {
  margin: 0 0 1em;
}

.safe-report-markdown :deep(ul),
.safe-report-markdown :deep(ol) {
  padding-left: 1.5rem;
}

.safe-report-markdown :deep(a) {
  color: var(--color-link);
  text-decoration: underline;
  text-underline-offset: 0.15em;
}

.safe-report-markdown :deep(a:hover) {
  color: var(--color-link-hover);
}

.safe-report-markdown :deep(blockquote) {
  padding-left: var(--space-3);
  border-left: 3px solid var(--color-border);
  color: var(--color-text-secondary);
}

.safe-report-markdown :deep(code) {
  padding: 0.1em 0.3em;
  border-radius: var(--radius-sm);
  background: var(--color-surface-inset);
  font-size: 0.9em;
}

.safe-report-markdown :deep(pre) {
  max-width: 100%;
  padding: var(--space-3);
  overflow: auto;
  border-radius: var(--radius-md);
  background: var(--color-surface-inset);
}

.safe-report-markdown :deep(pre code) {
  padding: 0;
  background: transparent;
  white-space: pre;
}

.safe-report-markdown :deep(table) {
  display: block;
  width: 100%;
  max-width: 100%;
  overflow-x: auto;
  border-collapse: collapse;
}

.safe-report-markdown :deep(th),
.safe-report-markdown :deep(td) {
  padding: var(--space-2) var(--space-3);
  border: 1px solid var(--color-border);
  text-align: left;
  vertical-align: top;
}

.safe-report-markdown :deep(hr) {
  margin: 1.5rem 0;
  border: 0;
  border-top: 1px solid var(--color-border);
}

@media (max-width: 720px) {
  .safe-report-markdown {
    font-size: 0.8125rem;
  }

  .safe-report-markdown :deep(th),
  .safe-report-markdown :deep(td) {
    padding: var(--space-2);
  }
}
</style>
