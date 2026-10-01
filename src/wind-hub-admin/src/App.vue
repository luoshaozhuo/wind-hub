<script setup lang="ts">
import { onMounted, ref } from 'vue'
import OverviewPage from './pages/OverviewPage.vue'
import DevicesPage from './pages/DevicesPage.vue'
import PointsPage from './pages/PointsPage.vue'
import TasksPage from './pages/TasksPage.vue'
import SinksPage from './pages/SinksPage.vue'
import QualityPage from './pages/QualityPage.vue'
import DebugPage from './pages/DebugPage.vue'
import SystemHealthPage from './pages/SystemHealthPage.vue'
import SettingsPage from './pages/SettingsPage.vue'
import ConfigPage from './pages/ConfigPage.vue'
import LogsPage from './pages/LogsPage.vue'
import { store } from './mock/data'
import { useViewport } from './composables/useViewport'
import { hydrateAdminStore } from './services/bootstrap'

const menu = ref('Devices')
const mobileNavOpen = ref(false)
const { isMobile } = useViewport()
const backendReady=ref(false)
const backendError=ref('')

onMounted(async()=>{
  try{await hydrateAdminStore();backendReady.value=true}
  catch(error){backendError.value=String(error)}
})

const runMenu = [
  { key: 'Overview', label: 'Overview' },
  { key: 'Devices', label: 'Devices' },
  { key: 'Points', label: 'Points' },
  { key: 'Tasks', label: 'Tasks' },
  { key: 'Sinks', label: 'Sinks' },
]

function selectMenu(key: string) {
  menu.value = key
  mobileNavOpen.value = false
}
</script>

<template>
  <el-container class="app-shell">
    <el-aside v-if="!isMobile" width="var(--app-sidebar-width)" class="app-sidebar">
      <div class="brand"><b>WH</b><span>Wind Hub</span></div>
      <el-menu :default-active="menu" class="app-menu" @select="selectMenu">
        <el-menu-item-group title="运行">
          <el-menu-item v-for="m in runMenu" :key="m.key" :index="m.key">{{ m.label }}</el-menu-item>
        </el-menu-item-group>
        <el-menu-item-group title="工程">
          <el-menu-item index="Quality">Quality</el-menu-item>
          <el-menu-item index="Debug">Diagnostics</el-menu-item>
          <el-menu-item index="SystemHealth">System Health</el-menu-item>
        </el-menu-item-group>
        <el-menu-item-group title="配置">
          <el-menu-item index="Settings">System Settings</el-menu-item>
          <el-menu-item index="Config">Configuration Files</el-menu-item>
        </el-menu-item-group>
        <el-menu-item-group title="系统">
          <el-menu-item index="Logs">Logs</el-menu-item>
        </el-menu-item-group>
      </el-menu>
    </el-aside>

    <el-container class="app-main-shell">
      <el-header class="app-header">
        <el-button v-if="isMobile" text class="mobile-menu-button" aria-label="Open navigation" @click="mobileNavOpen = true">☰</el-button>
        <div class="header-title">
          <b>Wind Hub Admin</b>
          <span>{{ store.systemInfo.siteName }} · 采集系统管理控制台</span>
        </div>
        <div class="header-spacer" />
        <div class="version-summary">
          <span class="version-item"><span class="version-label">Collector</span><b>{{ store.systemInfo.collectorVersion }}</b></span>
          <span class="version-separator">·</span>
          <span class="version-item"><span class="version-label">Admin</span><b>{{ store.systemInfo.adminVersion }}</b></span>
        </div>
        <el-tag type="success">{{ store.systemInfo.runtimeStatus }}</el-tag>
        <el-tag :type="backendReady?'success':backendError?'danger':'info'">{{backendReady?'backend':backendError?'offline':'connecting'}}</el-tag>
      </el-header>

      <el-main class="app-main">
        <section class="content">
          <OverviewPage v-if="menu === 'Overview'" />
          <DevicesPage v-if="menu === 'Devices'" />
          <PointsPage v-if="menu === 'Points'" />
          <TasksPage v-if="menu === 'Tasks'" />
          <SinksPage v-if="menu === 'Sinks'" />
          <QualityPage v-if="menu === 'Quality'" />
          <DebugPage v-if="menu === 'Debug'" />
          <SystemHealthPage v-if="menu === 'SystemHealth'" />
          <SettingsPage v-if="menu === 'Settings'" />
          <ConfigPage v-if="menu === 'Config'" />
          <LogsPage v-if="menu === 'Logs'" />
        </section>
      </el-main>
    </el-container>
  </el-container>

  <el-drawer v-model="mobileNavOpen" direction="ltr" size="var(--app-mobile-nav-width)" title="Wind Hub" class="mobile-nav-drawer">
    <el-menu :default-active="menu" @select="selectMenu">
      <el-menu-item-group title="运行">
        <el-menu-item v-for="m in runMenu" :key="m.key" :index="m.key">{{ m.label }}</el-menu-item>
      </el-menu-item-group>
      <el-menu-item-group title="工程">
        <el-menu-item index="Quality">Quality</el-menu-item>
        <el-menu-item index="Debug">Diagnostics</el-menu-item>
        <el-menu-item index="SystemHealth">System Health</el-menu-item>
      </el-menu-item-group>
      <el-menu-item-group title="配置">
        <el-menu-item index="Settings">System Settings</el-menu-item>
        <el-menu-item index="Config">Configuration Files</el-menu-item>
      </el-menu-item-group>
      <el-menu-item-group title="系统">
        <el-menu-item index="Logs">Logs</el-menu-item>
      </el-menu-item-group>
    </el-menu>
  </el-drawer>
</template>
