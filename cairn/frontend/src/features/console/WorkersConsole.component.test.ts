import { flushPromises, mount } from '@vue/test-utils'
import { ref } from 'vue'
import { afterEach, describe, expect, it, vi } from 'vitest'

const formConfig = ref({
  runtime: {
    interval: 3,
    max_workers: 1,
    max_running_projects: 1,
    max_project_workers: 1,
    healthcheck_timeout: 20,
    prompt_group: 'default',
  },
  tasks: {
    bootstrap: { timeout: 300, conclude_timeout: 90 },
    reason: { timeout: 300, max_intents: 2 },
    explore: { timeout: 900, conclude_timeout: 300 },
  },
  container: {
    image: 'cairn-worker:latest',
    network_mode: 'none',
    completed_action: 'stop',
    allowed_images: [],
    cap_add: [],
    execution_profile: 'shared_local',
  },
  images: [],
  execution_profiles: [],
})
const formDirty = ref(false)
const yamlDirty = ref(true)
const editorYamlContent = ref('runtime:\n  interval: 3\n')

function dispatcherState() {
  return {
    dispatcher: ref({
      project_count: 0,
      active_project_count: 0,
      running_project_count: 0,
      running_task_count: 0,
      queued_task_count: 0,
      heartbeat_stale: false,
      worker_cooldowns: [],
      worker_rejections: [],
      worker_pool: [],
      container_images: [],
      cap_baseline: [],
    }),
    dispatcherYamlPath: ref('/etc/cairn/dispatch.yaml'),
    yamlWritable: ref(true),
    isLoadingSummary: ref(false),
    isSummaryError: ref(false),
    summaryError: ref(null),
    isLoadingConfig: ref(false),
    isConfigError: ref(false),
    configError: ref(null),
    isYamlError: ref(false),
    yamlError: ref(null),
    isRefreshing: ref(false),
    isSavingYaml: ref(false),
    isSavingForm: ref(false),
    formConfig,
    formDirty,
    formReadOnly: ref(false),
    yamlDirty,
    yamlReadOnly: ref(false),
    editorYamlContent,
    refreshAll: vi.fn(),
    saveForm: vi.fn(),
    saveYaml: vi.fn(),
    resetForm: vi.fn(),
    resetYaml: vi.fn(),
    removeItem: vi.fn(),
  }
}

describe('WorkersConsole Tabs migration', () => {
  afterEach(() => {
    vi.restoreAllMocks()
    vi.doUnmock('vue-router')
    vi.doUnmock('@/components/MonacoEditor.vue')
    vi.doUnmock('@/composables/useDispatcherFormConfig')
    vi.resetModules()
    formDirty.value = false
    yamlDirty.value = true
    editorYamlContent.value = 'runtime:\n  interval: 3\n'
  })

  it('preserves dirty YAML confirmation and form-to-YAML serialization', async () => {
    vi.doMock('vue-router', () => ({
      useRoute: () => ({ query: { mode: 'yaml' } }),
    }))
    vi.doMock('@/components/MonacoEditor.vue', () => ({
      default: {
        name: 'MonacoEditor',
        props: ['modelValue'],
        template: '<div data-testid="monaco">{{ modelValue }}</div>',
      },
    }))
    vi.doMock('@/composables/useDispatcherFormConfig', async (importOriginal) => {
      const actual = await importOriginal<typeof import('@/composables/useDispatcherFormConfig')>()
      return {
        ...actual,
        useDispatcherFormConfig: dispatcherState,
      }
    })

    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false)
    const { default: WorkersConsole } = await import('./WorkersConsole.vue')
    const wrapper = mount(WorkersConsole, { attachTo: document.body })
    await flushPromises()

    const tabs = wrapper.findAll('[role="tab"]')
    expect(tabs[1]?.attributes('aria-selected')).toBe('true')

    await tabs[0]?.trigger('mousedown', { button: 0 })
    await flushPromises()

    expect(confirm).toHaveBeenCalledOnce()
    expect(tabs[1]?.attributes('aria-selected')).toBe('true')

    confirm.mockReturnValue(true)
    await tabs[0]?.trigger('mousedown', { button: 0 })
    await flushPromises()

    expect(tabs[0]?.attributes('aria-selected')).toBe('true')

    yamlDirty.value = false
    formDirty.value = true
    await tabs[1]?.trigger('mousedown', { button: 0 })
    await flushPromises()

    expect(tabs[1]?.attributes('aria-selected')).toBe('true')
    expect(editorYamlContent.value).toContain('prompt_group: default')

    wrapper.unmount()
  }, 15_000)

  it('uses shared controls and preserves form save enablement', async () => {
    vi.doMock('vue-router', () => ({
      useRoute: () => ({ query: { mode: 'form' } }),
    }))
    vi.doMock('@/components/MonacoEditor.vue', () => ({
      default: {
        name: 'MonacoEditor',
        props: ['modelValue'],
        template: '<div data-testid="monaco">{{ modelValue }}</div>',
      },
    }))
    const state = dispatcherState()
    vi.doMock('@/composables/useDispatcherFormConfig', async (importOriginal) => {
      const actual = await importOriginal<typeof import('@/composables/useDispatcherFormConfig')>()
      return {
        ...actual,
        useDispatcherFormConfig: () => state,
      }
    })

    const { default: WorkersConsole } = await import('./WorkersConsole.vue')
    const wrapper = mount(WorkersConsole, { attachTo: document.body })
    await flushPromises()

    expect(wrapper.findAll('.ui-form-field').length).toBeGreaterThan(0)
    expect(wrapper.findAll('.ui-input').length).toBeGreaterThan(0)
    expect(wrapper.findAll('.ui-select__trigger')).toHaveLength(1)
    expect(wrapper.findAll('.ui-checkbox')).toHaveLength(0)
    expect(wrapper.text()).not.toContain('allow_mock_workers')
    expect(wrapper.findAll('.ui-button').length).toBeGreaterThanOrEqual(6)
    expect(wrapper.find('.form-input').exists()).toBe(false)
    expect(wrapper.find('.form-select').exists()).toBe(false)
    expect(wrapper.find('.btn-secondary').exists()).toBe(false)
    expect(wrapper.find('.btn-success').exists()).toBe(false)

    const saveButton = wrapper
      .findAll<HTMLButtonElement>('button')
      .find((button) => button.text().includes('保存表单'))
    expect(saveButton?.element.disabled).toBe(true)

    formDirty.value = true
    await flushPromises()
    expect(saveButton?.element.disabled).toBe(false)
    await saveButton?.trigger('click')
    expect(state.saveForm).toHaveBeenCalledOnce()

    wrapper.unmount()
  }, 15_000)
})
