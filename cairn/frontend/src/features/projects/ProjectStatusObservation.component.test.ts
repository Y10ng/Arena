import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, describe, expect, it } from 'vitest'
import type { VueWrapper } from '@vue/test-utils'
import ProjectStatusObservation from './ProjectStatusObservation.vue'
import { runtimeV2Fixture } from './projectRuntimeFixtures'

let wrapper: VueWrapper
const now = Date.now()
const runtime = () =>
  runtimeV2Fixture({
    project_status: 'stopped',
    state: 'waiting',
    code: 'cleanup_pending',
    message: '<b>资源清理尚未确认</b>',
    observed_at: new Date(now).toISOString(),
  })
function render(overrides = {}) {
  wrapper = mount(ProjectStatusObservation, {
    attachTo: document.body,
    props: {
      project: { id: 'p1', status: 'stopped' },
      runtime: runtime(),
      failed: false,
      fetching: false,
      now,
      dataUpdatedAt: now,
      ...overrides,
    },
  })
  return wrapper
}
async function open() {
  await wrapper.get('button[aria-label="运行详情"]').trigger('click')
  await flushPromises()
  return document.body.querySelector('.project-observation-details')!
}
afterEach(() => {
  wrapper?.unmount()
  document.body.innerHTML = ''
})

describe('R2 shared status observation', () => {
  it('shows the pause policy only for ordinary projects and keeps timeout separate from physical stop', async () => {
    render({ project: { id: 'p1', status: 'paused' }, runtime: undefined })
    expect((await open()).textContent).toContain('暂停满12小时将自动结束，工作文件保留')
    await wrapper.setProps({ project: { id: 'p1', status: 'paused', verification_request_id: 'verification-1' } })
    expect(document.body.querySelector('.project-observation-details')!.textContent).not.toContain('暂停满12小时')
    await wrapper.setProps({ project: { id: 'p1', status: 'stopped', coverage_stop_reason: 'pause_timeout', execution_control: { action: 'finish', state: 'requested' } } })
    expect(wrapper.text()).toContain('暂停超时')
    expect(document.body.querySelector('.project-observation-details')!.textContent).toContain('已自动请求结束')
    expect(wrapper.text()).toContain('物理终止待确认')
    expect(wrapper.text()).not.toContain('已完成')
    expect(document.body.querySelector('.project-observation-details')!.textContent).toContain('此操作不关闭周期计划')
  })

  it('keeps ended status separate from cleanup and renders escaped raw diagnostics', async () => {
    render()
    expect(wrapper.get('[aria-label="项目状态"]').text()).toBe('已结束')
    expect(wrapper.text()).toContain('资源清理待确认')
    const details = await open()
    expect(details.textContent).toContain('<b>资源清理尚未确认</b>')
    expect(details.querySelector('b')).toBeNull()
    expect(details.textContent).toContain('cleanup_pending')
    expect(details.textContent).toContain('进程退出确认未确认')
    expect(details.textContent).toContain('资源清理确认未确认')
    ;(details.querySelector('button') as HTMLButtonElement).click()
    expect(wrapper.emitted('refresh')).toHaveLength(1)
  })

  it('retains same-project history on read failure but never shows another project', async () => {
    render()
    await wrapper.setProps({ failed: true })
    expect(wrapper.text()).toContain('观测未更新')
    let details = await open()
    expect(details.textContent).toContain('历史观测时间')
    expect(details.textContent).toContain('显示上次读取内容')
    expect(details.textContent).toContain('cleanup_pending')
    await wrapper.setProps({ project: { id: 'p2', status: 'completed' } })
    await flushPromises()
    expect(document.body.querySelector('.project-observation-details')).toBeNull()
    expect(wrapper.text()).toContain('已完成')
    details = await open()
    expect(details.textContent).not.toContain('cleanup_pending')
    expect(details.textContent).not.toContain('资源清理尚未确认')
  })

  it('separates initial loading, initial failure and missing observation', async () => {
    render({ runtime: undefined, fetching: true })
    expect(wrapper.text()).toContain('观测加载中')
    await wrapper.setProps({ fetching: false, failed: true })
    expect(wrapper.text()).toContain('观测读取失败')
    expect((await open()).querySelector('dl')).toBeNull()
    await wrapper.setProps({ failed: false })
    expect(wrapper.text()).toContain('暂无运行观测')
  })

  it('marks locally expired, inconsistent and unsupported snapshots without comparing server clocks', async () => {
    render()
    await wrapper.setProps({ now: now + 60_001 })
    expect(wrapper.text()).toContain('观测已失效')
    expect((await open()).textContent).toContain('历史观测时间')
    await wrapper.setProps({ now: now - 8000 })
    expect(wrapper.text()).toContain('资源清理待确认')
    await wrapper.setProps({
      runtime: { ...runtime(), observed_at: new Date(Date.now() + 120_000).toISOString() },
    })
    expect(wrapper.text()).toContain('资源清理待确认')
    await wrapper.setProps({ now, runtime: { ...runtime(), project_status: 'active' } })
    expect(wrapper.text()).toContain('观测与项目状态不一致')
    await wrapper.setProps({ runtime: { ...runtime(), schema_version: 99 } })
    expect(wrapper.text()).toContain('观测版本不支持')
    expect(document.body.querySelector('.project-observation-details dl')).toBeNull()
  })

  it('does not infer failure or success from empty runs and unconfirmed v2 axes', async () => {
    render({
      project: { id: 'p1', status: 'completed' },
      runtime: runtimeV2Fixture({
        project_status: 'completed',
        state: 'completed',
        code: 'project_completed',
      }),
    })
    expect(wrapper.text()).toBe('已完成')
    expect((await open()).textContent).toContain('资源清理确认未确认')
    await wrapper.setProps({
      project: { id: 'p1', status: 'paused' },
      runtime: runtimeV2Fixture({
        project_status: 'paused',
        state: 'pausing',
        code: 'project_pause_draining',
      }),
    })
    expect(wrapper.text()).toContain('已暂停')
    expect(wrapper.text()).toContain('物理暂停待确认')
  })

  it('shows request acceptance before a physical receipt and reads exact resources in the same popover', async () => {
    const requested_at = '2026-11-02T00:00:00Z'
    const confirmed_at = '2026-11-02T00:00:01Z'
    const execution_control = {
      action: 'pause' as const,
      state: 'requested' as const,
      requested_at,
      confirmed_at: null,
      resources: [
        {
          kind: 'research',
          container_id: 'exact-container',
          task_run_id: 'same-task',
          exec_id: 'same-exec',
          state: 'paused',
        },
      ],
    }
    render({
      project: { id: 'p1', status: 'paused', execution_control },
      runtime: undefined,
      failed: true,
    })
    expect(wrapper.get('[aria-label="项目状态"]').text()).toBe('正在暂停')
    expect(wrapper.text()).toContain('物理暂停待确认')
    expect(wrapper.text()).toContain('观测读取失败')
    let details = await open()
    expect(details.textContent).toContain(requested_at)
    expect(details.textContent).toContain('物理确认时间未确认')
    expect(details.textContent).toContain('不代表本次控制已生效')
    expect(details.textContent).toContain('exact-container')
    expect(details.textContent).toContain('same-task')
    expect(details.textContent).toContain('same-exec')
    await wrapper.setProps({
      project: {
        id: 'p1',
        status: 'paused',
        execution_control: { ...execution_control, state: 'paused', confirmed_at },
      },
    })
    expect(wrapper.get('[aria-label="项目状态"]').text()).toBe('已暂停')
    details = document.body.querySelector('.project-observation-details')!
    expect(details.textContent).toContain('物理暂停已确认')
    expect(details.textContent).toContain(confirmed_at)
    expect(details.textContent).not.toContain('不代表本次控制已生效')
  })

  it.each([
    ['resume', 'running', 'active', '原地恢复已确认'],
    ['finish', 'stopped', 'stopped', '所属执行终止已确认'],
  ])(
    'displays the physical %s receipt without requiring a runtime query',
    async (action, state, status, text) => {
      render({
        runtime: undefined,
        project: {
          id: 'p1',
          status,
          execution_control: {
            action,
            state,
            requested_at: '2026-11-02T00:00:00Z',
            confirmed_at: '2026-11-02T00:00:01Z',
            resources: [{ container_id: 'original-container', state }],
          },
        },
      })
      expect((await open()).textContent).toContain(text)
    },
  )

  it.each(['failed', 'lost'])(
    'keeps %s visible without leaking raw control errors',
    async (state) => {
      render({
        runtime: undefined,
        project: {
          id: 'p1',
          status: 'paused',
          execution_control: {
            action: 'resume',
            state,
            requested_at: '2026-11-02T00:00:00Z',
            error: '/private/token=canary',
          },
        },
      })
      expect(wrapper.get('[aria-label="项目状态"]').text()).toBe('恢复失败')
      const details = await open()
      expect(details.textContent).toContain('物理状态未确认')
      expect(details.textContent).not.toContain('canary')
      expect(wrapper.text()).not.toContain('canary')
    },
  )
})
