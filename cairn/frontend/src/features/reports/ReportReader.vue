<template>
  <section class="report-reader" :class="{ 'report-reader--embedded': embedded }" :aria-label="`${documentLabel}阅读`">
    <header class="report-reader-toolbar">
      <div class="report-reader-title"><slot name="title" /></div>
      <div class="report-reader-actions">
        <Button
          v-if="ready && mode === 'rendered' && headings.length"
          ref="directoryToggle"
          class="report-reader-directory-toggle"
          variant="secondary"
          size="sm"
          :aria-expanded="directoryOpen"
          :aria-controls="`${readerId}-directory`"
          @click="directoryOpen = !directoryOpen"
          >目录</Button
        >
        <Button
          v-if="ready"
          variant="ghost"
          size="sm"
          :aria-pressed="mode === 'raw'"
          @click="mode = mode === 'raw' ? 'rendered' : 'raw'"
          >{{ mode === 'raw' ? '排版' : '原文' }}</Button
        >
        <slot name="actions" />
      </div>
    </header>
    <slot name="notice" />
    <div v-if="!ready" class="report-reader-state"><slot name="state" /></div>
    <div
      v-else
      class="report-reader-body"
      :class="{ 'report-reader-body--with-directory': mode === 'rendered' && headings.length && !embedded, 'report-reader-body--directory-open': directoryOpen && embedded }"
    >
      <nav
        v-if="mode === 'rendered' && headings.length && (!embedded || directoryOpen)"
        :id="`${readerId}-directory`"
        class="report-reader-directory"
        :class="{ 'report-reader-directory--open': directoryOpen }"
        :aria-label="`当前${documentLabel}章节目录`"
        @keydown.esc="closeDirectory"
      >
        <strong>章节目录</strong>
        <ol>
          <li v-for="heading in headings" :key="heading.id">
            <button
              type="button"
              :class="{ 'report-reader-subheading': heading.level > firstLevel }"
              :aria-current="activeHeading === heading.id ? 'location' : undefined"
              @click="jumpToHeading(heading.id)"
            >
              {{ heading.title || '无标题章节' }}
            </button>
          </li>
        </ol>
      </nav>
      <div
        ref="scrollPane"
        class="report-reader-scroll"
        tabindex="0"
        :aria-label="`${documentLabel}${mode === 'raw' ? '原文' : '正文'}`"
        @scroll="updateCurrentHeading"
        @click="handleContentClick"
      >
        <SafeReportMarkdown
          v-if="mode === 'rendered'"
          :key="identity"
          :content="content"
          :anchor-prefix="readerId"
          :identity="identity"
          :images="images"
          :load-image="loadImage"
          @headings="updateHeadings"
        />
        <pre v-else class="report-reader-raw">{{ content }}</pre>
      </div>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, ref, useId, watch } from 'vue'
import { Button } from '@/components/ui'
import { toLogicalPixels } from '@/lib/uiScale'
import SafeReportMarkdown from './SafeReportMarkdown.vue'
import type { ReportHeading } from './safeReportMarkdown'
import type { ReportDisplayImage, ReportImageLoader } from './reportImages'

const props = withDefaults(
  defineProps<{
    content: string
    identity: string
    images?: ReportDisplayImage[]
    loadImage?: ReportImageLoader
    ready?: boolean
    embedded?: boolean
    documentLabel?: '报告' | '文件'
  }>(),
  { ready: true, embedded: false, documentLabel: '报告' },
)
const mode = defineModel<'rendered' | 'raw'>('mode', { default: 'rendered' })
const readerId = `report-${useId()}`
const headings = ref<ReportHeading[]>([])
const firstLevel = computed(() => Math.min(...headings.value.map((heading) => heading.level)))
const activeHeading = ref('')
const directoryOpen = ref(false)
const directoryToggle = ref<InstanceType<typeof Button> | null>(null)
function closeDirectory(event: KeyboardEvent) {
  if (!directoryOpen.value) return
  event.preventDefault()
  event.stopPropagation()
  directoryOpen.value = false
  directoryToggle.value?.$el.focus()
}
const scrollPane = ref<HTMLElement | null>(null)
const positions = { rendered: 0, raw: 0 }
let mounted = true
let resettingIdentity = false

function updateHeadings(value: ReportHeading[]) {
  headings.value = value
  void nextTick(() => {
    if (mounted) updateCurrentHeading()
  })
}

function headingElement(id: string) {
  const pane = scrollPane.value
  const element = pane?.ownerDocument.getElementById(id)
  return element && pane?.contains(element) ? element : null
}

function jumpToHeading(id: string) {
  const pane = scrollPane.value
  const heading = headingElement(id)
  if (!pane || !heading) return
  // 只移动正文所属滚动区，不能用 hash 锚点覆盖业务路由。
  pane.scrollTop += toLogicalPixels(heading.getBoundingClientRect().top - pane.getBoundingClientRect().top) - 16
  heading.focus({ preventScroll: true })
  activeHeading.value = id
  directoryOpen.value = false
}

function updateCurrentHeading() {
  const pane = scrollPane.value
  if (!pane) return
  positions[mode.value] = pane.scrollTop
  if (mode.value !== 'rendered') return
  const top = pane.getBoundingClientRect().top
  let current = headings.value[0]?.id ?? ''
  for (const heading of headings.value) {
    const element = headingElement(heading.id)
    if (element && toLogicalPixels(element.getBoundingClientRect().top - top) <= 24) current = heading.id
  }
  activeHeading.value = current
}

function handleContentClick(event: MouseEvent) {
  const target = event.target instanceof Element ? event.target.closest('a') : null
  const href = target?.getAttribute('href')
  if (!href?.startsWith('#')) return
  event.preventDefault()
  const heading = headings.value.find((item) => `#${item.id}` === href)
  if (heading) jumpToHeading(heading.id)
}

watch(mode, (value, previous) => {
  if (resettingIdentity) return
  if (scrollPane.value) positions[previous] = scrollPane.value.scrollTop
  directoryOpen.value = false
  void nextTick(() => {
    if (mounted && scrollPane.value) scrollPane.value.scrollTop = positions[value]
  })
})
watch(
  () => props.identity,
  () => {
    resettingIdentity = true
    headings.value = []
    activeHeading.value = ''
    directoryOpen.value = false
    mode.value = 'rendered'
    positions.rendered = 0
    positions.raw = 0
    void nextTick(() => {
      if (mounted && scrollPane.value) scrollPane.value.scrollTop = 0
      resettingIdentity = false
    })
  },
  { flush: 'sync' },
)
onBeforeUnmount(() => {
  mounted = false
})
</script>

<style scoped>
.report-reader {
  min-width: 0;
  min-height: 0;
  flex: 1 1 0;
  display: flex;
  flex-direction: column;
  container: report-reader / inline-size;
  background: var(--bg-surface);
}
.report-reader-toolbar,
.report-reader-actions {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: var(--space-2);
}
.report-reader-toolbar {
  flex: 0 0 auto;
  justify-content: space-between;
  padding: var(--space-3) var(--space-4);
  border-bottom: 1px solid var(--border-subtle);
}
.report-reader-title {
  min-width: 0;
  flex: 1 1 200px;
}
.report-reader-actions {
  justify-content: flex-end;
}
.report-reader-title :deep(h1),
.report-reader-title :deep(h2) {
  margin: 0;
  font-size: var(--text-object);
  font-weight: var(--font-semibold);
  overflow-wrap: anywhere;
}
.report-reader-body {
  min-width: 0;
  min-height: 0;
  flex: 1 1 0;
  display: grid;
  grid-template-columns: minmax(0, 1fr);
  position: relative;
}
.report-reader-body--with-directory {
  grid-template-columns: 250px minmax(0, 1fr);
}
.report-reader-directory {
  min-width: 0;
  min-height: 0;
  overflow-y: auto;
  padding: var(--space-3);
  border-right: 1px solid var(--border-subtle);
}
.report-reader-directory > strong {
  display: block;
  padding: var(--space-2);
  font-size: var(--text-sm);
  color: var(--text-muted);
}
.report-reader-directory ol {
  list-style: none;
  margin: 0;
  padding: 0;
}
.report-reader-directory button {
  width: 100%;
  min-height: var(--control-height-sm);
  padding: var(--space-2);
  border: 0;
  border-radius: var(--radius-sm);
  background: transparent;
  color: var(--text-muted);
  font: inherit;
  font-size: var(--text-body);
  line-height: var(--leading-normal);
  transition: background-color var(--dur), color var(--dur);
  text-align: left;
  overflow-wrap: anywhere;
  cursor: pointer;
}
.report-reader-directory button:hover {
  background: var(--surface-hover);
  color: var(--text);
}
.report-reader-directory button[aria-current='location'] {
  background: var(--surface-selected);
  color: var(--accent);
}
.report-reader-directory button.report-reader-subheading {
  padding-left: var(--space-4);
}
.report-reader-directory button:focus-visible,
.report-reader-scroll:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: -2px;
}
.report-reader-scroll {
  min-width: 0;
  min-height: 0;
  overflow-y: auto;
  padding: var(--space-5) var(--space-6);
  overscroll-behavior-y: contain;
  scrollbar-gutter: stable;
}
.report-reader-scroll :deep(.safe-report-markdown) {
  max-width: none;
  font-size: var(--text-reading);
  line-height: var(--leading-reading);
}
.report-reader-scroll :deep(.safe-report-markdown > div > :is(p, ul, ol, blockquote)) {
  max-width: 55em;
}
.report-reader-scroll :deep([tabindex='-1']:focus) {
  outline: none;
}
.report-reader-raw {
  margin: 0;
  color: var(--text);
  font: var(--text-body)/var(--leading-reading) var(--font-mono);
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}
.report-reader-state {
  min-height: 0;
  flex: 1 1 0;
  overflow-y: auto;
  padding: var(--space-4);
  display: grid;
  place-items: center;
}
.report-reader-directory-toggle.ui-button {
  display: none;
}
@container report-reader (max-width: 740px) {
  .report-reader-directory-toggle.ui-button {
    display: inline-flex;
  }
  .report-reader-body--with-directory {
    grid-template-columns: minmax(0, 1fr);
  }
  .report-reader-directory {
    display: none;
  }
  .report-reader-directory--open {
    display: block;
    position: absolute;
    inset: 0 auto 0 0;
    width: min(280px, 85%);
    background: var(--bg-surface);
    box-shadow: var(--shadow-pop);
    z-index: 1;
  }
  .report-reader-scroll {
    padding: var(--space-4);
  }
}
.report-reader--embedded { background: transparent; }
.report-reader--embedded .report-reader-toolbar { padding: var(--space-3) 0; }
.report-reader--embedded .report-reader-directory-toggle.ui-button { display: inline-flex; }
.report-reader--embedded .report-reader-body--directory-open { grid-template-rows: auto minmax(0, 1fr); }
.report-reader--embedded .report-reader-directory { position: static; display: block; width: auto; max-height: 180px; padding: var(--space-2) 0; border-right: 0; border-bottom: 1px solid var(--border-subtle); background: transparent; box-shadow: none; }
.report-reader--embedded .report-reader-directory > strong { display: none; }
.report-reader--embedded .report-reader-scroll { padding: var(--space-4) 0; }
.report-reader--embedded .report-reader-state { padding-inline: 0; }
.report-reader--embedded .report-reader-title :deep(h2) { font-size: var(--text-section); }
@media (prefers-reduced-motion: reduce) { .report-reader-directory button { transition: none; } }
</style>
