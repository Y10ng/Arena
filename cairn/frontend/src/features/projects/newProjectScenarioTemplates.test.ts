import { describe, expect, it } from 'vitest'

import type { ScenarioCatalogEntry } from '@/api/types'
import { applyScenarioTemplates, type ScenarioTemplateState } from './newProjectScenarioTemplates'

const scenario = (
  key: string,
  originTemplate: string | null,
  goalTemplate: string | null,
): ScenarioCatalogEntry => ({
  key,
  label: key,
  is_default: false,
  origin_template: originTemplate,
  goal_template: goalTemplate,
})

const initialState = (): ScenarioTemplateState => ({
  selectedKey: null,
  origin: { value: '', userTouched: false, lastAutoApplied: null },
  goal: { value: '', userTouched: false, lastAutoApplied: null },
})

describe('applyScenarioTemplates', () => {
  it('fills empty untouched fields on the initial selection and preserves exact whitespace', () => {
    const origin = '  admin:secret\nhttps://target.local/path  \n'
    const goal = '\nConfirm access without changing credentials.\n'

    const result = applyScenarioTemplates(initialState(), scenario('default', origin, goal))

    expect(result.origin.value).toBe(origin)
    expect(result.goal.value).toBe(goal)
    expect(result.origin.lastAutoApplied).toBe(origin)
    expect(result.goal.lastAutoApplied).toBe(goal)
  })

  it('replaces values that still equal their last auto-applied templates', () => {
    const first = applyScenarioTemplates(initialState(), scenario('one', 'origin one', 'goal one'))
    const result = applyScenarioTemplates(first, scenario('two', 'origin two', 'goal two'))

    expect(result.origin.value).toBe('origin two')
    expect(result.goal.value).toBe('goal two')
  })

  it('preserves a user-edited field independently while updating the other field', () => {
    const first = applyScenarioTemplates(initialState(), scenario('one', 'origin one', 'goal one'))
    const edited: ScenarioTemplateState = {
      ...first,
      origin: {
        ...first.origin,
        value: 'user origin\npassword=keep-verbatim',
        userTouched: true,
      },
    }

    const result = applyScenarioTemplates(edited, scenario('two', 'origin two', 'goal two'))

    expect(result.origin.value).toBe('user origin\npassword=keep-verbatim')
    expect(result.goal.value).toBe('goal two')
  })

  it('preserves an empty field that the user explicitly touched', () => {
    const first = applyScenarioTemplates(initialState(), scenario('one', 'origin one', 'goal one'))
    const edited: ScenarioTemplateState = {
      ...first,
      origin: { ...first.origin, value: '', userTouched: true },
    }

    const result = applyScenarioTemplates(edited, scenario('two', 'origin two', 'goal two'))

    expect(result.origin.value).toBe('')
    expect(result.goal.value).toBe('goal two')
  })

  it('clears auto-applied fields when the next scenario has no template', () => {
    const first = applyScenarioTemplates(initialState(), scenario('one', 'origin one', 'goal one'))
    const result = applyScenarioTemplates(first, scenario('two', null, null))

    expect(result.origin).toEqual({ value: '', userTouched: false, lastAutoApplied: null })
    expect(result.goal).toEqual({ value: '', userTouched: false, lastAutoApplied: null })
  })

  it('preserves a user-owned field while clearing the other auto-applied field', () => {
    const first = applyScenarioTemplates(initialState(), scenario('one', 'origin one', 'goal one'))
    const edited: ScenarioTemplateState = {
      ...first,
      origin: { ...first.origin, value: 'user origin', userTouched: true },
    }

    const result = applyScenarioTemplates(edited, scenario('two', null, null))

    expect(result.origin).toEqual(edited.origin)
    expect(result.goal).toEqual({ value: '', userTouched: false, lastAutoApplied: null })
  })

  it('preserves user input even when it equals the previous auto-applied template', () => {
    const first = applyScenarioTemplates(initialState(), scenario('one', 'origin one', 'goal one'))
    const edited: ScenarioTemplateState = {
      ...first,
      origin: { ...first.origin, userTouched: true },
    }

    const result = applyScenarioTemplates(edited, scenario('two', 'origin two', 'goal two'))

    expect(result.origin).toEqual(edited.origin)
    expect(result.goal.value).toBe('goal two')
  })

  it('leaves pristine fields unchanged when the selected scenario has no template', () => {
    const result = applyScenarioTemplates(initialState(), scenario('default', null, null))

    expect(result.origin).toEqual(initialState().origin)
    expect(result.goal).toEqual(initialState().goal)
  })

  it('does not reapply for the same selected key after a catalog refetch', () => {
    const first = applyScenarioTemplates(initialState(), scenario('one', 'origin one', 'goal one'))
    const edited: ScenarioTemplateState = {
      ...first,
      origin: { ...first.origin, value: 'edited origin', userTouched: true },
    }

    const result = applyScenarioTemplates(edited, scenario('one', 'refetched origin', 'refetched goal'))

    expect(result).toBe(edited)
  })
})
