<script setup lang="ts">
import MetricCard from '../components/MetricCard.vue'
import LineChart from '../components/LineChart.vue'
import { trend } from '../mock/data'
</script>

<template>
  <div>
    <div class="page-head"><div><h1>Overview</h1><p>运行状态与采集健康摘要</p></div></div>
    <div class="metric-grid">
      <MetricCard title="在线设备" value="46 / 48" hint="2 台离线" />
      <MetricCard title="运行任务" value="12 / 12" hint="全部正常" />
      <MetricCard title="采集成功率" value="99.92%" hint="最近 15 min" />
      <MetricCard title="P95 采集延迟" value="83 ms" hint="最近 15 min" />
    </div>
    <el-card shadow="never" class="panel">
      <template #header>实时概览</template>
      <LineChart :x="trend.map(v => v.time)" :series="[{ name: 'Active power', data: trend.map(v => v.power) }]" />
    </el-card>
  </div>
</template>
