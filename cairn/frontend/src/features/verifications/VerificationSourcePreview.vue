<template>
  <section class="verification-source-preview" aria-label="所选漏洞">
    <header class="verification-heading"><h3>{{ preview.vulnerability.title }}</h3><RouterLink :to="{ name: 'Findings', query: { vulnerabilityProjectId: preview.project_id, vulnerabilityId: preview.vulnerability_id } }" target="_blank">来源漏洞</RouterLink></header>
    <p>{{ preview.project_title }} · {{ vulnerabilitySeverityMeta(preview.vulnerability.severity).label }} · {{ vulnerabilityStatusMeta(preview.vulnerability.status).label }}</p>
    <p v-if="preview.vulnerability.target" class="verification-text">{{ preview.vulnerability.target }}</p>
    <Alert v-if="preview.missing_materials.length" tone="warning" title="资料缺项"><ul><li v-for="(missing, index) in preview.missing_materials" :key="index">{{ missing.message }}</li></ul></Alert>
    <details v-if="preview.evidence.evidence_items.length || preview.vulnerability.repro_steps" class="verification-source-details" @toggle="evidenceOpen = ($event.target as HTMLDetailsElement).open"><summary>查看复现步骤与报文</summary><template v-if="evidenceOpen"><SafeReportMarkdown v-if="preview.vulnerability.repro_steps" :content="preview.vulnerability.repro_steps" /><EvidenceReader v-if="preview.evidence.evidence_items.length" presentation="step" :entity-key="`${preview.project_id}:${preview.vulnerability_id}:${preview.evidence.observation_id || 'record'}`" :project-id="preview.project_id" :items="preview.evidence.evidence_items" :load-preview="loadPreview" :original-url="originalUrl" :image-url="imageUrl" /></template></details>
  </section>
</template>
<script setup lang="ts">
import { ref } from 'vue'
import type { VerificationSourcePreview } from '@/api/verificationRequests'
import { fetchArtifactPreview } from '@/api/client'
import type { EvidenceRef } from '@/api/types'
import { Alert } from '@/components/ui'
import EvidenceReader from '@/components/patterns/EvidenceReader.vue'
import SafeReportMarkdown from '@/features/reports/SafeReportMarkdown.vue'
import { vulnerabilitySeverityMeta, vulnerabilityStatusMeta } from '@/features/vulnerabilities/vulnerabilityMetadata'
const props = defineProps<{ preview: VerificationSourcePreview }>()
const evidenceOpen = ref(false)
function loadPreview(reference: EvidenceRef, signal: AbortSignal) { return fetchArtifactPreview(props.preview.project_id, reference.artifact_id, signal) }
function originalUrl(reference: EvidenceRef) { return `/projects/${encodeURIComponent(props.preview.project_id)}/artifacts/${encodeURIComponent(reference.artifact_id)}/content` }
function imageUrl(reference: EvidenceRef) {
  const artifact = props.preview.artifacts.find((item) => item.artifact_id === reference.artifact_id)
  return artifact?.available && artifact.kind === 'screenshot' && /\.(png|jpe?g|webp|gif)$/i.test(artifact.filename || '') ? originalUrl(reference) : null
}
</script>
