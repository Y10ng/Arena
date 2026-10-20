import { assert, describe, it } from 'vitest'

import type { KnowledgeFile } from '@/api/types'
import {
  buildKnowledgeDirectoryGroups,
  buildKnowledgeNavigation,
  knowledgeFileDirectory,
  knowledgeFileName,
  knowledgeFilePathInDirectory,
  nextKnowledgeFilePath,
} from './knowledgeConsoleModel'

const makeFile = (path: string, size = 10): KnowledgeFile => ({
  name: knowledgeFileName(path),
  path,
  size,
  updated_at: null,
  warnings: [],
})

describe('knowledge console directory model', () => {
  it('splits knowledge paths into directory and filename', () => {
    assert.equal(knowledgeFileDirectory('policies/auth/oauth.md'), 'policies/auth')
    assert.equal(knowledgeFileName('policies/auth/oauth.md'), 'oauth.md')
    assert.equal(knowledgeFileDirectory('README.md'), '')
    assert.equal(knowledgeFileName('README.md'), 'README.md')
  })

  it('groups files by directory with root first', () => {
    const groups = buildKnowledgeDirectoryGroups([
      makeFile('frontend/vue.md', 5),
      makeFile('README.md', 3),
      makeFile('frontend/react.md', 7),
      makeFile('backend/api/openapi.md', 11),
    ])

    assert.deepEqual(
      groups.map((group) => [group.directory, group.files.length, group.totalSize]),
      [
        ['', 1, 3],
        ['backend', 0, 0],
        ['backend/api', 1, 11],
        ['frontend', 2, 12],
      ],
    )
  })

  it('shows only expanded directories and reveals ancestors for matching file paths', () => {
    const files = [makeFile('README.md'), makeFile('backend/api/openapi.md')]
    assert.deepEqual(
      buildKnowledgeNavigation(files, new Set()).map((entry) => entry.path),
      ['backend', 'README.md'],
    )
    assert.deepEqual(
      buildKnowledgeNavigation(files, new Set(['backend'])).map((entry) => entry.path),
      ['backend', 'backend/api', 'README.md'],
    )
    assert.deepEqual(
      buildKnowledgeNavigation(files, new Set(), 'API/OPEN').map((entry) => [
        entry.path,
        entry.depth,
      ]),
      [
        ['backend', 0],
        ['backend/api', 1],
        ['backend/api/openapi.md', 2],
      ],
    )
    assert.deepEqual(buildKnowledgeNavigation([], new Set()), [])
  })

  it('builds new file paths inside the selected directory', () => {
    assert.equal(knowledgeFilePathInDirectory('frontend', 'README.md'), 'frontend/README.md')
    assert.equal(knowledgeFilePathInDirectory('', 'README.md'), 'README.md')
  })

  it('picks a non-conflicting README name in the selected directory', () => {
    const files = [
      makeFile('frontend/README.md'),
      makeFile('frontend/README-2.md'),
      makeFile('README.md'),
    ]

    assert.equal(nextKnowledgeFilePath('frontend', files), 'frontend/README-3.md')
    assert.equal(nextKnowledgeFilePath('backend', files), 'backend/README.md')
  })
})
