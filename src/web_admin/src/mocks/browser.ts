// 浏览器端 MSW worker：仅 mock 模式（VITE_API_MODE=mock）由 main.ts 动态加载。
import { setupWorker } from 'msw/browser'
import { handlers } from './handlers'

export const worker = setupWorker(...handlers)
