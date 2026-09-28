import { describe, expect, it } from 'vitest'
import { emptyWorkerDraft, normalizeConfig, normalizeWorker } from './useDispatcherFormConfig'

describe('dispatcher form config defaults', () => {
  it('defaults to goal-driven prompts without replacing an explicit group', () => {
    expect(normalizeConfig({}).runtime.prompt_group).toBe('goal-driven-v1')
    expect(normalizeConfig({ runtime: { prompt_group: 'custom' } }).runtime.prompt_group)
      .toBe('custom')
  })

  it('creates pi drafts without exposing a mock runtime switch', () => {
    expect(emptyWorkerDraft().type).toBe('pi')
    expect(normalizeConfig({}).runtime).not.toHaveProperty('allow_mock_workers')
  })

  it('keeps historical engine metadata instead of converting it to pi or mock', () => {
    expect(normalizeWorker({ name: 'Legacy', engine: 'claudecode' }).type).toBe('claudecode')
    expect(normalizeWorker({ name: 'Legacy', type: 'codex' }).type).toBe('codex')
    expect(normalizeWorker({ name: 'Unknown' }).type).toBe('')
    expect(normalizeConfig({ workers: [{ name: 'Legacy', engine: 'future-engine' }] }).workers[0].type)
      .toBe('future-engine')
  })

  it('gives Explore a longer execute budget and keeps conclude bounded', () => {
    const config = normalizeConfig({})

    expect(config.tasks.explore).toMatchObject({
      timeout: 900,
      conclude_timeout: 300,
    })
    expect(config.tasks.bootstrap).toMatchObject({
      timeout: 300,
      conclude_timeout: 90,
    })
  })

  it('preserves explicitly configured Explore budgets', () => {
    const config = normalizeConfig({
      tasks: {
        explore: { timeout: 1200, conclude_timeout: 180 },
      },
    })

    expect(config.tasks.explore).toMatchObject({
      timeout: 1200,
      conclude_timeout: 180,
    })
  })
})
