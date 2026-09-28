import { describe, expect, it } from 'vitest'
import type { ArtifactKind, ProjectArtifact } from '@/api/client'
import {
  artifactFilename,
  artifactIsScreenshot,
  artifactIsTextPreviewable,
  artifactKindLabel,
  artifactViewCategory,
  filterAndSortArtifacts,
  kindOptionsForArtifacts,
  viewModeForArtifact,
} from './artifactsModel'

const artifact = (
  id: string,
  kind: ArtifactKind,
  category: ProjectArtifact['category'],
  createdAt: string,
  uri = `artifacts/${id}`,
): ProjectArtifact => ({
  id,
  project_id: 'p1',
  task_run_id: null,
  kind,
  kind_deprecated: false,
  deprecation_message: null,
  category,
  uri,
  sha256: id.repeat(8),
  size: 12,
  created_at: createdAt,
  metadata: {},
})

describe('artifacts model', () => {
  it('maps workspace views and artifact categories', () => {
    expect(artifactViewCategory('deliverable')).toBe('deliverable')
    expect(artifactViewCategory('diagnostic')).toBe('evidence')
    expect(viewModeForArtifact(artifact('a', 'report', 'deliverable', '2026-08-20'))).toBe(
      'deliverable',
    )
    expect(viewModeForArtifact(artifact('b', 'log', 'evidence', '2026-08-20'))).toBe('diagnostic')
  })

  it('filters by category and exact kind, sorts newest first, and keeps ties stable', () => {
    const source = [
      artifact('old', 'report', 'deliverable', '2026-08-18T00:00:00Z'),
      artifact('tie-one', 'file', 'deliverable', '2026-08-20T00:00:00Z'),
      artifact('evidence', 'log', 'evidence', '2026-08-21T00:00:00Z'),
      artifact('tie-two', 'report', 'deliverable', '2026-08-20T00:00:00Z'),
    ]

    expect(filterAndSortArtifacts(source, 'deliverable', 'all').map(({ id }) => id)).toEqual([
      'tie-one',
      'tie-two',
      'old',
    ])
    expect(filterAndSortArtifacts(source, 'deliverable', 'report').map(({ id }) => id)).toEqual([
      'tie-two',
      'old',
    ])
    expect(filterAndSortArtifacts(source, 'diagnostic', 'all').map(({ id }) => id)).toEqual([
      'evidence',
    ])
    expect(source.map(({ id }) => id)).toEqual(['old', 'tie-one', 'evidence', 'tie-two'])
  })

  it('searches decoded filenames without matching URI directories', () => {
    const source = [
      artifact(
        'encoded',
        'report',
        'deliverable',
        '2026-08-20T00:00:00Z',
        'artifact://projects/p1/runs/r1/reports/Final%20Report.MD',
      ),
      artifact(
        'directory-only',
        'report',
        'deliverable',
        '2026-08-21T00:00:00Z',
        'artifact://projects/p1/runs/final-report/notes.txt',
      ),
      artifact(
        'diagnostic',
        'log',
        'evidence',
        '2026-08-22T00:00:00Z',
        'artifact://projects/p1/runs/r1/final-report.log',
      ),
    ]

    expect(
      filterAndSortArtifacts(source, 'deliverable', 'all', ' final report ').map(({ id }) => id),
    ).toEqual(['encoded'])
    expect(
      filterAndSortArtifacts(source, 'deliverable', 'report', 'REPORT.md').map(({ id }) => id),
    ).toEqual(['encoded'])
    expect(filterAndSortArtifacts(source, 'deliverable', 'all', 'runs')).toEqual([])
    expect(
      filterAndSortArtifacts(source, 'diagnostic', 'log', 'FINAL-REPORT').map(({ id }) => id),
    ).toEqual(['diagnostic'])
    expect(filterAndSortArtifacts(source, 'deliverable', 'all', '   ').map(({ id }) => id)).toEqual(
      ['directory-only', 'encoded'],
    )
    expect(source.map(({ id }) => id)).toEqual(['encoded', 'directory-only', 'diagnostic'])
  })

  it('keeps default kinds and exposes historical kinds present in the current category', () => {
    const source = [
      artifact('capture', 'pcap', 'deliverable', '2026-08-20'),
      artifact('archive', 'har', 'evidence', '2026-08-20'),
    ]

    expect(kindOptionsForArtifacts(source, 'deliverable').map(({ value }) => value)).toEqual([
      'all',
      'screenshot',
      'report',
      'patch',
      'file',
      'pcap',
    ])
    expect(kindOptionsForArtifacts(source, 'diagnostic').map(({ value }) => value)).toEqual([
      'all',
      'raw_output',
      'log',
      'hook_log',
      'har',
    ])
  })

  it('classifies all supported preview kinds and filenames', () => {
    const textKinds: ArtifactKind[] = [
      'raw_output',
      'log',
      'report',
      'patch',
      'file',
      'har',
      'hook_log',
    ]
    for (const kind of textKinds) {
      expect(artifactIsTextPreviewable(artifact(kind, kind, 'evidence', '2026-08-20'))).toBe(true)
      expect(artifactKindLabel(kind)).not.toBe('')
    }
    expect(artifactIsTextPreviewable(artifact('pcap', 'pcap', 'deliverable', '2026-08-20'))).toBe(
      false,
    )
    expect(artifactIsScreenshot(artifact('shot', 'screenshot', 'deliverable', '2026-08-20'))).toBe(
      true,
    )
    expect(
      artifactFilename(
        artifact('encoded', 'report', 'deliverable', '2026-08-20', 'reports/final%20report.md'),
      ),
    ).toBe('final report.md')
  })
})
