<template>
  <Drawer
    :open="drawerOpen"
    :title="drawerTitle"
    :description="drawerDescription"
    side="right"
    size="md"
    portal-disabled
    :close-label="closeLabel"
    @update:open="handleOpenChange"
    @escape-key-down="handleEscapeKeyDown"
  >
    <template #title>
      <div v-if="activeMode === 'node' && selectedNode" class="drawer-title-row">
        <span class="graph-drawer-kind-icon" :class="`node-kind-${nodePresentation.tone}`">
          <component :is="nodePresentation.icon" :size="16" :stroke-width="2" aria-hidden="true" />
        </span>
        <div class="graph-drawer-heading">
          <div class="graph-drawer-heading-line">
            <span class="node-status-badge" :class="`tone-${nodeStatus.tone}`">{{
              nodeStatus.label
            }}</span>
            <span>{{ nodePresentation.label }}</span>
          </div>
          <span class="graph-drawer-title mono">{{ selectedNode.id }}</span>
        </div>
      </div>
      <div v-else-if="activeMode === 'edge' && selectedEdge" class="drawer-title-row">
        <span class="graph-drawer-kind-icon edge"
          ><Workflow :size="16" :stroke-width="2" aria-hidden="true"
        /></span>
        <div class="graph-drawer-heading">
          <div class="graph-drawer-heading-line">
            <span class="node-status-badge intent">{{ edgePresentation.label }}</span>
            <span>{{
              isNarrativeOutputEdge ? 'Reason 输出 / Dispatched Explore' : edgePresentation.label
            }}</span>
          </div>
          <span class="graph-drawer-title mono">{{ selectedEdge.id }}</span>
        </div>
      </div>
      <span v-else>{{ drawerTitle }}</span>
    </template>

    <div v-if="activeMode === 'node' && selectedNode" class="graph-drawer-body">
      <div class="drawer-section">
        <h4>{{ selectedNode.data?.kind === 'reason' ? 'Reason 决策摘要' : '节点描述' }}</h4>
        <p class="desc-text pre-wrap">
          {{
            selectedNode.data?.reason?.rationale ||
            selectedNode.data?.label ||
            selectedNode.data?.fact?.description
          }}
        </p>
        <div
          v-if="selectedNode.data?.kind === 'reason' && !selectedNode.data?.reason?.auditAvailable"
          class="empty-placeholder"
        >
          本次 Reason 运行没有可用的 decision_result 审计事件。
        </div>
        <Button
          v-if="canRetrySelectedNode"
          class="graph-retry-button"
          variant="secondary"
          size="sm"
          @click="handleRetryFromFact(selectedNode.id)"
        >
          ↻ 换方向重试
        </Button>
      </div>

      <div class="drawer-section">
        <h4>审计链条 (Audit details)</h4>
        <template v-if="selectedNode.data?.kind === 'reason' && selectedNode.data.reason">
          <div class="meta-row">
            <span class="meta-label">运行状态:</span>
            <span class="meta-value mono">{{ selectedNode.data.reason.status || 'unknown' }}</span>
          </div>
          <div class="meta-row">
            <span class="meta-label">Reason Worker:</span>
            <span class="meta-value mono">{{ selectedNode.data.reason.worker || '未知' }}</span>
          </div>
          <div class="meta-row">
            <span class="meta-label">决策结果:</span>
            <span class="meta-value">{{ selectedNode.data.reason.outcome || '无决策事件' }}</span>
          </div>
          <div v-if="selectedNode.data.reason.confidence !== null" class="meta-row">
            <span class="meta-label">置信度:</span>
            <span class="meta-value mono">
              {{ Math.round(selectedNode.data.reason.confidence * 100) }}%
            </span>
          </div>
          <div class="meta-row">
            <span class="meta-label">开始 / 结束:</span>
            <span class="meta-value mono">
              {{ selectedNode.data.reason.startedAt || '未知' }} /
              {{ selectedNode.data.reason.endedAt || '运行中' }}
            </span>
          </div>
          <div class="meta-row">
            <span class="meta-label">输入 Fact:</span>
            <span class="meta-value mono">
              {{ selectedNode.data.reason.inputNodeIds.join(', ') || '无可关联输入' }}
            </span>
          </div>
          <div class="meta-row">
            <span class="meta-label">输出节点:</span>
            <span class="meta-value mono">
              {{ selectedNode.data.reason.outputNodeIds.join(', ') || '无图谱写入' }}
            </span>
          </div>
          <div v-if="selectedNode.data.reason.blockedBy.length" class="meta-row">
            <span class="meta-label">阻塞项:</span>
            <span class="meta-value">{{ selectedNode.data.reason.blockedBy.length }} 项</span>
          </div>
          <div v-if="selectedNode.data.reason.dependsOnTargets.length" class="meta-row">
            <span class="meta-label">依赖 Target:</span>
            <span class="meta-value"
              >{{ selectedNode.data.reason.dependsOnTargets.length }} 个</span
            >
          </div>
          <div v-if="selectedNode.data.reason.rejectedOptions.length" class="meta-row">
            <span class="meta-label">否决选项:</span>
            <span class="meta-value">{{ selectedNode.data.reason.rejectedOptions.length }} 项</span>
          </div>
        </template>
        <template v-else>
          <div class="meta-row">
            <span class="meta-label">写入者:</span>
            <span class="meta-value mono">{{ selectedNode.data?.creator || 'system' }}</span>
          </div>
          <div class="meta-row">
            <span class="meta-label">创建时间:</span>
            <span class="meta-value mono">{{ selectedNode.data?.createdAt || '未知' }}</span>
          </div>
        </template>
        <div v-if="selectedNode.data?.taskRunId" class="meta-row">
          <span class="meta-label">产生运行 ID:</span>
          <button
            type="button"
            class="meta-value mono link"
            @click="emit('goToRun', selectedNode.data.taskRunId)"
          >
            {{ selectedNode.data.taskRunId }}
          </button>
        </div>
      </div>

      <div v-if="isFactSelectedNode" class="drawer-section">
        <h4>证据列表 (Evidence/Artifacts)</h4>
        <div v-if="evidenceList.length > 0" class="evidence-list">
          <button
            v-for="ev in evidenceList"
            :key="ev.artifact_id"
            type="button"
            class="evidence-row-item"
            @click="emit('previewArtifact', ev.artifact_id)"
          >
            <div class="ev-left">
              <svg
                width="12"
                height="12"
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                stroke-width="2"
                class="text-info"
                aria-hidden="true"
              >
                <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
                <polyline points="14 2 14 8 20 8" />
                <line x1="16" y1="13" x2="8" y2="13" />
                <line x1="16" y1="17" x2="8" y2="17" />
                <polyline points="10 9 9 9 8 9" />
              </svg>
              <span class="ev-name text-truncate">
                {{ ev.artifact?.uri ? ev.artifact.uri.split('/').pop() : ev.artifact_id }}
              </span>
            </div>
            <span v-if="ev.created_at" class="ev-badge success">Validated</span>
          </button>
        </div>
        <div v-else class="empty-placeholder">暂无关联的证据资产文件</div>
      </div>

      <div class="drawer-section">
        <h4>任务运行审计</h4>
        <div v-if="selectedNode.data?.taskRun" class="runs-mini-list">
          <button
            type="button"
            class="run-mini-row"
            @click="emit('goToRun', selectedNode.data?.taskRunId || '')"
          >
            <div class="run-mini-left">
              <span class="live-dot" :class="selectedNode.data?.runStatus" />
              <span class="run-type mono">{{ selectedNode.data?.taskRun?.task_type }}</span>
            </div>
            <span class="run-worker mono">{{ selectedNode.data?.worker }}</span>
          </button>
          <div v-if="selectedNode.data?.taskRun?.error_type" class="run-meta-desc">
            <span class="error-badge"> 错误: {{ selectedNode.data?.taskRun?.error_type }} </span>
            <p class="error-msg text-danger">
              {{ selectedNode.data?.taskRun?.error_message }}
            </p>
          </div>
        </div>
        <div v-else class="empty-placeholder">无活跃或产生它的任务运行记录</div>
      </div>

      <div class="drawer-section graph-raw-data">
        <div class="graph-raw-data-head">
          <h4>原始数据</h4>
          <Button variant="secondary" size="sm" @click="copyRawData">
            <Copy :size="13" :stroke-width="2" aria-hidden="true" />复制 JSON
          </Button>
        </div>
        <pre class="graph-raw-json" tabindex="0">{{ formattedRawData }}</pre>
      </div>
    </div>

    <div v-else-if="activeMode === 'edge' && selectedEdge" class="graph-drawer-body">
      <div class="drawer-section">
        <h4>
          {{
            isNarrativeOutputEdge
              ? '派发探索摘要'
              : isReasonAuditEdge
                ? 'Reason 审计链路'
                : '意图决策描述'
          }}
        </h4>
        <p class="desc-text pre-wrap">
          {{ selectedEdge.data?.label || selectedEdge.data?.intent?.description }}
        </p>
      </div>

      <div v-if="isNarrativeOutputEdge" class="drawer-section">
        <h4>Dispatched Explore / Intent</h4>
        <div
          v-for="narrative in selectedEdge.data?.presentation?.narratives || []"
          :key="narrative.intentId"
          class="decision-block"
        >
          <div class="meta-row">
            <span class="meta-label">Intent:</span>
            <span class="meta-value mono">{{ narrative.intentId }}</span>
          </div>
          <p class="rationale-text">{{ narrative.label }}</p>
          <div class="meta-row">
            <span class="meta-label">状态:</span>
            <span class="meta-value mono">{{ narrative.state || 'unknown' }}</span>
          </div>
          <div class="meta-row">
            <span class="meta-label">Worker / 发起人:</span>
            <span class="meta-value mono">
              {{ narrative.worker || '未认领' }} / {{ narrative.creator || '未知' }}
            </span>
          </div>
          <div v-if="narrative.taskRunId" class="meta-row">
            <span class="meta-label">Explore Run:</span>
            <button
              type="button"
              class="meta-value mono link"
              @click="emit('goToRun', narrative.taskRunId)"
            >
              {{ narrative.taskRunId }}
            </button>
          </div>
          <div v-if="narrative.isCriticalPath" class="meta-row">
            <span class="meta-label">因果路径:</span>
            <span class="meta-value">Critical path</span>
          </div>
        </div>
      </div>

      <div v-if="!isReasonAuditEdge" class="drawer-section">
        <h4>决策推理审计</h4>
        <div v-if="decisionResult" class="decision-block">
          <div class="meta-row">
            <span class="meta-label">决策结果:</span>
            <span class="meta-value text-intent">{{ decisionResult.outcome }}</span>
          </div>
          <div class="meta-row">
            <span class="meta-label">置信度:</span>
            <span class="meta-value mono">{{ decisionResult.confidence }}</span>
          </div>
          <div v-if="decisionResult.blocked" class="meta-row">
            <span class="meta-label">阻塞状态:</span>
            <span class="meta-value text-danger">Blocked</span>
          </div>
          <div class="meta-row">
            <span class="meta-label">产生理由:</span>
            <p class="rationale-text text-muted">{{ decisionResult.rationale }}</p>
          </div>
        </div>
        <div v-else class="empty-placeholder">尚无该意图的最终决策审计事件</div>
      </div>

      <div class="drawer-section">
        <h4>{{ isReasonAuditEdge ? 'Reason 运行关联' : '意图归属 (Claim Historical)' }}</h4>
        <div class="meta-row">
          <span class="meta-label">{{
            isReasonAuditEdge ? '链路方向:' : '当前 Claim 认领者:'
          }}</span>
          <span class="meta-value mono">
            {{
              isReasonAuditEdge
                ? selectedEdge.data?.auditDirection === 'input'
                  ? 'Fact → Reason'
                  : 'Reason → 输出'
                : selectedEdge.data?.worker || '无'
            }}
          </span>
        </div>
        <div class="meta-row">
          <span class="meta-label">{{ isReasonAuditEdge ? 'Reason Worker:' : '发起人:' }}</span>
          <span class="meta-value mono">
            {{ selectedEdge.data?.worker || selectedEdge.data?.creator || '未知' }}
          </span>
        </div>
        <div
          v-if="selectedEdge.data?.presentation?.role === 'reason-input-summary'"
          class="meta-row"
        >
          <span class="meta-label">完整输入:</span>
          <span class="meta-value mono">
            {{ selectedEdge.data.presentation.inputNodeIds?.join(', ') || '无' }}
          </span>
        </div>
        <div v-if="isReasonAuditEdge && selectedEdge.data?.taskRunId" class="meta-row">
          <span class="meta-label">Run ID:</span>
          <button
            type="button"
            class="meta-value mono link"
            @click="emit('goToRun', selectedEdge.data?.taskRunId || '')"
          >
            {{ selectedEdge.data.taskRunId }}
          </button>
        </div>
        <div v-else class="meta-row">
          <span class="meta-label">创建时间:</span>
          <span class="meta-value mono">{{ selectedEdge.data?.createdAt }}</span>
        </div>
      </div>

      <div class="drawer-section graph-raw-data">
        <div class="graph-raw-data-head">
          <h4>原始数据</h4>
          <Button variant="secondary" size="sm" @click="copyRawData">
            <Copy :size="13" :stroke-width="2" aria-hidden="true" />复制 JSON
          </Button>
        </div>
        <pre class="graph-raw-json" tabindex="0">{{ formattedRawData }}</pre>
      </div>
    </div>
  </Drawer>
</template>

<script setup lang="ts">
import { computed, watch } from 'vue'
import { Copy, Workflow } from '@lucide/vue'
import { Button, Drawer } from '@/components/ui'
import { useUIStore } from '@/stores/ui'
import {
  getGraphEdgePresentation,
  getGraphNodePresentation,
  resolveGraphNodeStatus,
} from './graphPresentation'
import type { GraphEdge, GraphEvidenceRecord, GraphNode } from './graphTypes'

interface DecisionResult {
  outcome: string
  confidence: string
  blocked: boolean
  rationale: string
}

const props = defineProps<{
  showNodeDrawer: boolean
  showEdgeDrawer: boolean
  selectedNode: GraphNode | null
  selectedEdge: GraphEdge | null
  evidenceList: GraphEvidenceRecord[]
  decisionResult: DecisionResult | null
  getNodeTypeLabel: (node: GraphNode) => string
}>()

const emit = defineEmits<{
  (event: 'close'): void
  (event: 'goToRun', runId: string): void
  (event: 'previewArtifact', artifactId: string): void
  (event: 'retryFromFact', factId: string): void
}>()
const uiStore = useUIStore()

const activeMode = computed<'node' | 'edge' | null>(() => {
  if (props.showNodeDrawer && props.selectedNode) return 'node'
  if (props.showEdgeDrawer && props.selectedEdge) return 'edge'
  return null
})

const drawerOpen = computed(() => activeMode.value !== null)
const nodePresentation = computed(() => getGraphNodePresentation(props.selectedNode || 'fact'))
const nodeStatus = computed(() =>
  resolveGraphNodeStatus(props.selectedNode || { id: '', data: { status: 'unknown' } }),
)
const edgePresentation = computed(() =>
  getGraphEdgePresentation(
    props.selectedEdge || { id: '', source: '', target: '', type: 'intent' },
  ),
)
const selectionIdentity = computed(() => {
  if (activeMode.value === 'node') return `node:${props.selectedNode?.id || ''}`
  if (activeMode.value === 'edge') return `edge:${props.selectedEdge?.id || ''}`
  return ''
})
const formattedRawData = computed(() => {
  const value = activeMode.value === 'node' ? props.selectedNode?.data : props.selectedEdge?.data
  return JSON.stringify(value || {}, null, 2)
})
let copyGeneration = 0
watch([selectionIdentity, formattedRawData], () => {
  copyGeneration += 1
})
const isReasonAuditEdge = computed(() => props.selectedEdge?.type === 'reason-audit')
const isNarrativeOutputEdge = computed(
  () => props.selectedEdge?.data?.presentation?.role === 'reason-output-intent',
)
const isFactSelectedNode = computed(() =>
  ['fact', 'origin', 'goal'].includes(props.selectedNode?.data?.kind || 'fact'),
)
const canRetrySelectedNode = computed(
  () =>
    props.selectedNode?.data?.kind === 'fact' &&
    props.selectedNode.id !== 'origin' &&
    props.selectedNode.id !== 'goal',
)
const drawerTitle = computed(() =>
  activeMode.value === 'edge' ? '决策意图边详情' : '图谱节点详情',
)
const drawerDescription = computed(() =>
  activeMode.value === 'edge'
    ? '查看意图决策、推理结果与 Claim 审计信息。'
    : '查看节点事实、证据资产与任务运行审计。',
)
const closeLabel = computed(() =>
  activeMode.value === 'edge' ? '关闭决策意图边详情' : '关闭图谱节点详情',
)

function handleOpenChange(value: boolean) {
  if (!value) emit('close')
}

function handleRetryFromFact(factId: string) {
  emit('close')
  emit('retryFromFact', factId)
}

async function copyRawData() {
  const generation = ++copyGeneration
  const identity = selectionIdentity.value
  const text = formattedRawData.value
  try {
    await navigator.clipboard.writeText(text)
    if (identity && identity === selectionIdentity.value && generation === copyGeneration) {
      uiStore.showToast('原始 JSON 已复制', 'success')
    }
  } catch {
    if (identity && identity === selectionIdentity.value && generation === copyGeneration) {
      uiStore.showToast('复制失败，请手动选择文本复制', 'danger')
    }
  }
}

function handleEscapeKeyDown(event: KeyboardEvent) {
  event.stopImmediatePropagation()
}
</script>
