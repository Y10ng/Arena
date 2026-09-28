<template>
  <div class="status-page">
    <Card class="status-card" padding="lg">
      <EmptyState title="无权访问此页面" description="当前账户没有访问此页面所需的权限。">
        <template #icon>
          <ShieldX :size="36" :stroke-width="1.6" />
        </template>
        <div class="status-code" aria-hidden="true">403</div>
        <template #actions>
          <Button @click="returnToOverview">返回仪表盘</Button>
        </template>
      </EmptyState>
    </Card>
  </div>
</template>

<script setup lang="ts">
import { ShieldX } from '@lucide/vue'
import { useRouter } from 'vue-router'
import { Button, Card, EmptyState } from '@/components/ui'

const router = useRouter()

function returnToOverview() {
  router.replace({ name: 'Overview' })
}
</script>

<style scoped>
.status-page {
  min-height: 100%;
  display: grid;
  place-items: center;
  padding: var(--space-6) var(--space-4);
}

.status-card {
  width: min(100%, 680px);
}

.status-code {
  color: var(--text-faint);
  font-size: var(--text-2xl);
  font-weight: var(--font-semibold);
  letter-spacing: 0.08em;
}
</style>
