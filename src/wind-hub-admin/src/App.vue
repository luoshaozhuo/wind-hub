<script setup lang="ts">
import { ref } from 'vue'
import OverviewPage from './pages/OverviewPage.vue'
import DevicesPage from './pages/DevicesPage.vue'
import PointsPage from './pages/PointsPage.vue'
import TasksPage from './pages/TasksPage.vue'
import QualityPage from './pages/QualityPage.vue'
import DebugPage from './pages/DebugPage.vue'
import ConfigPage from './pages/ConfigPage.vue'
import LogsPage from './pages/LogsPage.vue'
import { store } from './mock/data'

const menu = ref('Devices')

const runMenu = [
  { key: 'Overview', label: 'Overview' },
  { key: 'Devices', label: 'Devices' },
  { key: 'Points', label: 'Points' },
  { key: 'Tasks', label: 'Tasks' },
  { key: 'Quality', label: 'Quality' },
]
</script>

<template>
  <div class="shell">
    <aside>
      <div class="brand"><b>WH</b> Wind Hub</div>

      <div class="section">运行</div>
      <button
        v-for="m in runMenu"
        :key="m.key"
        :class="{ active: menu === m.key }"
        @click="menu = m.key"
      >
        {{ m.label }}
      </button>

      <div class="section">工程</div>
      <button :class="{ active: menu === 'Debug' }" @click="menu = 'Debug'">Debug</button>

      <div class="section">系统</div>
      <button
        v-for="m in ['Config', 'Logs']"
        :key="m"
        :class="{ active: menu === m }"
        @click="menu = m"
      >
        {{ m }}
      </button>
    </aside>

    <main>
      <header>
        <div class="header-title">
          <b>Wind Hub Admin</b>
          <span>{{ store.systemInfo.siteName }} · 采集系统管理控制台</span>
        </div>

        <div class="header-spacer"></div>

        <div class="version-summary">
          <span class="version-item">
            <span class="version-label">Collector</span>
            <b>{{ store.systemInfo.collectorVersion }}</b>
          </span>
          <span class="version-separator">·</span>
          <span class="version-item">
            <span class="version-label">Admin</span>
            <b>{{ store.systemInfo.adminVersion }}</b>
          </span>
        </div>

        <el-tag type="success">{{ store.systemInfo.runtimeStatus }}</el-tag>
        <el-tag type="warning">mock</el-tag>
      </header>

      <section class="content">
        <OverviewPage v-if="menu === 'Overview'" />
        <DevicesPage v-if="menu === 'Devices'" />
        <PointsPage v-if="menu === 'Points'" />
        <TasksPage v-if="menu === 'Tasks'" />
        <QualityPage v-if="menu === 'Quality'" />
        <DebugPage v-if="menu === 'Debug'" />
        <ConfigPage v-if="menu === 'Config'" />
        <LogsPage v-if="menu === 'Logs'" />
      </section>
    </main>
  </div>
</template>
