import type { CreateProjectHintRequest, Fact, Hint } from '@/api/types'
import type { BadgeTone } from '@/components/ui/Badge.vue'

export interface HintCreatorMeta {
  label: string
  tone: BadgeTone
}

export interface HintAnchorMeta {
  kind: 'project' | 'fact' | 'intent'
  label: string
  tone: BadgeTone
}

export const middleHintFacts = (facts: readonly Fact[]): Fact[] =>
  facts.filter((fact) => fact.id !== 'origin' && fact.id !== 'goal')

export const hintCreatorMeta = (creator: string): HintCreatorMeta =>
  creator === 'system'
    ? { label: 'AGENT 自治系统', tone: 'info' }
    : { label: creator, tone: 'neutral' }

export const hintAnchorMeta = (hint: Pick<Hint, 'fact_id' | 'intent_id'>): HintAnchorMeta => {
  if (hint.intent_id) return { kind: 'intent', label: `intent ${hint.intent_id}`, tone: 'info' }
  if (hint.fact_id) return { kind: 'fact', label: `fact ${hint.fact_id}`, tone: 'success' }
  return { kind: 'project', label: '项目级', tone: 'neutral' }
}

export const sortHintsNewestFirst = (hints: readonly Hint[]): Hint[] =>
  hints
    .map((hint, sourceIndex) => ({ hint, sourceIndex }))
    .sort(
      (left, right) =>
        right.hint.created_at.localeCompare(left.hint.created_at) ||
        left.sourceIndex - right.sourceIndex,
    )
    .map(({ hint }) => hint)

export const createHintPayload = (
  content: string,
  intentId: string,
  factId: string,
): CreateProjectHintRequest => {
  const body: CreateProjectHintRequest = { content: content.trim() }
  if (intentId) body.intent_id = intentId
  else if (factId) body.fact_id = factId
  return body
}

export const mergeCreatedHint = (hints: readonly Hint[], created: Hint): Hint[] => {
  const existingIndex = hints.findIndex((hint) => hint.id === created.id)
  const merged = [...hints]
  if (existingIndex >= 0) merged[existingIndex] = created
  else merged.push(created)
  return sortHintsNewestFirst(merged)
}
