import { createApp } from 'vue'
import { createPinia } from 'pinia'
import { VueQueryPlugin } from '@tanstack/vue-query'
import ElementPlus from 'element-plus'
import 'element-plus/dist/index.css'
import App from './App.vue'
import { queryClient } from './api/queryClient'
import './styles/index.css'

createApp(App)
  .use(createPinia())
  .use(VueQueryPlugin, { queryClient })
  .use(ElementPlus)
  .mount('#app')
