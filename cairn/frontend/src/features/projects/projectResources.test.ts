import { describe, expect, it } from 'vitest'

import type { ProjectResourceConfig } from '@/api/types'
import {
  canMutateProjectResources,
  formatProjectResourceSize,
  inferProjectResourceKind,
  projectResourceFileError,
  projectResourceKindOptions,
  projectResourcePathError,
  projectResourcePayload,
} from './projectResources'

const config: ProjectResourceConfig = {
  kinds: ['note', 'script', 'file'],
  limits: {
    max_active_count: 200,
    max_file_bytes: 10 * 1024 * 1024,
    max_active_total_bytes: 100 * 1024 * 1024,
    max_zip_upload_bytes: 50 * 1024 * 1024,
    max_zip_entries: 400,
    max_compression_ratio: 200,
    max_open_batches_per_user: 4,
    max_staged_bytes_per_user: 200 * 1024 * 1024,
    max_path_bytes: 1024,
    max_path_segment_bytes: 255,
  },
  reserved_names: ['manifest.json'],
  batch_ttl_seconds: 86400,
  zip_policy: {
    allowed_compression_methods: ['stored', 'deflated'],
    nested_archives: 'opaque',
    preserve_relative_directories: true,
  },
}

describe('project resources', () => {
  it('classifies scripts and notes while treating unknown or binary files as opaque', () => {
    expect(inferProjectResourceKind('auth/login.py')).toBe('script')
    expect(inferProjectResourceKind('notes/flow.md')).toBe('note')
    expect(inferProjectResourceKind('images/diagram.png')).toBe('file')
    expect(inferProjectResourceKind('archives/source.zip')).toBe('file')
    expect(projectResourceKindOptions(config).map((option) => option.value)).toEqual([
      'note',
      'script',
      'file',
    ])
  })

  it('uses server path limits and rejects manifest.json in every nested segment', () => {
    expect(projectResourcePathError('docs/guide.txt', config)).toBe('')
    expect(projectResourcePathError('docs/manifest.json', config)).toContain('保留名称')
    expect(projectResourcePathError('MANIFEST.JSON/assets/logo.png', config)).toContain('保留名称')
    expect(projectResourcePathError('../login.py', config)).toContain('不能包含')
  })

  it('validates arbitrary file bytes without decoding them as UTF-8', () => {
    const binary = new File([new Uint8Array([0xff, 0xd8, 0xff, 0x00])], 'photo.jpg')
    expect(projectResourceFileError(binary, 'photo.jpg', config)).toBe('')

    const tinyConfig = {
      ...config,
      limits: { ...config.limits, max_file_bytes: 2 },
    }
    expect(projectResourceFileError(binary, 'photo.jpg', tinyConfig)).toContain('单文件')
  })

  it('keeps the legacy JSON text payload helper compatible', () => {
    expect(
      projectResourcePayload([
        {
          id: 1,
          kind: 'script',
          path: ' auth/login.py ',
          content: 'print(1)',
          sizeBytes: 8,
        },
      ]),
    ).toEqual([{ kind: 'script', path: 'auth/login.py', content: 'print(1)' }])
    expect(formatProjectResourceSize(10 * 1024 * 1024)).toBe('10.0 MiB')
  })

  it('requires paused or stopped status and a loaded task snapshot with no active tasks', () => {
    expect(canMutateProjectResources('paused', true, [])).toBe(true)
    expect(canMutateProjectResources('paused', false, [])).toBe(false)
    expect(canMutateProjectResources('paused', true, ['running'])).toBe(false)
    expect(canMutateProjectResources('stopped', true, [])).toBe(true)
    expect(canMutateProjectResources('active', true, [])).toBe(false)
    expect(canMutateProjectResources('stopped', false, [])).toBe(false)
    expect(canMutateProjectResources('stopped', true, ['queued'])).toBe(false)
    expect(canMutateProjectResources('stopped', true, ['running'])).toBe(false)
    expect(canMutateProjectResources('stopped', true, ['succeeded'])).toBe(true)
  })
})
