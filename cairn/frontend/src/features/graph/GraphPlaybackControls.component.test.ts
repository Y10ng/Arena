import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'
import GraphPlaybackControls from './GraphPlaybackControls.vue'

function mountControls(overrides: Record<string, unknown> = {}) {
  return mount(GraphPlaybackControls, {
    props: {
      isPlaying: false,
      playbackIndex: 1,
      eventCount: 4,
      playbackTimeLabel: '10:20:30',
      playbackEventDesc: '发现管理员接口',
      ...overrides,
    },
  })
}

describe('GraphPlaybackControls visual controls', () => {
  it('uses the shared icon button while preserving playback toggle semantics', async () => {
    const wrapper = mountControls()
    const toggle = wrapper.get('button[aria-label="开始历史推演回放"]')

    expect(toggle.attributes('aria-pressed')).toBe('false')
    expect(wrapper.findAll('.ui-icon-button')).toHaveLength(1)
    expect(wrapper.findAll('.ui-badge')).toHaveLength(1)
    expect(wrapper.find('.btn-secondary').exists()).toBe(false)
    await toggle.trigger('click')

    expect(wrapper.emitted('togglePlayback')).toHaveLength(1)
  })

  it('keeps the native range input and emits index before slider interruption', async () => {
    const wrapper = mountControls()
    const slider = wrapper.get<HTMLInputElement>('input[type="range"]')

    expect(slider.attributes('min')).toBe('0')
    expect(slider.attributes('max')).toBe('4')
    expect(slider.attributes('aria-valuetext')).toBe('10:20:30')

    slider.element.value = '3'
    await slider.trigger('input')

    expect(wrapper.emitted('update:playbackIndex')).toEqual([[3]])
    expect(wrapper.emitted('sliderInput')).toHaveLength(1)
    expect(Object.keys(wrapper.emitted()).filter((event) => event !== 'input')).toEqual([
      'update:playbackIndex',
      'sliderInput',
    ])
  })

  it('reflects playing state and renders time and event descriptions', () => {
    const wrapper = mountControls({ isPlaying: true })

    expect(wrapper.get('button[aria-label="暂停历史推演回放"]').attributes('aria-pressed')).toBe(
      'true',
    )
    expect(wrapper.get('.playback-time').text()).toBe('10:20:30')
    expect(wrapper.get('.playback-desc').text()).toBe('发现管理员接口')
  })
})
