import type { CoverageCell, CoverageQualitySnapshot, SurfaceManifestEntry } from '@/api/types'
import type { BadgeTone } from '@/components/ui'

export type CoverageTone = 'success' | 'finding' | 'blocked' | 'muted' | 'pending'

export interface CoverageBadgeMeta {
  label: string
  tone: BadgeTone
}

const OUTCOME_LABELS: Record<string, string> = {
  finding: '发现漏洞',
  tested_no_finding: '已测试 · 未发现',
  temporarily_blocked: '暂时受阻',
  permanently_unreachable: '永久不可达',
  out_of_scope: '超出范围',
  not_applicable: '不适用',
}

const QUALITY_STATUS_LABELS: Record<string, string> = {
  ready: '无残余风险',
  observing: '存在残余风险',
  unavailable: '快照不可用',
}

const STOP_REASON_LABELS: Record<string, string> = {
  coverage_complete: '覆盖已完成',
  budget_exhausted: '评估预算已耗尽',
  deadline_exceeded: '评估截止时间已到',
  saturated: '探索已饱和',
}

const RESIDUAL_RISK_LABELS: Record<string, string> = {
  coverage_scope_disposition_missing: '覆盖范围处置尚未完成',
  manifest_surface_without_required_cell: 'Manifest Surface 缺少必测 Cell',
  required_cell_unresolved: '仍有必测 Cell 未解决',
  required_cell_unqualified: '仍有必测 Cell 未达到证据要求',
  required_cell_administratively_skipped: '必测 Cell 被管理性跳过',
  permanent_limitation: '存在永久覆盖限制',
  active_target_blocker: '存在活跃目标阻断',
  failed_assessment_attempt: '存在失败的评估尝试',
  finding_pending_independent_verification: '漏洞仍待独立验证',
  finding_independent_verification_not_reproduced: '独立验证未能复现漏洞',
  finding_independent_verification_inconclusive: '独立验证结论不充分',
  finding_independent_verification_conflicting: '独立验证结论存在冲突',
  'coverage_stopped:budget_exhausted': '覆盖停止：评估预算已耗尽',
  'coverage_stopped:deadline_exceeded': '覆盖停止：评估截止时间已到',
  'coverage_stopped:saturated': '覆盖停止：探索已饱和',
}

const CELL_STATUS_META: Record<string, CoverageBadgeMeta> = {
  pending: { label: '待处理', tone: 'neutral' },
  exploring: { label: '测试中', tone: 'info' },
  explored: { label: '已测试', tone: 'success' },
  verified: { label: '已验证', tone: 'success' },
  skipped: { label: '已跳过', tone: 'warning' },
}

const CELL_RESULT_TONES: Record<string, BadgeTone> = {
  finding: 'danger',
  tested_no_finding: 'success',
  temporarily_blocked: 'warning',
  permanently_unreachable: 'warning',
  out_of_scope: 'neutral',
  not_applicable: 'neutral',
}

export const CHAIN_CANDIDATE_LABEL = '漏洞链候选'
export const CHAIN_VERIFICATION_READY_LABEL = '可进入链级验证'

export const qualityStatusLabel = (status: string) => QUALITY_STATUS_LABELS[status] || status

export const qualityStatusMeta = (status: string): CoverageBadgeMeta => ({
  label: qualityStatusLabel(status),
  tone: status === 'ready' ? 'success' : status === 'observing' ? 'warning' : 'neutral',
})

export const coverageStopReasonLabel = (reason: string) => STOP_REASON_LABELS[reason] || reason

export const residualRiskLabel = (code: string) => RESIDUAL_RISK_LABELS[code] || code

export const coverageStatusMeta = (status: string): CoverageBadgeMeta =>
  CELL_STATUS_META[status] || { label: status || '未知状态', tone: 'neutral' }

export const coverageResultMeta = (result: string): CoverageBadgeMeta => ({
  label: result ? OUTCOME_LABELS[result] || result : '尚无结果',
  tone: result ? CELL_RESULT_TONES[result] || 'neutral' : 'neutral',
})

export const coverageOutcomeLabel = (cell: CoverageCell) => {
  if (cell.result) return coverageResultMeta(cell.result).label
  if (cell.status === 'exploring') return '测试中'
  if (cell.status === 'skipped') return cell.skip_reason || '已跳过'
  return '待测试'
}

export const coverageTone = (cell: CoverageCell): CoverageTone => {
  if (cell.result === 'finding') return 'finding'
  if (cell.result === 'tested_no_finding') return 'success'
  if (cell.result === 'temporarily_blocked' || cell.blocker_fingerprint) return 'blocked'
  if (
    ['permanently_unreachable', 'out_of_scope', 'not_applicable'].includes(cell.result) ||
    cell.status === 'skipped'
  )
    return 'muted'
  return 'pending'
}

export const qualifiedPercent = (snapshot?: CoverageQualitySnapshot | null) => {
  if (!snapshot?.total_count) return 0
  return Math.round((snapshot.qualified_count / snapshot.total_count) * 100)
}

export const coverageRequirementLabel = (cell: CoverageCell) => {
  if (cell.coverage_requirement === 'required') return '必测'
  if (cell.coverage_requirement === 'optional') return '可选'
  if (cell.coverage_requirement) return cell.coverage_requirement
  if (cell.tier === 'core') return '必测'
  if (cell.tier === 'extended') return '可选'
  return cell.tier || '未知要求'
}

export const coverageRequirementMeta = (cell: CoverageCell): CoverageBadgeMeta => ({
  label: coverageRequirementLabel(cell),
  tone:
    cell.coverage_requirement === 'required' || (!cell.coverage_requirement && cell.tier === 'core')
      ? 'warning'
      : 'neutral',
})

export const countDistinctManifestSurfaces = (
  observations: readonly Pick<SurfaceManifestEntry, 'surface_id'>[],
): number =>
  new Set(
    observations
      .map(({ surface_id }) => surface_id.trim())
      .filter((surfaceId) => surfaceId.length > 0),
  ).size

export const isCooldownActive = (cell: CoverageCell, now = new Date()) => {
  if (!cell.blocker_cooldown_until) return false
  const expiresAt = Date.parse(cell.blocker_cooldown_until)
  return Number.isFinite(expiresAt) && expiresAt > now.getTime()
}
