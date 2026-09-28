import { nextTick, onMounted, onUnmounted, ref, type Ref } from 'vue'

export function useGraphFullscreen(
  graphPageElement: Ref<HTMLElement | null>,
  fitViewport: () => void,
) {
  const isGraphFullscreen = ref(false)
  const fullscreenMode = ref<'native' | 'fixed' | null>(null)

  const toggleFullscreen = async () => {
    if (isGraphFullscreen.value) {
      await exitGraphFullscreen()
    } else {
      await enterGraphFullscreen()
    }
  }

  const enterGraphFullscreen = async () => {
    const graphPage = graphPageElement.value
    if (!graphPage) return

    try {
      if (graphPage.requestFullscreen) {
        await graphPage.requestFullscreen()
        fullscreenMode.value = 'native'
      } else {
        fullscreenMode.value = 'fixed'
        isGraphFullscreen.value = true
      }
    } catch {
      fullscreenMode.value = 'fixed'
      isGraphFullscreen.value = true
    }

    await nextTick()
    fitViewport()
  }

  const exitGraphFullscreen = async () => {
    if (fullscreenMode.value === 'native' && document.fullscreenElement) {
      await document.exitFullscreen()
    } else {
      fullscreenMode.value = null
      isGraphFullscreen.value = false
      await nextTick()
      fitViewport()
    }
  }

  const syncFullscreenState = () => {
    const isNativeFullscreen = document.fullscreenElement === graphPageElement.value
    if (isNativeFullscreen) {
      fullscreenMode.value = 'native'
      isGraphFullscreen.value = true
    } else if (fullscreenMode.value === 'native') {
      fullscreenMode.value = null
      isGraphFullscreen.value = false
    }

    nextTick(() => {
      fitViewport()
    })
  }

  const onGraphKeydown = (event: KeyboardEvent) => {
    if (event.key !== 'Escape' || fullscreenMode.value !== 'fixed') return

    // Graph 内联浮层先消费 Escape，下一次按键再退出 fixed fullscreen。
    if (graphPageElement.value?.querySelector('[role="dialog"], [role="alertdialog"]')) return
    void exitGraphFullscreen()
  }

  const cleanupFullscreen = () => {
    if (
      fullscreenMode.value === 'native' &&
      document.fullscreenElement === graphPageElement.value
    ) {
      document.exitFullscreen().catch(() => {})
    }
    fullscreenMode.value = null
    isGraphFullscreen.value = false
  }

  onMounted(() => {
    document.addEventListener('fullscreenchange', syncFullscreenState)
    window.addEventListener('keydown', onGraphKeydown)
  })

  onUnmounted(() => {
    document.removeEventListener('fullscreenchange', syncFullscreenState)
    window.removeEventListener('keydown', onGraphKeydown)
    cleanupFullscreen()
  })

  return {
    isGraphFullscreen,
    toggleFullscreen,
  }
}
