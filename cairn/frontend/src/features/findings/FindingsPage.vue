<template>
  <div class="findings-page">
    <Tabs
      class="findings-route-tabs"
      :model-value="activeView"
      :options="viewOptions"
      variant="line"
      :unmount-on-hide="true"
      @update:model-value="setActiveView"
    >
      <template #panel="{ option }">
        <VulnerabilityManagement v-if="option.value === 'vulnerabilities'" />
        <ReportManagement v-else />
      </template>
    </Tabs>
  </div>
</template>

<script setup lang="ts">
import { computed, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { Tabs, type TabsOption } from '@/components/ui'
import ReportManagement from './ReportManagement.vue'
import VulnerabilityManagement from './VulnerabilityManagement.vue'

type FindingsView = 'vulnerabilities' | 'reports'

const route = useRoute()
const router = useRouter()
const viewOptions = [
  { value: 'vulnerabilities', label: '漏洞管理' },
  { value: 'reports', label: '报告管理' },
] satisfies readonly TabsOption[]

const activeView = computed<FindingsView>(() =>
  route.query.view === 'reports' ? 'reports' : 'vulnerabilities',
)

function setActiveView(view: string) {
  const nextView: FindingsView = view === 'reports' ? 'reports' : 'vulnerabilities'
  const query = { ...route.query }
  const vulnerabilityKeys = [
    'vulnQ',
    'vulnProject',
    'severity',
    'status',
    'vulnerabilityProjectId',
    'vulnerabilityId',
  ]
  const reportKeys = [
    'reportQ',
    'reportProject',
    'reportTemplate',
    'dateFrom',
    'dateTo',
    'page',
    'pageSize',
    'reportProjectId',
    'reportId',
  ]
  for (const key of nextView === 'reports' ? vulnerabilityKeys : reportKeys) delete query[key]
  if (nextView === 'vulnerabilities') delete query.view
  else query.view = nextView
  void router.replace({ query })
}

watch(
  () => route.query.view,
  (view) => {
    if (view !== undefined && view !== 'vulnerabilities' && view !== 'reports')
      setActiveView('vulnerabilities')
  },
  { immediate: true },
)
</script>

<style src="./FindingsPage.css"></style>
