import { flushPromises, mount } from '@vue/test-utils'
import { QueryClient, VueQueryPlugin } from '@tanstack/vue-query'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiError } from '@/api/client'
import type { VerificationSummary } from '@/api/verificationRequests'
import VerificationActions from './VerificationActions.vue'

const mocks = vi.hoisted(() => ({ remove: vi.fn(), download: vi.fn() }))
vi.mock('@/api/verificationRequests', async (importOriginal) => ({
  ...await importOriginal<typeof import('@/api/verificationRequests')>(),
  deleteVerificationRequest: mocks.remove,
  downloadVerificationReport: mocks.download,
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
})
afterEach(() => {
  wrappers.splice(0).forEach((wrapper) => wrapper.unmount())
  clients.splice(0).forEach((client) => client.clear())
  document.body.innerHTML = ''
})

describe('复测永久删除操作', () => {
  it('详情菜单不重复再次复测，执行未止的删除原因可见', async () => {
    render({ ...summary, can_delete: false, delete_block_reason: '执行环境尚未回收' }, 'detail')
    await openMenu()
    expect(document.body.textContent).not.toContain('再次复测')
    const remove = menuItem('删除')
    expect(remove.getAttribute('data-disabled')).not.toBeNull()
    expect(remove.textContent).toContain('执行环境尚未回收')
    remove.click()
    await flushPromises()
    expect(mocks.remove).not.toHaveBeenCalled()
  })

  it('严格清理失败不移除行，重读待清理投影，只有成功后才通知删除', async () => {
    const { wrapper, invalidate } = render()
    mocks.remove.mockRejectedValueOnce(new ApiError('文件清理失败，可重试', 503, null)).mockResolvedValueOnce(undefined)
    await openMenu()
    menuItem('删除').click()
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
    expect(menuItem('重试清理').getAttribute('data-disabled')).toBeNull()
    menuItem('再次复测').click()
    menuItem('导出报告').click()
    await flushPromises()
    expect(wrapper.emitted('rerun')).toBeUndefined()
    expect(mocks.download).not.toHaveBeenCalled()
  })
})
