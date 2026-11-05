import { fileURLToPath, URL } from 'node:url'
import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

const serverStaticDir = '../src/cairn/server/static'
const verifyBuildDir = '.build/verify'

// 日常验证构建与服务端发布产物分离，避免检查代码时改写已提交的 static 文件。
export default defineConfig(({ mode }) => ({
  base: '/static/',
  plugins: [vue()],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  build: {
    outDir: mode === 'verify' ? verifyBuildDir : serverStaticDir,
    // 发布保留旧 hash，避免已打开页面的延迟加载失效；隔离验证目录可清空。
    emptyOutDir: mode === 'verify',
    assetsDir: 'assets',
    rollupOptions: {
      output: {
        entryFileNames: 'assets/[name]-[hash].js',
        chunkFileNames: 'assets/[name]-[hash].js',
        assetFileNames: 'assets/[name]-[hash][extname]',
      },
    },
  },
}))
