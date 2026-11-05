import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { useProjectResourceBatch } from './useProjectResourceBatch'
import ProjectResourceBatchUploader from './ProjectResourceBatchUploader.vue'
import { ApiError } from '@/api/client'
import type { ProjectResourceBatchMember } from '@/api/types'

const mocks = vi.hoisted(() => ({
  createBatch: vi.fn(),
  deleteMember: vi.fn(),
  getConfig: vi.fn(),
  getBatch: vi.fn(),
  importZip: vi.fn(),
  patchMember: vi.fn(),
  uploadFile: vi.fn(),
  cancelBatch: vi.fn(),
  showToast: vi.fn(),
}))

vi.mock('@/api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/client')>()
  return {
    ApiError: actual.ApiError,
    cancelProjectResourceBatch: mocks.cancelBatch,
    createProjectResourceBatch: mocks.createBatch,
    deleteProjectResourceBatchMember: mocks.deleteMember,
    getProjectResourceBatch: mocks.getBatch,
    getProjectResourceConfig: mocks.getConfig,
    importProjectResourceBatchZip: mocks.importZip,
    updateProjectResourceBatchMember: mocks.patchMember,
    uploadProjectResourceBatchFile: mocks.uploadFile,
  }
})

vi.mock('@/stores/ui', () => ({
  useUIStore: () => ({ showToast: mocks.showToast }),
}))

const config = {
  kinds: ['note', 'script', 'file'],
  limits: {
    max_active_count: 200,
    max_file_bytes: 500_000_000,
    max_active_total_bytes: 2_000_000_000,
    max_zip_upload_bytes: 500_000_000,
    max_zip_entries: 400,
    max_compression_ratio: 200,
    max_open_batches_per_user: 4,
    max_staged_bytes_per_user: 4_000_000_000,
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

const emptyBatch = {
  id: 'batch-1',
  state: 'open',
  created_at: '2026-09-01T00:00:00Z',
  expires_at: '2026-09-02T00:00:00Z',
  resource_count: 0,
  total_bytes: 0,
  members: [],
}

beforeEach(() => {
  mocks.getConfig.mockReset().mockResolvedValue(config)
  mocks.createBatch.mockReset().mockResolvedValue(emptyBatch)
  mocks.getBatch.mockReset().mockResolvedValue(emptyBatch)
  mocks.uploadFile.mockReset()
  mocks.importZip.mockReset()
  mocks.patchMember.mockReset()
  mocks.deleteMember.mockReset().mockResolvedValue(undefined)
  mocks.cancelBatch.mockReset().mockResolvedValue(undefined)
  mocks.showToast.mockReset()
})

afterEach(() => { vi.restoreAllMocks() })

// 这里只验证浏览器预检，不把 size fixture 当成真实 500 MB 传输验收。
function sizedFile(name: string, size: number) {
  const file = new File(['fixture'], name)
  Object.defineProperty(file, 'size', { value: size })
  return file
}

describe('ProjectResourceBatchUploader', () => {
  const selectFiles = async (wrapper: ReturnType<typeof mount>, files: File[]) => {
    const input = wrapper.find<HTMLInputElement>('input[type="file"]')
    Object.defineProperty(input.element, 'files', { configurable: true, value: files })
    await input.trigger('change')
    await flushPromises()
  }

  it.each(['选择', '拖放'])('%s 混合边界文件时只弹一次，超限不入队且合法项继续', async (mode) => {
    const allowed = sizedFile('allowed.bin', 500_000_000)
    const oversized = [sizedFile('large.bin', 500_000_001), sizedFile('larger.bin', 500_000_002)]
    mocks.uploadFile.mockResolvedValue({ id: 'allowed', path: allowed.name, kind: 'file', source: 'file', size_bytes: allowed.size, sha256: 'a'.repeat(64) })
    const wrapper = mount(ProjectResourceBatchUploader, { attachTo: document.body, props: { modelValue: null } })
    try {
      await flushPromises()
      expect(wrapper.text()).not.toContain('500 MB')
      const files = [oversized[0]!, allowed, oversized[1]!]
      if (mode === '选择') await selectFiles(wrapper, files)
      else { await wrapper.trigger('drop', { dataTransfer: { files } }); await flushPromises() }
      const dialogs = document.body.querySelectorAll('[role="dialog"]')
      expect(dialogs).toHaveLength(1)
      expect(dialogs[0]!.textContent).toContain('以下文件超过 500 MB，未上传。')
      expect([...dialogs[0]!.querySelectorAll('li')].map((li) => li.textContent)).toEqual(['large.bin', 'larger.bin'])
      expect(mocks.uploadFile).toHaveBeenCalledOnce()
      expect(mocks.uploadFile.mock.calls[0]![1]).toBe(allowed)
      expect(wrapper.find('.resource-pending-list').exists()).toBe(false)
      expect(wrapper.emitted('pending')?.at(-1)).toEqual([false])
      expect(mocks.showToast).not.toHaveBeenCalled()
      expect(wrapper.find('[role="alert"]').exists()).toBe(false)
      dialogs[0]!.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))
      await flushPromises()
      await new Promise((resolve) => setTimeout(resolve, 0))
      expect(document.body.querySelector('[role="dialog"]')).toBeNull()
      expect(document.activeElement?.textContent).toBe('添加文件')
    } finally { wrapper.unmount() }
  })

  it('独立 ZIP 超限不创建批次，服务端成员超限与配额拒绝不混淆', async () => {
    const session = useProjectResourceBatch()
    await session.importZip(sizedFile('too-large.zip', 500_000_001))
    expect(mocks.createBatch).not.toHaveBeenCalled()
    expect(session.pending.value).toBe(false)
    mocks.importZip.mockRejectedValueOnce(new ApiError('成员过大', 413, { detail: { code: 'project_resource_file_too_large', category: 'zip_entry', limit_bytes: 500_000_000, path: 'inside/large.bin' } }))
    await session.importZip(sizedFile('allowed.zip', 500_000_000))
    expect(session.oversized.value).toEqual([{ name: 'inside/large.bin', limit: 500_000_000, member: true }])
    expect(session.pending.value).toBe(false)
    mocks.uploadFile.mockRejectedValueOnce(new ApiError('暂存总额超限', 413, { detail: { code: 'project_resource_quota_exceeded' } }))
    await expect(session.uploadFiles([new File(['x'], 'quota.bin')])).rejects.toThrow('暂存总额超限')
    expect(session.oversized.value).toEqual([])
    expect(session.uploads.value[0]!.state).toBe('failed')
    Object.defineProperty(session.uploads.value[0]!.file, 'size', { value: 500_000_001 })
    await session.retryUpload(session.uploads.value[0]!)
    expect(session.pending.value).toBe(false)
    expect(mocks.uploadFile).toHaveBeenCalledOnce()
  })

  it('retains successful members and only retries the explicitly rejected file', async () => {
    const first = new File(['one'], 'first.txt')
    const second = new File(['two'], 'second.txt')
    const makeMember = (id: string, path: string) => ({
      id,
      kind: 'file',
      path,
      size_bytes: 3,
      sha256: 'a'.repeat(64),
      source: 'file',
    })
    mocks.uploadFile
      .mockResolvedValueOnce(makeMember('first', 'first.txt'))
      .mockRejectedValueOnce(new ApiError('配额拒绝', 409, {}))
      .mockResolvedValueOnce(makeMember('second', 'second.txt'))
    const wrapper = mount(ProjectResourceBatchUploader, {
      attachTo: document.body,
      props: { modelValue: null },
    })
    await flushPromises()
    await selectFiles(wrapper, [first, second])
    expect(wrapper.text()).toContain('first.txt')
    expect(wrapper.text()).toContain('上传失败')
    expect(wrapper.emitted('pending')?.at(-1)).toEqual([true])
    const retry = wrapper.findAll('button').find((button) => button.text() === '补传')!
    await retry.trigger('click')
    await flushPromises()
    expect(mocks.createBatch).toHaveBeenCalledOnce()
    expect(mocks.uploadFile.mock.calls.map((call) => call[2])).toEqual([
      'first.txt',
      'second.txt',
      'second.txt',
    ])
    expect(wrapper.emitted('pending')?.at(-1)).toEqual([false])
    const latest = wrapper.emitted('update:modelValue')?.at(-1)?.[0] as { members: unknown[] }
    expect(latest.members).toHaveLength(2)
    wrapper.unmount()
  })

  it('does not replay an unknown upload when the member is absent from one batch snapshot', async () => {
    mocks.uploadFile.mockRejectedValue(new TypeError('连接断开'))
    const wrapper = mount(ProjectResourceBatchUploader, {
      attachTo: document.body,
      props: { modelValue: null },
    })
    await flushPromises()
    await selectFiles(wrapper, [new File(['one'], 'unknown.txt')])
    expect(mocks.getBatch).toHaveBeenCalledWith('batch-1')
    expect(wrapper.text()).toContain('结果未确认')
    expect(wrapper.emitted('pending')?.at(-1)).toEqual([true])
    expect(wrapper.findAll('button').some((button) => button.text() === '补传')).toBe(false)
    await wrapper
      .findAll('button')
      .find((button) => button.text() === '核对批次')!
      .trigger('click')
    await flushPromises()
    expect(mocks.uploadFile).toHaveBeenCalledOnce()
    expect(wrapper.emitted('pending')?.at(-1)).toEqual([true])
    wrapper.unmount()
  })

  it('以随机 uploadId 核对未知上传，保留 File 且禁止整文件读取或重传', async () => {
    const file = new File(['one'], 'confirmed.txt')
    const arrayBuffer = vi.spyOn(file, 'arrayBuffer').mockRejectedValue(new Error('禁止整文件读取'))
    mocks.uploadFile.mockRejectedValue(new TypeError('响应断开'))
    const session = useProjectResourceBatch()
    await expect(session.uploadFiles([file])).rejects.toThrow('响应断开')
    const upload = session.uploads.value[0]!
    expect(upload.file).toBe(file)
    expect(upload.uploadId).toMatch(/^prm_[a-f0-9]{32}$/)
    expect(mocks.uploadFile).toHaveBeenCalledWith('batch-1', file, file.name, undefined, undefined, upload.uploadId)
    const member = { id: 'another-upload', path: file.name, source: 'file', kind: 'file', size_bytes: 3, sha256: 'a'.repeat(64) }
    mocks.getBatch.mockResolvedValue({ ...emptyBatch, resource_count: 1, total_bytes: 3, members: [member] })
    await expect(session.checkBatch()).rejects.toThrow('仍有操作结果未确认')
    expect(session.uploads.value[0]!.file).toBe(file)
    await session.retryUpload(upload)
    session.removeUpload(upload)
    expect(session.pending.value).toBe(true)
    mocks.getBatch.mockResolvedValue({ ...emptyBatch, resource_count: 1, total_bytes: 3, members: [{ ...member, id: upload.uploadId }] })
    await session.checkBatch()
    expect(session.pending.value).toBe(false)
    expect(arrayBuffer).not.toHaveBeenCalled()
    expect(mocks.uploadFile).toHaveBeenCalledOnce()
  })

  it('添加混合大小文件保留已有成员未保存路径，服务端明确拒绝仅移除当前项', async () => {
    const member: ProjectResourceBatchMember = { id: 'existing', path: 'existing.bin', kind: 'file', source: 'file', size_bytes: 3, sha256: 'a'.repeat(64) }
    const wrapper = mount(ProjectResourceBatchUploader, { attachTo: document.body, props: { modelValue: { ...emptyBatch, state: 'open', resource_count: 1, total_bytes: 3, members: [member] } } })
    try {
      await flushPromises()
      const input = wrapper.get<HTMLInputElement>('[aria-label="资源相对路径"]')
      await input.setValue('unsaved.bin')
      mocks.uploadFile.mockResolvedValueOnce({ ...member, id: 'new', path: 'new.bin' })
        .mockRejectedValueOnce(new ApiError('文件过大', 413, { detail: { code: 'project_resource_file_too_large', category: 'file', limit_bytes: 500_000_000 } }))
      await selectFiles(wrapper, [new File(['new'], 'new.bin'), new File(['bad'], 'rejected.bin'), sizedFile('oversized.bin', 500_000_001)])
      expect(input.element.value).toBe('unsaved.bin')
      expect(wrapper.emitted('pending')?.at(-1)).toEqual([true])
      expect(wrapper.find('.resource-pending-list').exists()).toBe(false)
      expect(document.body.querySelectorAll('[role="dialog"]')).toHaveLength(1)
      expect(mocks.showToast).not.toHaveBeenCalled()
      const latest = wrapper.emitted('update:modelValue')?.at(-1)?.[0] as { members: { path: string }[] }
      expect(latest.members.map((item) => item.path)).toEqual(['existing.bin', 'new.bin'])
    } finally { wrapper.unmount() }
  })

  it('retains the pending file and batch if cancellation is rejected', async () => {
    mocks.uploadFile.mockRejectedValue(new ApiError('拒绝文件', 422, {}))
    mocks.cancelBatch.mockRejectedValueOnce(new ApiError('清理失败', 503, {}))
    const wrapper = mount(ProjectResourceBatchUploader, {
      attachTo: document.body,
      props: { modelValue: null },
    })
    await flushPromises()
    await selectFiles(wrapper, [new File(['one'], 'failed.txt')])
    const uploader = wrapper.vm as unknown as { cancel: () => Promise<void> }
    await expect(uploader.cancel()).rejects.toThrow('清理失败')
    expect(wrapper.text()).toContain('failed.txt')
    expect(wrapper.emitted('pending')?.at(-1)).toEqual([true])
    await uploader.cancel()
    await flushPromises()
    expect(wrapper.emitted('update:modelValue')?.at(-1)).toEqual([null])
    expect(wrapper.emitted('pending')?.at(-1)).toEqual([false])
    wrapper.unmount()
  })

  it('uploads arbitrary bytes as a raw File without client UTF-8 decoding', async () => {
    const member = {
      id: 'member-1',
      kind: 'file',
      path: 'image.png',
      size_bytes: 4,
      sha256: 'a'.repeat(64),
      source: 'file',
    }
    mocks.uploadFile.mockResolvedValue(member)
    const wrapper = mount(ProjectResourceBatchUploader, {
      attachTo: document.body,
      props: { modelValue: null },
    })
    await flushPromises()
    const arrayBuffer = vi.spyOn(File.prototype, 'arrayBuffer')
    const file = new File([new Uint8Array([0xff, 0x00, 0x80, 0x01])], 'image.png', {
      type: 'image/png',
    })
    const input = document.body.querySelectorAll<HTMLInputElement>('input[type="file"]')[0]
    Object.defineProperty(input, 'files', { configurable: true, value: [file] })
    input.dispatchEvent(new Event('change', { bubbles: true }))
    await flushPromises()

    expect(mocks.createBatch).toHaveBeenCalledOnce()
    expect(mocks.uploadFile).toHaveBeenCalledWith('batch-1', file, 'image.png', undefined, undefined, expect.stringMatching(/^prm_[a-f0-9]{32}$/))
    expect(arrayBuffer).not.toHaveBeenCalled()
    expect(
      document.body.querySelector<HTMLInputElement>('[aria-label="资源相对路径"]')?.value,
    ).toBe('image.png')
    expect(document.body.textContent).toContain('文件')
    wrapper.unmount()
  })

  it('accepts dropped files on the compact uploader without implicitly expanding ZIP', async () => {
    const file = new File(['zip-bytes'], 'raw.zip')
    mocks.uploadFile.mockResolvedValue({
      id: 'raw-zip', kind: 'file', path: 'raw.zip', size_bytes: file.size,
      sha256: 'c'.repeat(64), source: 'file',
    })
    const wrapper = mount(ProjectResourceBatchUploader, { props: { modelValue: null } })
    await flushPromises()
    await wrapper.trigger('dragover')
    expect(wrapper.classes()).toContain('is-dragging')
    await wrapper.trigger('drop', { dataTransfer: { files: [file] } })
    await flushPromises()
    expect(mocks.uploadFile).toHaveBeenCalledWith('batch-1', file, 'raw.zip', undefined, undefined, expect.stringMatching(/^prm_[a-f0-9]{32}$/))
    expect(mocks.importZip).not.toHaveBeenCalled()
    expect(wrapper.classes()).not.toContain('is-dragging')
    expect(wrapper.find('.resource-member-details > summary').text()).toBe('raw.zip')
    expect(wrapper.find('.resource-member-details').attributes('open')).toBeUndefined()
    wrapper.unmount()
  })

  it('uses explicit server ZIP import and supports member patch and delete', async () => {
    const zipMember = {
      id: 'member-zip',
      kind: 'note',
      path: 'docs/readme.txt',
      size_bytes: 12,
      sha256: 'b'.repeat(64),
      source: 'zip',
    }
    const importedBatch = {
      ...emptyBatch,
      resource_count: 1,
      total_bytes: 12,
      members: [zipMember],
    }
    mocks.importZip.mockResolvedValue(importedBatch)
    mocks.patchMember.mockResolvedValue({ ...zipMember, path: 'docs/renamed.txt' })
    const wrapper = mount(ProjectResourceBatchUploader, {
      attachTo: document.body,
      props: { modelValue: null },
    })
    await flushPromises()
    const zip = new File(['zip-bytes'], 'bundle.zip', { type: 'application/zip' })
    const input = document.body.querySelectorAll<HTMLInputElement>('input[type="file"]')[1]
    Object.defineProperty(input, 'files', { configurable: true, value: [zip] })
    input.dispatchEvent(new Event('change', { bubbles: true }))
    await flushPromises()

    expect(mocks.importZip).toHaveBeenCalledWith('batch-1', zip)
    expect(document.body.textContent).toContain('ZIP 成员')

    const pathInput = document.body.querySelector<HTMLInputElement>('[aria-label="资源相对路径"]')!
    pathInput.value = 'docs/renamed.txt'
    pathInput.dispatchEvent(new Event('input', { bubbles: true }))
    expect(wrapper.emitted('pending')?.at(-1)).toEqual([true])
    expect(mocks.patchMember).not.toHaveBeenCalled()
    pathInput.dispatchEvent(new FocusEvent('blur', { bubbles: true }))
    await flushPromises()
    expect(mocks.patchMember).toHaveBeenCalledWith('batch-1', 'member-zip', {
      path: 'docs/renamed.txt',
    })
    expect(wrapper.emitted('pending')?.at(-1)).toEqual([false])

    expect(wrapper.find('.resource-member-details > summary').text()).toBe('docs/renamed.txt')
    document.body.querySelector<HTMLButtonElement>('button[aria-label="移除待导入资料 docs/renamed.txt"]')?.click()
    await flushPromises()
    expect(mocks.deleteMember).toHaveBeenCalledWith('batch-1', 'member-zip')
    expect(wrapper.findAll('.resource-batch-member')).toHaveLength(0)
    wrapper.unmount()
  })
})
