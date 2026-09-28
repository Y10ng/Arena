/// <reference types="node" />

import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { describe, expect, it } from 'vitest'

const projectsSource = readFileSync(
  fileURLToPath(new URL('./projects/Projects-list.css', import.meta.url)),
  'utf8',
)
const scenarioSource = readFileSync(
  fileURLToPath(new URL('./console/scenario-console.css', import.meta.url)),
  'utf8',
)
const knowledgeSource = readFileSync(
  fileURLToPath(new URL('./console/knowledge-console.css', import.meta.url)),
  'utf8',
)
const filterBarSource = readFileSync(
  fileURLToPath(new URL('../components/patterns/FilterBar.vue', import.meta.url)),
  'utf8',
)

const semanticSurfaceTokens = [
  'var(--bg-surface)',
  'var(--surface-hover)',
  'var(--surface-selected)',
  'var(--surface-disabled)',
  'var(--text-disabled)',
]

const ruleBody = (source: string, selector: string): string => {
  const escapedSelector = selector.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
  return source.match(new RegExp(`${escapedSelector}[^{}]*\\{([^}]*)\\}`))?.[1] || ''
}

describe('feature surface state contract', () => {
  it('keeps dynamic FilterBar status sizing opt-in and responsive', () => {
    expect(filterBarSource).toContain(
      'min-inline-size: var(--filter-bar-status-min-inline-size, 0)',
    )
    expect(filterBarSource).toContain('text-align: var(--filter-bar-status-text-align, start)')
    expect(filterBarSource).toContain('white-space: var(--filter-bar-status-white-space, normal)')
    expect(filterBarSource).toContain('min-inline-size: 0')
    expect(projectsSource).toContain('--filter-bar-status-min-inline-size: 11em')
    expect(projectsSource).toContain('--filter-bar-status-text-align: end')
    expect(projectsSource).toContain('--filter-bar-status-white-space: nowrap')
  })

  it('resets native list buttons to their parent surfaces', () => {
    expect(ruleBody(projectsSource, '.segment-btn')).toContain('background-color: transparent')
    expect(ruleBody(projectsSource, '.project-row-main')).toContain('background-color: transparent')

    ;['.scenario-list-item', '.knowledge-set-item', '.knowledge-file-item'].forEach((selector) => {
      expect(ruleBody(scenarioSource, selector)).toContain('background-color: transparent')
    })
    expect(ruleBody(knowledgeSource, '.knowledge-directory-item')).toContain(
      'background-color: transparent',
    )
  })

  it('defines explicit project segment states with disabled precedence', () => {
    expect(projectsSource).toContain("[data-theme='light'] .segment-btn {")
    expect(projectsSource).toContain("[data-theme='light'] .segment-btn:hover:not(:disabled) {")
    expect(projectsSource).toContain("[data-theme='light'] .segment-btn.active:not(:disabled) {")
    expect(projectsSource).toContain("[data-theme='light'] .segment-btn:disabled {")
    expect(projectsSource.indexOf('.segment-btn:disabled {')).toBeGreaterThan(
      projectsSource.indexOf('.segment-btn.active:not(:disabled) {'),
    )
    semanticSurfaceTokens.forEach((token) => expect(projectsSource).toContain(token))
  })

  it('uses the same scoped state vocabulary for scenario and knowledge rows', () => {
    ;['scenario-list-item', 'knowledge-set-item', 'knowledge-file-item'].forEach((className) => {
      expect(scenarioSource).toContain(`[data-theme='light'] .${className}`)
      expect(scenarioSource).toContain(`.${className}:hover:not(:disabled)`)
      expect(scenarioSource).toContain(`.${className}.active:not(:disabled)`)
      expect(scenarioSource).toContain(`.${className}:disabled`)
    })
    semanticSurfaceTokens.forEach((token) => expect(scenarioSource).toContain(token))
    expect(scenarioSource).toContain('.knowledge-set-item.invalid')
    expect(scenarioSource).toContain('border-left: 2px solid var(--danger)')
  })

  it('keeps knowledge directory states light-theme scoped and semantic', () => {
    expect(knowledgeSource).toContain("[data-theme='light'] .knowledge-directory-item {")
    expect(knowledgeSource).toContain(
      "[data-theme='light'] .knowledge-directory-item:hover:not(:disabled) {",
    )
    expect(knowledgeSource).toContain(
      "[data-theme='light'] .knowledge-directory-item.active:not(:disabled) {",
    )
    expect(knowledgeSource).toContain("[data-theme='light'] .knowledge-directory-item:disabled {")
    semanticSurfaceTokens.forEach((token) => expect(knowledgeSource).toContain(token))
    expect(knowledgeSource).toContain('border-left: 2px solid var(--accent)')
  })
})
