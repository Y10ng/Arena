import { describe, expect, it } from 'vitest'

import type { CoverageCell, SurfaceManifestEntry } from '@/api/types'
import {
  CHAIN_CANDIDATE_LABEL,
  CHAIN_VERIFICATION_READY_LABEL,
  countDistinctManifestSurfaces,
  coverageOutcomeLabel,
  coverageRequirementLabel,
  coverageRequirementMeta,
  coverageResultMeta,
  coverageStatusMeta,
  coverageStopReasonLabel,
  coverageTone,
  isCooldownActive,
  qualityStatusLabel,
  qualityStatusMeta,
  qualifiedPercent,
  residualRiskLabel,
} from './coverageModel'

const cell = (overrides: Partial<CoverageCell> = {}): CoverageCell => ({
  project_id: 'p1',
  slot_key: 'slot-1',
  kind: 'coverage',
  description: '',
  status: 'pending',
  surface_kind: 'endpoint',
  test_family: 'authorization',
  coverage_requirement: 'required',
  coverage_rule_id: 'object-authorization',
  applicability_basis: '对象 ID 与角色边界',
  completion_criteria: '完成跨用户变体',
  result: '',
  skip_reason: '',
  blocker_strategy_key: '',
  tier: 'core',
  created_at: '2026-08-17T00:00:00Z',
  updated_at: '2026-08-17T00:00:00Z',
  ...overrides,
})

const observation = (
  entryId: string,
  surfaceId: string,
): Pick<SurfaceManifestEntry, 'entry_id' | 'surface_id'> => ({
  entry_id: entryId,
  surface_id: surfaceId,
})

describe('coverageModel', () => {
  it('distinguishes findings, no-finding, blockers, and skips', () => {
    expect(coverageOutcomeLabel(cell({ result: 'finding' }))).toBe('发现漏洞')
    expect(coverageTone(cell({ result: 'finding' }))).toBe('finding')
    expect(coverageTone(cell({ result: 'tested_no_finding' }))).toBe('success')
    expect(coverageTone(cell({ result: 'temporarily_blocked' }))).toBe('blocked')
    expect(coverageTone(cell({ result: 'out_of_scope' }))).toBe('muted')
  })

  it('keeps operational status and structured result as separate metadata', () => {
    expect(coverageStatusMeta('pending')).toEqual({ label: '待处理', tone: 'neutral' })
    expect(coverageResultMeta('temporarily_blocked')).toEqual({
      label: '暂时受阻',
      tone: 'warning',
    })
    expect(coverageStatusMeta('future-status')).toEqual({
      label: 'future-status',
      tone: 'neutral',
    })
    expect(coverageResultMeta('future-result')).toEqual({
      label: 'future-result',
      tone: 'neutral',
    })
  })

  it('calculates evidence-qualified coverage without treating zero as complete', () => {
    expect(
      qualifiedPercent({
        total_count: 4,
        qualified_count: 3,
        unqualified_count: 1,
        unresolved_count: 0,
        finding_count: 1,
        tested_no_finding_count: 2,
        permanent_limitation_count: 0,
        administrative_skip_count: 0,
      }),
    ).toBe(75)
    expect(
      qualifiedPercent({
        total_count: 6,
        qualified_count: 2,
        unqualified_count: 4,
        unresolved_count: 0,
        finding_count: 0,
        tested_no_finding_count: 2,
        permanent_limitation_count: 0,
        administrative_skip_count: 0,
      }),
    ).toBe(33)
    expect(qualifiedPercent(null)).toBe(0)
  })

  it('uses authoritative requirement before the legacy tier fallback', () => {
    expect(coverageRequirementLabel(cell())).toBe('必测')
    expect(coverageRequirementLabel(cell({ coverage_requirement: 'optional', tier: 'core' }))).toBe(
      '可选',
    )
    expect(coverageRequirementLabel(cell({ coverage_requirement: '', tier: 'core' }))).toBe('必测')
    expect(coverageRequirementLabel(cell({ coverage_requirement: '', tier: 'extended' }))).toBe(
      '可选',
    )
    expect(
      coverageRequirementMeta(cell({ coverage_requirement: 'future-requirement', tier: 'core' })),
    ).toEqual({ label: 'future-requirement', tone: 'neutral' })
  })

  it('counts distinct non-empty Surface identities without discarding observations', () => {
    const observations = [
      observation('entry-1', 'surface-a'),
      observation('entry-2', 'surface-a'),
      observation('entry-3', 'surface-b'),
      observation('entry-4', '  '),
    ]

    expect(countDistinctManifestSurfaces(observations)).toBe(2)
    expect(observations).toHaveLength(4)
    expect(countDistinctManifestSurfaces([])).toBe(0)
  })

  it('evaluates active, expired, missing, and malformed cooldown timestamps', () => {
    const now = new Date('2026-08-17T00:05:00Z')
    expect(isCooldownActive(cell({ blocker_cooldown_until: '2026-08-17T00:10:00Z' }), now)).toBe(
      true,
    )
    expect(isCooldownActive(cell({ blocker_cooldown_until: '2026-08-17T00:04:00Z' }), now)).toBe(
      false,
    )
    expect(isCooldownActive(cell({ blocker_cooldown_until: 'invalid' }), now)).toBe(false)
    expect(isCooldownActive(cell({ blocker_cooldown_until: null }), now)).toBe(false)
  })

  it('keeps quality, convergence, and chain states semantically separate', () => {
    expect(qualityStatusLabel('ready')).toBe('无残余风险')
    expect(qualityStatusLabel('observing')).toBe('存在残余风险')
    expect(qualityStatusLabel('unavailable')).toBe('快照不可用')
    expect(qualityStatusMeta('ready')).toEqual({ label: '无残余风险', tone: 'success' })
    expect(qualityStatusMeta('unavailable')).toEqual({
      label: '快照不可用',
      tone: 'neutral',
    })
    expect(qualityStatusMeta('future-quality')).toEqual({
      label: 'future-quality',
      tone: 'neutral',
    })
    expect(coverageStopReasonLabel('saturated')).toBe('探索已饱和')
    expect(coverageStopReasonLabel('unknown_reason')).toBe('unknown_reason')
    expect(CHAIN_CANDIDATE_LABEL).toBe('漏洞链候选')
    expect(CHAIN_VERIFICATION_READY_LABEL).toBe('可进入链级验证')
  })

  it.each([
    ['coverage_scope_disposition_missing', '覆盖范围处置尚未完成'],
    ['manifest_surface_without_required_cell', 'Manifest Surface 缺少必测 Cell'],
    ['required_cell_unresolved', '仍有必测 Cell 未解决'],
    ['required_cell_unqualified', '仍有必测 Cell 未达到证据要求'],
    ['required_cell_administratively_skipped', '必测 Cell 被管理性跳过'],
    ['permanent_limitation', '存在永久覆盖限制'],
    ['active_target_blocker', '存在活跃目标阻断'],
    ['failed_assessment_attempt', '存在失败的评估尝试'],
    ['finding_pending_independent_verification', '漏洞仍待独立验证'],
    ['finding_independent_verification_not_reproduced', '独立验证未能复现漏洞'],
    ['finding_independent_verification_inconclusive', '独立验证结论不充分'],
    ['finding_independent_verification_conflicting', '独立验证结论存在冲突'],
    ['coverage_stopped:budget_exhausted', '覆盖停止：评估预算已耗尽'],
    ['coverage_stopped:deadline_exceeded', '覆盖停止：评估截止时间已到'],
    ['coverage_stopped:saturated', '覆盖停止：探索已饱和'],
  ])('maps residual risk %s', (code, label) => {
    expect(residualRiskLabel(code)).toBe(label)
  })

  it('keeps unknown residual risk codes visible', () => {
    expect(residualRiskLabel('future_risk')).toBe('future_risk')
  })
})
