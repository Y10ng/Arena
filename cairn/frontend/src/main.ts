import { createApp } from 'vue'
import { createPinia } from 'pinia'
import { VueQueryPlugin } from '@tanstack/vue-query'
import router from './router'
import App from './App.vue'
import { setUnauthorizedHandler } from './api/client'
import { queryClient } from './lib/queryClient'
import { useAuthStore } from './stores/auth'
import './design-system/tokens.css'
import './design-system/primitives.css'

const PRELOAD_RELOAD_KEY = 'cairn:preload-reload'

// 发布版本切换后，旧页面可能仍引用已删除的懒加载 chunk；最多自动刷新一次获取新入口。
window.addEventListener('vite:preloadError', (event) => {
  event.preventDefault()
  if (window.sessionStorage.getItem(PRELOAD_RELOAD_KEY) === '1') return
  window.sessionStorage.setItem(PRELOAD_RELOAD_KEY, '1')
  window.location.reload()
})

const app = createApp(App)
const pinia = createPinia()
app.use(pinia)
app.use(router)
app.use(VueQueryPlugin, { queryClient })

const authStore = useAuthStore(pinia)
setUnauthorizedHandler((requestGeneration) => {
  if (!authStore.clearSessionIfCurrent(requestGeneration)) return
  if (router.currentRoute.value.name !== 'Login') {
    void router.replace({ name: 'Login', query: { redirect: router.currentRoute.value.fullPath } })
  }
}, () => authStore.authGeneration)

app.mount('#app')

void router.isReady().then(() => {
  window.sessionStorage.removeItem(PRELOAD_RELOAD_KEY)
})
