import { createRouter, createWebHistory } from 'vue-router'
import AdminLayout from '../layouts/AdminLayout.vue'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    {
      path: '/',
      component: AdminLayout,
      children: [
        { path: '', redirect: '/overview' },
        { path: 'overview', component: () => import('../pages/OverviewPage.vue') },
        { path: 'devices', component: () => import('../pages/DevicesPage.vue') },
        { path: 'trends', component: () => import('../pages/TrendsPage.vue') },
        { path: 'tasks', component: () => import('../pages/TasksPage.vue') },
        { path: 'quality', component: () => import('../pages/QualityPage.vue') },
        { path: 'debug', component: () => import('../pages/DebugPage.vue') },
        { path: 'config', component: () => import('../pages/ConfigPage.vue') },
        { path: 'logs', component: () => import('../pages/LogsPage.vue') },
      ],
    },
  ],
})

export default router
