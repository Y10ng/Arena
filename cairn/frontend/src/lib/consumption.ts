import type { ConsumptionSummary } from '@/api/types/consumption'

export function tokenSubtotal(summary?: ConsumptionSummary | null) {
  return summary?.responses && Object.values(summary.usage).some(value => value !== null)
    ? summary.tokens.toLocaleString('zh-CN') : '未记录'
}

export function estimatedAmounts(summary?: ConsumptionSummary | null) {
  if (!summary?.amounts.length) return '费用未定价'
  // 服务端 Decimal 字符串直接展示，避免 JS 浮点把小额已知消费四舍五入成零。
  const values = summary.amounts.map(({ currency, amount }) => {
    const decimal = amount.includes('.') ? amount.replace(/0+$/, '').replace(/\.$/, '') : amount
    return `${currency} ${decimal}`
  }).join(' · ')
  return `${summary.unpriced_responses || summary.partial_responses ? '已计价部分估算' : '估算'} ${values}`
}

export const consumptionSource: Record<string, string> = {
  agent_chat: 'ChatBot', model_test: '模型试用', model_test_draft: '草稿试用',
  connectivity_test: '平台诊断', bootstrap: '初始化', reason: '研究推理', explore: '探索',
  verifier: '验证', external_verify: '独立复测', report: '报告', assessment_report: '报告',
}
