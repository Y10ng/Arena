import { beforeEach, describe, expect, it, vi } from 'vitest'
import { api } from './client'
import { fetchWorkspacePreview, listWorkspaceEntries, workspaceContentUrl } from './workspace'
vi.mock('./client', () => ({ api: vi.fn() }))
beforeEach(() => vi.clearAllMocks())
describe('workspace read-only API', () => {
  it('encodes relative directory, identity and continuation with cancellation', async () => {
    const controller = new AbortController()
    await listWorkspaceEntries('p/1', '中文/a&b', 'wid', 'cursor+/', controller.signal)
    const [method, url, body, signal] = vi.mocked(api).mock.calls[0]!
    expect(method).toBe('GET')
    expect(body).toBeUndefined()
    expect(signal).toBe(controller.signal)
    expect(url).toMatch(/^\/projects\/p%2F1\/workspace\/entries\?/)
    const query = new URL(url, 'http://localhost').searchParams
    expect(query.get('path')).toBe('中文/a&b')
    expect(query.get('workspace_id')).toBe('wid')
    expect(query.get('cursor')).toBe('cursor+/')
  })
  it('does not invent identity for initial root and supports bounded head/tail preview', async () => {
    await listWorkspaceEntries('p1')
    expect(vi.mocked(api).mock.calls[0]![1]).toBe('/projects/p1/workspace/entries?path=')
    await fetchWorkspacePreview('p1', 'wid', 'file.txt', 'tail')
    expect(vi.mocked(api).mock.calls[1]![1]).toBe(
      '/projects/p1/workspace/preview?workspace_id=wid&path=file.txt&window=tail',
    )
  })
  it('returns a direct full download URL instead of fetching a Blob', () => {
    expect(workspaceContentUrl('p1', 'wid', 'a b.txt')).toBe(
      '/projects/p1/workspace/content?workspace_id=wid&path=a+b.txt',
    )
    expect(workspaceContentUrl('p1', 'wid', 'a.png', true)).toContain('&inline=true')
    expect(api).not.toHaveBeenCalled()
  })
})
