<template>
  <section class="coverage-page">
    <div class="coverage-header coverage-header--actions">
      <Button
        variant="secondary"
        size="sm"
        :loading="isRefreshing"
        :disabled="!currentProjectDetail"
        @click="refreshAll"
      >
        刷新
      </Button>
    </div>

    <div
      v-if="projectInitialLoading"
      class="coverage-project-skeleton"
      role="status"
      aria-live="polite"
    >
      <Skeleton height="7rem" />
      <Skeleton height="12rem" />
      <span class="sr-only">正在读取项目 Coverage 配置…</span>
    </div>

    <ErrorState
      v-else-if="projectInitialError"
      title="项目 Coverage 配置加载失败"
      :description="workspaceStore.error || '无法读取当前项目。'"
      @retry="retryProject"
    />

    <EmptyState
      v-else-if="!isStructured"
      title="当前项目未启用结构化 Coverage 协议"
      description="此页面不使用旧版终态比例推断覆盖质量。"
    />

    <template v-else>
      <Alert v-if="!quality" tone="info" title="质量快照尚未生成">
        Surface observations 与 Coverage Cells 仍按权威接口展示；页面不会从终态数量推断证据合格率。
      </Alert>

      <div class="coverage-metrics">
        <Card class="coverage-metric" padding="sm">
          <span>不同 Manifest Surface</span>
          <strong>{{ manifestSurfaceCount }}</strong>
          <small>
            {{ surfaces.length }} 条 observation · 缺失必测 Surface
            {{ quality ? quality.missing_required_surface_count : '—' }}
          </small>
        </Card>
        <Card class="coverage-metric" padding="sm">
          <span>必测证据合格率</span>
          <strong>{{ quality ? `${requiredPercent}%` : '—' }}</strong>
          <small v-if="quality">
            必测 {{ quality.required.qualified_count }}/{{ quality.required.total_count }} · 可选
            {{ quality.optional.qualified_count }}/{{ quality.optional.total_count }}
          </small>
          <small v-else>等待权威质量快照</small>
        </Card>
        <Card class="coverage-metric" padding="sm">
          <span>评估 / 执行</span>
          <strong v-if="quality">
            {{ quality.charged_attempt_count }} / {{ quality.runtime_execution_count }}
          </strong>
          <strong v-else>—</strong>
          <small v-if="quality">运行时重试 {{ quality.runtime_retry_count }}</small>
          <small v-else>等待权威质量快照</small>
        </Card>
        <Card class="coverage-metric" padding="sm">
          <span>独立验证</span>
          <strong>{{ quality ? quality.independently_verified_finding_count : '—' }}</strong>
          <small v-if="quality">
            待验证 {{ quality.pending_verifier_count }} · 评估 Observation
            {{ quality.observation_count }}
          </small>
          <small v-else>等待权威质量快照</small>
        </Card>
      </div>

      <div class="coverage-summary-grid">
        <Card class="coverage-quality-card">
          <div class="coverage-section-head">
            <div>
              <span class="coverage-kicker">质量快照</span>
              <h3>必测证据覆盖</h3>
            </div>
            <Badge :tone="qualityMeta.tone" size="sm">{{ qualityMeta.label }}</Badge>
          </div>

          <template v-if="quality">
            <div
              class="coverage-progress"
              role="progressbar"
              aria-label="必测 Cell 证据合格率"
              aria-valuemin="0"
              aria-valuemax="100"
              :aria-valuenow="requiredPercent"
            >
              <span :style="{ width: `${requiredPercent}%` }" />
            </div>
            <p class="coverage-progress-text">
              证据合格 {{ quality.required.qualified_count }} /
              {{ quality.required.total_count }}；未合格
              {{ quality.required.unqualified_count }}；未解决
              {{ quality.required.unresolved_count }}。
            </p>

            <dl class="coverage-summary-list">
              <div>
                <dt>漏洞数量</dt>
                <dd>{{ quality.finding_count }}</dd>
              </div>
              <div>
                <dt>状态已确认</dt>
                <dd>{{ quality.confirmed_finding_count }}</dd>
              </div>
              <div>
                <dt>活跃阻断</dt>
                <dd>{{ quality.active_blocker_count }}</dd>
              </div>
              <div>
                <dt>冷却中</dt>
                <dd>{{ quality.active_cooldown_count }}</dd>
              </div>
              <div>
                <dt>{{ CHAIN_CANDIDATE_LABEL }}</dt>
                <dd>{{ quality.chain_candidate_count }}</dd>
              </div>
              <div>
                <dt>{{ CHAIN_VERIFICATION_READY_LABEL }}</dt>
                <dd>{{ quality.chain_verification_ready_count }}</dd>
              </div>
              <div>
                <dt>可选项证据合格</dt>
                <dd>{{ quality.optional.qualified_count }}/{{ quality.optional.total_count }}</dd>
              </div>
              <div>
                <dt>失败尝试</dt>
                <dd>{{ quality.failed_attempt_count }}</dd>
              </div>
            </dl>
          </template>
          <EmptyState
            v-else
            title="必测证据覆盖未知"
            description="等待 assessment quality 快照后再显示合格率、漏洞与验证统计。"
          />
        </Card>

        <Card class="coverage-risks-card">
          <div class="coverage-section-head">
            <div>
              <span class="coverage-kicker">收敛限制</span>
              <h3>残余风险</h3>
            </div>
            <Badge tone="neutral" size="sm">{{ stopReasonText }}</Badge>
          </div>
          <div class="coverage-risks">
            <ul v-if="quality?.residual_risks.length">
              <li v-for="risk in quality.residual_risks" :key="risk">
                {{ residualRiskLabel(risk) }}
              </li>
            </ul>
            <span v-else-if="quality">暂无已登记残余风险</span>
            <span v-else>质量快照尚未生成，残余风险未知</span>
          </div>
        </Card>
      </div>

      <Card class="coverage-cells-card">
        <div class="coverage-section-head">
          <div>
            <span class="coverage-kicker">Surface × 测试族</span>
            <h3>Coverage Cell</h3>
          </div>
          <div class="coverage-heading-status">
            <Spinner v-if="cellsBackgroundFetching" size="sm" decorative />
            <Badge tone="neutral" size="sm">{{ cells.length }} 个 Cell</Badge>
          </div>
        </div>

        <ErrorState
          v-if="cellsInitialError"
          title="Coverage Cell 加载失败"
          :description="errorMessage(cellQuery.error.value)"
          @retry="retryCells"
        />
        <div v-else-if="cellsInitialLoading" class="coverage-table-skeleton" role="status">
          <Skeleton v-for="index in 4" :key="index" height="3.5rem" />
          <span class="sr-only">正在读取权威 Coverage Cell…</span>
        </div>
        <template v-else>
          <Alert v-if="cellsStaleError" tone="warning" title="Coverage Cell 刷新失败">
            {{ errorMessage(cellQuery.error.value) }}
          </Alert>
          <EmptyState
            v-if="cells.length === 0"
            title="尚未登记 Coverage Cell"
            description="结构化 Coverage 已启用，但当前还没有权威 Cell。"
          />
          <DataTable
            v-else
            :rows="cells"
            :columns="cellColumns"
            :row-key="cellRowKey"
            caption="项目 Coverage Cell 列表"
            density="compact"
          >
            <template #cell-surface="{ row }">
              <div class="coverage-cell-stack coverage-cell-surface">
                <strong>{{ row.surface_key || row.description || row.slot_key }}</strong>
                <span>{{ row.description }}</span>
                <code>{{ row.surface_kind }} · {{ row.surface_id || row.slot_key }}</code>
              </div>
            </template>
            <template #cell-family="{ row }">
              <div class="coverage-cell-stack">
                <code>{{ row.test_family || row.kind }}</code>
                <span>{{ row.coverage_rule_id }}</span>
                <span>{{ row.applicability_basis }}</span>
              </div>
            </template>
            <template #cell-requirement="{ row }">
              <div class="coverage-cell-stack">
                <Badge :tone="coverageRequirementMeta(row).tone" size="sm">
                  {{ coverageRequirementMeta(row).label }}
                </Badge>
                <span>{{ row.completion_criteria }}</span>
              </div>
            </template>
            <template #cell-status="{ row }">
              <Badge :tone="coverageStatusMeta(row.status).tone" size="sm">
                {{ coverageStatusMeta(row.status).label }}
              </Badge>
            </template>
            <template #cell-result="{ row }">
              <div class="coverage-cell-stack">
                <Badge :tone="coverageResultMeta(row.result).tone" size="sm">
                  {{ coverageResultMeta(row.result).label }}
                </Badge>
                <span v-if="isCooldownActive(row)">
                  冷却至 {{ dateTime(row.blocker_cooldown_until) }}
                </span>
                <span v-else-if="row.skip_reason">{{ row.skip_reason }}</span>
                <span v-if="row.blocker_fingerprint"> 阻断指纹 {{ row.blocker_fingerprint }} </span>
                <span v-if="row.blocker_strategy_key">策略 {{ row.blocker_strategy_key }}</span>
              </div>
            </template>
            <template #cell-links="{ row }">
              <div class="coverage-links">
                <RouterLink v-if="row.resulting_fact_id" :to="factLink(row.resulting_fact_id)">
                  Fact
                </RouterLink>
                <RouterLink v-if="row.verified_run_id" :to="runLink(row.verified_run_id)">
                  Run
                </RouterLink>
                <RouterLink v-if="row.result === 'finding'" :to="vulnerabilityLink">
                  漏洞
                </RouterLink>
                <span
                  v-if="!row.resulting_fact_id && !row.verified_run_id && row.result !== 'finding'"
                >
                  —
                </span>
              </div>
            </template>
          </DataTable>
        </template>
      </Card>

      <Card class="coverage-manifest-card">
        <div class="coverage-section-head">
          <div>
            <span class="coverage-kicker">Append-only Manifest</span>
            <h3>Surface Manifest Observations</h3>
          </div>
          <div class="coverage-heading-status">
            <Spinner v-if="surfacesBackgroundFetching" size="sm" decorative />
            <Badge tone="neutral" size="sm">
              {{ surfaces.length }} 条 observation · {{ distinctSurfaceCount }} 个 Surface
            </Badge>
          </div>
        </div>

        <ErrorState
          v-if="surfacesInitialError"
          title="Surface Manifest 加载失败"
          :description="errorMessage(surfaceQuery.error.value)"
          @retry="retrySurfaces"
        />
        <div v-else-if="surfacesInitialLoading" class="coverage-manifest-skeleton" role="status">
          <Skeleton v-for="index in 3" :key="index" height="9rem" />
          <span class="sr-only">正在读取 Surface Manifest observations…</span>
        </div>
        <template v-else>
          <Alert v-if="surfacesStaleError" tone="warning" title="Surface Manifest 刷新失败">
            {{ errorMessage(surfaceQuery.error.value) }}
          </Alert>
          <EmptyState
            v-if="surfaces.length === 0"
            title="尚未登记 Surface observation"
            description="当前没有可审阅的 Manifest observation。"
          />
          <div v-else class="coverage-observation-grid">
            <Card
              v-for="surface in surfaces"
              :key="surface.entry_id"
              class="coverage-observation"
              padding="sm"
            >
              <div class="coverage-observation__meta">
                <Badge tone="info" size="sm">{{ surface.surface_kind }}</Badge>
                <code>{{ surface.entry_id }}</code>
              </div>
              <strong>{{ surface.surface_key }}</strong>
              <code>{{ surface.surface_id }}</code>
              <p class="coverage-full-text">{{ surface.description || '无补充描述' }}</p>
              <div class="coverage-evidence-list">
                <RouterLink
                  v-for="evidence in surface.evidence"
                  :key="`${surface.entry_id}:${evidence.artifact_id}:${evidence.pointer || ''}`"
                  :to="artifactLink(evidence.artifact_id)"
                >
                  <strong>{{ evidence.artifact_id }}</strong>
                  <span v-if="evidence.pointer" class="coverage-full-text">
                    {{ evidence.pointer }}
                  </span>
                </RouterLink>
                <span v-if="surface.evidence.length === 0">无证据定位指针</span>
              </div>
            </Card>
          </div>
        </template>
      </Card>
    </template>
  </section>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { useQuery } from '@tanstack/vue-query'

import { listProjectCoverageCells, listProjectSurfaceManifest } from '@/api/client'
import type { CoverageCell, ProjectDetail, SurfaceManifestEntry } from '@/api/types'
import { DataTable, ErrorState, type DataTableColumn } from '@/components/patterns'
import { Alert, Badge, Button, Card, EmptyState, Skeleton, Spinner } from '@/components/ui'
import { dateTime } from '@/lib/format'
import { useUIStore } from '@/stores/ui'
import { useWorkspaceStore } from '@/stores/workspace'
import {
  CHAIN_CANDIDATE_LABEL,
  CHAIN_VERIFICATION_READY_LABEL,
  countDistinctManifestSurfaces,
  coverageRequirementMeta,
  coverageResultMeta,
  coverageStatusMeta,
  coverageStopReasonLabel,
  isCooldownActive,
  qualityStatusMeta,
  qualifiedPercent,
  residualRiskLabel,
} from './coverageModel'
import './CoveragePage.css'

interface CoverageResourceSnapshot<Item> {
  projectId: string
  visitGeneration: number
  items: Item[]
}

type CoverageCellColumnKey = 'surface' | 'family' | 'requirement' | 'status' | 'result' | 'links'

const route = useRoute()
const uiStore = useUIStore()
const workspaceStore = useWorkspaceStore()
const projectId = computed(() => String(route.params.id || ''))
const projectVisitGeneration = ref(projectId.value ? 1 : 0)
const manualRefreshPending = ref(false)

const currentProjectDetail = computed<ProjectDetail | null>(() => {
  const detail = workspaceStore.projectDetail
  return workspaceStore.currentProjectId === projectId.value &&
    detail?.project.id === projectId.value
    ? detail
    : null
})
const project = computed(() => currentProjectDetail.value?.project || null)
const quality = computed(() => project.value?.assessment_quality || null)
const isStructured = computed(
  () =>
    currentProjectDetail.value !== null && project.value?.assessment_protocol === 'default-copy-v1',
)

const surfaceQuery = useQuery<CoverageResourceSnapshot<SurfaceManifestEntry>>({
  queryKey: computed(() => [
    'workspaceCoverageSurfaceManifest',
    projectId.value,
    projectVisitGeneration.value,
  ]),
  queryFn: async ({ signal }) => {
    const requestProjectId = projectId.value
    const visitGeneration = projectVisitGeneration.value
    const items = await listProjectSurfaceManifest(requestProjectId, signal)
    return { projectId: requestProjectId, visitGeneration, items }
  },
  enabled: computed(() => Boolean(projectId.value) && isStructured.value),
  refetchInterval: 10000,
})

const cellQuery = useQuery<CoverageResourceSnapshot<CoverageCell>>({
  queryKey: computed(() => [
    'workspaceCoverageCells',
    projectId.value,
    projectVisitGeneration.value,
  ]),
  queryFn: async ({ signal }) => {
    const requestProjectId = projectId.value
    const visitGeneration = projectVisitGeneration.value
    const items = await listProjectCoverageCells(requestProjectId, signal)
    return { projectId: requestProjectId, visitGeneration, items }
  },
  enabled: computed(() => Boolean(projectId.value) && isStructured.value),
  refetchInterval: 10000,
})

const currentSurfaceSnapshot = computed(() => {
  const snapshot = surfaceQuery.data.value
  return snapshot?.projectId === projectId.value &&
    snapshot.visitGeneration === projectVisitGeneration.value
    ? snapshot
    : null
})
const currentCellSnapshot = computed(() => {
  const snapshot = cellQuery.data.value
  return snapshot?.projectId === projectId.value &&
    snapshot.visitGeneration === projectVisitGeneration.value
    ? snapshot
    : null
})
const surfaces = computed(() => currentSurfaceSnapshot.value?.items || [])
const cells = computed(() => currentCellSnapshot.value?.items || [])

const projectInitialLoading = computed(
  () => !currentProjectDetail.value && (workspaceStore.isLoading || !workspaceStore.error),
)
const projectInitialError = computed(
  () => !currentProjectDetail.value && Boolean(workspaceStore.error) && !workspaceStore.isLoading,
)
const surfacesInitialLoading = computed(
  () => surfaceQuery.isPending.value && currentSurfaceSnapshot.value === null,
)
const surfacesInitialError = computed(
  () => surfaceQuery.isError.value && currentSurfaceSnapshot.value === null,
)
const surfacesStaleError = computed(
  () => surfaceQuery.isError.value && currentSurfaceSnapshot.value !== null,
)
const surfacesBackgroundFetching = computed(
  () => surfaceQuery.isFetching.value && currentSurfaceSnapshot.value !== null,
)
const cellsInitialLoading = computed(
  () => cellQuery.isPending.value && currentCellSnapshot.value === null,
)
const cellsInitialError = computed(
  () => cellQuery.isError.value && currentCellSnapshot.value === null,
)
const cellsStaleError = computed(
  () => cellQuery.isError.value && currentCellSnapshot.value !== null,
)
const cellsBackgroundFetching = computed(
  () => cellQuery.isFetching.value && currentCellSnapshot.value !== null,
)
const isRefreshing = computed(
  () =>
    manualRefreshPending.value ||
    workspaceStore.isLoading ||
    surfaceQuery.isFetching.value ||
    cellQuery.isFetching.value,
)

const distinctSurfaceCount = computed(() => countDistinctManifestSurfaces(surfaces.value))
const manifestSurfaceCount = computed(
  () => quality.value?.manifest_surface_count ?? distinctSurfaceCount.value,
)
const requiredPercent = computed(() => qualifiedPercent(quality.value?.required))
const qualityMeta = computed(() => qualityStatusMeta(quality.value?.status || 'unavailable'))
const stopReasonText = computed(() => {
  const reason = quality.value?.stop_reason || project.value?.coverage_stop_reason
  return reason ? coverageStopReasonLabel(reason) : '尚未收敛'
})

const cellColumns: readonly DataTableColumn<CoverageCell, CoverageCellColumnKey>[] = [
  { key: 'surface', label: 'Surface' },
  { key: 'family', label: '测试族' },
  { key: 'requirement', label: '要求' },
  { key: 'status', label: '状态' },
  { key: 'result', label: '结果与阻断' },
  { key: 'links', label: '证据 / 跳转' },
]

const cellRowKey = (cell: CoverageCell) => cell.slot_key
const errorMessage = (error: unknown) =>
  error instanceof Error ? error.message : error ? String(error) : '未知错误'

const factLink = (factId: string) => ({
  name: 'WorkspaceGraph',
  params: { id: projectId.value },
  query: { nodeId: factId },
})
const runLink = (runId: string) => ({
  name: 'WorkspaceRuns',
  params: { id: projectId.value },
  query: { runId },
})
const artifactLink = (artifactId: string) => ({
  name: 'WorkspaceArtifacts',
  params: { id: projectId.value },
  query: { artifactId },
})
const vulnerabilityLink = computed(() => ({
  name: 'WorkspaceVulnerabilities',
  params: { id: projectId.value },
}))

const retryProject = () => {
  if (projectId.value) void workspaceStore.loadProject(projectId.value)
}
const retrySurfaces = () => void surfaceQuery.refetch()
const retryCells = () => void cellQuery.refetch()

const refreshAll = async () => {
  if (!currentProjectDetail.value) return
  manualRefreshPending.value = true
  uiStore.triggerGlobalRefresh()
  const refreshes: Promise<unknown>[] = []
  if (isStructured.value) refreshes.push(surfaceQuery.refetch(), cellQuery.refetch())
  try {
    await Promise.allSettled(refreshes)
  } finally {
    manualRefreshPending.value = false
  }
}

watch(
  projectId,
  () => {
    projectVisitGeneration.value += 1
  },
  { flush: 'sync' },
)
</script>
