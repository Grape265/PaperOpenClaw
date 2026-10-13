<script setup lang="ts">
defineProps<{ title: string; hint?: string }>()
</script>

<template>
  <div class="empty">
    <div class="empty-title">{{ title }}</div>
    <p v-if="hint" class="empty-hint">{{ hint }}</p>
    <slot />
  </div>
</template>

<style scoped>
.empty-title {
  font-size: 14px;
  color: var(--text-dim);
  font-weight: 550;
}

.empty-hint {
  margin: 6px 0 0;
  font-size: 12.5px;
  color: var(--text-faint);
}
</style>
