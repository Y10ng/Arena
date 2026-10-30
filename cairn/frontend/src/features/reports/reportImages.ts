import { fetchBlob } from '@/api/client'
import { parseReportMarkdown } from './safeReportMarkdown'

// 只消费服务端按当前来源及输出边界核验的清单，不从 Markdown URL 推导读取权限。
export interface ReportDisplayImage {
  artifact_id: string
  caption: string
  content_type: string
  evidence_item_id?: string | null
  unavailable_reason?: string | null
}
export interface ReportImageData {
  image: ReportDisplayImage
  blob?: Blob
  error?: string
}
export type ReportImageLoader = (image: ReportDisplayImage, signal: AbortSignal) => Promise<Blob>
export const projectImageLoader = (projectId: string): ReportImageLoader =>
  (image, signal) => fetchBlob(`/projects/${encodeURIComponent(projectId)}/artifacts/${encodeURIComponent(image.artifact_id)}/content`, signal)

export async function loadReportImages(images: ReportDisplayImage[], load: ReportImageLoader, signal: AbortSignal): Promise<ReportImageData[]> {
  return Promise.all(images.map(async (image) => {
    if (image.unavailable_reason) return { image, error: image.unavailable_reason }
    try {
      const blob = await load(image, signal)
      if (!['image/png', 'image/jpeg', 'image/gif', 'image/webp'].includes(blob.type) || blob.type !== image.content_type) throw new Error('图片类型不匹配或不支持。')
      // 解码失败也局部降级，不能把损坏图片写入页面或交付物。
      const bitmap = await createImageBitmap(blob)
      bitmap.close()
      return { image, blob }
    } catch (error) {
      if (signal.aborted) throw error
      return { image, error: error instanceof Error ? error.message : '图片读取失败。' }
    }
  }))
}

const markdownCaption = (text: string) => text.replace(/[\\`*_[\]<>]/g, '\\$&').replace(/[\r\n]+/g, ' ')
export function withDisplayImages(content: string, images: ReportDisplayImage[]) {
  if (!images.length) return content
  const used = new Set(parseReportMarkdown(content).flatMap((token) => (token.children || []).filter((child) => child.type === 'image').map((child) => child.attrGet('src'))))
  return content + images.filter((image) => !used.has(`arena-image:${image.artifact_id}`)).map((image) => `\n\n![${markdownCaption(image.caption || '证据截图')}](arena-image:${image.artifact_id})`).join('')
}

export function rewriteReportImages(content: string, resolve: (id: string, caption: string) => string) {
  const lines = content.split('\n')
  // 仅替换 Markdown 图片所在的正文区，不碰代码围栏中的协议原文。
  for (const token of parseReportMarkdown(content).reverse()) {
    if (token.type !== 'inline' || !token.map || !token.children?.some((child) => child.type === 'image')) continue
    const [start, end] = token.map
    lines.splice(start, end - start, lines.slice(start, end).join('\n').replace(/(`+)[\s\S]*?\1|!\[((?:\\.|[^\]\\])*)\]\(arena-image:([a-zA-Z0-9_-]+)\)/g, (match, code, caption, id) => code ? match : resolve(id, caption)))
  }
  return lines.join('\n')
}

export async function blobDataUrl(blob: Blob): Promise<string> {
  const bytes = new Uint8Array(await blob.arrayBuffer())
  let binary = ''
  for (let offset = 0; offset < bytes.length; offset += 8192) binary += String.fromCharCode(...bytes.subarray(offset, offset + 8192))
  return `data:${blob.type};base64,${btoa(binary)}`
}
