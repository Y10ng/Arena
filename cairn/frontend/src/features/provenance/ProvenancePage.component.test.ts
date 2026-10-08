import { flushPromises, mount } from '@vue/test-utils'
import { QueryClient, VueQueryPlugin } from '@tanstack/vue-query'
import { reactive } from 'vue'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import Select from '@/components/ui/Select.vue'
import ProvenancePage from './ProvenancePage.vue'

const mocks = vi.hoisted(() => ({
  api: vi.fn(),
  listVulnerabilities: vi.fn(),
  push: vi.fn(),
  replace: vi.fn(),
}))

vi.mock('@/api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/client')>()
  return {
    ...actual,
    api: mocks.api,
    listVulnerabilities: mocks.listVulnerabilities,
  }
})

const route = reactive({
  query: {} as Record<string, string | undefined>,
})

vi.mock('vue-router', () => ({
  useRoute: () => route,
  useRouter: () => ({ push: mocks.push, replace: mocks.replace }),
}))

const projects = [
  { id: 'project-a', title: '项目 A' },
  { id: 'project-b', title: '项目 B' },
]

function mountPage() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  })
  return mount(ProvenancePage, {
    attachTo: document.body,
    global: {
      plugins: [[VueQueryPlugin, { queryClient }]],
    },
  })
}

beforeEach(() => {
  route.query = {}
  mocks.api.mockReset().mockImplementation((_method: string, path: string) => {
    if (path === '/projects') return Promise.resolve(projects)
    return Promise.reject(new Error(`未配置请求：${path}`))
  })
  mocks.listVulnerabilities.mockReset().mockResolvedValue([])
  mocks.push.mockReset()
  mocks.replace.mockReset()
  document.body.innerHTML = ''
})

afterEach(() => {
  document.body.innerHTML = ''
})

describe('ProvenancePage project selection', () => {
  it('loads projects first and waits for a project before loading findings', async () => {
    const wrapper = mountPage()
    await flushPromises()

    expect(mocks.api).toHaveBeenCalledWith('GET', '/projects', undefined, expect.any(AbortSignal))
    expect(mocks.listVulnerabilities).not.toHaveBeenCalled()
    expect(wrapper.text()).toContain('先选择项目')
    expect(wrapper.text()).not.toContain(
      '从成果反向追踪发现、运行操作、终端日志、证据绑定与独立验证。',
    )

    wrapper.unmount()
  })

  it('loads only the selected project findings and shows its empty state', async () => {
    const wrapper = mountPage()
    await flushPromises()

    wrapper.findComponent(Select).vm.$emit('update:modelValue', 'project-b')
    await flushPromises()

    expect(mocks.listVulnerabilities).toHaveBeenCalledTimes(1)
    expect(mocks.listVulnerabilities).toHaveBeenCalledWith(
      'project-b',
      undefined,
      undefined,
      expect.any(AbortSignal),
    )
    expect(wrapper.text()).toContain('当前项目暂无可溯源成果')
    expect(mocks.replace).toHaveBeenLastCalledWith(
      expect.objectContaining({
        query: expect.objectContaining({ projectId: 'project-b' }),
      }),
    )

    wrapper.unmount()
  })

  it('restores only a valid project deep link', async () => {
    route.query = { projectId: 'project-b', vulnerabilityId: 'missing-finding' }
    const wrapper = mountPage()
    await flushPromises()

    expect(mocks.listVulnerabilities).toHaveBeenCalledWith(
      'project-b',
      undefined,
      undefined,
      expect.any(AbortSignal),
    )
    expect(wrapper.findComponent(Select).props('modelValue')).toBe('project-b')

    wrapper.unmount()
  })

  it('clears an invalid project deep link without requesting its findings', async () => {
    route.query = { projectId: 'project-missing', vulnerabilityId: 'finding-old' }
    const wrapper = mountPage()
    await flushPromises()

    expect(mocks.listVulnerabilities).not.toHaveBeenCalled()
    expect(mocks.replace).toHaveBeenLastCalledWith(
      expect.objectContaining({
        query: expect.objectContaining({
          projectId: undefined,
          vulnerabilityId: undefined,
          observationId: undefined,
          runId: undefined,
        }),
      }),
    )

    wrapper.unmount()
  })

  it('keeps long finding text available while opting into the bounded layout', async () => {
    const longTitle = `超长成果标题${'绕过验证链路'.repeat(20)}`
    const longTarget = `https://example.test/${'nested-segment/'.repeat(20)}`
    mocks.listVulnerabilities.mockResolvedValue([
      {
        id: 'finding-long',
        project_id: 'project-a',
        title: longTitle,
        severity: 'high',
        target: longTarget,
        vuln_type: 'xss',
      },
      {
        id: 'finding-second',
        project_id: 'project-a',
        title: '第二个成果',
        severity: 'medium',
        target: 'second.example.test',
        vuln_type: 'ssrf',
      },
    ])

    route.query = { projectId: 'project-a' }
    const wrapper = mountPage()
    await flushPromises()

    expect(wrapper.get('.master-detail').classes()).toContain('master-detail--fill-height')
    expect(wrapper.get('.provenance-finding__title').text()).toBe(longTitle)
    expect(wrapper.get('.provenance-finding__title').attributes('title')).toBe(longTitle)
    expect(wrapper.get('.provenance-finding__meta').text()).toBe(longTarget)
    expect(wrapper.get('.provenance-finding__meta').attributes('title')).toBe(longTarget)

    const detail = wrapper.get<HTMLElement>('.provenance-detail')
    detail.element.scrollTop = 240
    await wrapper.findAll('.provenance-finding')[1]?.trigger('click')
    await flushPromises()
    expect(detail.element.scrollTop).toBe(0)

    wrapper.unmount()
  })

  it('uses one coordinated chain surface and preserves observation and run selection', async () => {
    mocks.listVulnerabilities.mockResolvedValue([
      {
        id: 'finding-a',
        project_id: 'project-a',
        title: '测试成果',
        severity: 'high',
        target: 'example.test',
        vuln_type: 'xss',
      },
    ])
    mocks.api.mockImplementation((_method: string, path: string) => {
      if (path === '/projects') return Promise.resolve(projects)
      if (path.includes('/vulnerabilities/finding-a/provenance'))
        return Promise.resolve({
          vulnerability: {
            id: 'finding-a',
            project_id: 'project-a',
            title: '测试成果',
            severity: 'high',
            target: 'example.test',
            vuln_type: 'xss',
          },
          observations: [
            {
              id: 'observation-a',
              task_run_id: 'run-a',
              observed_at: '2026-08-24T00:00:00Z',
              worker: 'worker-a',
            },
            {
              id: 'observation-b',
              task_run_id: 'run-b',
              observed_at: '2026-08-25T00:00:00Z',
              worker: 'worker-b',
            },
          ],
          verifications: [
            {
              id: 'verification-a',
              outcome: 'confirmed',
              verifier_worker: 'verifier',
              verifier_task_run_id: 'run-a',
            },
          ],
          facts: [],
          intents: [],
          task_runs: [
            {
              id: 'run-a',
              worker_name: 'worker-a',
              task_type: 'explore',
              status: 'succeeded',
            },
            {
              id: 'run-b',
              worker_name: 'worker-b',
              task_type: 'reason',
              status: 'succeeded',
            },
          ],
          artifacts: [],
          evidence: [],
          relations: [],
          completeness_warnings: [],
        })
      if (
        path.includes('/agent-events') ||
        path.includes('/command-events') ||
        path.includes('/hook-events') ||
        path.includes('/prompt-events') ||
        path.includes('/result-events') ||
        path.includes('/artifacts?task_run_id=')
      )
        return Promise.resolve([])
      return Promise.reject(new Error(`未配置请求：${path}`))
    })
    route.query = {
      projectId: 'project-a',
      vulnerabilityId: 'finding-a',
      observationId: 'observation-a',
      runId: 'run-a',
    }

    const wrapper = mountPage()
    await flushPromises()

    expect(wrapper.findAll('.provenance-chain-card')).toHaveLength(1)
    expect(wrapper.findAll('.provenance-chain-section')).toHaveLength(2)
    const observations = wrapper.findAll('.provenance-observation')
    const runs = wrapper.findAll('.provenance-run-option')
    expect(observations).toHaveLength(2)
    expect(runs).toHaveLength(2)
    expect(observations[0]?.attributes('aria-pressed')).toBe('true')
    expect(runs[0]?.attributes('aria-pressed')).toBe('true')

    await observations[1]?.trigger('click')
    await flushPromises()
    expect(observations[1]?.attributes('aria-pressed')).toBe('true')
    expect(runs[1]?.attributes('aria-pressed')).toBe('true')
    expect(mocks.replace).toHaveBeenLastCalledWith(
      expect.objectContaining({
        query: expect.objectContaining({
          projectId: 'project-a',
          vulnerabilityId: 'finding-a',
          observationId: 'observation-b',
          runId: 'run-b',
        }),
      }),
    )

    await runs[0]?.trigger('click')
    await flushPromises()
    expect(observations[1]?.attributes('aria-pressed')).toBe('true')
    expect(runs[0]?.attributes('aria-pressed')).toBe('true')

    wrapper.unmount()
  })

  it('renders raw hook values as inert text', async () => {
    const rawValue = '<img src=x onerror="globalThis.compromised=true">'
    mocks.listVulnerabilities.mockResolvedValue([
      {
        id: 'finding-a',
        project_id: 'project-a',
        title: '测试成果',
        severity: 'high',
        target: 'example.test',
        vuln_type: 'xss',
      },
    ])
    mocks.api.mockImplementation((_method: string, path: string) => {
      if (path === '/projects') return Promise.resolve(projects)
      if (path.includes('/vulnerabilities/finding-a/provenance'))
        return Promise.resolve({
          vulnerability: {
            id: 'finding-a',
            project_id: 'project-a',
            title: '测试成果',
            severity: 'high',
            target: 'example.test',
            vuln_type: 'xss',
          },
          observations: [],
          verifications: [],
          facts: [],
          intents: [],
          task_runs: [
            {
              id: 'run-a',
              worker_name: 'claude-code',
              task_type: 'explore',
              status: 'succeeded',
            },
          ],
          artifacts: [],
          evidence: [],
          relations: [],
          completeness_warnings: [],
        })
      if (path.endsWith('/agent-events')) return Promise.resolve([])
      if (path.includes('/command-events')) return Promise.resolve([])
      if (path.includes('/prompt-events')) return Promise.resolve([])
      if (path.includes('/result-events')) return Promise.resolve([])
      if (path.includes('/artifacts?task_run_id=')) return Promise.resolve([])
      if (path.includes('/hook-events'))
        return Promise.resolve([
          {
            task_run_id: 'run-a',
            events: [
              {
                id: 'hook-a',
                task_run_id: 'run-a',
                project_id: 'project-a',
                ts: '2026-08-25T00:00:00Z',
                recorded_at: '2026-08-25T00:00:00Z',
                phase: 'explore_execute',
                kind: 'hook_event',
                session_id: 'session-a',
                tool_name: 'Bash',
                hook_event_name: 'PreToolUse',
                operation_id: 'operation-a',
                status: 'allowed',
                tool_input: { command: rawValue },
                tool_response: {},
                risk_level: 'low',
                summary: rawValue,
              },
            ],
          },
        ])
      return Promise.reject(new Error(`未配置请求：${path}`))
    })
    route.query = { projectId: 'project-a' }

    const wrapper = mountPage()
    await flushPromises()

    expect(wrapper.find('.provenance-json').text()).toContain('<img src=x')
    expect(wrapper.find('.provenance-json').text()).toContain('globalThis.compromised=true')
    expect(wrapper.find('.provenance-json img').exists()).toBe(false)
    expect(document.querySelector('img[onerror]')).toBeNull()

    wrapper.unmount()
  })
})
