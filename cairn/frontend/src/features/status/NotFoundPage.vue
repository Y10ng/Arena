<template>
  <section class="status-page" aria-labelledby="not-found-title">
    <div class="status-content">
      <div class="status-heading">
        <FileQuestionMark :size="22" aria-hidden="true" /><span>404</span>
      </div>
      <h2 id="not-found-title">页面不存在</h2>
      <p>该地址不存在、已移动或不再可用。</p>
      <div class="status-actions">
        <Button @click="returnToOverview"><ArrowLeft :size="16" />返回工作台</Button>
      </div>
    </div>
  </section>
</template>

<script setup lang="ts">
import { ArrowLeft, FileQuestionMark } from '@lucide/vue'
import { useRouter } from 'vue-router'
import { Button } from '@/components/ui'

const router = useRouter()

function returnToOverview() {
  router.replace({ name: 'Overview' })
}
</script>

<style scoped>
.status-page {
  min-height: 100%;
  display: grid;
  align-items: center;
  padding: var(--space-8) var(--space-7);
  background: var(--color-canvas);
}
.status-content {
  width: min(100%, 560px);
  margin: 0 auto;
}
.status-heading {
  display: flex;
  align-items: center;
  gap: var(--space-3);
  color: var(--text-muted);
  font-size: var(--text-md);
  font-variant-numeric: tabular-nums;
}
h2 {
  margin: var(--space-5) 0 var(--space-3);
  color: var(--text);
  font-size: var(--text-object);
  line-height: 1.4;
  font-weight: var(--font-semibold);
}
p {
  margin: 0;
  color: var(--text-muted);
  font-size: var(--text-md);
  line-height: 1.8;
}
.status-actions {
  display: flex;
  margin-top: var(--space-6);
  padding-top: var(--space-5);
  border-top: 1px solid var(--border);
}
@media (max-width: 520px) {
  .status-page {
    padding-inline: var(--space-5);
  }
}
</style>
