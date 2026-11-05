<template>
  <Popover v-model:open="open" align="start" size="lg">
    <template #trigger>
      <button type="button" class="project-observation-trigger" aria-label="运行详情">
        <span class="project-observation-status">
          <Badge :tone="status.tone" size="sm" dot aria-label="项目状态">{{ status.label }}</Badge>
          <ChevronDown :size="12" aria-hidden="true" />
        </span>
        <span v-if="project.coverage_stop_reason === 'pause_timeout'" class="project-observation-notice">暂停超时</span>
        <span
          v-if="observation.notice"
          class="project-observation-notice"
          :data-tone="observation.tone"
        >
          {{ observation.notice }}
        </span>
      </button>
    </template>
    <section class="project-observation-details" aria-label="运行详情">
      <header>
        <strong>运行详情</strong>
        <Button size="sm" variant="ghost" :loading="fetching" @click="$emit('refresh')"
          >刷新观测</Button
        >
      </header>
      <p v-if="observation.notice" class="project-observation-notice" :data-tone="observation.tone">
        {{ observation.notice }}<template v-if="failed && snapshot">，显示上次读取内容</template>
      </p>
      <p v-if="project.status === 'paused' && !project.verification_request_id && runtime?.actions?.finish?.reason !== 'verification_request_managed'" class="project-observation-policy">暂停满12小时将自动结束，工作文件保留</p>
      <p v-if="project.coverage_stop_reason === 'pause_timeout'" class="project-observation-notice">连续暂停满12小时，已自动请求结束本次评估，工作文件保留。此操作不关闭周期计划。</p>
      <dl v-if="control" aria-label="执行控制回执">
        <div>
          <dt>控制请求</dt>
          <dd>{{ control.actionLabel }}</dd>
        </div>
        <div>
          <dt>受理时间</dt>
          <dd>{{ control.control.requested_at || '未提供' }}</dd>
        </div>
        <div>
          <dt>物理状态</dt>
          <dd>{{ control.stateText }}</dd>
        </div>
        <div>
          <dt>物理确认时间</dt>
          <dd>{{ control.confirmed ? control.control.confirmed_at : '未确认' }}</dd>
        </div>
        <div>
          <dt>资源观测</dt>
          <dd>
            {{
              control.confirmed ? '本次回执的所属执行资源' : '前次观测／本次未确认，不代表本次控制已生效'
            }}
          </dd>
        </div>
        <div v-if="control.control.resources_request_id">
          <dt>资源观测来源</dt>
          <dd>{{ control.control.resources_request_id }} · {{ control.control.resources_observed_at || '观测时间未记录' }}</dd>
        </div>
        <div v-for="(resource, index) in control.resources" :key="index">
          <dt>执行资源 {{ index + 1 }}</dt>
          <dd>{{ describeResource(resource) }}</dd>
        </div>
        <div v-if="!control.resources.length">
          <dt>执行资源</dt>
          <dd>{{ control.confirmed ? '当前无所属执行现场' : '暂无资源回执' }}</dd>
        </div>
      </dl>
      <dl v-if="snapshot">
        <div>
          <dt>{{ observation.historical ? '历史观测时间' : '观测时间' }}</dt>
          <dd>{{ snapshot.observed_at }}</dd>
        </div>
        <div>
          <dt>观测时项目状态</dt>
          <dd>{{ resolveProjectStatus(snapshot.project_status).label }}</dd>
        </div>
        <div>
          <dt>运行观测</dt>
          <dd>{{ snapshot.state }}</dd>
        </div>
        <div>
          <dt>诊断</dt>
          <dd class="project-observation-message">{{ snapshot.message }}</dd>
        </div>
        <div>
          <dt>诊断码</dt>
          <dd>{{ snapshot.code }}</dd>
        </div>
        <template v-if="snapshot.schema_version === 2">
          <div v-for="axis in axes" :key="axis.key">
            <dt>{{ axis.label }}</dt>
            <dd>
              {{ snapshot.axes[axis.key].state
              }}<template v-if="snapshot.axes[axis.key].code">
                · {{ snapshot.axes[axis.key].code }}</template
              >
            </dd>
          </div>
          <div>
            <dt>进程退出确认</dt>
            <dd>{{ snapshot.axes.process.termination_confirmed ? '已确认' : '未确认' }}</dd>
          </div>
          <div>
            <dt>资源清理确认</dt>
            <dd>{{ snapshot.axes.resources.cleanup_confirmed ? '已确认' : '未确认' }}</dd>
          </div>
        </template>
      </dl>
    </section>
  </Popover>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { ChevronDown } from '@lucide/vue'
import { Badge, Button, Popover } from '@/components/ui'
import type { ProjectExecutionControl, ProjectExecutionResource, ProjectRuntime } from '@/api/types'
import { resolveProjectStatus, resolveRuntimeObservation } from './workspaceStatus'

const props = defineProps<{
  project: { id: string; status: string; execution_control?: ProjectExecutionControl; coverage_stop_reason?: string | null; verification_request_id?: string | null }
  runtime?: ProjectRuntime
  dataUpdatedAt: number
  failed: boolean
  fetching: boolean
  now: number
  controlWaitingExpired?: boolean
}>()
defineEmits<{ refresh: [] }>()
const open = ref(false)
watch(
  () => props.project.id,
  () => {
    open.value = false
  },
)
const observation = computed(() => {
  const view = resolveRuntimeObservation(
    props.project,
    props.runtime,
    { failed: props.failed, fetching: props.fetching, dataUpdatedAt: props.dataUpdatedAt },
    Math.max(props.now, Date.now()),
  )
  if (props.controlWaitingExpired) {
    const notice = props.project.execution_control?.state === 'requested'
      ? `物理状态尚未确认，请刷新观测。${view.notice}`
      : `动作资格观测未更新，请刷新观测。${view.control?.notice || ''}`
    return { ...view, notice }
  }
  return view
})
const control = computed(() => observation.value.control)
const status = computed(() => control.value || resolveProjectStatus(props.project.status))
const snapshot = computed(() => observation.value.snapshot)
const resourceFields = [
  { key: 'kind', label: '类型' },
  { key: 'state', label: '状态' },
  { key: 'container_id', label: 'container ID' },
  { key: 'container_name', label: '容器名称' },
  { key: 'task_run_id', label: 'TaskRun' },
  { key: 'exec_id', label: 'exec ID' },
  { key: 'scope_id', label: 'scope ID' },
] as const
const describeResource = (resource: ProjectExecutionResource) =>
  resourceFields
    .filter((field) => resource[field.key])
    .map((field) => `${field.label}：${resource[field.key]}`)
    .join(' · ') || '资源身份未提供'
const axes = [
  { key: 'execution', label: '执行' },
  { key: 'delivery', label: '交付' },
  { key: 'acceptance', label: '业务接受' },
  { key: 'process', label: '进程' },
  { key: 'resources', label: '资源' },
] as const
</script>

<style scoped>
.project-observation-trigger {
  display: grid;
  justify-items: start;
  gap: var(--space-1);
  border: 0;
  border-radius: var(--radius-sm);
  padding: var(--space-1) 0;
  background: transparent;
  color: var(--text-muted);
  font: inherit;
  text-align: left;
  cursor: pointer;
}
.project-observation-trigger:hover {
  color: var(--text);
}
.project-observation-trigger:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 3px;
}
.project-observation-status,
.project-observation-details header {
  display: flex;
  align-items: center;
  gap: var(--space-2);
}
.project-observation-details header {
  justify-content: space-between;
}
.project-observation-notice {
  color: var(--warning);
  font-size: var(--text-xs);
  overflow-wrap: anywhere;
}
.project-observation-notice[data-tone='danger'] {
  color: var(--danger);
}
.project-observation-details dl {
  display: grid;
  gap: var(--space-3);
  margin: var(--space-3) 0 0;
}
.project-observation-details dl > div {
  display: grid;
  grid-template-columns: 110px minmax(0, 1fr);
  gap: var(--space-3);
}
.project-observation-details dt {
  color: var(--text-muted);
}
.project-observation-details dd {
  margin: 0;
  overflow-wrap: anywhere;
}
.project-observation-message {
  white-space: pre-wrap;
}
</style>
