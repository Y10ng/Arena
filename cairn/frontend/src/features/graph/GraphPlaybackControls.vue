<template>
  <footer class="playback-bar" aria-label="历史推演回放">
    <div class="playback-left">
      <Tooltip :content="isPlaying ? '暂停' : '回放探索过程'">
        <IconButton
          class="btn-play-pause"
          variant="secondary"
          size="sm"
          :label="isPlaying ? '暂停历史推演回放' : '开始历史推演回放'"
          :aria-pressed="isPlaying"
          @click="$emit('togglePlayback')"
        >
          <Pause v-if="isPlaying" :size="14" :stroke-width="2" />
          <Play v-else :size="14" :stroke-width="2" />
        </IconButton>
      </Tooltip>
      <span class="playback-title">历史推演回放</span>
    </div>
    <div class="playback-center">
      <!-- shared Input 不支持 range；保留原生滑块以维持数值与 input 事件语义。 -->
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
      <Badge class="playback-time mono" tone="neutral" size="sm">
        {{ playbackTimeLabel }}
      </Badge>
    </div>
    <div class="playback-right">
      <Tooltip :content="playbackEventDesc" :disabled="!playbackEventDesc">
        <span class="playback-desc text-truncate">{{ playbackEventDesc }}</span>
      </Tooltip>
    </div>
  </footer>
</template>

<script setup lang="ts">
import { Pause, Play } from '@lucide/vue'
import { Badge, IconButton, Tooltip } from '@/components/ui'

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
