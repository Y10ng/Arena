<template>
  <section ref="reader" class="evidence-reader" :class="{ 'evidence-reader--step': presentation === 'step', 'evidence-reader--fill': fillHeight }" :aria-label="presentation === 'step' ? '步骤证据' : '关键证据'">
    <header v-if="!isHttp && (presentation === 'standalone' || $slots['header-context'] || $slots['header-actions'])" class="evidence-reader__header">
      <slot name="header-context"><h3 v-if="presentation === 'standalone'">关键证据</h3></slot>
      <span v-if="source" class="evidence-reader__source">{{ sourceLabels[source] }}</span>
      <div v-if="$slots['header-actions']" class="evidence-reader__header-actions"><slot name="header-actions" /></div>
    </header>
    <nav v-if="!isHttp && (presentation === 'step' ? items.length > 1 : primary.length > 1 || (primary.length === 1 && supporting.length > 0))" class="evidence-reader__navigation" aria-label="关键交互">
      <Button v-for="(item, index) in presentation === 'step' ? items : primary" :key="item.id" size="sm" :variant="selectedId === item.id ? 'secondary' : 'ghost'" :aria-pressed="selectedId === item.id" @click="selectItem(item.id)">{{ item.title || `交互 ${index + 1}` }}</Button>
    </nav>
    <details v-if="!isHttp && presentation === 'standalone' && supporting.length" class="evidence-reader__supporting">
      <summary>补充证据 {{ supporting.length }}</summary>
      <nav class="evidence-reader__navigation" aria-label="补充证据"><Button v-for="(item, index) in supporting" :key="item.id" size="sm" :variant="selectedId === item.id ? 'secondary' : 'ghost'" :aria-pressed="selectedId === item.id" @click="selectItem(item.id)">{{ item.title || `补充 ${index + 1}` }}</Button></nav>
    </details>
    <template v-if="selected">
      <div v-if="!isHttp && presentation === 'standalone'" class="evidence-reader__identity"><strong v-if="selected.title">{{ selected.title }}</strong><span>{{ sourceLabels[selected.source] }}</span></div>
      <div class="evidence-reader__packets" :class="{ 'evidence-reader__packets--http': isHttp }">
        <header v-if="isHttp" class="evidence-reader__summary">
          <div v-if="$slots['header-context']" class="evidence-reader__header-context"><slot name="header-context" /></div>
          <h3 v-else-if="presentation === 'standalone' && items.length === 1">HTTP 证据</h3>
          <nav v-if="items.length > 1" class="evidence-reader__interaction" aria-label="证据交互导航">
            <Button class="evidence-reader__icon" variant="ghost" size="sm" aria-label="上一交互" :disabled="selectedIndex <= 0" @click="selectItem(items[selectedIndex - 1]!.id)"><ChevronLeft :size="15" /></Button>
            <span class="evidence-reader__position">{{ selectedIndex + 1 }}/{{ items.length }}</span>
            <Select :model-value="selectedId" :options="interactionOptions" aria-label="选择证据交互" @update:model-value="selectItem($event)" />
            <Button class="evidence-reader__icon" variant="ghost" size="sm" aria-label="下一交互" :disabled="selectedIndex >= items.length - 1" @click="selectItem(items[selectedIndex + 1]!.id)"><ChevronRight :size="15" /></Button>
          </nav>
          <span v-else class="evidence-reader__target" :title="requestTarget || selected.title">{{ requestTarget || selected.title }}</span>
          <Popover v-if="flowId" :key="`${entityKey}:${selected.id}:identity`" align="end" size="md">
            <template #trigger><Button class="evidence-reader__info" variant="ghost" size="sm" aria-label="结构化重建说明"><Info :size="14" />结构化</Button></template>
            <div class="evidence-reader__preview-details">
              <p>结构化重建，非原始字节流。</p>
              <template v-if="flow && !fillHeight">
                <p>flow {{ flow.id }} · revision {{ flow.revision }}</p>
                <p>源执行 {{ flow.scope.task_run_id }} · scope {{ flow.scope.id }} · generation {{ flow.scope.generation }}</p>
                <p>采集状态：{{ flowStates[flow.source_state] }}；读取状态：{{ flowStates[flow.state] }}</p>
              </template>
              <section v-for="panel in displayedPanels.filter((entry) => entry.readable)" :key="`${panel.key}-readable`">
                <strong>{{ panel.label }}阅读来源</strong>
                <p>正文为解码后的派生视图；投影为 http-evidence-v1，保留原选段坐标，可能仅含正文占位。两者均非完整网络原包。</p>
                <p>{{ panel.readable!.body_artifact_id }} · SHA-256 {{ panel.readable!.body_sha256 }}</p>
              </section>
              <section v-for="panel in displayedPanels.filter((entry) => entry.projection)" :key="panel.key">
                <strong>{{ panel.label }}投影</strong>
                <p>{{ panel.projection!.representation }} · revision {{ panel.projection!.revision }}</p>
                <p>SHA-256 {{ panel.projection!.sha256 }}</p>
                <p>正文 {{ panel.projection!.body_artifact_id }} · SHA-256 {{ panel.projection!.body_sha256 }}</p>
              </section>
            </div>
          </Popover>
          <span v-if="flow" class="evidence-reader__flow-state" :class="{ 'evidence-reader__flow-state--warning': flow.state !== 'complete' || flow.source_state === 'failed' }">{{ flowStates[flow.source_state === 'failed' ? 'failed' : flow.state] }}</span>
          <span v-if="!flowId || selected.source !== 'capture'" class="evidence-reader__source">{{ sourceLabels[selected.source] }}</span>
          <div v-if="$slots['header-actions']" class="evidence-reader__header-actions"><slot name="header-actions" /></div>
          <Button v-if="pairOverflows || pairExpanded" class="evidence-reader__icon" variant="ghost" size="sm" :aria-label="pairExpanded ? '收起成对报文' : '放大成对报文'" :title="pairExpanded ? '收起成对报文' : '放大成对报文'" :aria-expanded="pairExpanded" @click="pairExpanded = !pairExpanded"><Minimize2 v-if="pairExpanded" :size="15" /><Maximize2 v-else :size="15" /></Button>
        </header>
        <template v-if="flowId">
          <p v-if="flowLoading && !flow" class="evidence-reader__notice" role="status">正在读取流量来源…</p>
          <p v-else-if="flowError" class="evidence-reader__notice" role="alert">{{ flowError }}<span v-if="lastFlowRefresh"> · 上次成功 {{ lastFlowRefresh }}</span><Button size="sm" variant="ghost" @click="refreshStopped = false; refreshFailures = 0; loadFlow()">重试</Button></p>
          <p v-else-if="flow?.state === 'receiving'" class="evidence-reader__notice">{{ flow.source_state === 'receiving' ? '仍在接收' : '来源已结束，仍在同步' }} · 当前预览非稳定证据<Button v-if="!autoRefresh" size="sm" variant="ghost" @click="loadFlow()">刷新状态</Button></p>
        </template>
        <div v-if="isHttp" class="evidence-reader__side-switch" role="group" aria-label="报文视图">
          <Button v-for="panel in panels" :key="panel.key" size="sm" :variant="activeSide === panel.key ? 'secondary' : 'ghost'" :aria-pressed="activeSide === panel.key" @click="selectSide(panel.key)">{{ panel.label }}<span v-if="!flowId && !panel.reference && panel.inline === null"> · 未记录</span></Button>
        </div>
        <div ref="panelContainer" class="evidence-reader__panels" :class="{ 'evidence-reader__panels--http': isHttp }" :style="isHttp ? { '--request-share': `${splitRatio}%` } : undefined">
          <div v-if="isHttp" class="evidence-reader__separator" role="separator" tabindex="0" aria-label="调整请求与响应宽度" aria-orientation="vertical" :aria-valuenow="Math.round(splitRatio)" :aria-valuemin="splitBounds.min" :aria-valuemax="splitBounds.max" @pointerdown="startSplit" @keydown="resizeSplit" />
          <article v-for="panel in displayedPanels" :key="`${entityKey}:${selected.id}:${panel.key}`" class="evidence-reader__panel" :class="{ 'evidence-reader__panel--selected': activeSide === panel.key }" :aria-label="`${panel.label}阅读器`">
            <header class="evidence-reader__toolbar" :class="{ 'evidence-reader__toolbar--compact': compactPanel(panel) }">
              <h4>{{ panel.label }}</h4>
              <span v-if="isHttp && panel.summary.marker" class="evidence-reader__protocol" :title="panel.summary.marker">{{ panel.summary.marker }}</span>
              <template v-if="panel.content !== null">
                <div class="evidence-reader__modes" role="group" :aria-label="`${panel.label}显示模式`">
                  <span v-if="compactPanel(panel)" class="evidence-reader__current-mode">{{ modeLabels[panel.state.mode] }}</span>
                  <template v-else>
                    <Button v-if="flowId" variant="ghost" size="sm" :aria-pressed="panel.state.mode === 'readable'" @click="switchMode(panel, 'readable')">正文</Button>
                    <Button variant="ghost" size="sm" :aria-pressed="panel.state.mode === 'raw'" @click="switchMode(panel, 'raw')">{{ flowId ? '投影' : 'Raw' }}</Button>
                  </template>
                  <Button v-if="!compactPanel(panel) && panel.view.prettyAvailable && panel.state.mode !== 'hex'" variant="ghost" size="sm" :aria-pressed="panel.state.mode === 'pretty'" title="仅格式化正文，不改原文" @click="switchMode(panel, 'pretty')">Pretty</Button>
                  <Button v-if="flowId && !compactPanel(panel)" variant="ghost" size="sm" :aria-pressed="panel.state.mode === 'hex'" title="查看正文原字节" @click="switchMode(panel, 'hex')">Hex</Button>
                </div>
                <span v-if="panel.readable?.decoded && ['complete', 'too-large'].includes(panel.readable.decode_status)" class="evidence-reader__protocol" title="解码派生视图，原头部与正文原件未改写">已解压</span>
                <div class="evidence-reader__tools">
                  <Button class="evidence-reader__icon" variant="ghost" size="sm" :aria-label="`${panel.label}搜索`" :aria-pressed="panel.state.searchOpen" title="搜索已载入文本" @click="panel.state.searchOpen = !panel.state.searchOpen"><Search :size="15" /></Button>
                  <PopoverRoot :open="copyFeedback[panel.key]?.open ?? false" @update:open="setCopyFeedbackOpen(panel.key, $event)">
                    <PopoverAnchor as-child><Button :id="`${readerId}-${panel.key}-copy-action`" class="evidence-reader__icon" variant="ghost" size="sm" :aria-label="`${panel.label}${copyLabel(panel)}`" :title="copyLabel(panel)" aria-haspopup="dialog" :aria-controls="copyFeedback[panel.key]?.open ? `${readerId}-${panel.key}-copy` : undefined" :aria-expanded="copyFeedback[panel.key]?.open ?? false" @click="copy(panel)" @keydown="enterCopyFeedback(panel.key, $event)"><Copy :size="15" /></Button></PopoverAnchor>
                    <PopoverPortal>
                      <PopoverContent class="ui-popover__content ui-popover__content--md evidence-reader__copy-feedback" :class="{ 'evidence-reader__copy-feedback--error': copyFeedback[panel.key]?.status === 'error' }" side="bottom" align="end" :side-offset="6" :collision-padding="8" :aria-label="`${panel.label}复制反馈`" @open-auto-focus.prevent @close-auto-focus.prevent @escape-key-down="focusCopyAction(panel.key)">
                        <div :id="`${readerId}-${panel.key}-copy`" class="evidence-reader__copy-body">
                          <span>{{ copyFeedback[panel.key]?.message }}</span>
                          <Button v-if="copyFeedback[panel.key]?.status === 'error'" variant="link" size="sm" @click="selectRaw(panel)">选择当前文本手工复制</Button>
                        </div>
                      </PopoverContent>
                    </PopoverPortal>
                  </PopoverRoot>
                  <span v-if="copyFeedback[panel.key]" class="evidence-reader__live" role="status" aria-live="polite" aria-atomic="true">{{ copyFeedback[panel.key]?.message }}</span>
                  <Button v-if="!isHttp && (overflowPanels[panel.key] || panel.state.expanded)" class="evidence-reader__icon" variant="ghost" size="sm" :aria-label="`${panel.label}${panel.state.expanded ? '收起报文' : '放大报文'}`" :title="panel.state.expanded ? '收起报文' : '放大报文'" :aria-expanded="panel.state.expanded" @click="panel.state.expanded = !panel.state.expanded"><Minimize2 v-if="panel.state.expanded" :size="15" /><Maximize2 v-else :size="15" /></Button>
                </div>
              </template>
              <span v-if="panel.readable?.text_escaped" class="evidence-reader__protocol">转义</span>
              <span v-if="isTruncated(panel)" class="evidence-reader__protocol">片段</span>
              <DropdownMenu v-if="panel.content !== null || (panel.downloadUrl)" :items="panelMenuItems(panel)" :label="`${panel.label}选项`" @select="selectPanelOption(panel, $event)">
                <template #trigger><Button class="evidence-reader__icon" variant="ghost" size="sm" :aria-label="`${panel.label}更多`" title="更多"><Ellipsis :size="16" /></Button></template>
                <DropdownMenuItem v-if="panel.downloadUrl" as-child class="ui-dropdown-menu__item"><a :href="panel.downloadUrl || undefined" target="_blank" rel="noopener" download>{{ flowId ? '下载正文原件' : '下载原件' }}</a></DropdownMenuItem>
                <p v-if="panel.body" class="evidence-reader__menu-hint">正文 Hex / ASCII 显示已保存的正文原字节，可能含内容编码，并非完整报文或传输帧。</p>
                <p v-else-if="panel.view.reason" class="evidence-reader__menu-hint">{{ panel.view.reason }}</p>
                <p v-else-if="panel.state.mode === 'pretty'" class="evidence-reader__menu-hint">格式化视图仅调整 JSON 空白，复制仍为原文。</p>
              </DropdownMenu>
            </header>
            <div class="evidence-reader__content" :data-evidence-body="panel.key" tabindex="-1" :aria-label="`${panel.label}正文`">
              <p v-if="flow && panel.key !== 'artifact' && flow.sync.bodies[panel.key].status === 'pending'" class="evidence-reader__notice">{{ panel.label }}正文尚未同步。</p>
              <template v-if="panel.reference && imageUrl?.(panel.reference) && previews[panel.key] && !previews[panel.key]?.deferred"><p v-if="imageErrors[panel.key]" class="evidence-reader__notice" role="alert">图像读取失败，请读取原件核对。</p><img v-else-if="imageSources[panel.key]" class="evidence-reader__image" :src="imageSources[panel.key]" :alt="selected.title || '本轮观察图像'" @error="imageErrors[panel.key] = true" /></template>
              <p v-else-if="flowId && flowSideNotice(panel)" class="evidence-reader__notice">{{ flowSideNotice(panel) }}</p>
              <p v-else-if="!flowId && !panel.reference && panel.inline === null" class="evidence-reader__notice">{{ isHttp ? `未记录${panel.label}` : '未记录证据内容' }}</p>
              <p v-else-if="previews[panel.key]?.loading" class="evidence-reader__notice" role="status">正在读取{{ panel.label }}…<Button size="sm" variant="ghost" @click="cancelPanel(panel)">取消读取</Button></p>
              <div v-else-if="previews[panel.key]?.error" class="evidence-reader__notice" role="alert">{{ previews[panel.key]?.error }}<Button size="sm" variant="ghost" @click="loadPanel(panel, previews[panel.key]?.offset, previews[panel.key]?.excerpt)">重试</Button></div>
              <MonacoEditor v-else-if="panel.content !== null" :ref="(editor) => registerEditor(panel.key, editor)" :key="`${entityKey}:${selected.id}:${panel.key}`" :model-value="panel.state.mode === 'pretty' ? panel.view.pretty ?? panel.content : panel.content" :language="panel.state.mode === 'hex' ? 'plaintext' : panel.view.language" :variant="isHttp ? 'http' : 'packet'" :http-density="isHttp ? 'compact' : undefined" :reading-state="panel.state" :reading-view="panel.state.mode === 'pretty' ? `${panel.state.textMode}-pretty` : panel.state.mode" read-only :word-wrap="panel.state.wrap ? 'on' : 'off'" :auto-height="!isHttp" :height="isHttp ? packetHeight : undefined" :max-height="panel.state.expanded ? '80dvh' : 'min(48dvh, 520px)'" :min-height="64" :aria-label="panel.label" @content-height-change="contentHeights[panel.key] = $event" @overflow-change="overflowPanels[panel.key] = $event" />
              <p v-else-if="previews[panel.key]?.preview" class="evidence-reader__notice">二进制内容不作文本预览，请读取原件。</p>
              <div v-if="previews[panel.key]?.deferred && !previews[panel.key]?.loading && !previews[panel.key]?.error" class="evidence-reader__pagination"><Button size="sm" variant="secondary" @click="loadBody(panel)">加载{{ panel.label }}{{ isHttp ? '正文' : '内容' }}</Button><span>{{ savedSizeLabel(panel) }}</span></div>
              <p v-if="readStatus(panel)" class="evidence-reader__notice" :role="panel.readable && ['invalid-encoding', 'unsupported-encoding'].includes(panel.readable.decode_status) ? 'alert' : 'status'">{{ readStatus(panel) }}</p>
              <template v-if="panel.live || panel.body">
                <p v-if="panel.body && !panel.body.accepted_bytes" class="evidence-reader__notice">{{ panel.body.sealed ? '正文为空。' : '尚无已同步字节。' }}</p>
                <p v-if="panel.live?.header_truncated" class="evidence-reader__notice">头部超过预览上限；当前仅显示头部前缀。</p>
                <div class="evidence-reader__pagination" :aria-label="`${panel.label}正文分页`">
                  <span>正文 {{ (panel.body || panel.live)!.offset }}–{{ (panel.body || panel.live)!.next_offset }} / 已同步 {{ (panel.body || panel.live)!.accepted_bytes }} 字节</span>
                  <Button v-if="(panel.body || panel.live)!.offset > 0" size="sm" variant="ghost" @click="changePage(panel, 'first')">首页</Button>
                  <Button v-if="previousOffsets[panel.key]?.length" size="sm" variant="ghost" @click="changePage(panel, 'previous')">上一页</Button>
                  <Button v-if="(panel.body || panel.live)!.has_more && (panel.body || panel.live)!.next_offset > (panel.body || panel.live)!.offset" size="sm" variant="ghost" @click="changePage(panel, 'next')">下一页</Button>
                </div>
              </template>
              <template v-if="panel.projection">
                <p v-if="panel.projection.body_display !== 'utf8'" class="evidence-reader__notice">{{ bodyDisplayLabels[panel.projection.body_display] }} · 投影仅含头部与占位</p>
                <div class="evidence-reader__pagination" :aria-label="`${panel.label}投影分页`">
                  <span>UTF-8 字节 {{ panel.projection.offset }}–{{ panel.projection.next_offset }} / {{ panel.projection.size }}</span>
                  <Button v-if="panel.projection.offset > 0" size="sm" variant="ghost" @click="changePage(panel, 'first')">首页</Button>
                  <Button v-if="previousOffsets[panel.key]?.length" size="sm" variant="ghost" @click="changePage(panel, 'previous')">上一页</Button>
                  <Button v-if="panel.projection.has_more" size="sm" variant="ghost" @click="changePage(panel, 'next')">下一页</Button>
                </div>
              </template>
              <nav v-if="flowExcerpts.some((excerpt) => excerpt.side === panel.key)" class="evidence-reader__pagination" :aria-label="`${panel.label}已保存选段`">
                <Button v-for="(excerpt, index) in flowExcerpts.filter((entry) => entry.side === panel.key)" :key="`${excerpt.pointer}:${index}`" size="sm" variant="ghost" :disabled="!excerptRange(excerpt) || flow?.state === 'receiving' || !flow" :title="excerpt.pointer" @click="openExcerpt(panel, excerpt)">选段 {{ index + 1 }} · {{ excerpt.pointer }}</Button>
              </nav>
            </div>
          </article>
        </div>
      </div>
    </template>
    <p v-else class="evidence-reader__notice">{{ items.length ? '选择补充证据查看' : emptyLabel }}</p>
  </section>
</template>
<script setup lang="ts">
import { computed, defineAsyncComponent, nextTick, onBeforeUnmount, onMounted, reactive, ref, useId, watch } from 'vue'
import { ChevronLeft, ChevronRight, Copy, Ellipsis, Info, Maximize2, Minimize2, Search } from '@lucide/vue'
import { DropdownMenuItem, PopoverAnchor, PopoverContent, PopoverPortal, PopoverRoot } from 'reka-ui'
import { ApiError, fetchBlob, getTrafficFlow, getTrafficReadable, getTrafficProjection, getTrafficPreview, getTrafficBodyPreview, trafficBodyUrl, type ArtifactPreview } from '@/api/client'
import type { EvidenceItem, EvidenceRef, EvidenceSource, FlowEvidenceExcerpt, TrafficFlowDetail, TrafficReadable, TrafficProjection, TrafficPreview, TrafficBodyPreview, TrafficSide, TrafficState } from '@/api/types'
import type { MonacoReadingState } from '@/components/MonacoEditor.vue'
import { httpMessageSummary, httpMessageView } from '@/lib/httpMessageView'
import { copyText } from '@/lib/copyText'
import { Button, DropdownMenu, Popover, Select } from '@/components/ui'
const MonacoEditor = defineAsyncComponent(() => import('@/components/MonacoEditor.vue'))
const props = withDefaults(defineProps<{
  entityKey: string
  projectId: string
  items: EvidenceItem[]
  source?: EvidenceSource
  selectedItemId?: string
  loadPreview: (reference: EvidenceRef, signal: AbortSignal) => Promise<ArtifactPreview>
  artifactSize?: (reference: EvidenceRef) => number | null | undefined
  originalUrl: (reference: EvidenceRef) => string | null
  imageUrl?: (reference: EvidenceRef) => string | null
  presentation?: 'standalone' | 'step'
  emptyLabel?: string
  fillHeight?: boolean
  autoRefresh?: boolean
  readingStates?: EvidenceReadingStates
}>(), { presentation: 'standalone', emptyLabel: '未记录关键证据', fillHeight: false, autoRefresh: true })
const emit = defineEmits<{ 'update:selectedItemId': [id: string]; flowLoaded: [detail: TrafficFlowDetail] }>()
const sourceLabels: Record<EvidenceSource, string> = { capture: '采集原件', manual: '人工记录', historical: '历史原文' }
const primary = computed(() => props.items.filter((item) => item.role === 'primary'))
const supporting = computed(() => props.items.filter((item) => item.role === 'supporting'))
const selectedId = ref('')
const selected = computed(() => props.items.find((item) => item.id === selectedId.value))
const isHttp = computed(() => selected.value?.kind === 'http')
const flowId = computed(() => selected.value?.flow_id || null)
const flow = ref<TrafficFlowDetail | null>(null)
const flowLoading = ref(false)
const flowError = ref('')
const flowStates: Record<TrafficState, string> = { receiving: '接收中', complete: '完整', partial: '部分记录', failed: '采集失败' }
const bodyDisplayLabels = { binary: '二进制正文', 'non-utf8': '非 UTF-8 正文', compressed: '压缩正文' }
const previousOffsets = ref<Record<string, number[]>>({})
const flowExcerpts = computed(() => selected.value?.kind === 'http' && selected.value.flow_id ? selected.value.excerpts || [] : [])
const selectedIndex = computed(() => props.items.findIndex((item) => item.id === selectedId.value))
const interactionOptions = computed(() => props.items.map((item, index) => ({ value: item.id, label: `${item.role === 'supporting' ? '补充' : '主证据'} · ${item.title || `交互 ${index + 1}`}` })))
const modeLabels = computed(() => ({ readable: '正文', raw: flowId.value ? '投影' : 'Raw', pretty: 'Pretty', hex: 'Hex' }))
const activeSide = ref('request')
const activeSides = new Map<string, string>()
function selectSide(side: string) { activeSide.value = side; activeSides.set(selectedId.value, side) }
const overflowPanels = ref<Record<string, boolean>>({})
const contentHeights = ref<Record<string, number>>({})
const expandedItems = reactive(new Set<string>())
const pairExpanded = computed({ get: () => expandedItems.has(selectedId.value), set: (value: boolean) => { if (value) expandedItems.add(selectedId.value); else expandedItems.delete(selectedId.value) } })
const pairOverflows = computed(() => Object.values(overflowPanels.value).some(Boolean))
// 使用真实内容测量值而非已协调的视窗高度，避免左右高度互相回写。
const packetHeight = computed(() => props.fillHeight ? `${Math.max(100, availableHeight.value)}px` : `min(${Math.max(64, ...Object.values(contentHeights.value))}px, ${pairExpanded.value ? '80dvh' : 'clamp(280px, 36dvh, 400px)'})`)
interface Panel { key: TrafficSide | 'artifact'; label: string; reference: EvidenceRef | null; inline: string | null }
const AUTO_BODY_LIMIT = 256 * 1024
interface PreviewState { deferred?: boolean; loading: boolean; preview: ArtifactPreview | null; readable?: TrafficReadable; projection?: TrafficProjection; live?: TrafficPreview; body?: TrafficBodyPreview; error: string; offset?: number; excerpt?: FlowEvidenceExcerpt }
interface ReadingState extends MonacoReadingState { mode: 'readable' | 'raw' | 'pretty' | 'hex'; textMode: 'readable' | 'raw'; wrap: boolean; expanded: boolean }
// 仅在当前阅读器生命周期内保留各交互的视图，不把证据或搜索词写入浏览器存储。
export type EvidenceReadingStates = Map<string, { identity: string | null; state: ReadingState }>
const readingStates: EvidenceReadingStates = props.readingStates ?? new Map()
function stateFor(panel: Panel) {
  const key = JSON.stringify([props.entityKey, selectedId.value, panel.key])
  const identity = flowId.value ? flowId.value : panel.reference ? JSON.stringify(panel.reference) : panel.inline
  let saved = readingStates.get(key)
  if (!saved || saved.identity !== identity) {
    saved = { identity, state: reactive({ mode: flowId.value ? 'readable' : 'raw', textMode: flowId.value ? 'readable' : 'raw', wrap: true, expanded: false, views: {}, searchOpen: false, query: '', matchIndex: 0 }) }
    readingStates.set(key, saved)
  }
  return saved.state
}
const panels = computed<Panel[]>(() => {
  const item = selected.value
  if (!item) return []
  if (item.kind === 'http') return [
    { key: 'request', label: '请求', reference: item.request ?? null, inline: item.request_text ?? null },
    { key: 'response', label: '响应', reference: item.response ?? null, inline: item.response_text ?? null },
  ]
  return [{ key: 'artifact', label: item.kind === 'text' ? '记录原文' : '证据原件', reference: item.artifact ?? null, inline: item.text ?? null }]
})
const previews = ref<Record<string, PreviewState>>({})
const displayedPanels = computed(() => panels.value.map((panel) => {
  const content = panelContent(panel)
  const preview = previews.value[panel.key]
  const projection = preview?.projection
  const state = stateFor(panel)
  const view = httpMessageView(content || '', isTruncated(panel), { format: state.mode === 'pretty', decodeStatus: preview?.readable?.decode_status, textEscaped: preview?.readable?.text_escaped, displayTruncated: preview?.readable?.display_truncated })
  if (preview?.live || (projection && projection.body_display !== 'utf8')) { view.pretty = null; view.prettyAvailable = false }
  return { ...panel, content, projection, readable: preview?.readable, live: preview?.live, body: preview?.body, view, summary: httpMessageSummary(projection?.offset ? '' : content || ''), state, downloadUrl: panelOriginalUrl(panel) }
}))
const requestTarget = computed(() => displayedPanels.value.find((panel) => panel.key === 'request')?.summary.target || '')
const reader = ref<HTMLElement>()
const panelContainer = ref<HTMLElement>()
const availableHeight = ref(240)
const containerWidth = ref(0)
const splitRatio = ref(50)
const splitBounds = computed(() => {
  const minimum = containerWidth.value > 0 ? Math.min(50, 290 / containerWidth.value * 100) : 50
  return { min: Math.ceil(minimum), max: Math.floor(100 - minimum) }
})
// 依据实际分栏宽度收纳模式，Inspector 或拖动分隔条不依赖 viewport 断点。
function compactPanel(panel: Panel) {
  const width = containerWidth.value
  const panelWidth = width < 600 ? width : (width - 6) * (panel.key === 'request' ? splitRatio.value : 100 - splitRatio.value) / 100
  return isHttp.value && width > 0 && panelWidth < 360
}
function panelMenuItems(panel: (typeof displayedPanels.value)[number]) {
  if (panel.content === null) return []
  const modes = compactPanel(panel)
    ? [...(flowId.value ? [{ value: 'readable', label: '正文' }] : []), { value: 'raw', label: flowId.value ? '投影' : 'Raw' }, ...(panel.view.prettyAvailable && panel.state.mode !== 'hex' ? [{ value: 'pretty', label: 'Pretty' }] : []), ...(flowId.value ? [{ value: 'hex', label: 'Hex' }] : [])]
    : []
  return [...modes.map((mode) => ({ ...mode, label: `${mode.label}${panel.state.mode === mode.value ? '（当前）' : ''}` })), { value: 'wrap', label: panel.state.wrap ? '关闭自动换行' : '开启自动换行' }]
}
function selectPanelOption(panel: Panel, option: string) {
  if (option === 'wrap') stateFor(panel).wrap = !stateFor(panel).wrap
  else if (option === 'readable' || option === 'raw' || option === 'pretty' || option === 'hex') switchMode(panel, option)
}
function setSplit(value: number) { splitRatio.value = Math.max(splitBounds.value.min, Math.min(splitBounds.value.max, value)) }
function saveSplit() { try { localStorage.setItem('arena.http.split', String(splitRatio.value)) } catch { /* 布局偏好不可写不影响阅读。 */ } }
function resizeSplit(event: KeyboardEvent) {
  if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return
  event.preventDefault()
  setSplit(event.key === 'Home' ? splitBounds.value.min : event.key === 'End' ? splitBounds.value.max : splitRatio.value + (event.key === 'ArrowLeft' ? -3 : 3))
  saveSplit()
}
let stopSplit: (() => void) | undefined
function startSplit(event: PointerEvent) {
  if (event.button !== 0 || !panelContainer.value) return
  event.preventDefault()
  const handle = event.currentTarget as HTMLElement
  const bounds = panelContainer.value.getBoundingClientRect()
  handle.focus(); handle.setPointerCapture(event.pointerId)
  const move = (pointer: PointerEvent) => setSplit((pointer.clientX - bounds.left) / bounds.width * 100)
  const stop = () => { handle.removeEventListener('pointermove', move); handle.removeEventListener('pointerup', stop); handle.removeEventListener('pointercancel', stop); saveSplit(); stopSplit = undefined }
  stopSplit?.(); stopSplit = stop
  handle.addEventListener('pointermove', move); handle.addEventListener('pointerup', stop); handle.addEventListener('pointercancel', stop)
}
let layoutObserver: ResizeObserver | undefined
function measureLayout() {
  if (!reader.value || !panelContainer.value) return
  const bounds = panelContainer.value.getBoundingClientRect()
  containerWidth.value = bounds.width; setSplit(splitRatio.value)
  const toolbar = panelContainer.value.querySelector('.evidence-reader__toolbar')?.getBoundingClientRect().height || (isHttp.value ? 32 : 44)
  const footers = Array.from(panelContainer.value.querySelectorAll('.evidence-reader__content')).map((content) => Array.from(content.children).reduce((sum, child) => sum + (child.classList.contains('monaco-reader') ? 0 : child.getBoundingClientRect().height), 0))
  availableHeight.value = Math.max(100, reader.value.getBoundingClientRect().bottom - bounds.top - toolbar - Math.max(0, ...footers) - 2)
}
const readerId = useId()
const editorRefs = new Map<string, InstanceType<typeof MonacoEditor>>()
function registerEditor(key: string, editor: unknown) {
  if (editor) { editorRefs.set(key, editor as InstanceType<typeof MonacoEditor>); void revealPendingBody() }
  else editorRefs.delete(key)
}
let pendingReveal: { itemId: string; generation: number } | undefined
// 父级先更新唯一选择再调用；定位只消费已载入正文，不改变模式、分页或请求身份。
function cancelReveal() { pendingReveal = undefined }
function revealBody(itemId: string) {
  if (itemId !== selectedId.value) return
  pendingReveal = { itemId, generation }
  void revealPendingBody()
}
async function revealPendingBody() {
  const pending = pendingReveal
  if (!pending) return
  await nextTick()
  if (pendingReveal !== pending) return
  if (pending.generation !== generation || pending.itemId !== selectedId.value) { pendingReveal = undefined; return }
  if (flowLoading.value || Object.values(previews.value).some((preview) => preview.loading)) return
  const contentPanels = displayedPanels.value.filter((entry) => entry.content !== null)
  const panel = contentPanels[0] || displayedPanels.value[0]
  if (!panel || contentPanels.some((entry) => !editorRefs.has(entry.key))) return
  selectSide(panel.key)
  await nextTick()
  if (pendingReveal !== pending || pending.generation !== generation) return
  const body = reader.value?.querySelector<HTMLElement>(`[data-evidence-body="${panel.key}"]`)
  if (!body) return
  pendingReveal = undefined
  // 同一证据复用两侧编辑器；只重定位当前内容，外层滚动和焦点仍由一侧承接。
  for (const entry of contentPanels) editorRefs.get(entry.key)?.selectTextRange(0, 0)
  reader.value?.querySelectorAll<HTMLElement>('[data-evidence-body]').forEach((element) => { element.scrollTop = 0 })
  body.scrollIntoView({ block: 'start', behavior: 'auto' })
  body.focus({ preventScroll: true })
}
const copyFeedback = ref<Record<string, { status: 'copying' | 'success' | 'error'; message: string; open: boolean }>>({})
function setCopyFeedbackOpen(key: string, open: boolean) {
  const feedback = copyFeedback.value[key]
  if (feedback) feedback.open = open
}
function focusCopyAction(key: string) { document.getElementById(`${readerId}-${key}-copy-action`)?.focus({ preventScroll: true }) }
function enterCopyFeedback(key: string, event: KeyboardEvent) {
  if (!copyFeedback.value[key]?.open || copyFeedback.value[key]?.status !== 'error' || event.shiftKey || !['Tab', 'ArrowDown'].includes(event.key)) return
  const recovery = document.getElementById(`${readerId}-${key}-copy`)?.querySelector('button')
  if (recovery) { event.preventDefault(); recovery.focus() }
}
const copyOperations = new Map<string, symbol>()
const imageErrors = ref<Record<string, boolean>>({})
const imageSources = ref<Record<string, string>>({})
const initialSelection = () => props.items.find((item) => item.id === props.selectedItemId)?.id || primary.value[0]?.id || (props.presentation === 'step' ? props.items[0]?.id || '' : '')
function selectItem(id: string) { selectedId.value = id; emit('update:selectedItemId', id) }
const controllers = new Map<string, AbortController>()
const manualBodies = new Set<string>()
const deferredBodies = new Set<string>()
let generation = 0
let refreshTimer: ReturnType<typeof setTimeout> | undefined
let refreshFailures = 0
let refreshStopped = false
const lastFlowRefresh = ref('')
function cancel() { pendingReveal = undefined; generation++; controllers.forEach((controller) => controller.abort()); controllers.clear(); manualBodies.clear(); deferredBodies.clear(); Object.values(imageSources.value).forEach((url) => URL.revokeObjectURL(url)); imageSources.value = {} }
watch(() => props.entityKey, () => { cancel(); selectedId.value = initialSelection(); previews.value = {}; copyFeedback.value = {}; copyOperations.clear(); readingStates.clear(); activeSides.clear(); expandedItems.clear() }, { flush: 'sync' })
watch(() => props.items, () => {
  if (!props.items.some((item) => item.id === selectedId.value)) selectedId.value = initialSelection()
}, { immediate: true })
watch(() => props.selectedItemId, (id) => { if (props.items.some((item) => item.id === id)) selectedId.value = id! })
watch(() => JSON.stringify([props.projectId, props.entityKey, selected.value]), () => {
  cancel(); previews.value = {}; copyFeedback.value = {}; copyOperations.clear(); imageErrors.value = {}; overflowPanels.value = {}; contentHeights.value = {}
  clearTimeout(refreshTimer); refreshFailures = 0; refreshStopped = false; lastFlowRefresh.value = ''
  flow.value = null; flowError.value = ''; flowLoading.value = false; previousOffsets.value = {}
  activeSide.value = activeSides.get(selectedId.value) || panels.value.find((panel) => panel.reference || panel.inline !== null)?.key || 'request'
  if (flowId.value) void loadFlow()
  else for (const panel of panels.value) if (panel.reference) void loadPanel(panel, 0, undefined, true)
}, { immediate: true })
function scheduleRefresh() {
  clearTimeout(refreshTimer)
  if (!props.autoRefresh || refreshStopped || document.hidden || !flowId.value || (flow.value && flow.value.state !== 'receiving')) return
  refreshTimer = setTimeout(() => void loadFlow(), Math.min(30000, 3000 * 2 ** refreshFailures))
}
async function loadFlow(propagate = false) {
  const id = flowId.value
  if (!id || flowLoading.value) return
  const controller = new AbortController()
  controllers.set('flow', controller)
  const currentGeneration = generation
  flowLoading.value = true; flowError.value = ''
  try {
    const detail = await getTrafficFlow(props.projectId, id, controller.signal)
    if (controller.signal.aborted || currentGeneration !== generation) return
    if (detail.id !== id || detail.project_id !== props.projectId || detail.scope.project_id !== props.projectId) throw new Error('流量来源不匹配，已停止展示。')
    const previous = flow.value
    flow.value = detail
    emit('flowLoaded', detail)
    await Promise.all(panels.value.map(async (panel) => {
      if (panel.key === 'artifact' || detail.sync.bodies[panel.key].status === 'absent') return
      const state = previews.value[panel.key]
      const changed = !previous || previous.state !== detail.state || previous.header_text?.[panel.key] !== detail.header_text?.[panel.key] || JSON.stringify(previous.sync.bodies[panel.key]) !== JSON.stringify(detail.sync.bodies[panel.key])
      if (changed || !state || state.error) {
        // 终态切换清除非稳定坐标；稳定正文不因摘要刷新重复读取。
        if (previous?.state === 'receiving' && detail.state !== 'receiving') {
          previousOffsets.value[panel.key] = []
          stateFor(panel).views = {}
        }
        if (changed) { controllers.get(panel.key)?.abort(); manualBodies.delete(panel.key); deferredBodies.delete(panel.key) }
        if (changed || !state?.loading) await loadPanel(panel, 0, undefined, true)
      }
    }))
    if (currentGeneration !== generation) return
    refreshFailures = 0; lastFlowRefresh.value = new Date().toLocaleTimeString()
  } catch (error) {
    if (controller.signal.aborted || currentGeneration !== generation) return
    flowError.value = error instanceof ApiError && [401, 403, 404].includes(error.status) ? '流量不存在或当前无权读取，已停止刷新。' : error instanceof Error ? error.message : '流量读取失败'
    refreshFailures++
    if (refreshFailures >= 5) { refreshStopped = true; flowError.value += ' · 多次读取失败，自动刷新已暂停，请手动重试。' }
    if (error instanceof ApiError && [401, 403, 404].includes(error.status)) { refreshStopped = true; previews.value = {}; flow.value = null }
    if (propagate) throw error
  } finally {
    if (!controller.signal.aborted && currentGeneration === generation) { flowLoading.value = false; scheduleRefresh() }
  }
}
defineExpose({ refreshFlow: () => loadFlow(true), revealBody, cancelReveal })
function savedSizeLabel(panel: Panel) {
  const size = flow.value && panel.key !== 'artifact' ? flow.value.sync.bodies[panel.key].accepted_bytes : panel.reference ? props.artifactSize?.(panel.reference) : null
  if (size === null || size === undefined) return '已保存大小未知'
  return `已保存 ${size < 1024 ? `${size} B` : size < 1024 * 1024 ? `${(size / 1024).toFixed(1)} KiB` : `${(size / 1024 / 1024).toFixed(2)} MiB`}`
}
function readStatus(panel: Panel) {
  const preview = previews.value[panel.key]
  const readable = preview?.readable
  if (readable) {
    if (readable.decode_status === 'pending') return '正文尚未封存'
    if (readable.decode_status === 'invalid-encoding') return '正文解码失败 · 可查看 Hex 或下载原件'
    if (readable.decode_status === 'unsupported-encoding') return '正文编码不支持 · 可查看 Hex 或下载原件'
    if (readable.decode_status === 'non-text') return '非文本正文 · 可查看 Hex 或下载原件'
    if (readable.display_truncated || readable.decode_status === 'too-large') return `已载入显示 ${new TextEncoder().encode(readable.body_text).length} 字节 · 解码正文${readable.decode_status === 'too-large' ? '至少' : ''} ${readable.decoded_body_bytes ?? '未知'} 字节`
  }
  if (preview?.preview?.truncated) return `已载入 ${preview.preview.bytes_read} / ${preview.preview.size} 字节`
  return ''
}
function cancelPanel(panel: Panel) {
  controllers.get(panel.key)?.abort(); controllers.delete(panel.key)
  previews.value[panel.key] = { loading: false, preview: null, error: '', deferred: true }
  manualBodies.delete(panel.key)
}
function loadBody(panel: Panel) {
  manualBodies.add(panel.key)
  const state = stateFor(panel)
  state.mode = flowId.value ? 'readable' : 'raw'; state.textMode = state.mode
  void loadPanel(panel)
}
async function loadPanel(panel: Panel, offset = 0, excerpt?: FlowEvidenceExcerpt, automatic = false) {
  if (!panel.reference && !flowId.value) return
  if (automatic) {
    const state = stateFor(panel)
    state.mode = flowId.value ? 'readable' : 'raw'; state.textMode = state.mode
    const body = flow.value && panel.key !== 'artifact' ? flow.value.sync.bodies[panel.key] : null
    const size = body ? body.captured_bytes === undefined && body.accepted_bytes === undefined ? null : Math.max(body.captured_bytes ?? 0, body.accepted_bytes ?? 0) : panel.reference ? props.artifactSize?.(panel.reference) : null
    if (size === null || size === undefined || size > AUTO_BODY_LIMIT || flow.value?.state === 'receiving') {
      controllers.get(panel.key)?.abort()
      deferredBodies.add(panel.key)
      previews.value[panel.key] = { loading: false, preview: null, error: '', deferred: true }
      return
    }
  }
  const imagePath = panel.reference && props.imageUrl?.(panel.reference)
  if (imagePath) {
    controllers.get(panel.key)?.abort()
    const controller = new AbortController()
    controllers.set(panel.key, controller)
    previews.value[panel.key] = { loading: true, preview: null, error: '' }
    try {
      const blob = await fetchBlob(imagePath, controller.signal)
      if (!['image/png', 'image/jpeg', 'image/gif', 'image/webp'].includes(blob.type)) throw new Error('不支持的图片类型。')
      const bitmap = await createImageBitmap(blob)
      bitmap.close()
      if (controller.signal.aborted) return
      const previous = imageSources.value[panel.key]
      if (previous) URL.revokeObjectURL(previous)
      imageSources.value[panel.key] = URL.createObjectURL(blob)
    } catch {
      if (!controller.signal.aborted) imageErrors.value[panel.key] = true
    } finally {
      if (!controller.signal.aborted) previews.value[panel.key] = { loading: false, preview: null, error: '' }
    }
    return
  }
  controllers.get(panel.key)?.abort()
  const controller = new AbortController()
  controllers.set(panel.key, controller)
  const currentGeneration = generation
  const entity = props.entityKey
  const projectId = props.projectId
  const artifactId = panel.reference?.artifact_id
  const id = flowId.value
  copyOperations.delete(panel.key)
  delete copyFeedback.value[panel.key]
  previews.value = { ...previews.value, [panel.key]: { loading: true, preview: null, error: '', offset, excerpt } }
  const isCurrent = () => !controller.signal.aborted && currentGeneration === generation && entity === props.entityKey && controllers.get(panel.key) === controller
  try {
    if (id && panel.key !== 'artifact') {
      if (stateFor(panel).mode === 'hex') {
        const body = await getTrafficBodyPreview(projectId, id, panel.key, offset, controller.signal)
        if (!isCurrent()) return
        if (body.side !== panel.key || body.revision !== flow.value?.sync.bodies[panel.key].revision) throw new Error('正文版本已变化，请刷新来源。')
        previews.value[panel.key] = { loading: false, preview: null, body, error: '', offset: body.offset }
        return
      }
      if (flow.value?.state === 'receiving') {
        const live = await getTrafficPreview(projectId, id, panel.key, offset, controller.signal)
        if (!isCurrent()) return
        if (live.project_id !== projectId || live.flow_id !== id || live.side !== panel.key || live.revision !== flow.value?.revision) throw new Error('接收进度已变化，等待下一次刷新。')
        previews.value[panel.key] = { loading: false, preview: null, live, error: '', offset: live.offset }
        return
      }
      if (stateFor(panel).textMode === 'readable') {
        const readable = await getTrafficReadable(projectId, id, panel.key, controller.signal, automatic ? 'auto' : 'manual')
        if (!isCurrent()) return
        if (readable.project_id !== projectId || readable.flow_id !== id || readable.side !== panel.key || readable.revision !== flow.value?.revision || readable.representation !== 'http-readable' || readable.excerpt_eligible !== false) throw new Error('正文阅读身份已变化，请重新读取证据。')
        if (readable.decode_status === 'deferred') deferredBodies.add(panel.key)
        previews.value[panel.key] = { loading: false, preview: null, readable, deferred: readable.decode_status === 'deferred', error: '' }
        return
      }
      const projection = await getTrafficProjection(projectId, id, panel.key, offset, 65536, controller.signal)
      if (!isCurrent()) return
      if (projection.project_id !== projectId || projection.flow_id !== id || projection.side !== panel.key || projection.representation !== 'http-evidence-v1' || projection.revision !== flow.value?.revision || projection.offset !== offset) throw new Error('流量投影身份已变化，请重新读取证据。')
      if (excerpt && (excerpt.representation !== projection.representation || excerpt.sha256 !== projection.sha256 || excerpt.revision !== projection.revision)) throw new Error('选段与当前投影版本或摘要不匹配，未展示替代内容。')
      const range = excerpt ? excerptRange(excerpt) : null
      if (range && (range.end > projection.size || (projection.body_display !== 'utf8' && range.end > projection.header_size))) throw new Error('选段超出可引用文本范围；二进制占位不是正文证据。')
      previews.value = { ...previews.value, [panel.key]: { loading: false, preview: null, projection, error: '', offset, excerpt } }
      if (range) {
        await nextTick()
        if (isCurrent()) editorRefs.get(panel.key)?.selectTextRange(0, new TextDecoder().decode(new TextEncoder().encode(projection.text).slice(0, range.end - range.start)).length)
      }
      return
    }
    const preview = await props.loadPreview(panel.reference!, controller.signal)
    if (!isCurrent()) return
    if (preview.project_id !== projectId || preview.artifact_id !== artifactId) throw new Error('证据响应身份不匹配，已停止展示。')
    previews.value = { ...previews.value, [panel.key]: { loading: false, preview, error: '' } }
  } catch (error) {
    if (!isCurrent()) return
    previews.value = { ...previews.value, [panel.key]: { loading: false, preview: null, error: error instanceof Error ? error.message : '证据读取失败', offset, excerpt } }
  }
}
function excerptRange(excerpt: FlowEvidenceExcerpt) {
  const match = /^utf8-bytes=(\d+):(\d+)$/.exec(excerpt.pointer)
  if (!match) return null
  const start = Number(match[1]); const end = Number(match[2])
  return Number.isSafeInteger(start) && Number.isSafeInteger(end) && start >= 0 && end > start ? { start, end } : null
}
function openExcerpt(panel: Panel, excerpt: FlowEvidenceExcerpt) {
  const range = excerptRange(excerpt)
  if (!range) return
  selectSide(panel.key)
  stateFor(panel).mode = 'raw'
  stateFor(panel).textMode = 'raw'
  previousOffsets.value[panel.key] = []
  void loadPanel(panel, range.start, excerpt)
}
function changePage(panel: Panel, direction: 'first' | 'previous' | 'next') {
  const preview = previews.value[panel.key]
  const projection = preview?.projection || preview?.live || preview?.body
  if (!projection) return
  const history = previousOffsets.value[panel.key] ||= []
  let offset = 0
  if (direction === 'next') {
    if (!projection.has_more) return
    history.push(projection.offset); offset = projection.next_offset
  } else if (direction === 'previous') offset = history.pop() ?? 0
  else previousOffsets.value[panel.key] = []
  void loadPanel(panel, offset)
}
function panelOriginalUrl(panel: Panel) {
  if (flowId.value && panel.key !== 'artifact') return flow.value?.sync.bodies[panel.key].status === 'available' ? trafficBodyUrl(props.projectId, flowId.value, panel.key) : null
  return panel.reference ? props.originalUrl(panel.reference) : null
}
function flowSideNotice(panel: Panel) {
  if (!flow.value || panel.key === 'artifact') return ''
  const status = flow.value.sync.bodies[panel.key].status
  return status === 'absent' ? (flow.value.source_state === 'receiving' && panel.key === 'response' ? '等待响应' : `未记录${panel.label}`) : ''
}
function hexText(body: TrafficBodyPreview) {
  const bytes = Uint8Array.from(atob(body.content_base64), (char) => char.charCodeAt(0))
  const lines: string[] = []
  for (let index = 0; index < bytes.length; index += 16) {
    const row = Array.from(bytes.slice(index, index + 16))
    lines.push(`${(body.offset + index).toString(16).padStart(8, '0')}  ${row.map((byte) => byte.toString(16).padStart(2, '0')).join(' ').padEnd(47)}  ${row.map((byte) => byte >= 32 && byte <= 126 ? String.fromCharCode(byte) : '.').join('')}`)
  }
  return lines.join('\n')
}
function panelContent(panel: Panel): string | null {
  if (!flowId.value) return panel.reference ? previews.value[panel.key]?.preview?.content ?? null : panel.inline
  const preview = previews.value[panel.key]
  if (preview?.readable) return preview.readable.header_text + preview.readable.body_text
  if (preview?.body) return hexText(preview.body)
  if (preview?.live) return preview.live.header_text + preview.live.text
  return preview?.projection?.text ?? (panel.key !== 'artifact' ? flow.value?.header_text?.[panel.key] ?? null : null)
}
function isTruncated(panel: Panel) {
  const preview = previews.value[panel.key]
  if (preview?.readable) return preview.readable.display_truncated || ['too-large', 'pending'].includes(preview.readable.decode_status)
  if (preview?.live || preview?.body) return true
  const projection = preview?.projection
  return projection ? projection.offset > 0 || projection.has_more : Boolean(preview?.preview?.truncated)
}
function switchMode(panel: Panel, mode: ReadingState['mode']) {
  const state = stateFor(panel)
  const needsRead = mode === 'hex' || state.mode === 'hex' || ((mode === 'raw' || mode === 'readable') && mode !== state.textMode)
  if (mode === 'raw' || mode === 'readable') state.textMode = mode
  state.mode = mode
  if (needsRead) {
    previousOffsets.value[panel.key] = []
    if (mode !== 'hex' && !manualBodies.has(panel.key) && (deferredBodies.has(panel.key) || previews.value[panel.key]?.body)) {
      void loadPanel(panel, 0, undefined, true)
    } else void loadPanel(panel)
  }
}
// 局域网 HTTP 入口没有 Clipboard API；仅在用户点击期间使用浏览器仍支持的复制命令。
function copyLabel(panel: Panel) {
  const preview = previews.value[panel.key]
  if (previews.value[panel.key]?.deferred) return '复制已载入头部'
  if (stateFor(panel).mode === 'hex') return '复制当前 Hex 展示'
  const fragment = isTruncated(panel) ? '片段' : ''
  if (preview?.readable?.text_escaped) return `复制转义文本${fragment}`
  if (preview?.readable) return fragment ? '复制已载入正文片段' : '复制正文视图'
  if (preview?.projection) return `复制 v1 投影${fragment}`
  return fragment ? '复制已载入片段' : flowId.value ? '复制重建文本' : '复制原文'
}
async function copy(panel: Panel) {
  const content = panelContent(panel)
  if (content === null) return
  const currentGeneration = generation
  const operation = Symbol()
  copyOperations.set(panel.key, operation)
  const isCurrent = () => currentGeneration === generation && copyOperations.get(panel.key) === operation
  const hasClipboardApi = typeof navigator.clipboard?.writeText === 'function'
  copyFeedback.value[panel.key] = { status: 'copying', message: '正在复制…', open: true }
  try {
    await copyText(content, reader.value!)
    if (isCurrent()) copyFeedback.value[panel.key] = { status: 'success', message: `已${copyLabel(panel)}`, open: copyFeedback.value[panel.key]?.open ?? false }
  } catch (error) {
    if (!isCurrent()) return
    const denied = error instanceof DOMException && error.name === 'NotAllowedError'
    copyFeedback.value[panel.key] = { status: 'error', message: hasClipboardApi
      ? denied ? '浏览器未允许剪贴板写入。可检查本站权限，或选择当前文本手工复制。' : '剪贴板写入未完成，请选择当前文本手工复制。'
      : '此环境没有 Clipboard API，浏览器复制命令也未完成。请选择当前文本手工复制。', open: copyFeedback.value[panel.key]?.open ?? false }
  }
}
async function selectRaw(panel: Panel) {
  const currentGeneration = generation
  const state = stateFor(panel)
  // 手工恢复选择同一份未格式化展示，不将转义正文悄悄换成 v1 占位。
  if (state.mode === 'pretty') state.mode = state.textMode
  setCopyFeedbackOpen(panel.key, false)
  await nextTick()
  if (currentGeneration === generation) editorRefs.get(panel.key)?.selectAll()
}
function visibilityChanged() { clearTimeout(refreshTimer); if (!document.hidden) scheduleRefresh() }
watch(() => props.autoRefresh, scheduleRefresh)
watch([previews, flowLoading], revealPendingBody, { deep: true, flush: 'post' })
watch([previews, pairExpanded], () => nextTick(measureLayout), { deep: true, flush: 'post' })
watch(panelContainer, (element, previous) => {
  if (previous) layoutObserver?.unobserve(previous)
  if (element) layoutObserver?.observe(element)
  void nextTick(measureLayout)
}, { flush: 'post' })
onMounted(() => {
  try { const saved = Number(localStorage.getItem('arena.http.split')); if (saved >= 20 && saved <= 80) splitRatio.value = saved } catch { /* 只保存无敏感布局比例。 */ }
  layoutObserver = new ResizeObserver(measureLayout)
  if (reader.value) layoutObserver.observe(reader.value)
  if (panelContainer.value) layoutObserver.observe(panelContainer.value)
  document.addEventListener('visibilitychange', visibilityChanged)
  measureLayout()
})
onBeforeUnmount(() => { cancel(); clearTimeout(refreshTimer); stopSplit?.(); layoutObserver?.disconnect(); document.removeEventListener('visibilitychange', visibilityChanged) })
</script>
<style scoped>
.evidence-reader { container-type: inline-size; container-name: evidence-reader; min-width: 0; display: flex; flex-direction: column; gap: var(--space-2); }
.evidence-reader__header, .evidence-reader__identity, .evidence-reader__navigation, .evidence-reader__side-switch { display: flex; flex-wrap: wrap; align-items: center; gap: var(--space-2); }
.evidence-reader__header h3 { flex: 1; margin: 0; font-size: var(--text-md); font-weight: var(--font-semibold); }
.evidence-reader__source, .evidence-reader__identity { color: var(--text-muted); font-size: var(--text-sm); }
.evidence-reader__identity strong { color: var(--text); }
.evidence-reader__navigation .ui-button { max-width: 100%; height: auto; min-height: 32px; white-space: normal; overflow-wrap: anywhere; }
.evidence-reader__packets { min-width: 0; border: 1px solid var(--border); border-radius: var(--radius-xl); background: var(--color-surface); }
.evidence-reader__summary { display: flex; flex-wrap: wrap; align-items: center; gap: 0 var(--space-2); min-height: 36px; padding: 0 var(--space-2); border-bottom: 1px solid var(--border); border-radius: var(--radius-xl) var(--radius-xl) 0 0; background: var(--color-surface-inset); }
.evidence-reader__summary h3 { flex: none; margin: 0; font-size: var(--text-sm); font-weight: var(--font-semibold); }
.evidence-reader__header-context { min-width: 0; display: flex; flex-wrap: wrap; align-items: center; gap: var(--space-1); }
.evidence-reader__header-actions { min-width: 0; margin-left: auto; display: flex; flex-wrap: wrap; align-items: center; gap: var(--space-1); }
.evidence-reader__summary :deep(.ui-button) { min-height: 32px; padding-block: 0; }
.evidence-reader__interaction { display: flex; align-items: center; min-width: 0; flex: 1 1 240px; gap: var(--space-1); }
.evidence-reader__interaction :deep(.ui-select__trigger) { flex: 1; width: 0; min-width: 48px; min-height: 32px; padding: 0 var(--space-2); border-color: transparent; background: transparent; font-size: var(--text-sm); }
.evidence-reader__position { flex: none; font-size: var(--text-xs); color: var(--text-muted); font-variant-numeric: tabular-nums; }
.evidence-reader__info.ui-button { flex: none; padding-inline: var(--space-1); font-size: var(--text-xs); color: var(--text-muted); }
.evidence-reader__flow-state { flex: none; font-size: var(--text-xs); color: var(--text-muted); }
.evidence-reader__flow-state--warning { color: var(--color-warning); }
.evidence-reader__target { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--text-muted); font: var(--text-sm)/var(--leading-normal) var(--font-mono); }
.evidence-reader__summary .evidence-reader__source { flex: none; }
.evidence-reader__panels { display: grid; grid-template-columns: minmax(0, 1fr); align-items: start; }
.evidence-reader__panels--http { grid-template-columns: minmax(0, var(--request-share, 50%)) 6px minmax(0, 1fr); grid-template-rows: auto auto; align-items: stretch; }
.evidence-reader__panel { min-width: 0; }
.evidence-reader__panels--http .evidence-reader__panel { display: grid; grid-column: 1; grid-row: 1 / 3; grid-template-rows: subgrid; }
.evidence-reader__panels--http .evidence-reader__panel + .evidence-reader__panel { grid-column: 3; }
.evidence-reader__separator { grid-column: 2; grid-row: 1 / 3; cursor: col-resize; touch-action: none; background: var(--color-surface-inset); border-inline: 1px solid var(--border); z-index: 1; }
.evidence-reader__separator:focus-visible, .evidence-reader__separator:hover { outline: 2px solid var(--accent); outline-offset: -2px; }
.evidence-reader--fill { height: 100%; min-height: 0; }
.evidence-reader--fill .evidence-reader__packets { min-height: 0; height: 100%; display: flex; flex-direction: column; }
.evidence-reader--fill .evidence-reader__panels { flex: 1; min-height: 0; grid-template-rows: auto minmax(0, 1fr); }
.evidence-reader--fill .evidence-reader__content { min-height: 0; overflow: auto; }
.evidence-reader__content { min-width: 0; background: var(--color-surface); scroll-margin-block-start: var(--space-2); }
.evidence-reader__content:focus-visible { outline: 2px solid var(--accent); outline-offset: -2px; }
.evidence-reader__panel:first-child .evidence-reader__content { border-bottom-left-radius: var(--radius-xl); }
.evidence-reader__panel:last-child .evidence-reader__content { border-bottom-right-radius: var(--radius-xl); }
.evidence-reader__side-switch { display: none; padding: var(--space-1) var(--space-2); border-bottom: 1px solid var(--border); background: var(--color-surface-inset); }
.evidence-reader__toolbar { display: flex; align-items: center; flex-wrap: nowrap; gap: var(--space-1); min-width: 0; min-height: 44px; padding: var(--space-1) var(--space-2); border-bottom: 1px solid var(--border); background: var(--color-surface-inset); }
.evidence-reader__packets--http .evidence-reader__toolbar { min-height: 32px; padding: 0 var(--space-1); }
.evidence-reader__packets--http .evidence-reader__toolbar .ui-button { min-height: 32px; padding-block: 0; font-size: var(--text-xs); }
.evidence-reader__toolbar h4 { flex: none; margin: 0; font-size: var(--text-sm); font-weight: var(--font-semibold); }
.evidence-reader__current-mode { padding-inline: var(--space-1); font-size: var(--text-xs); border-bottom: 2px solid var(--accent); line-height: 30px; }
.evidence-reader__protocol { flex: 0 1 auto; max-width: 52px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--text-muted); font: var(--text-xs)/var(--leading-normal) var(--font-mono); padding-inline: var(--space-1); }
.evidence-reader__toolbar--compact .evidence-reader__protocol { max-width: 32px; }
.evidence-reader__modes, .evidence-reader__tools { display: flex; flex: none; align-items: center; gap: 0; }
.evidence-reader__tools { margin-left: auto; }
.evidence-reader__modes .ui-button { position: relative; min-height: 32px; padding-inline: var(--space-1); border-radius: 0; color: var(--text-muted); font-weight: var(--font-normal); }
.evidence-reader__modes .ui-button[aria-pressed="true"] { color: var(--text); background: transparent; }
.evidence-reader__modes .ui-button[aria-pressed="true"]::after { content: ''; position: absolute; height: 2px; bottom: 0; inset-inline: var(--space-1); background: var(--accent); }
.evidence-reader__icon.ui-button { flex: none; width: 32px; min-height: 32px; padding: 0; color: var(--text-muted); }
.evidence-reader__icon[aria-pressed="true"] { color: var(--text); background: var(--surface-selected); }
.evidence-reader__notice { margin: 0; padding: var(--space-2) var(--space-3); color: var(--text-muted); font-size: var(--text-sm); line-height: 1.7; overflow-wrap: anywhere; }
.evidence-reader__pagination { display: flex; flex-wrap: wrap; align-items: center; gap: var(--space-2); padding: var(--space-2) var(--space-3); font-size: var(--text-sm); }
.evidence-reader__pagination .ui-button { height: auto; white-space: normal; overflow-wrap: anywhere; }
.evidence-reader__menu-hint { max-width: 280px; margin: 0; padding: var(--space-2) var(--space-3); color: var(--text-muted); font-size: var(--text-sm); line-height: 1.5; }
.evidence-reader__limit.ui-button { flex: none; min-height: 32px; padding-inline: var(--space-1); color: var(--color-warning); white-space: nowrap; font-size: var(--text-sm); }
.evidence-reader__preview-details { display: grid; gap: var(--space-3); overflow-wrap: anywhere; }
.evidence-reader__preview-details section { display: grid; gap: var(--space-2); }
.evidence-reader__preview-details p { margin: 0; line-height: 1.6; }
.evidence-reader__preview-details a { color: var(--accent); }
.evidence-reader__copy-feedback { overflow-wrap: anywhere; }
.evidence-reader__copy-body { display: flex; flex-direction: column; align-items: start; gap: var(--space-2); }
.evidence-reader__copy-feedback--error { color: var(--color-warning); }
.evidence-reader__copy-feedback .ui-button { min-height: 32px; height: auto; padding: 0; white-space: normal; text-align: left; }
.evidence-reader__live { position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px; overflow: hidden; clip-path: inset(50%); white-space: nowrap; }
.evidence-reader__image { display: block; max-width: 100%; max-height: 60dvh; object-fit: contain; }
.evidence-reader__supporting summary { color: var(--text-muted); cursor: pointer; padding-block: var(--space-2); font-size: var(--text-sm); }
@container evidence-reader (max-width: 599px) {
  .evidence-reader__separator { display: none; }
  .evidence-reader__side-switch { display: flex; }
  .evidence-reader__summary { flex-wrap: wrap; }
  .evidence-reader__interaction { flex-basis: 100%; }
  .evidence-reader__target:empty { display: none; }
  .evidence-reader__summary .evidence-reader__source { margin-left: auto; }
  .evidence-reader__panels--http { grid-template-columns: minmax(0, 1fr); }
  .evidence-reader__panels--http .evidence-reader__panel { grid-column: 1; }
  .evidence-reader__panels--http .evidence-reader__panel + .evidence-reader__panel { grid-column: 1; border-left: 0; }
  .evidence-reader__panels--http .evidence-reader__panel:not(.evidence-reader__panel--selected) { display: none; }
}
@container evidence-reader (max-width: 279px) {
  .evidence-reader__toolbar { flex-wrap: wrap; }
}
</style>
