<template>
  <section class="verification-source-preview" aria-label="所选漏洞">
    <header class="verification-heading"><div><p class="verification-source-label">来源漏洞</p><h3>{{ preview.vulnerability.title }}</h3><VulnerabilityNumber :number="preview.number" /></div><RouterLink :to="{ name: 'Findings', query: { vulnerabilityProjectId: preview.project_id, vulnerabilityId: preview.vulnerability_id, vulnerabilityObservationId: preview.evidence.observation_id || undefined } }" target="_blank">查看来源</RouterLink></header>
    <dl class="verification-source-facts">
      <div><dt>所属项目</dt><dd>{{ preview.project_title }}</dd></div>
      <div><dt>严重程度 / 状态</dt><dd>{{ vulnerabilitySeverityMeta(preview.vulnerability.severity).label }} · {{ vulnerabilityStatusMeta(preview.vulnerability.status).label }}</dd></div>
      <div v-if="preview.vulnerability.target"><dt>原目标</dt><dd class="verification-text">{{ preview.vulnerability.target }}</dd></div>
    </dl>
    <SafeReportMarkdown v-if="preview.readable?.description_markdown" :content="preview.readable.description_markdown" />
    <section aria-label="成立条件"><h4>成立条件</h4><ul v-if="preview.readable?.preconditions?.length"><li v-for="condition in preview.readable.preconditions" :key="condition">{{ condition }}</li></ul><p v-else>未记录独立成立条件，不代表无权限或访问前提。</p></section>
    <p v-if="preview.material_validation === 'on_acceptance'" role="status">已读取正文和材料索引；原件可用性与输入边界将在受理时核验。</p>
    <Alert v-if="preview.missing_materials.length" tone="warning" title="相关材料说明"><p>以下问题只影响对应材料，不是统一受理门槛。</p><ul><li v-for="(missing, index) in preview.missing_materials" :key="index">{{ missing.message }}</li></ul></Alert>
    <div :key="entityKey" class="verification-stack">
      <details class="verification-source-details" aria-label="复现步骤">
        <summary>复现步骤与证据<span v-if="steps.length"> · {{ steps.length }} 步</span></summary>
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
        <section v-if="preview.evidence.supplemental_evidence_items?.length" class="verification-other-materials" aria-label="漏洞记录补充">
          <h5>漏洞记录补充</h5><p>历史上下文，不是所选发现步骤或本次复测的新证据。</p>
          <EvidenceItemDisclosure v-for="item in preview.evidence.supplemental_evidence_items" :key="item.id" :item="item" :entity-key="`${entityKey}:supplement`" :project-id="preview.project_id" :load-preview="loadPreview" :original-url="originalUrl" :artifact-size="artifactSize" />
        </section>
        <section class="verification-source-remediation" aria-label="修复建议">
          <h4>修复建议</h4>
          <SafeReportMarkdown v-if="preview.readable?.remediation_markdown" :content="preview.readable.remediation_markdown" />
          <p v-else class="verification-muted">未记录修复建议。</p>
        </section>
      </details>
    </div>
  </section>
</template>
<script setup lang="ts">
import VulnerabilityNumber from '@/features/vulnerabilities/VulnerabilityNumber.vue'
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

<style scoped>
.verification-source-preview { display: flex; flex-direction: column; gap: var(--space-3); min-width: 0; }
.verification-heading { display: flex; flex-wrap: wrap; align-items: baseline; gap: var(--space-3); }
.verification-heading > div { flex: 1; min-width: 0; }
.verification-heading h3 { margin: var(--space-1) 0 0; font-size: var(--text-object); font-weight: var(--font-semibold); overflow-wrap: anywhere; }
.verification-source-facts { display: grid; gap: var(--space-2); margin: 0; font-size: var(--text-body); }
.verification-source-facts > div { display: grid; grid-template-columns: 100px minmax(0, 1fr); gap: var(--space-3); }
.verification-source-facts dt { color: var(--text-muted); font-size: var(--text-sm); }
.verification-source-facts dd { margin: 0; overflow-wrap: anywhere; }
.verification-source-remediation { margin-block: var(--space-4); }
.verification-source-remediation h4 { margin: 0; font-size: var(--text-md); font-weight: var(--font-medium); }
.verification-heading a { color: var(--link); font-size: var(--text-sm); }
.verification-source-preview p { margin: 0; color: var(--text-muted); font-size: var(--text-sm); overflow-wrap: anywhere; }
.verification-source-preview .verification-text { white-space: pre-wrap; font-family: var(--font-mono); }
.verification-stack { display: grid; gap: var(--space-2); min-width: 0; }
.verification-source-details { border-top: 1px solid var(--border-subtle); min-width: 0; }
.verification-source-details summary { min-height: 40px; padding-block: var(--space-2); cursor: pointer; font-weight: var(--font-medium); }
.verification-source-details summary:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
.verification-source-details summary > span { color: var(--text-muted); font-size: var(--text-sm); font-weight: normal; }
.verification-test-steps { display: grid; gap: var(--space-4); margin: 0; padding: var(--space-3) 0; list-style: none; }
.verification-test-step { padding: 0; min-width: 0; }
.verification-test-step h5, .verification-other-materials h5 { margin: 0 0 var(--space-2); font-size: var(--text-md); }
.verification-source-details :deep(.safe-report-markdown) { margin-block: var(--space-3); font-size: var(--text-reading); line-height: var(--leading-reading); }
.verification-source-details :deep(.safe-report-markdown h1) { font-size: var(--text-object); }
.verification-source-details :deep(.safe-report-markdown h2) { font-size: var(--text-section); }
.verification-step-evidence { display: grid; gap: var(--space-2); min-width: 0; margin-top: var(--space-3); }
.verification-other-materials { margin-top: var(--space-4); }
</style>
