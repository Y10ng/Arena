import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { reactive } from 'vue'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { ProjectDetail } from '@/api/types'
import { ApiError } from '@/api/client'
import WorkspaceOverview from './WorkspaceOverview.vue'

const mocks = vi.hoisted(() => ({
  api: vi.fn(), commit: vi.fn(), deactivate: vi.fn(), getBatch: vi.fn(), cancel: vi.fn(),
  config: vi.fn(), create: vi.fn(), upload: vi.fn(), fetchText: vi.fn(),
  leave: vi.fn(), update: vi.fn(),
}))
vi.mock('vue-router', () => ({ onBeforeRouteLeave: mocks.leave, onBeforeRouteUpdate: mocks.update }))
vi.mock('@/stores/workspace', () => ({ useWorkspaceStore: () => store }))
vi.mock('@/api/client', async (original) => ({
  ...await original<typeof import('@/api/client')>(), api: mocks.api,
  commitProjectResourceBatch: mocks.commit, deactivateProjectResource: mocks.deactivate,
  getProjectResourceBatch: mocks.getBatch, cancelProjectResourceBatch: mocks.cancel,
  getProjectResourceConfig: mocks.config, createProjectResourceBatch: mocks.create,
  uploadProjectResourceBatchFile: mocks.upload, fetchText: mocks.fetchText,
}))
const resource = { id: 'resource-1', path: 'raw/image.bin', kind: 'file', size_bytes: 4, sha256: 'a'.repeat(64), source: 'file' }
const batch = { id: 'batch-1', state: 'open', members: [{ ...resource, id: 'member-1' }], resource_count: 1, total_bytes: 4 }
function detail(id = 'project-1') {
  return {
    project: { id, status: 'stopped', title: '不是 Origin' },
    facts: [{ id: 'goal', description: '  **目标**\n第二行  ' }, { id: 'other', description: '不能按位置取值' }, { id: 'origin', description: '<script>原文</script>\n  范围  ' }],
    resources: [{ ...resource }],
    library_bindings: [{ library_key: 'library/key', library_name: '冻结库', version: 7, digest_sha256: 'b'.repeat(64), paths: ['selected/file.bin'] }],
  } as unknown as ProjectDetail
}
const store = reactive({ projectDetail: detail(), error: '', loadProject: vi.fn() })
let wrapper: VueWrapper | undefined
function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (error: unknown) => void
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}
function button(text: string) {
  const element = [...document.body.querySelectorAll<HTMLButtonElement>('button')].find((item) => item.textContent?.trim() === text)
  if (!element) throw new Error(`找不到按钮 ${text}`)
  return element
}
async function click(text: string) { button(text).click(); await flushPromises() }
async function render(allowed = true) {
  wrapper = mount(WorkspaceOverview, { attachTo: document.body, props: { canMutateResources: allowed, resourceMutationMessage: allowed ? '' : '暂停尚未确认，不能变更资料' } })
  await flushPromises()
  return wrapper
}
async function stageFile() {
  await click('添加资料')
  const input = document.body.querySelector<HTMLInputElement>('input[type="file"]')!
  Object.defineProperty(input, 'files', { configurable: true, value: [new File(['test'], 'draft.bin')] })
  input.dispatchEvent(new Event('change', { bubbles: true }))
  await flushPromises()
}
beforeEach(() => {
  vi.clearAllMocks()
  store.projectDetail = detail()
  store.error = ''
  store.loadProject.mockReset().mockResolvedValue(undefined)
  mocks.api.mockReset().mockResolvedValue([{ path: 'selected/file.bin', size_bytes: 4 }])
  mocks.commit.mockReset().mockResolvedValue(undefined)
  mocks.deactivate.mockReset().mockResolvedValue(undefined)
  mocks.getBatch.mockReset().mockResolvedValue(batch)
  mocks.cancel.mockReset().mockResolvedValue(undefined)
  mocks.create.mockReset().mockResolvedValue({ ...batch, members: [], resource_count: 0, total_bytes: 0 })
  mocks.upload.mockReset().mockResolvedValue({ ...resource, id: 'member-1', path: 'draft.bin' })
  mocks.config.mockResolvedValue({ kinds: ['file'], limits: { max_file_bytes: 500_000_000, max_zip_upload_bytes: 500_000_000, max_active_count: 200, max_active_total_bytes: 2_000_000_000, max_path_bytes: 1024, max_path_segment_bytes: 255 }, reserved_names: [] })
})
afterEach(() => { wrapper?.unmount(); wrapper = undefined; vi.restoreAllMocks(); vi.unstubAllGlobals() })

describe('WorkspaceOverview', () => {
  it('原件预检失败只反馈对应行，重试成功由浏览器下载且不读取正文', async () => {
    const fetch = vi.fn().mockResolvedValueOnce({ ok: false, status: 404 }).mockResolvedValueOnce({ ok: true })
    vi.stubGlobal('fetch', fetch)
    const nativeClick = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {})
    const view = await render()
    await view.get('a').trigger('click')
    await flushPromises()
    expect(view.get('.material-list li [role="alert"]').text()).toContain('404')
    expect(nativeClick).not.toHaveBeenCalled()
    await view.get('a').trigger('click')
    await flushPromises()
    expect(fetch).toHaveBeenLastCalledWith('/projects/project-1/resources/resource-1', { method: 'HEAD', credentials: 'same-origin', cache: 'no-store' })
    expect(nativeClick).toHaveBeenCalledOnce()
    expect(view.find('.material-list li [role="alert"]').exists()).toBe(false)
    expect(mocks.fetchText).not.toHaveBeenCalled()
  })
  it('按固定 ID 原样呈现 Origin/Goal，冻结库子集使用项目版本接口与二进制链接', async () => {
    const view = await render()
    expect(view.get('[aria-label="Origin"] .project-original').element.textContent).toBe('<script>原文</script>\n  范围  ')
    expect(view.get('[aria-label="Goal"] .project-original').element.textContent).toBe('  **目标**\n第二行  ')
    expect(view.find('script').exists()).toBe(false)
    expect(mocks.api).toHaveBeenCalledExactlyOnceWith('GET', '/projects/project-1/advisory-libraries/library%2Fkey/versions/7/files')
    const links = view.findAll('a')
    expect(links.map((link) => link.text())).toEqual(['raw/image.bin', 'selected/file.bin'])
    expect(links[1]!.attributes('href')).toBe('/projects/project-1/advisory-libraries/library%2Fkey/versions/7/files/selected/file.bin')
    expect(links[0]!.attributes('href')).toContain('/projects/project-1/resources/resource-1')
    for (const link of links) expect(link.attributes('download')).toBeDefined()
    expect(mocks.fetchText).not.toHaveBeenCalled()
    expect(mocks.create).not.toHaveBeenCalled()
    expect(document.body.querySelector('[role="dialog"]')).toBeNull()
    store.projectDetail.facts = []
    store.projectDetail.resources = []
    store.projectDetail.library_bindings = []
    await flushPromises()
    expect(view.get('[aria-label="Origin"]').text()).toContain('未记录')
    expect(view.get('[aria-label="Goal"]').text()).toContain('未记录')
    expect(view.text()).toContain('没有上传文件')
    expect(view.text()).toContain('没有绑定资料库文件')
  })

  it('同项目读取失败保留正文，库失败局部反馈并忽略同项目换绑定与切项目旧响应', async () => {
    const old = deferred<unknown[]>()
    mocks.api.mockReturnValueOnce(old.promise)
    const view = await render()
    store.projectDetail.library_bindings[0]!.version = 8
    mocks.api.mockRejectedValueOnce(new ApiError('无权访问', 403, {}))
    await flushPromises()
    expect(view.text()).toContain('资料列表读取失败')
    store.error = '项目刷新失败'
    expect(view.get('[aria-label="Origin"]').text()).toContain('范围')
    old.resolve([{ path: 'stale.bin', size_bytes: 1 }])
    await flushPromises()
    expect(view.text()).not.toContain('stale.bin')
    const previousProject = deferred<unknown[]>()
    mocks.api.mockReturnValueOnce(previousProject.promise)
    await click('重试')
    store.projectDetail = detail('project-2')
    await flushPromises()
    previousProject.resolve([{ path: 'other-project.bin', size_bytes: 1 }])
    await flushPromises()
    expect(view.text()).not.toContain('other-project.bin')
    expect(view.findAll('a').every((link) => link.attributes('href')?.includes('/projects/project-2/'))).toBe(true)
  })

  it('关闭重开保留 File/路径草稿与批次，焦点回入口；取消离开不清空草稿', async () => {
    await render()
    await stageFile()
    const input = document.body.querySelector<HTMLInputElement>('[aria-label="资源相对路径"]')!
    input.value = 'unsaved/path.bin'
    input.dispatchEvent(new Event('input', { bubbles: true }))
    await flushPromises()
    store.error = '刷新失败'
    store.projectDetail = { ...store.projectDetail }
    await flushPromises()
    expect(document.body.querySelector<HTMLInputElement>('[aria-label="资源相对路径"]')!.value).toBe('unsaved/path.bin')
    await click('关闭')
    await new Promise((resolve) => setTimeout(resolve, 0))
    expect(mocks.cancel).not.toHaveBeenCalled()
    expect(document.activeElement?.textContent).toBe('继续添加')
    await click('继续添加')
    expect(document.body.querySelector<HTMLInputElement>('[aria-label="资源相对路径"]')!.value).toBe('unsaved/path.bin')
    expect(button('提交增补').disabled).toBe(true)
    vi.spyOn(window, 'confirm').mockReturnValue(false)
    expect(await mocks.leave.mock.calls[0]![0]()).toBe(false)
    expect(mocks.cancel).not.toHaveBeenCalled()
  })

  it('只提交一次且忙时不可关闭，未知提交保留草稿并核对 consumed_project_id', async () => {
    const pending = deferred<void>()
    mocks.commit.mockReturnValue(pending.promise)
    await render()
    await stageFile()
    button('提交增补').click(); button('提交增补').click()
    await flushPromises()
    expect(mocks.commit).toHaveBeenCalledExactlyOnceWith('project-1', 'batch-1')
    expect(button('关闭').disabled).toBe(true)
    expect(document.body.querySelector('button[aria-label="关闭"]')).toBeNull()
    pending.reject(new TypeError('连接断开'))
    await flushPromises()
    expect(button('提交增补').disabled).toBe(true)
    expect(await mocks.leave.mock.calls[0]![0]()).toBe(false)
    mocks.getBatch.mockResolvedValue({ ...batch, state: 'consumed', consumed_project_id: 'wrong-project' })
    await click('核对导入结果')
    expect(store.loadProject).not.toHaveBeenCalled()
    expect(document.body.textContent).toContain('草稿已保留')
    mocks.getBatch.mockResolvedValue({ ...batch, state: 'consumed', consumed_project_id: 'project-1' })
    await click('核对导入结果')
    expect(store.loadProject).toHaveBeenCalledWith('project-1')
    expect(mocks.commit).toHaveBeenCalledOnce()
    expect(document.body.querySelector('[role="dialog"][data-state="open"]')).toBeNull()
  })

  it('服务端明确拒绝保留草稿可重试，读资格控制上传与停用', async () => {
    await render(false)
    expect(button('停用').disabled).toBe(true)
    await click('添加资料')
    expect(document.body.textContent).toContain('暂停尚未确认，不能变更资料')
    expect(button('添加文件').disabled).toBe(true)
    await click('关闭')
    await wrapper!.setProps({ canMutateResources: true })
    await stageFile()
    mocks.commit.mockRejectedValueOnce(new ApiError('任务仍在运行', 409, {}))
    await click('提交增补')
    expect(button('提交增补').disabled).toBe(false)
    expect(document.body.textContent).toContain('draft.bin')
    await click('提交增补')
    expect(store.loadProject).toHaveBeenCalledWith('project-1')
  })

  it('未知停用不重放，刷新失败或附件仍存在不冒称完成', async () => {
    await render()
    mocks.deactivate.mockRejectedValueOnce(new TypeError('断开'))
    await click('停用')
    expect(button('停用').disabled).toBe(true)
    expect(await mocks.update.mock.calls[0]![0]({ params: { id: 'project-2' } }, { params: { id: 'project-1' } })).toBe(false)
    await click('核对停用结果')
    expect(button('停用').disabled).toBe(true)
    store.error = '刷新失败'
    store.projectDetail.resources = []
    await click('核对停用结果')
    expect(button('核对停用结果')).toBeDefined()
    store.error = ''
    await click('核对停用结果')
    expect(document.body.textContent).not.toContain('核对停用结果')
    expect(mocks.deactivate).toHaveBeenCalledOnce()
  })

  it('离开草稿先确认，取消批次失败仍留在当前项目并保留资料', async () => {
    await render()
    await stageFile()
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    mocks.cancel.mockRejectedValueOnce(new ApiError('暂时无法取消', 503, {}))
    const leave = mocks.leave.mock.calls[0]![0]
    expect(await leave()).toBe(false)
    await flushPromises()
    expect(document.body.textContent).toContain('draft.bin')
    expect(document.body.textContent).toContain('暂时无法取消')
    expect(await leave()).toBe(true)
    expect(mocks.cancel).toHaveBeenCalledTimes(2)
  })

  it('未知提交的核对响应在切项目后不得设置新项目错误', async () => {
    await render()
    await stageFile()
    mocks.commit.mockRejectedValueOnce(new TypeError('回执丢失'))
    await click('提交增补')
    const check = deferred<unknown>()
    mocks.getBatch.mockReturnValueOnce(check.promise)
    await click('核对导入结果')
    store.projectDetail = detail('project-2')
    await flushPromises()
    check.resolve({ ...batch, state: 'open' })
    await flushPromises()
    expect(document.body.textContent).not.toContain('草稿已保留')
    expect(document.body.textContent).not.toContain('回执丢失')
    expect(button('停用').disabled).toBe(false)
    expect(store.loadProject).not.toHaveBeenCalled()
  })

  it('切项目后迟到的停用失败不能污染新项目或锁死操作', async () => {
    const pending = deferred<void>()
    mocks.deactivate.mockReturnValueOnce(pending.promise)
    await render()
    await click('停用')
    store.projectDetail = detail('project-2')
    await flushPromises()
    pending.reject(new TypeError('旧项目失败'))
    await flushPromises()
    expect(document.body.textContent).not.toContain('旧项目失败')
    expect(document.body.textContent).not.toContain('核对停用结果')
    expect(button('停用').disabled).toBe(false)
  })
})
