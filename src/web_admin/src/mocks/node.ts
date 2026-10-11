// Node 端 MSW server：Vitest 组件/单元测试复用同一组 handlers，
// 保证 mock 契约在测试与 dev:mock 间一致。
import { setupServer } from 'msw/node'
import { handlers } from './handlers'

export const server = setupServer(...handlers)
