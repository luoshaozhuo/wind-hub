<script setup lang="ts">
import { onMounted, onUnmounted, ref } from 'vue'
import { runtimeApi } from './api/runtime'
import OverviewPage from './pages/OverviewPage.vue'
import DevicesPage from './pages/DevicesPage.vue'
import PointsPage from './pages/PointsPage.vue'
import TasksPage from './pages/TasksPage.vue'
import SinksPage from './pages/SinksPage.vue'
import SystemHealthPage from './pages/SystemHealthPage.vue'
import DebugPage from './pages/DebugPage.vue'
import QualityPage from './pages/QualityPage.vue'

type Page = 'Overview' | 'Devices' | 'Points' | 'Tasks' | 'Sinks' | 'SystemHealth' | 'Debug' | 'Quality'
const menu = ref<Page>('Overview')
const mobileMenuOpen = ref(false)
const collectorStatus = ref<'online' | 'offline' | 'checking'>('checking')
const commanderStatus = ref<'online' | 'offline' | 'checking'>('checking')
let timer: ReturnType<typeof setInterval> | undefined
let checking = false
let active = true

const groups: { title: string; pages: { key: Page; label: string }[] }[] = [
  { title: '运行', pages: [
    { key: 'Overview', label: '运行总览' },
    { key: 'Devices', label: '设备' },
    { key: 'Points', label: '测点' },
    { key: 'Tasks', label: '采集任务' },
    { key: 'Sinks', label: '数据输出' },
  ] },
  { title: '诊断', pages: [
    { key: 'Quality', label: '采集质量' },
    { key: 'Debug', label: '设备与测点诊断' },
    { key: 'SystemHealth', label: '系统健康' },
  ] },
]

function choosePage(key: string) {
  menu.value = key as Page
  mobileMenuOpen.value = false
}

async function checkServices() {
  if (checking) return
  checking = true
  try {
    const results = await Promise.allSettled([runtimeApi.collectorStatus(), runtimeApi.commanderStatus()])
    if (!active) return
    collectorStatus.value = results[0].status === 'fulfilled' ? 'online' : 'offline'
    commanderStatus.value = results[1].status === 'fulfilled' ? 'online' : 'offline'
  } finally {
    checking = false
  }
}
onMounted(() => {
  active = true
  void checkServices()
  timer = setInterval(() => void checkServices(), 5000)
})
onUnmounted(() => { active = false; if (timer) clearInterval(timer) })
</script>

<template>
  <el-container class="app-shell">
    <el-aside width="220px" class="app-sidebar admin-desktop-sidebar">
      <div class="brand"><b>WH</b><span>Wind Hub</span></div>
      <el-menu :default-active="menu" class="app-menu" @select="choosePage">
        <el-menu-item-group v-for="group in groups" :key="group.title" :title="group.title">
          <el-menu-item v-for="page in group.pages" :key="page.key" :index="page.key">{{ page.label }}</el-menu-item>
        </el-menu-item-group>
      </el-menu>
    </el-aside>
    <el-container class="app-main-shell">
      <el-header class="app-header">
        <el-button class="admin-mobile-menu" text aria-label="打开导航" @click="mobileMenuOpen = true">☰</el-button>
        <div class="header-title"><b>Wind Hub Admin</b><span>采集及控制系统管理平台</span></div>
        <div class="header-spacer" />
        <el-tag :type="collectorStatus === 'online' ? 'success' : collectorStatus === 'offline' ? 'danger' : 'info'">
          Collector {{ collectorStatus }}
        </el-tag>
        <el-tag :type="commanderStatus === 'online' ? 'success' : commanderStatus === 'offline' ? 'danger' : 'info'">
          Commander {{ commanderStatus }}
        </el-tag>
      </el-header>
      <el-main class="app-main">
        <section class="content">
          <OverviewPage v-if="menu === 'Overview'" />
          <DevicesPage v-else-if="menu === 'Devices'" />
          <PointsPage v-else-if="menu === 'Points'" />
          <TasksPage v-else-if="menu === 'Tasks'" />
          <SinksPage v-else-if="menu === 'Sinks'" />
          <SystemHealthPage v-else-if="menu === 'SystemHealth'" />
          <QualityPage v-else-if="menu === 'Quality'" />
          <DebugPage v-else />
        </section>
      </el-main>
    </el-container>
  </el-container>
  <el-drawer v-model="mobileMenuOpen" direction="ltr" size="min(300px, 90vw)" title="Wind Hub" class="mobile-nav-drawer">
    <el-menu :default-active="menu" class="app-menu" @select="choosePage">
      <el-menu-item-group v-for="group in groups" :key="group.title" :title="group.title">
        <el-menu-item v-for="page in group.pages" :key="page.key" :index="page.key">{{ page.label }}</el-menu-item>
      </el-menu-item-group>
    </el-menu>
  </el-drawer>
</template>

<style scoped>
.admin-mobile-menu { display: none; }
@media (max-width: 767px) {
  .admin-desktop-sidebar { display: none; }
  .admin-mobile-menu { display: inline-flex; }
}
</style>
