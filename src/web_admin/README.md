# wind-hub-admin

Wind Hub 采集系统管理控制台（Vue 3.5 + Element Plus + Vite）。

## 功能

- **Tasks**：采集任务与实例状态查看，任务级 / 实例级 start / stop（需 Control API Key）。
- **Devices**：设备连接状态、最新值查看、设备诊断（Verify）、单点命令写入。
  写入结果按三态展示：成功 / 失败 / **UNKNOWN（结果未知，命令可能已执行，
  禁止盲目重试）**，并展示 `command_id` 供审计对账。
- **Sinks**：Sink 健康状态与队列深度，诊断写测试。

页面每 5 秒自动轮询刷新；所有请求带 15 秒硬超时。

## 开发

```bash
npm install
npm run dev        # 经 Vite proxy 把 /api 转发到 127.0.0.1:8080（Web Server）
```

- `VITE_API_BASE`：自定义后端地址（覆盖默认 proxy 目标，例如
  `VITE_API_BASE=http://192.168.1.10:8080 npm run dev`）。

后端不可达时页面以错误提示展示，不会白屏。

## 测试

```bash
npm test             # Vitest 单元测试（REST 契约、错误透传）
npm run test:e2e     # Playwright E2E
npm run build        # vue-tsc 类型检查 + 产物构建
npm run lint
npm run format:check
npm run check:api    # 与后端 REST 契约一致性检查
```
