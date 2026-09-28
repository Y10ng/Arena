import pluginVue from 'eslint-plugin-vue'
import { defineConfigWithVueTs, vueTsConfigs } from '@vue/eslint-config-typescript'

export default defineConfigWithVueTs(
  {
    name: 'cairn/files-to-lint',
    files: ['src/**/*.{ts,tsx,vue}', '*.{ts,js}'],
  },
  {
    name: 'cairn/files-to-ignore',
    ignores: ['.build/**', 'coverage/**', 'node_modules/**', '../src/cairn/server/static/**'],
  },
  pluginVue.configs['flat/essential'],
  vueTsConfigs.recommended,
  {
    name: 'cairn/type-safety-baseline',
    rules: {
      '@typescript-eslint/no-explicit-any': 'warn',
      '@typescript-eslint/ban-ts-comment': ['error', { 'ts-ignore': true }],
      '@typescript-eslint/no-empty-object-type': 'warn',
      '@typescript-eslint/no-unused-vars': 'warn',
      'prefer-const': 'warn',
      'vue/multi-word-component-names': 'warn',
    },
  },
  {
    name: 'cairn/headless-boundary',
    files: [
      'src/features/**/*.{ts,tsx,vue}',
      'src/layouts/**/*.{ts,tsx,vue}',
      'src/stores/**/*.{ts,tsx,vue}',
    ],
    rules: {
      'no-restricted-imports': [
        'error',
        {
          paths: [
            {
              name: 'reka-ui',
              message: '业务层必须通过 src/components/ui 的 Cairn 封装使用 Reka UI。',
            },
          ],
        },
      ],
    },
  },
)
