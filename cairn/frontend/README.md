# Arena 前端

Arena 的 Vue 3 控制台前端。源码位于 `src/`，完整界面由后端 Server 同源提供，内部 API 与静态目录仍沿用 `cairn` 路径。

## 推荐构建与使用

在本目录执行：

```bash
npm ci
npm run build:server
```

`build:server` 先运行 `vue-tsc -b` 做类型检查，再将 Vite 产物写入 `../src/cairn/server/static/`。构建会清空该产物目录，不应在其中存放手工文件。`npm run build` 是同一构建的别名。

随后按[项目 README](../../README.md)启动完整系统，通过 Server 地址打开界面；登录、API 和前端资源使用同一来源。前端修改后重新执行 `npm run build:server`，再刷新 Server 页面验收。

## 开发

```bash
npm run dev
```

Vite 开发服务器提供前端热更新，页面位于终端显示地址的 `/static/` 路径。当前 `vite.config.ts` **没有配置 API proxy**，因此它不是可直接登录和调用后端的完整系统入口。需要联调实际 API 时，推荐使用上述 `build:server` 后的 Server 页面；不要把 Vite 开发或 preview 服务当作后端服务。

`npm run build:verify` 会做类型检查并将产物写入 `.build/verify/`，不覆盖 Server 的静态文件。

## 定向测试

按改动选择具体测试文件，不必默认执行整套测试：

```bash
npm run test:unit -- src/composables/useDispatcherFormConfig.test.ts
npm run test:component -- src/features/console/ScenariosConsole.component.test.ts
```
