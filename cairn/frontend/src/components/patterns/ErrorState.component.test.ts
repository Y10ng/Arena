import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'
import ErrorState from './ErrorState.vue'

describe('ErrorState', () => {
  it('renders an accessible error and emits retry', async () => {
    const wrapper = mount(ErrorState, {
      props: {
        title: '加载失败',
        description: '无法获取项目列表',
        retryLabel: '重新加载',
      },
      slots: { icon: '<span data-test="icon">!</span>' },
    })

    expect(wrapper.get('[role="alert"]').text()).toContain('加载失败')
    expect(wrapper.text()).toContain('无法获取项目列表')
    expect(wrapper.get('[data-test="icon"]').text()).toBe('!')

    await wrapper.get('button').trigger('click')

    expect(wrapper.get('button').text()).toBe('重新加载')
    expect(wrapper.emitted('retry')).toHaveLength(1)
  })
})
