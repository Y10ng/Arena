<template>
  <article class="console-page system-update-console">
    <header class="console-resource-heading">
      <h1>系统更新</h1>
      <div class="header-actions">
        <Button
          v-if="status && canCheck(status.state.phase)"
          size="sm"
          :loading="checking"
          :disabled="isBusy"
          @click="check"
        >
          检查更新
        </Button>
      </div>
    </header>

    <Alert v-if="statusQuery.isError.value && !status" tone="danger" title="状态加载失败">
      {{ errorMessage(statusQuery.error.value) }}
      <Button variant="secondary" size="sm" @click="statusQuery.refetch()">重试</Button>
    </Alert>

    <Alert v-else-if="statusQuery.isPending.value" tone="info">
      <Spinner size="sm" decorative />正在读取更新状态…
    </Alert>

    <template v-else-if="status">
      <Alert v-if="status.install_type === 'git'" tone="info" title="源码安装">
        当前为 git 源码安装，一键更新不可用。请使用 git pull 与 deploy/update.sh 手动更新。
      </Alert>

      <Alert v-else-if="status.install_type === 'unsupported'" tone="warning" title="安装类型未知">
        无法识别安装方式，请使用 deploy/update.sh 手动更新。
      </Alert>

      <div class="update-grid">
        <section class="console-card version-card">
          <h2>版本信息</h2>
          <dl class="version-matrix">
            <div>
              <dt>当前版本</dt>
              <dd>{{ status.version }}</dd>
            </div>
            <div>
              <dt>最新版本</dt>
              <dd>{{ status.state.channel_version || '—' }}</dd>
            </div>
            <div>
              <dt>更新状态</dt>
              <dd>
                <span :class="['phase-badge', `phase-${status.state.phase}`]">
                  {{ phaseLabel(status.state.phase) }}
                </span>
              </dd>
            </div>
            <div v-if="status.state.channel?.notes_url">
              <dt>发布说明</dt>
              <dd>
                <a :href="status.state.channel.notes_url" target="_blank" rel="noopener">查看</a>
              </dd>
            </div>
          </dl>
        </section>

        <section class="console-card gateway-card">
          <h2>网关镜像</h2>
          <p class="text-faint">{{ status.gateway_image || '未配置' }}</p>
          <p v-if="status.state.previous_gateway_ref" class="text-faint">
            上次更新前：{{ status.state.previous_gateway_ref }}
          </p>
        </section>
      </div>

      <Alert v-if="status.state.error" tone="danger" title="更新失败">
        {{ status.state.error }}
        <span v-if="status.state.apply_log_path" class="text-faint">
          日志：{{ status.state.apply_log_path }}
        </span>
      </Alert>

      <Alert v-if="status.state.phase === 'interrupted'" tone="warning" title="更新已中断">
        服务重启导致更新中断。请重新执行检查更新。
      </Alert>

      <Alert v-if="status.state.phase === 'ready'" tone="success" title="准备就绪">
        新版本已下载并校验完成，可以开始更新。
        <span
          v-if="status.running_task_count > 0 || status.queued_task_count > 0"
          class="text-danger"
        >
          当前有 {{ status.running_task_count }} 个运行中任务、{{
            status.queued_task_count
          }}
          个排队任务，更新将停止这些任务。
        </span>
      </Alert>

      <div class="update-actions">
        <Button
          v-if="canPrepare(status.state.phase)"
          :loading="preparing"
          :disabled="isBusy"
          @click="prepare"
        >
          下载并准备更新
        </Button>

        <Button
          v-if="canApply(status.state.phase)"
          variant="danger"
          :loading="applying"
          :disabled="isBusy"
          @click="applyUpdate"
        >
          立即更新并重启
        </Button>

        <Button
          v-if="canCancel(status.state.phase)"
          variant="secondary"
          :disabled="isBusy"
          @click="cancel"
        >
          取消准备
        </Button>
      </div>

      <div v-if="showProgress(status.state.phase)" class="update-progress">
        <Spinner size="sm" decorative />
        <span>{{ status.state.prepare_progress || phaseLabel(status.state.phase) }}</span>
      </div>

      <p v-if="!status.channel_configured" class="text-faint">
        更新渠道未配置。请设置 CAIRN_UPDATE_CHANNEL_URL 环境变量指向 release.json。
      </p>
    </template>
  </article>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useQuery } from '@tanstack/vue-query'
import { Button, Alert, Spinner } from '@/components/ui'
import {
  getSystemUpdateStatus,
  checkSystemUpdate,
  prepareSystemUpdate,
  cancelSystemUpdate,
  applySystemUpdate,
} from '@/api/systemUpdate'
import type { UpdateStatusResponse } from '@/api/systemUpdate'
import {
  phaseLabel,
  canCheck,
  canPrepare,
  canCancel,
  canApply,
  showProgress,
  pollInterval,
} from './systemUpdateModel'
import './console.css'

const statusQuery = useQuery<UpdateStatusResponse>({
  queryKey: ['systemUpdateStatus'],
  queryFn: () => getSystemUpdateStatus(),
  refetchInterval: (query) => {
    const phase = query.state.data?.state.phase
    return phase ? (pollInterval(phase) ?? false) : false
  },
})

const status = computed(() => statusQuery.data.value)
const checking = ref(false)
const preparing = ref(false)
const applying = ref(false)
const actionError = ref('')

const isBusy = computed(() => checking.value || preparing.value || applying.value)

const errorMessage = (error: unknown) =>
  error instanceof Error && error.message ? error.message : '未知错误'

const check = async () => {
  checking.value = true
  actionError.value = ''
  try {
    await checkSystemUpdate()
    await statusQuery.refetch()
  } catch (error) {
    actionError.value = errorMessage(error)
  } finally {
    checking.value = false
  }
}

const prepare = async () => {
  preparing.value = true
  actionError.value = ''
  try {
    await prepareSystemUpdate()
    await statusQuery.refetch()
  } catch (error) {
    actionError.value = errorMessage(error)
  } finally {
    preparing.value = false
  }
}

const cancel = async () => {
  actionError.value = ''
  try {
    await cancelSystemUpdate()
    await statusQuery.refetch()
  } catch (error) {
    actionError.value = errorMessage(error)
  }
}

const applyUpdate = async () => {
  if (!confirm('确定要立即更新并重启服务吗？运行中的任务将被停止。')) return
  applying.value = true
  actionError.value = ''
  try {
    await applySystemUpdate()
    await statusQuery.refetch()
  } catch (error) {
    actionError.value = errorMessage(error)
  } finally {
    applying.value = false
  }
}

onMounted(() => {
  void statusQuery.refetch()
})

onUnmounted(() => {
  // 轮询由 vue-query 自动清理
})
</script>

<style scoped>
.system-update-console {
  display: flex;
  flex-direction: column;
  gap: var(--space-4);
}

.update-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
  gap: var(--space-4);
}

.version-card,
.gateway-card {
  padding: var(--space-4);
}

.version-card h2,
.gateway-card h2 {
  margin: 0 0 var(--space-3);
  font-size: var(--text-lg);
  font-weight: var(--font-semibold);
}

.version-matrix {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  margin: 0;
}

.version-matrix > div {
  display: flex;
  justify-content: space-between;
  align-items: center;
}

.version-matrix dt {
  color: var(--text-muted);
  font-size: var(--text-sm);
}

.version-matrix dd {
  margin: 0;
  font-weight: var(--font-medium);
}

.phase-badge {
  padding: 2px var(--space-2);
  border-radius: var(--radius-sm);
  font-size: var(--text-xs);
  font-weight: var(--font-medium);
}

.phase-available {
  background: var(--success-soft);
  color: var(--success);
}

.phase-up_to_date {
  background: var(--bg-inset);
  color: var(--text-muted);
}

.phase-preparing,
.phase-applying,
.phase-checking {
  background: var(--info-soft);
  color: var(--info);
}

.phase-ready {
  background: var(--success-soft);
  color: var(--success);
}

.phase-done {
  background: var(--success-soft);
  color: var(--success);
}

.phase-failed,
.phase-interrupted {
  background: var(--danger-soft);
  color: var(--danger);
}

.update-actions {
  display: flex;
  gap: var(--space-2);
  flex-wrap: wrap;
}

.update-progress {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  color: var(--text-muted);
}
</style>
