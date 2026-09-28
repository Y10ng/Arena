# Frontend E2E 验收

默认执行 `npm run test:e2e` 时，Playwright 会构建并启动本地 Vite preview。`shell.spec.ts` 只 mock 认证、项目列表和 Tenant monitoring 等后端边界，页面、路由、焦点、响应式布局和 accessibility 均运行真实生产前端代码。

如需连接已授权的完整测试实例，设置 `CAIRN_E2E_BASE_URL`；此时 Playwright 不会启动本地 preview。涉及真实保存、删除或 Worker 执行的流程应只在隔离测试 Tenant 中运行，并单独提供测试账户和数据清理策略。

常用命令：

```bash
npm run test:e2e
npm run test:a11y
npx playwright test --project=mobile-chromium
```
