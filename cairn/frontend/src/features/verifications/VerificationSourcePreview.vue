<template>
  <section class="verification-source-preview" aria-label="所选漏洞原文与历史证据">
    <header class="verification-heading"><h3>{{ preview.vulnerability.title }}</h3><RouterLink :to="{ name: 'Findings', query: { vulnerabilityProjectId: preview.project_id, vulnerabilityId: preview.vulnerability_id } }" target="_blank">查看来源漏洞</RouterLink></header>
    <p class="verification-muted">{{ preview.project_title }} · {{ preview.vulnerability_id }} · {{ vulnerabilitySeverityMeta(preview.vulnerability.severity).label }} · {{ vulnerabilityStatusMeta(preview.vulnerability.status).label }}</p>
    <p class="verification-text">原目标：{{ preview.vulnerability.target || '未记录' }}</p>
    <p class="verification-muted">以下均为来源原文与历史证据，不是本轮观察，也不自动构成本次授权。受理时由服务端读取并保存独立输入，预览不替代权威原件。</p>
    <Alert v-if="preview.missing_materials.length" tone="warning" title="历史资料存在缺项"><ul><li v-for="(missing, index) in preview.missing_materials" :key="index">{{ missing.message }}<span class="verification-muted"> · {{ missing.artifact_id || missing.source_id }} · {{ missing.code }}</span></li></ul></Alert>
    <div class="verification-source-originals">
      <h4>漏洞原文</h4>
      <SafeReportMarkdown v-if="preview.vulnerability.repro_steps" :content="preview.vulnerability.repro_steps" />
      <details v-if="preview.vulnerability.repro_request"><summary>原始复现请求</summary><pre class="verification-code">{{ preview.vulnerability.repro_request }}</pre></details>
      <details v-if="preview.vulnerability.repro_response"><summary>原始复现响应</summary><pre class="verification-code">{{ preview.vulnerability.repro_response }}</pre></details>
      <details v-if="preview.vulnerability.remediation"><summary>原修复建议</summary><SafeReportMarkdown :content="preview.vulnerability.remediation" /></details>
      <details v-for="fact in preview.facts" :key="fact.id"><summary>相关 Fact · {{ fact.id }}</summary><SafeReportMarkdown :content="fact.description" /><pre class="verification-code">{{ JSON.stringify(fact, null, 2) }}</pre></details>
      <details v-for="observation in preview.observations" :key="observation.id"><summary>历史发现原文 · {{ observation.id }}</summary><pre class="verification-code">{{ JSON.stringify(observation, null, 2) }}</pre></details>
      <details v-for="verification in preview.verifications" :key="verification.id"><summary>历史复核 · {{ outcomeLabels[verification.outcome] }} · {{ verification.id }}</summary><SafeReportMarkdown v-if="verification.notes" :content="verification.notes" /><pre class="verification-code">{{ JSON.stringify(verification, null, 2) }}</pre></details>
      <details v-if="preview.fact_evidence.length"><summary>历史 Fact 证据引用原文</summary><pre class="verification-code">{{ JSON.stringify(preview.fact_evidence, null, 2) }}</pre></details>
      <details><summary>完整漏洞原始字段</summary><pre class="verification-code">{{ JSON.stringify(preview.vulnerability, null, 2) }}</pre></details>
    </div>
    <h4>将携带的直接关联历史证据</h4>
    <div v-for="artifact in preview.artifacts" :key="artifact.artifact_id" class="verification-file-row"><span>{{ artifact.filename || artifact.artifact_id }}<span class="verification-muted"><template v-if="artifact.size_bytes !== undefined"> · {{ formatProjectResourceSize(artifact.size_bytes) }}</template> ·{{ artifact.available ? '可读取' : '不可读取，存在缺项' }}</span></span><a v-if="artifact.available" :href="`/projects/${encodeURIComponent(preview.project_id)}/artifacts/${encodeURIComponent(artifact.artifact_id)}/content`" target="_blank" rel="noopener" download>下载历史原件</a><details><summary>来源与完整性</summary><pre class="verification-code">{{ JSON.stringify(artifact, null, 2) }}</pre></details></div>
    <p v-if="!preview.artifacts.length" class="verification-muted">未找到直接关联的历史文件证据；原文仍保留，是否足够以本轮实际验证为准。</p>
  </section>
</template>
<script setup lang="ts">
import type { VerificationSourcePreview } from '@/api/verificationRequests'
import { Alert } from '@/components/ui'
import SafeReportMarkdown from '@/features/reports/SafeReportMarkdown.vue'
import { formatProjectResourceSize } from '@/features/projects/projectResources'
import { vulnerabilitySeverityMeta, vulnerabilityStatusMeta } from '@/features/vulnerabilities/vulnerabilityMetadata'
import { outcomeLabels } from './verificationPresentation'
defineProps<{ preview: VerificationSourcePreview }>()
</script>
