import { assert, describe, it } from 'vitest'

import type { PromptFile, PromptGroup } from '@/api/types'
import {
  buildPromptFileCategories,
  createPromptFileSnapshot,
  isPromptFileSnapshotDirty,
  promptGroupCapabilities,
} from './promptConsoleModel'

describe('prompt console draft model', () => {
  it('does not mark the initial empty draft as dirty', () => {
    const snapshot = createPromptFileSnapshot(null, '')

    assert.equal(isPromptFileSnapshotDirty(null, snapshot, snapshot), false)
  })

  it('requires a selected file before reporting unsaved prompt changes', () => {
    const saved = createPromptFileSnapshot(null, '')
    const current = createPromptFileSnapshot(null, 'late editor update')

    assert.equal(isPromptFileSnapshotDirty(null, current, saved), false)
  })

  it('marks a selected prompt file dirty when content differs from the saved snapshot', () => {
    const saved = createPromptFileSnapshot('reason.md', 'saved')
    const current = createPromptFileSnapshot('reason.md', 'edited')

    assert.equal(isPromptFileSnapshotDirty('reason.md', current, saved), true)
  })

  it('groups report and verifier files into virtual categories', () => {
    const makeFile = (path: string): PromptFile => ({
      name: path,
      path,
      size: 1,
      required_tokens: [],
      missing_tokens: [],
    })
    const categories = buildPromptFileCategories([
      makeFile('verifier.md'),
      makeFile('report.md'),
      makeFile('reason.md'),
    ])

    assert.deepEqual(categories.map(category => [category.key, category.files.map(file => file.path)]), [
      ['reasoning', ['reason.md']],
      ['reporting', ['report.md']],
      ['verification', ['verifier.md']],
    ])
  })

  it('distinguishes custom delete and managed override restore actions', () => {
    const base: PromptGroup = {
      key: 'custom',
      path: '/tmp/custom',
      builtin: false,
      managed: true,
      file_count: 5,
      referenced_by: [],
      overrides_builtin: false,
      capabilities: ['exploration'],
      visibility: 'visible',
      valid: true,
      warnings: [],
    }
    assert.deepEqual(promptGroupCapabilities(base), {
      canEdit: true,
      canDelete: true,
      canRestore: false,
      canRepair: false,
    })
    assert.deepEqual(promptGroupCapabilities({
      ...base,
      key: 'default',
      builtin: true,
      overrides_builtin: true,
      referenced_by: ['default'],
      valid: false,
    }), {
      canEdit: true,
      canDelete: false,
      canRestore: true,
      canRepair: true,
    })
  })
})
