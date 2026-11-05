<template>
  <footer class="playback-bar" aria-label="历史推演回放">
    <div class="playback-left">
      <Tooltip :content="isPlaying ? '暂停' : '回放探索过程'">
        <IconButton
          class="btn-play-pause"
          variant="secondary"
          :label="isPlaying ? '暂停历史推演回放' : '开始历史推演回放'"
          :aria-pressed="isPlaying"
          @click="$emit('togglePlayback')"
        >
          <Pause v-if="isPlaying" :size="16" :stroke-width="2" />
          <Play v-else :size="16" :stroke-width="2" />
        </IconButton>
      </Tooltip>
      <div class="playback-heading">
        <span class="playback-title">历史推演回放</span>
        <span class="playback-time mono">{{ playbackTimeLabel }}</span>
      </div>
    </div>
    <div class="playback-center">
      <!-- 保留原生 range 的键盘操作、数值与 input 事件语义。 -->
      <input
        type="range"
        :value="playbackIndex"
        min="0"
        :max="eventCount"
        class="playback-slider"
        aria-label="回放进度"
        :aria-valuetext="playbackTimeLabel"
        @input="onSliderInput"
      />
    </div>
    <span class="playback-position">{{ playbackIndex }} / {{ eventCount }}</span>
    <div v-if="playbackEventDesc" class="playback-right">
      <p class="playback-desc" tabindex="0">{{ playbackEventDesc }}</p>
    </div>
  </footer>
</template>

<script setup lang="ts">
import { Pause, Play } from '@lucide/vue'
import { IconButton, Tooltip } from '@/components/ui'

defineProps<{
  isPlaying: boolean
  playbackIndex: number
  eventCount: number
  playbackTimeLabel: string
  playbackEventDesc: string
}>()

const emit = defineEmits<{
  (event: 'togglePlayback'): void
  (event: 'update:playbackIndex', value: number): void
  (event: 'sliderInput'): void
}>()

const onSliderInput = (event: Event) => {
  emit('update:playbackIndex', Number((event.target as HTMLInputElement).value))
  emit('sliderInput')
}
</script>
