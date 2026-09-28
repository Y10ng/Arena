import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'
import MasterDetail from './MasterDetail.vue'

describe('MasterDetail', () => {
  it('renders named slots and configuration layout marker', () => {
    const wrapper = mount(MasterDetail, {
      props: { configuration: true },
      slots: {
        master: '<nav>配置列表</nav>',
        detail: '<main>配置详情</main>',
      },
    })

    expect(wrapper.classes()).toEqual(
      expect.arrayContaining(['master-detail', 'configuration-workspace']),
    )
    expect(wrapper.get('.md-master').text()).toBe('配置列表')
    expect(wrapper.get('.md-detail').text()).toBe('配置详情')
  })

  it('keeps workspace layout modes opt-in', () => {
    const wrapper = mount(MasterDetail)

    expect(wrapper.classes()).toContain('master-detail')
    expect(wrapper.classes()).not.toContain('configuration-workspace')
    expect(wrapper.classes()).not.toContain('master-detail--fill-height')
  })

  it('adds the full-height marker without changing configuration mode', () => {
    const wrapper = mount(MasterDetail, { props: { fillHeight: true } })

    expect(wrapper.classes()).toContain('master-detail--fill-height')
    expect(wrapper.classes()).not.toContain('configuration-workspace')
  })
})
