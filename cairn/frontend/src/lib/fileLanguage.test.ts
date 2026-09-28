import { describe, expect, it } from 'vitest'
import { fileNameFromUri, isMarkdownFile, resolveFileLanguage } from './fileLanguage'

describe('fileLanguage', () => {
  it('resolves common knowledge file languages', () => {
    expect(resolveFileLanguage('README.md')).toBe('markdown')
    expect(resolveFileLanguage('rules.yaml')).toBe('yaml')
    expect(resolveFileLanguage('config.json')).toBe('json')
    expect(resolveFileLanguage('notes.txt')).toBe('plaintext')
  })

  it('resolves code artifact languages from uri file names', () => {
    const uri = 'artifact://projects/p001/runs/tr001/artifact_001-script.py'
    expect(fileNameFromUri(uri)).toBe('artifact_001-script.py')
    expect(resolveFileLanguage(uri, 'file')).toBe('python')
  })

  it('uses artifact kind fallbacks when the uri has no useful suffix', () => {
    expect(resolveFileLanguage('artifact://projects/p001/runs/tr001/artifact_002', 'har')).toBe('json')
    expect(resolveFileLanguage('artifact://projects/p001/runs/tr001/artifact_003', 'report')).toBe('markdown')
    expect(resolveFileLanguage('artifact://projects/p001/runs/tr001/artifact_004', 'raw_output')).toBe('plaintext')
  })

  it('recognizes markdown files through encoded artifact uris', () => {
    expect(isMarkdownFile('artifact://x/runs/y/report%20final.md', 'file')).toBe(true)
  })
})
