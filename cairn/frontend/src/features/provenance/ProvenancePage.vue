<template>
  <main class="provenance-page">
    <Card
      padding="sm"
      class="provenance-controls"
      :class="{ 'provenance-controls--workspace': isWorkspaceMode }"
    >
      <FormField v-if="!isWorkspaceMode" label="项目" class="provenance-project-field">
        <Select
          v-model="selectedProjectId"
          :options="projectOptions"
          :disabled="isLoadingProjects || !projectOptions.length"
          placeholder="先选择项目"
          aria-label="选择溯源项目"
        />
      </FormField>
      <FilterBar label="成果溯源筛选" class="provenance-filter-bar">
        <Input
          v-model="search"
          aria-label="搜索成果"
          placeholder="搜索标题、目标、类型…"
          :disabled="!effectiveProjectId"
        />
        <template #status>
          <span v-if="isFetchingFindings">正在同步成果…</span>
          <span v-else-if="effectiveProjectId">{{ filteredFindings.length }} 个成果</span>
          <span v-else>请选择项目</span>
        </template>
        <template #actions>
          <Button
            variant="secondary"
            size="sm"
            :loading="isFetchingFindings"
            :disabled="!effectiveProjectId"
            @click="refetchFindings()"
          >
            刷新
          </Button>
        </template>
      </FilterBar>
    </Card>

    <ErrorState
      v-if="projectsError && !projectOptions.length"
      title="项目加载失败"
      :description="errorMessage(projectsError)"
      @retry="refetchProjects()"
    />
    <EmptyState
      v-else-if="!isWorkspaceMode && !isLoadingProjects && !projectOptions.length"
      title="暂无项目"
      description="创建项目后，可在这里选择项目并查看成果溯源。"
    />
    <EmptyState
      v-else-if="!effectiveProjectId"
      title="先选择项目"
      description="选择具体项目后，再从该项目的成果中查看执行溯源。"
    />
    <ErrorState
      v-else-if="findingsError && !findings.length"
      title="成果加载失败"
      :description="errorMessage(findingsError)"
      @retry="refetchFindings()"
    />
    <div v-else-if="isLoadingFindings" class="provenance-loading" role="status">
      <Skeleton
        v-for="index in 4"
        :key="index"
        height="4rem"
        :decorative="false"
        label="正在加载成果"
      />
    </div>
    <EmptyState
      v-else-if="!findings.length"
      title="当前项目暂无可溯源成果"
      description="Worker 确认漏洞并写入 Observation 后，将在这里形成执行溯源。"
    />
    <EmptyState
      v-else-if="!filteredFindings.length"
      title="没有匹配的成果"
      description="请调整搜索条件后重试。"
    />

    <MasterDetail v-else fill-height class="provenance-layout">
      <template #master>
        <Card padding="sm" class="provenance-findings-card">
          <h2>项目成果</h2>
          <div class="provenance-findings" aria-label="成果列表">
            <Button
              v-for="finding in filteredFindings"
              :key="`${finding.project_id}:${finding.id}`"
              variant="ghost"
              size="sm"
              class="provenance-finding"
              :class="{ active: selectedVulnerabilityId === finding.id }"
              :aria-pressed="selectedVulnerabilityId === finding.id"
              @click="selectFinding(finding)"
            >
              <span class="provenance-finding__top">
                <Badge :tone="severityTone(finding.severity)" size="sm">
                  {{ finding.severity }}
                </Badge>
              </span>
              <strong class="provenance-finding__title" :title="finding.title">
                {{ finding.title }}
              </strong>
              <span class="provenance-finding__meta mono" :title="findingMetadata(finding)">
                {{ findingMetadata(finding) }}
              </span>
            </Button>
          </div>
        </Card>
      </template>

      <template #detail>
        <section ref="detailPane" class="provenance-detail">
          <EmptyState
            v-if="!selectedVulnerabilityId"
            title="选择一个成果"
            description="选择后查看发现与验证链，并定位具体 Run 的操作和日志。"
          />
          <ErrorState
            v-else-if="provenanceError && !provenance"
            title="溯源加载失败"
            :description="errorMessage(provenanceError)"
            @retry="refetchProvenance()"
          />
          <div v-else-if="isLoadingProvenance" class="provenance-loading" role="status">
            <Skeleton height="8rem" :decorative="false" label="正在加载溯源" />
            <Skeleton height="16rem" :decorative="false" label="正在加载运行记录" />
          </div>
          <template v-else-if="provenance">
            <Card padding="sm" class="provenance-summary">
              <div class="provenance-summary__title">
                <div>
                  <Badge :tone="severityTone(provenance.vulnerability.severity)" size="sm">
                    {{ provenance.vulnerability.severity }}
                  </Badge>
                  <h2>{{ provenance.vulnerability.title }}</h2>
                </div>
                <Badge size="sm">{{ provenance.observations.length }} 次发现</Badge>
              </div>
              <p>{{ provenance.vulnerability.target || '未记录影响目标' }}</p>
              <Alert
                v-for="warning in provenanceWarnings"
                :key="warning"
                tone="warning"
                class="provenance-warning"
                >{{ warning }}</Alert
              >
            </Card>

            <Card padding="none" class="provenance-chain-card">
              <div class="provenance-chain-grid">
                <section
                  class="provenance-chain-section"
                  aria-labelledby="provenance-discovery-title"
                >
                  <h2 id="provenance-discovery-title">发现与验证链</h2>
                  <div class="provenance-observations">
                    <Button
                      v-for="observation in provenance.observations"
                      :key="observation.id"
                      variant="secondary"
                      size="sm"
                      class="provenance-observation"
                      :class="{ active: selectedObservationId === observation.id }"
                      :aria-pressed="selectedObservationId === observation.id"
                      @click="selectObservation(observation)"
                    >
                      <strong>{{ formatTime(observation.observed_at) }}</strong>
                      <span>{{ observation.worker || '未知 Worker' }}</span>
                      <code>#{{ shortId(observation.task_run_id || observation.id) }}</code>
                    </Button>
                  </div>
                  <div v-if="!provenance.observations.length" class="provenance-empty-note">
                    历史成果缺少 Observation，已按 canonical 来源回退。
                  </div>
                  <div v-if="provenance.verifications.length" class="provenance-verifications">
                    <h3>独立验证</h3>
                    <Button
                      v-for="verification in provenance.verifications"
                      :key="verification.id"
                      variant="ghost"
                      size="sm"
                      class="provenance-verification"
                      @click="selectRun(verification.verifier_task_run_id)"
                    >
                      <Badge :tone="verificationTone(verification.outcome)" size="sm">
                        {{ verification.outcome }}
                      </Badge>
                      <span>{{ verification.verifier_worker }}</span>
                      <code>#{{ shortId(verification.verifier_task_run_id) }}</code>
                    </Button>
                  </div>
                </section>

                <section class="provenance-chain-section" aria-labelledby="provenance-runs-title">
                  <h2 id="provenance-runs-title">关联运行</h2>
                  <div class="provenance-runs">
                    <Button
                      v-for="run in provenance.task_runs"
                      :key="run.id"
                      variant="ghost"
                      size="sm"
                      class="provenance-run-option"
                      :class="{ active: selectedRunId === run.id }"
                      :aria-pressed="selectedRunId === run.id"
                      @click="selectRun(run.id)"
                    >
                      <span>{{ run.task_type }}</span>
                      <StatusBadge :status="run.status" size="sm" />
                      <code>#{{ shortId(run.id) }}</code>
                    </Button>
                  </div>
                  <div v-if="!provenance.task_runs.length" class="provenance-empty-note">
                    没有可关联的运行记录。
                  </div>
                </section>
              </div>
            </Card>

            <Card padding="none" class="provenance-run-card">
              <div class="provenance-run-header">
                <div>
                  <h2>执行操作与日志</h2>
                  <p v-if="selectedRun">
                    {{ selectedRun.worker_name }} · {{ selectedRun.task_type }} · #{{
                      selectedRun.id
                    }}
                  </p>
                  <p v-else>选择关联 Run 后查看完整操作。</p>
                </div>
                <Spinner v-if="isFetchingRunDetails" size="sm" label="正在同步运行详情" />
              </div>

              <ErrorState
                v-if="runDetailsError && selectedRunId"
                title="运行详情加载失败"
                :description="errorMessage(runDetailsError)"
                @retry="refetchRunDetails()"
              />
              <EmptyState
                v-else-if="!selectedRunId"
                title="没有选中的 Run"
                description="选择发现或验证记录以载入操作时间线。"
              />
              <Tabs
                v-else
                v-model="activeView"
                :options="viewOptions"
                activation-mode="manual"
                variant="line"
                class="provenance-tabs"
              >
                <template #panel="{ option }">
                  <div v-if="option.value === 'timeline'" class="provenance-timeline">
                    <details v-for="item in timeline" :key="item.id" class="provenance-event">
                      <summary>
                        <time>{{ formatTime(item.ts) }}</time>
                        <Badge size="sm">{{ item.kind }}</Badge>
                        <strong>{{ item.title }}</strong>
                        <Badge
                          :tone="item.incomplete ? 'warning' : statusTone(item.status)"
                          size="sm"
                        >
                          {{ item.incomplete ? '记录不完整' : item.status }}
                        </Badge>
                        <span>{{ item.summary || '—' }}</span>
                      </summary>
                      <pre class="provenance-json mono">{{
                        JSON.stringify(item.payload, null, 2)
                      }}</pre>
                    </details>
                    <EmptyState
                      v-if="!timeline.length && !isFetchingRunDetails"
                      title="暂无操作事件"
                      description="旧 Run 可能未开启 Hook；这不代表 Worker 没有执行操作。"
                    />
                  </div>

                  <div v-else-if="option.value === 'terminal'" class="provenance-terminal">
                    <div class="provenance-output-nav">
                      <Button
                        v-for="output in terminalOutputs"
                        :key="output.id"
                        variant="secondary"
                        size="sm"
                        :aria-pressed="selectedOutputId === output.id"
                        @click="selectOutput(output.id)"
                        >{{ output.label }}</Button
                      >
                    </div>
                    <Alert v-if="selectedOutput?.captureTruncated" tone="warning">
                      此日志采集时已截断；已丢弃的中间内容无法恢复。
                    </Alert>
                    <a
                      v-if="selectedOutputId"
                      :href="artifactDownloadUrl(selectedOutputId)"
                      download
                      >下载已保存日志</a
                    >
                    <pre v-if="terminalPreview?.content" class="provenance-terminal__screen mono">{{
                      terminalPreview.content
                    }}</pre>
                    <Spinner v-else-if="isFetchingTerminal" size="sm" label="正在加载终端日志" />
                    <EmptyState
                      v-else
                      title="暂无终端日志"
                      description="该 Run 没有 stdout、stderr 或诊断 Artifact。"
                    />
                  </div>

                  <div v-else class="provenance-evidence">
                    <section>
                      <h3>来源 Facts</h3>
                      <div
                        v-for="fact in provenance.facts"
                        :key="fact.id"
                        class="provenance-record"
                      >
                        <code>{{ fact.id }}</code
                        ><span>{{ fact.description }}</span>
                      </div>
                      <p v-if="!provenance.facts.length">暂无关联 Fact。</p>
                    </section>
                    <section>
                      <h3>证据与产物</h3>
                      <Button
                        v-for="artifact in provenance.artifacts"
                        :key="artifact.id"
                        variant="ghost"
                        size="sm"
                        @click="openArtifact(artifact.id)"
                      >
                        <code>{{ artifact.kind }}</code
                        ><span>{{ artifact.uri }}</span>
                      </Button>
                      <p v-if="!provenance.artifacts.length">暂无关联 Artifact。</p>
                    </section>
                  </div>
                </template>
              </Tabs>
            </Card>
          </template>
        </section>
      </template>
    </MasterDetail>
  </main>
</template>

<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useQuery } from '@tanstack/vue-query'
import {
  api,
  listVulnerabilities,
  type ArtifactPreview,
  type Vulnerability,
  type VulnerabilityListItem,
  type VulnerabilityObservation,
  type VulnerabilityVerification,
} from '@/api/client'
import type {
  Fact,
  FactEvidence,
  Intent,
  ProjectSummary,
  TaskRunResultEventGroup,
} from '@/api/types'
import { ErrorState, FilterBar, MasterDetail } from '@/components/patterns'
import {
  Alert,
  Badge,
  Button,
  Card,
  EmptyState,
  FormField,
  Input,
  Select,
  Skeleton,
  Spinner,
  StatusBadge,
  Tabs,
  type BadgeTone,
} from '@/components/ui'
import { buildTerminalOutputs, type TerminalOutput } from '@/features/runs/runTerminalModel'
import type {
  AgentEvent,
  RunArtifact,
  TaskRun,
  TaskRunCommandEvent,
  TaskRunEventGroup,
  TaskRunHookEvent,
  TaskRunPromptEvent,
} from '@/features/runs/runTypes'
import { buildProvenanceTimeline } from './provenanceModel'

interface ProvenanceRelation {
  source_id: string
  target_id: string
  relation: string
  source?: string
}

interface VulnerabilityProvenance {
  vulnerability: Vulnerability
  observations: VulnerabilityObservation[]
  verifications: VulnerabilityVerification[]
  facts: Fact[]
  intents: Intent[]
  task_runs: TaskRun[]
  artifacts: RunArtifact[]
  evidence: FactEvidence[]
  relations: ProvenanceRelation[]
  warnings?: string[]
  completeness_warnings?: string[]
}

const props = defineProps<{ fixedProjectId?: string }>()
const route = useRoute()
const router = useRouter()
const queryString = (value: unknown) => (typeof value === 'string' ? value : '')
const viewOptions = [
  { label: '操作时间线', value: 'timeline' },
  { label: '终端日志', value: 'terminal' },
  { label: '结果与证据', value: 'evidence' },
]
const requestedView = queryString(route.query.view)

const search = ref('')
const activeView = ref(
  viewOptions.some((option) => option.value === requestedView) ? requestedView : 'timeline',
)
const selectedProjectId = ref(props.fixedProjectId || queryString(route.query.projectId))
const selectedVulnerabilityId = ref(queryString(route.query.vulnerabilityId))
const selectedObservationId = ref(queryString(route.query.observationId))
const selectedRunId = ref(queryString(route.query.runId))
const selectedOutputId = ref('')
const isWorkspaceMode = computed(() => Boolean(props.fixedProjectId))

const projectsQuery = useQuery<ProjectSummary[]>({
  queryKey: ['provenance', 'projects'],
  queryFn: ({ signal }) => api('GET', '/projects', undefined, signal),
  enabled: computed(() => !isWorkspaceMode.value),
})
const projects = computed(() => projectsQuery.data.value || [])
const projectOptions = computed(() =>
  projects.value.map((project) => ({ value: project.id, label: project.title })),
)
const effectiveProjectId = computed(() => {
  if (props.fixedProjectId) return props.fixedProjectId
  if (!projectsQuery.isSuccess.value) return ''
  return projects.value.some((project) => project.id === selectedProjectId.value)
    ? selectedProjectId.value
    : ''
})

const findingsQuery = useQuery({
  queryKey: ['provenance', 'findings', effectiveProjectId],
  queryFn: ({ queryKey, signal }) =>
    listVulnerabilities(String(queryKey[2]), undefined, undefined, signal),
  enabled: computed(() => Boolean(effectiveProjectId.value)),
})

const findings = computed<(Vulnerability | VulnerabilityListItem)[]>(() =>
  (findingsQuery.data.value || []).filter(
    (finding) => finding.project_id === effectiveProjectId.value,
  ),
)
const filteredFindings = computed(() => {
  const needle = search.value.trim().toLowerCase()
  if (!needle) return findings.value
  return findings.value.filter((finding) =>
    [finding.title, finding.target, finding.vuln_type, finding.id]
      .join(' ')
      .toLowerCase()
      .includes(needle),
  )
})

function findingMetadata(finding: Vulnerability | VulnerabilityListItem): string {
  return finding.target || finding.vuln_type || finding.id
}

const provenanceQuery = useQuery<VulnerabilityProvenance>({
  queryKey: ['provenance', effectiveProjectId, selectedVulnerabilityId],
  queryFn: ({ queryKey, signal }) =>
    api(
      'GET',
      `/projects/${encodeURIComponent(String(queryKey[1]))}/vulnerabilities/${encodeURIComponent(String(queryKey[2]))}/provenance`,
      undefined,
      signal,
    ),
  enabled: computed(() => Boolean(effectiveProjectId.value && selectedVulnerabilityId.value)),
})
const provenance = computed(() => provenanceQuery.data.value || null)
const provenanceWarnings = computed(
  () => provenance.value?.completeness_warnings || provenance.value?.warnings || [],
)
const selectedRun = computed(
  () => provenance.value?.task_runs.find((run) => run.id === selectedRunId.value) || null,
)

const runDetailsQuery = useQuery({
  queryKey: ['provenance', 'run-details', effectiveProjectId, selectedRunId],
  queryFn: async ({ queryKey, signal }) => {
    const projectId = encodeURIComponent(String(queryKey[2]))
    const rawRunId = String(queryKey[3])
    const runId = encodeURIComponent(rawRunId)
    const [agentEvents, commands, hooks, prompts, results, artifacts] = await Promise.all([
      api<AgentEvent[]>(
        'GET',
        `/projects/${projectId}/task-runs/${runId}/agent-events`,
        undefined,
        signal,
      ),
      api<TaskRunEventGroup<TaskRunCommandEvent>[]>(
        'GET',
        `/projects/${projectId}/task-runs/command-events?task_run_id=${runId}`,
        undefined,
        signal,
      ),
      api<TaskRunEventGroup<TaskRunHookEvent>[]>(
        'GET',
        `/projects/${projectId}/task-runs/hook-events?task_run_id=${runId}`,
        undefined,
        signal,
      ),
      api<TaskRunEventGroup<TaskRunPromptEvent>[]>(
        'GET',
        `/projects/${projectId}/task-runs/prompt-events?task_run_id=${runId}`,
        undefined,
        signal,
      ),
      api<TaskRunResultEventGroup[]>(
        'GET',
        `/projects/${projectId}/task-runs/result-events?task_run_id=${runId}`,
        undefined,
        signal,
      ),
      api<RunArtifact[]>(
        'GET',
        `/projects/${projectId}/artifacts?task_run_id=${runId}`,
        undefined,
        signal,
      ),
    ])
    const group = <T,>(groups: TaskRunEventGroup<T>[]): T[] =>
      groups.find((item) => item.task_run_id === rawRunId)?.events || []
    return {
      agentEvents,
      commands: group(commands),
      hooks: group(hooks),
      prompts: group(prompts),
      results: results[0]?.events || [],
      artifacts,
    }
  },
  enabled: computed(() => Boolean(effectiveProjectId.value && selectedRunId.value)),
})

const runDetails = computed(() => runDetailsQuery.data.value)
const timeline = computed(() =>
  buildProvenanceTimeline({
    hooks: runDetails.value?.hooks,
    commands: runDetails.value?.commands,
    prompts: runDetails.value?.prompts,
    results: runDetails.value?.results,
    artifacts: runDetails.value?.artifacts,
  }),
)
const terminalOutputs = computed<TerminalOutput[]>(() =>
  buildTerminalOutputs(runDetails.value?.artifacts || []),
)
const selectedOutput = computed(
  () => terminalOutputs.value.find((output) => output.id === selectedOutputId.value) || null,
)
const terminalQuery = useQuery<ArtifactPreview>({
  queryKey: ['provenance', 'terminal', effectiveProjectId, selectedOutputId],
  queryFn: ({ queryKey, signal }) =>
    api(
      'GET',
      `/projects/${encodeURIComponent(String(queryKey[2]))}/artifacts/${encodeURIComponent(String(queryKey[3]))}/preview-tail`,
      undefined,
      signal,
    ),
  enabled: computed(() => Boolean(effectiveProjectId.value && selectedOutputId.value)),
})
const terminalPreview = computed(() => terminalQuery.data.value || null)

const detailPane = ref<HTMLElement | null>(null)

const resetRunSelection = () => {
  selectedRunId.value = ''
  selectedOutputId.value = ''
}
const resetVulnerabilitySelection = () => {
  selectedVulnerabilityId.value = ''
  selectedObservationId.value = ''
  resetRunSelection()
}
const selectFinding = async (finding: Vulnerability | VulnerabilityListItem) => {
  if (selectedVulnerabilityId.value === finding.id) return
  selectedVulnerabilityId.value = finding.id
  selectedObservationId.value = ''
  resetRunSelection()
  await nextTick()
  if (detailPane.value) detailPane.value.scrollTop = 0
}
const selectObservation = (observation: VulnerabilityObservation) => {
  selectedObservationId.value = observation.id
  selectRun(observation.task_run_id || '')
}
const selectRun = (runId: string) => {
  if (selectedRunId.value === runId) return
  selectedRunId.value = runId
  selectedOutputId.value = ''
}
const selectOutput = (artifactId: string) => {
  selectedOutputId.value = artifactId
}

watch(selectedProjectId, (projectId, previousProjectId) => {
  if (isWorkspaceMode.value || projectId === previousProjectId) return
  search.value = ''
  activeView.value = 'timeline'
  resetVulnerabilitySelection()
})

watch(
  () => props.fixedProjectId,
  (projectId, previousProjectId) => {
    if (projectId === previousProjectId) return
    selectedProjectId.value = projectId || ''
    search.value = ''
    activeView.value = 'timeline'
    resetVulnerabilitySelection()
  },
)

watch(
  [projects, () => projectsQuery.isSuccess.value],
  ([value, isSuccess]) => {
    if (isWorkspaceMode.value || !isSuccess) return
    if (selectedProjectId.value && !value.some((project) => project.id === selectedProjectId.value))
      selectedProjectId.value = ''
  },
  { immediate: true },
)

watch(
  [findings, () => findingsQuery.isSuccess.value],
  ([value, isSuccess]) => {
    if (!isSuccess || !effectiveProjectId.value) return
    if (!value.length) {
      resetVulnerabilitySelection()
      return
    }
    if (value.some((finding) => finding.id === selectedVulnerabilityId.value)) return
    const requestedVulnerability = queryString(route.query.vulnerabilityId)
    selectFinding(value.find((finding) => finding.id === requestedVulnerability) || value[0])
  },
  { immediate: true },
)

watch(
  provenance,
  (value) => {
    if (!value) return
    const requestedObservation =
      selectedObservationId.value || queryString(route.query.observationId)
    const observation =
      value.observations.find((item) => item.id === requestedObservation) ||
      value.observations[value.observations.length - 1]
    selectedObservationId.value = observation?.id || ''
    const requestedRun = selectedRunId.value || queryString(route.query.runId)
    selectRun(
      value.task_runs.some((run) => run.id === requestedRun)
        ? requestedRun
        : observation?.task_run_id || value.task_runs[0]?.id || '',
    )
  },
  { immediate: true },
)

watch(
  terminalOutputs,
  (outputs) => {
    if (!outputs.some((output) => output.id === selectedOutputId.value))
      selectedOutputId.value = outputs[0]?.id || ''
  },
  { immediate: true },
)

watch(
  [effectiveProjectId, selectedVulnerabilityId, selectedObservationId, selectedRunId, activeView],
  () => {
    void router.replace({
      query: {
        ...route.query,
        projectId: isWorkspaceMode.value ? undefined : effectiveProjectId.value || undefined,
        vulnerabilityId: selectedVulnerabilityId.value || undefined,
        observationId: selectedObservationId.value || undefined,
        runId: selectedRunId.value || undefined,
        view: selectedVulnerabilityId.value ? activeView.value : undefined,
      },
    })
  },
)

const openArtifact = (artifactId: string) =>
  void router.push(
    `/projects/${encodeURIComponent(effectiveProjectId.value)}/artifacts?artifactId=${encodeURIComponent(artifactId)}`,
  )
const artifactDownloadUrl = (artifactId: string) =>
  `/projects/${encodeURIComponent(effectiveProjectId.value)}/artifacts/${encodeURIComponent(artifactId)}/content`
const shortId = (value: string) => value.slice(0, 10)
const formatTime = (value: string) => (value ? new Date(value).toLocaleString('zh-CN') : '未知时间')
const errorMessage = (error: unknown) =>
  error instanceof Error ? error.message : String(error || '未知错误')
const severityTone = (severity: string): BadgeTone =>
  severity === 'critical'
    ? 'danger'
    : severity === 'high'
      ? 'warning'
      : severity === 'medium'
        ? 'info'
        : 'neutral'
const verificationTone = (outcome: string): BadgeTone =>
  outcome === 'confirmed' ? 'success' : outcome === 'not_reproduced' ? 'danger' : 'warning'
const statusTone = (status: string): BadgeTone =>
  ['success', 'succeeded', 'valid', 'created'].includes(status)
    ? 'success'
    : ['failed', 'invalid', 'denied'].includes(status)
      ? 'danger'
      : ['planned', 'started', 'running'].includes(status)
        ? 'info'
        : 'neutral'

const {
  isLoading: isLoadingProjects,
  error: projectsError,
  refetch: refetchProjects,
} = projectsQuery
const {
  isLoading: isLoadingFindings,
  isFetching: isFetchingFindings,
  error: findingsError,
  refetch: refetchFindings,
} = findingsQuery
const {
  isLoading: isLoadingProvenance,
  error: provenanceError,
  refetch: refetchProvenance,
} = provenanceQuery
const {
  isFetching: isFetchingRunDetails,
  error: runDetailsError,
  refetch: refetchRunDetails,
} = runDetailsQuery
const { isFetching: isFetchingTerminal } = terminalQuery
</script>

<style src="./ProvenancePage.css" scoped></style>
