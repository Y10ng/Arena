import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { defineComponent, reactive } from 'vue'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiError, type ArtifactPreview, type ProjectArtifact } from '@/api/client'
import type { WorkspaceEntries, WorkspaceEntry, WorkspacePreview, WorkspaceSearch } from '@/api/workspace'
import ArtifactsPage from './ArtifactsPage.vue'

const mocks = vi.hoisted(() => ({
  entries: vi.fn(),
  search: vi.fn(),
  preview: vi.fn(),
  saved: vi.fn(),
  head: vi.fn(),
  tail: vi.fn(),
  push: vi.fn(),
  replace: vi.fn(),
  toast: vi.fn(),
}))
vi.mock('@/api/workspace', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/workspace')>()),
  listWorkspaceEntries: mocks.entries,
  searchWorkspaceFiles: mocks.search,
  fetchWorkspacePreview: mocks.preview,
}))
vi.mock('@/api/client', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/client')>()),
  listProjectArtifacts: mocks.saved,
  fetchArtifactPreview: mocks.head,
  fetchArtifactTailPreview: mocks.tail,
}))
const route = reactive({ query: {} as Record<string, string>, params: { id: 'p1' } })
const store = reactive({ currentProjectId: 'p1' })
const ui = reactive({ globalRefreshKey: 0, showToast: mocks.toast })
vi.mock('vue-router', () => ({
  useRoute: () => route,
  useRouter: () => ({ push: mocks.push, replace: mocks.replace }),
}))
vi.mock('@/stores/workspace', () => ({ useWorkspaceStore: () => store }))
vi.mock('@/stores/ui', () => ({ useUIStore: () => ui }))
vi.mock('@/components/MonacoEditor.vue', () => ({
  __esModule: true,
  __isTeleport: false,
  default: defineComponent({
    name: 'MonacoEditor',
    props: ['modelValue', 'language', 'readingState', 'readingView'],
    template: '<pre data-testid="monaco">{{ modelValue }}</pre>',
  }),
}))
vi.mock('@/components/MarkdownPreview.vue', () => ({
  default: defineComponent({
    props: ['content'],
    template: '<article data-testid="markdown">{{ content }}</article>',
  }),
}))
const wid = 'a'.repeat(64)
const file = (path: string, kind: WorkspaceEntry['kind'] = 'file'): WorkspaceEntry => ({
  name: path.split('/').pop()!,
  path,
  kind,
  size: kind === 'directory' ? null : 200,
  modified_at: '2026-11-03T00:00:00Z',
})
const listing = (
  entries: WorkspaceEntry[] = [file('notes', 'directory'), file('readme.md'), file('output.txt')],
  overrides: Partial<WorkspaceEntries> = {},
): WorkspaceEntries => ({
  project_id: 'p1',
  workspace_id: wid,
  root_path: '/home/kali/workspace',
  path: '',
  entries,
  next_cursor: null,
  has_more: false,
  live: true,
  ...overrides,
})
const preview = (
  path: string,
  content = 'hello',
  overrides: Partial<WorkspacePreview> = {},
): WorkspacePreview => ({
  project_id: 'p1',
  workspace_id: wid,
  path,
  size: 200,
  modified_at: '2026-11-03T00:00:00Z',
  preview_kind: 'text',
  media_type: 'text/plain',
  content,
  bytes_read: 5,
  truncated: true,
  window: 'head',
  live: true,
  ...overrides,
})
const saved: ProjectArtifact = {
  id: 'original',
  project_id: 'p1',
  task_run_id: 'run-1',
  kind: 'report',
  kind_deprecated: false,
  deprecation_message: null,
  category: 'deliverable',
  uri: 'saved/readme.md',
  sha256: 'a'.repeat(64),
  size: 200,
  created_at: '2026-11-03T00:00:00Z',
  metadata: {},
}
const savedPreview = (overrides: Partial<ArtifactPreview> = {}): ArtifactPreview => ({
  artifact_id: saved.id,
  project_id: 'p1',
  task_run_id: saved.task_run_id,
  kind: saved.kind,
  kind_deprecated: false,
  deprecation_message: null,
  content: 'saved original',
  size: 200,
  bytes_read: 14,
  truncated: true,
  sha256: saved.sha256,
  ...overrides,
})
function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason: unknown) => void
  const promise = new Promise<T>((res, rej) => {
    resolve = res
    reject = rej
  })
  return { promise, resolve, reject }
}
let wrapper: VueWrapper
async function render() {
  wrapper = mount(ArtifactsPage, { attachTo: document.body })
  await flushPromises()
  return wrapper
}
const row = (path: string, kind?: string) =>
  wrapper.get(`[role="treeitem"][data-path="${path}"]${kind ? `[data-kind="${kind}"]` : ''}`)
const button = (text: string) => wrapper.findAll('button').find((item) => item.text() === text)!
async function choose(path: string) {
  await row(path).trigger('click')
  await flushPromises()
}
beforeEach(() => {
  vi.clearAllMocks()
  store.currentProjectId = 'p1'
  route.query = {}
  route.params.id = 'p1'
  ui.globalRefreshKey = 0
  mocks.entries.mockImplementation(async (_project, path) =>
    listing(path ? [file(`${path}/nested.txt`)] : undefined, { path }),
  )
  mocks.preview.mockImplementation(async (_project, _workspace, path, window) =>
    preview(path, `content:${path}:${window}`, { window }),
  )
  mocks.saved.mockResolvedValue([saved])
  mocks.head.mockResolvedValue(savedPreview())
  mocks.tail.mockResolvedValue(savedPreview({ content: 'saved tail' }))
  mocks.replace.mockImplementation(async ({ query }) => {
    route.query = query
  })
  Object.defineProperty(HTMLElement.prototype, 'hasPointerCapture', {
    configurable: true,
    value: () => false,
  })
  Object.defineProperty(HTMLElement.prototype, 'setPointerCapture', {
    configurable: true,
    value: () => undefined,
  })
  Object.defineProperty(HTMLElement.prototype, 'releasePointerCapture', {
    configurable: true,
    value: () => undefined,
  })
  Object.defineProperty(HTMLElement.prototype, 'scrollIntoView', {
    configurable: true,
    value: () => undefined,
  })
})
afterEach(() => {
  wrapper?.unmount()
  document.body.innerHTML = ''
})

describe('real research workspace browser', () => {
  it('loads only one level, leaves the tree visible beside preview and removes legacy categories', async () => {
    await render()
    expect(mocks.entries).toHaveBeenCalledTimes(1)
    expect(mocks.entries.mock.calls[0]!.slice(0, 4)).toEqual(['p1', '', undefined, undefined])
    expect(mocks.saved).not.toHaveBeenCalled()
    expect(wrapper.text()).not.toContain('不是原子快照或正式归档')
    expect(wrapper.find('input[aria-label="搜索文件名或路径"]').exists()).toBe(true)
    expect(button('刷新').classes()).toContain('ui-button--secondary')
    for (const removed of ['项目文件', '诊断日志', '正式报告清单', '返回文件'])
      expect(wrapper.text()).not.toContain(removed)
    expect(wrapper.find('select').exists()).toBe(false)
    await choose('readme.md')
    expect(wrapper.get('[role="tree"]').isVisible()).toBe(true)
    expect(wrapper.get('[data-testid="markdown"]').text()).toContain('content:readme.md:head')
    expect(row('readme.md').attributes('aria-selected')).toBe('true')
    expect(wrapper.get('a[download]').attributes('href')).toContain('/workspace/content?')
    expect(wrapper.get('a[download]').attributes('href')).not.toContain('inline=true')
    expect(wrapper.text()).toContain('内容已截断')
    await button('查看源码').trigger('click')
    await flushPromises()
    expect(wrapper.get('[data-testid="monaco"]').text()).toContain('content:readme.md:head')
  })
  it('lazily expands by keyboard, moves focus, and keeps directories from replacing selection', async () => {
    await render()
    await choose('readme.md')
    const directory = row('notes')
    ;(directory.element as HTMLElement).focus()
    await directory.trigger('keydown', { key: 'ArrowRight' })
    await flushPromises()
    expect(directory.attributes('aria-expanded')).toBe('true')
    expect(mocks.entries.mock.calls[1]!.slice(0, 4)).toEqual(['p1', 'notes', wid, undefined])
    expect(wrapper.text()).toContain('content:readme.md:head')
    await directory.trigger('keydown', { key: 'ArrowRight' })
    await flushPromises()
    expect(document.activeElement).toBe(row('notes/nested.txt').element)
    await row('notes/nested.txt').trigger('keydown', { key: 'ArrowLeft' })
    await flushPromises()
    expect(document.activeElement).toBe(directory.element)
    await directory.trigger('keydown', { key: 'ArrowLeft' })
    await flushPromises()
    expect(directory.attributes('aria-expanded')).toBe('false')
    await directory.trigger('keydown', { key: 'Enter' })
    await flushPromises()
    expect(directory.attributes('aria-expanded')).toBe('true')
    expect(mocks.entries).toHaveBeenCalledTimes(2)
  })
  it('supports root and directory continuation without eager recursive enumeration', async () => {
    mocks.entries.mockImplementation(async (_project, path, _id, cursor) =>
      path
        ? listing(cursor ? [file('notes/second.txt')] : [file('notes/first.txt')], {
            path,
            next_cursor: cursor ? null : 'child-page',
            has_more: !cursor,
          })
        : listing(cursor ? [file('last.txt')] : [file('notes', 'directory')], {
            next_cursor: cursor ? null : 'root-page',
            has_more: !cursor,
          }),
    )
    await render()
    await button('继续加载目录').trigger('click')
    await flushPromises()
    expect(row('last.txt').element).toBeTruthy()
    expect(mocks.entries.mock.calls[1]!.slice(0, 4)).toEqual(['p1', '', wid, 'root-page'])
    await choose('notes')
    await row('notes', 'more').trigger('keydown', { key: 'Enter' })
    await flushPromises()
    expect(row('notes/first.txt').element).toBeTruthy()
    expect(row('notes/second.txt').element).toBeTruthy()
    expect(mocks.entries.mock.calls[3]!.slice(0, 4)).toEqual(['p1', 'notes', wid, 'child-page'])
  })
  it('supports directory names that match Object prototype keys', async () => {
    mocks.entries.mockImplementation(async (_project, path) =>
      listing(path ? [file(`${path}/note.txt`)] : [file('__proto__', 'directory'), file('constructor', 'directory')], { path }),
    )
    await render()
    await choose('__proto__')
    expect(row('__proto__/note.txt').element).toBeTruthy()
    await choose('constructor')
    expect(row('constructor/note.txt').element).toBeTruthy()
  })
  it('shows empty directories and keeps blocked files non-readable', async () => {
    mocks.entries.mockImplementation(async (_project, path) =>
      listing(path ? [] : [file('empty', 'directory'), file('link', 'blocked')], { path }),
    )
    await render()
    await choose('empty')
    expect(wrapper.text()).toContain('目录为空')
    await choose('link')
    expect(mocks.preview).not.toHaveBeenCalled()
    expect(wrapper.find('a[download]').exists()).toBe(false)
    expect(row('link').attributes('title')).toContain('不允许读取')
  })
  it.each([
    ['workspace_not_created', '尚未创建'],
    ['workspace_owner_unknown', '归属'],
    ['workspace_deleted', '已删除'],
    ['workspace_permission_denied', '权限'],
    ['workspace_unreachable', '不可达'],
    ['workspace_layer_unsupported', '历史现场'],
  ])('distinguishes %s from an empty directory', async (code, expected) => {
    mocks.entries.mockRejectedValue(new ApiError('host-secret', 409, { detail: code }))
    await render()
    expect(wrapper.text()).toContain(expected)
    expect(wrapper.text()).not.toContain('目录为空')
    expect(wrapper.text()).not.toContain('host-secret')
    expect(button('重试目录').exists()).toBe(true)
  })
  it('retains selection, expansion and focused row across refresh', async () => {
    await render()
    await choose('notes')
    await choose('notes/nested.txt')
    ;(row('notes/nested.txt').element as HTMLElement).focus()
    ui.globalRefreshKey++
    await flushPromises()
    expect(row('notes').attributes('aria-expanded')).toBe('true')
    expect(row('notes/nested.txt').attributes('aria-selected')).toBe('true')
    expect(document.activeElement).toBe(row('notes/nested.txt').element)
    expect(wrapper.text()).toContain('content:notes/nested.txt:head')
  })
  it('keeps the reader mounted and reuses Monaco reading state during refresh', async () => {
    await render()
    await choose('output.txt')
    const editor = wrapper.findComponent({ name: 'MonacoEditor' })
    const element = editor.element
    const state = editor.props('readingState')
    const view = editor.props('readingView')
    expect(view).toContain('output.txt')
    const panel = wrapper.get('.workspace-tree-scroll').element as HTMLElement
    const body = wrapper.get('.artifact-preview-body').element as HTMLElement
    panel.scrollTop = 90
    body.scrollTop = 140
    const pending = deferred<WorkspacePreview>()
    mocks.preview.mockReturnValueOnce(pending.promise)
    ui.globalRefreshKey++
    await flushPromises()
    expect(wrapper.get('[data-testid="monaco"]').element).toBe(element)
    expect(wrapper.text()).toContain('暂显示上次读取内容')
    pending.resolve(preview('output.txt', 'updated'))
    await flushPromises()
    expect(wrapper.findComponent({ name: 'MonacoEditor' }).props('readingState')).toBe(state)
    expect(wrapper.findComponent({ name: 'MonacoEditor' }).props('readingView')).toBe(view)
    expect(wrapper.get('[data-testid="monaco"]').element).toBe(element)
    expect(panel.scrollTop).toBe(90)
    expect(body.scrollTop).toBe(140)
  })
  it('does not leak a late directory response into another project or a new visit', async () => {
    const pending = deferred<WorkspaceEntries>()
    mocks.entries.mockReturnValueOnce(pending.promise)
    await render()
    store.currentProjectId = 'p2'
    await flushPromises()
    store.currentProjectId = 'p1'
    await flushPromises()
    pending.resolve(listing([file('stale.txt')]))
    await flushPromises()
    expect(wrapper.text()).not.toContain('stale.txt')
  })
  it('isolates file, preview window and project late responses', async () => {
    const pending = deferred<WorkspacePreview>()
    mocks.preview.mockReturnValueOnce(pending.promise)
    await render()
    await choose('readme.md')
    await choose('output.txt')
    pending.resolve(preview('readme.md', 'STALE'))
    await flushPromises()
    expect(wrapper.text()).not.toContain('STALE')
    expect(wrapper.text()).toContain('content:output.txt:head')
    const tail = deferred<WorkspacePreview>()
    mocks.preview.mockReturnValueOnce(tail.promise)
    await button('查看文件尾部').trigger('click')
    await flushPromises()
    await choose('readme.md')
    tail.resolve(preview('output.txt', 'STALE-TAIL', { window: 'tail' }))
    await flushPromises()
    expect(wrapper.text()).not.toContain('STALE-TAIL')
    const oldProject = deferred<WorkspacePreview>()
    mocks.preview.mockReturnValueOnce(oldProject.promise)
    await choose('output.txt')
    store.currentProjectId = 'p2'
    await flushPromises()
    oldProject.resolve(preview('output.txt', 'OLD-PROJECT'))
    await flushPromises()
    expect(wrapper.text()).not.toContain('OLD-PROJECT')
  })
  it('rejects mismatched identities and clears replaced workspace selection', async () => {
    mocks.preview.mockResolvedValueOnce(preview('other.txt', 'CROSS-PATH'))
    await render()
    await choose('output.txt')
    expect(wrapper.text()).not.toContain('CROSS-PATH')
    expect(wrapper.text()).toContain('读取失败')
    mocks.preview.mockRejectedValueOnce(
      new ApiError('', 409, { detail: 'workspace_identity_changed' }),
    )
    await choose('readme.md')
    expect(wrapper.text()).toContain('身份已变化')
    expect(wrapper.find('a[download]').exists()).toBe(false)
    expect(wrapper.find('[role="tree"]').exists()).toBe(false)
  })
  it('uses the inline endpoint only for server-confirmed safe images and leaves HTML/SVG as text', async () => {
    mocks.entries.mockResolvedValue(
      listing([file('a.png'), file('page.html'), file('a.svg'), file('capture.pcap')]),
    )
    mocks.preview.mockResolvedValueOnce(
      preview('a.png', '', { preview_kind: 'image', content: null, media_type: 'image/png' }),
    )
    await render()
    await choose('a.png')
    expect(wrapper.get('img').attributes('src')).toContain('inline=true')
    await wrapper.get('img').trigger('error')
    expect(wrapper.text()).toContain('图片载入失败')
    mocks.preview.mockResolvedValueOnce(preview('page.html', '<script>canary()</script>'))
    await choose('page.html')
    expect(wrapper.find('iframe, script').exists()).toBe(false)
    expect(wrapper.get('[data-testid="monaco"]').text()).toContain('<script>')
    mocks.preview.mockResolvedValueOnce(preview('a.svg', '<svg onload="canary()"/>'))
    await choose('a.svg')
    expect(wrapper.find('img').exists()).toBe(false)
    mocks.preview.mockResolvedValueOnce(
      preview('capture.pcap', '', { content: null, preview_kind: 'binary' }),
    )
    await choose('capture.pcap')
    expect(wrapper.text()).toContain('不支持在线预览')
    expect(wrapper.get('a[download]').element).toBeTruthy()
  })
  it('keeps long names accessible and announces lossy UTF-8 windows', async () => {
    const name = `${'长文件名'.repeat(30)}.txt`
    mocks.entries.mockResolvedValue(listing([file(name)]))
    mocks.preview.mockResolvedValue(preview(name, '�', { encoding_lossy: true }))
    await render()
    await choose(name)
    expect(row(name).attributes('title')).toBe(name)
    expect(wrapper.text()).toContain('替代字符')
  })
})

describe('workspace filename search', () => {
  const result = (query: string, entries: WorkspaceEntry[] = [], more = false): WorkspaceSearch => ({
    ...listing(entries), query, next_cursor: more ? 'next' : null, has_more: more,
  })
  async function search(query: string) {
    await wrapper.get('input[aria-label="搜索文件名或路径"]').setValue(query)
    await wrapper.get('form[role="search"]').trigger('submit')
    await flushPromises()
  }
  it('submits explicitly, continues incomplete empty pages, previews unopened paths, and restores the tree', async () => {
    mocks.search.mockResolvedValueOnce(result('nested', [], true))
      .mockResolvedValueOnce(result('nested', [file('unopened/nested.txt')]))
      .mockResolvedValueOnce(result('nested', [file('unopened/nested.txt')]))
    await render()
    await choose('notes')
    const tree = wrapper.get('.workspace-tree-scroll').element as HTMLElement
    tree.scrollTop = 75
    const input = wrapper.get('input[aria-label="搜索文件名或路径"]')
    await input.setValue('nested')
    expect(mocks.search).not.toHaveBeenCalled()
    const ime = new KeyboardEvent('keydown', { key: 'Enter', isComposing: true, cancelable: true })
    input.element.dispatchEvent(ime)
    expect(ime.defaultPrevented).toBe(true)
    await search('nested')
    expect(wrapper.text()).not.toContain('未找到匹配文件')
    await button('继续查找').trigger('click')
    await flushPromises()
    expect(mocks.search.mock.calls[1]!.slice(0, 4)).toEqual(['p1', wid, 'nested', 'next'])
    await wrapper.get('.workspace-search-result').trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('content:unopened/nested.txt:head')
    expect(mocks.entries.mock.calls.every((call) => call[1] !== 'unopened')).toBe(true)
    const calls = mocks.entries.mock.calls.length
    await button('刷新').trigger('click')
    await flushPromises()
    expect(mocks.search.mock.calls[2]!.slice(0, 4)).toEqual(['p1', wid, 'nested', undefined])
    expect(mocks.entries).toHaveBeenCalledTimes(calls)
    await input.trigger('keydown', { key: 'Escape' })
    expect(row('notes').attributes('aria-expanded')).toBe('true')
    expect(tree.scrollTop).toBe(75)
    expect(wrapper.text()).toContain('content:unopened/nested.txt:head')
    expect(mocks.preview).toHaveBeenCalledTimes(2)
  })
  it('isolates edited, cleared, replaced and old-project responses, with a true completed no-match state', async () => {
    await render()
    for (const change of ['edit', 'clear', 'project']) {
      const pending = deferred<WorkspaceSearch>()
      mocks.search.mockReturnValueOnce(pending.promise)
      await search('old')
      const signal = mocks.search.mock.calls.at(-1)![4] as AbortSignal
      if (change === 'edit') await wrapper.get('input').setValue('new')
      else if (change === 'clear') await wrapper.get('[aria-label="清除搜索"]').trigger('click')
      else { store.currentProjectId = 'p2'; await flushPromises() }
      expect(signal.aborted).toBe(true)
      pending.resolve(result('old', [file('STALE.txt')]))
      await flushPromises()
      expect(wrapper.text()).not.toContain('STALE')
    }
    store.currentProjectId = 'p1'
    await flushPromises()
    mocks.search.mockResolvedValueOnce(result('none'))
    await search('none')
    expect(wrapper.text()).toContain('未找到匹配文件')
    mocks.search.mockRejectedValueOnce(new ApiError('', 403, { detail: 'workspace_permission_denied' }))
    await search('blocked')
    expect(wrapper.text()).toContain('没有读取')
    expect(wrapper.text()).not.toContain('未找到匹配文件')
    mocks.search.mockRejectedValueOnce(new ApiError('', 409, { detail: 'workspace_identity_changed' }))
    await search('changed')
    expect(wrapper.text()).toContain('身份已变化')
    expect(wrapper.find('.workspace-search-result').exists()).toBe(false)
  })
})

describe('saved Artifact deep links stay independent', () => {
  it('keeps the real tree visible without a fabricated selection and preserves source metadata', async () => {
    route.query = { artifactId: saved.id }
    await render()
    expect(wrapper.text()).toContain('已保存原件')
    expect(wrapper.text()).toContain('saved original')
    expect(wrapper.findAll('[role="treeitem"][aria-selected="true"]')).toHaveLength(0)
    expect(wrapper.get('a[download]').attributes('href')).toBe(
      '/projects/p1/artifacts/original/content',
    )
    await button('文件信息').trigger('click')
    await flushPromises()
    expect(document.body.textContent).toContain(saved.sha256)
    const run = Array.from(document.body.querySelectorAll('button')).find(
      (item) => item.textContent === '查看运行',
    )!
    run.click()
    await flushPromises()
    expect(mocks.push).toHaveBeenCalledWith({
      name: 'WorkspaceRuns',
      params: { id: 'p1' },
      query: { runId: 'run-1' },
    })
    await button('查看文件尾部').trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('saved tail')
  })
  it('does not clear valid deep links when live workspace is unavailable', async () => {
    route.query = { artifactId: saved.id }
    mocks.entries.mockRejectedValue(new ApiError('', 410, { detail: 'workspace_deleted' }))
    await render()
    expect(wrapper.text()).toContain('已删除')
    expect(wrapper.text()).toContain('saved original')
    expect(mocks.replace).not.toHaveBeenCalled()
  })
  it('lets an in-flight saved preview finish after live workspace identity changes', async () => {
    const pending = deferred<ArtifactPreview>()
    mocks.head.mockReturnValueOnce(pending.promise)
    route.query = { artifactId: saved.id }
    await render()
    mocks.entries.mockRejectedValueOnce(
      new ApiError('', 409, { detail: 'workspace_identity_changed' }),
    )
    await button('刷新').trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('身份已变化')
    pending.resolve(savedPreview())
    await flushPromises()
    expect(wrapper.text()).toContain('saved original')
    expect(wrapper.text()).not.toContain('正在载入预览')
    expect(wrapper.get('a[download]').attributes('href')).toContain('/artifacts/original/content')
  })
  it('retains invalid link identity with an explicit error rather than guessing a matching file', async () => {
    route.query = { artifactId: 'missing' }
    await render()
    expect(wrapper.text()).toContain('指定的已保存原件不存在')
    expect(mocks.preview).not.toHaveBeenCalled()
    expect(mocks.replace).not.toHaveBeenCalled()
  })
  it('ignores saved-list and saved-preview responses after selecting a real file', async () => {
    const pending = deferred<ProjectArtifact[]>()
    mocks.saved.mockReturnValueOnce(pending.promise)
    route.query = { artifactId: saved.id }
    await render()
    await choose('readme.md')
    pending.resolve([saved])
    await flushPromises()
    expect(wrapper.text()).toContain('content:readme.md:head')
    expect(wrapper.text()).not.toContain('已保存原件')
    expect(wrapper.text()).not.toContain('saved original')
    const previewPending = deferred<ArtifactPreview>()
    mocks.head.mockReturnValueOnce(previewPending.promise)
    route.query = { artifactId: saved.id }
    await flushPromises()
    await choose('output.txt')
    previewPending.resolve(savedPreview({ content: 'OLD-SAVED' }))
    await flushPromises()
    expect(wrapper.text()).not.toContain('OLD-SAVED')
    expect(route.query.artifactId).toBeUndefined()
  })
  it('blocks mismatched saved preview identity and arbitrary exception detail', async () => {
    route.query = { artifactId: saved.id }
    mocks.head.mockResolvedValue(savedPreview({ project_id: 'other', content: 'SECRET' }))
    await render()
    expect(wrapper.text()).not.toContain('SECRET')
    expect(wrapper.text()).toContain('读取失败')
  })
})
