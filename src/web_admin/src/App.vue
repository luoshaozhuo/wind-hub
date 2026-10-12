<script setup lang="ts">
import { ref } from 'vue'
import DevicesPage from './pages/DevicesPage.vue'
import TasksPage from './pages/TasksPage.vue'
import SinksPage from './pages/SinksPage.vue'

const menu = ref('Devices')
</script>

<template>
  <el-container class="app-shell">
    <el-aside width="220px" class="app-sidebar">
      <div class="brand"><b>WH</b><span>Wind Hub</span></div>
      <el-menu :default-active="menu" @select="(key: string) => (menu = key)">
        <el-menu-item index="Devices">Devices</el-menu-item>
        <el-menu-item index="Tasks">Tasks</el-menu-item>
        <el-menu-item index="Sinks">Sinks</el-menu-item>
      </el-menu>
    </el-aside>
    <el-main>
      <DevicesPage v-if="menu === 'Devices'" />
      <TasksPage v-else-if="menu === 'Tasks'" />
      <SinksPage v-else />
    </el-main>
  </el-container>
</template>
