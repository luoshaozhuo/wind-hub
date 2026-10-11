import { defineConfig } from 'vitest/config'
import vue from '@vitejs/plugin-vue'
export default defineConfig({plugins: [vue()], test: {environment: 'jsdom', include: ['../../tests/web_admin/unit/**/*.spec.ts'], env: {VITE_API_BASE: 'http://wind-hub-test.invalid'}}})
