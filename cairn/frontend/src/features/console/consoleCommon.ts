import { toRaw } from 'vue'

export const MASKED_API_KEY = '••••••••••••'

export const clonePlain = <T>(value: T): T => structuredClone(toRaw(value))

export const formatError = (error: unknown) => error instanceof Error ? error.message : String(error || '未知错误')

export const formatInfraList = (items: string[] = []) => items.length > 0 ? items.join(', ') : '—'
