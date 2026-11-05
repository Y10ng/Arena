import { describe, expect, it, vi } from 'vitest'
import { agentEventsUrl, createAgentConversation, deleteAgentAttachment, deleteAgentConversation, getAgentAttachmentLimits, getAgentCleanup, listAgentAttachments, listAgentCleanupRequests, listAgentConversations, listAgentMessages, listAgentWork, sendAgentMessage, uploadAgentAttachment } from './projectAgent'

const mocks = vi.hoisted(() => ({ api: vi.fn(), apiRaw: vi.fn() }))
vi.mock('./client', () => mocks)

describe('全局会话 HTTP 合同', () => {
  it('所有消费者使用会话身份，项目只作为可空创建来源和固定消息参数', () => {
    const signal = new AbortController().signal
    listAgentConversations(signal)
    createAgentConversation(null)
    createAgentConversation('p1')
    listAgentMessages('c1', signal)
    listAgentWork('c1', signal)
    listAgentAttachments('c1', signal)
    getAgentAttachmentLimits('c1', signal)
    const body = { client_message_id: 'input', text: '合成输入', attachment_ids: ['f1'], project_id: null }
    sendAgentMessage('c1', body)
    deleteAgentAttachment('c1', 'f1')
    deleteAgentConversation('c1')
    listAgentCleanupRequests(signal)
    getAgentCleanup('c1')
    const file = new File(['合成附件'], 'sample.txt')
    uploadAgentAttachment('c1', file)
    expect(mocks.api.mock.calls).toEqual([
      ['GET', '/agent-conversations', undefined, signal],
      ['POST', '/agent-conversations', { project_id: null }],
      ['POST', '/agent-conversations', { project_id: 'p1' }],
      ['GET', '/agent-conversations/c1/messages?after=0', undefined, signal],
      ['GET', '/agent-conversations/c1/actions', undefined, signal],
      ['GET', '/agent-conversations/c1/attachments', undefined, signal],
      ['GET', '/agent-conversations/c1/attachment-limits', undefined, signal],
      ['POST', '/agent-conversations/c1/messages', body],
      ['DELETE', '/agent-conversations/c1/attachments/f1'],
      ['DELETE', '/agent-conversations/c1'],
      ['GET', '/agent-conversations/cleanup-requests', undefined, signal],
      ['GET', '/agent-conversations/c1/cleanup'],
    ])
    expect(mocks.apiRaw).toHaveBeenCalledExactlyOnceWith('POST', '/agent-conversations/c1/attachments?filename=sample.txt', file)
    expect(agentEventsUrl('c1', '12')).toBe('/agent-conversations/c1/events?after=12')
  })
})
