<script setup lang="ts">
import { computed, ref } from 'vue'
import { useRoute } from 'vue-router'
import { Menu } from '@element-plus/icons-vue'

const route = useRoute()
const drawer = ref(false)
const menus = [
  ['Overview', '/overview'], ['Devices', '/devices'], ['Trends', '/trends'], ['Tasks', '/tasks'],
  ['Quality', '/quality'], ['Debug', '/debug'], ['Config', '/config'], ['Logs', '/logs'],
]
const active = computed(() => route.path)
</script>

<template>
  <div class="shell">
    <aside class="sidebar desktop-only">
      <div class="brand">Wind Hub</div>
      <el-menu :default-active="active" router class="nav-menu">
        <el-menu-item v-for="[label, path] in menus" :key="path" :index="path">{{ label }}</el-menu-item>
      </el-menu>
    </aside>

    <el-drawer v-model="drawer" direction="ltr" size="260px" :with-header="false">
      <div class="brand">Wind Hub</div>
      <el-menu :default-active="active" router @select="drawer = false">
        <el-menu-item v-for="[label, path] in menus" :key="path" :index="path">{{ label }}</el-menu-item>
      </el-menu>
    </el-drawer>

    <main class="main">
      <header class="topbar">
        <el-button class="mobile-only" text :icon="Menu" @click="drawer = true" />
        <div>
          <strong>Wind Hub Admin</strong>
          <span class="subtitle">Independent operations console</span>
        </div>
        <el-tag type="success">mock</el-tag>
      </header>
      <section class="content"><router-view /></section>
    </main>
  </div>
</template>
