<script setup lang="ts">
withDefaults(defineProps<{
  title: string
  description?: string
}>(), { description: '' })
</script>

<template>
  <div class="admin-page-heading">
    <div class="admin-page-heading__text">
      <h2>{{ title }}</h2>
      <p v-if="description">{{ description }}</p>
    </div>
    <div class="admin-page-heading__actions"><slot name="actions" /></div>
  </div>
</template>

<style scoped>
.admin-page-heading {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: var(--space-4, 16px);
  flex-wrap: wrap;
  margin-bottom: var(--space-5, 20px);
}
.admin-page-heading__text { min-width: 0; flex: 1; }
.admin-page-heading h2 { margin: 0; }
.admin-page-heading p { margin: 8px 0 0; color: var(--el-text-color-secondary); }
.admin-page-heading__actions { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
</style>
