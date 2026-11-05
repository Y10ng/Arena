import { describe, expect, it } from 'vitest'
import { ApiError } from '@/api/client'
import {
  artifactFilename,
  artifactIsTextPreviewable,
  workspaceErrorMessage,
  workspaceNodeKey,
} from './artifactsModel'

describe('workspace presentation boundaries', () => {
  it('keeps names and saved text support without reconstructing live paths', () => {
    expect(artifactFilename({ uri: 'artifact://saved/中文%20报告.md' })).toBe('中文 报告.md')
    expect(artifactIsTextPreviewable({ kind: 'report' })).toBe(true)
    expect(artifactIsTextPreviewable({ kind: 'screenshot' })).toBe(false)
    expect(artifactIsTextPreviewable({ kind: 'pcap' })).toBe(false)
  })
  it('keys by project, workspace identity, entry kind and full relative path', () => {
    const key = workspaceNodeKey('p1', 'w1', 'file', 'a/readme.md')
    expect(key).not.toBe(workspaceNodeKey('p2', 'w1', 'file', 'a/readme.md'))
    expect(key).not.toBe(workspaceNodeKey('p1', 'w2', 'file', 'a/readme.md'))
    expect(key).not.toBe(workspaceNodeKey('p1', 'w1', 'file', 'b/readme.md'))
  })
  it.each([
    ['not_created', '尚未创建'],
    ['deleted', '已删除'],
    ['owner_unknown', '归属'],
    ['unreachable', '不可达'],
    ['permission_denied', '权限'],
    ['layer_unsupported', '历史现场'],
    ['identity_changed', '身份已变化'],
    ['cursor_invalid', '续页已失效'],
    ['path_private', '私有'],
    ['path_blocked', '特殊文件'],
    ['path_not_found', '已不存在'],
  ])('maps %s without exposing raw server exceptions', (code, text) => {
    expect(
      workspaceErrorMessage(new ApiError('/private/secret', 409, { detail: `workspace_${code}` })),
    ).toContain(text)
  })
  it('never renders arbitrary errors or private server details', () => {
    expect(workspaceErrorMessage(new ApiError('token=canary', 500, { detail: '/host/path' }))).toBe(
      '文件读取失败，请稍后重试。',
    )
    expect(workspaceErrorMessage(new Error('/host/path'))).not.toContain('/host/path')
  })
})
