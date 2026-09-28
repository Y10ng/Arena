import { flushPromises, mount } from '@vue/test-utils'
import { defineComponent, reactive, ref } from 'vue'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { VueWrapper } from '@vue/test-utils'
import type { ArtifactPreview, ProjectArtifact } from '@/api/client'
import ArtifactsPage from './ArtifactsPage.vue'

const mocks = vi.hoisted(() => ({
  fetchArtifactPreview: vi.fn(),
  fetchArtifactTailPreview: vi.fn(),
  listProjectArtifacts: vi.fn(),
  push: vi.fn(),
  refetch: vi.fn(),
  replace: vi.fn(),
  showToast: vi.fn(),
  useQuery: vi.fn(),
}))

vi.mock('@/api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/client')>()
  return {
    ...actual,
    fetchArtifactPreview: mocks.fetchArtifactPreview,
    fetchArtifactTailPreview: mocks.fetchArtifactTailPreview,
    listProjectArtifacts: mocks.listProjectArtifacts,
  }
})

vi.mock('@tanstack/vue-query', () => ({ useQuery: mocks.useQuery }))

const route = reactive({
  query: {} as Record<string, string | string[] | null | undefined>,
  params: { id: 'project-a' },
})

vi.mock('vue-router', () => ({
  useRoute: () => route,
  useRouter: () => ({ push: mocks.push, replace: mocks.replace }),
}))

const workspaceStore = reactive({ currentProjectId: 'project-a' as string | null })
const uiStore = reactive({ globalRefreshKey: 0, showToast: mocks.showToast })

vi.mock('@/stores/workspace', () => ({ useWorkspaceStore: () => workspaceStore }))
vi.mock('@/stores/ui', () => ({ useUIStore: () => uiStore }))
vi.mock('@/components/MonacoEditor.vue', () => ({
  __esModule: true,
  __isTeleport: false,
  default: defineComponent({
    name: 'MonacoEditor',
    props: ['modelValue', 'language', 'wordWrap'],
    template: '<pre data-testid="monaco">{{ modelValue }}</pre>',
  }),
}))
vi.mock('@/components/MarkdownPreview.vue', () => ({
  __esModule: true,
  __isTeleport: false,
  default: defineComponent({
    name: 'MarkdownPreview',
    props: ['content'],
    template: '<article data-testid="markdown">{{ content }}</article>',
  }),
}))

const queryState = {
  data: ref<ProjectArtifact[] | undefined>([]),
  error: ref<Error | null>(null),
  isError: ref(false),
  isFetching: ref(false),
  isLoading: ref(false),
}
const wrappers: VueWrapper[] = []

let sequence = 0
function artifact(overrides: Partial<ProjectArtifact> = {}): ProjectArtifact {
  sequence += 1
  return {
    id: `artifact-${sequence}`,
    project_id: 'project-a',
    task_run_id: null,
    kind: 'report',
    kind_deprecated: false,
    deprecation_message: null,
    category: 'deliverable',
    uri: `reports/report-${sequence}.md`,
    sha256: 'a'.repeat(64),
    size: 100,
    created_at: `2026-08-${String(sequence).padStart(2, '0')}T00:00:00Z`,
    metadata: {},
    ...overrides,
  }
}

function preview(
  target: ProjectArtifact,
  overrides: Partial<ArtifactPreview> = {},
): ArtifactPreview {
  return {
    artifact_id: target.id,
    project_id: target.project_id,
    task_run_id: target.task_run_id,
    kind: target.kind,
    kind_deprecated: target.kind_deprecated,
    deprecation_message: target.deprecation_message,
    content: '# preview',
    size: target.size,
    bytes_read: 9,
    truncated: true,
    sha256: target.sha256,
    ...overrides,
  }
}

function mountPage() {
  const wrapper = mount(ArtifactsPage, { attachTo: document.body })
  wrappers.push(wrapper)
  return wrapper
}

function buttonByText(text: string): HTMLButtonElement {
  const button = [...document.body.querySelectorAll<HTMLButtonElement>('button')].find(
    (candidate) => candidate.textContent?.trim() === text,
  )
  if (!button) throw new Error(`找不到按钮：${text}`)
  return button
}

function deferred<T>() {
  let resolve!: (value: T | PromiseLike<T>) => void
  let reject!: (reason?: unknown) => void
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise
    reject = rejectPromise
  })
  return { promise, resolve, reject }
}

beforeEach(() => {
  sequence = 0
  HTMLElement.prototype.hasPointerCapture = () => false
  HTMLElement.prototype.setPointerCapture = () => undefined
  HTMLElement.prototype.releasePointerCapture = () => undefined
  workspaceStore.currentProjectId = 'project-a'
  uiStore.globalRefreshKey = 0
  route.params.id = 'project-a'
  route.query = {}
  queryState.data.value = []
  queryState.error.value = null
  queryState.isError.value = false
  queryState.isFetching.value = false
  queryState.isLoading.value = false
  for (const mock of Object.values(mocks)) mock.mockReset()
  mocks.listProjectArtifacts.mockResolvedValue([])
  mocks.refetch.mockResolvedValue(undefined)
  mocks.replace.mockResolvedValue(undefined)
  mocks.push.mockResolvedValue(undefined)
  mocks.useQuery.mockReturnValue({ ...queryState, refetch: mocks.refetch })
  mocks.fetchArtifactPreview.mockImplementation(async (_projectId, _artifactId) => ({
    artifact_id: _artifactId,
    project_id: _projectId,
    task_run_id: null,
    kind: 'file',
    kind_deprecated: false,
    deprecation_message: null,
    content: 'head',
    size: 4,
    bytes_read: 4,
    truncated: false,
    sha256: '',
  }))
  mocks.fetchArtifactTailPreview.mockImplementation(async (projectId, artifactId) => ({
    artifact_id: artifactId,
    project_id: projectId,
    task_run_id: null,
    kind: 'file',
    kind_deprecated: false,
    deprecation_message: null,
    content: 'tail',
    size: 4,
    bytes_read: 4,
    truncated: false,
    sha256: '',
  }))
})

afterEach(() => {
  wrappers.splice(0).forEach((wrapper) => wrapper.unmount())
  document.body.innerHTML = ''
  vi.unstubAllGlobals()
})

describe('ArtifactsPage', () => {
  it('uses the project-scoped typed query, accessible table, explicit actions, and guarded refresh', async () => {
    const item = artifact({ id: 'artifact-a', task_run_id: 'run-123456789' })
    queryState.data.value = [item]
    const wrapper = mountPage()
    const options = mocks.useQuery.mock.calls[0]?.[0] as {
      queryKey: [string, { value: string | null }]
      queryFn: () => Promise<ProjectArtifact[]>
      enabled: { value: boolean }
    }

    expect(options.queryKey[0]).toBe('workspaceArtifacts')
    expect(options.queryKey[1].value).toBe('project-a')
    expect(options.enabled.value).toBe(true)
    await options.queryFn()
    expect(mocks.listProjectArtifacts).toHaveBeenCalledWith('project-a')
    expect(wrapper.get('caption').text()).toBe('项目产物列表')
    expect(wrapper.findAll('th').every((heading) => heading.attributes('scope') === 'col')).toBe(
      true,
    )
    expect(wrapper.find('tbody tr').attributes('role')).toBeUndefined()

    buttonByText('#run-1234').click()
    expect(mocks.push).toHaveBeenCalledWith({
      name: 'WorkspaceRuns',
      params: { id: 'project-a' },
      query: { runId: 'run-123456789' },
    })

    uiStore.globalRefreshKey += 1
    await flushPromises()
    expect(mocks.refetch).toHaveBeenCalledOnce()
    workspaceStore.currentProjectId = null
    await flushPromises()
    uiStore.globalRefreshKey += 1
    await flushPromises()
    expect(mocks.refetch).toHaveBeenCalledOnce()
  })

  it('distinguishes initial loading, initial error, project empty, filter empty, and stale errors', async () => {
    queryState.data.value = undefined
    queryState.isLoading.value = true
    const wrapper = mountPage()
    expect(wrapper.get('[aria-label="正在加载项目产物"]')).toBeDefined()

    queryState.isLoading.value = false
    queryState.error.value = new Error('网络不可用')
    queryState.isError.value = true
    await flushPromises()
    expect(wrapper.text()).toContain('产物加载失败')
    expect(wrapper.text()).toContain('网络不可用')

    queryState.isError.value = false
    queryState.error.value = null
    queryState.data.value = []
    await flushPromises()
    expect(wrapper.text()).toContain('暂无项目产物')

    queryState.data.value = [artifact({ kind: 'log', category: 'evidence' })]
    await flushPromises()
    expect(wrapper.text()).toContain('没有符合当前筛选条件的产物')

    queryState.isError.value = true
    queryState.error.value = new Error('刷新失败')
    queryState.isFetching.value = true
    queryState.data.value = [artifact({ uri: 'reports/visible.md' })]
    await flushPromises()
    expect(wrapper.text()).toContain('产物同步失败')
    expect(wrapper.text()).toContain('visible.md')
    expect(wrapper.text()).toContain('同步中…')
  })

  it('filters decoded filenames locally while preserving the selected preview and route', async () => {
    const selected = artifact({
      id: 'selected',
      kind: 'report',
      uri: 'reports/Final%20Report.MD',
      task_run_id: 'run-selected',
    })
    const notes = artifact({
      id: 'notes',
      kind: 'file',
      uri: 'runs/final-report/notes.txt',
    })
    const diagnostic = artifact({
      id: 'diagnostic',
      category: 'evidence',
      kind: 'log',
      uri: 'logs/final-report.log',
    })
    route.query = { artifactId: selected.id, keep: 'yes' }
    queryState.data.value = [selected, notes, diagnostic]
    mocks.fetchArtifactTailPreview.mockResolvedValue(
      preview(selected, { content: 'selected preview', truncated: false }),
    )
    const wrapper = mountPage()
    await flushPromises()

    const label = wrapper
      .findAll('label')
      .find((candidate) => candidate.text().includes('搜索文件名'))
    expect(label).toBeDefined()
    const inputId = label?.attributes('for')
    expect(inputId).toBeTruthy()
    const input = wrapper.get<HTMLInputElement>(`#${inputId}`)
    expect(input.attributes('placeholder')).toBe('按文件名筛选…')
    expect(wrapper.findAll('.artifact-file-name').map((node) => node.text())).toEqual([
      'notes.txt',
      'Final Report.MD',
    ])
    expect(wrapper.text()).toContain('selected preview')

    await input.setValue(' final report ')
    expect(wrapper.findAll('.artifact-file-name').map((node) => node.text())).toEqual([
      'Final Report.MD',
    ])
    expect(mocks.listProjectArtifacts).not.toHaveBeenCalled()
    expect(mocks.refetch).not.toHaveBeenCalled()

    await input.setValue('notes')
    expect(wrapper.findAll('.artifact-file-name').map((node) => node.text())).toEqual(['notes.txt'])
    const kindSelect = wrapper.findComponent({ name: 'UiSelect' })
    kindSelect.vm.$emit('update:modelValue', 'report')
    await flushPromises()
    expect(wrapper.text()).toContain('没有符合当前筛选条件的产物')
    kindSelect.vm.$emit('update:modelValue', 'all')
    await flushPromises()
    expect(wrapper.findAll('.artifact-file-name').map((node) => node.text())).toEqual(['notes.txt'])
    expect(wrapper.text()).toContain('selected preview')
    expect(route.query).toEqual({ artifactId: 'selected', keep: 'yes' })
    expect(wrapper.find('tbody tr').attributes('aria-current')).toBeUndefined()

    await input.setValue('runs')
    expect(wrapper.text()).toContain('没有符合当前筛选条件的产物')
    expect(wrapper.text()).toContain('文件名搜索条件')

    await input.setValue('')
    const restoredRows = wrapper.findAll('tbody tr')
    expect(restoredRows).toHaveLength(2)
    expect(restoredRows[1]?.attributes('aria-current')).toBe('true')

    const tabs = document.body.querySelectorAll<HTMLButtonElement>('[role="tab"]')
    tabs[1]?.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, button: 0 }))
    await input.setValue('FINAL-REPORT')
    await flushPromises()
    expect(wrapper.findAll('.artifact-file-name').map((node) => node.text())).toEqual([
      'final-report.log',
    ])

    workspaceStore.currentProjectId = 'project-b'
    route.params.id = 'project-b'
    queryState.data.value = [
      artifact({ id: 'project-b-item', project_id: 'project-b', uri: 'project-b.txt' }),
    ]
    await flushPromises()
    expect(input.element.value).toBe('')
  })

  it('keeps newest-first rows, switches view, and exposes historical kinds only when present', async () => {
    queryState.data.value = [
      artifact({ id: 'old', uri: 'old.md', created_at: '2026-08-01T00:00:00Z' }),
      artifact({
        id: 'capture',
        kind: 'pcap',
        uri: 'capture.pcap',
        created_at: '2026-08-20T00:00:00Z',
      }),
      artifact({ id: 'log', kind: 'log', category: 'evidence', uri: 'worker.log' }),
    ]
    const wrapper = mountPage()

    expect(wrapper.findAll('.artifact-file-name').map((node) => node.text())).toEqual([
      'capture.pcap',
      'old.md',
    ])
    const tabs = document.body.querySelectorAll<HTMLButtonElement>('[role="tab"]')
    tabs[1]?.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, button: 0 }))
    await flushPromises()
    expect(wrapper.text()).toContain('worker.log')
    expect(wrapper.text()).not.toContain('capture.pcap')
  })

  it('reconciles evidence deep links, query removal, and missing artifact IDs', async () => {
    const diagnostic = artifact({
      id: 'diagnostic-a',
      category: 'evidence',
      kind: 'log',
      uri: 'worker.log',
    })
    route.query = { artifactId: diagnostic.id, keep: 'yes' }
    queryState.data.value = [diagnostic]
    const wrapper = mountPage()
    await flushPromises()

    expect(wrapper.text()).toContain('worker.log')
    expect(document.body.querySelectorAll('[role="tab"]')[1]?.getAttribute('aria-selected')).toBe(
      'true',
    )
    expect(mocks.fetchArtifactTailPreview).toHaveBeenCalledWith('project-a', 'diagnostic-a')
    expect(wrapper.get('tbody tr').attributes('aria-current')).toBe('true')

    route.query = { keep: 'yes' }
    await flushPromises()
    expect(wrapper.text()).toContain('选择一个文件开始审阅')

    route.query = { artifactId: 'missing', keep: 'yes' }
    await flushPromises()
    expect(mocks.replace).toHaveBeenLastCalledWith({
      name: 'WorkspaceArtifacts',
      params: { id: 'project-a' },
      query: { keep: 'yes' },
    })
    expect(wrapper.text()).toContain('指定产物不存在或已删除')
  })

  it('waits for an active background refresh before rejecting a cached-list deep link', async () => {
    const cached = artifact({ id: 'cached', uri: 'cached.md' })
    const fresh = artifact({ id: 'fresh', uri: 'fresh.md' })
    route.query = { artifactId: fresh.id, keep: 'yes' }
    queryState.data.value = [cached]
    queryState.isFetching.value = true
    const wrapper = mountPage()
    await flushPromises()

    expect(mocks.replace).not.toHaveBeenCalled()
    expect(wrapper.text()).toContain('选择一个文件开始审阅')

    queryState.data.value = [cached, fresh]
    queryState.isFetching.value = false
    await flushPromises()

    expect(wrapper.text()).toContain('fresh.md')
    expect(wrapper.text()).not.toContain('指定产物不存在或已删除')
    expect(mocks.fetchArtifactTailPreview).toHaveBeenCalledWith('project-a', 'fresh')
  })

  it('renders only report Markdown while preserving raw Markdown and JSON previews', async () => {
    const reportMarkdown = artifact({
      id: 'report-markdown',
      kind: 'report',
      uri: 'reports/a194-vulnerability-report.MARKDOWN',
    })
    const reportSnapshot = artifact({
      id: 'report-snapshot',
      kind: 'report',
      uri: 'reports/a190-report-input-snapshot.json',
    })
    const genericMarkdown = artifact({
      id: 'generic-markdown',
      kind: 'file',
      uri: 'README.md',
    })
    queryState.data.value = [reportMarkdown, reportSnapshot, genericMarkdown]
    mocks.fetchArtifactTailPreview.mockImplementation(async (_projectId, artifactId) => {
      const target = [reportMarkdown, reportSnapshot, genericMarkdown].find(
        (candidate) => candidate.id === artifactId,
      )
      if (!target) throw new Error('artifact missing')
      const content =
        artifactId === reportMarkdown.id
          ? '# 安全报告\n\n<script>alert(1)</script>'
          : artifactId === reportSnapshot.id
            ? '{"packet": true}'
            : '# README source'
      return preview(target, { content, truncated: false })
    })
    route.query = { artifactId: reportMarkdown.id }
    const wrapper = mountPage()
    await flushPromises()

    expect(wrapper.get('.artifact-rendered-report h1').text()).toBe('安全报告')
    expect(wrapper.find('.artifact-rendered-report script').exists()).toBe(false)
    expect(wrapper.get('.artifact-rendered-report').text()).toContain('<script>alert(1)</script>')
    expect(wrapper.find('[data-testid="markdown"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="monaco"]').exists()).toBe(false)

    route.query = { artifactId: reportSnapshot.id }
    await flushPromises()
    expect(wrapper.get('[data-testid="monaco"]').text()).toBe('{"packet": true}')
    expect(wrapper.find('.artifact-rendered-report').exists()).toBe(false)

    route.query = { artifactId: genericMarkdown.id }
    await flushPromises()
    expect(wrapper.get('[data-testid="markdown"]').text()).toBe('# README source')
    expect(wrapper.find('.artifact-rendered-report').exists()).toBe(false)
  })

  it('uses tail then head windows with accurate truncation wording and encoded downloads', async () => {
    const item = artifact({
      id: 'artifact / one',
      project_id: 'project / one',
      uri: '报告 final.md',
    })
    workspaceStore.currentProjectId = 'project / one'
    route.params.id = 'project / one'
    route.query = { artifactId: item.id }
    queryState.data.value = [item]
    mocks.fetchArtifactTailPreview.mockResolvedValue(preview(item))
    mocks.fetchArtifactPreview.mockResolvedValue(preview(item, { content: 'head' }))
    const wrapper = mountPage()
    await flushPromises()

    expect(wrapper.text()).toContain('当前显示文件尾部窗口')
    expect(wrapper.text()).toContain('下载原文件可获取完整服务器产物')
    expect(wrapper.text()).not.toContain('加载完整内容')
    expect(wrapper.get<HTMLAnchorElement>('a[download]').attributes('href')).toBe(
      '/projects/project%20%2F%20one/artifacts/artifact%20%2F%20one/content',
    )

    buttonByText('查看文件开头').click()
    await flushPromises()
    expect(mocks.fetchArtifactPreview).toHaveBeenCalledWith('project / one', 'artifact / one')
    expect(wrapper.text()).toContain('这不是完整文件')
    expect(wrapper.text()).toContain('查看文件尾部')
  })

  it('keeps the loaded preview when the selected artifact action is clicked again', async () => {
    const item = artifact({ id: 'selected', uri: 'selected.txt', kind: 'file' })
    queryState.data.value = [item]
    route.query = { artifactId: item.id }
    mocks.fetchArtifactTailPreview.mockResolvedValue(
      preview(item, { content: 'loaded content', truncated: false }),
    )
    const wrapper = mountPage()
    await flushPromises()

    expect(wrapper.text()).toContain('loaded content')
    expect(mocks.fetchArtifactTailPreview).toHaveBeenCalledOnce()

    buttonByText('已打开').click()
    await flushPromises()

    expect(wrapper.text()).toContain('loaded content')
    expect(mocks.fetchArtifactTailPreview).toHaveBeenCalledOnce()
    expect(mocks.replace).not.toHaveBeenCalled()
  })

  it('ignores stale preview completion and failure across artifact and project visits', async () => {
    const first = artifact({ id: 'shared', uri: 'first.txt', kind: 'file' })
    const second = artifact({ id: 'second', uri: 'second.txt', kind: 'file' })
    const firstRequest = deferred<ArtifactPreview>()
    const secondRequest = deferred<ArtifactPreview>()
    mocks.fetchArtifactTailPreview
      .mockReturnValueOnce(firstRequest.promise)
      .mockReturnValueOnce(secondRequest.promise)
    queryState.data.value = [first, second]
    const wrapper = mountPage()

    document.body.querySelector<HTMLButtonElement>('[aria-label="预览产物 first.txt"]')?.click()
    await flushPromises()
    document.body.querySelector<HTMLButtonElement>('[aria-label="预览产物 second.txt"]')?.click()
    await flushPromises()
    secondRequest.resolve(preview(second, { content: 'second content', truncated: false }))
    await flushPromises()
    firstRequest.reject(new Error('旧请求失败'))
    await flushPromises()

    expect(wrapper.text()).toContain('second content')
    expect(wrapper.text()).not.toContain('旧请求失败')
    expect(mocks.showToast).not.toHaveBeenCalled()

    workspaceStore.currentProjectId = 'project-b'
    queryState.data.value = [
      artifact({ id: 'shared', project_id: 'project-b', uri: 'project-b.txt' }),
    ]
    await flushPromises()
    workspaceStore.currentProjectId = 'project-a'
    queryState.data.value = [first]
    await flushPromises()
    expect(wrapper.text()).toContain('选择一个文件开始审阅')
  })

  it('renders screenshot fallback and download-only binary without requesting text previews', async () => {
    const screenshot = artifact({ id: 'shot', kind: 'screenshot', uri: 'screen.png' })
    const capture = artifact({ id: 'capture', kind: 'pcap', uri: 'capture.pcap' })
    queryState.data.value = [screenshot, capture]
    route.query = { artifactId: screenshot.id }
    const wrapper = mountPage()
    await flushPromises()

    const image = wrapper.get('img')
    expect(image.attributes('alt')).toBe('产物截图：screen.png')
    await image.trigger('error')
    expect(wrapper.text()).toContain('截图载入失败')
    expect(mocks.fetchArtifactTailPreview).not.toHaveBeenCalled()

    route.query = { artifactId: capture.id }
    await flushPromises()
    expect(wrapper.text()).toContain('该类型不支持在线文本预览')
    expect(mocks.fetchArtifactTailPreview).not.toHaveBeenCalled()
  })

  it('reports clipboard success only after resolution and handles rejection', async () => {
    const item = artifact({ id: 'copy', uri: 'copy.md' })
    queryState.data.value = [item]
    route.query = { artifactId: item.id }
    const writeText = vi
      .fn()
      .mockResolvedValueOnce(undefined)
      .mockRejectedValueOnce(new Error('denied'))
    vi.stubGlobal('navigator', { clipboard: { writeText } })
    mountPage()
    await flushPromises()

    buttonByText('复制 HASH').click()
    await flushPromises()
    expect(mocks.showToast).toHaveBeenLastCalledWith('SHA256 HASH 值已复制到剪切板', 'success')

    buttonByText('复制 HASH').click()
    await flushPromises()
    expect(mocks.showToast).toHaveBeenLastCalledWith('复制 SHA256 HASH 失败', 'danger')
  })
})
