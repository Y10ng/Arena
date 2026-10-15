import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import ProjectResourceBatchUploader from './ProjectResourceBatchUploader.vue'
import { ApiError } from '@/api/client'

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

describe('ProjectResourceBatchUploader', () => {
  const selectFiles = async (wrapper: ReturnType<typeof mount>, files: File[]) => {
    const input = wrapper.find<HTMLInputElement>('input[type="file"]')
    Object.defineProperty(input.element, 'files', { configurable: true, value: files })
    await input.trigger('change')
    await flushPromises()
  }

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

  it('accepts an unknown file upload only after batch membership and original-byte digest agree', async () => {
    const file = new File(['one'], 'confirmed.txt')
    const arrayBuffer = vi
      .spyOn(file, 'arrayBuffer')
      .mockResolvedValue(new Uint8Array([1, 2, 3]).buffer)
    const digest = vi.fn().mockResolvedValue(new Uint8Array(32).fill(0xaa).buffer)
    vi.stubGlobal('crypto', { subtle: { digest } })
    mocks.uploadFile.mockRejectedValue(new TypeError('响应断开'))
    mocks.getBatch.mockResolvedValue({
      ...emptyBatch,
      resource_count: 1,
      total_bytes: 3,
      members: [
        {
          id: 'confirmed',
          path: file.name,
          source: 'file',
          kind: 'file',
          size_bytes: 3,
          sha256: 'aa'.repeat(32),
        },
      ],
    })
    const wrapper = mount(ProjectResourceBatchUploader, {
      attachTo: document.body,
      props: { modelValue: null },
    })
    try {
      await flushPromises()
      await selectFiles(wrapper, [file])
      expect(arrayBuffer).toHaveBeenCalledOnce()
      expect(digest).toHaveBeenCalledWith('SHA-256', new Uint8Array([1, 2, 3]).buffer)
      expect(wrapper.emitted('pending')?.at(-1)).toEqual([false])
      expect(wrapper.text()).toContain('已核对临时批次')
      expect(mocks.uploadFile).toHaveBeenCalledOnce()
    } finally {
      wrapper.unmount()
      vi.unstubAllGlobals()
    }
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
    expect(mocks.uploadFile).toHaveBeenCalledWith('batch-1', file, 'image.png')
    expect(arrayBuffer).not.toHaveBeenCalled()
    expect(
      document.body.querySelector<HTMLInputElement>('[aria-label="资源相对路径"]')?.value,
    ).toBe('image.png')
    expect(document.body.textContent).toContain('文件')
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

    document.body.querySelector<HTMLButtonElement>('button[aria-label="移除待导入资料"]')?.click()
    await flushPromises()
    expect(mocks.deleteMember).toHaveBeenCalledWith('batch-1', 'member-zip')
    expect(document.body.textContent).toContain('未添加辅助资料')
    wrapper.unmount()
  })
})
