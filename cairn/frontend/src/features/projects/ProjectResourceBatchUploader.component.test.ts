import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import ProjectResourceBatchUploader from './ProjectResourceBatchUploader.vue'

const mocks = vi.hoisted(() => ({
  createBatch: vi.fn(),
  deleteMember: vi.fn(),
  getConfig: vi.fn(),
  importZip: vi.fn(),
  patchMember: vi.fn(),
  uploadFile: vi.fn(),
  cancelBatch: vi.fn(),
  showToast: vi.fn(),
}))

vi.mock('@/api/client', () => ({
  cancelProjectResourceBatch: mocks.cancelBatch,
  createProjectResourceBatch: mocks.createBatch,
  deleteProjectResourceBatchMember: mocks.deleteMember,
  getProjectResourceConfig: mocks.getConfig,
  importProjectResourceBatchZip: mocks.importZip,
  updateProjectResourceBatchMember: mocks.patchMember,
  uploadProjectResourceBatchFile: mocks.uploadFile,
}))

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
  mocks.uploadFile.mockReset()
  mocks.importZip.mockReset()
  mocks.patchMember.mockReset()
  mocks.deleteMember.mockReset().mockResolvedValue(undefined)
  mocks.cancelBatch.mockReset().mockResolvedValue(undefined)
  mocks.showToast.mockReset()
})

describe('ProjectResourceBatchUploader', () => {
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
    pathInput.dispatchEvent(new FocusEvent('blur', { bubbles: true }))
    await flushPromises()
    expect(mocks.patchMember).toHaveBeenCalledWith('batch-1', 'member-zip', {
      path: 'docs/renamed.txt',
    })

    document.body.querySelector<HTMLButtonElement>('button[aria-label="移除待导入资料"]')?.click()
    await flushPromises()
    expect(mocks.deleteMember).toHaveBeenCalledWith('batch-1', 'member-zip')
    expect(document.body.textContent).toContain('未添加辅助资料')
    wrapper.unmount()
  })
})
