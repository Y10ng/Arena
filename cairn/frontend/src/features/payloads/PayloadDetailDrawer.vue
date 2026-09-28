<template>
  <Drawer
    :open="open"
    title="Payload 详情"
    description="以不可信纯文本审阅实际注入内容与目标直接响应。"
    side="right"
    size="lg"
    close-label="关闭 Payload 详情"
    @update:open="handleOpenChange"
  >
    <template #title>
      <div v-if="currentRecord" class="payload-drawer__title">
        <Badge :tone="statusMeta.tone" size="sm">{{ statusMeta.label }}</Badge>
        <span>{{ currentRecord.injection_type }}</span>
      </div>
      <span v-else>Payload 详情</span>
    </template>

    <ErrorState
      v-if="isError && !currentRecord"
      title="Payload 详情加载失败"
      :description="errorMessage"
      @retry="refetch()"
    />
    <div v-else-if="!currentRecord" class="payload-drawer__loading" role="status">
      正在加载 Payload 详情…
    </div>
    <div v-else class="payload-drawer__body">
      <ErrorState
        v-if="isError"
        title="Payload 详情刷新失败"
        :description="errorMessage"
        @retry="refetch()"
      />

      <section class="payload-drawer__section">
        <h3>记录信息</h3>
        <dl class="payload-drawer__meta">
          <div>
            <dt>记录 ID</dt>
            <dd class="mono">{{ currentRecord.id }}</dd>
          </div>
          <div>
            <dt>Capture ID</dt>
            <dd class="mono">{{ currentRecord.capture_id }}</dd>
          </div>
          <div>
            <dt>阶段</dt>
            <dd>{{ currentRecord.phase }}</dd>
          </div>
          <div>
            <dt>TaskRun</dt>
            <dd>
              <Button variant="link" size="sm" @click="goToRun(currentRecord.task_run_id)">
                {{ currentRecord.task_run_id }} →
              </Button>
            </dd>
          </div>
          <div>
            <dt>记录时间</dt>
            <dd>{{ currentRecord.created_at }}</dd>
          </div>
          <div>
            <dt>更新时间</dt>
            <dd>{{ currentRecord.updated_at }}</dd>
          </div>
        </dl>
      </section>

      <section class="payload-drawer__section">
        <div class="payload-drawer__section-head">
          <div>
            <h3>注入 Payload</h3>
            <p>{{ currentRecord.original_format }}</p>
          </div>
          <Button
            variant="secondary"
            size="sm"
            @click="copyText(currentRecord.payload_text, 'Payload 已复制')"
          >
            复制 Payload
          </Button>
        </div>
        <MonacoEditor
          :model-value="currentRecord.payload_text"
          language="plaintext"
          :read-only="true"
          word-wrap="on"
          height="280px"
          aria-label="注入 Payload 明文"
        />
      </section>

      <section class="payload-drawer__section">
        <div class="payload-drawer__section-head">
          <div>
            <h3>目标直接响应</h3>
            <p>{{ responseDescription }}</p>
          </div>
          <Button v-if="hasResponseText" variant="secondary" size="sm" @click="copyResponse">
            复制响应
          </Button>
        </div>
        <MonacoEditor
          v-if="hasResponseText"
          :model-value="currentRecord.response_text || ''"
          language="plaintext"
          :read-only="true"
          word-wrap="on"
          height="260px"
          aria-label="目标直接响应明文"
        />
        <EmptyState v-else :title="responseEmptyTitle" :description="responseEmptyDescription" />
      </section>
    </div>
  </Drawer>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { useRouter } from 'vue-router'
import { useQuery } from '@tanstack/vue-query'
import { getProjectInjectionPayload } from '@/api/client'
import type { InjectionPayloadRecord } from '@/api/types'
import MonacoEditor from '@/components/MonacoEditor.vue'
import { ErrorState } from '@/components/patterns'
import { Badge, Button, Drawer, EmptyState, type BadgeTone } from '@/components/ui'
import { useUIStore } from '@/stores/ui'
import { injectionPayloadKeys } from './payloadQueries'

const props = defineProps<{
  projectId: string
  payloadId: string | null
  visitGeneration: number
  open: boolean
}>()

const emit = defineEmits<{ close: [] }>()
const router = useRouter()
const uiStore = useUIStore()

const { data, error, isError, refetch } = useQuery<{
  projectId: string
  payloadId: string
  visitGeneration: number
  record: InjectionPayloadRecord
}>({
  queryKey: computed(() =>
    injectionPayloadKeys.detail(props.projectId, props.payloadId, props.visitGeneration),
  ),
  queryFn: async ({ queryKey, signal }) => {
    const [, , requestProjectId, , requestPayloadId, requestVisitGeneration] =
      queryKey as ReturnType<typeof injectionPayloadKeys.detail>
    if (!requestPayloadId) throw new Error('缺少 Payload ID')
    const record = await getProjectInjectionPayload(requestProjectId, requestPayloadId, signal)
    return {
      projectId: requestProjectId,
      payloadId: requestPayloadId,
      visitGeneration: requestVisitGeneration,
      record,
    }
  },
  enabled: computed(() => props.open && Boolean(props.projectId && props.payloadId)),
})

const currentRecord = computed(() => {
  const snapshot = data.value
  if (
    !snapshot ||
    snapshot.projectId !== props.projectId ||
    snapshot.payloadId !== props.payloadId ||
    snapshot.visitGeneration !== props.visitGeneration ||
    snapshot.record.project_id !== props.projectId ||
    snapshot.record.id !== props.payloadId
  ) {
    return null
  }
  return snapshot.record
})

const statusMetadata: Record<string, { label: string; tone: BadgeTone }> = {
  pending: { label: '等待响应', tone: 'info' },
  returned: { label: '已返回', tone: 'success' },
  empty: { label: '空响应', tone: 'neutral' },
  blocked: { label: '被阻止', tone: 'warning' },
  timeout: { label: '超时', tone: 'warning' },
  error: { label: '错误', tone: 'danger' },
  not_sent: { label: '未发送', tone: 'neutral' },
  interrupted: { label: '已中断', tone: 'warning' },
}

const statusMeta = computed(() =>
  currentRecord.value
    ? statusMetadata[currentRecord.value.response_status] || {
        label: currentRecord.value.response_status,
        tone: 'neutral' as BadgeTone,
      }
    : { label: '加载中', tone: 'neutral' as BadgeTone },
)
const errorMessage = computed(() =>
  error.value instanceof Error && error.value.message ? error.value.message : '请稍后重试。',
)
const responseDescription = computed(
  () => currentRecord.value?.response_format || statusMeta.value.label,
)
const hasResponseText = computed(() => (currentRecord.value?.response_text?.length || 0) > 0)
const responseEmptyTitle = computed(() =>
  currentRecord.value?.response_status === 'pending' ? '目标响应尚未完成' : '未保存响应正文',
)
const responseEmptyDescription = computed(() => {
  const status = currentRecord.value?.response_status
  if (status === 'pending') return '记录已开始，等待当前 TaskRun 补全目标直接响应。'
  if (status === 'empty') return '目标明确返回了空响应。'
  if (status === 'not_sent') return '该测试因授权或安全边界未实际发送。'
  return `当前记录状态：${statusMeta.value.label}。`
})

function handleOpenChange(value: boolean) {
  if (!value) emit('close')
}

function goToRun(taskRunId: string) {
  void router.push({
    name: 'WorkspaceRuns',
    params: { id: props.projectId },
    query: { runId: taskRunId },
  })
}

function copyResponse() {
  const responseText = currentRecord.value?.response_text
  if (responseText) void copyText(responseText, '目标响应已复制')
}

async function copyText(text: string, successMessage: string) {
  const projectId = props.projectId
  const payloadId = props.payloadId
  try {
    await navigator.clipboard.writeText(text)
    if (projectId === props.projectId && payloadId === props.payloadId) {
      uiStore.showToast(successMessage, 'success')
    }
  } catch {
    if (projectId === props.projectId && payloadId === props.payloadId) {
      uiStore.showToast('复制失败，请手动选择文本复制', 'danger')
    }
  }
}
</script>

<style scoped>
.payload-drawer__title,
.payload-drawer__section-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-3);
}

.payload-drawer__title {
  justify-content: flex-start;
}

.payload-drawer__body {
  display: grid;
  gap: var(--space-5);
}

.payload-drawer__loading {
  padding: var(--space-5);
  color: var(--text-faint);
}

.payload-drawer__section {
  display: grid;
  gap: var(--space-3);
}

.payload-drawer__section h3,
.payload-drawer__section p {
  margin: 0;
}

.payload-drawer__section p {
  margin-top: var(--space-1);
  color: var(--text-faint);
  font-size: var(--font-size-sm);
}

.payload-drawer__meta {
  display: grid;
  gap: var(--space-2);
  margin: 0;
}

.payload-drawer__meta > div {
  display: grid;
  grid-template-columns: minmax(7rem, 0.3fr) minmax(0, 1fr);
  gap: var(--space-3);
  align-items: center;
}

.payload-drawer__meta dt {
  color: var(--text-faint);
}

.payload-drawer__meta dd {
  min-width: 0;
  margin: 0;
  overflow-wrap: anywhere;
}

@media (max-width: 640px) {
  .payload-drawer__section-head {
    align-items: flex-start;
    flex-direction: column;
  }

  .payload-drawer__meta > div {
    grid-template-columns: 1fr;
    gap: var(--space-1);
  }
}
</style>
