<template>
  <article class="safe-report-markdown">
    <!-- eslint-disable-next-line vue/no-v-html -- HTML 禁用，图片只能由已授权 Blob 映射注入。 -->
    <div v-html="document.html" @error.capture="imageFailed" />
    <Button v-if="images?.length && loadImage && !imagesLoaded" size="sm" variant="ghost" :loading="imagesLoading" @click="showImages">显示证据图片</Button>
  </article>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, ref, shallowRef, watch } from 'vue'
import { Button } from '@/components/ui'
import { renderSafeReportMarkdown, type ReportHeading, type ReportImageSources } from './safeReportMarkdown'
import { loadReportImages, withDisplayImages, type ReportDisplayImage, type ReportImageLoader } from './reportImages'

const props = defineProps<{
  content: string
  anchorPrefix?: string
  identity?: string
  images?: ReportDisplayImage[]
  loadImage?: ReportImageLoader
}>()
const emit = defineEmits<{
  headings: [headings: ReportHeading[]]
}>()
const sources = shallowRef<ReportImageSources>(new Map())
const imagesLoaded = ref(false)
const imagesLoading = ref(false)
let controller: AbortController | undefined
const objectUrls = new Set<string>()
function clearImages() {
  controller?.abort()
  for (const url of objectUrls) URL.revokeObjectURL(url)
  objectUrls.clear()
  sources.value = new Map()
  imagesLoaded.value = false
  imagesLoading.value = false
}
async function showImages() {
  if (!props.loadImage || imagesLoading.value) return
  controller = new AbortController()
  const signal = controller.signal
  imagesLoading.value = true
  try {
    const loaded = await loadReportImages(props.images || [], props.loadImage, signal)
    if (signal.aborted) return
    sources.value = new Map(loaded.map(({ image, blob, error }) => {
      const url = blob ? URL.createObjectURL(blob) : undefined
      if (url) objectUrls.add(url)
      return [`arena-image:${image.artifact_id}`, { url, error }]
    }))
    imagesLoaded.value = true
  } catch (error) {
    if (!signal.aborted) sources.value = new Map((props.images || []).map((image) => [`arena-image:${image.artifact_id}`, { error: error instanceof Error ? error.message : '图片读取失败。' }]))
  } finally {
    if (!signal.aborted) imagesLoading.value = false
  }
}
function imageFailed(event: Event) {
  const target = event.target
  if (!(target instanceof HTMLImageElement)) return
  const next = new Map(sources.value)
  for (const [key, value] of next) if (value.url === target.src) {
    URL.revokeObjectURL(value.url)
    objectUrls.delete(value.url)
    next.set(key, { error: '图片无法显示；正文和原文证据仍可使用。' })
  }
  sources.value = next
}
watch(() => [props.identity, props.images, props.loadImage], clearImages, { flush: 'sync' })
onBeforeUnmount(clearImages)
const document = computed(() => {
  const images: ReportImageSources = new Map((props.images || []).map((image) => [`arena-image:${image.artifact_id}`, { error: image.unavailable_reason || undefined }]))
  for (const [key, value] of sources.value) images.set(key, value)
  return renderSafeReportMarkdown(withDisplayImages(props.content, props.images || []), props.anchorPrefix, images)
})
watch(document, (value) => emit('headings', value.headings), { immediate: true })
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

.safe-report-markdown :deep(> div > :first-child) {
  margin-top: 0;
}

.safe-report-markdown :deep(> div > :last-child) {
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

.safe-report-markdown :deep(img) {
  display: block;
  max-width: 100%;
  height: auto;
  margin-block: var(--space-3);
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
