import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { defineComponent, h } from 'vue'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { api } from '@/api/client'
import { QueryClient, VueQueryPlugin } from '@tanstack/vue-query'
import ProjectScheduleDetail from './ProjectScheduleDetail.vue'
import ProjectTestEgressDialog from './ProjectTestEgressDialog.vue'
import TestEgressFields from '@/components/patterns/TestEgressFields.vue'

vi.mock('@/api/client', () => ({ api: vi.fn() }))
vi.mock('vue-router', () => ({ onBeforeRouteLeave: vi.fn(), onBeforeRouteUpdate: vi.fn(), useRoute: () => ({ params: { id: 'schedule-a' } }), useRouter: () => ({ push: vi.fn() }) }))
const SelectStub = defineComponent({
  props: ['modelValue', 'options', 'disabled'], emits: ['update:modelValue'],
  setup: (props, { emit }) => () => h('select', { value: props.modelValue, disabled: props.disabled, onChange: (event: Event) => emit('update:modelValue', (event.target as HTMLSelectElement).value) }, props.options.map((option: { value: string; label: string }) => h('option', { value: option.value }, option.label))),
})
const current = { mode: 'http' as const, host: 'proxy.example.test', port: 8080, auth_configured: true, revision: 2 }
let wrapper: VueWrapper
let client: QueryClient | undefined
function open() {
  wrapper = mount(ProjectTestEgressDialog, { props: { projectId: 'project-a', value: current }, global: { stubs: { UiDialog: { template: '<div><slot /><slot name="footer" /></div>' }, UiSelect: SelectStub } } })
  return wrapper
}
async function save() {
  await wrapper.findAll('button').find((button) => button.text() === '保存代理设置')!.trigger('click')
  await flushPromises()
}
afterEach(() => { wrapper?.unmount(); client?.clear(); vi.resetAllMocks() })

describe('代理设置表单', () => {
  it('单次计划编辑将出口和改期一起提交，不发送project或单独更新项目', async () => {
    const rules = { mode: 'once', timezone: 'Asia/Shanghai', start_at: '2099-06-01T09:00:00+08:00', local_time: null, weekdays: [], end_date: null, max_runs: null }
    const schedule = { ...rules, id: 'schedule-a', version: 4, status: 'active', project_id: 'project-a', project: null, test_egress: current, resources: [], library_bindings: [], generated_count: 0 }
    vi.mocked(api).mockImplementation(async (_method, path) => path.endsWith('/history?offset=0&limit=20') ? { items: [], total: 0 } : schedule)
    client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } })
    wrapper = mount(ProjectScheduleDetail, { global: { plugins: [[VueQueryPlugin, { queryClient: client }]], stubs: {
      UiDialog: { props: ['open'], template: '<div v-if="open"><slot /><slot name="footer" /></div>' }, UiSelect: SelectStub, NewProject: true,
      ScheduleTiming: defineComponent({ setup: (_props, { expose }) => { expose({ validate: () => ({ rules, deadline: null }) }); return () => h('div') } }),
    } } })
    await flushPromises()
    await wrapper.findAll('button').find((button) => button.text() === '改期与代理设置')!.trigger('click')
    const fields = wrapper.getComponent(TestEgressFields)
    expect(fields.props('authConfigured')).toBe(true)
    fields.vm.$emit('update:modelValue', { ...fields.props('modelValue'), mode: 'socks5', port: 1080 })
    await flushPromises()
    await wrapper.findAll('button').find((button) => button.text() === '保存改期与代理设置')!.trigger('click')
    await flushPromises()
    expect(api).toHaveBeenCalledWith('PUT', '/project-schedules/schedule-a', { ...rules, version: 4, test_egress: { mode: 'socks5', host: current.host, port: 1080, auth_action: 'keep' } })
    expect(vi.mocked(api).mock.calls.filter(([method]) => method === 'PUT')).toHaveLength(1)
  })

  it('读取只显示已配置，保存keep不携带凭据；替换失败保留输入，clear不发送残留密码', async () => {
    open()
    vi.mocked(api).mockResolvedValue(current)
    expect(wrapper.text()).toContain('认证已配置')
    expect(wrapper.find('input[type="password"]').exists()).toBe(false)
    await save()
    expect(api).toHaveBeenLastCalledWith('PUT', '/projects/project-a/test-egress', { mode: 'http', host: current.host, port: 8080, auth_action: 'keep' })
    await wrapper.findAll('select')[1]!.setValue('replace')
    const inputs = wrapper.findAll('input')
    await inputs[2]!.setValue('sample-user')
    await inputs[3]!.setValue('sample-password')
    vi.mocked(api).mockRejectedValueOnce(new Error('不可显示的请求输入'))
    await save()
    expect(wrapper.getComponent(TestEgressFields).props('modelValue').password).toBe('sample-password')
    expect(wrapper.text()).not.toContain('不可显示的请求输入')
    expect(wrapper.text()).toContain('当前输入已保留')
    await wrapper.findAll('select')[1]!.setValue('clear')
    expect(wrapper.getComponent(TestEgressFields).props('modelValue').password).toBe('')
    await save()
    expect(api).toHaveBeenLastCalledWith('PUT', '/projects/project-a/test-egress', { mode: 'http', host: current.host, port: 8080, auth_action: 'clear' })
    expect(wrapper.text()).toContain('仅影响后续新执行')
  })

  it('拒绝URL地址和非法端口，直接连接不携带地址或凭据', async () => {
    open()
    await wrapper.findAll('input')[0]!.setValue('http://sample-user:sample-password@proxy.test')
    await save()
    expect(api).not.toHaveBeenCalled()
    expect(wrapper.text()).toContain('不含协议、路径或认证信息')
    await wrapper.findAll('input')[0]!.setValue('proxy.example.test')
    await wrapper.findAll('input')[1]!.setValue('65536')
    await save()
    expect(api).not.toHaveBeenCalled()
    expect(wrapper.text()).toContain('1–65535')
    await wrapper.findAll('select')[0]!.setValue('direct')
    vi.mocked(api).mockResolvedValue({ mode: 'direct', host: null, port: null, auth_configured: false, revision: 3 })
    await save()
    expect(api).toHaveBeenLastCalledWith('PUT', '/projects/project-a/test-egress', { mode: 'direct', auth_action: 'clear' })
    expect(wrapper.emitted('saved')).toHaveLength(1)
  })
})
