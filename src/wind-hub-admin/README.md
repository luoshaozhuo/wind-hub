# wind-hub-admin

Wind Hub 采集系统管理控制台（Vue 3 + Element Plus + Vue Query + Pinia）。

## 开发

```bash
npm install
npm run dev        # 连接真实后端（默认 real 模式）
npm run dev:mock   # 使用 MSW mock 后端，无需启动 Server
```

- `npm run dev`：real 模式。默认经 Vite proxy 把 `/api` 转发到 `127.0.0.1:8080`。
- `npm run dev:mock`：mock 模式（`VITE_API_MODE=mock`，见 `.env.mock`）。MSW 在
  HTTP 边界拦截 `/api/v1/...`，与 real 模式共用同一套 api / composable / store / page。
- `VITE_API_BASE`：自定义真实后端地址（覆盖默认 proxy 目标，例如
  `VITE_API_BASE=http://192.168.1.10:8080 npm run dev`）。

后端不可达时页面业务结构仍然渲染（空数据 / 默认值），Header 显示
`API OFFLINE` 并提供 Retry；错误只是状态，不会卸载业务页面。

## 测试

```bash
npm test             # Vitest 单元/组件测试（含离线渲染与 mock 后端契约）
npm run test:e2e     # Playwright：real 模式、后端不可达容错
npm run test:e2e:mock # Playwright：dev:mock 全量浏览
npm run build        # vue-tsc 类型检查 + 产物构建
npm run lint
npm run format:check
```
