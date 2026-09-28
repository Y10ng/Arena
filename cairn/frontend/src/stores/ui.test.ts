// @vitest-environment jsdom

import { beforeEach, describe, expect, it } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { useUIStore } from './ui'

const THEME_STORAGE_KEY = 'cairn.theme'

describe('UI store theme persistence', () => {
  beforeEach(() => {
    localStorage.clear()
    document.documentElement.setAttribute('data-theme', 'dark')
    setActivePinia(createPinia())
  })

  it('defaults to light when no theme is persisted', () => {
    const store = useUIStore()

    expect(store.theme).toBe('light')
    expect(document.documentElement.dataset.theme).toBe('light')
    expect(localStorage.getItem(THEME_STORAGE_KEY)).toBe('light')
  })

  it('falls back to light when the persisted theme is invalid', () => {
    localStorage.setItem(THEME_STORAGE_KEY, 'system')

    const store = useUIStore()

    expect(store.theme).toBe('light')
    expect(document.documentElement.dataset.theme).toBe('light')
    expect(localStorage.getItem(THEME_STORAGE_KEY)).toBe('light')
  })

  it('preserves an explicit dark preference', () => {
    localStorage.setItem(THEME_STORAGE_KEY, 'dark')

    const store = useUIStore()

    expect(store.theme).toBe('dark')
    expect(document.documentElement.dataset.theme).toBe('dark')
    expect(localStorage.getItem(THEME_STORAGE_KEY)).toBe('dark')
  })

  it('keeps the store, document, and persistence synchronized when toggled', () => {
    const store = useUIStore()

    store.toggleTheme()

    expect(store.theme).toBe('dark')
    expect(document.documentElement.dataset.theme).toBe('dark')
    expect(localStorage.getItem(THEME_STORAGE_KEY)).toBe('dark')

    store.toggleTheme()

    expect(store.theme).toBe('light')
    expect(document.documentElement.dataset.theme).toBe('light')
    expect(localStorage.getItem(THEME_STORAGE_KEY)).toBe('light')
  })
})
