import { computed, defineComponent, nextTick, ref } from 'vue'
import { mount } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { useGraphPlayback } from './useGraphPlayback'

interface PlaybackHarness {
  playbackIndex: number
  isPlaying: boolean
  cutoffTime: string | null
  playbackEventDesc: string
  togglePlayback: () => void
  resetPlayback: () => void
  onPlaybackSliderInput: () => void
}

const Harness = defineComponent({
  setup() {
    const events = ref([
      { time: '2026-08-21T10:00:00.000Z', label: '创建起点' },
      { time: '2026-08-21T10:00:01.000Z', label: '发现事实' },
    ])
    return useGraphPlayback(computed(() => events.value))
  },
  template: '<div />',
})

beforeEach(() => {
  vi.useFakeTimers()
})

afterEach(() => {
  vi.runOnlyPendingTimers()
  vi.useRealTimers()
})

describe('useGraphPlayback timers', () => {
  it('advances index and cutoff in event order, then stops at the end', async () => {
    const wrapper = mount(Harness)
    const playback = wrapper.vm as unknown as PlaybackHarness

    playback.togglePlayback()
    expect(playback.isPlaying).toBe(true)

    vi.advanceTimersByTime(1000)
    await nextTick()
    expect(playback.playbackIndex).toBe(1)
    expect(playback.cutoffTime).toBe('2026-08-21T10:00:00.000Z')
    expect(playback.playbackEventDesc).toBe('创建起点')

    vi.advanceTimersByTime(1000)
    await nextTick()
    expect(playback.playbackIndex).toBe(2)
    expect(playback.cutoffTime).toBe('2026-08-21T10:00:01.000Z')

    vi.advanceTimersByTime(1000)
    expect(playback.isPlaying).toBe(false)
    expect(playback.playbackIndex).toBe(2)

    wrapper.unmount()
  })

  it('stops without changing the selected index when the slider interrupts playback', () => {
    const wrapper = mount(Harness)
    const playback = wrapper.vm as unknown as PlaybackHarness

    playback.playbackIndex = 1
    playback.togglePlayback()
    playback.onPlaybackSliderInput()

    expect(playback.isPlaying).toBe(false)
    expect(playback.playbackIndex).toBe(1)

    vi.advanceTimersByTime(3000)
    expect(playback.playbackIndex).toBe(1)

    wrapper.unmount()
  })

  it('restarts completed playback from index zero and reset clears cutoff and timer', async () => {
    const wrapper = mount(Harness)
    const playback = wrapper.vm as unknown as PlaybackHarness

    playback.playbackIndex = 2
    await nextTick()
    playback.togglePlayback()

    expect(playback.isPlaying).toBe(true)
    expect(playback.playbackIndex).toBe(0)

    playback.resetPlayback()
    expect(playback.isPlaying).toBe(false)
    expect(playback.playbackIndex).toBe(0)
    expect(playback.cutoffTime).toBeNull()

    wrapper.unmount()
  })
})
