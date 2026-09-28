import { flushPromises, mount } from '@vue/test-utils'
import { ref } from 'vue'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { VueWrapper } from '@vue/test-utils'
import type { Vulnerability } from '@/api/client'
import VulnerabilitiesPage from './VulnerabilitiesPage.vue'

const mocks = vi.hoisted(() => ({
  createVulnerability: vi.fn(),
  invalidateQueries: vi.fn(),
  listVulnerabilities: vi.fn(),
  refetch: vi.fn(),
  route: undefined as unknown as { params: { id: string } },
  showToast: vi.fn(),
  useQuery: vi.fn(),
  workspace: {
    currentProjectId: 'project-a',
    projectDetail: null as unknown,
  },
}))

vi.mock('@/api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/client')>()
  return {
    ...actual,
    createVulnerability: mocks.createVulnerability,
    listVulnerabilities: mocks.listVulnerabilities,
  }
})

vi.mock('@tanstack/vue-query', () => ({
  useQuery: mocks.useQuery,
  useQueryClient: () => ({ invalidateQueries: mocks.invalidateQueries }),
}))

vi.mock('vue-router', async () => {
  const { reactive } = await import('vue')
  mocks.route = reactive({ params: { id: 'project-a' } })
  return { useRoute: () => mocks.route }
})

vi.mock('@/stores/ui', () => ({
  useUIStore: () => ({ showToast: mocks.showToast }),
}))

vi.mock('@/stores/workspace', () => ({
  useWorkspaceStore: () => mocks.workspace,
}))

vi.mock('@/features/reports/ProjectReportDialog.vue', () => ({
  default: {
    name: 'ProjectReportDialog',
    props: ['open', 'projectId', 'projectStatus', 'capability'],
    emits: ['update:open', 'closeAutoFocus'],
    template: '<div v-if="open" data-testid="project-report-dialog">报告 Dialog</div>',
  },
}))

vi.mock('./VulnerabilityDrawer.vue', () => ({
  default: {
    name: 'VulnerabilityDrawer',
    props: ['projectId', 'vulnId', 'open'],
    emits: ['close', 'select-vulnerability', 'status-changed'],
    template:
      '<div data-testid="vulnerability-drawer"><button type="button" data-testid="select-candidate" @click="$emit(\'select-vulnerability\', \'vuln-filtered\')">打开候选</button></div>',
  },
}))

const vulnerability: Vulnerability = {
  id: 'vuln-a',
  project_id: 'project-a',
  title: 'Authorization: Bearer full-token <script>alert(1)</script>',
  severity: 'high',
  vuln_type: 'IDOR token=visible',
  target: 'GET /users/2?password=clear-text',
  confidence: 0.9,
  status: 'new',
  source_fact_id: null,
  packet_artifact_id: null,
  remediation: '',
  repro_request: '',
  repro_response: '',
  repro_steps: '',
  discovered_by_run_id: null,
  discovered_by_worker: null,
  slot_key: null,
  fingerprint: null,
  identity: {},
  created_at: '2026-08-20T10:00:00Z',
  updated_at: '2026-08-20T10:00:00Z',
}

const wrappers: VueWrapper[] = []

function mountPage() {
  const wrapper = mount(VulnerabilitiesPage, { attachTo: document.body })
  wrappers.push(wrapper)
  return wrapper
}

function setQueryState(options: {
  data?: unknown
  error?: Error | null
  isError?: boolean
  isFetching?: boolean
  isLoading?: boolean
}) {
  mocks.useQuery.mockReturnValue({
    data: ref(options.data ?? null),
    error: ref(options.error ?? null),
    isError: ref(options.isError ?? false),
    isFetching: ref(options.isFetching ?? false),
    isLoading: ref(options.isLoading ?? false),
    refetch: mocks.refetch,
  })
}

function buttonByText(text: string) {
  const button = [...document.body.querySelectorAll<HTMLButtonElement>('button')].find(
    (candidate) => candidate.textContent?.trim() === text,
  )
  if (!button) throw new Error(`找不到按钮：${text}`)
  return button
}

function controlByLabel<T extends HTMLElement>(labelText: string, last = false) {
  const labels = [...document.body.querySelectorAll<HTMLLabelElement>('label')].filter(
    (candidate) => candidate.textContent?.includes(labelText),
  )
  const label = last ? labels.at(-1) : labels[0]
  const control = label?.htmlFor ? document.getElementById(label.htmlFor) : null
  if (!control) throw new Error(`找不到控件：${labelText}`)
  return control as T
}

function setValue(control: HTMLInputElement | HTMLTextAreaElement, value: string) {
  control.value = value
  control.dispatchEvent(new Event('input', { bubbles: true }))
}

function submitCreateForm() {
  document
    .querySelector<HTMLFormElement>('#create-vulnerability-form')
    ?.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }))
}

function pressEscape() {
  document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))
}

function deferred<T>() {
  let resolve!: (value: T | PromiseLike<T>) => void
  let reject!: (reason?: unknown) => void
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise
    reject = rejectPromise
  })
  return { promise, reject, resolve }
}

beforeEach(() => {
  mocks.route.params.id = 'project-a'
  mocks.workspace.currentProjectId = 'project-a'
  mocks.workspace.projectDetail = {
    project: {
      id: 'project-a',
      status: 'completed',
      report_capability: {
        supported: true,
        policy_version: 1,
        template_profile: 'assessment-report-packet-v1',
        reason: null,
      },
    },
  }
  HTMLElement.prototype.hasPointerCapture = () => false
  HTMLElement.prototype.setPointerCapture = () => undefined
  HTMLElement.prototype.releasePointerCapture = () => undefined
  mocks.createVulnerability.mockReset().mockResolvedValue({ id: 'vuln-a' })
  mocks.invalidateQueries.mockReset().mockResolvedValue(undefined)
  mocks.listVulnerabilities.mockReset().mockResolvedValue([])
  mocks.refetch.mockReset().mockResolvedValue(undefined)
  mocks.showToast.mockReset()
  mocks.useQuery.mockReset()
  setQueryState({})
})

afterEach(() => {
  wrappers.splice(0).forEach((wrapper) => wrapper.unmount())
  document.body.innerHTML = ''
})

describe('VulnerabilitiesPage migration', () => {
  it('adds the exact report action, passes route-matching workspace metadata, and closes on navigation', async () => {
    const wrapper = mountPage()

    buttonByText('生成报告').click()
    await flushPromises()

    const dialog = wrapper.getComponent({ name: 'ProjectReportDialog' })
    expect(dialog.props()).toEqual(
      expect.objectContaining({
        open: true,
        projectId: 'project-a',
        projectStatus: 'completed',
        capability: expect.objectContaining({ supported: true }),
      }),
    )
    expect(wrapper.find('[data-testid="project-report-dialog"]').exists()).toBe(true)

    mocks.route.params.id = 'project-b'
    await flushPromises()
    expect(wrapper.findComponent({ name: 'ProjectReportDialog' }).exists()).toBe(false)
    expect(buttonByText('生成报告').disabled).toBe(true)
  })

  it('uses the route-derived request snapshot, server filters, polling, and AbortSignal', async () => {
    mocks.listVulnerabilities.mockResolvedValueOnce([vulnerability])
    mountPage()

    const queryOptions = mocks.useQuery.mock.calls[0]?.[0] as {
      queryKey: { value: readonly [string, string, string, string, string, number] }
      queryFn: (context: {
        queryKey: readonly [string, string, string, string, string, number]
        signal: AbortSignal
      }) => Promise<unknown>
      refetchInterval: number
    }
    const controller = new AbortController()

    expect(queryOptions.queryKey.value).toEqual([
      'vulnerabilities',
      'project',
      'project-a',
      '',
      '',
      0,
    ])
    expect(queryOptions.refetchInterval).toBe(10_000)
    await expect(
      queryOptions.queryFn({ queryKey: queryOptions.queryKey.value, signal: controller.signal }),
    ).resolves.toEqual({
      projectId: 'project-a',
      severity: '',
      status: '',
      visitGeneration: 0,
      items: [vulnerability],
    })
    expect(mocks.listVulnerabilities).toHaveBeenCalledWith(
      'project-a',
      undefined,
      undefined,
      controller.signal,
    )
  })

  it('isolates A→B→A visits and clears the local Drawer selection', async () => {
    setQueryState({
      data: {
        projectId: 'project-a',
        severity: '',
        status: '',
        visitGeneration: 0,
        items: [vulnerability],
      },
    })
    const wrapper = mountPage()
    const queryOptions = mocks.useQuery.mock.calls[0]?.[0] as {
      queryKey: { value: readonly unknown[] }
    }

    document.body
      .querySelector<HTMLButtonElement>(`button[aria-label="查看详情：${vulnerability.title}"]`)
      ?.click()
    await flushPromises()
    expect(wrapper.getComponent({ name: 'VulnerabilityDrawer' }).props('open')).toBe(true)

    mocks.route.params.id = 'project-b'
    await flushPromises()
    expect(queryOptions.queryKey.value).toEqual([
      'vulnerabilities',
      'project',
      'project-b',
      '',
      '',
      1,
    ])
    expect(wrapper.getComponent({ name: 'VulnerabilityDrawer' }).props()).toEqual(
      expect.objectContaining({ projectId: 'project-b', vulnId: null, open: false }),
    )

    mocks.route.params.id = 'project-a'
    await flushPromises()
    expect(queryOptions.queryKey.value).toEqual([
      'vulnerabilities',
      'project',
      'project-a',
      '',
      '',
      2,
    ])
    expect(document.body.textContent).not.toContain(vulnerability.title)
  })

  it('renders canonical filters and an accessible table without truncating sensitive text', async () => {
    setQueryState({
      data: {
        projectId: 'project-a',
        severity: '',
        status: '',
        visitGeneration: 0,
        items: [vulnerability],
      },
    })
    const wrapper = mountPage()
    await flushPromises()

    expect(document.body.textContent).toContain('全部严重度')
    expect(document.body.textContent).toContain('全部状态')
    expect(document.body.textContent).toContain(vulnerability.title)
    expect(document.body.textContent).toContain(vulnerability.vuln_type)
    expect(document.body.textContent).not.toContain(vulnerability.target)
    expect(document.body.querySelector('script')).toBeNull()
    expect(document.body.querySelector('caption')?.textContent).toContain('项目漏洞列表')
    expect(document.body.querySelectorAll('th[scope="col"]')).toHaveLength(5)
    expect(
      Array.from(document.body.querySelectorAll('th[scope="col"]')).map((heading) =>
        heading.textContent?.trim(),
      ),
    ).not.toContain('目标')
    expect(document.body.querySelector('[role="region"][tabindex="0"]')).not.toBeNull()

    const action = document.body.querySelector<HTMLButtonElement>(
      `button[aria-label="查看详情：${vulnerability.title}"]`,
    )
    expect(action?.textContent?.trim()).toBe('详情')
    action?.click()
    await flushPromises()
    expect(wrapper.get('tbody tr').attributes('aria-current')).toBe('true')
    expect(wrapper.getComponent({ name: 'VulnerabilityDrawer' }).props()).toEqual(
      expect.objectContaining({ projectId: 'project-a', vulnId: 'vuln-a', open: true }),
    )
  })

  it('opens a duplicate candidate even when it is absent from the filtered table', async () => {
    setQueryState({
      data: {
        projectId: 'project-a',
        severity: '',
        status: '',
        visitGeneration: 0,
        items: [vulnerability],
      },
    })
    const wrapper = mountPage()

    document.body
      .querySelector<HTMLButtonElement>(`button[aria-label="查看详情：${vulnerability.title}"]`)
      ?.click()
    await flushPromises()
    await wrapper.get('[data-testid="select-candidate"]').trigger('click')
    await flushPromises()

    expect(wrapper.getComponent({ name: 'VulnerabilityDrawer' }).props()).toEqual(
      expect.objectContaining({ projectId: 'project-a', vulnId: 'vuln-filtered', open: true }),
    )
    expect(wrapper.get('tbody tr').attributes('aria-current')).toBeUndefined()
  })

  it('distinguishes initial loading, initial failure, and filtered empty states', async () => {
    setQueryState({ isLoading: true })
    const loading = mountPage()
    expect(document.body.querySelector('[aria-label="正在加载漏洞"]')).not.toBeNull()
    loading.unmount()
    document.body.innerHTML = ''

    setQueryState({ error: new Error('列表请求失败'), isError: true })
    const failed = mountPage()
    expect(document.body.textContent).toContain('漏洞加载失败')
    expect(document.body.textContent).toContain('列表请求失败')
    buttonByText('重试').click()
    expect(mocks.refetch).toHaveBeenCalled()
    failed.unmount()
    document.body.innerHTML = ''

    setQueryState({
      data: {
        projectId: 'project-a',
        severity: '',
        status: '',
        visitGeneration: 0,
        items: [],
      },
    })
    mountPage()
    expect(document.body.textContent).toContain('当前项目暂无漏洞记录')
  })

  it('opens an accessible Dialog, focuses the title, and restores trigger focus', async () => {
    mountPage()
    const trigger = buttonByText('＋ 录入漏洞')

    trigger.click()
    await flushPromises()

    const dialog = document.body.querySelector<HTMLElement>('[role="dialog"]')
    const titleInput = controlByLabel<HTMLInputElement>('漏洞名称')
    expect(dialog).not.toBeNull()
    expect(dialog?.getAttribute('aria-labelledby')).toBeTruthy()
    expect(dialog?.getAttribute('aria-describedby')).toBeTruthy()
    expect(dialog?.textContent).toContain('人工录入项目漏洞、影响范围、修复建议与复现证据。')
    expect(document.activeElement).toBe(titleInput)

    buttonByText('取消').click()
    await flushPromises()
    expect(document.body.querySelector('[role="dialog"]')).toBeNull()
    expect(document.activeElement).toBe(trigger)

    trigger.click()
    await flushPromises()
    pressEscape()
    await flushPromises()
    expect(document.body.querySelector('[role="dialog"]')).toBeNull()
  })

  it('submits the complete default eight-field payload and preserves query invalidation', async () => {
    mountPage()
    buttonByText('＋ 录入漏洞').click()
    await flushPromises()

    setValue(controlByLabel<HTMLInputElement>('漏洞名称'), '登录接口 SQL 注入')
    submitCreateForm()
    await flushPromises()

    expect(mocks.createVulnerability).toHaveBeenCalledWith('project-a', {
      title: '登录接口 SQL 注入',
      severity: 'medium',
      vuln_type: '',
      target: '',
      remediation: '',
      confidence: 0.6,
      repro_request: '',
      repro_response: '',
    })
    expect(Object.keys(mocks.createVulnerability.mock.calls[0]?.[1] ?? {})).toEqual([
      'title',
      'severity',
      'vuln_type',
      'target',
      'remediation',
      'confidence',
      'repro_request',
      'repro_response',
    ])

    expect(mocks.invalidateQueries.mock.calls).toEqual([
      [{ queryKey: ['vulnerabilities', 'project', 'project-a'] }],
      [{ queryKey: ['vulnerabilities', 'badge', 'project-a'] }],
      [{ queryKey: ['vulnerabilities', 'all'] }],
      [{ queryKey: ['vulnerability-duplicate-candidates', 'project-a'] }],
    ])
    expect(mocks.showToast).toHaveBeenCalledWith('漏洞已录入', 'success')
    expect(document.body.querySelector('[role="dialog"]')).toBeNull()
  })

  it('preserves edited severity, confidence, and HTTP evidence fields', async () => {
    mountPage()
    buttonByText('＋ 录入漏洞').click()
    await flushPromises()

    setValue(controlByLabel<HTMLInputElement>('漏洞名称'), '水平越权')
    setValue(controlByLabel<HTMLInputElement>('类型'), 'IDOR')
    setValue(controlByLabel<HTMLInputElement>('影响目标'), 'GET /api/users/2')
    setValue(controlByLabel<HTMLTextAreaElement>('修复建议'), '校验资源所有者。')
    setValue(
      controlByLabel<HTMLTextAreaElement>('复现请求'),
      'GET /api/users/2 HTTP/1.1\nHost: example.test',
    )
    setValue(controlByLabel<HTMLTextAreaElement>('复现响应'), 'HTTP/1.1 200 OK\n\n{"id":2}')

    const confidence = controlByLabel<HTMLInputElement>('可信度')
    setValue(confidence, '0.9')

    const severity = controlByLabel<HTMLButtonElement>('严重度', true)
    severity.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true, button: 0 }))
    severity.dispatchEvent(new PointerEvent('pointerup', { bubbles: true, button: 0 }))
    severity.click()
    await flushPromises()
    const highOption = [...document.body.querySelectorAll<HTMLElement>('[role="option"]')].find(
      (option) => option.textContent?.includes('高危'),
    )
    if (!highOption) throw new Error('找不到高危严重度选项')
    highOption.dispatchEvent(new PointerEvent('pointerup', { bubbles: true, button: 0 }))
    highOption.click()
    await flushPromises()

    submitCreateForm()
    await flushPromises()

    expect(mocks.createVulnerability).toHaveBeenCalledWith('project-a', {
      title: '水平越权',
      severity: 'high',
      vuln_type: 'IDOR',
      target: 'GET /api/users/2',
      remediation: '校验资源所有者。',
      confidence: 0.9,
      repro_request: 'GET /api/users/2 HTTP/1.1\nHost: example.test',
      repro_response: 'HTTP/1.1 200 OK\n\n{"id":2}',
    })
  })

  it('blocks duplicate submission and all dismissal while pending', async () => {
    const pending = deferred<unknown>()
    mocks.createVulnerability.mockReturnValue(pending.promise)
    mountPage()
    buttonByText('＋ 录入漏洞').click()
    await flushPromises()

    setValue(controlByLabel<HTMLInputElement>('漏洞名称'), '并发创建保护')
    submitCreateForm()
    submitCreateForm()
    await flushPromises()

    expect(mocks.createVulnerability).toHaveBeenCalledTimes(1)
    expect(buttonByText('创建').getAttribute('aria-busy')).toBe('true')
    expect(buttonByText('取消').disabled).toBe(true)
    expect(document.body.querySelector('button[aria-label="关闭录入漏洞"]')).toBeNull()

    pressEscape()
    document.body.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true }))
    document.body.dispatchEvent(new PointerEvent('pointerup', { bubbles: true }))
    await flushPromises()
    expect(document.body.querySelector('[role="dialog"]')).not.toBeNull()

    pending.resolve({ id: 'vuln-a' })
    await flushPromises()
    expect(document.body.querySelector('[role="dialog"]')).toBeNull()
  })

  it('invalidates the captured project without closing or notifying a newer project Dialog', async () => {
    const pending = deferred<unknown>()
    mocks.createVulnerability.mockReturnValueOnce(pending.promise)
    mountPage()
    buttonByText('＋ 录入漏洞').click()
    await flushPromises()

    setValue(controlByLabel<HTMLInputElement>('漏洞名称'), '旧项目漏洞')
    submitCreateForm()
    await flushPromises()

    mocks.route.params.id = 'project-b'
    await flushPromises()
    expect(document.body.querySelector('[role="dialog"]')).toBeNull()

    buttonByText('＋ 录入漏洞').click()
    await flushPromises()
    const newTitle = controlByLabel<HTMLInputElement>('漏洞名称')
    setValue(newTitle, '新项目草稿')

    pending.resolve({ id: 'vuln-a' })
    await flushPromises()

    expect(mocks.createVulnerability).toHaveBeenCalledWith(
      'project-a',
      expect.objectContaining({ title: '旧项目漏洞' }),
    )
    expect(mocks.invalidateQueries.mock.calls).toEqual([
      [{ queryKey: ['vulnerabilities', 'project', 'project-a'] }],
      [{ queryKey: ['vulnerabilities', 'badge', 'project-a'] }],
      [{ queryKey: ['vulnerabilities', 'all'] }],
      [{ queryKey: ['vulnerability-duplicate-candidates', 'project-a'] }],
    ])
    expect(document.body.querySelector('[role="dialog"]')).not.toBeNull()
    expect(newTitle.value).toBe('新项目草稿')
    expect(mocks.showToast).not.toHaveBeenCalled()
  })

  it('retains the draft after failure and guards whitespace-only titles', async () => {
    mocks.createVulnerability.mockRejectedValueOnce(new Error('漏洞写入失败'))
    mountPage()
    buttonByText('＋ 录入漏洞').click()
    await flushPromises()

    const title = controlByLabel<HTMLInputElement>('漏洞名称')
    setValue(title, '  ')
    submitCreateForm()
    await flushPromises()
    expect(mocks.createVulnerability).not.toHaveBeenCalled()

    setValue(title, '保留失败输入')
    setValue(controlByLabel<HTMLTextAreaElement>('修复建议'), '保留这段建议')
    submitCreateForm()
    await flushPromises()

    expect(document.body.querySelector('[role="dialog"]')).not.toBeNull()
    expect(title.value).toBe('保留失败输入')
    expect(controlByLabel<HTMLTextAreaElement>('修复建议').value).toBe('保留这段建议')
    expect(buttonByText('创建').disabled).toBe(false)
    expect(mocks.showToast).toHaveBeenCalledWith('漏洞写入失败', 'danger')
  })
})
