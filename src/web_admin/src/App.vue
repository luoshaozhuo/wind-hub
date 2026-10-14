<script setup lang="ts">
import { ref } from 'vue'
import OverviewPage from './pages/OverviewPage.vue'
import DevicesPage from './pages/DevicesPage.vue'
import PointsPage from './pages/PointsPage.vue'
import TasksPage from './pages/TasksPage.vue'
import SinksPage from './pages/SinksPage.vue'

type Page = 'Overview' | 'Devices' | 'Points' | 'Tasks' | 'Sinks'
const menu = ref<Page>('Overview')
</script>

<template>
  <el-container class="app-shell">
    <el-aside width="220px" class="app-sidebar">
      <div class="brand"><b>WH</b><span>Wind Hub</span></div>
      <el-menu :default-active="menu" @select="(key: string) => menu = key as Page">
        <el-menu-item-group title="运行">
          <el-menu-item index="Overview">运行总览</el-menu-item>
          <el-menu-item index="Devices">设备</el-menu-item>
          <el-menu-item index="Points">测点</el-menu-item>
          <el-menu-item index="Tasks">采集任务</el-menu-item>
          <el-menu-item index="Sinks">数据输出</el-menu-item>
        </el-menu-item-group>
      </el-menu>
    </el-aside>
    <el-main>
      <OverviewPage v-if="menu === 'Overview'" />
      <DevicesPage v-else-if="menu === 'Devices'" />
      <PointsPage v-else-if="menu === 'Points'" />
      <TasksPage v-else-if="menu === 'Tasks'" />
      <SinksPage v-else />
    </el-main>
  </el-container>
</template>
