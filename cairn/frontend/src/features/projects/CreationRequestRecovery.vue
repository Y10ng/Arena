<template>
  <Alert v-if="cacheWarning" tone="warning" title="请求引用缓存提示">{{ cacheWarning }}</Alert>
  <Alert
    v-for="reference in references"
    :key="reference.request_id"
    tone="warning"
    :title="results[reference.request_id] ? '原请求已保存' : '有保存请求尚未确认'"
  >
    {{ reference.operation === 'schedule' ? '计划' : '项目' }}请求 {{ reference.request_id }}。
    <template v-if="!results[reference.request_id]">
      刷新后不保留正文、文件或原
      payload，只能查询；尚未查到不代表失败，不要按标题猜测或换键重复创建。
    </template>
    <p v-if="messages[reference.request_id]">{{ messages[reference.request_id] }}</p>
    <template #actions>
      <Button
        variant="secondary"
        size="sm"
        :loading="busy === reference.request_id"
        :disabled="Boolean(busy)"
        @click="query(reference)"
        >查询已提交结果</Button
      >
      <Button
        v-if="
          results[reference.request_id]?.schedule_id || results[reference.request_id]?.project_id
        "
        size="sm"
        @click="enter(reference)"
        >进入已保存{{ results[reference.request_id]!.schedule_id ? '计划' : '项目' }}</Button
      >
    </template>
  </Alert>
</template>
<script setup lang="ts">
import { ref } from 'vue'
import { isNavigationFailure, useRouter } from 'vue-router'
import { Alert, Button } from '@/components/ui'
import {
  readCreationRequests,
  getCreationRequest,
  forgetCreationRequest,
  type CreationRequestReference,
  type CreationRequestResult,
} from '@/api/projectSchedules'
const router = useRouter()
const cached = readCreationRequests()
const references = ref(cached.references)
const cacheWarning = ref(cached.warning)
const busy = ref('')
const messages = ref<Record<string, string>>({})
const results = ref<Record<string, CreationRequestResult>>({})
async function query(reference: CreationRequestReference) {
  if (busy.value) return
  busy.value = reference.request_id
  try {
    const result = await getCreationRequest(reference)
    if (result.status === 'committed') {
      results.value[reference.request_id] = result
      messages.value[reference.request_id] =
        result.schedule_id || result.project_id
          ? '服务端已确认保存，请使用关联入口。'
          : '原请求已保存，但项目已删除，不能用此请求重新创建。'
      const cache = forgetCreationRequest(reference.request_id)
      if (cache.warning) cacheWarning.value = cache.warning
    } else
      messages.value[reference.request_id] =
        '尚未查到已提交结果，请稍后再查。跨刷新无法恢复原 payload，不能自动重发。'
  } catch (error) {
    messages.value[reference.request_id] = error instanceof Error ? error.message : '查询失败'
  } finally {
    busy.value = ''
  }
}
async function enter(reference: CreationRequestReference) {
  const result = results.value[reference.request_id]
  if (!result || (!result.schedule_id && !result.project_id)) return
  try {
    const failure = await router.push(
      result.schedule_id
        ? `/project-schedules/${result.schedule_id}`
        : `/projects/${result.project_id}`,
    )
    if (isNavigationFailure(failure))
      messages.value[reference.request_id] = '导航未完成，可再次进入已保存对象。'
  } catch (error) {
    messages.value[reference.request_id] =
      error instanceof Error ? error.message : '导航失败，请重试入口'
  }
}
</script>
