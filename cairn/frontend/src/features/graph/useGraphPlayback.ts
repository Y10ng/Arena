import { computed, onUnmounted, ref, watch, type ComputedRef } from 'vue'

interface PlaybackEvent {
  time: string
  label: string
}

export function useGraphPlayback(playbackEvents: ComputedRef<PlaybackEvent[]>) {
  const playbackIndex = ref(0)
  const isPlaying = ref(false)
  const cutoffTime = ref<string | null>(null)
  let playbackTimer: number | null = null

  watch(playbackIndex, (indexVal) => {
    const idx = Number(indexVal)
    if (idx === 0) {
      cutoffTime.value = null
    } else if (playbackEvents.value[idx - 1]) {
      cutoffTime.value = playbackEvents.value[idx - 1].time
    }
  })

  const togglePlayback = () => {
    if (isPlaying.value) {
      stopPlayback()
    } else {
      startPlayback()
    }
  }

  const startPlayback = () => {
    if (playbackEvents.value.length === 0) return
    stopPlayback()
    isPlaying.value = true
    if (playbackIndex.value >= playbackEvents.value.length) {
      playbackIndex.value = 0
    }

    playbackTimer = window.setInterval(() => {
      if (playbackIndex.value < playbackEvents.value.length) {
        playbackIndex.value++
      } else {
        stopPlayback()
      }
    }, 1000)
  }

  function stopPlayback() {
    isPlaying.value = false
    if (playbackTimer) {
      clearInterval(playbackTimer)
      playbackTimer = null
    }
  }

  const resetPlayback = () => {
    playbackIndex.value = 0
    cutoffTime.value = null
    stopPlayback()
  }

  const onPlaybackSliderInput = () => {
    stopPlayback()
  }

  const playbackTimeLabel = computed(() => {
    if (playbackIndex.value === 0) return '起点时刻'
    const event = playbackEvents.value[playbackIndex.value - 1]
    if (!event) return '最新时刻'
    try {
      const date = new Date(event.time)
      return date.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', second: '2-digit' })
    } catch {
      return '—'
    }
  })

  const playbackEventDesc = computed(() => {
    if (playbackIndex.value === 0) return '系统初始状态'
    const event = playbackEvents.value[playbackIndex.value - 1]
    return event ? event.label : ''
  })

  onUnmounted(stopPlayback)

  return {
    playbackIndex,
    isPlaying,
    cutoffTime,
    playbackTimeLabel,
    playbackEventDesc,
    togglePlayback,
    stopPlayback,
    resetPlayback,
    onPlaybackSliderInput,
  }
}
