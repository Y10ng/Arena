import { flushPromises, mount } from '@vue/test-utils'
import { QueryClient, VueQueryPlugin } from '@tanstack/vue-query'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiError } from '@/api/client'
import type { VerificationSummary } from '@/api/verificationRequests'
import VerificationActions from './VerificationActions.vue'

const mocks = vi.hoisted(() => ({ remove: vi.fn(), download: vi.fn(), read: vi.fn(), detail: vi.fn(), create: vi.fn() }))
vi.mock('@/api/verificationRequests', async (importOriginal) => ({
  ...await importOriginal<typeof import('@/api/verificationRequests')>(),
  deleteVerificationRequest: mocks.remove,
  getVerificationRequest: mocks.detail,
}))
vi.mock('@/features/reports/reportExport', async (importOriginal) => ({
  ...await importOriginal<typeof import('@/features/reports/reportExport')>(),
  fetchReportMarkdown: mocks.read,
  createReportExport: mocks.create,
  downloadReportBlob: mocks.download,
}))

const summary: VerificationSummary = {
  schema_version: 1, id: 'round-a', request_id: 'request-a', title: '此轮复测', external_id: null,
  mode: 'retest', source: { type: 'user', id: 'user-a', name: '内部用户' }, input_source: null,
  execution_status: 'completed', can_delete: true, delete_block_reason: null,
  purge_state: null, purge_error: null, result: null, error: null,
  created_at: '2026-10-16T00:00:00Z', updated_at: '2026-10-16T00:00:00Z',
}
const wrappers: ReturnType<typeof mount>[] = []
const clients: QueryClient[] = []
function render(request = summary, context: 'list' | 'detail' = 'list') {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  clients.push(client)
  const invalidate = vi.spyOn(client, 'invalidateQueries')
  const wrapper = mount(VerificationActions, {
    attachTo: document.body,
    props: { request, context },
    global: { plugins: [[VueQueryPlugin, { queryClient: client }]] },
  })
  wrappers.push(wrapper)
  return { wrapper, invalidate }
}
function button(label: string) {
  const match = [...document.querySelectorAll<HTMLButtonElement>('button')].find((node) => node.textContent?.trim() === label)
  if (!match) throw new Error(`找不到按钮：${label}`)
  return match
}
function menuItem(label: string) {
  const match = [...document.querySelectorAll<HTMLElement>('[role="menuitem"]')].find((node) => node.textContent?.trim().startsWith(label))
  if (!match) throw new Error(`找不到菜单项：${label}`)
  return match
}
async function openMenu() {
  document.querySelector<HTMLButtonElement>('[data-verification-actions]')!.click()
  await flushPromises()
}
beforeEach(() => {
  HTMLElement.prototype.hasPointerCapture = () => false
  HTMLElement.prototype.setPointerCapture = () => undefined
  HTMLElement.prototype.releasePointerCapture = () => undefined
  mocks.remove.mockReset()
  mocks.download.mockReset()
  mocks.read.mockReset()
  mocks.detail.mockReset()
  mocks.create.mockReset().mockResolvedValue(new Blob(['报告']))
})
afterEach(() => {
  wrappers.splice(0).forEach((wrapper) => wrapper.unmount())
  clients.splice(0).forEach((client) => client.clear())
  document.body.innerHTML = ''
})

describe('复测永久删除操作', () => {
  it('证据包图片清单失败仍导出正文，切换轮次取消迟到的纯Markdown下载', async () => {
    const result = { schema_version: 4 as const, outcome: 'not_reproduced' as const, summary: '本轮未复现', remediation: '检查资源归属', tested_scope: '合成证据', limitations: [], reason_code: null, evidence_refs: [], evidence_items: [], test_steps: [], key_evidence: [] }
    const { wrapper } = render({ ...summary, result })
    mocks.read.mockResolvedValueOnce('# 本轮正文\n\n```http\n原始报文\n```')
    mocks.detail.mockRejectedValueOnce(new Error('图片清单不可用'))
    await openMenu()
    menuItem('Markdown 证据包').click()
    await flushPromises()
    expect(mocks.read).toHaveBeenCalledWith('/api/v1/verification-requests/round-a/report', expect.any(AbortSignal))
    expect(mocks.detail).toHaveBeenCalledWith('round-a', expect.any(AbortSignal))
    expect(mocks.create).toHaveBeenCalledWith(expect.stringContaining('图片清单读取失败；正文与原文证据仍已保留。'), 'tar', '复测报告-round-a', [])
    expect(mocks.download).toHaveBeenCalledWith(expect.any(Blob), '复测报告-round-a', 'tar')
    let resolve!: (value: string) => void
    mocks.read.mockImplementationOnce(() => new Promise<string>((done) => { resolve = done }))
    await openMenu()
    menuItem('导出报告').click()
    await flushPromises()
    const signal = mocks.read.mock.calls[1]![1] as AbortSignal
    await wrapper.setProps({ request: { ...summary, id: 'round-b', result } })
    expect(signal.aborted).toBe(true)
    resolve('过期正文')
    await flushPromises()
    expect(mocks.download).toHaveBeenCalledTimes(1)
  })

  it('删除入口和禁用原因默认可见，详情菜单不重复再次复测', async () => {
    render({ ...summary, can_delete: false, delete_block_reason: '执行环境尚未回收' }, 'detail')
    const remove = button('删除复测记录')
    expect(remove.disabled).toBe(true)
    expect(document.getElementById(remove.getAttribute('aria-describedby')!)?.textContent).toBe('执行环境尚未回收')
    await openMenu()
    expect(document.body.textContent).not.toContain('再次复测')
    remove.click()
    await flushPromises()
    expect(mocks.remove).not.toHaveBeenCalled()
  })

  it('严格清理失败不移除行，重读待清理投影，只有成功后才通知删除', async () => {
    const { wrapper, invalidate } = render()
    mocks.remove.mockRejectedValueOnce(new ApiError('文件清理失败，可重试', 503, null)).mockResolvedValueOnce(undefined)
    button('删除复测记录').click()
    await flushPromises()
    expect(document.body.textContent).toContain('删除此轮复测及其产生的文件，不影响原漏洞和其他已受理复测；删除后无法恢复。')
    expect(document.body.textContent).not.toContain('底层原件仍保留')
    button('永久删除').click()
    await flushPromises()
    expect(wrapper.emitted('deleted')).toBeUndefined()
    expect(document.body.textContent).toContain('未确认删除成功')
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ['verification-request', 'round-a'], exact: true })
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ['verification-requests'] })
    button('永久删除').click()
    await flushPromises()
    expect(mocks.remove).toHaveBeenCalledTimes(2)
    expect(wrapper.emitted('deleted')).toEqual([['round-a']])
  })

  it('清理未完成时仍能重试，但不能导出或再次复测', async () => {
    const { wrapper } = render({ ...summary, execution_status: 'cleanup_pending', purge_state: 'pending', purge_error: '文件暂时不可删除' })
    await openMenu()
    expect(menuItem('再次复测').getAttribute('data-disabled')).not.toBeNull()
    expect(menuItem('导出报告').getAttribute('data-disabled')).not.toBeNull()
    expect(button('重试清理').disabled).toBe(false)
    menuItem('再次复测').click()
    menuItem('导出报告').click()
    await flushPromises()
    expect(wrapper.emitted('rerun')).toBeUndefined()
    expect(mocks.download).not.toHaveBeenCalled()
  })
})
