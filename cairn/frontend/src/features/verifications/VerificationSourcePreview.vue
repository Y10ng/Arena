<template>
  <section class="verification-source-preview" aria-label="所选漏洞">
    <header class="verification-heading"><h3>{{ preview.vulnerability.title }}</h3><RouterLink :to="{ name: 'Findings', query: { vulnerabilityProjectId: preview.project_id, vulnerabilityId: preview.vulnerability_id, vulnerabilityObservationId: preview.evidence.observation_id || undefined } }" target="_blank">来源漏洞</RouterLink></header>
    <p>{{ preview.project_title }} · {{ vulnerabilitySeverityMeta(preview.vulnerability.severity).label }} · {{ vulnerabilityStatusMeta(preview.vulnerability.status).label }}</p>
    <p v-if="preview.vulnerability.target" class="verification-text">{{ preview.vulnerability.target }}</p>
    <Alert v-if="preview.missing_materials.length" tone="warning" title="资料缺项"><ul><li v-for="(missing, index) in preview.missing_materials" :key="index">{{ missing.message }}</li></ul></Alert>
    <div :key="entityKey" class="verification-stack">
      <section class="verification-source-details" aria-label="复现步骤">
        <h4>复现步骤</h4>
        <ol v-if="steps.length" class="verification-test-steps">
          <li v-for="(step, index) in steps" :key="index" class="verification-test-step">
            <h5>步骤 {{ index + 1 }}</h5>
            <SafeReportMarkdown :content="step.action" />
            <SafeReportMarkdown v-if="step.observation" :content="step.observation" />
            <div class="verification-step-evidence">
              <template v-for="(entry, entryIndex) in step.evidence" :key="`${entry.id}:${entryIndex}`">
                <EvidenceItemDisclosure v-if="entry.item" :item="entry.item" :entity-key="`${entityKey}:step:${index}`" :project-id="preview.project_id" :source="preview.evidence.source" :load-preview="loadPreview" :original-url="originalUrl" :image-url="imageUrl" :artifact-size="artifactSize" />
                <p v-else class="verification-muted">{{ entry.id }}（未找到引用）</p>
              </template>
            </div>
          </li>
        </ol>
        <SafeReportMarkdown v-else-if="preview.vulnerability.repro_steps" :content="preview.vulnerability.repro_steps" />
        <p v-else class="verification-muted">未记录复现步骤。</p>
        <section v-if="unboundItems.length" class="verification-other-materials" aria-label="其他材料">
          <h5>其他材料 · {{ unboundItems.length }}</h5>
          <p class="verification-muted">以下材料未绑定结构化步骤。</p>
          <div class="verification-step-evidence">
            <EvidenceItemDisclosure v-for="item in unboundItems" :key="item.id" :item="item" :entity-key="`${entityKey}:unlinked`" :project-id="preview.project_id" :source="preview.evidence.source" :load-preview="loadPreview" :original-url="originalUrl" :image-url="imageUrl" :artifact-size="artifactSize" />
          </div>
        </section>
      </section>
      <section class="verification-source-details" aria-label="修复建议">
        <h4>修复建议</h4>
        <SafeReportMarkdown v-if="preview.vulnerability.remediation" :content="preview.vulnerability.remediation" />
        <p v-else class="verification-muted">未记录修复建议。</p>
      </section>
    </div>
  </section>
</template>
<script setup lang="ts">
import { computed } from 'vue'
import type { VerificationSourcePreview } from '@/api/verificationRequests'
import { fetchArtifactPreview } from '@/api/client'
import type { EvidenceRef } from '@/api/types'
import { Alert } from '@/components/ui'
import EvidenceItemDisclosure from '@/components/patterns/EvidenceItemDisclosure.vue'
import SafeReportMarkdown from '@/features/reports/SafeReportMarkdown.vue'
import { vulnerabilitySeverityMeta, vulnerabilityStatusMeta } from '@/features/vulnerabilities/vulnerabilityMetadata'
const props = defineProps<{ preview: VerificationSourcePreview }>()
const entityKey = computed(() => `${props.preview.project_id}:${props.preview.vulnerability_id}:${props.preview.evidence.observation_id || 'record'}`)
const steps = computed(() => props.preview.evidence.reproduction_steps.map((step) => ({
  ...step,
  evidence: step.evidence_item_ids.map((id) => ({ id, item: props.preview.evidence.evidence_items.find((item) => item.id === id) })),
})))
const unboundItems = computed(() => {
  const bound = new Set(steps.value.flatMap((step) => step.evidence_item_ids))
  return props.preview.evidence.evidence_items.filter((item) => !bound.has(item.id))
})
function loadPreview(reference: EvidenceRef, signal: AbortSignal) { return fetchArtifactPreview(props.preview.project_id, reference.artifact_id, signal) }
function originalUrl(reference: EvidenceRef) { return `/projects/${encodeURIComponent(props.preview.project_id)}/artifacts/${encodeURIComponent(reference.artifact_id)}/content` }
function artifactSize(reference: EvidenceRef) { return props.preview.artifacts.find((item) => item.artifact_id === reference.artifact_id)?.size_bytes }
function imageUrl(reference: EvidenceRef) {
  const artifact = props.preview.artifacts.find((item) => item.artifact_id === reference.artifact_id)
  return artifact?.available && artifact.kind === 'screenshot' && /\.(png|jpe?g|webp|gif)$/i.test(artifact.filename || '') ? originalUrl(reference) : null
}
</script>
