// client 坐标与 DOMRect 已含根缩放；仅在写回布局像素时换算，比例与库定位不走此处。
export function toLogicalPixels(pixels: number): number {
  const scale = Number.parseFloat(getComputedStyle(document.documentElement).getPropertyValue('--ui-scale')) || 1
  return pixels / scale
}
