<template>
  <div class="overview-container console-page">
    <header class="page-header">
      <h1 class="page-title">仪表盘</h1>
      <div class="overview-actions">
        <Button variant="ghost" :disabled="isFetching" @click="refetch()">
          <template #icon><RefreshCw :size="16" /></template>
          {{ isFetching ? '更新中' : '刷新' }}
        </Button>
        <RouterLink :to="{ name: 'Projects' }">任务管理</RouterLink>
        <RouterLink :to="{ name: 'Reports' }">查看报告</RouterLink>
      </div>
    </header>

    <Alert v-if="!data && isError" tone="danger" role="alert">
      仪表盘加载失败，请重试。
      <Button variant="secondary" size="sm" :disabled="isFetching" @click="refetch()">重试</Button>
    </Alert>
    <div v-else-if="!data" class="overview-loading" role="status">
      <Spinner size="sm" decorative /> 正在加载仪表盘…
    </div>

    <template v-if="data">
      <div class="overview-freshness" aria-live="polite">
        <span v-if="isError" class="overview-update-error">更新失败，显示上次数据 · </span>
        <span
          >数据时间
          <time :datetime="data.generated_at">{{ dateTime(data.generated_at) }}</time></span
        >
      </div>
      <dl class="overview-stats" aria-label="当前可见范围统计">
        <div>
          <dt>进行中项目</dt>
          <dd>{{ data.projects.by_status.active ?? 0 }}</dd>
        </div>
        <div>
          <dt>待启动项目</dt>
          <dd>{{ data.projects.by_status.pending ?? 0 }}</dd>
        </div>
        <div>
          <dt>漏洞记录</dt>
          <dd>{{ data.vulnerabilities.total }}</dd>
        </div>
        <div>
          <dt>严重／高危记录</dt>
          <dd>{{ elevatedCount }}</dd>
        </div>
      </dl>

      <div class="overview-main">
        <section class="overview-panel" aria-labelledby="overview-projects-title">
          <div class="overview-section-header">
            <h2 id="overview-projects-title">项目概览</h2>
            <RouterLink :to="{ name: 'Projects' }">全部项目</RouterLink>
          </div>
          <p id="overview-project-scope" class="sr-only">
            仅统计普通项目，不含复测内部项目与尚未生成项目的计划。
          </p>
          <div v-if="data.project_previews.length" class="overview-table-scroll">
            <table
              class="overview-table overview-projects"
              aria-describedby="overview-project-scope"
            >
              <thead>
                <tr>
                  <th scope="col">名称</th>
                  <th scope="col">状态</th>
                  <th scope="col">创建时间</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="project in data.project_previews" :key="project.id">
                  <td>
                    <RouterLink
                      class="overview-name"
                      :to="{ name: 'WorkspaceGraph', params: { id: project.id } }"
                      >{{ project.title }}</RouterLink
                    >
                  </td>
                  <td>
                    <Badge :tone="resolveProjectStatus(project.status).tone" size="sm">{{
                      resolveProjectStatus(project.status).label
                    }}</Badge>
                  </td>
                  <td class="overview-time">
                    <time :datetime="project.created_at">{{ dateTime(project.created_at) }}</time>
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
          <EmptyState v-else title="暂无项目" description="前往任务管理开始创建项目。" />
        </section>

        <section class="overview-panel overview-severity" aria-labelledby="overview-severity-title">
          <div class="overview-section-header">
            <h2 id="overview-severity-title">严重度概览</h2>
          </div>
          <table class="overview-severity-table">
            <caption class="sr-only">
              当前身份可见的各状态漏洞记录；条形以零为共同起点，按最高数量缩放。
            </caption>
            <thead class="sr-only">
              <tr>
                <th scope="col">严重度</th>
                <th scope="col">数量比较</th>
                <th scope="col">记录数</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="row in severityRows" :key="row.key">
                <th scope="row">
                  <Tooltip :content="row.description" side="top">
                    <RouterLink
                      :to="{ name: 'Findings', query: { severity: row.key } }"
                      :aria-label="row.description"
                    >
                      {{ row.label }}
                    </RouterLink>
                  </Tooltip>
                </th>
                <td class="overview-bar-cell">
                  <span class="overview-bar-track" aria-hidden="true">
                    <span
                      class="overview-bar"
                      :class="`overview-severity-${row.key}`"
                      :style="{ width: `${row.widthPercent}%` }"
                    ></span>
                  </span>
                </td>
                <td class="overview-count">{{ row.count }}</td>
              </tr>
            </tbody>
          </table>
        </section>
      </div>

      <section class="overview-panel" aria-labelledby="overview-findings-title">
        <div class="overview-section-header">
          <h2 id="overview-findings-title">最近漏洞记录</h2>
          <RouterLink :to="{ name: 'Findings' }">全部漏洞</RouterLink>
        </div>
        <p id="overview-vulnerability-scope" class="sr-only">
          当前身份可见的全部生命周期状态，含获准查看的复测来源；时间为记录创建时间。
        </p>
        <div v-if="data.vulnerability_previews.length" class="overview-table-scroll">
          <table
            class="overview-table overview-findings"
            aria-describedby="overview-vulnerability-scope"
          >
            <thead>
              <tr>
                <th scope="col">标题</th>
                <th scope="col">严重度</th>
                <th scope="col">状态</th>
                <th scope="col">所属项目</th>
                <th scope="col">记录时间</th>
              </tr>
            </thead>
            <tbody>
              <tr
                v-for="item in data.vulnerability_previews"
                :key="`${item.project_id}:${item.id}`"
              >
                <td>
                  <RouterLink
                    class="overview-name"
                    :to="{
                      name: 'Findings',
                      query: { vulnerabilityProjectId: item.project_id, vulnerabilityId: item.id },
                    }"
                    >{{ item.title }}</RouterLink
                  >
                </td>
                <td>{{ vulnerabilitySeverityMeta(item.severity).label }}</td>
                <td>
                  <Badge :tone="vulnerabilityStatusMeta(item.status).tone" size="sm">{{
                    vulnerabilityStatusMeta(item.status).label
                  }}</Badge>
                </td>
                <td class="overview-project-title">{{ item.project_title }}</td>
                <td class="overview-time">
                  <time :datetime="item.created_at">{{ dateTime(item.created_at) }}</time>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
        <EmptyState v-else title="暂无漏洞记录" />
      </section>
    </template>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { RouterLink } from 'vue-router'
import { useQuery } from '@tanstack/vue-query'
import { RefreshCw } from '@lucide/vue'
import { getOverview, overviewKey } from '@/api/overview'
import { Alert, Badge, Button, EmptyState, Spinner, Tooltip } from '@/components/ui'
import { resolveProjectStatus } from '@/features/projects/workspaceStatus'
import {
  vulnerabilitySeverityMeta,
  vulnerabilityStatusMeta,
} from '@/features/vulnerabilities/vulnerabilityMetadata'
import { dateTime } from '@/lib/format'
import { useAuthStore } from '@/stores/auth'
import { buildSeverityRows, overviewRefreshInterval } from './overviewSeverityModel'

defineOptions({ name: 'OverviewPage' })

const authStore = useAuthStore()
const visible = ref(document.visibilityState === 'visible')
const updateVisibility = () => {
  visible.value = document.visibilityState === 'visible'
}
onMounted(() => document.addEventListener('visibilitychange', updateVisibility))
onUnmounted(() => document.removeEventListener('visibilitychange', updateVisibility))

const { data, isError, isFetching, refetch } = useQuery({
  queryKey: computed(() => overviewKey(authStore.session)),
  queryFn: ({ signal }) => getOverview(signal),
  enabled: computed(() => authStore.isAuthenticated && visible.value),
  staleTime: 30_000,
  retry: false,
  refetchOnWindowFocus: 'always',
  refetchInterval: (query) => overviewRefreshInterval(query.state.data, visible.value),
  refetchIntervalInBackground: false,
})

const severityRows = computed(() =>
  data.value ? buildSeverityRows(data.value.vulnerabilities) : [],
)
const elevatedCount = computed(
  () =>
    (data.value?.vulnerabilities.by_severity.critical ?? 0) +
    (data.value?.vulnerabilities.by_severity.high ?? 0),
)
</script>

<style src="./Overview.css" scoped></style>
