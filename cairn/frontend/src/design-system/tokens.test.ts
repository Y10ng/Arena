/// <reference types="node" />

import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { describe, expect, it } from 'vitest'

const tokenSource = readFileSync(fileURLToPath(new URL('./tokens.css', import.meta.url)), 'utf8')

const requiredTokens = [
  '--color-canvas',
  '--color-surface',
  '--color-surface-raised',
  '--color-surface-inset',
  '--color-surface-selected',
  '--color-border-subtle',
  '--color-border',
  '--color-border-strong',
  '--color-text',
  '--color-text-secondary',
  '--color-text-disabled',
  '--color-severity-critical',
  '--color-severity-high',
  '--color-severity-medium',
  '--color-severity-low',
  '--color-severity-info',
  '--severity-critical',
  '--severity-high',
  '--severity-medium',
  '--severity-low',
  '--severity-info',
  '--font-sans',
  '--font-mono',
  '--font-normal',
  '--font-medium',
  '--font-semibold',
  '--text-md',
  '--text-2xl',
  '--leading-normal',
  '--space-8',
  '--radius-2xl',
  '--control-height-xl',
  '--shadow-xs',
  '--shadow-sm',
  '--shadow-md',
  '--shadow-pop',
  '--shadow-modal',
  '--ring',
  '--layout-sidebar-width',
  '--layout-topbar-height',
  '--overlay-dialog-md',
  '--z-modal',
  '--z-toast',
  '--dur-slow',
  '--ease-enter',
  '--breakpoint-mobile',
  '--breakpoint-wide',
] as const

describe('design token contract', () => {
  it('defines every foundation token required by the modernized component layer', () => {
    requiredTokens.forEach((token) => {
      expect(tokenSource).toContain(`${token}:`)
    })
  })

  it('keeps light theme and reduced-motion overrides explicit', () => {
    expect(tokenSource).toContain("[data-theme='light']")
    expect(tokenSource).toContain('--color-surface: #ffffff')
    expect(tokenSource).toContain('--glass-bg: rgb(255 255 255 / 92%)')
    expect(tokenSource).toContain('@media (prefers-reduced-motion: reduce)')
    expect(tokenSource).toContain('--dur-slow: 1ms')
  })

  it('defines theme-aware severity colors and stable semantic aliases for charts', () => {
    expect(tokenSource).toContain('--color-severity-critical: #d95f64')
    expect(tokenSource).toContain('--color-severity-critical: #b43f46')
    expect(tokenSource).toContain('--severity-critical: var(--color-severity-critical)')
    expect(tokenSource).toContain('--severity-info: var(--color-severity-info)')
  })

  it('uses available system and Chinese fallback fonts without assuming bundled web fonts', () => {
    expect(tokenSource).toContain("'PingFang SC'")
    expect(tokenSource).toContain("'Microsoft YaHei'")
    expect(tokenSource).toContain("'Noto Sans CJK SC'")
    expect(tokenSource).not.toContain('--font-sans: Inter')
    expect(tokenSource).not.toContain("--font-mono: 'JetBrains Mono'")
  })
})
