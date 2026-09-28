import { assert, describe, it } from 'vitest'

import type { PromptFile, PromptGroup } from '@/api/types'
import {
  buildPromptGroupSelectOptions,
  isPromptGroupReferenceValid,
  missingPromptTokens,
  normalizePromptFiles,
  normalizePromptGroups,
  promptGroupKindLabel,
  promptGroupRoleLabel,
  visiblePromptGroupsForConsole,
} from './promptGroupOptions'

const makeGroup = (overrides: Partial<PromptGroup> = {}): PromptGroup => ({
  key: 'playbooks',
  path: '/prompts/playbooks',
  builtin: false,
  managed: true,
  file_count: 5,
  referenced_by: [],
  overrides_builtin: false,
  capabilities: ['exploration'],
  visibility: 'visible',
  valid: true,
  warnings: [],
  ...overrides,
})

const makeFile = (overrides: Partial<PromptFile> = {}): PromptFile => ({
  name: 'reason.md',
  path: 'reason.md',
  size: 10,
  updated_at: null,
  required_tokens: [],
  missing_tokens: [],
  ...overrides,
})

describe('prompt group option helpers', () => {
  it('normalizes legacy metadata and sorts default first, then builtins before custom groups', () => {
    const legacy = {
      ...makeGroup({ key: 'default', builtin: true, managed: false }),
      capabilities: undefined,
      visibility: undefined,
    } as unknown as PromptGroup
    const groups = normalizePromptGroups([
      makeGroup({ key: 'playbooks', builtin: false, managed: true }),
      makeGroup({ key: 'mock', builtin: true, managed: false }),
      legacy,
    ])

    assert.deepEqual(
      groups.map((group) => group.key),
      ['default', 'mock', 'playbooks'],
    )
    assert.deepEqual(groups[0].capabilities, ['exploration'])
    assert.equal(groups[0].visibility, 'visible')
  })

  it('labels ownership and exploration/report roles', () => {
    assert.equal(promptGroupKindLabel(makeGroup({ builtin: true, managed: false })), '内置')
    assert.equal(promptGroupKindLabel(makeGroup({ builtin: false, managed: true })), '自定义')
    assert.equal(
      promptGroupKindLabel(makeGroup({ builtin: true, managed: true, overrides_builtin: true })),
      '覆盖内置',
    )
    assert.equal(promptGroupRoleLabel(makeGroup({ capabilities: ['exploration'] })), '探索')
    assert.equal(promptGroupRoleLabel(makeGroup({ capabilities: ['durable-report'] })), '仅报告')
    assert.equal(
      promptGroupRoleLabel(makeGroup({ capabilities: ['exploration', 'durable-report'] })),
      '探索 + 报告',
    )
  })

  it('filters selectors by capability and keeps a selected compatibility group only', () => {
    const groups = [
      makeGroup({ key: 'default', builtin: true, managed: false }),
      makeGroup({ key: 'durable', capabilities: ['durable-report'] }),
      makeGroup({ key: 'dual', capabilities: ['exploration', 'durable-report'] }),
      makeGroup({
        key: 'legacy',
        builtin: true,
        managed: false,
        visibility: 'compatibility-only',
      }),
    ]

    const exploration = buildPromptGroupSelectOptions({
      groups,
      selectedPromptGroup: 'legacy',
      capability: 'exploration',
    })
    assert.deepEqual(
      exploration.map((option) => option.value),
      ['default', 'dual', 'legacy'],
    )
    assert.match(exploration.at(-1)?.label || '', /兼容保留（仅当前选择）/)
    assert.equal(exploration.at(-1)?.selectable, false)

    const report = buildPromptGroupSelectOptions({
      groups,
      selectedPromptGroup: 'durable',
      capability: 'durable-report',
    })
    assert.deepEqual(
      report.map((option) => option.value),
      ['dual', 'durable'],
    )
    assert.match(report.find((option) => option.value === 'durable')?.label || '', /仅报告/)
  })

  it('preserves missing and capability-incompatible references with distinct validity', () => {
    const groups = [makeGroup({ key: 'playbooks' })]
    const missingOptions = buildPromptGroupSelectOptions({
      groups,
      selectedPromptGroup: 'missing_group',
      capability: 'exploration',
    })
    const missing = missingOptions.find((option) => option.value === 'missing_group')
    assert.equal(missing?.exists, false)
    assert.match(missing?.label || '', /不存在/)

    const incompatibleOptions = buildPromptGroupSelectOptions({
      groups,
      selectedPromptGroup: 'playbooks',
      capability: 'durable-report',
    })
    assert.equal(incompatibleOptions[0]?.exists, true)
    assert.equal(incompatibleOptions[0]?.compatible, false)
    assert.match(incompatibleOptions[0]?.label || '', /不支持报告/)

    assert.equal(isPromptGroupReferenceValid('missing_group', groups, 'exploration'), false)
    assert.equal(isPromptGroupReferenceValid('playbooks', groups, 'exploration'), true)
    assert.equal(isPromptGroupReferenceValid('playbooks', groups, 'durable-report'), false)
    const legacy = makeGroup({
      key: 'default-report',
      builtin: true,
      managed: false,
      capabilities: ['exploration'],
      visibility: 'compatibility-only',
    })
    assert.equal(isPromptGroupReferenceValid('default-report', [legacy], 'durable-report'), false)
    assert.equal(
      isPromptGroupReferenceValid('default-report', [legacy], 'durable-report', [
        'compatibility-only',
      ]),
      true,
    )
    const mock = makeGroup({
      key: 'mock',
      builtin: true,
      managed: false,
      visibility: 'test-only',
    })
    assert.equal(isPromptGroupReferenceValid('mock', [mock], 'exploration'), false)
    assert.equal(isPromptGroupReferenceValid('mock', [mock], 'exploration', ['test-only']), true)
    assert.equal(
      isPromptGroupReferenceValid(
        'playbooks',
        [makeGroup({ key: 'playbooks', valid: false })],
        'exploration',
      ),
      false,
    )
  })

  it('shows visible groups and ordinary custom groups in the management console', () => {
    const groups = visiblePromptGroupsForConsole([
      makeGroup({ key: 'default', builtin: true, managed: false }),
      makeGroup({
        key: 'default-report',
        builtin: true,
        managed: true,
        overrides_builtin: true,
        visibility: 'compatibility-only',
      }),
      makeGroup({
        key: 'mock',
        builtin: true,
        managed: false,
        visibility: 'test-only',
      }),
      makeGroup({ key: 'custom', visibility: 'compatibility-only' }),
    ])

    assert.deepEqual(
      groups.map((group) => group.key),
      ['default', 'custom'],
    )
  })

  it('sorts prompt files in execution order and drops empty paths', () => {
    const files = normalizePromptFiles([
      makeFile({ path: 'bootstrap.md' }),
      makeFile({ path: '' }),
      makeFile({ path: 'reason.md' }),
      makeFile({ path: '_output_lang.md' }),
    ])

    assert.deepEqual(
      files.map((file) => file.path),
      ['reason.md', 'bootstrap.md', '_output_lang.md'],
    )
  })

  it('computes missing placeholders from draft content', () => {
    assert.deepEqual(missingPromptTokens('hello {graph_yaml}', ['{graph_yaml}', '{fact_ids}']), [
      '{fact_ids}',
    ])
  })
})
