---
title: 覆盖追踪与覆盖矩阵
tags: [logic-vuln, coverage, methodology]
applies_to: [logic-vuln]
summary: 端点×角色×流转状态覆盖矩阵、coverage: 事实约定、避免漏测与重复测
last_verified_at: 2026-07-08
---

# 覆盖追踪与覆盖矩阵

逻辑漏洞挖掘的核心是**系统覆盖**，否则会漏测（某角色×端点没碰）或重复测。把攻击面建模为三维单元：`端点 × 角色 × 流转状态`。

## 覆盖矩阵

- **端点**：API 路径、页面、功能入口。
- **角色**：匿名、普通用户 A、普通用户 B、管理员等。
- **流转状态**：未登录、已登录未下单、已下单未支付、已支付等。

每个单元至少测一次"该角色在该状态下访问该端点"是否被正确放行/拒绝。优先补**未测单元**。逻辑漏洞常出现在：

- (角色 × 端点)：某端点对某角色没做权限校验。
- (端点 × 状态)：某端点在某状态下没校验前置状态。

## `coverage:` 事实约定

为了让 fact 图累积可被后续 reason 识别的覆盖轨迹，已测单元的结论事实建议以 `coverage:` 开头描述，便于 reason 用关键词检索已覆盖单元、避免重复。示例：

- `coverage: GET /api/orders/{id} as role:user-a → 200 返回自己的订单（baseline）`
- `coverage: GET /api/orders/{id} as role:user-b → 200 返回 user-a 的订单（IDOR 成立）`
- `coverage: POST /api/order/confirm 跳过支付 → 200 确认成功（跳步成立）`
- `coverage: GET /api/admin/users as role:user → 403（垂直越权已防住，未发现漏洞）`

无论是否发现漏洞，已测单元都应落 fact，这样 reason 能区分"已测无漏洞"与"未测"。

## 在 reason 里使用覆盖

reason 在提新 intent 前，应：
1. 从图中 facts 里提取已有 `coverage:` 记录，汇总已覆盖单元。
2. 找出高价值未覆盖单元（尤其涉及权限/资产/状态迁移的）。
3. 优先为未覆盖单元提 intent；已覆盖且无漏洞的单元不重复提。
4. 把覆盖评估简述写进 `rationale`，让决策可审计。
