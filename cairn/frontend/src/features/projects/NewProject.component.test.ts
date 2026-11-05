import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { ref, unref } from 'vue'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { api, ApiError } from '@/api/client'
import { onBeforeRouteLeave } from 'vue-router'
import NewProject from './NewProject.vue'
import TestEgressFields from '@/components/patterns/TestEgressFields.vue'
import CreationRequestRecovery from './CreationRequestRecovery.vue'
import type { ProjectSchedule } from '@/api/projectSchedules'

const aiAgentOrigin = 'AI Agent origin\n- tenant: test-tenant\n- side effects: reversible only'
const aiAgentGoal = 'AI Agent goal\n1. prompt injection\n2. tool authorization\n3. isolation'

const mocks = vi.hoisted(() => ({
  scenarios: [
    {
      key: 'secondary',
      label: '次要场景',
      is_default: false,
      origin_template: 'secondary origin',
      goal_template: 'secondary goal',
    },
    {
      key: 'default',
      label: '默认场景',
      is_default: true,
      origin_template: 'default origin\npublic environment context',
      goal_template: 'default goal',
    },
    {
      key: 'ai-agent-security',
      label: 'AI Agent 安全评估',
      is_default: false,
      origin_template: 'AI Agent origin\n- tenant: test-tenant\n- side effects: reversible only',
      goal_template: 'AI Agent goal\n1. prompt injection\n2. tool authorization\n3. isolation',
    },
  ],
  libraries: [{ key: 'library-a', name: '授权资料', builtin: false, file_count: 3 }],
  getLibraryManifest: vi.fn(),
  queryState: {} as Record<string, { loading?: boolean; fetching?: boolean; error?: boolean }>,
  queryData: {} as Record<string, { value: unknown }>,
  queryFlags: {} as Record<
    string,
    { isLoading: { value: boolean }; isFetching: { value: boolean } }
  >,
  createProject: vi.fn(),
  createResourceBatch: vi.fn(),
  getResourceConfig: vi.fn(),
  getResourceBatch: vi.fn(),
  uploadResourceFile: vi.fn(),
  cancelResourceBatch: vi.fn(),
  router: { push: vi.fn() },
  uiStore: { showToast: vi.fn() },
}))

vi.mock('@/api/client', async (importOriginal) => ({
  ApiError: (await importOriginal<typeof import('@/api/client')>()).ApiError,
  api: vi.fn(),
  cancelProjectResourceBatch: mocks.cancelResourceBatch,
  createProject: mocks.createProject,
  createProjectResourceBatch: mocks.createResourceBatch,
  deleteProjectResourceBatchMember: vi.fn(),
  getProjectResourceConfig: mocks.getResourceConfig,
  getProjectResourceBatch: mocks.getResourceBatch,
  getLibraryCatalogManifest: mocks.getLibraryManifest,
  importProjectResourceBatchZip: vi.fn(),
  listLibraryCatalog: vi.fn(),
  updateProjectResourceBatchMember: vi.fn(),
  uploadProjectResourceBatchFile: mocks.uploadResourceFile,
}))

vi.mock('@/stores/ui', () => ({
  useUIStore: () => mocks.uiStore,
}))

vi.mock('vue-router', async (importOriginal) => ({
  isNavigationFailure: (await importOriginal<typeof import('vue-router')>()).isNavigationFailure,
  onBeforeRouteLeave: vi.fn(),
  useRouter: () => mocks.router,
}))

vi.mock('@tanstack/vue-query', () => ({
  useQuery: ({ queryKey }: { queryKey: unknown }) => {
    const key = String((unref(queryKey) as unknown[])[0])
    const values: Record<string, unknown> = {
      'scenario-catalog': mocks.scenarios,
      'library-catalog': mocks.libraries,
    }
    const state = mocks.queryState[key] || {}
    const data = ref(values[key])
    mocks.queryData[key] = data
    const flags = {
      isLoading: ref(Boolean(state.loading)),
      isFetching: ref(Boolean(state.fetching ?? state.loading)),
    }
    mocks.queryFlags[key] = flags
    return {
      data,
      ...flags,
      isError: ref(Boolean(state.error)),
      refetch: vi.fn(),
    }
  },
}))

const wrappers: VueWrapper[] = []

async function mountPage(schedule?: ProjectSchedule, leaveSafetyOn = false) {
  const wrapper = mount(NewProject, {
    props: { open: true, returnFocus: null, schedule: schedule ? { library_selection_explicit: true, ...schedule } : undefined },
    attachTo: document.body,
    global: {
      mocks: { $router: mocks.router },
      stubs: { RouterLink: { template: '<a><slot /></a>' } },
    },
  })
  wrappers.push(wrapper)
  await flushPromises()
  if (!schedule && !leaveSafetyOn) {
    document.querySelector<HTMLInputElement>('input[aria-label="安全限制"]')!.click()
    await flushPromises()
  }
  return wrapper
}

function buttonByText(text: string) {
  return [...document.body.querySelectorAll<HTMLButtonElement>('button')].find((button) =>
    button.textContent?.includes(text),
  )
}

async function setFieldValue(selector: string, value: string) {
  const field = document.querySelector<HTMLInputElement | HTMLTextAreaElement>(selector)!
  field.value = value
  field.dispatchEvent(new Event('input', { bubbles: true }))
  await flushPromises()
}

async function selectScheduleMode(mode: 'immediate' | 'once' | 'daily' | 'weekly') {
  const radio = document.querySelector<HTMLInputElement>(`.schedule-mode-options input[value="${mode}"]`)!
  radio.click()
  await flushPromises()
}

async function selectOption(selectId: string, optionText: string) {
  const trigger = document.querySelector<HTMLButtonElement>(`#${selectId}[role="combobox"]`)!
  trigger.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true, button: 0 }))
  trigger.dispatchEvent(new PointerEvent('pointerup', { bubbles: true, button: 0 }))
  trigger.click()
  await flushPromises()

  const option = [...document.body.querySelectorAll<HTMLElement>('[role="option"]')].find(
    (candidate) => candidate.textContent?.includes(optionText),
  )!
  option.dispatchEvent(new PointerEvent('pointerup', { bubbles: true, button: 0 }))
  option.click()
  await flushPromises()
}

beforeEach(() => {
  HTMLElement.prototype.hasPointerCapture = () => false
  HTMLElement.prototype.setPointerCapture = () => undefined
  HTMLElement.prototype.releasePointerCapture = () => undefined
  HTMLElement.prototype.scrollIntoView = () => undefined

  vi.mocked(api).mockReset()
  mocks.libraries = [{ key: 'library-a', name: '授权资料', builtin: false, file_count: 3 }]
  mocks.queryState = {}
  mocks.queryData = {}
  mocks.queryFlags = {}
  mocks.getLibraryManifest.mockReset().mockResolvedValue({
    library_key: 'library-a', library_name: '授权资料', file_count: 3, safety_path: null,
    files: ['guide.md', 'dir/one.md', 'dir/two.md'].map((path) => ({ path, size_bytes: 1, sha256: 'a'.repeat(64) })),
  })
  mocks.createProject.mockReset().mockResolvedValue({ project: { id: 'project-created' } })
  mocks.getResourceConfig.mockReset().mockResolvedValue({
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
  })
  mocks.createResourceBatch.mockReset().mockResolvedValue({
    id: 'batch-created',
    state: 'open',
    created_at: '2026-09-01T00:00:00Z',
    expires_at: '2026-09-02T00:00:00Z',
    resource_count: 0,
    total_bytes: 0,
    members: [],
  })
  mocks.uploadResourceFile
    .mockReset()
    .mockImplementation(async (_batchId: string, file: File, path: string) => ({
      id: 'member-1',
      kind: path.endsWith('.sh') ? 'script' : 'file',
      path,
      size_bytes: file.size,
      sha256: 'a'.repeat(64),
      source: 'file',
    }))
  mocks.cancelResourceBatch.mockReset().mockResolvedValue(undefined)
  mocks.getResourceBatch.mockReset()
  mocks.router.push.mockReset().mockResolvedValue(undefined)
  mocks.uiStore.showToast.mockReset()
})

afterEach(() => {
  wrappers.splice(0).forEach((wrapper) => wrapper.unmount())
  document.body.innerHTML = ''
  vi.restoreAllMocks()
  sessionStorage.clear()
})

describe('项目创建的当前与已保存资料选择', () => {
  function configureSafetyLibrary() {
    mocks.libraries.push({ key: 'knowledge_base', name: '根目录', builtin: true, file_count: 2 })
    const manifest = {
      library_key: 'knowledge_base', library_name: '根目录', file_count: 2,
      safety_path: '执行安全约束.md',
      files: ['执行安全约束.md', 'notes.md'].map((path) => ({ path, size_bytes: 1, sha256: 'a'.repeat(64) })),
    }
    mocks.getLibraryManifest.mockImplementation(async (key: string) => key === 'knowledge_base' ? manifest : {
      library_key: key, library_name: '授权资料', file_count: 0, safety_path: null, files: [],
    })
    return manifest
  }

  it('新草稿默认加入安全文件，开关、标签与树复选共用实际选择且只默认一次', async () => {
    configureSafetyLibrary()
    await mountPage(undefined, true)
    await setFieldValue('#np-title', '安全开关')
    const safety = document.querySelector<HTMLInputElement>('input[aria-label="安全限制"]')!
    expect(safety.checked).toBe(true)
    expect(buttonByText('创建并启动')?.disabled).toBe(false)
    document.querySelector<HTMLButtonElement>('button[aria-label="移除 knowledge_base 执行安全约束.md"]')!.click()
    await flushPromises()
    expect(safety.checked).toBe(false)
    buttonByText('从资料库选择')!.click()
    await flushPromises()
    const file = document.querySelector<HTMLInputElement>('input[aria-label="选择文件 knowledge_base 执行安全约束.md"]')!
    expect(file.checked).toBe(false)
    file.click()
    await flushPromises()
    expect(safety.checked).toBe(true)
    buttonByText('完成选择')!.click()
    await flushPromises()
    safety.click()
    await flushPromises()
    expect(document.querySelector('button[aria-label="移除 knowledge_base 执行安全约束.md"]')).toBeNull()
    mocks.queryData['library-catalog']!.value = [...mocks.libraries]
    await flushPromises()
    expect(safety.checked).toBe(false)
    safety.click()
    await flushPromises()
    buttonByText('创建并启动')!.click()
    await flushPromises()
    expect(mocks.createProject.mock.calls[0]![0].library_selections).toEqual([{
      library_key: 'knowledge_base', source: 'current', paths: ['执行安全约束.md'],
      expected_files: { '执行安全约束.md': 'a'.repeat(64) },
    }])
  })

  it('安全清单迟到不覆盖用户主动关闭，关闭期间仍可提交空选择', async () => {
    const manifest = configureSafetyLibrary()
    let resolve!: (value: unknown) => void
    mocks.getLibraryManifest.mockImplementationOnce(() => new Promise((done) => { resolve = done }))
    await mountPage(undefined, true)
    await setFieldValue('#np-title', '关闭迟到安全选择')
    const safety = document.querySelector<HTMLInputElement>('input[aria-label="安全限制"]')!
    expect(safety.checked).toBe(true)
    expect(buttonByText('创建并启动')?.disabled).toBe(true)
    safety.click()
    await flushPromises()
    resolve(manifest)
    await flushPromises()
    expect(safety.checked).toBe(false)
    expect(document.querySelector('button[aria-label="移除 knowledge_base 执行安全约束.md"]')).toBeNull()
    buttonByText('创建并启动')!.click()
    await flushPromises()
    expect(mocks.createProject.mock.calls[0]![0].library_selections).toEqual([])
  })

  it('计划只回显保存的安全文件，不自动读取或加入当前正文', async () => {
    const manifest = configureSafetyLibrary()
    const plan = {
      id: 'safety-plan', mode: 'daily', timezone: 'Asia/Shanghai', start_at: '2099-06-01T09:00:00+08:00',
      local_time: '09:00', weekdays: [], end_date: null, max_runs: null, version: 1, resources: [],
      library_bindings: [{ library_key: 'knowledge_base', library_name: '根目录', version: 3,
        digest_sha256: 'old-digest', file_count: 1, paths: ['执行安全约束.md'], selection_sha256: 'selection' }],
      project: { title: '安全计划', origin: '背景', goal: '目标', scenario: 'default', authorization_expires_at: null },
    } as unknown as ProjectSchedule
    vi.mocked(api).mockImplementation(async (method, path) => {
      if (method === 'GET' && path === '/project-schedules/safety-plan/library-manifests/knowledge_base') return {
        ...manifest, file_count: 1, files: [{ path: '执行安全约束.md', size_bytes: 1, sha256: 'b'.repeat(64) }],
      }
      throw new ApiError('受控拒绝', 422, {})
    })
    await mountPage(plan)
    expect(document.querySelector<HTMLInputElement>('input[aria-label="安全限制"]')!.checked).toBe(true)
    expect(mocks.getLibraryManifest).not.toHaveBeenCalled()
    buttonByText('保存未来配置')!.click()
    await flushPromises()
    expect(api).toHaveBeenCalledWith('PUT', '/project-schedules/safety-plan', expect.objectContaining({
      project: expect.objectContaining({ library_selections: [{
        library_key: 'knowledge_base', source: 'saved', paths: ['执行安全约束.md'],
      }] }),
    }))
  })

  it('整库移除文件变半选，同名跨库不合并并提交已接受的逐文件摘要', async () => {
    mocks.libraries.push({ key: 'library-b', name: '另一资料', builtin: false, file_count: 1 })
    mocks.getLibraryManifest.mockImplementation(async (key: string) => key === 'library-a' ? {
      library_key: key, library_name: '授权资料',
      file_count: 3, safety_path: null,
      files: ['guide.md', 'dir/one.md', 'dir/two.md'].map((path) => ({ path, size_bytes: 1, sha256: 'a'.repeat(64) })),
    } : {
      library_key: key, library_name: '另一资料',
      file_count: 1, safety_path: null,
      files: [{ path: 'guide.md', size_bytes: 1, sha256: 'a'.repeat(64) }],
    })
    await mountPage()
    await setFieldValue('#np-title', '版本与文件')
    buttonByText('从资料库选择')!.click()
    await flushPromises()
    document.querySelector<HTMLInputElement>('input[aria-label="选择资料库 授权资料 (library-a)"]')!.click()
    document.querySelector<HTMLInputElement>('input[aria-label="选择资料库 另一资料 (library-b)"]')!.click()
    await flushPromises()
    buttonByText('完成选择')!.click()
    await flushPromises()
    expect(document.body.textContent).toContain('授权资料 (library-a) · guide.md')
    expect(document.body.textContent).toContain('另一资料 (library-b) · guide.md')
    document.querySelector<HTMLButtonElement>('button[aria-label="移除 library-a dir/one.md"]')!.click()
    await flushPromises()
    buttonByText('从资料库选择')!.click()
    await flushPromises()
    const root = document.querySelector<HTMLInputElement>('input[aria-label="选择资料库 授权资料 (library-a)"]')!
    expect(root.indeterminate).toBe(true)
    await setFieldValue('input[aria-label="搜索资料库或文件路径"]', 'dir/two')
    expect(document.querySelector('input[aria-label="选择文件 library-a dir/two.md"]')).not.toBeNull()
    buttonByText('完成选择')!.click()
    await flushPromises()
    buttonByText('创建并启动')!.click()
    await flushPromises()
    expect(mocks.createProject.mock.calls[0]![0].library_selections).toEqual(expect.arrayContaining([
      { library_key: 'library-a', source: 'current', paths: ['guide.md', 'dir/two.md'], expected_files: { 'guide.md': 'a'.repeat(64), 'dir/two.md': 'a'.repeat(64) } },
      { library_key: 'library-b', source: 'current', paths: null, expected_files: { 'guide.md': 'a'.repeat(64) } },
    ]))
  })

  it('清单加载和错误不显示为已选，重试后可选择目录并移除最后一个文件', async () => {
    mocks.getLibraryManifest.mockRejectedValueOnce(new Error('清单不可用'))
    await mountPage()
    await setFieldValue('#np-title', '文件清单')
    buttonByText('从资料库选择')!.click()
    await flushPromises()
    expect(document.body.textContent).toContain('清单不可用')
    expect(document.querySelector('input[aria-label="选择资料库 授权资料 (library-a)"]')).toBeNull()
    buttonByText('重试')!.click()
    await flushPromises()
    document.querySelector<HTMLButtonElement>('button[aria-label="展开 授权资料"]')!.click()
    await flushPromises()
    document.querySelector<HTMLInputElement>('input[aria-label="选择目录 library-a dir"]')!.click()
    await flushPromises()
    const root = document.querySelector<HTMLInputElement>('input[aria-label="选择资料库 授权资料 (library-a)"]')!
    expect(root.indeterminate).toBe(true)
    buttonByText('完成选择')!.click()
    await flushPromises()
    document.querySelector<HTMLButtonElement>('button[aria-label="移除 library-a dir/one.md"]')!.click()
    document.querySelector<HTMLButtonElement>('button[aria-label="移除 library-a dir/two.md"]')!.click()
    await flushPromises()
    buttonByText('创建并启动')!.click()
    await flushPromises()
    expect(mocks.createProject.mock.calls[0]![0].library_selections).toEqual([])
  })

  it('历史计划未选择资料时保留缺省，只有明确停用后提交显式空选择', async () => {
    vi.mocked(api).mockRejectedValue(new ApiError('受控拒绝', 422, {}))
    const plan = {
      id: 'legacy', mode: 'daily', timezone: 'Asia/Shanghai', start_at: '2099-06-01T09:00:00+08:00',
      local_time: '09:00', weekdays: [], end_date: null, max_runs: null, version: 2, resources: [],
      library_bindings: [], library_selection_explicit: false,
      project: { title: '历史计划', origin: '背景', goal: '目标', scenario: 'default', authorization_expires_at: null },
    } as unknown as ProjectSchedule
    await mountPage(plan)
    expect(document.body.textContent).toContain('沿用历史场景默认资料')
    buttonByText('保存未来配置')!.click()
    await flushPromises()
    const preserved = vi.mocked(api).mock.calls.find(([method]) => method === 'PUT')![2] as Record<string, unknown>
    expect(preserved).not.toHaveProperty('library_selection_explicit')
    expect(preserved.project).not.toHaveProperty('library_selections')
    expect(preserved.project).not.toHaveProperty('library_keys')
    const checkbox = [...document.querySelectorAll<HTMLInputElement>('input[type="checkbox"]')]
      .find((input) => input.parentElement?.textContent?.includes('不再使用历史默认资料'))!
    checkbox.click()
    await flushPromises()
    buttonByText('保存未来配置')!.click()
    await flushPromises()
    const explicit = vi.mocked(api).mock.calls.filter(([method]) => method === 'PUT').at(-1)![2]
    expect(explicit).toMatchObject({ library_selection_explicit: true, project: { library_selections: [] } })
  })

  it('计划资料选择标记缺失时不假定历史默认已清空', async () => {
    const plan = {
      id: 'unknown-legacy', mode: 'daily', timezone: 'Asia/Shanghai', start_at: '2099-06-01T09:00:00+08:00',
      local_time: '09:00', weekdays: [], end_date: null, max_runs: null, version: 1, resources: [],
      library_bindings: [], library_selection_explicit: undefined,
      project: { title: '计划', origin: '背景', goal: '目标', scenario: 'default', authorization_expires_at: null },
    } as unknown as ProjectSchedule
    await mountPage(plan)
    expect(document.body.textContent).toContain('服务未提供计划的资料选择状态')
    expect(buttonByText('保存未来配置')?.disabled).toBe(true)
    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', ctrlKey: true }))
    await flushPromises()
    expect(api).not.toHaveBeenCalledWith('PUT', '/project-schedules/unknown-legacy', expect.anything())
  })

  it('旧计划只用保存的元数据和原范围；缺文件时更新不静默丢失', async () => {
    const plan = {
      id: 'old-plan', mode: 'daily', timezone: 'Asia/Shanghai', start_at: '2099-06-01T09:00:00+08:00',
      local_time: '09:00', weekdays: [], end_date: null, max_runs: null, version: 7, resources: [],
      library_bindings: [{ library_key: 'library-a', library_name: '授权资料', version: 1,
        digest_sha256: 'old-digest', file_count: 2, paths: ['dir/one.md', 'guide.md'], selection_sha256: 'selection' }],
      project: { title: '既有计划', origin: '背景', goal: '目标', scenario: 'default', authorization_expires_at: null },
    } as unknown as ProjectSchedule
    vi.mocked(api).mockImplementation(async (method, path) => {
      if (method === 'GET' && path === '/project-schedules/old-plan/library-manifests/library-a') return {
        library_key: 'library-a', library_name: '授权资料',
        file_count: 2, safety_path: null,
        files: ['dir/one.md', 'guide.md'].map((path) => ({ path, size_bytes: 1, sha256: 'a'.repeat(64) })),
      }
      throw new ApiError('受控拒绝', 422, {})
    })
    mocks.getLibraryManifest.mockResolvedValueOnce({
      library_key: 'library-a', library_name: '授权资料',
      file_count: 1, safety_path: null, files: [{ path: 'guide.md', size_bytes: 1, sha256: 'a'.repeat(64) }],
    }).mockResolvedValueOnce({
      library_key: 'library-a', library_name: '授权资料',
      file_count: 3, safety_path: null,
      files: ['dir/one.md', 'guide.md', 'new.md'].map((path) => ({ path, size_bytes: 1, sha256: 'a'.repeat(64) })),
    })
    await mountPage(plan)
    expect(document.querySelector<HTMLInputElement>('input[aria-label="安全限制"]')!.checked).toBe(false)
    buttonByText('从资料库选择')!.click()
    await flushPromises()
    const root = document.querySelector<HTMLInputElement>('input[aria-label="选择资料库 授权资料 (library-a)"]')!
    expect(root.checked).toBe(true)
    expect(document.body.textContent).toContain('已绑定范围')
    root.click()
    await flushPromises()
    expect(root.checked).toBe(false)
    root.click()
    await flushPromises()
    expect(root.checked).toBe(true)
    expect(mocks.getLibraryManifest).not.toHaveBeenCalled()
    buttonByText('完成选择')!.click()
    await flushPromises()
    buttonByText('保存未来配置')!.click()
    await flushPromises()
    expect(api).toHaveBeenCalledWith('PUT', '/project-schedules/old-plan', expect.objectContaining({
      project: expect.objectContaining({ library_selections: [
        { library_key: 'library-a', source: 'saved', paths: ['dir/one.md', 'guide.md'] },
      ] }),
    }))
    vi.mocked(api).mockClear()
    buttonByText('更新为当前资料')!.click()
    await flushPromises()
    expect(document.body.textContent).toContain('当前资料缺少原选文件')
    expect(document.querySelector('button[aria-label="移除 library-a dir/one.md"]')).not.toBeNull()
    buttonByText('更新为当前资料')!.click()
    await flushPromises()
    buttonByText('保存未来配置')!.click()
    await flushPromises()
    expect(api).toHaveBeenCalledWith('PUT', '/project-schedules/old-plan', expect.objectContaining({
      project: expect.objectContaining({ library_selections: [
        { library_key: 'library-a', source: 'current', paths: ['dir/one.md', 'guide.md'], expected_files: { 'dir/one.md': 'a'.repeat(64), 'guide.md': 'a'.repeat(64) } },
      ] }),
    }))
    const payload = vi.mocked(api).mock.calls.find(([method]) => method === 'PUT')![2] as Record<string, unknown>
    expect(payload).not.toHaveProperty('refresh_library_keys')
    expect(payload.project).not.toHaveProperty('library_keys')
  })

  it('保留历史空整库绑定，禁止更新时静默加入文件，并允许明确移除', async () => {
    const plan = {
      id: 'empty-whole-plan', mode: 'daily', timezone: 'Asia/Shanghai', start_at: '2099-06-01T09:00:00+08:00',
      local_time: '09:00', weekdays: [], end_date: null, max_runs: null, version: 1, resources: [],
      library_selection_explicit: false,
      library_bindings: [{ library_key: 'library-a', library_name: '授权资料', version: 1,
        digest_sha256: 'old-empty', file_count: 0, paths: null, selection_sha256: 'selection' }],
      project: { title: '历史空库计划', origin: '背景', goal: '目标', scenario: 'default', authorization_expires_at: null },
    } as unknown as ProjectSchedule
    vi.mocked(api).mockImplementation(async (method, path) => {
      if (method === 'GET' && path === '/project-schedules/empty-whole-plan/library-manifests/library-a') return {
        library_key: 'library-a', library_name: '授权资料',
        file_count: 0, safety_path: null, files: [],
      }
      throw new ApiError('受控拒绝', 422, {})
    })
    await mountPage(plan)
    expect(document.body.textContent).toContain('授权资料 (library-a) · 历史空整库绑定')
    expect(buttonByText('保存未来配置')?.disabled).toBe(false)
    buttonByText('保存未来配置')!.click()
    await flushPromises()
    expect(api).toHaveBeenCalledWith('PUT', '/project-schedules/empty-whole-plan', expect.objectContaining({
      project: expect.objectContaining({ library_selections: [
        { library_key: 'library-a', source: 'saved', paths: null },
      ] }),
    }))
    buttonByText('更新为当前资料')!.click()
    await flushPromises()
    expect(document.body.textContent).toContain('历史空整库绑定不能直接更新')
    expect(mocks.getLibraryManifest).not.toHaveBeenCalled()
    buttonByText('移除该绑定')!.click()
    await flushPromises()
    buttonByText('保存未来配置')!.click()
    await flushPromises()
    expect(vi.mocked(api).mock.calls.filter(([method]) => method === 'PUT').at(-1)![2]).toMatchObject({
      project: { library_selections: [] },
    })
  })

  it('整库更新后不自动加入新文件，也不悄悄丢掉旧文件', async () => {
    const plan = {
      id: 'whole-plan', mode: 'daily', timezone: 'Asia/Shanghai', start_at: '2099-06-01T09:00:00+08:00',
      local_time: '09:00', weekdays: [], end_date: null, max_runs: null, version: 1, resources: [],
      library_bindings: [{ library_key: 'library-a', library_name: '授权资料', version: 1,
        digest_sha256: 'old', file_count: 2, paths: null, selection_sha256: 'selection' }],
      project: { title: '计划', origin: '背景', goal: '目标', scenario: 'default', authorization_expires_at: null },
    } as unknown as ProjectSchedule
    const files = ['guide.md', 'dir/one.md'].map((path) => ({ path, size_bytes: 1, sha256: 'a'.repeat(64) }))
    vi.mocked(api).mockImplementation(async (method, path) => {
      if (method === 'GET' && path === '/project-schedules/whole-plan/library-manifests/library-a') return {
        library_key: 'library-a', library_name: '授权资料',
        file_count: 2, safety_path: null, files,
      }
      throw new ApiError('受控拒绝', 422, {})
    })
    mocks.getLibraryManifest.mockResolvedValue({
      library_key: 'library-a', library_name: '授权资料',
      file_count: 3, safety_path: null, files: [...files, { path: 'new.md', size_bytes: 1, sha256: 'a'.repeat(64) }],
    })
    await mountPage(plan)
    buttonByText('更新为当前资料')!.click()
    await flushPromises()
    expect(document.querySelector('button[aria-label="移除 library-a new.md"]')).toBeNull()
    buttonByText('保存未来配置')!.click()
    await flushPromises()
    expect(api).toHaveBeenCalledWith('PUT', '/project-schedules/whole-plan', expect.objectContaining({
      project: expect.objectContaining({ library_selections: [
        { library_key: 'library-a', source: 'current', paths: ['guide.md', 'dir/one.md'], expected_files: { 'guide.md': 'a'.repeat(64), 'dir/one.md': 'a'.repeat(64) } },
      ] }),
    }))
  })

  it('资料更新请求迟到时不覆盖用户随后收窄的原选择', async () => {
    const plan = {
      id: 'race', mode: 'daily', timezone: 'Asia/Shanghai', start_at: '2099-06-01T09:00:00+08:00',
      local_time: '09:00', weekdays: [], end_date: null, max_runs: null, version: 1, resources: [],
      library_bindings: [{ library_key: 'library-a', library_name: '授权资料', version: 1,
        digest_sha256: 'old', file_count: 2, paths: null, selection_sha256: 'selection' }],
      project: { title: '计划', origin: '背景', goal: '目标', scenario: 'default', authorization_expires_at: null },
    } as unknown as ProjectSchedule
    const files = ['guide.md', 'dir/one.md'].map((path) => ({ path, size_bytes: 1, sha256: 'a'.repeat(64) }))
    vi.mocked(api).mockImplementation(async (method, path) => {
      if (method === 'GET' && path === '/project-schedules/race/library-manifests/library-a') return {
        library_key: 'library-a', library_name: '授权资料',
        file_count: 2, safety_path: null, files,
      }
      throw new ApiError('受控拒绝', 422, {})
    })
    let resolve!: (value: unknown) => void
    mocks.getLibraryManifest.mockImplementationOnce(() => new Promise((done) => { resolve = done }))
    await mountPage(plan)
    buttonByText('更新为当前资料')!.click()
    document.querySelector<HTMLButtonElement>('button[aria-label="移除 library-a dir/one.md"]')!.click()
    resolve({ library_key: 'library-a', library_name: '授权资料',
      file_count: 2, safety_path: null, files })
    await flushPromises()
    expect(document.querySelector('button[aria-label="移除 library-a guide.md"]')).not.toBeNull()
    buttonByText('保存未来配置')!.click()
    await flushPromises()
    expect(api).toHaveBeenCalledWith('PUT', '/project-schedules/race', expect.objectContaining({
      project: expect.objectContaining({ library_selections: [
        { library_key: 'library-a', source: 'saved', paths: ['guide.md'] },
      ] }),
    }))
  })

  it.each(['committed', 'unknown'] as const)('422 先查询原请求收据，%s 不自动换键重发', async (status) => {
    mocks.createProject.mockRejectedValueOnce(new ApiError('资料选择合同已更新', 422, {}))
    vi.mocked(api).mockImplementation(async (method, path) => {
      const original = mocks.createProject.mock.calls[0]![0]
      expect(method).toBe('GET')
      expect(path).toBe(`/creation-requests/${original.request_id}?operation=project`)
      return { request_id: original.request_id, operation: 'project', status,
        project_id: status === 'committed' ? 'existing-project' : null, schedule_id: null }
    })
    await mountPage()
    await setFieldValue('#np-title', '跨合同恢复')
    buttonByText('创建并启动')!.click()
    await flushPromises()
    expect(mocks.createProject).toHaveBeenCalledOnce()
    expect(api).toHaveBeenCalledOnce()
    expect(buttonByText('修改内容并使用新请求')).toBeUndefined()
    if (status === 'committed') {
      expect(mocks.router.push).toHaveBeenCalledWith('/projects/existing-project')
      expect(sessionStorage.getItem('arena.creation-request')).toBeNull()
    } else {
      expect(mocks.router.push).not.toHaveBeenCalled()
      expect(document.body.textContent).toContain('尚未查到已提交结果，不代表请求失败')
      expect(JSON.parse(sessionStorage.getItem('arena.creation-request')!)).toEqual([{
        request_id: mocks.createProject.mock.calls[0]![0].request_id, operation: 'project',
      }])
    }
  })
})

describe('NewProject creation boundaries', () => {
  it('代理设置：即时创建默认直连，代理凭据仅随原请求重试而不写恢复缓存', async () => {
    mocks.createProject.mockRejectedValueOnce(new Error('连接断开'))
    const wrapper = await mountPage()
    const fields = wrapper.getComponent(TestEgressFields)
    expect(fields.props('modelValue').mode).toBe('direct')
    fields.vm.$emit('update:modelValue', {
      mode: 'socks5',
      host: 'proxy.example.test',
      port: 1080,
      auth_action: 'replace',
      username: 'sample-user',
      password: 'sample-password',
    })
    await setFieldValue('#np-title', '出口样本')
    buttonByText('创建并启动')!.click()
    await flushPromises()
    const payload = mocks.createProject.mock.calls[0]![0]
    expect(payload.test_egress).toEqual({
      mode: 'socks5',
      host: 'proxy.example.test',
      port: 1080,
      auth_action: 'replace',
      username: 'sample-user',
      password: 'sample-password',
    })
    expect(payload.origin + payload.goal).not.toContain('sample-password')
    expect(sessionStorage.getItem('arena.creation-request')).not.toMatch(
      /sample-password|sample-user|test_egress/,
    )
    buttonByText('用原请求恢复提交')!.click()
    await flushPromises()
    expect(mocks.createProject.mock.calls[1]![0]).toEqual(payload)
    expect(fields.props('modelValue').password).toBe('')
  })

  it('代理设置：单次计划创建携带HTTP出口，周期编辑只从公共值保留认证', async () => {
    const wrapper = await mountPage()
    await setFieldValue('#np-title', '单次出口')
    wrapper
      .getComponent(TestEgressFields)
      .vm.$emit('update:modelValue', {
        mode: 'http',
        host: 'proxy.example.test',
        port: 8080,
        auth_action: 'keep',
        username: '',
        password: '',
      })
    await selectScheduleMode('once')
    await setFieldValue('#schedule-start', '2099-04-05T09:00')
    vi.mocked(api).mockRejectedValue(new ApiError('受控拒绝', 422, {}))
    buttonByText('保存单次预约')!.click()
    await flushPromises()
    expect(api).toHaveBeenCalledWith(
      'POST',
      '/project-schedules',
      expect.objectContaining({
        project: expect.objectContaining({
          test_egress: {
            mode: 'http',
            host: 'proxy.example.test',
            port: 8080,
            auth_action: 'keep',
          },
        }),
      }),
    )
    wrapper.unmount()
    const plan = await mountPage({
      id: 'egress-plan',
      tenant_id: 'tenant-test',
      creator_id: 'user-test',
      status: 'active',
      next_run_at: null,
      project_id: null,
      last_project_id: null,
      generated_count: 0,
      block_reason: null,
      created_at: '2026-10-26T00:00:00Z',
      updated_at: '2026-10-26T00:00:00Z',
      last_result: null,
      mode: 'daily',
      timezone: 'Asia/Shanghai',
      start_at: '2099-06-01T09:00:00+08:00',
      local_time: '09:00',
      weekdays: [],
      end_date: null,
      max_runs: null,
      version: 7,
      resources: [],
      library_bindings: [],
      project: {
        title: '出口计划',
        origin: '背景',
        goal: '目标',
        scenario: 'default',
        authorization_expires_at: null,
        test_egress: {
          mode: 'socks5',
          host: 'proxy.example.test',
          port: 1080,
          auth_configured: true,
          revision: 3,
        },
      },
    } as ProjectSchedule)
    expect(plan.getComponent(TestEgressFields).props('authConfigured')).toBe(true)
    expect(plan.getComponent(TestEgressFields).props('modelValue').password).toBe('')
    buttonByText('保存未来配置')!.click()
    await flushPromises()
    expect(api).toHaveBeenCalledWith(
      'PUT',
      '/project-schedules/egress-plan',
      expect.objectContaining({
        project: expect.objectContaining({
          test_egress: {
            mode: 'socks5',
            host: 'proxy.example.test',
            port: 1080,
            auth_action: 'keep',
          },
        }),
      }),
    )
  })

  it('不查询模板或 Worker，保留正文并由服务端确定执行配置', async () => {
    await mountPage()
    expect(Object.keys(mocks.queryData)).not.toContain('worker-templates')
    expect(Object.keys(mocks.queryData)).not.toContain('tenant-worker-options')
    expect(document.querySelector('#np-worker-template')).toBeNull()
    expect(document.body.textContent).not.toContain('执行设置')
    await setFieldValue('#np-title', '正文先填写')
    await setFieldValue('#np-origin-input', '  原始背景\n  保留格式  ')
    buttonByText('创建并启动')!.click()
    await flushPromises()
    expect(mocks.createProject).toHaveBeenCalledExactlyOnceWith(
      expect.objectContaining({ origin: '  原始背景\n  保留格式  ' }),
    )
    expect(mocks.createProject.mock.calls[0]![0]).not.toHaveProperty('tenant_worker_ids')
  })

  it('启动方式使用单选组，仅时间歧义时显示偏移选择', async () => {
    await mountPage()
    const group = document.querySelector<HTMLElement>('[role="radiogroup"][aria-label="启动方式"]')!
    const radios = [...group.querySelectorAll<HTMLInputElement>('input[type="radio"]')]
    expect(radios.map((radio) => radio.value)).toEqual(['immediate', 'once', 'daily', 'weekly'])
    expect(new Set(radios.map((radio) => radio.name)).size).toBe(1)
    expect(radios.filter((radio) => radio.checked).map((radio) => radio.value)).toEqual(['immediate'])
    await selectScheduleMode('once')
    await setFieldValue('#schedule-timezone', 'America/New_York')
    await setFieldValue('#schedule-start', '2030-10-01T09:00')
    expect(document.querySelector('#schedule-offset')).toBeNull()
    await setFieldValue('#schedule-start', '2030-11-03T01:30')
    expect(document.querySelector('#schedule-offset')).not.toBeNull()
    expect(radios.filter((radio) => radio.checked).map((radio) => radio.value)).toEqual(['once'])
  })

  it('代理方式直接可见，非直连显示字段并保留首错定位', async () => {
    await mountPage()
    const panel = document.querySelector<HTMLElement>('#np-proxy-settings')!
    const scroll = vi.spyOn(panel, 'scrollIntoView')
    expect(panel.querySelector('fieldset')?.getAttribute('aria-label')).toBe('代理设置')
    expect(panel.querySelector('input')).toBeNull()
    const mode = panel.querySelector<HTMLElement>('[role="combobox"]')!
    expect(mode.textContent).toContain('直接连接')
    await selectOption(mode.id, 'HTTP 代理')
    expect(mode.textContent).toContain('HTTP 代理')
    expect(panel.querySelector('input')).not.toBeNull()
    await setFieldValue('#np-title', '代理校验')
    buttonByText('创建并启动')!.click()
    await flushPromises()
    expect(mocks.createProject).not.toHaveBeenCalled()
    expect(document.body.textContent).toContain('请填写代理主机地址')
    expect(scroll).toHaveBeenCalled()
  })

  it.each(['daily', 'weekly'] as const)(
    '旧 %s 计划期限在时间设置编辑，未改保值、清空取消且不提交 Worker IDs',
    async (mode) => {
      const authorization = '2099-07-01T10:30:20+08:00'
      const deadline = '2099-06-30T18:20:10+08:00'
      vi.mocked(api).mockRejectedValue(new ApiError('受控拒绝', 422, {}))
      await mountPage({
        id: 'retained-plan',
        tenant_id: 'tenant-test',
        creator_id: 'user-test',
        mode,
        timezone: 'Asia/Shanghai',
        start_at: '2099-06-01T09:00:00+08:00',
        local_time: '09:00',
        weekdays: mode === 'weekly' ? [1, 3] : [],
        end_date: null,
        max_runs: null,
        version: 7,
        status: 'active',
        next_run_at: '2099-06-01T09:00:00+08:00',
        project_id: null,
        last_project_id: null,
        generated_count: 0,
        block_reason: null,
        created_at: '2099-05-01T00:00:00Z',
        updated_at: '2099-05-01T00:00:00Z',
        last_result: null,
        resources: [],
        library_bindings: [],
        project: {
          title: '既有计划',
          origin: '历史背景',
          goal: '历史目标',
          scenario: 'default',
          authorization_expires_at: authorization,
          explore_deadline: deadline,
        },
      })
      expect(document.querySelector('#np-timing-settings #np-explore-deadline')).not.toBeNull()
      expect(document.querySelector('#np-proxy-settings #np-explore-deadline')).toBeNull()
      buttonByText('保存未来配置')!.click()
      await flushPromises()
      const first = vi.mocked(api).mock.calls.find(([method]) => method === 'PUT')![2] as {
        project: Record<string, unknown>
      }
      expect(first.project).toMatchObject({
        authorization_expires_at: authorization,
        explore_deadline: deadline,
      })
      expect(first.project).not.toHaveProperty('tenant_worker_ids')
      await setFieldValue('#np-explore-deadline', '')
      await setFieldValue('#np-authorization-expires-at', '')
      buttonByText('保存未来配置')!.click()
      await flushPromises()
      expect(api).toHaveBeenLastCalledWith(
        'PUT',
        '/project-schedules/retained-plan',
        expect.objectContaining({
          project: expect.objectContaining({
            authorization_expires_at: null,
            explore_deadline: null,
          }),
          version: 7,
        }),
      )
    },
  )

  it('缺少模型的 422 保留草稿与配置入口，并先核对原请求收据', async () => {
    vi.mocked(api).mockResolvedValue({ status: 'unknown', project_id: null, schedule_id: null })
    mocks.createProject.mockRejectedValueOnce(
      new ApiError('Worker explore (worker-explore) 不可用：model_unconfigured', 422, {}),
    )
    await mountPage()
    await setFieldValue('#np-title', '等待配置')
    await setFieldValue('#np-origin-input', '保留原始背景')
    buttonByText('创建并启动')!.click()
    await flushPromises()
    expect(document.querySelector<HTMLInputElement>('#np-title')?.value).toBe('等待配置')
    expect(document.querySelector<HTMLTextAreaElement>('#np-origin-input')?.value).toBe(
      '保留原始背景',
    )
    expect(document.querySelector('a[target="_blank"]')?.textContent).toContain('前往 Worker 配置')
    expect(document.body.textContent).toContain('保存结果未确认')
    expect(api).toHaveBeenCalledWith('GET', `/creation-requests/${mocks.createProject.mock.calls[0]![0].request_id}?operation=project`)
    expect(mocks.createProject).toHaveBeenCalledOnce()
  })

  it('reports a random source failure before any request and allows retry', async () => {
    const random = vi.spyOn(crypto, 'getRandomValues').mockImplementationOnce(() => {
      throw new Error('随机源不可用')
    })
    await mountPage()
    await setFieldValue('#np-title', '本地失败')
    buttonByText('创建并启动')!.click()
    await flushPromises()
    expect(mocks.createProject).not.toHaveBeenCalled()
    expect(sessionStorage.getItem('arena.creation-request')).toBeNull()
    expect(document.body.textContent).toContain('请求尚未发出：随机源不可用')
    expect(document.body.textContent).not.toContain('保存结果未确认')
    expect(buttonByText('创建并启动')?.disabled).toBe(false)
    random.mockRestore()
    buttonByText('创建并启动')!.click()
    await flushPromises()
    expect(mocks.createProject).toHaveBeenCalledOnce()
    expect(mocks.createProject.mock.calls[0]![0].request_id).toMatch(
      /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/,
    )
  })

  it('keeps the original unknown payload when optional cache writes fail', async () => {
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new Error('缓存写入不可用')
    })
    mocks.createProject.mockRejectedValueOnce(new Error('连接断开'))
    await mountPage()
    await setFieldValue('#np-title', '缓存不可用')
    buttonByText('创建并启动')!.click()
    await flushPromises()
    const original = JSON.parse(JSON.stringify(mocks.createProject.mock.calls[0]![0]))
    expect(document.body.textContent).toContain('跨刷新查询入口未保存')
    expect(document.body.textContent).toContain('连接断开')
    expect(buttonByText('用原请求恢复提交')?.disabled).toBe(false)
    buttonByText('用原请求恢复提交')!.click()
    await flushPromises()
    expect(mocks.createProject.mock.calls.map(([payload]) => payload)).toEqual([original, original])
    expect(document.body.textContent).toContain('项目已创建，等待调度')
    expect(document.body.textContent).not.toContain('保存结果未确认')
  })

  it('keeps confirmed success when cache cleanup and success feedback fail', async () => {
    vi.spyOn(Storage.prototype, 'removeItem').mockImplementation(() => {
      throw new Error('清理不可用')
    })
    mocks.uiStore.showToast.mockImplementationOnce(() => {
      throw new Error('反馈不可用')
    })
    await mountPage()
    await setFieldValue('#np-title', '已确认成功')
    buttonByText('创建并启动')!.click()
    await flushPromises()
    expect(document.body.textContent).toContain('项目已创建，等待调度')
    expect(document.body.textContent).toContain('请求引用缓存清理未完成')
    expect(document.body.textContent).toContain('已确认保存，后续页面反馈未完成：反馈不可用')
    expect(document.body.textContent).not.toContain('保存结果未确认')
    expect(buttonByText('取消')?.disabled).toBe(false)
    buttonByText('进入已创建项目')!.click()
    await flushPromises()
    expect(mocks.router.push).toHaveBeenLastCalledWith('/projects/project-created')
    expect(mocks.createProject).toHaveBeenCalledOnce()
  })

  it.each([
    { targets: null },
    { targets: [] },
    { targets: [{ type: 'url', value: 'https://example.test/path?x=1', label: '原始附加字段' }] },
  ])('preserves stored targets when editing a recurring plan: %j', async ({ targets }) => {
    vi.mocked(api).mockRejectedValue(new ApiError('受控编辑拒绝', 422, {}))
    const wrapper = await mountPage({
      id: 'history',
      mode: 'daily',
      timezone: 'Asia/Shanghai',
      start_at: '2099-06-01T09:00:00+08:00',
      local_time: '09:00',
      weekdays: [],
      end_date: null,
      max_runs: null,
      version: 7,
      resources: [],
      library_bindings: [],
      project: {
        title: '既有计划',
        origin: '历史背景',
        goal: '历史目标',
        scenario: 'default',
        targets,
      },
    } as unknown as ProjectSchedule)
    if (targets === null)
      expect(wrapper.findComponent({ name: 'ScheduleTiming' }).props('project')).not.toHaveProperty(
        'targets',
      )
    await setFieldValue('#np-title', '仅修改标题')
    expect(document.querySelector('#np-targets')).toBeNull()
    buttonByText('保存未来配置')!.click()
    await flushPromises()
    expect(api).toHaveBeenCalledWith(
      'PUT',
      '/project-schedules/history',
      expect.objectContaining({
        version: 7,
        project: expect.objectContaining({ title: '仅修改标题', targets }),
      }),
    )
  })

  it.each(['{bad json', '[{"request_id":"broken","operation":"project"}]'])(
    'clears damaged cache and shows its warning in the refresh recovery entry: %s',
    async (raw) => {
      sessionStorage.setItem('arena.creation-request', raw)
      const wrapper = mount(CreationRequestRecovery)
      wrappers.push(wrapper)
      await flushPromises()
      expect(wrapper.text()).toContain('请求引用缓存已损坏并清除')
      expect(sessionStorage.getItem('arena.creation-request')).toBeNull()
      expect(api).not.toHaveBeenCalled()
    },
  )

  it('shows optional cache read failures without failing the recovery entry', async () => {
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new Error('读取不可用')
    })
    const wrapper = mount(CreationRequestRecovery)
    wrappers.push(wrapper)
    await flushPromises()
    expect(wrapper.text()).toContain('请求引用缓存不可用')
    expect(wrapper.text()).toContain('读取不可用')
    expect(api).not.toHaveBeenCalled()
  })
})

describe('NewProject shared control migration', () => {
  it('focuses the title, selects the default scenario, preserves touched scenario templates', async () => {
    await mountPage()

    expect(document.activeElement).toBe(document.querySelector('#np-title'))
    expect(document.querySelector<HTMLTextAreaElement>('#np-origin-input')?.value).toBe(
      'default origin\npublic environment context',
    )
    expect(document.querySelector('#np-scenario[role="combobox"]')?.textContent).toContain(
      '默认场景',
    )
    expect(document.querySelector('#np-targets-input')).toBeNull()
    expect(document.body.textContent).toContain('Origin（已知信息）')
    expect(document.body.textContent).toContain('Goal（目标）')
    expect(document.body.textContent).not.toContain('入口凭证')

    const origin = document.querySelector<HTMLTextAreaElement>('#np-origin-input')!
    origin.value = 'user supplied origin'
    origin.dispatchEvent(new Event('input', { bubbles: true }))
    await flushPromises()
    await selectOption('np-scenario', '次要场景')

    expect(origin.value).toBe('user supplied origin')
    expect(document.querySelector<HTMLTextAreaElement>('#np-goal-input')?.value).toBe(
      'secondary goal',
    )
    expect(document.querySelector('#np-scenario')?.getAttribute('aria-expanded')).toBe('false')
    mocks.queryData['scenario-catalog'].value = mocks.scenarios.map((scenario) => ({
      ...scenario,
      goal_template: `${scenario.goal_template} refreshed`,
    }))
    await flushPromises()
    expect(document.querySelector('#np-scenario')?.getAttribute('aria-expanded')).toBe('false')
    expect(origin.value).toBe('user supplied origin')
    expect(document.querySelector('#np-scenario[role="combobox"]')?.textContent).toContain(
      '次要场景',
    )
  })

  it('applies AI Agent templates exactly and preserves user-edited fields', async () => {
    await mountPage()

    await selectOption('np-scenario', 'AI Agent 安全评估')

    const origin = document.querySelector<HTMLTextAreaElement>('#np-origin-input')!
    const goal = document.querySelector<HTMLTextAreaElement>('#np-goal-input')!
    expect(origin.value).toBe(aiAgentOrigin)
    expect(goal.value).toBe(aiAgentGoal)

    goal.value = 'user-defined Agent coverage'
    goal.dispatchEvent(new Event('input', { bubbles: true }))
    await flushPromises()
    await selectOption('np-scenario', '次要场景')

    expect(origin.value).toBe('secondary origin')
    expect(goal.value).toBe('user-defined Agent coverage')
  })

  it('submits address-free Origin and Goal without targets and includes the uploaded resource batch id', async () => {
    await mountPage()

    await setFieldValue('#np-title', '  Workspace title  ')
    await setFieldValue(
      '#np-origin-input',
      '已有接口说明和两个测试角色，重点关注订单审批相关功能。',
    )
    await setFieldValue('#np-goal-input', '重点检查普通用户之间的数据隔离及审批流程。')

    buttonByText('从资料库选择')?.click()
    await flushPromises()
    document.querySelector<HTMLInputElement>('input[aria-label="选择资料库 授权资料 (library-a)"]')!.click()
    await flushPromises()
    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', ctrlKey: true }))
    await flushPromises()
    expect(mocks.createProject).not.toHaveBeenCalled()
    buttonByText('完成选择')?.click()
    await flushPromises()

    const fileInput = document.querySelector<HTMLInputElement>('input[type="file"]')!
    Object.defineProperty(fileInput, 'files', {
      configurable: true,
      value: [new File(['echo should-not-run\n'], 'probe.sh', { type: 'text/plain' })],
    })
    fileInput.dispatchEvent(new Event('change', { bubbles: true }))
    await flushPromises()

    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', ctrlKey: true }))
    await flushPromises()

    expect(mocks.createProject).toHaveBeenCalledOnce()
    expect(mocks.createProject).toHaveBeenCalledWith({
      test_egress: { mode: 'direct', auth_action: 'clear' },
      title: 'Workspace title',
      origin: '已有接口说明和两个测试角色，重点关注订单审批相关功能。',
      goal: '重点检查普通用户之间的数据隔离及审批流程。',
      scenario: 'default',
      request_id: expect.any(String),
      explore_deadline: null,
      authorization_expires_at: null,
      resource_batch_id: 'batch-created',
      library_selections: [{ library_key: 'library-a', source: 'current', paths: null, expected_files: { 'guide.md': 'a'.repeat(64), 'dir/one.md': 'a'.repeat(64), 'dir/two.md': 'a'.repeat(64) } }],
    })
    expect(mocks.uploadResourceFile).toHaveBeenCalledWith(
      'batch-created',
      expect.any(File),
      'probe.sh',
    )
    expect(mocks.router.push).toHaveBeenCalledWith('/projects/project-created')
  })

  it('资料选择器关闭后保留失败文件与正文，补传使用原 File', async () => {
    mocks.uploadResourceFile.mockRejectedValueOnce(new ApiError('文件未接收', 422, {}))
    const backgroundTrigger = document.createElement('button')
    backgroundTrigger.textContent = '新建项目'
    document.body.append(backgroundTrigger)
    backgroundTrigger.focus()
    const wrapper = await mountPage()
    await setFieldValue('#np-title', '资料草稿')
    await setFieldValue('#np-origin-input', '不应丢失的长正文\n第二段')
    const file = new File(['local draft'], 'notes.txt', { type: 'text/plain' })
    const input = document.querySelector<HTMLInputElement>('input[type="file"]')!
    Object.defineProperty(input, 'files', { configurable: true, value: [file] })
    input.dispatchEvent(new Event('change', { bubbles: true }))
    await flushPromises()
    buttonByText('从资料库选择')!.click()
    await flushPromises()
    buttonByText('完成选择')!.click()
    await flushPromises()
    expect(document.querySelector('input[type="file"]')).toBe(input)
    expect(document.activeElement).toBe(document.getElementById('np-library-picker-trigger'))
    expect(document.querySelector<HTMLTextAreaElement>('#np-origin-input')!.value).toBe(
      '不应丢失的长正文\n第二段',
    )
    expect(document.body.textContent).toContain('notes.txt')
    buttonByText('补传')!.click()
    await flushPromises()
    expect(mocks.uploadResourceFile).toHaveBeenCalledTimes(2)
    expect(mocks.uploadResourceFile).toHaveBeenLastCalledWith('batch-created', file, 'notes.txt')
    expect(mocks.cancelResourceBatch).not.toHaveBeenCalled()
    expect(mocks.createProject).not.toHaveBeenCalled()
    wrapper.unmount()
    await flushPromises()
    expect(document.activeElement).toBe(backgroundTrigger)
  })

  it('does not submit the creation draft while an IME is composing', async () => {
    await mountPage()
    await setFieldValue('#np-title', '中文输入草稿')
    window.dispatchEvent(
      new KeyboardEvent('keydown', { key: 'Enter', ctrlKey: true, isComposing: true }),
    )
    window.dispatchEvent(
      new KeyboardEvent('keydown', { key: 'Enter', metaKey: true, keyCode: 229 }),
    )
    await flushPromises()
    expect(mocks.createProject).not.toHaveBeenCalled()
    expect((document.querySelector('#np-title') as HTMLInputElement).value).toBe('中文输入草稿')
    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', ctrlKey: true }))
    await flushPromises()
    expect(mocks.createProject).toHaveBeenCalledOnce()
  })

  it('creates with explicit null without touching the optional authorization controls', async () => {
    await mountPage()

    expect(document.querySelector('#np-explore-budget')).toBeNull()
    expect(document.querySelector('#np-explore-deadline')).toBeNull()
    expect(document.querySelector('#np-authorization-expires-at')).toBeNull()
    expect(document.body.textContent).not.toContain('执行边界')
    expect(document.body.textContent).not.toContain('明确确认真实授权无限期有效')
    expect(document.querySelector('input[aria-label="授权无限期有效"]')).toBeNull()

    await setFieldValue('#np-title', 'Unlimited authorization')
    buttonByText('创建并启动')?.click()
    await flushPromises()

    expect(mocks.createProject).toHaveBeenCalledOnce()
    expect(mocks.createProject).toHaveBeenCalledWith({
      test_egress: { mode: 'direct', auth_action: 'clear' },
      title: 'Unlimited authorization',
      request_id: expect.any(String),
      explore_deadline: null,
      origin: 'default origin\npublic environment context',
      goal: 'default goal',
      scenario: 'default',
      authorization_expires_at: null,
      library_selections: [],
    })
    expect(mocks.router.push).toHaveBeenCalledWith('/projects/project-created')
  })

  it('validates the optional once stop in the schedule timezone and ignores it in immediate mode', async () => {
    vi.mocked(api).mockRejectedValue(new ApiError('受控计划拒绝', 400, {}))
    await mountPage()
    await setFieldValue('#np-title', 'Independent stop time')
    await selectScheduleMode('once')
    await setFieldValue('#schedule-timezone', 'Asia/Shanghai')
    await setFieldValue('#schedule-start', '2035-04-05T09:00')
    await selectOption('schedule-stop-mode', '指定停止时间')
    await setFieldValue('#schedule-stop', '2035-04-05T09:00')
    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', ctrlKey: true }))
    await flushPromises()
    expect(api).not.toHaveBeenCalledWith('POST', '/project-schedules', expect.anything())
    expect(document.body.textContent).toContain('停止时间必须严格晚于启动时间')
    await setFieldValue('#schedule-stop', '2035-04-05T10:30')
    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', ctrlKey: true }))
    await flushPromises()
    expect(api).toHaveBeenCalledWith(
      'POST',
      '/project-schedules',
      expect.objectContaining({
        start_at: '2035-04-05T09:00:00+08:00',
        project: expect.objectContaining({
          explore_deadline: '2035-04-05T10:30:00+08:00',
          authorization_expires_at: null,
        }),
      }),
    )
    await selectScheduleMode('immediate')
    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', ctrlKey: true }))
    await flushPromises()
    expect(mocks.createProject).toHaveBeenCalledWith(
      expect.objectContaining({ explore_deadline: null, authorization_expires_at: null }),
    )
  })

  it('keeps an uploaded batch after project validation fails so submission can retry', async () => {
    vi.mocked(api).mockResolvedValue({ status: 'unknown', project_id: null, schedule_id: null })
    mocks.createProject
      .mockRejectedValueOnce(new ApiError('项目字段校验失败', 422, {}))
      .mockResolvedValueOnce({ project: { id: 'project-created' } })
    await mountPage()

    await setFieldValue('#np-title', 'Retry project')

    const fileInput = document.querySelectorAll<HTMLInputElement>('input[type="file"]')[0]
    Object.defineProperty(fileInput, 'files', {
      configurable: true,
      value: [new File(['binary'], 'evidence.bin')],
    })
    fileInput.dispatchEvent(new Event('change', { bubbles: true }))
    await flushPromises()

    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', ctrlKey: true }))
    await flushPromises()
    expect(document.body.textContent).toContain('项目字段校验失败')
    expect(mocks.cancelResourceBatch).not.toHaveBeenCalled()

    buttonByText('用原请求恢复提交')!.click()
    await flushPromises()
    expect(mocks.createProject).toHaveBeenCalledTimes(2)
    expect(mocks.createProject.mock.calls[1]?.[0]).toEqual(mocks.createProject.mock.calls[0]?.[0])
    expect(mocks.createProject.mock.calls[0]?.[0]).toMatchObject({
      authorization_expires_at: null,
      resource_batch_id: 'batch-created',
    })
    expect(mocks.createProject.mock.calls[1]?.[0]).toMatchObject({
      authorization_expires_at: null,
      resource_batch_id: 'batch-created',
    })
    expect(mocks.createResourceBatch).toHaveBeenCalledOnce()
    expect(mocks.uploadResourceFile).toHaveBeenCalledOnce()
  })

  it.each([
    new Error('连接断开'),
    new ApiError('服务异常', 503, {}),
    new ApiError('请求超时', 408, {}),
  ])('keeps an unconfirmed creation protected after %s', async (error) => {
    mocks.createProject.mockRejectedValueOnce(error)
    const wrapper = await mountPage()
    await setFieldValue('#np-title', 'Unconfirmed project')

    const fileInput = document.querySelector<HTMLInputElement>('input[type="file"]')!
    Object.defineProperty(fileInput, 'files', {
      configurable: true,
      value: [new File(['binary'], 'evidence.bin')],
    })
    fileInput.dispatchEvent(new Event('change', { bubbles: true }))
    await flushPromises()
    buttonByText('创建并启动')?.click()
    await flushPromises()

    expect(document.body.textContent).toContain('保存结果未确认')
    expect(document.querySelector<HTMLInputElement>('#np-title')?.value).toBe('Unconfirmed project')
    expect(document.body.textContent).toContain('evidence.bin')
    expect(buttonByText('创建并启动')?.disabled).toBe(true)
    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', ctrlKey: true }))
    buttonByText('取消')?.click()
    await flushPromises()
    expect(mocks.createProject).toHaveBeenCalledOnce()
    expect(mocks.cancelResourceBatch).not.toHaveBeenCalled()
    expect(wrapper.emitted('discarded')).toBeUndefined()
    expect(wrapper.emitted('update:open')).toContainEqual([false])
  })

  it('restores an unknown creation with the original request and allows normal navigation', async () => {
    mocks.createProject.mockRejectedValueOnce(new Error('连接断开'))
      .mockRejectedValueOnce(new ApiError('请求格式不匹配', 422, {}))
    await mountPage()
    await setFieldValue('#np-title', 'Recover original request')
    buttonByText('创建并启动')?.click()
    await flushPromises()

    const original = mocks.createProject.mock.calls[0]![0]
    expect(original).not.toHaveProperty('tenant_worker_ids')
    await flushPromises()
    expect(original.request_id).toMatch(/^[0-9a-f-]{36}$/i)
    const references = JSON.parse(sessionStorage.getItem('arena.creation-request')!)
    expect(references).toEqual([{ request_id: original.request_id, operation: 'project' }])
    const leave = vi.mocked(onBeforeRouteLeave).mock.calls.at(-1)![0] as () => unknown
    expect(leave()).toBe(true)

    vi.mocked(api).mockResolvedValueOnce({
      request_id: original.request_id,
      operation: 'project',
      status: 'unknown',
      project_id: null,
      schedule_id: null,
    })
    buttonByText('查询请求结果')?.click()
    await flushPromises()
    expect(api).toHaveBeenLastCalledWith(
      'GET',
      `/creation-requests/${original.request_id}?operation=project`,
    )
    expect(document.body.textContent).toContain('尚未查到已提交结果，不代表请求失败')
    expect(mocks.createProject).toHaveBeenCalledOnce()

    buttonByText('用原请求恢复提交')?.click()
    await flushPromises()
    expect(mocks.createProject).toHaveBeenCalledTimes(2)
    expect(mocks.createProject.mock.calls[1]![0]).toEqual(original)
    expect(document.body.textContent).toContain('保存结果未确认')
    expect(buttonByText('修改内容并使用新请求')).toBeUndefined()
    buttonByText('用原请求恢复提交')?.click()
    await flushPromises()
    expect(mocks.createProject).toHaveBeenCalledTimes(3)
    expect(mocks.createProject.mock.calls[2]![0]).toEqual(original)
    expect(mocks.cancelResourceBatch).not.toHaveBeenCalled()
    expect(sessionStorage.getItem('arena.creation-request')).toBeNull()
    expect(mocks.router.push).toHaveBeenLastCalledWith('/projects/project-created')
  })

  it('keeps a created project entry after navigation fails without creating or cancelling again', async () => {
    mocks.router.push.mockRejectedValueOnce(new Error('导航不可用'))
    await mountPage()
    await setFieldValue('#np-title', 'Created project')
    buttonByText('创建并启动')?.click()
    await flushPromises()

    expect(document.body.textContent).toContain('项目已创建，等待调度')
    expect(document.body.textContent).toContain('跳转失败：导航不可用')
    expect(buttonByText('创建并启动')?.disabled).toBe(true)
    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', ctrlKey: true }))
    buttonByText('进入已创建项目')?.click()
    await flushPromises()
    expect(mocks.createProject).toHaveBeenCalledOnce()
    expect(mocks.cancelResourceBatch).not.toHaveBeenCalled()
    expect(mocks.router.push).toHaveBeenCalledTimes(2)
    expect(mocks.router.push).toHaveBeenLastCalledWith('/projects/project-created')
  })

  it('retains the form and batch when cancellation fails and retries only cleanup', async () => {
    mocks.cancelResourceBatch.mockRejectedValueOnce(new ApiError('清理暂不可用', 503, {}))
    const wrapper = await mountPage()
    await setFieldValue('#np-title', 'Keep after cleanup failure')
    const fileInput = document.querySelector<HTMLInputElement>('input[type="file"]')!
    Object.defineProperty(fileInput, 'files', {
      configurable: true,
      value: [new File(['binary'], 'evidence.bin')],
    })
    fileInput.dispatchEvent(new Event('change', { bubbles: true }))
    await flushPromises()
    buttonByText('取消')?.click()
    await flushPromises()
    expect(document.body.textContent).toContain('放弃本次填写？')
    buttonByText('放弃并关闭')?.click()
    await flushPromises()
    expect(document.body.textContent).toContain('临时批次取消失败')
    expect(document.body.textContent).toContain('清理暂不可用')
    expect(wrapper.emitted('discarded')).toBeUndefined()
    expect(document.querySelector<HTMLInputElement>('#np-title')?.value).toBe(
      'Keep after cleanup failure',
    )
    expect(mocks.router.push).not.toHaveBeenCalled()

    buttonByText('放弃并关闭')?.click()
    await flushPromises()
    expect(mocks.cancelResourceBatch).toHaveBeenCalledTimes(2)
    expect(mocks.cancelResourceBatch).toHaveBeenLastCalledWith('batch-created')
    expect(wrapper.emitted('discarded')).toEqual([[]])
    expect(mocks.createProject).not.toHaveBeenCalled()
  })

  it('allows only one in-flight creation and blocks closing until its response', async () => {
    let resolveCreation!: (value: { project: { id: string } }) => void
    mocks.createProject.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          resolveCreation = resolve
        }),
    )
    const wrapper = await mountPage()
    await setFieldValue('#np-title', 'Single request')
    buttonByText('创建并启动')?.click()
    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', ctrlKey: true }))
    await flushPromises()
    expect(mocks.createProject).toHaveBeenCalledOnce()
    expect(buttonByText('取消')?.disabled).toBe(true)
    document
      .querySelector<HTMLElement>('[role="dialog"]')
      ?.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))
    await flushPromises()
    expect(wrapper.emitted('discarded')).toBeUndefined()
    expect(wrapper.emitted('update:open')).toBeUndefined()

    resolveCreation({ project: { id: 'project-created' } })
    await flushPromises()
    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', ctrlKey: true }))
    await flushPromises()
    expect(mocks.createProject).toHaveBeenCalledOnce()
    expect(mocks.router.push).toHaveBeenCalledWith('/projects/project-created')
  })

  it('creates without loading, selecting, or submitting project groups', async () => {
    await mountPage()
    await setFieldValue('#np-title', '无组项目')
    expect(document.querySelector('#np-group-select')).toBeNull()
    expect(mocks.queryData['project-groups']).toBeUndefined()
    expect(buttonByText('创建并启动')?.disabled).toBe(false)
    buttonByText('创建并启动')!.click()
    await flushPromises()
    expect(mocks.createProject).toHaveBeenCalledOnce()
    expect(mocks.createProject.mock.calls[0]![0]).not.toHaveProperty('group_id')
  })
})
