<template>
  <section class="consumption-panel" aria-labelledby="token-trend-title">
    <div class="consumption-trend">
    <header class="consumption-heading">
      <div><h2 id="token-trend-title">Token 用量</h2><span>最近 14 天 · {{ data.timezone }}</span></div>
      <Button variant="ghost" size="sm" :aria-expanded="showTable" @click="showTable = !showTable">{{ showTable ? '收起数据' : '查看数据' }}</Button>
    </header>
    <div ref="plot" class="token-plot" @mouseleave="active = null">
      <svg v-if="hasPoints" :viewBox="`0 0 ${plotWidth} 300`" role="group" aria-label="近14天已记录Token单系列折线图">
        <g v-for="tick in [0, ceiling / 2, ceiling]" :key="tick">
          <line x1="64" :y1="y(tick)" :x2="plotWidth - 30" :y2="y(tick)" class="grid-line" />
          <text x="54" :y="y(tick) + 4" text-anchor="end">{{ axisNumber(tick) }}</text>
        </g>
        <path :d="path" class="token-line" />
        <template v-for="(day, index) in data.days" :key="day.date">
          <circle v-if="values[index] != null && (index === values.length - 1 || values[index + 1] == null)" :cx="x(index)" :cy="y(values[index]!)" r="6" class="token-dot" />
          <text v-if="index % 3 === 0 || index === data.days.length - 1" :x="x(index)" y="286" text-anchor="middle">{{ day.date.slice(5) }}</text>
          <g tabindex="0" :aria-label="`${day.date}：${tokenSubtotal(day)} Token；${estimatedAmounts(day)}`"
            @focus="active = index" @blur="active = null" @pointermove="active = index">
            <rect :x="x(index) - hitWidth / 2" y="12" :width="hitWidth" height="254" class="hit-target" />
          </g>
        </template>
        <template v-if="active !== null">
          <line :x1="x(active)" y1="18" :x2="x(active)" y2="258" class="crosshair" />
          <circle v-if="values[active] != null" :cx="x(active)" :cy="y(values[active]!)" r="6" class="token-dot" />
        </template>
      </svg>
      <div v-if="selected" class="token-tooltip" :style="{ left: `${Math.min(74, Math.max(0, x(active!) / plotWidth * 100 - 10))}%` }" role="status">
        <strong>{{ tokenSubtotal(selected) }} Token</strong><span>{{ selected.date }}</span><span>{{ estimatedAmounts(selected) }}</span>
      </div>
      <EmptyState v-if="!hasPoints" title="这 14 天尚无可绘制的用量记录" />
    </div>
    <div v-if="showTable" class="consumption-scroll">
      <table><caption class="sr-only">与折线相同的业务日范围；未计量不补零</caption><thead><tr><th>日期</th><th>Token</th><th>估算费用</th><th>计量范围</th></tr></thead>
        <tbody><tr v-for="day in data.days" :key="day.date"><th>{{ day.date }}</th><td>{{ tokenSubtotal(day) }}</td><td>{{ estimatedAmounts(day) }}</td><td>{{ day.coverage === 'unavailable' ? '未启用' : '已记录小计' }}</td></tr></tbody>
      </table>
    </div>
    <details class="coverage-note">
      <summary>计量范围<span v-if="data.undated.responses"> · {{ data.undated.responses }} 条历史汇总无发生日期</span></summary>
      <p>{{ data.scope === 'global' ? '全局范围，含平台诊断' : '当前租户范围' }} · 非完整账单<span v-if="data.undated.responses">；无日期历史汇总仅计入总量</span></p>
    </details>
    <div v-if="showTable" class="consumption-models">
      <div class="consumption-heading model-heading"><h2>模型用量</h2><Button variant="ghost" size="sm" :aria-label="descending ? '按 Token 用量升序排列' : '按 Token 用量降序排列'" @click="descending = !descending">Token {{ descending ? '↓' : '↑' }}</Button></div>
      <ul v-if="data.models.length" class="model-usage" aria-label="近14天模型用量">
        <li v-for="(item, index) in sortedModels" :key="index">
          <div><span>{{ item.model.display_name || item.model.model_name || '未记录模型' }}</span><small>{{ consumptionSource[item.source] || item.source }}<template v-if="item.model.model_revision"> · 版本 {{ item.model.model_revision }}</template><template v-if="item.partial_responses"> · {{ item.partial_responses }} 次部分计量</template><template v-if="item.unpriced_responses && item.amounts.length"> · {{ item.unpriced_responses }} 次未定价</template></small></div>
          <div class="numeric">{{ tokenSubtotal(item) }} Token<small>{{ estimatedAmounts(item) }}</small></div>
        </li>
      </ul>
      <EmptyState v-else title="暂无模型消费记录" />
    </div>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import type { ConsumptionOverview } from '@/api/types/consumption'
import { Button, EmptyState } from '@/components/ui'
import { consumptionSource, estimatedAmounts, tokenSubtotal } from '@/lib/consumption'
const props = defineProps<{ data: ConsumptionOverview }>()
const showTable = ref(false)
const plot = ref<HTMLElement | null>(null)
const plotWidth = ref(800)
let observer: ResizeObserver | undefined
onMounted(() => {
  observer = new ResizeObserver(([entry]) => {
    if (entry) plotWidth.value = Math.max(320, entry.contentRect.width)
  })
  if (plot.value) observer.observe(plot.value)
})
onUnmounted(() => observer?.disconnect())
const hitWidth = computed(() => (plotWidth.value - 94) / Math.max(1, props.data.days.length - 1))
const descending = ref(true)
const sortedModels = computed(() => [...props.data.models].sort((a, b) => (descending.value ? b.tokens - a.tokens : a.tokens - b.tokens)))
const active = ref<number | null>(null)
const selected = computed(() => active.value === null ? null : props.data.days[active.value])
const values = computed(() => props.data.days.map(day => day.coverage !== 'unavailable' && tokenSubtotal(day) !== '未记录' ? day.tokens : null))
const hasPoints = computed(() => values.value.some(value => value !== null))
const ceiling = computed(() => {
  const maximum = Math.max(1, ...values.value.map(value => value ?? 0))
  const unit = 10 ** Math.floor(Math.log10(maximum))
  return Math.ceil(maximum / unit) * unit
})
const x = (index: number) => 64 + index * (plotWidth.value - 94) / Math.max(1, props.data.days.length - 1)
const y = (value: number) => 258 - value / ceiling.value * 234
const path = computed(() => values.value.map((value, index) => value === null ? '' : `${index === 0 || values.value[index - 1] === null ? 'M' : 'L'} ${x(index)} ${y(value)}`).join(' '))
const axisNumber = (value: number) => value >= 10000 ? `${Number((value / 1000).toFixed(1))}K` : value.toLocaleString('zh-CN')
</script>

<style scoped>
.consumption-panel { min-width: 0; }
.consumption-trend { min-width: 0; padding: 20px; background: var(--color-surface); border: 1px solid var(--color-border-subtle); border-radius: var(--radius-lg); box-shadow: var(--shadow-sm); }
.consumption-models { margin-top: var(--space-5); min-width: 0; }
.consumption-panel :deep(.ui-empty-state) { padding: var(--space-6) var(--space-4); }
.consumption-panel :deep(.ui-empty-state__title) { color: var(--text-muted); font-size: var(--text-sm); font-weight: 400; }
.consumption-heading { display: flex; align-items: center; justify-content: space-between; gap: 16px; }
.consumption-heading h2 { margin: 0; font-size: var(--text-section); font-weight: 600; }
.consumption-heading span, .coverage-note { color: var(--text-muted); font-size: var(--text-sm); }
.token-plot { position: relative; margin-top: 14px; height: 300px; }
.token-plot :deep(.ui-empty-state) { height: 100%; box-sizing: border-box; }
svg { display: block; width: 100%; height: 300px; overflow: hidden; }
svg text { fill: var(--text-muted); font-size: 12px; }
.grid-line { stroke: var(--border); stroke-width: 1; vector-effect: non-scaling-stroke; }
.token-line { stroke: var(--color-chart-token); stroke-width: 2; fill: none; stroke-linecap: round; stroke-linejoin: round; vector-effect: non-scaling-stroke; }
.token-dot { fill: var(--color-chart-token); stroke: var(--color-surface); stroke-width: 2; vector-effect: non-scaling-stroke; pointer-events: none; }
.crosshair { stroke: var(--border-strong); stroke-width: 1; pointer-events: none; }
.hit-target { fill: transparent; }
g:focus { outline: none; } g:focus-visible .hit-target { stroke: var(--accent); stroke-width: 1; }
.token-tooltip { position: absolute; top: 5px; display: grid; gap: 3px; max-width: 240px; padding: 10px 12px; color: var(--text); background: var(--surface-raised); border: 1px solid var(--border); border-radius: var(--radius-md); box-shadow: var(--shadow-sm); pointer-events: none; font-size: var(--text-sm); overflow-wrap: anywhere; }
.token-tooltip span { color: var(--text-muted); }
.coverage-note { margin-top: var(--space-3); }
.coverage-note summary { width: fit-content; cursor: pointer; }
.coverage-note p { margin: var(--space-2) 0 0; }
.model-heading { align-items: flex-start; flex-wrap: wrap; margin-bottom: 12px; }
.consumption-scroll { overflow-x: auto; }
.numeric { text-align: right; }
.model-usage { list-style: none; padding: 0; margin: 0; font-size: var(--text-sm); }
.model-usage li { display: grid; grid-template-columns: minmax(0, 1fr) minmax(140px, auto); gap: var(--space-4); padding: var(--space-3) 0; border-bottom: 1px solid var(--border); overflow-wrap: anywhere; }
.model-usage .numeric { font-variant-numeric: tabular-nums; }
table { width: 100%; border-collapse: collapse; text-align: left; font-size: var(--text-sm); }
th, td { padding: 10px 12px; border-bottom: 1px solid var(--border); font-variant-numeric: tabular-nums; }
th { font-weight: 500; } thead th { background: var(--bg-inset); color: var(--text-muted); white-space: nowrap; }
small { display: block; color: var(--text-muted); margin-top: 4px; overflow-wrap: anywhere; }
.sr-only { position: absolute; width: 1px; height: 1px; overflow: hidden; clip-path: inset(50%); }
@media (max-width: 1100px) {
  .consumption-panel { grid-template-columns: minmax(0, 1fr); }
}
@media (forced-colors: active), print { .token-line { stroke: CanvasText; } .token-dot { fill: CanvasText; stroke: Canvas; } }
</style>
