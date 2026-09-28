# 逻辑漏洞挖掘知识集

配合 `logic-vuln` prompt 组使用，聚焦业务逻辑漏洞挖掘。
agent 会用 rg/ls 按需读取下列文件，不要整篇复制进输出：

- business-logic-overview.md — 类别与方法论总览
- access-control.md — IDOR / 水平·垂直越权 / 功能级访问控制
- state-and-flow.md — 状态机篡改 / 跳步 / 乱序 / 竞态
- parameter-tampering.md — 参数篡改 / mass assignment / 类型混淆
- evidence-and-coverage.md — 覆盖矩阵与请求/响应取证规范

核心思路：先建模业务（入口/角色/流转/状态/信任边界），再以「端点×角色×流转状态」覆盖矩阵驱动，单维变体重放，可复现才算确认。
